"""
Load testing against a running Jubilo stack -- local dev (docker-compose)
or production (Railway, through the public gateway). Driven by
`jubilo-cli load ...`; the plan, the numbers it is checking against and
the runbook live in design_docs/2026-10-04-load-testing-plan.md.

Three steps, each its own command:

  ./jubilo-cli load users 50          provision load-test accounts
  ./jubilo-cli load tokens            mint a token per account -> load/tokens.json
  ./jubilo-cli load run youth_event   drive k6 with load/k6/<scenario>.js

Target selection is by environment variable so the same commands work
against both stacks and nothing defaults to production:

  JUBILO_LOAD_TARGET=dev   (default) https://<DOCKER_HOST_IP>, our own CA,
                           tokens via the dev-only jubilo_postman password
                           grant (same as services/e2e.py)
  JUBILO_LOAD_TARGET=prod  https://www.mijubilo.com, real TLS, tokens via
                           the mobile app's real login + PKCE flow, since
                           production deliberately has no password-grant
                           client (jubilo-auth/scripts/auth_prod_setup.py)

Token minting is paced gently and backs off on 429, but since
2026-10-05 it no longer has to crawl: jubilo-auth's /auth/o/token
throttle is keyed per credential (refresh token / code / account), not
per source IP, and /auth/login only counts FAILED attempts per IP --
exactly because a venue's Wi-Fi puts every attendee behind one IP, which
this harness was the first to run into (one laptop looked like a venue).
Before that change one IP could mint at most 10 tokens/min, which with
15-minute access tokens capped a single generator at ~150 live users.
The k6 scripts refresh expired tokens themselves (one /o/token call
each) and the CLI saves the rotated refresh tokens back after each run,
so the minting pass only has to happen once per session.

Nothing here writes to any service's data except: load-test users
(jubilo-auth/scripts/load_test_users.py, explicit and reversible) and
the OAuth token rows minting creates (expire on their own). Every k6
scenario is GET-only.
"""

import base64
import csv
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from services.docker import docker_get_host_ip, docker_run, _read_env_file

INFRA_ROOT = Path(__file__).resolve().parent.parent
LOAD_DIR = INFRA_ROOT / "load"
K6_DIR = LOAD_DIR / "k6"
USERS_FILE = LOAD_DIR / "users.csv"
TOKENS_FILE = LOAD_DIR / "tokens.json"
RESULTS_DIR = LOAD_DIR / "results"
CA_CERT = str(INFRA_ROOT / "rootCA.pem")
AUTH_ENV_PATH = INFRA_ROOT.parent / "jubilo-auth" / ".env"

PROD_BASE_URL = "https://www.mijubilo.com"
# jubilo_mobile's client id in production -- public client, same value the
# app ships with (jubilo-mobile/eas.json, "production" profile).
PROD_MOBILE_CLIENT_ID = "VE73YX895NmwfK4Ydfi3dkqsoK_uBQx92LgtqEd7zTo"
MOBILE_REDIRECT_URI = "jubilo-mobile://oauthredirect"
TOKEN_SCOPE = "auth music church"

# Pacing between token requests. jubilo-auth's /o/token limit is per
# credential (10/min each) and login's per-IP counter only counts
# failures, so a generator minting one token per account is never the
# thing being limited; a short gap just keeps auth's token work from
# landing as one burst, and 429 still backs off for a full window.
MINT_PACE_SECONDS = 0.7
THROTTLE_BACKOFF_SECONDS = 61
HTTP_TIMEOUT_SECONDS = 15


# Progress lines are the whole point of the slow, paced steps here (minting,
# refreshing); keep them visible when stdout is a pipe or a file, not
# block-buffered until exit.
sys.stdout.reconfigure(line_buffering=True)


class LoadFailure(Exception):
	pass


def _target():
	target = os.environ.get("JUBILO_LOAD_TARGET", "dev").strip().lower()
	if target not in ("dev", "prod"):
		raise LoadFailure(f"JUBILO_LOAD_TARGET must be 'dev' or 'prod', got {target!r}")
	return target


def _base_url(target):
	if target == "prod":
		return os.environ.get("JUBILO_LOAD_BASE_URL", PROD_BASE_URL).rstrip("/")
	return f"https://{docker_get_host_ip()}"


def _verify(target):
	return True if target == "prod" else CA_CERT


