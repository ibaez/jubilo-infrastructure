"""
End-to-end verification of jubilo-auth's cross-service revocation push (see
jubilo-auth/accounts/tasks.py::broadcast_revocation and
jubilo-music/music/tasks.py::handle_revocation_event) -- confirms a token
jubilo-music has already cached from a real introspection call stops working
within seconds of a real role change, instead of the ~15 minutes it would
otherwise take to expire on its own.

DEV ONLY. This is NOT part of either service's `manage.py test` suite and is
NOT run by CI -- CI runs each repo's tests in isolation (mocked, no real
cross-service Redis/RQ workers), and this needs the full docker-compose
stack actually running. It creates a real throwaway AuthUser/Participant/
invitation through the real HTTP API, the same way `jubilo dev setup` does
for its own fixture users -- safe by construction (this only ever targets
containers named in this repo's own docker-compose.yml, never a production
DATABASE_URL), but repeated runs accumulate rows in the dev DB;
`docker compose down -v` + `jubilo dev setup` is the intended way to reset,
not anything this script does itself.

Run with: ./jubilo dev e2e
Assumes `jubilo dev setup` has already been run at least once against this
stack -- reuses its superuser credentials and the jubilo_postman OAuth
client already provisioned there.

Adding another scenario: write a new function here (self-contained, same
shape as e2e_test_revocation) and call it from the `jubilo` CLI's dev_e2e,
same pattern as dev_test aggregating dev_test_jubilo_auth/
dev_test_jubilo_music. `./jubilo dev e2e` then runs every scenario in
sequence.
"""

import re
import time
from pathlib import Path

import requests

from services.docker import docker_get_host_ip, _read_env_file

SUPERUSER_EMAIL = "su1@gmail.com"
SUPERUSER_PASSWORD = "su1"

INFRA_ROOT = Path(__file__).resolve().parent.parent
CA_CERT = str(INFRA_ROOT / "rootCA.pem")
AUTH_ENV_PATH = INFRA_ROOT.parent / "jubilo-auth" / ".env"

MAILPIT_TIMEOUT_SECONDS = 30
MAILPIT_POLL_INTERVAL_SECONDS = 1
REVOCATION_TIMEOUT_SECONDS = 20
REVOCATION_POLL_INTERVAL_SECONDS = 1


class E2EFailure(Exception):
	pass


