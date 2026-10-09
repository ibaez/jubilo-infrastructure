# Load testing plan

2026-10-04. Context: district anniversary in ~1 week (~15 people), youth
event in ~2 weeks (200-300 people). The 2026-10-03 scaling recommendation
(`2026-10-03-event-scaling-recommendation.pdf`) already concluded both fit
inside the single-replica production setup; this plan is how we check that
claim with measurements instead of arithmetic, and how we find where the
system actually breaks.

**Decision (2026-10-04): dev stack only for now.** Production data stays
untouched. The harness can target production (`JUBILO_LOAD_TARGET=prod`),
and the plan below keeps that path documented, but it is not to be run
without an explicit decision to do so.

**Capacity target (2026-10-05): at least 500 people per non-convention
event, with margin.** The convention (~35,000, July 2027) has its own
plan in the 2026-10-03 recommendation. Everything sized here assumes the
worst case for shared addresses: all 500 behind a single carrier IP.
`jubilo-auth/.../tests/test_token_throttle.py` pins this number
(`TARGET_ATTENDEES`), so moving the target is a deliberate edit there.

| Layer | What 500 means | Where it stands |
|---|---|---|
| Token refreshes | ~34/min steady (15-min tokens) plus ~50/min during a 10-minute door rush, worst case all behind one address | `/auth/o/token` 200/min per IP, >2x margin; a looping client stays capped |
| Sign-ups at the venue | ~10/min for 50 invitees inside 5 minutes behind one address | invitation validate/accept 60/min per IP |
| Login mistakes | a few failures a minute behind one address | 20 failures/min per IP, successes and page loads free |
| Introspection on auth | one per attendee per 15 min, ~0.6/s | nothing, the 2026-10-03 analysis puts the real cost at convention scale only |
| API load | 500 all active at once is ~100 req/s of the mixed profile; realistic peak a third of that | dev's unthrottled ceiling was ~430 req/s on a laptop; see the 500-VU run under Results |
| Photo uploads | burst of uploads at the event | RQ worker replicas, the lever from the 2026-10-03 recommendation |

## What dev can and cannot tell us

A Docker-on-macOS stack with the three Postgres databases, Redis,
Meilisearch and every service on one laptop is not Railway's 8 vCPU / 8 GB
per service, there is no TLS edge, and the "network" is loopback. So:

| Dev gives us | Dev does not give us |
|---|---|
| Proof the harness works (tokens, request mix, parsing) before anything touches production | The production requests-per-second ceiling |
| Relative cost of each endpoint: a slow serializer or an N+1 query is slow on any hardware, and the ranking carries over | Absolute latencies (expect production to be slower per request over the public internet, faster per core) |
| Throttle behaviour under a crowd, which is pure code and identical in both | Railway edge behaviour (its own limits, TLS cost, DNS re-resolution in the gateway) |
| Concurrency bugs: errors that only appear with many simultaneous users | Postgres behaviour at Railway's connection and memory limits |

Read dev numbers as "which endpoint, and does anything break", not "how
many users".

## What we know before measuring

From the 2026-10-03 recommendation, verified on production at the time:

- gunicorn sync workers: auth 12, music 16, church 16, one replica each,
  one short-lived DB connection per in-flight request (`CONN_MAX_AGE=0`),
  Postgres `max_connections` 500.
- Access tokens live 15 min. jubilo-music and jubilo-church validate a
  bearer token by introspecting it on jubilo-auth once and caching the
  result locally for its lifetime (django-oauth-toolkit resource-server
  mode), so auth sees roughly one introspection per user per 15 minutes,
  not one per request.
- Audio and photos are presigned Cloudflare R2 URLs. The bytes never
  pass through the gateway or gunicorn. The API load is JSON only.

Throttles (jubilo-auth `settings.py`, jubilo-music `settings.py`; church
has none) shape what any load test can even do:

| Where | Rate | Keyed by |
|---|---|---|
| auth `/auth/login` | 20 failures/min | source IP (was 5 requests/min, page loads included, until 2026-10-05) |
| auth `/auth/o/token` | 200/min | source IP (was 10/min until 2026-10-05) |
| auth `/auth/o/authorize` | 10/min | logged-in user |
| auth, any authenticated endpoint | 60/min | user |
| music, any authenticated endpoint | 60/min | user |
| music `/search` | 120/min | user (own bucket; until 2026-10-05 the shared 60/min also applied, so this was unreachable) |
| music `/hymn/<id>` | 120/min | user (own bucket, added 2026-10-05) |
| music `/hymn/<id>/audio/download-url` | 30/min | user (own bucket; same fix as search) |
| church | none | — |