def _auth_flow(target):
	"""'pkce' (what the app does; the only option in prod) or 'password'
	(dev-only jubilo_postman grant). JUBILO_LOAD_AUTH_FLOW=pkce forces the
	production flow against dev to rehearse it."""
	if target == "prod":
		return "pkce"
	flow = os.environ.get("JUBILO_LOAD_AUTH_FLOW", "password").strip().lower()
	if flow not in ("pkce", "password"):
		raise LoadFailure(f"JUBILO_LOAD_AUTH_FLOW must be 'pkce' or 'password', got {flow!r}")
	return flow


def _pkce_client_id(target):
	if target == "prod":
		return os.environ.get("JUBILO_LOAD_CLIENT_ID", PROD_MOBILE_CLIENT_ID)
	auth_env, _ = _read_env_file(str(AUTH_ENV_PATH))
	client_id = auth_env.get("JUBILO_MOBILE_CLIENT_ID")
	if not client_id:
		raise LoadFailure(f"JUBILO_MOBILE_CLIENT_ID not found in {AUTH_ENV_PATH} -- run `jubilo-cli dev setup` first.")
	return client_id


def _read_users():
	if not USERS_FILE.exists():
		raise LoadFailure(
			f"{USERS_FILE} not found -- run `jubilo-cli load users <N>` first (dev), or paste the "
			f"`email,password` lines printed by `python -m scripts.load_test_users create` in the Railway "
			f"Console into that file (prod)."
		)
	users = []
	with USERS_FILE.open(newline="") as f:
		for row in csv.reader(f):
			if not row or row[0].startswith("#"):
				continue
			if len(row) != 2:
				raise LoadFailure(f"{USERS_FILE}: expected `email,password` per line, got {row!r}")
			users.append({"email": row[0].strip(), "password": row[1].strip()})
	if not users:
		raise LoadFailure(f"{USERS_FILE} has no users")
	return users


# ------------------------------
# load users
#
def load_users(service_name_list=None):
	target = _target()
	args = list(service_name_list or [])

	if args and args[0] == "delete":
		if target == "prod":
			print("Production: run this in the Railway Console for jubilo-auth:\n")
			print("    python -m scripts.load_test_users delete\n")
			return
		docker_run("auth", "python -m scripts.load_test_users delete")
		if USERS_FILE.exists():
			USERS_FILE.unlink()
		return

	count = int(args[0]) if args else 20
	password = os.environ.get("JUBILO_LOAD_USER_PASSWORD") or ("Lt-" + secrets.token_urlsafe(12))

	if target == "prod":
		print("Production: load-test users are provisioned by hand (Railway Console for jubilo-auth):\n")
		print(f"    python -m scripts.load_test_users create {count} --password '{password}'\n")
		print(f"Paste its `email,password` output lines into {USERS_FILE}, then run `jubilo-cli load tokens`.")
		print("When done: `python -m scripts.load_test_users delete` in the same console.")
		return

	# Dev: run the same script inside the jubilo_auth container and keep the
	# credentials it prints. docker_run streams output, so capture it via a
	# plain subprocess instead to parse the CSV lines.
	LOAD_DIR.mkdir(exist_ok=True)
	cmd = (
		f"docker compose run --rm -e LOAD_TEST_USER_PASSWORD jubilo_auth "
		f"python -m scripts.load_test_users create {count}"
	)
	env = {**os.environ, "LOAD_TEST_USER_PASSWORD": password}
	result = subprocess.run(cmd, shell=True, cwd=INFRA_ROOT, env=env, capture_output=True, text=True)
	if result.returncode != 0:
		print(result.stdout)
		print(result.stderr, file=sys.stderr)
		raise LoadFailure("load_test_users create failed")

	lines = [l for l in result.stdout.splitlines() if re.match(r"^loadtest-\d{4}@", l)]
	if len(lines) != count:
		print(result.stdout)
		print(result.stderr, file=sys.stderr)
		raise LoadFailure(f"expected {count} credential lines, got {len(lines)}")
	USERS_FILE.write_text("\n".join(lines) + "\n")
	print(f"Wrote {len(lines)} users to {USERS_FILE}")


# ------------------------------
# load tokens
#
def _post_token(base_url, verify, data):
	"""POST /auth/o/token, retrying after the per-IP throttle window on 429."""
	while True:
		response = requests.post(f"{base_url}/auth/o/token", data=data, verify=verify, timeout=HTTP_TIMEOUT_SECONDS)
		if response.status_code == 429:
			print(f"  /o/token throttled, waiting {THROTTLE_BACKOFF_SECONDS}s...")
			time.sleep(THROTTLE_BACKOFF_SECONDS)
			continue
		if response.status_code != 200:
			raise LoadFailure(f"/o/token -> {response.status_code}: {response.text[:300]}")
		payload = response.json()
		if "access_token" not in payload:
			raise LoadFailure(f"/o/token response missing access_token: {payload}")
		return payload


