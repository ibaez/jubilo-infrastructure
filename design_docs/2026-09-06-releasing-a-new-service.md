# Releasing a new service onto Jubilo's production infrastructure

## Context

jubilo-church was the first service promoted to production since the
original auth/music pair went live. It surfaced a full set of gaps in the
release process that hadn't been exercised before -- some in the code,
some in Railway's own platform behavior, some in how the mobile client
stays in sync with the backend. This doc captures the real order of
operations and every issue actually hit, so the next service (jubilo-
testigo is already stubbed out in jubilo-auth's `SERVICES` dict as a
future candidate) doesn't rediscover the same ones from scratch.

Ownership split throughout: routine, scripted steps are done by whoever's
driving the code changes; anything touching Railway Console itself
(provisioning, env vars, triggering deploys, running one-off management
commands) is done by a human with Railway access, never automated away.

## Order of operations

Dependencies run in this direction: Railway infrastructure must exist
before jubilo-auth can be told to route to it; jubilo-auth must recognize
the new service before its own first deploy can do anything useful
(participant mirroring needs jubilo-auth's introspection/broadcast to
already know about it); the gateway and mobile app are the last mile.

1. **Railway Console**: provision the new Postgres instance and the two
   new services (web + worker) for `<service>`, set their env vars.
2. **jubilo-auth code**: promote `SERVICES["<service>"]`/
   `INVITATION_MIRROR_TARGETS["<service>"]`/
   `REVOCATION_TARGETS["<service>"]` from whatever dev-only overlay they
   were staged in into the real `settings.py`; add
   `REDIS_<SERVICE>_QUEUE_URL` to `REQUIRED_ENV_VARS`; add the new
   service's OAuth Application provisioning to `auth_prod_setup.py`; add
   the same new env var to `.github/workflows/test.yml` (see Issue 4).
3. **Railway Console**: add `REDIS_<SERVICE>_QUEUE_URL` to jubilo-auth's
   own service env vars, deploy jubilo-auth, confirm the deployed commit
   actually matches what was just pushed (see Issue 3), then manually run
   `auth_prod_setup.py` once.
4. **New service code**: add `railway.json` (migrate) and
   `railway.worker.json` (rqworker) -- these won't actually take effect
   yet on a brand-new service (see Issue 1), but should exist for when a
   later redeploy does pick them up, and as documentation of intent.
5. **Railway Console**: since config-as-code doesn't apply to a new
   service's first deploy, manually set the web service's **Pre-Deploy
   Command** and the worker service's **Start Command** directly in each
   service's Settings -> Deploy section (same values as the JSON files).
   Deploy the new service for the first time.
6. **jubilo-gateway code**: add a real nginx `location /api/<service>/`
   block, matching an already-working service's exact shape (internal
   Railway hostname, the `rewrite ... break;` path-stripping trick, `X-
   Forwarded-Proto https` hardcoded).
7. **Railway Console**: deploy jubilo-gateway.
8. **jubilo-mobile code**: add
   `EXPO_PUBLIC_JUBILO_<SERVICE>_BASE_URL` to `eas.json`'s build profiles
   (production, and development/preview using the local gateway IP for
   parity with auth/music).
9. **Backfill existing accounts**: run `backfill_service_default_role
   <service> --operator-id <id>` in jubilo-auth's Railway Console shell
   (see Issue 6) -- every account created before this service existed has
   zero scopes for it otherwise.
10. **If the new service has its own seed/setup command** (structural
    reference data, not user data): mint a Bearer token and run it,
    expecting it to need the same production-safety header fix as Issue 8
    if it uses DRF's `APIClient` internally.
11. **Trigger a new EAS build** for both platforms once the mobile changes
    are in.
12. **End-to-end smoke test**: log out and back in on a real device (see
    Issue 7), confirm the new service's screens appear, accept a real
    invitation and confirm it mirrors into the new service's production
    database with the right fields and history row.

## Issues encountered and their resolutions

### 1. Railway config-as-code doesn't apply to a brand-new service

`railway.json`'s `preDeployCommand` / `railway.worker.json`'s
`startCommand` are silently ignored for a service's very first deploy --
Railway only picks up config-as-code once the service already exists with
some baseline configuration. This looks identical to the files just being
wrong, which cost real time chasing the wrong cause.

