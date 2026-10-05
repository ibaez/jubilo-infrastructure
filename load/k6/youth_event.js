// Realistic event-crowd traffic: each VU is one attendee with the app
// open, doing what the app actually does (closed model -- a user waits
// for a response, thinks, then acts). The request mix is taken from the
// screens in jubilo-mobile:
//
//   app boot (app/_layout + (home)/home.tsx): /auth/user/me, church
//     /participant/me, /event?when=recent, /event?when=upcoming, music
//     /playlist, /hymn/next-autoplay
//   browsing ((music) tab + hymn/[hymn_id]): /search?type=hymn, /hymn/<id>,
//     /hymn/<id>/audio/download-url (the audio itself is a presigned R2
//     URL the app fetches from Cloudflare, never through us), /topic,
//     /hymn-lyric/recently-updated, /hymn-audio/recently-added
//   photos ((church) tab, event screen): /event/<id>/pictures (images are
//     presigned R2 URLs, same as audio)
//
// One session is ~15 requests over 60-90s, under jubilo-music's 60/min
// per-user throttle (search has its own 120/min scope). Shared accounts
// (more VUs than minted tokens) or a shorter think time will push a user
// over it, and that shows up as jubilo_throttled_429 -- which is a
// finding about the throttle, not about capacity.
//
// Knobs (KEY=VALUE after the scenario name):
//   VUS   peak concurrent attendees (default 50)
//   RAMP  time to reach VUS (default 2m)   HOLD  time at VUS (default 5m)
//   THINK_MIN / THINK_MAX  seconds between actions (default 4 / 10)
//
// Pass/fail: p95 under 1s, p99 under 2.5s, under 1% failed requests
// (DRF 429s count as failures, deliberately).

import { AUTH, CHURCH, MUSIC, authGet, items, pick, think } from './lib.js';

const VUS = parseInt(__ENV.VUS || '50', 10);
const RAMP = __ENV.RAMP || '2m';
const HOLD = __ENV.HOLD || '5m';
const THINK_MIN = parseFloat(__ENV.THINK_MIN || '4');
const THINK_MAX = parseFloat(__ENV.THINK_MAX || '10');

// Common words in the Spanish hymnal -- enough spread to exercise
// Meilisearch rather than one cached query.
const SEARCH_TERMS = ['dios', 'señor', 'gloria', 'amor', 'cristo', 'alabanza', 'santo', 'cielo', 'gracia', 'fe', 'paz', 'jesus', 'rey', 'luz', 'vida'];

export const options = {
	scenarios: {
		attendees: {
			executor: 'ramping-vus',
			startVUs: 0,
			stages: [
				{ duration: RAMP, target: VUS },
				{ duration: HOLD, target: VUS },
				{ duration: '30s', target: 0 },
			],
			gracefulRampDown: '30s',
		},
	},
	thresholds: {
		http_req_failed: ['rate<0.01'],
		http_req_duration: ['p(95)<1000', 'p(99)<2500'],
		'http_req_duration{name:music /search}': ['p(95)<1500'],
		'http_req_duration{name:music /hymn/<id>}': ['p(95)<1000'],
	},
	summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function boot() {
	authGet(`${AUTH}/user/me`, 'auth /user/me');
	authGet(`${CHURCH}/participant/me`, 'church /participant/me');
	const recent = authGet(`${CHURCH}/event?when=recent`, 'church /event?when=recent');
	authGet(`${CHURCH}/event?when=upcoming`, 'church /event?when=upcoming');
	authGet(`${MUSIC}/playlist`, 'music /playlist');
	authGet(`${MUSIC}/hymn/next-autoplay`, 'music /hymn/next-autoplay');
	return items(recent);
}

function browseHymn() {
	const res = authGet(`${MUSIC}/search?q=${encodeURIComponent(pick(SEARCH_TERMS))}&type=hymn&offset=0`, 'music /search');
	const hits = items(res);
	think(THINK_MIN, THINK_MAX);
	if (hits.length === 0) return;
	const hit = pick(hits);
	const id = hit.hymn_id !== undefined ? hit.hymn_id : hit.id;
	if (id === undefined) return;
	const detail = authGet(`${MUSIC}/hymn/${id}`, 'music /hymn/<id>');
	think(THINK_MIN, THINK_MAX);
	// The app only offers playback (and so only asks for the presigned
	// URL) when the hymn detail says it has audio; asking otherwise is a
	// 404 by design, not a failure of the service.
	let hasAudio = false;
	try { hasAudio = detail.status === 200 && detail.json().has_audio === true; } catch (e) { /* not JSON */ }
	if (hasAudio && Math.random() < 0.6) {
		authGet(`${MUSIC}/hymn/${id}/audio/download-url`, 'music /hymn/<id>/audio/download-url');
		think(THINK_MIN, THINK_MAX);
	}
}

export default function () {
	const events = boot();
	think(THINK_MIN, THINK_MAX);

	const actions = 3 + Math.floor(Math.random() * 3);
	for (let i = 0; i < actions; i++) {
		const r = Math.random();
		if (r < 0.65) {
			browseHymn();
		} else if (r < 0.80) {
			authGet(`${MUSIC}/topic`, 'music /topic');
			think(THINK_MIN, THINK_MAX);
		} else if (r < 0.90) {
			authGet(`${MUSIC}/hymn-lyric/recently-updated`, 'music /hymn-lyric/recently-updated');
			authGet(`${MUSIC}/hymn-audio/recently-added`, 'music /hymn-audio/recently-added');
			think(THINK_MIN, THINK_MAX);
		} else if (events.length > 0) {
			authGet(`${CHURCH}/event/${pick(events).id}/pictures`, 'church /event/<id>/pictures');
			think(THINK_MIN, THINK_MAX);
		}
	}
}