def _mint_dev(base_url, verify, user):
	auth_env, _ = _read_env_file(str(AUTH_ENV_PATH))
	client_id = auth_env.get("JUBILO_POSTMAN_CLIENT_ID")
	if not client_id:
		raise LoadFailure(f"JUBILO_POSTMAN_CLIENT_ID not found in {AUTH_ENV_PATH} -- run `jubilo-cli dev setup` first.")
	payload = _post_token(base_url, verify, {
		"grant_type": "password",
		"username": user["email"],
		"password": user["password"],
		"client_id": client_id,
		"scope": TOKEN_SCOPE,
	})
	return payload, client_id


def _mint_prod(base_url, verify, user, client_id):
	"""
	Replays exactly what the mobile app does (jubilo-mobile/lib/auth/
	authClient.ts login()): a session login on /auth/login, then
	/auth/o/authorize with a PKCE S256 challenge -- jubilo_mobile is
	registered --skip-authorization, so a live session is redirected
	straight to jubilo-mobile://oauthredirect?code=... -- then the code is
	exchanged on /auth/o/token with the verifier. No browser needed; the
	custom-scheme redirect is read from the Location header instead of
	followed.
	"""
	verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
	challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
	authorize_url = f"{base_url}/auth/o/authorize/?" + urlencode({
		"response_type": "code",
		"client_id": client_id,
		"redirect_uri": MOBILE_REDIRECT_URI,
		"scope": TOKEN_SCOPE,
		"code_challenge": challenge,
		"code_challenge_method": "S256",
	})
	login_url = f"{base_url}/auth/login"

	session = requests.Session()
	session.verify = verify

	while True:
		page = session.get(login_url, params={"next": authorize_url}, timeout=HTTP_TIMEOUT_SECONDS)
		if page.status_code == 429:
			print(f"  /auth/login throttled, waiting {THROTTLE_BACKOFF_SECONDS}s...")
			time.sleep(THROTTLE_BACKOFF_SECONDS)
			continue
		page.raise_for_status()
		match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
		if not match:
			raise LoadFailure("login page has no csrfmiddlewaretoken")
		login = session.post(
			login_url,
			params={"next": authorize_url},
			data={"csrfmiddlewaretoken": match.group(1), "username": user["email"], "password": user["password"]},
			headers={"Referer": page.url},
			allow_redirects=False,
			timeout=HTTP_TIMEOUT_SECONDS,
		)
		if login.status_code == 429:
			print(f"  /auth/login throttled, waiting {THROTTLE_BACKOFF_SECONDS}s...")
			time.sleep(THROTTLE_BACKOFF_SECONDS)
			continue
		break

	if login.status_code != 302:
		raise LoadFailure(f"login for {user['email']} did not redirect (HTTP {login.status_code}) -- wrong password, or the account is locked out")

	authorize = session.get(authorize_url, allow_redirects=False, timeout=HTTP_TIMEOUT_SECONDS)
	if authorize.status_code != 302 or not authorize.headers.get("Location", "").startswith(MOBILE_REDIRECT_URI):
		raise LoadFailure(f"/o/authorize -> {authorize.status_code} {authorize.headers.get('Location', '')[:120]}: {authorize.text[:200]}")
	code = parse_qs(urlparse(authorize.headers["Location"]).query).get("code", [None])[0]
	if not code:
		raise LoadFailure("authorize redirect carried no code")

	# End the browser-style session the way the app does (sessionLogoutEndpoint)
	# so no server-side session lingers for a load-test account.
	session.post(f"{base_url}/auth/logout", allow_redirects=False, timeout=HTTP_TIMEOUT_SECONDS)

	payload = _post_token(base_url, verify, {
		"grant_type": "authorization_code",
		"code": code,
		"redirect_uri": MOBILE_REDIRECT_URI,
		"client_id": client_id,
		"code_verifier": verifier,
	})
	return payload