**Fix**: for a new service's first deploy, set the Pre-Deploy Command /
Start Command manually in Railway Console's Settings -> Deploy UI. Keep
the JSON files in the repo anyway (documentation of intent, and they may
start being honored on a later redeploy) but don't trust them alone for a
first deploy.

### 2. Missing tables on first deploy (`relation "oauth2_provider_accesstoken" does not exist`)

Direct downstream consequence of Issue 1 -- `python manage.py migrate`
never actually ran, so the fresh Postgres instance had no schema at all
beyond whatever `CREATE DATABASE` provides. Surfaced as a 500 on every
authenticated request, not just church-specific ones, since
`OAuth2TokenMiddleware` itself couldn't query its own token table.

**Fix**: same as Issue 1's fix, then manually run `python manage.py
migrate` once via Railway Console shell to unblock immediately. Confirm
by checking the actual deploy logs for the Pre-Deploy Command step
succeeding on every subsequent deploy, not just assuming because the
config file exists.

### 3. jubilo-auth's live deploy was one commit behind what was pushed

Code changes were committed and pushed to `master`, but Railway's actual
running deploy was still building from the previous commit -- pushing to
the branch Railway watches doesn't guarantee it redeploys immediately or
automatically in every configuration.

**Fix**: after pushing, check Railway Console's deploy history for the
service and confirm the commit hash it's actually running matches. If
not, trigger a manual redeploy.

### 4. CI breaks the moment a new `REQUIRED_ENV_VARS` entry is added

Adding `REDIS_<SERVICE>_QUEUE_URL` (or any new var) to `settings.py`'s
`REQUIRED_ENV_VARS` immediately breaks GitHub Actions CI with an opaque
`ImproperlyConfigured` deep inside Django's own command-dispatch error
handling (surfaces as a confusing `KeyError: 'collectstatic'` traceback
before the real cause) -- CI's own `.github/workflows/test.yml` has its
own hardcoded `env:` block that doesn't automatically pick up new
required vars.

**Fix**: every time a var is added to `REQUIRED_ENV_VARS`, add a matching
line to `test.yml`'s `env:` block in the same change, with a throwaway
value the CI Postgres/Redis containers can satisfy (e.g.
`redis://localhost:6379/<next index>`).

### 5. 401s on every request after the new service is technically deployed

Even with jubilo-auth recognizing the service and the new service's own
migrations applied, every real authenticated request 401s. Root cause:
`auth_prod_setup.py` hadn't been run yet, so jubilo-auth has no registered
OAuth Application for the new service's `(CLIENT_ID, CLIENT_SECRET)` pair
-- the new service's own introspection calls back to jubilo-auth
(`RESOURCE_SERVER_INTROSPECTION_CREDENTIALS`) fail authentication, and
`OAuth2TokenMiddleware` treats every token as invalid as a result.

**Fix**: run `auth_prod_setup.py` in jubilo-auth's Railway Console shell
before testing anything against the new service. Confirm the new
service's `JUBILO_<SERVICE>_CLIENT_ID`/`SECRET` env vars match exactly
what was registered.

### 6. Pre-existing accounts get zero scopes for the new service

`compute_scopes_for_user` only ever reads a user's **existing**
`UserServiceRole` rows -- it never synthesizes a service's `default_role`
for a service that didn't exist when the account (or its roles) were
created. Every account created before the new service went live has
literally no row for it, so ends up with zero scopes for that service no
matter what, even after logging back in.

**Fix**: wrote `backfill_service_default_role <service> --operator-id
<id>` (jubilo-auth management command) -- creates a default-role
`UserServiceRole` for every existing account missing one. **Skips
superusers** -- their scopes are computed fresh from `settings.SERVICES`
on every token issuance regardless of any stored row, so backfilling one
for them would only misleadingly show up as the lowest-tier role in their
own roles list despite them actually having full access. Run once per new
service, after `auth_prod_setup.py`.

### 7. A superuser's own existing token still shows zero new scopes after everything above is fixed

Scopes are computed once, at `/o/authorize` time
(`NarrowingOAuth2Validator.validate_scopes`) -- **not** recomputed on a
`refresh_token` grant. A token (or its refreshed successor) issued before
the new service existed in `settings.SERVICES` keeps whatever scope set
was computed back then, forever, regardless of how many times it's
silently refreshed in the background.