Two consequences. A single simulated user can never exceed 1 request/s on
music, so a crowd needs one account per simulated user. And, until
2026-10-05, one load generator IP could mint at most 10 tokens/min,
which with a 15-minute token lifetime capped the simultaneously-live
users one laptop could sustain at about 150. That limit was never hit by
anything shaped like real traffic here, but reading it prompted the
one change this work led to: phones on a data plan reach the server
through their carrier's shared IPv4 address (the domain has no IPv6
record), so at an event part of the crowd can look like one caller, and
300 attendees on 15-minute tokens need about 20 refreshes a minute
between them. The token limit is now 200/min per IP and login counts
only failed attempts per IP (jubilo-auth, 2026-10-05); see "Change
made" under Results. How concentrated attendees really are behind
shared addresses is unknown, so this is sizing by arithmetic, to be
checked against the auth logs after the youth event.

## Expected event load, for scale

An attendee with the app open makes about 6 requests at boot and then
roughly one request every 5-10 seconds while actively browsing. If every
one of 300 attendees were actively browsing at once, that is about 30-60
requests/s across the services, dominated by music. A realistic peak
(a third active at a time) is 10-20 requests/s. The per-user throttle
caps the theoretical worst case at 300 requests/s on music.

## The harness

Everything is in `jubilo-infrastructure` and runs through `jubilo-cli`
(see `CLAUDE.md` and `services/load.py` for the details):

1. `./jubilo-cli load users <N>` creates `loadtest-NNNN@loadtest.mijubilo.com`
   accounts with the three basic roles (`auth_basic`, `music_basic`,
   `church_basic`), exactly an attendee's scopes. The script is
   `jubilo-auth/scripts/load_test_users.py`; the same command works in
   Railway's Console for production. `load users delete` retires them the
   way the app retires any account: anonymized in place and deactivated,
   never a hard delete (other services hold foreign keys to users).
2. `./jubilo-cli load tokens` mints one token per account into
   `load/tokens.json`, paced under the per-IP throttle (dev: password
   grant via `jubilo_postman`, ~7 s per user; prod: the app's own
   login + PKCE flow, ~13 s per user, since production deliberately has
   no password-grant client).
3. `./jubilo-cli load run <scenario> [KEY=VALUE...]` runs k6
   (`brew install k6`) on `load/k6/<scenario>.js` and saves the summary
   under `load/results/`.

k6 refreshes expired tokens itself (one `/o/token` call per user per 15
minutes, backing off on 429), so a token set minted once lasts a whole
session of runs. Every scenario is GET-only.

### Scenario `youth_event` (closed model: N attendees)

Each VU is one attendee doing what the app does: boot (`/auth/user/me`,
church `/participant/me`, `/event?when=recent|upcoming`, music `/playlist`,
`/hymn/next-autoplay`), then 3-5 actions with 4-10 s think time drawn from
search → hymn detail → presigned audio URL (65%), topics (15%), recently
updated/added lists (10%), event photos (10%). About 15 requests per
60-90 s session, under the 60/min per-user throttle.

Pass: p95 under 1 s, p99 under 2.5 s, under 1% failed requests. 429s are
counted as failures on purpose: at a real event they are user-visible
errors, and if the scenario trips them it means the throttle, not
capacity, is the first limit a crowd hits.

Knobs: `VUS` (default 50), `RAMP` (2m), `HOLD` (5m), `THINK_MIN`/`THINK_MAX`.

### Scenario `ceiling` (open model: climbing request rate)

Requests arrive at a fixed, climbing rate no matter how slow responses
get, which is what a crowd does. Stages 25 → 50 → 100 → 150 → 200 → PEAK
requests/s (or any list via `STAGES=200,300,400`), `STEP` long each
(default 1m). Every request is tagged with its stage, so the summary is
a per-rate table of p95 and failure rate. The run aborts itself once p95
passes 2 s for 20 s; the last stage with flat latency and no dropped
iterations is the ceiling.

- `MODE=church` (default): one cheap authenticated GET through the whole
  path (edge, gateway, gunicorn, token cache, Postgres). Church has no
  per-user throttle, so it is the only Django path that can be pushed
  past 1 request/s per account.
- `MODE=gateway`: `GET /invite/`, a static file from nginx. Isolates the
  edge and gateway from Django.
- `MODE=music`: `GET /api/music/topic` (Redis-cached). Throttled, so the
  usable ceiling is about 1 request/s times the number of accounts and
  429s are expected past that.

Also watch `dropped_iterations`: if k6 could not keep the rate, the laptop
running k6 is the bottleneck and the rate on the axis was not actually
sent.

## Runbook (dev)

```bash
cd jubilo-infrastructure && source .venv/bin/activate
./jubilo-cli load users 100
./jubilo-cli load tokens                             # ~12 min for 100
./jubilo-cli load run youth_event VUS=100 HOLD=5m    # the youth event, all active at once
./jubilo-cli load run ceiling MODE=gateway
./jubilo-cli load run ceiling MODE=church
./jubilo-cli load run ceiling MODE=church STAGES=200,300,400,500,600,800 STEP=30s   # bracket the break
./jubilo-cli load run ceiling MODE=music STAGES=25,50,100,150 STEP=30s
```

Before each run the CLI refreshes any token that would expire during it
(about 7 s per token under the throttle) and after each run it saves the
refresh tokens k6 rotated back into `load/tokens.json`, so the token set
only has to be minted once per session.