def _get_password_grant_token(docker_host_ip, postman_client_id, email, password, scope):
	response = requests.post(
		f"https://{docker_host_ip}/auth/o/token",
		data={
			"grant_type": "password",
			"username": email,
			"password": password,
			"client_id": postman_client_id,
			"scope": scope,
		},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()
	json_data = response.json()
	if "access_token" not in json_data:
		raise E2EFailure(f"Token response missing access_token: {json_data}")
	return json_data["access_token"]


def _get_password_grant_token_scopes(docker_host_ip, postman_client_id, email, password, scope):
	# Same request shape as _get_password_grant_token, but for callers that
	# need to inspect the granted scope string itself (e.g. proving an
	# override actually changed what lands on the token) rather than just
	# needing a bearer token to make an authenticated request with.
	response = requests.post(
		f"https://{docker_host_ip}/auth/o/token",
		data={
			"grant_type": "password",
			"username": email,
			"password": password,
			"client_id": postman_client_id,
			"scope": scope,
		},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()
	json_data = response.json()
	if "scope" not in json_data:
		raise E2EFailure(f"Token response missing scope: {json_data}")
	return set(json_data["scope"].split())


def _wait_for_invitation_code(email):
	# Same polling shape as dev_setup's own invitation email wait -- the
	# invitation email is sent asynchronously by jubilo_auth_worker (an RQ
	# job), so a single-shot check racing the worker would be flaky.
	deadline = time.time() + MAILPIT_TIMEOUT_SECONDS
	latest = None

	while latest is None and time.time() < deadline:
		response = requests.get("http://localhost:8025/api/v1/messages")
		messages = response.json().get("messages", [])
		latest = next((m for m in messages if m["To"][0]["Address"] == email), None)
		if latest is None:
			time.sleep(MAILPIT_POLL_INTERVAL_SECONDS)

	if latest is None:
		raise E2EFailure(
			f"Timed out after {MAILPIT_TIMEOUT_SECONDS}s waiting for the invitation email for {email}. "
			"Check that jubilo_auth_worker is running: docker compose logs jubilo_auth_worker"
		)

	detail = requests.get(f"http://localhost:8025/api/v1/message/{latest['ID']}")
	body = detail.json()["Text"]
	match = re.search(r'\b\d{6}\b', body)
	if not match:
		raise E2EFailure(f"Could not find a 6-digit invitation code in the email body:\n{body}")
	return match.group()


def e2e_test_revocation(service_name_list=None):
	print("Running end-to-end revocation push verification...")

	docker_host_ip = docker_get_host_ip()
	auth_env, _ = _read_env_file(str(AUTH_ENV_PATH))
	postman_client_id = auth_env.get("JUBILO_POSTMAN_CLIENT_ID")

	if not postman_client_id:
		raise E2EFailure(
			f"JUBILO_POSTMAN_CLIENT_ID not found in {AUTH_ENV_PATH} -- run `jubilo dev setup` first."
		)

	test_email = f"e2e-revoke-{int(time.time())}@test.com"
	test_password = "E2ERevokeTest123!"

	print("Acquiring superuser access token...")
	superuser_token = _get_password_grant_token(
		docker_host_ip, postman_client_id, SUPERUSER_EMAIL, SUPERUSER_PASSWORD, "auth music"
	)

	# ------------------------------
	# Invited with BOTH roles, not just music -- the test user needs
	# auth:profile (from an auth role) to call /auth/user/me for its own id
	# below, since UserServiceRoleUpdate takes a user id in the body, not an
	# email. compute_scopes_for_user only grants scopes for roles actually
	# held, so a music-only invite would never carry auth:profile even when
	# "auth music" is requested at token time.
	#
	print(f"Inviting throwaway test user {test_email}...")
	response = requests.post(
		f"https://{docker_host_ip}/auth/invitation",
		json={
			"email": test_email,
			"first_name": "E2E",
			"last_name": "Revoke",
			"roles": [
				{"service": "music", "role": "music_member"},
				{"service": "auth", "role": "auth_member"},
			],
		},
		headers={"Authorization": f"Bearer {superuser_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()

	invitation_code = _wait_for_invitation_code(test_email)

	print("Validating and accepting invitation...")
	response = requests.post(
		f"https://{docker_host_ip}/auth/invitation/validate",
		data={"email": test_email, "invitation_code": invitation_code},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()
	invitation_token = response.json()["invitation_token"]

	response = requests.post(
		f"https://{docker_host_ip}/auth/invitation/accept",
		data={"invitation_token": invitation_token, "password": test_password},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()

	print("Acquiring the test user's own access token...")
	test_user_token = _get_password_grant_token(
		docker_host_ip, postman_client_id, test_email, test_password, "auth music"
	)

	response = requests.get(
		f"https://{docker_host_ip}/auth/user/me",
		headers={"Authorization": f"Bearer {test_user_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()
	test_user_id = response.json()["id"]
	print(f"Test user AuthUser id: {test_user_id}")

	music_artist_url = f"https://{docker_host_ip}/api/music/artist"

	# ------------------------------
	# This is the step that actually matters for the test: a real request
	# through jubilo-music's own OAuth2Authentication, which -- on a token
	# it hasn't seen before -- calls jubilo-auth's /o/introspect for real and
	# caches the result as its own local AccessToken row. Nothing here is
	# simulated; this is the exact caching gap the revocation push exists to
	# shrink.
	#
	print("Making an authenticated request to jubilo-music (this is what makes it cache the token locally)...")
	response = requests.get(
		music_artist_url,
		headers={"Authorization": f"Bearer {test_user_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	if response.status_code != 200:
		raise E2EFailure(
			f"Expected 200 from jubilo-music before revocation, got {response.status_code}: {response.text}"
		)
	print("jubilo-music accepted the token, and has now cached it locally.")

	print("Triggering a real role change via /auth/user-service-role as the superuser...")
	response = requests.post(
		f"https://{docker_host_ip}/auth/user-service-role",
		json={"user": test_user_id, "roles": [{"service": "music", "role": "music_member"}]},
		headers={"Authorization": f"Bearer {superuser_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()
	print("Role change accepted -- jubilo-auth has revoked the test user's tokens and pushed a revocation broadcast.")

	print(f"Polling jubilo-music with the test user's ORIGINAL (now-revoked) token for up to {REVOCATION_TIMEOUT_SECONDS}s...")
	deadline = time.time() + REVOCATION_TIMEOUT_SECONDS
	start = time.time()
	rejected_after_seconds = None

	while time.time() < deadline:
		response = requests.get(
			music_artist_url,
			headers={"Authorization": f"Bearer {test_user_token}"},
			verify=CA_CERT,
			timeout=5,
		)
		if response.status_code == 401:
			rejected_after_seconds = time.time() - start
			break
		time.sleep(REVOCATION_POLL_INTERVAL_SECONDS)

	if rejected_after_seconds is None:
		raise E2EFailure(
			f"jubilo-music was still accepting the revoked token after {REVOCATION_TIMEOUT_SECONDS}s -- "
			"the revocation broadcast either didn't fire or jubilo_music_worker isn't consuming the "
			"'revocation' queue. Check: docker compose logs jubilo_auth_worker jubilo_music_worker"
		)

	print(
		f"PASS: jubilo-music rejected the revoked token after {rejected_after_seconds:.1f}s "
		"(well under its ~15 minute cache lifetime)."
	)


def e2e_test_scope_override(service_name_list=None):
	print("Running end-to-end UserScopeOverride verification...")

	docker_host_ip = docker_get_host_ip()
	auth_env, _ = _read_env_file(str(AUTH_ENV_PATH))
	postman_client_id = auth_env.get("JUBILO_POSTMAN_CLIENT_ID")

	if not postman_client_id:
		raise E2EFailure(
			f"JUBILO_POSTMAN_CLIENT_ID not found in {AUTH_ENV_PATH} -- run `jubilo dev setup` first."
		)

	test_email = f"e2e-scope-{int(time.time())}@test.com"
	test_password = "E2EScopeTest123!"

	# ------------------------------
	# music_member's own scopes_set is exactly {"music:access"} -- makes it a
	# clean target for a revoke (there's nothing else masking the effect).
	# music:update belongs to music_staff (rank 20) and above, not
	# music_member (rank 10) -- makes it a clean target for a grant. The
	# superuser actor bypasses every rank check in the serializer, so which
	# roles these scopes actually belong to doesn't gate the request itself;
	# it only matters for proving the *effect* is real below.
	#
	GRANT_SCOPE = "music:update"
	REVOKE_SCOPE = "music:access"

	print("Acquiring superuser access token...")
	superuser_token = _get_password_grant_token(
		docker_host_ip, postman_client_id, SUPERUSER_EMAIL, SUPERUSER_PASSWORD, "auth music"
	)

	# Same reasoning as e2e_test_revocation's own invite -- auth:profile
	# (from an auth role) is needed to call /auth/user/me for the test
	# user's own id, since UserScopeOverride takes a user id in the body.
	print(f"Inviting throwaway test user {test_email}...")
	response = requests.post(
		f"https://{docker_host_ip}/auth/invitation",
		json={
			"email": test_email,
			"first_name": "E2E",
			"last_name": "Scope",
			"roles": [
				{"service": "music", "role": "music_member"},
				{"service": "auth", "role": "auth_member"},
			],
		},
		headers={"Authorization": f"Bearer {superuser_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()

	invitation_code = _wait_for_invitation_code(test_email)

	print("Validating and accepting invitation...")
	response = requests.post(
		f"https://{docker_host_ip}/auth/invitation/validate",
		data={"email": test_email, "invitation_code": invitation_code},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()
	invitation_token = response.json()["invitation_token"]

	response = requests.post(
		f"https://{docker_host_ip}/auth/invitation/accept",
		data={"invitation_token": invitation_token, "password": test_password},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()

	print("Acquiring the test user's own access token to look up its id...")
	test_user_token = _get_password_grant_token(
		docker_host_ip, postman_client_id, test_email, test_password, "auth music"
	)

	response = requests.get(
		f"https://{docker_host_ip}/auth/user/me",
		headers={"Authorization": f"Bearer {test_user_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()
	test_user_id = response.json()["id"]
	print(f"Test user AuthUser id: {test_user_id}")

	override_url = f"https://{docker_host_ip}/auth/user-scope-override"

	print(f"Confirming the baseline token (role-derived only) has {REVOKE_SCOPE!r} and not {GRANT_SCOPE!r}...")
	baseline_scopes = _get_password_grant_token_scopes(
		docker_host_ip, postman_client_id, test_email, test_password, "music"
	)
	if REVOKE_SCOPE not in baseline_scopes:
		raise E2EFailure(f"Expected {REVOKE_SCOPE!r} on the baseline music_member token, got: {sorted(baseline_scopes)}")
	if GRANT_SCOPE in baseline_scopes:
		raise E2EFailure(f"Did not expect {GRANT_SCOPE!r} on the baseline music_member token, got: {sorted(baseline_scopes)}")

	print(f"Granting {GRANT_SCOPE!r} to the test user as the superuser...")
	response = requests.post(
		override_url,
		json={"user": test_user_id, "overrides": [{"scope": GRANT_SCOPE, "kind": "grant"}]},
		headers={"Authorization": f"Bearer {superuser_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()

	# ------------------------------
	# The grant above just called revoke_user_tokens() on the test user, so
	# a fresh password-grant is required to see the effect -- the same
	# force-relogin tradeoff documented on revoke_user_tokens itself.
	#
	print("Re-acquiring a token to confirm the grant took effect...")
	granted_scopes = _get_password_grant_token_scopes(
		docker_host_ip, postman_client_id, test_email, test_password, "music"
	)
	if GRANT_SCOPE not in granted_scopes:
		raise E2EFailure(f"Expected {GRANT_SCOPE!r} after granting it, got: {sorted(granted_scopes)}")
	if REVOKE_SCOPE not in granted_scopes:
		raise E2EFailure(f"Expected {REVOKE_SCOPE!r} to still be present after an unrelated grant, got: {sorted(granted_scopes)}")
	print(f"Confirmed: {GRANT_SCOPE!r} is now on the token, beyond what music_member's role alone provides.")

	print(f"Revoking {REVOKE_SCOPE!r} from the test user as the superuser...")
	response = requests.post(
		override_url,
		json={"user": test_user_id, "overrides": [{"scope": REVOKE_SCOPE, "kind": "revoke"}]},
		headers={"Authorization": f"Bearer {superuser_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()

	print("Re-acquiring a token to confirm the revoke took effect...")
	revoked_scopes = _get_password_grant_token_scopes(
		docker_host_ip, postman_client_id, test_email, test_password, "music"
	)
	if REVOKE_SCOPE in revoked_scopes:
		raise E2EFailure(f"Expected {REVOKE_SCOPE!r} to be gone after revoking it, got: {sorted(revoked_scopes)}")
	if GRANT_SCOPE not in revoked_scopes:
		raise E2EFailure(f"Expected {GRANT_SCOPE!r} (from the earlier grant) to still be present, got: {sorted(revoked_scopes)}")
	print(f"Confirmed: {REVOKE_SCOPE!r} is gone even though music_member's role alone would normally include it.")

	print(f"Clearing the {GRANT_SCOPE!r} override as the superuser...")
	response = requests.post(
		override_url,
		json={"user": test_user_id, "overrides": [{"scope": GRANT_SCOPE, "kind": "clear"}]},
		headers={"Authorization": f"Bearer {superuser_token}"},
		verify=CA_CERT,
		timeout=5,
	)
	response.raise_for_status()

	print("Re-acquiring a token to confirm the clear took effect...")
	cleared_scopes = _get_password_grant_token_scopes(
		docker_host_ip, postman_client_id, test_email, test_password, "music"
	)
	if GRANT_SCOPE in cleared_scopes:
		raise E2EFailure(f"Expected {GRANT_SCOPE!r} to be gone after clearing its override, got: {sorted(cleared_scopes)}")
	if REVOKE_SCOPE in cleared_scopes:
		raise E2EFailure(f"Expected {REVOKE_SCOPE!r} to remain revoked (its own override was never cleared), got: {sorted(cleared_scopes)}")

	print(
		"PASS: grant added a scope beyond the role, revoke removed a role-derived scope, "
		"and clear reverted the grant while leaving the unrelated revoke in place -- all "
		"proven against real, freshly issued tokens."
	)
