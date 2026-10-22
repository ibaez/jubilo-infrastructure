# Jubilo: credential rotation runbook

Status: written 2026-10-22, after the Railway platform breach (status.railway.com/incident/QH8L8B3E)
required rotating every secret below. Reference for next time any upstream provider (Railway,
Cloudflare, Resend, AWS) discloses a credential exposure.

## First: check for an active platform incident before touching anything

Don't rotate a live, actively-checked secret while Railway itself (or whichever provider hosts the
thing you're rotating) is having its own incident. Rotating breaks the old value immediately with
no grace period, and the consuming service has to successfully restart or redeploy to pick up the
new one -- during a platform incident that pickup can be blocked for an extended, unpredictable
period, with no way to force it from your side. This happened for real: `jubilo-auth` was fully
down for hours because its database password was rotated while Railway's Hobby-tier deploy
pipeline was still frozen.

Check status.railway.com first. If there's an active incident, wait until it's confirmed resolved
for your account tier specifically (restoration happens in stages -- Enterprise/Pro first,
Hobby/Free/Trial last) before rotating anything. The one exception: if a secret is confirmed
or strongly suspected to be under active exploitation right now, the risk of leaving it live may
outweigh the risk of an extended outage -- that's a judgment call, not a rule.

## Restart vs. redeploy -- these are not interchangeable

A plain **restart** (same already-built image, same container, just stopped and started) is
enough for most env var changes. It is **not** enough for a variable whose value is itself
*composed* from other variables via Railway's template syntax (e.g.
`DATABASE_URL="postgresql://${{PGUSER}}:${{POSTGRES_PASSWORD}}@${{RAILWAY_PRIVATE_DOMAIN}}:5432/${{PGDATABASE}}"`,
or a service referencing another service's own composed value like
`DATABASE_URL="${{jubilo-auth-db.DATABASE_URL}}"`). That composed value appears to only recompute
on an actual new deploy -- confirmed the hard way: restarting `jubilo-auth` repeatedly after
regenerating its database password never picked up the new value, while a genuine redeploy (once
Railway's pipeline came back) fixed it immediately.

A **direct, single-value reference** (e.g. `JUBILO_MEILISEARCH_MASTER_KEY="${{jubilo-meili.MEILI_MASTER_KEY}}"`)
behaved differently -- a plain restart picked up the change fine. So: composed/templated values
need a redeploy; direct references and plain literals are usually fine with just a restart. When
in doubt, verify directly rather than assume -- see "How to verify" below.

## Full secret inventory, by rotation mechanism

### Railway-managed database passwords (Postgres, Redis)
`jubilo-auth-db`, `jubilo-music-db`, `jubilo-church-db`, `jubilo-redis` each have a
Database -> Config -> Connection tab with a **Regenerate** button. This changes the live
server's password and the stored variable together in one step -- don't hand-edit the password
variable directly, since the app and the server would then disagree.
- Breaks existing connections immediately, no grace period.
- Every consuming service needs the resolved connection-string variable to recompute (see
  restart-vs-redeploy above) and then needs a restart/redeploy itself to pick up the new value.
- Check each consuming service's `DATABASE_URL`/`REDIS_*_URL` is a Railway reference, not a
  hand-typed literal -- a reference keeps services in sync automatically; a literal needs manual
  re-syncing every rotation.
- Cross-service pairs that must match exactly: `jubilo-auth`'s `REDIS_MUSIC_QUEUE_URL` with
  `jubilo-music`'s own `REDIS_QUEUE_URL`; `jubilo-auth`'s `REDIS_CHURCH_QUEUE_URL` with
  `jubilo-church`'s own `REDIS_QUEUE_URL` (auth enqueues jobs onto these queues, the sibling
  service's worker consumes them -- a mismatch means jobs silently vanish into an unwatched queue,
  no error raised anywhere).
- `jubilo-redis` is a single shared instance across auth/music/church (different DB index per
  service, not separate instances) -- rotating it touches all three at once, higher stakes than
  the per-service Postgres databases.