def load_tokens(service_name_list=None):
	target = _target()
	base_url = _base_url(target)
	verify = _verify(target)
	users = _read_users()

	flow = _auth_flow(target)
	pace = MINT_PACE_SECONDS
	client_id = _pkce_client_id(target) if flow == "pkce" else None

	print(f"Minting tokens for {len(users)} users against {base_url} ({target}, {flow} flow)...")

	minted = []
	started = time.time()
	for index, user in enumerate(users, start=1):
		if index > 1:
			time.sleep(pace)
		if flow == "pkce":
			payload = _mint_prod(base_url, verify, user, client_id)
		else:
			payload, client_id = _mint_dev(base_url, verify, user)
		minted.append({
			"email": user["email"],
			"access_token": payload["access_token"],
			"refresh_token": payload.get("refresh_token"),
			"expires_at": int(time.time()) + int(payload.get("expires_in", 900)),
			"scope": payload.get("scope", ""),
		})
		print(f"  [{index}/{len(users)}] {user['email']} ok (scope: {payload.get('scope', '')})")
		# Keep the file current as we go so a long prod mint can be
		# interrupted and still leave something usable behind.
		_write_tokens(target, base_url, client_id, minted)

	print(f"Wrote {len(minted)} tokens to {TOKENS_FILE} in {(time.time() - started) / 60:.1f} min")


def _write_tokens(target, base_url, client_id, users):
	LOAD_DIR.mkdir(exist_ok=True)
	TOKENS_FILE.write_text(json.dumps({
		"target": target,
		"base_url": base_url,
		"client_id": client_id,
		"minted_at": datetime.now().isoformat(timespec="seconds"),
		"users": users,
	}, indent="\t"))


def _refresh_entry(base_url, verify, client_id, entry):
	"""Refresh one token entry in place; returns False if the stored refresh
	token is no longer valid (rotated by a run whose log was lost)."""
	response = None
	while True:
		response = requests.post(f"{base_url}/auth/o/token", data={
			"grant_type": "refresh_token",
			"refresh_token": entry["refresh_token"],
			"client_id": client_id,
		}, verify=verify, timeout=HTTP_TIMEOUT_SECONDS)
		if response.status_code == 429:
			print(f"  /o/token throttled, waiting {THROTTLE_BACKOFF_SECONDS}s...")
			time.sleep(THROTTLE_BACKOFF_SECONDS)
			continue
		break
	if response.status_code != 200:
		return False
	payload = response.json()
	entry["access_token"] = payload["access_token"]
	entry["refresh_token"] = payload.get("refresh_token", entry["refresh_token"])
	entry["expires_at"] = int(time.time()) + int(payload.get("expires_in", 900))
	return True


def _refresh_stale_tokens(target, tokens, horizon_seconds):
	"""
	Before a run, make sure no token expires within `horizon_seconds` (the
	run's length). A run that starts with many expired tokens has every VU
	refresh at once; under the old per-IP /o/token throttle that was a
	wall of 429s before the first real request went out (seen 2026-10-04:
	a ceiling run aborted at 32% failures, none of them the service under
	test), and even now it is a burst of token work on auth that belongs
	before the measurement, not inside it. A token whose refresh token is
	stale is re-minted from users.csv instead.
	"""
	base_url, verify = tokens["base_url"], _verify(target)
	flow = _auth_flow(target)
	client_id = tokens.get("client_id") or (_pkce_client_id(target) if flow == "pkce" else None)
	pace = MINT_PACE_SECONDS
	deadline = int(time.time()) + horizon_seconds
	stale = [e for e in tokens["users"] if e.get("expires_at", 0) < deadline]
	if not stale:
		return
	# The pass itself takes ~7s per token, during which the tokens that
	# looked fine keep aging -- push the deadline out by the pass's own
	# length (and once more for what that adds) so the run really starts
	# with nothing expiring inside the horizon.
	for _ in range(2):
		deadline = int(time.time()) + horizon_seconds + int(len(stale) * pace)
		stale = [e for e in tokens["users"] if e.get("expires_at", 0) < deadline]
	print(f"{len(stale)} of {len(tokens['users'])} tokens expire within {horizon_seconds // 60} min -- refreshing first...")
	passwords = {}
	if USERS_FILE.exists():
		passwords = {u["email"]: u["password"] for u in _read_users()}
	for index, entry in enumerate(stale, start=1):
		if index > 1:
			time.sleep(pace)
		if entry.get("refresh_token") and _refresh_entry(base_url, verify, client_id, entry):
			print(f"  [{index}/{len(stale)}] {entry['email']} refreshed")
		else:
			if entry["email"] not in passwords:
				raise LoadFailure(f"{entry['email']}: refresh token is stale and {USERS_FILE} has no password to re-mint with")
			user = {"email": entry["email"], "password": passwords[entry["email"]]}
			if flow == "pkce":
				payload = _mint_prod(base_url, verify, user, client_id)
			else:
				payload, client_id = _mint_dev(base_url, verify, user)
			entry.update({
				"access_token": payload["access_token"],
				"refresh_token": payload.get("refresh_token"),
				"expires_at": int(time.time()) + int(payload.get("expires_in", 900)),
			})
			print(f"  [{index}/{len(stale)}] {entry['email']} re-minted (refresh token was stale)")
		_write_tokens(target, base_url, client_id, tokens["users"])


