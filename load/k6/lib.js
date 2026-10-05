// Shared helpers for the k6 scenarios in this directory. Loaded by
// `jubilo-cli load run <scenario>`, which supplies BASE_URL, TOKENS_FILE
// and TARGET (see services/load.py).
//
// Each k6 VU runs its own copy of this module, so the module-level
// `current` below is per-VU state: a VU is one simulated user, pinned to
// one load-test account for the whole run (round-robin over the minted
// tokens if there are more VUs than accounts -- then two VUs share one
// account and its 60/min per-user throttle, which shows up as 429s).

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

export const BASE_URL = __ENV.BASE_URL;
export const AUTH = `${BASE_URL}/auth`;
export const MUSIC = `${BASE_URL}/api/music`;
export const CHURCH = `${BASE_URL}/api/church`;

const tokens = JSON.parse(open(__ENV.TOKENS_FILE));
export const USER_COUNT = tokens.users.length;

export const throttled = new Counter('jubilo_throttled_429');
export const refreshes = new Counter('jubilo_token_refreshes');
export const refreshFailures = new Counter('jubilo_token_refresh_failures');
export const serverErrors = new Counter('jubilo_server_errors_5xx');

let current = null;

function user() {
	if (current === null) {
		const u = tokens.users[(__VU - 1) % USER_COUNT];
		current = { email: u.email, access: u.access_token, refresh: u.refresh_token, expiresAt: u.expires_at };
	}
	return current;
}

// One /o/token refresh. jubilo-auth limits that endpoint per credential
// (10/min each, since 2026-10-05; per source IP before that), so a 429
// here means this VU's own token is being refreshed too often -- back
// off a random 15-45s and tell the caller to skip this iteration.
export function refreshToken() {
	const u = user();
	if (!u.refresh) {
		refreshFailures.add(1);
		return false;
	}
	const res = http.post(`${AUTH}/o/token`, {
		grant_type: 'refresh_token',
		refresh_token: u.refresh,
		client_id: tokens.client_id,
	}, { tags: { name: 'auth /o/token (refresh)' } });
	if (res.status === 429) {
		throttled.add(1, { name: 'auth /o/token (refresh)' });
		sleep(15 + Math.random() * 30);
		return false;
	}
	if (res.status !== 200) {
		refreshFailures.add(1);
		return false;
	}
	const body = res.json();
	u.access = body.access_token;
	u.refresh = body.refresh_token || u.refresh;
	u.expiresAt = Math.floor(Date.now() / 1000) + (body.expires_in || 900);
	refreshes.add(1);
	// jubilo-auth rotates refresh tokens (ROTATE_REFRESH_TOKEN, no grace
	// period), so the one in tokens.json is now dead. VUs cannot write
	// files; this line is picked out of k6's JSON log by services/load.py
	// after the run and merged back into tokens.json.
	console.log('JUBILO_TOKEN_ROTATED ' + JSON.stringify({ email: u.email, access_token: u.access, refresh_token: u.refresh, expires_at: u.expiresAt }));
	return true;
}

// Authenticated GET with the same handling the app's authFetch has: a 401
// (token expired) triggers one refresh and one retry. Proactively refreshes
// a token known to expire within 30s so the 401 path is the exception.
export function authGet(url, name, extraTags) {
	const u = user();
	const tags = Object.assign({ name }, extraTags || {});
	if (u.expiresAt - Math.floor(Date.now() / 1000) < 30) {
		refreshToken();
	}
	let res = http.get(url, { headers: { Authorization: `Bearer ${u.access}` }, tags });
	if (res.status === 401 && refreshToken()) {
		res = http.get(url, { headers: { Authorization: `Bearer ${u.access}` }, tags });
	}
	if (res.status === 429) throttled.add(1, { name });
	if (res.status >= 500) serverErrors.add(1, { name });
	check(res, { [`${name} 2xx`]: (r) => r.status >= 200 && r.status < 300 });
	return res;
}

// Response bodies are either a bare array or DRF-paginated {results: [...]}.
export function items(res) {
	if (res.status < 200 || res.status >= 300) return [];
	try {
		const body = res.json();
		if (Array.isArray(body)) return body;
		if (body && Array.isArray(body.results)) return body.results;
		if (body && Array.isArray(body.hits)) return body.hits;
	} catch (e) { /* not JSON */ }
	return [];
}

export function pick(list) {
	return list[Math.floor(Math.random() * list.length)];
}

export function think(minSeconds, maxSeconds) {
	sleep(minSeconds + Math.random() * (maxSeconds - minSeconds));
}
