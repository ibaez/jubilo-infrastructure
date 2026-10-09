// Finds the breaking point of one layer with an open (arrival-rate)
// model: requests arrive at a fixed, climbing rate regardless of how slow
// responses get, which is what a crowd does. Every request is tagged with
// the stage (target rate) it was sent in, and each stage gets its own
// p95 / failure-rate line in the summary, so the output reads as a table
// of "at N requests/s, latency was X and Y% failed". The run aborts
// itself once p95 passes 2 s for 20 s (latency collapse = the queue in
// front of gunicorn's sync workers has formed); the last clean stage is
// the ceiling.
//
//   ./jubilo-cli load run ceiling MODE=church   (default) one cheap authenticated
//                                                 jubilo-church GET: full path through
//                                                 Railway edge, gateway, gunicorn,
//                                                 token cache, Postgres. jubilo-church
//                                                 has no per-user throttle, so this is
//                                                 the only Django path that can be pushed
//                                                 past 60/min per account.
//   ./jubilo-cli load run ceiling MODE=gateway    GET /invite/ -- static file from nginx:
//                                                 Railway edge + gateway only, no Django.
//   ./jubilo-cli load run ceiling MODE=music      GET /api/music/topic (Redis-cached).
//                                                 Throttled at 60/min per account, so
//                                                 the usable ceiling is ~1 rps x accounts
//                                                 and 429s are expected beyond that.
//   ./jubilo-cli load run ceiling MODE=introspect POST /auth/o/introspect as a resource
//                                                 server would (Basic auth with a
//                                                 service's client credentials, the
//                                                 token in the body). Needs
//                                                 CLIENT_ID=... CLIENT_SECRET=... -- in
//                                                 dev, jubilo_auth's own
//                                                 JUBILO_MUSIC_CLIENT_ID/_SECRET. What
//                                                 it measures is the per-call client-
//                                                 secret check (design_docs/2026-10-08-
//                                                 client-secret-hasher.md in jubilo-
//                                                 auth): the minted tokens are long
//                                                 expired, which costs exactly the same
//                                                 and answers {"active": false} with a
//                                                 200, so nothing here depends on
//                                                 refreshing them -- run with
//                                                 JUBILO_LOAD_SKIP_REFRESH=1 in the
//                                                 environment to skip the runner's
//                                                 serial refresh pre-flight (~8 min
//                                                 once the 500 tokens have aged out).
//
// Knobs (KEY=VALUE after the scenario name):
//   STAGES  comma-separated target rates (default 25,50,100,150,200,PEAK)
//   PEAK    last default stage (default 300)
//   STEP    duration of each stage (default 1m)
//   CLIENT_ID / CLIENT_SECRET  MODE=introspect only, see above
//   INTROSPECT_URL  MODE=introspect only: jubilo-auth reached directly, not
//                   through the gateway (which 403s this path) -- see the
//                   comment at INTROSPECT_URL below for the dev value
//
// VUs are capped well below the point where thousands of them would share
// the ~100 minted accounts and race each other's token refreshes; when the
// target is saturated the symptom is dropped_iterations (k6 could not send
// the rate) and a climbing p95, which is the signal we want, not a VU
// explosion.

import http from 'k6/http';
import exec from 'k6/execution';
import encoding from 'k6/encoding';
import { check } from 'k6';
import { AUTH, BASE_URL, CHURCH, MUSIC, authGet, serverErrors, throttled } from './lib.js';

const MODE = __ENV.MODE || 'church';

// MODE=introspect: every VU introspects one of the minted tokens with the
// service credentials passed in. Read here (init context) once per VU;
// lib.js keeps its own copy private, so this re-reads the same file.
//
// Never through the gateway: both gateway configs return 403 for
// /auth/o/introspect (it is internal-only, reached by music/church over
// the private network), so the first run of this mode measured nginx's
// 403 at 2 ms and nothing else. INTROSPECT_URL points straight at
// jubilo-auth the way a service does -- in dev the container publishes
// port 8000, so INTROSPECT_URL=http://192.168.86.15:8000/auth/o/introspect
// (the LAN IP, which is in auth's ALLOWED_HOSTS; localhost is not).
const INTROSPECT_URL = __ENV.INTROSPECT_URL || `${AUTH}/o/introspect`;
const INTROSPECT_TOKENS = MODE === 'introspect' ? JSON.parse(open(__ENV.TOKENS_FILE)).users.map((u) => u.access_token) : [];
const INTROSPECT_AUTH = MODE === 'introspect'
	? 'Basic ' + encoding.b64encode(`${__ENV.CLIENT_ID || ''}:${__ENV.CLIENT_SECRET || ''}`)
	: null;
const PEAK = parseInt(__ENV.PEAK || '300', 10);
const STEP = __ENV.STEP || '1m';
const STAGES = (__ENV.STAGES ? __ENV.STAGES.split(',').map((s) => parseInt(s, 10)) : [25, 50, 100, 150, 200, PEAK])
	.filter((r, i, a) => r > 0 && a.indexOf(r) === i);

const STEP_MS = (() => {
	const m = /^(\d+)(ms|s|m|h)$/.exec(STEP);
	const mult = { ms: 1, s: 1000, m: 60000, h: 3600000 }[m ? m[2] : 's'];
	return (m ? parseInt(m[1], 10) : 60) * mult;
})();

const thresholds = {
	http_req_duration: [{ threshold: 'p(95)<2000', abortOnFail: true, delayAbortEval: '20s' }],
	http_req_failed: ['rate<0.05'],
	dropped_iterations: ['count<1'],
};
for (const r of STAGES) {
	thresholds[`http_req_duration{stage:${r}}`] = ['p(95)<2000'];
	thresholds[`http_req_failed{stage:${r}}`] = ['rate<0.05'];
}

export const options = {
	scenarios: {
		ceiling: {
			executor: 'ramping-arrival-rate',
			startRate: 10,
			timeUnit: '1s',
			preAllocatedVUs: 50,
			maxVUs: 300,
			stages: STAGES.map((target) => ({ duration: STEP, target })),
		},
	},
	thresholds,
	summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function currentStage() {
	const idx = Math.min(STAGES.length - 1, Math.floor(exec.instance.currentTestRunDuration / STEP_MS));
	return String(STAGES[idx]);
}

export default function () {
	const stage = currentStage();
	if (MODE === 'gateway') {
		const res = http.get(`${BASE_URL}/invite/`, { tags: { name: 'gateway /invite/', stage } });
		if (res.status === 429) throttled.add(1);
		if (res.status >= 500) serverErrors.add(1);
		check(res, { 'gateway /invite/ 200': (r) => r.status === 200 });
	} else if (MODE === 'music') {
		authGet(`${MUSIC}/topic`, 'music /topic', { stage });
	} else if (MODE === 'introspect') {
		const token = INTROSPECT_TOKENS[(__VU - 1) % INTROSPECT_TOKENS.length];
		const res = http.post(INTROSPECT_URL, { token }, {
			headers: { Authorization: INTROSPECT_AUTH },
			tags: { name: 'auth /o/introspect', stage },
		});
		if (res.status === 429) throttled.add(1);
		if (res.status >= 500) serverErrors.add(1);
		// 200 whether the token is active or not -- the client-secret check,
		// which is the cost under test, happens either way.
		check(res, { 'auth /o/introspect 200': (r) => r.status === 200 });
	} else {
		authGet(`${CHURCH}/event?when=upcoming`, 'church /event?when=upcoming', { stage });
	}
}