### OAuth client secrets (`JUBILO_CHURCH_CLIENT_SECRET`, `JUBILO_MUSIC_CLIENT_SECRET`, `JUBILO_MOBILE_CLIENT_SECRET`)
**The Railway env var alone does nothing.** `django-oauth-toolkit` hashes these at rest
(`OAUTH2_PROVIDER.CLIENT_SECRET_HASHER` in `jubilo_auth/settings.py`); the live credential is the
hashed `Application.client_secret` row in jubilo-auth's own database, set once at
`createapplication` time. The setup script (`scripts/auth_prod_setup.py`) only creates an
application if it doesn't already exist, so re-running it after an env var change is a no-op.
Actual rotation needs a direct database update, run against jubilo-auth in production (so it
needs auth's own database connection working first):
```python
python manage.py shell -c "
from oauth2_provider.models import Application
app = Application.objects.get(name='jubilo_church')  # or jubilo_music, jubilo_mobile
app.client_secret = 'NEW_PLAINTEXT_SECRET'
app.save()
"
```
Then update the matching Railway env var on both sides of the pair (auth + church, or auth +
music) and restart/redeploy both. `jubilo_mobile` is a PKCE-secured public client -- its secret
isn't meaningfully checked at runtime, and there's no second service to sync (the mobile app
itself never reads a Railway env var), so rotating it is low-priority cleanup, not a real fix.
`jubilo_postman` is dev/testing-only and deliberately not provisioned in production
(`auth_prod_setup.py`'s own comment) -- confirm it doesn't exist in prod before bothering to
rotate its env var at all.

### `INTERNAL_AUTH_TO_CHURCH_SECRET`
A genuine plain pre-shared secret, read live from the environment on every request (no database,
no hashing) -- auth sends it as a header (`accounts/services/church_authority.py`), church
compares it directly (`church/permissions/internal_service.py`). Best set up as a Railway
**Shared Variable** (Project Settings -> Shared Variables, or promote an existing service
variable via its ⋮ menu) so both auth and church reference the same canonical value instead of
needing two hand-typed copies kept in sync. Rotating it is then just: change the shared value
once, restart/redeploy both services.

### Meilisearch master key (`JUBILO_MEILISEARCH_MASTER_KEY` / `jubilo-meili`'s `MEILI_MASTER_KEY`)
Meilisearch is a generic Docker image (`getmeili/meilisearch`), not a Railway-native database
plugin -- no Regenerate button. Generate a new value yourself:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```
Meilisearch only reads `MEILI_MASTER_KEY` at startup -- no live rotation API. Non-destructive to
indexed data (it's an auth credential, not an encryption key). Sequence: set the new value on
`jubilo-meili`, restart/redeploy it first, then restart/redeploy `jubilo-music` (which references
it via `${{jubilo-meili.MEILI_MASTER_KEY}}`) right after.

### `SECRET_KEY` (all three Django services)
Each service needs its own distinct value -- never share across services, even though
`jubilo-church` and `jubilo-music` don't currently exercise it much (admin, sessions, and CSRF
middleware are all disabled in both; only `jubilo-auth` has active database-backed sessions).
Keep each worker's `SECRET_KEY` matching its own web service (reference or matching literal), not
an independently-set value, even though the worker itself rarely touches session data directly.
Generate with Django's own generator, from any service's console:
```bash
python3 -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### `DJANGO_SUPERUSER_PASSWORD` / the actual superuser account
The env var is only read once, at initial `createsuperuser` time -- changing it does nothing to
an already-existing account. Actually reset the account's password (needs auth's own working
database connection):
```bash
python3 manage.py changepassword <superuser-email>
```

### Cloudflare R2 access keys
Cloudflare dashboard -> Account API tokens -> **Roll** (not Delete + Create -- Roll keeps the
same token identity/permissions, just regenerates the secret). Rolling invalidates the old
secret immediately, no grace period, and shows the new Access Key ID + Secret Access Key exactly
once -- copy both before navigating away. The Access Key ID itself doesn't change on a roll (it's
derived from the account + token identity, not random secret material) -- only
`R2_SECRET_ACCESS_KEY` needs updating in Railway, not `R2_ACCESS_KEY_ID`.

### Resend API key
Only actually read by `jubilo-auth-worker` (where `send_mail` really executes, via RQ jobs) --
`jubilo-auth`'s own web process never calls `send_mail` directly (`EMAIL_BACKEND` is
`DEBUG`-gated to SMTP locally / Resend's API everywhere on Railway, but the web process never
reaches that code path). `jubilo-church`'s copy of `RESEND_API_KEY` is pure vestigial
boilerplate -- nothing in that repo calls `send_mail` or reads it at all. Only rotate it on the
Resend dashboard and update `jubilo-auth-worker`'s env var; don't bother with `jubilo-auth`
(web) or `jubilo-church`.

### AWS IAM key (treasury report gatekeeper, `jubilo-church`)
Separate provider, separate console (IAM, not Railway). If this feature/account isn't currently
in active use, confirm that before spending effort on it -- an unreachable AWS account makes its
own credentials moot regardless of exposure.

## Public networking exposure

Check whether each Railway-managed database has public networking enabled (Database -> Networking
-> Public networking) before assuming an exposed credential's blast radius is "private network
only." A credential exposed alongside public reachability means direct internet access with just
the password -- a materially larger risk than the same credential being unreachable from outside
Railway's private network. Turn off public networking for any database that doesn't actually need
external connections (check first whether you rely on it for direct access via a local DB client);
it's independent of credential rotation and safe to do regardless of deploy/outage state, since the
consuming services already connect via Railway's private network, not the public endpoint.

Also worth checking Railway's own project-level Activity/Audit log (if available) for who enabled
public networking and when, especially if you don't recall doing it yourself -- an attacker with a
compromised Railway API token (not just leaked env var values) could have enabled it directly as
a persistent access point, which is a materially more serious finding than routine forgotten
configuration.

## How to verify a rotation actually took effect

Don't trust that a dashboard showing the "right" value means the running process has it. Test
directly from the consuming service's own console:
```bash
# Postgres
python3 -c "import os, psycopg2; psycopg2.connect(os.environ['DATABASE_URL']); print('CONNECTED OK')"
# Redis
python3 -c "import os, redis; print(redis.from_url(os.environ['REDIS_QUEUE_URL']).ping())"
```
Open a **fresh** console session for this -- a console tab left open from before a restart/redeploy
captures its own stale environment snapshot at the time it was opened, and re-running a command in
an old session proves nothing about the current state.

If a variable shows the right value but the connection still fails with the old credential, check
(in order): whether it's a composed/templated reference needing an actual redeploy rather than a
restart; whether the service is scaled to more than one replica (only some of which may have
cycled); and only then suspect the rotation itself didn't apply correctly.
