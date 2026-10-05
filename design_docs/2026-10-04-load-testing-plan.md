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
| music `/search` | 120/min | user (own bucket) |
| music `/hymn/<id>/audio/download-url` | 30/min | user (own bucket) |
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

### What this says about the two events

Nothing in the realistic profile came within an order of magnitude of a
limit on dev, and the only hard limits found are the ones that are the
same in both environments by construction: the per-user throttles and
the cost of expired tokens on auth. The 2026-10-03 "do nothing more"
call stands. If production is ever measured, the parked runbook above
is the path; the harness has been exercised end to end here, including
the mid-run token refresh behaviour the app itself relies on.