**Fix**: there is no backend fix for this -- it's correct OAuth2
behavior. The device/account being tested with needs a genuine full
logout + login (new authorization-code flow), not just waiting for a
background token refresh.

### 8. A DRF-`APIClient`-based seed/setup command 400s or redirects in real production

Any one-off data-seeding management command written using
`rest_framework.test.APIClient` (an in-process test client, chosen
because it lets the command reuse real view/serializer/permission logic
without a network hop) breaks under real (`DEBUG=False`) production
settings in two separate ways:

- `APIClient` defaults to a `Host: testserver` header, which
  `ALLOWED_HOSTS` only accepts when `DEBUG=True` -> `DisallowedHost` 400.
- `SECURE_SSL_REDIRECT` (on whenever `DEBUG=False`) determines "was this
  request HTTPS" via `SECURE_PROXY_SSL_HEADER` (`X-Forwarded-Proto`,
  matching what the real gateway's nginx config sets) -- `APIClient`
  never sets that header on its own, so every request looks like plain
  HTTP and gets 301-redirected instead of actually reaching the view.

**Fix**: pass both headers explicitly in the command's own
`APIClient.credentials(...)` call:
```python
API_CLIENT.credentials(
    HTTP_AUTHORIZATION=f'Bearer {access_token}',
    HTTP_HOST=settings.JUBILO_GATEWAY_IP,       # already a real ALLOWED_HOSTS entry everywhere
    HTTP_X_FORWARDED_PROTO='https',
)
```
Check any new service's own one-off setup/seed commands for this exact
pattern before assuming they'll "just work" in production because they
work locally under `DEBUG=True`.

### 9. Mobile client's scope-gating silently drifts from the backend

Server-side authorization for a resource can be migrated from a flat
platform scope to an object-level check (real domain data -- who leads
this specific record -- rather than a role on the account) without the
mobile client ever being told. The client keeps checking a scope string
that no longer exists on any role, so the gated button/screen becomes
permanently invisible to everyone, including admins -- no error, no
crash, just a feature that silently stopped being reachable.

**Fix**: no generic fix, but the pattern to watch for: whenever a
service's role/scope definitions change server-side, grep the mobile
client for every scope string that was removed, added, or renamed, and
audit each call site. Where the API response already exposes a
server-computed authority field for that exact object (e.g.
`has_church_authority`), prefer that over any scope check -- it can never
drift out of sync with the real permission logic since it's computed by
the same code that enforces it. Where no such field exists, either gate
on a still-live coarser bypass scope (accepting that some genuinely
authorized users without a platform role won't see the button) or show
unconditionally and let the server 403.

## Verification checklist for the next release

- [ ] jubilo-auth: new service's `SERVICES`/`INVITATION_MIRROR_TARGETS`/
      `REVOCATION_TARGETS` entries live in `settings.py` itself, not a
      dev-only overlay
- [ ] jubilo-auth: `REQUIRED_ENV_VARS` and `.github/workflows/test.yml`'s
      `env:` block both updated together
- [ ] jubilo-auth: `auth_prod_setup.py` updated and actually run in
      production; confirm the Application shows as newly-created, not
      already-existing
- [ ] New service: Pre-Deploy Command / Start Command confirmed set in
      Railway Console UI directly (not just present in `railway.json`)
- [ ] New service: deploy logs show the migrate step actually running and
      succeeding
- [ ] New service's own client id/secret match byte-for-byte between its
      own env vars and what's registered in jubilo-auth
- [ ] jubilo-gateway: new `/api/<service>/` block deployed, confirmed
      reachable through the real public domain (not 404/502)
- [ ] jubilo-mobile: `EXPO_PUBLIC_JUBILO_<SERVICE>_BASE_URL` present in
      every build profile that needs it
- [ ] `backfill_service_default_role` run once for the new service
- [ ] Any one-off seed/setup command for the new service audited for the
      `APIClient` Host/X-Forwarded-Proto issue (Issue 8) before running it
      for real
- [ ] Mobile client's scope-gating audited against the new service's
      actual current role/scope definitions (Issue 9), not assumed correct
      from an earlier design doc
- [ ] Full logout/login performed on the actual test device before
      concluding a scopes-related fix didn't work
