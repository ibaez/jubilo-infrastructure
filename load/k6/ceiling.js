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
//
// Knobs (KEY=VALUE after the scenario name):
//   STAGES  comma-separated target rates (default 25,50,100,150,200,PEAK)
//   PEAK    last default stage (default 300)
//   STEP    duration of each stage (default 1m)
//
// VUs are capped well below the point where thousands of them would share
// the ~100 minted accounts and race each other's token refreshes; when the
// target is saturated the symptom is dropped_iterations (k6 could not send
// the rate) and a climbing p95, which is the signal we want, not a VU
// explosion.

import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { BASE_URL, CHURCH, MUSIC, authGet, serverErrors, throttled } from './lib.js';

const MODE = __ENV.MODE || 'church';
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
	} else {
		authGet(`${CHURCH}/event?when=upcoming`, 'church /event?when=upcoming', { stage });
	}
}