During a run, `docker stats` shows which container is CPU-bound, and
`docker compose logs -f jubilo_music` shows gunicorn worker timeouts
(`[CRITICAL] WORKER TIMEOUT`), the signature of a saturated sync worker
pool.

## Runbook (production, parked)

Not to be run under the 2026-10-04 decision. Recorded so the path is not
lost:

1. In Railway's Console for jubilo-auth:
   `python -m scripts.load_test_users create 100 --password '<pw>'`, and
   paste its output into `load/users.csv`.
2. `JUBILO_LOAD_TARGET=prod ./jubilo-cli load tokens` (about 22 min for
   100 users; interruptible, the file is kept current).
3. `JUBILO_LOAD_TARGET=prod ./jubilo-cli load run youth_event VUS=100`
   during a quiet hour, with the Railway metrics tab open for each
   service (CPU, memory, and each Postgres's connection count).
4. `JUBILO_LOAD_TARGET=prod ./jubilo-cli load run ceiling MODE=gateway`,
   then `MODE=church`.
5. Afterwards, `python -m scripts.load_test_users delete` in the same
   console (anonymizes and deactivates them, the app's standard account
   deletion). Minting leaves only expired token rows behind; the k6 runs
   write nothing.

What it would add over dev: the real per-request latency over the public
internet, Railway's edge limits, and where the 16-worker pools and the
Postgres connection counts actually sit under a crowd.

## Results (dev, 2026-10-04)

Local stack on an Apple M3 Pro, Docker VM with 12 CPUs / 8 GB, same
gunicorn worker counts as production (auth 12, music 16, church 16), k6
on the same machine. Raw k6 summaries and logs are in `load/results/`
(gitignored, local only). Remember the caveat at the top: these rank
endpoints and prove the harness; they are not production capacity.

### `youth_event`, 100 attendees all active at once (2 min ramp, 3 min hold)

| | |
|---|---|
| Requests | 6,874 over 6 min, 19/s at the plateau |
| Failed | 0 (no 429s, no 5xx) |
| Latency, all requests | median 16 ms, p95 39 ms, p99 235 ms, max 482 ms |
| music `/search` | median 15 ms, p95 30 ms |
| music `/hymn/<id>` | median 17 ms, p95 32 ms |
| Token refreshes during the run | 27, all succeeded, none throttled |
| Container CPU | nothing above a few percent at the plateau |

100 simultaneously active attendees is the pessimistic reading of a
300-person event (a third active at any moment), and on dev it does not
register. Every request path the app uses on boot and while browsing
returned 2xx under concurrency, which is the correctness result this run
was really for. The p99 tail is the first request each VU makes after
boot (cold token-cache lookups and Meilisearch's first query for a term),
not sustained slowness.

### `youth_event`, 500 attendees all active at once (2 min ramp, 3 min hold), 2026-10-05

The capacity-target run: every one of 500 attendees browsing at the same
time, which is more pessimistic than any real event of that size.

| | |
|---|---|
| Requests | 33,972 over 5.5 min, ~95/s at the plateau |
| Failed | 0 (no 429s, no 5xx) |
| Latency, all requests | median 15 ms, p95 48 ms, p99 212 ms, max 929 ms |
| music `/search` | median 13 ms, p95 23 ms |
| music `/hymn/<id>` | median 16 ms, p95 28 ms |
| Container CPU at peak | music ~92% of a core, music's Postgres ~90%, church's Postgres ~56%, everything else under 10% |

Five times the earlier 100-attendee run, same flat latency, zero
failures. The one thing worth noting is where the CPU went: music's
Postgres is as busy as music itself at under 100 requests/s of simple
queries, the same pattern the church ceiling showed. With
`CONN_MAX_AGE=0` every request opens and closes a database connection,
and connection setup is the most expensive thing Postgres does for a
cheap query, so this is the strongest hint yet that item 4 on the list
(persistent connections) is the next real capacity win. Not needed for
500 people; it is headroom.

### `ceiling MODE=gateway` (nginx static file, no Django)

Climbed to 400 requests/s with p95 3 ms, zero failures, zero dropped
iterations. The gateway and the k6 host are not a limit anywhere in the
range that matters for either event.

### First `ceiling MODE=church` attempt: a harness finding, not a service one

Aborted at 32% failures after 32 s with church itself healthy. The
tokens were 20-30 min old, so every VU tried to refresh at once; 34 hit
the 10/min per-IP `/o/token` throttle and 94 failed outright because
the previous run had already rotated their refresh tokens (jubilo-auth
sets `ROTATE_REFRESH_TOKEN` with no grace period) and k6 cannot write
the new ones to disk. Two things came out of it:

- The harness now logs every rotation from k6 and merges it back into
  `load/tokens.json` after each run, and refreshes any token that would
  expire during a run before starting it (`JUBILO_LOAD_REFRESH_HORIZON_SECONDS`,
  default 10 min), re-minting from `load/users.csv` when a refresh token
  is stale.
- An expired or invalid bearer token is expensive for **auth**, not for
  the service that receives it: jubilo-auth hit ~250% CPU during those
  32 s while church and its database stayed under 10%. Each rejected
  token makes church re-introspect it, and introspection runs the
  uncached PBKDF2 client-secret check the 2026-10-03 recommendation
  already identified as the convention-scale bottleneck. Valid tokens
  are cached per service for their 15-minute lifetime and never touch
  that path. Worth remembering for the convention: a crowd whose tokens
  all expire at the same moment (everyone opened the app at the doors)
  is a burst of introspections, and the fix is the one already written
  up there (cache or remove the outer `authenticate_client()` check).

### `ceiling MODE=church`, bracketed 200 → 800 requests/s (30 s stages)

| Offered rate | Delivered | p95 | Failed |
|---|---|---|---|
| 200/s | 200/s | 18 ms | 0% |
| 300/s | 300/s | 22 ms | 0% |
| 400/s | ~350/s | 143 ms | 0% |
| 500/s | ~430/s | 790 ms | 0.3% |
| 600/s | ~440/s | 770 ms | 0.1% |
| 800/s | ~410/s | 870 ms | 0.4% |

Dev's ceiling for the full authenticated Django path is about **430
requests/s**: flat to 300, the queue forms at 400, and above that the
delivered rate stops growing while k6 drops the iterations it cannot
send (12,755 dropped over the run). jubilo-church sat at ~90% of a core
and its Postgres at ~80% of a core; gateway, Redis and auth stayed
under 10%. The few failures past 400/s are token-refresh races between
VUs sharing accounts at saturation, not church errors (church itself
returned 2xx on 99.85% of 59k requests).

For scale: the realistic youth-event peak is 10-20 requests/s across
all services and the throttle-capped worst case is 300/s. Even on a
laptop sharing cores with three databases, the one unthrottled service
sits above both.

### `ceiling MODE=music`, 25 → 150 requests/s with 100 accounts

| Offered rate | p95 (served) | 429 throttled |
|---|---|---|
| 25/s | 13 ms | startup noise only |
| 50/s | 15 ms | 0.8% |
| 100/s | 25 ms | 49% |
| 150/s | 15 ms | 70% |

Exactly the throttle arithmetic: 100 accounts at 60/min is 100
requests/s of budget, so 50/s is clean, 100/s loses half, 150/s loses
70%. Music itself answers in ~10-15 ms throughout and never slows down;
the per-user limit, not capacity, is the first thing a crowd meets on
music. Whether 60/min per user is the right number for an event where
people flip between hymns quickly is a product question this surfaces,
not an infrastructure one (the app's own search budget is already
separate at 120/min for that reason).

### Change made from these results (2026-10-05)

Not from a measured limit: nothing shaped like real traffic hit a
throttle in these runs (the one burst of token 429s was the harness
refreshing 100 expired tokens at once, against the dev stack's relaxed
100/min). Reading the production settings while sizing the harness is
what raised it. `/auth/o/token` allowed 10 requests a minute per source
IP, and `/auth/login` 5 a minute per IP counting page loads and
successful sign-ins. Phones on a data plan reach the server through
their carrier's shared IPv4 (carrier NAT; `www.mijubilo.com` has no
IPv6 record), so at an event some unknown share of the crowd looks like
one caller, and 300 people on 15-minute tokens need about 20 refreshes
a minute between them.

Changed in jubilo-auth, with tests: the token limit is 200/min per IP
(covers the 500-person target even if every one of them shares a single
address, with >2x margin, and keeps a cap on a buggy build refreshing
in a loop), invitation validate/accept are 60/min per IP (a sign-up
rush at the door; the tokens are 256-bit random so these were never a
guessing defense), and login counts only failed attempts per IP at 20/min (`accounts/utils/
login_lockout.py`, the mirror of the per-account lockout), so a few
strangers mistyping behind one address cannot lock each other out while
password spraying from one place is still limited. A per-credential
keying for the token endpoint was tried and dropped: refresh tokens
rotate on every use, so it would have let a looping client refresh
without limit. How to find out whether any of this was needed: after
the youth event, search Railway's jubilo-auth logs for status 429 on
`/auth/o/token` and `/auth/login`.

Still per-IP and unchanged: `password_reset_validate` (5/min, it guards
a 6-digit code and has its own per-account lockout), `logout` and
`oauth_revoke` (20/min, rare at an event). `anon` (4/min) turns out not
to apply to any endpoint an attendee uses: the sign-up and reset views
replace the default throttle classes with their own scoped one, and
everything else requires authentication before throttling is checked.

### Follow-ups done (2026-10-05)

**Browsing budget on music.** The music ceiling run showed the 60/min
per-user rate was the first limit a crowd meets. Looking at why also
turned up that the "isolated" search scope never was: every scoped view
listed DRF's `UserRateThrottle` next to its own scope, and that
throttle's bucket is shared by every view for the user, so the global
60 tripped before search's 120 could. The three browsing endpoints
(search, hymn detail, presigned audio URL) now list only their own
scope; hymn detail got one (120/min). Everything else still shares the
60/min. Pinned by `jubilo-music/.../tests/misc/test_browsing_throttle.py`,
which fails against the old wiring on the 61st search of a minute.

**Access logs with request time on music and church.** gunicorn now
logs every request on all three services as
`<gateway ip> <x-forwarded-for> "<request>" <status> <bytes> <micros>us`,
matching what auth already had plus the forwarded-for column. On event
day that is how to tell a slow screen from a slow network, and the
forwarded-for column answers the open question behind the throttle
sizing: how many attendees actually share one carrier address.
Verified on the rebuilt dev containers. Doing so turned up that
jubilo-auth's access log had never worked: its `LOGGING` sets
`disable_existing_loggers: True`, which silenced gunicorn's access and
error loggers in every worker, so production had no per-request log and
no worker-timeout messages from auth. Fixed by setting it to `False`,
matching music and church (and Django's recommendation); the setting
only ever touched loggers created before Django's setup, so nothing
else changes. The "check Railway's jubilo-auth logs for 429s" step
above only works once that is deployed.

### Found from the first production access lines (2026-10-05)

With auth's access log finally reaching Railway, one request from one
phone showed the forwarded-for chain `136.51.59.72, 152.233.76.9,
100.64.0.3`: the phone, Railway's edge, Railway's internal hop. Three
proxies, not the one `NUM_PROXIES: 1` assumed, so DRF had been taking
the last entry, a rotating Railway-internal address, as the client
identity. Every per-IP throttle in production (token, sign-up, password
reset, logout, and the new login failure counter) had been keyed on
Railway's own hops: in practice never applied to a real caller, and not
spoof-resistant either. `NUM_PROXIES` is now 3, pinned by tests that
use that exact chain. The gateway's comments describing a single hop
are corrected. This also changes what the event checks mean: the
per-IP limits will be applying to real client addresses for the first
time, so the 429 search after the youth event is the first real
measurement of them.

Two more things the same lines showed. Introspection calls from music
and church take 420-580 ms each, which is the uncached PBKDF2 client
check the 2026-10-03 recommendation describes, now observed rather than
inferred; still once per token per service, so fine at event scale. And
a `CacheKeyWarning` on every introspection shows one service's OAuth
client id in production is the literal string
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`: the
command that was meant to generate it was pasted as the value. It works
(the registered id matches), so this is cosmetic plus log noise, but it
is worth rotating to a real random id by hand in Railway's variables
for both jubilo-auth's provisioning and the service that uses it.
(Done 2026-10-08, by hand in Railway's variables.)

### A tripwire on the chain NUM_PROXIES assumes (2026-10-07)

The three-hop chain above is Railway's routing, not a contract, and it
can change in either direction without notice. A hop added puts one of
Railway's own addresses in the slot the throttles key on, so every
phone shares one bucket and the app 429s -- loud, but found on a
Sunday. A hop removed makes `get_ident()` clamp to the leftmost entry.
Checked the same day (2026-10-08) by sending one request through the
gateway with two forged entries prepended: the line auth logged was
`136.51.59.72, 79.127.177.113, 100.64.0.13` -- the forged entries were
gone entirely. Railway's edge REPLACES a client-supplied
X-Forwarded-For rather than appending to it, so a client cannot get an
entry into the chain in any position, and the leftmost entry is the
real client. A hop removed is therefore not spoofable on its own today;
it would take the edge also starting to append -- two independent
Railway behaviors changing -- and nothing would notice either one by
itself. (The client cannot reach a later slot otherwise either: auth
has no public domain, and the gateway is only reachable through the
edge, so there is no path with fewer trusted hops.) The same line
showed the edge's own address differs from the 2026-10-05 one
(`79.127.177.113`, not `152.233.76.x`): the hop count is stable, the
addresses are not, so nothing may pin a Railway range. The internal
hop rotates too (`100.64.0.3`, `.10`, `.13`, `.15` across four
requests).

The same log window showed the private-network path for the first
time. Music's and church's `POST /auth/o/introspect` lines carry NO
`X-Forwarded-For` at all (`-` in the log), with `REMOTE_ADDR`
`10.151.15.109`, Railway's internal proxy -- a different address from
the one fronting the gateway's connections (`10.175.248.154`). So the
middleware skips that path on the missing header before its path
exemption is even consulted; the exemption stays as the guard for the
day Railway starts adding the header there. The part that matters for
capacity: with no header, DRF keys the `introspect` throttle
(3000/min) on `REMOTE_ADDR`, and that address was the same on every
introspect line seen. If music and church share it, 3000/min is one
system-wide ceiling on introspections, not a per-service one: at
15-minute tokens and two resource servers, about 22,500 people with
the app open in the same window before introspection itself 429s --
under the Sunday-morning figure the 50-100k growth plan implies, and a
limit independent of the PBKDF2 cost (those lines took 412-452 ms
each, the same ~0.5 s as before).

Raised the same day to `60000/min`, pinned by a new capacity test in
`test_token_throttle.py` the way `oauth_token` already is: every
registered user of the planned rollout (100,000) opening the app inside
one 15-minute token lifetime, introspected once by each of the two
resource servers, is ~13,333/min; x2 margin is 26,667, which 3000
failed and 60000 clears. Not re-keyed on the authenticated client,
though that was the first idea: the endpoint is internal-only, so the
rate was never a defence against callers, and the only job left for
it -- catching a runaway-loop bug in a service -- is one a rate cannot
do at this scale. A sync-gunicorn service is bounded by its own worker
count, so even one re-introspecting on every request lands in the
same range as a legitimate Sunday peak; the two are not separable by a
number. That failure shows in CPU and the access log instead. The
settings comment that had called 3000 a runaway backstop that "should
hold without retuning" was wrong on both counts and now says so. Note
the ordering: at one replica auth can only serve ~1,440 introspections
a minute (24/s of PBKDF2), below even the old 3000, so the throttle
was never the binding limit yet -- it would have become one the moment
the PBKDF2 cost is fixed or replicas are added, which is why it is
raised now rather than then.

### Introspection ceiling, before and after the client hasher (2026-10-08)

Measured on the dev stack, same day the hasher landed in jubilo-auth
(`design_docs/2026-10-08-client-secret-hasher.md` there). Two things the
first attempt taught about measuring this at all:

- **Not through the gateway.** Both gateway configs return 403 for
  `/auth/o/introspect` -- that is the internal-only rule working -- so a
  run through `BASE_URL` measured nginx's 403 at 2 ms and nothing else.
  `ceiling.js` gained `MODE=introspect` with an `INTROSPECT_URL` override
  that points straight at jubilo-auth the way a service does; in dev the
  container publishes port 8000, so
  `INTROSPECT_URL=http://192.168.86.15:8000/auth/o/introspect` (the LAN
  IP is in auth's `ALLOWED_HOSTS`; localhost is not).
- **Expired tokens are fine.** The client-secret check runs before the
  token lookup and costs the same whether the token is live; an expired
  one answers `{"active": false}` with a 200. So nothing here depends on
  refreshing the 500 minted tokens, and
  `JUBILO_LOAD_REFRESH_HORIZON_SECONDS=0` skips that pre-flight.

Open model (`ramping-arrival-rate`), 30 s per stage, one dev auth
container (12 sync workers), the music service's credentials:

| arrival rate | before (full PBKDF2) p95 | after (client hasher) p95 |
|-------------:|-------------------------:|--------------------------:|
|      10 rps  |                  406 ms  |                         - |
|      25 rps  |                  728 ms  |                     20 ms |
|      50 rps  | 2.2 s, iterations dropped, **run aborted** | 11 ms |
|     100 rps  |          (never reached) |                     12 ms |
|     200 rps  |          (never reached) |                     10 ms |
|     300 rps  |          (never reached) |                      9 ms |
|     500 rps  |          (never reached) |            12 ms, 0% failed |

Before: a ceiling of roughly 25-35 introspections/s -- where 12 workers
divided by ~0.4 s lands. After: no ceiling found by 500 rps, the highest
stage run; 27,881 requests, none failed. A single call by hand went from
350-470 ms (measured while the ramp was running, the same band as
production's 412-452 ms) to ~10 ms. The `--apply` step itself was the
production runbook rehearsed: dry run reported both apps as
`pbkdf2_sha256`, `--apply` re-hashed both, and music's credentials kept
working against the re-hashed secret without any change on music's side.

Same caveat as every dev number in this document: relative, not
production capacity. The relative result is the point -- the thing that
capped introspection is gone, and whatever caps it next (the token
lookup, the throttle, replicas) is well past anything the rollout
needs. Results: `load/results/20261008-110520-dev-ceiling.json` (before)
and `20261008-110839-dev-ceiling.json` (after).

**The new ceiling, found and attributed.** A third run pushed the
after-state from 500 to 2,000 rps (30 s stages) with `docker stats`
sampled every 15 s alongside:

| arrival rate | p95 | median | note |
|-------------:|----:|-------:|------|
|    500 rps | 10 ms |   6 ms | |
|    750 rps | 38 ms |   8 ms | the knee |
|  1,000 rps | 458 ms | 124 ms | saturated |
|  1,250 rps | 466 ms | 335 ms | plateau: the server serves what it can |
|  1,500 rps | 443 ms | 328 ms | " |
|  2,000 rps | 430 ms | 311 ms | ", 0.39% failed, 51k iterations dropped |

Roughly **900 introspections/s actually completed** on one dev auth
container -- the flat-latency plateau with dropped iterations is the
open model's signature for "the server is serving all it can", not a
k6 limit. Attributed by the samples: jubilo-auth's CPU at 880-935%
(nine-plus of its twelve sync workers' cores flat out), its Postgres at
70-83% of a core (the token and application lookups), Redis at 20-27%
(the throttle's per-key history list, visible, not binding). **Zero
429s** -- the `introspect` throttle never engaged, because the rate
actually served (~54k/min) sat just under its 60k/min. Before the
hasher the same container's ceiling was 25-35/s; this is ~30x. Result:
`load/results/20261008-112311-dev-ceiling.json`.

Two things that follow for production:

- Production's auth replica has 8 vCPU to this container's ~9.4
  effective, so expect a per-replica ceiling nearer 700-800/s there.
  The rollout's worst case is ~222/s (100k users, both resource
  servers, one 15-minute window); the convention's ~78/s. Both fit one
  replica with room.
- Once there is more than one auth replica, the throttle becomes the
  system ceiling, not CPU: every replica keys introspection on the same
  `REMOTE_ADDR` (Railway's internal proxy, no `X-Forwarded-For` on that
  path), so 60,000/min is 1,000 introspections/s for the whole system
  however many replicas serve it -- about 450,000 people with the app
  open in the same window, 4.5x the rollout's worst case. Fine for the
  foreseeable future; the number to revisit if the user base ever
  approaches that.

Lesson for the runner, applied: `JUBILO_LOAD_REFRESH_HORIZON_SECONDS=0`
does not skip refreshing tokens that have ALREADY expired, so this
run spent eight minutes refreshing the 500 accounts serially before
sending a single request. `JUBILO_LOAD_SKIP_REFRESH=1` now skips the
pre-flight outright (`services/load.py`), for a scenario that never
needs a live token.

`ProxyChainTripwireMiddleware` (jubilo-auth, `accounts/middleware.py`,
first in `MIDDLEWARE`) measures what `NUM_PROXIES` assumes on every
request and logs a warning the day it stops holding: fewer entries
than `NUM_PROXIES`, or a non-public address in the key slot
(`ipaddress.is_global`, which correctly excludes Railway's
100.64.0.0/10 internal hop where `is_private` would not). Once per
chain shape per gunicorn worker, so a changed topology is a dozen
lines after a deploy, not one per phone. It changes nothing about the
request; the fix is still reading the access log and setting the new
count, as it was set the first time. `/auth/o/introspect` is exempt:
it arrives over the private network with a shorter chain by design
(the gateway 403s it from outside). Off in `settings_dev`
(`PROXY_CHAIN_TRIPWIRE`), where there is no edge and the chain is
legitimately one entry long. What it cannot see: a hop added whose
address is public, which looks healthy per request and only shows as
the bucket collapse itself.

Found running the suite for this: `BaselineSecuritySettingsTests`
(HSTS seconds, secure cookies, SSL redirect) fails under the
container's `settings_dev`, with or without this change -- those
guards relax exactly the values they guard in dev, so they only ever
pass against production settings. Not fixed here; noted so it is not
mistaken for a regression next time.

The hypothesis from the earlier runs: `CONN_MAX_AGE=0` means every
request opens and closes a fresh Postgres connection, and connection
setup is the most expensive thing Postgres does for a cheap query --
which is why the databases looked as busy as the services themselves at
well under 100 requests/s. Set `conn_max_age=60` and
`conn_health_checks=True` (django's health-check ping so a 60s-old
connection that Railway recycled underneath a worker is silently
replaced instead of erroring) on all three services, same settings.py
pattern, and reran the two heaviest dev scenarios back to back on the
same stack, same moment, nothing else changed.

**Church ceiling bracket, 200 → 800 req/s offered:**

| Stage | p95 before | p95 after | requests delivered before | after |
|---|---|---|---|---|
| 200/s | 96 ms | 15 ms | 3,149 | 3,149 |
| 300/s | 68 ms | 16 ms | 7,499 | 7,500 |
| 400/s | 2.77 s | 410 ms | 7,690 | 10,199 |
| 500/s | 1.16 s | 476 ms | 9,350 | 13,477 |
| 600/s | 1.52 s | 841 ms | 9,111 | 14,295 |
| 800/s | 1.18 s | 910 ms | 9,178 | 13,790 |

Total delivered throughput over the bracket: 254 req/s average before,
346 req/s after (+36%). Dropped iterations (k6 unable to even send the
offered rate): 26,172 before, 9,739 after (-63%). Zero failures either
way -- church was never wrong, only queued.

**500-attendee youth_event, same closed-model profile as the earlier
run:**

| | Before | After |
|---|---|---|
| p95 | 48 ms | 30 ms |
| median | 15 ms | 8 ms |
| music's Postgres, peak CPU | 90% of a core | 10% of a core |
| church's Postgres, peak CPU | 56% of a core | 9% of a core |
| music service, peak CPU | 92% of a core | 85% of a core |

Database CPU dropped by roughly 85-90% at the same offered load and
latency roughly halved. The "before" numbers here are from today's
stack (after the access-log change), not the 2026-10-04 figures earlier
in this doc, which predate it -- the two access-log runs bracket the
before/after pair fairly, but are not directly comparable to the very
first 500-attendee run further up.

One persistent connection per worker per replica, not per request:
12+16+16=44 workers today against a 500 `max_connections` ceiling per
database, clear even at several replicas each (see the settings.py
comment for the arithmetic). Full test suites (auth/music/church) pass
unchanged after the setting, confirming it is purely a connection-
lifecycle change, not a behavior one.

Live in dev. Not yet in production -- this is a config change to how
all three services talk to their databases, the user's call on when to
ship it, same as every production step in this project.

### Photo pipeline: the worker's seconds per photo (2026-10-08)

Pictures are the one write path the rollout and the convention put at
user scale. The gate is `has_picture_submit_authority`, not the official
one: any current member can add up to `MEMBER_PICTURES_MAX_PER_EVENT` = 5
photos (8 MB each) to an event of their church or community once it has
started, and with `MEMBER_PHOTOS_PUBLISH_IMMEDIATELY` on, any member with
a church can at a kingdom-tier event -- so the convention's ~35,000
members are up to 175,000 photos over the week. Each is a multipart POST
the church web process accepts (writing the source to R2 inside the
request) and a job for the single `rqworker` process: decode, EXIF
transpose, three LANCZOS resizes, three JPEG encodes, three R2 PUTs,
one R2 DELETE -- and, first, a GET of the source from R2, since the web
and worker processes share nothing but storage. Photo lag is that
worker's seconds per photo.

**Harness:** `./jubilo-cli load photos COUNT=50 CONCURRENCY=8`
(`services/load_photos.py`; dev only by construction, deletes what it
creates). A 4000x3000 JPEG of 3.22 MB with an EXIF orientation tag, the
shape of a phone photo, generated once by Pillow inside the worker
container (this repo's venv has no Pillow) and cached at
`load/photo.jpg`. Uploaded as the dev superuser to a throwaway
kingdom-tier event (official authority: no cap, no started-event rule;
the worker does not care who uploaded). It reports the ingest side, the
drain, and -- `BENCH=5` -- the same Pillow work on the same bytes inside
the worker container with no storage at all: the CPU part isolated.
Unit tests for the stack-free parts in `tests/`.

**Results, three runs (one dev worker container, real Cloudflare R2
over the Mac's home uplink):**

| | 50 photos, 8 concurrent | 20, 8 | 20, 8 |
|---|---|---|---|
| ingest: uploads/s | 8.0 | 7.7 | 7.2 |
| ingest: POST median / p95 | 0.81 s / 1.80 s | 0.79 / 1.54 | 0.95 / 1.20 |
| backlog when the last POST returned | 50 | 20 | 20 |
| worker: seconds per photo, wall clock | **3.70** | 3.66 | 3.65 |
| worker: photos/s | 0.27 | 0.27 | 0.27 |
| last photo ready, after the last upload | 181 s | 72 s | 71 s |
| CPU bench, median of 5, no storage | **0.49 s** (0.48-0.50) | | |

Outputs per photo: 2,210 KB full (4000 px), 130 KB viewing (1600 px),
4 KB thumbnail (300 px). Worker CPU during the drain 13-29% of a core,
church web 0-9%, its Postgres under 4%, gateway under 4%.

**What it says.** The worker is serial and spends ~0.5 s of every 3.7 s
computing; the other ~3.2 s is the R2 leg (one 3.2 MB GET, a 2.2 MB
PUT, two small PUTs, a DELETE), which in dev rides a home uplink and in
production rides Railway's link to Cloudflare -- so 3.7 s is a dev
number that will not transfer and 0.5 s is the part that will (a
Railway vCPU vs a Mac core, the usual caveat). Even at a guessed
production R2 leg of 0.5-1 s, one worker process is ~1-1.5 s per photo:
~2,400-3,600 photos an hour, 50 a minute. A church's Sunday is fine on
that; a convention evening where 5,000 photos land in an hour is 2-3
photos/s and needs several worker processes.

The lever is cheap because the worker is I/O-bound: `rqworker` is one
process, so N processes (or N worker replicas -- RQ hands each job to
one worker, nothing else changes) give close to N times the throughput
until CPU binds, at roughly 2 photos/s per core at 0.5 s CPU each. That
is the convention-week item: measure production's own seconds per photo
from the worker's log timestamps on an ordinary event (there is no
updated timestamp on `EventPicture`; `created_dttm` to the worker's log
line) and size the worker count from it -- a job for before July 2027,
not before invitations open, since a slow drain shows as photos
arriving minutes late, not as errors.

The ingest side is a behaviour check here, not a ceiling: `PictureCreate`
holds a gunicorn sync worker for the whole R2 write of the source, 0.8 s
median in dev at 8 concurrent, and the gateway buffers the body first
(`client_max_body_size 22m`), so a slow phone never holds a Django
worker. With 16 sync workers per church replica, uploads/s per replica
is 16 over production's per-upload R2 write time -- unmeasured, and the
number to watch at the convention if church replicas are the question.
Nothing misbehaved at 8 concurrent uploads: no 5xx, no lost enqueue,
every photo reached `ready`, every delete returned 204.

### What this says about the two events

Nothing in the realistic profile came within an order of magnitude of a
limit on dev, and the only hard limits found are the ones that are the
same in both environments by construction: the per-user throttles and
the cost of expired tokens on auth. The 2026-10-03 "do nothing more"
call stands. If production is ever measured, the parked runbook above
is the path; the harness has been exercised end to end here, including
the mid-run token refresh behaviour the app itself relies on.