def _merge_rotated_tokens(target, tokens, k6_log):
	"""Fold every JUBILO_TOKEN_ROTATED line k6 logged (see load/k6/lib.js)
	back into tokens.json so the next run starts with live refresh tokens."""
	if not k6_log.exists():
		return 0
	rotated = {}
	for line in k6_log.read_text().splitlines():
		try:
			msg = json.loads(line).get("msg", "")
		except ValueError:
			continue
		if msg.startswith("JUBILO_TOKEN_ROTATED "):
			entry = json.loads(msg[len("JUBILO_TOKEN_ROTATED "):])
			rotated[entry["email"]] = entry  # last rotation wins
	if rotated:
		for entry in tokens["users"]:
			if entry["email"] in rotated:
				entry.update(rotated[entry["email"]])
		_write_tokens(target, tokens["base_url"], tokens.get("client_id"), tokens["users"])
	return len(rotated)


# ------------------------------
# load run
#
def load_run(service_name_list=None):
	target = _target()
	args = list(service_name_list or [])
	scenario = args[0] if args else "youth_event"
	script = K6_DIR / f"{scenario}.js"
	if not script.exists():
		available = ", ".join(p.stem for p in sorted(K6_DIR.glob("*.js")) if p.stem != "lib")
		raise LoadFailure(f"no scenario {script.name} -- available: {available}")
	if shutil.which("k6") is None:
		raise LoadFailure("k6 is not installed -- `brew install k6`")
	if not TOKENS_FILE.exists():
		raise LoadFailure(f"{TOKENS_FILE} not found -- run `jubilo-cli load tokens` first")

	tokens = json.loads(TOKENS_FILE.read_text())
	if tokens.get("target") != target:
		raise LoadFailure(
			f"{TOKENS_FILE} was minted for target {tokens.get('target')!r} but JUBILO_LOAD_TARGET is {target!r} -- "
			f"re-run `jubilo-cli load tokens` for this target"
		)

	RESULTS_DIR.mkdir(exist_ok=True)
	stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
	summary = RESULTS_DIR / f"{stamp}-{target}-{scenario}.json"
	k6_log = RESULTS_DIR / f"{stamp}-{target}-{scenario}.log"

	horizon = int(os.environ.get("JUBILO_LOAD_REFRESH_HORIZON_SECONDS", "600"))
	_refresh_stale_tokens(target, tokens, horizon)

	cmd = [
		"k6", "run",
		"--summary-export", str(summary),
		# k6's own log (warnings, aborts, console.log lines) goes to a file
		# as JSON so token rotations can be read back; the end-of-run
		# summary still prints to the terminal.
		"--log-output", f"file={k6_log}",
		"--log-format", "json",
		"--env", f"BASE_URL={tokens['base_url']}",
		"--env", f"TOKENS_FILE={TOKENS_FILE}",
		"--env", f"TARGET={target}",
	]
	if target == "dev":
		cmd.append("--insecure-skip-tls-verify")
	if not sys.stdout.isatty():
		# No live progress bar when output is piped/captured; the end-of-run
		# summary still prints.
		cmd.append("--quiet")
	# Every remaining `KEY=VALUE` argument is forwarded to the script
	# (VUS=100, HOLD=5m, MODE=gateway, ...); see each script's header.
	for extra in args[1:]:
		if "=" not in extra:
			raise LoadFailure(f"expected KEY=VALUE, got {extra!r}")
		cmd += ["--env", extra]
	cmd.append(str(script))

	print(f"Target: {target} {tokens['base_url']} ({len(tokens['users'])} tokens, minted {tokens['minted_at']})")
	print("Running:", " ".join(cmd))
	result = subprocess.run(cmd, cwd=INFRA_ROOT)
	rotated = _merge_rotated_tokens(target, tokens, k6_log)
	print(f"Summary written to {summary}; k6 log in {k6_log}; {rotated} rotated token(s) saved back to {TOKENS_FILE}")
	if result.returncode != 0:
		raise LoadFailure(f"k6 exited {result.returncode} (a threshold failed or the run was aborted -- see output above and {k6_log})")
