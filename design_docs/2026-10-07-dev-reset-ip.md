# `dev resetip`: swap the dev stack to a new LAN IP without rebuilding anything

2026-10-07. Built.

## Problem

Reported while traveling, off the home network: the dev stack's backend services and
jubilo-mobile's own `.env` both have the Mac's LAN IP baked in (`JUBILO_GATEWAY_IP`, the three
`EXPO_PUBLIC_*_BASE_URL` entries, and the gateway's own SSL cert, whose SAN list is `mkcert`'d to
that specific IP). A new network means a new IP, and the only existing path to update it was
`dev setup` -- a full `docker_down` (which destroys the DB outright, see that command's own
docstring on the Postgres services having no persistent volume), a full image rebuild, and fresh
OAuth client credentials for every service. None of that is what's actually needed just because
the Mac's own IP changed.

## What actually needed to change, and what didn't

Checked rather than assumed: `JUBILO_GATEWAY_IP` and the three `EXPO_PUBLIC_*_BASE_URL` values are
the only things in any `.env` file that are IP-dependent. Everything else `dev setup` writes --
`SECRET_KEY`, every `CLIENT_ID`/`CLIENT_SECRET` pair, `DJANGO_SUPERUSER_*`, database URLs -- stays
exactly as it is, since those are what's actually registered in each service's own database
(the auth service's OAuth `Application` rows, specifically); regenerating them without also
re-registering them server-side would desync the `.env` files from what the database expects and
break auth entirely, the opposite of what this needed to do.

## `dev resetip` (`jubilo-cli`)

New command, `./jubilo-cli dev resetip`. Detects the Mac's current IP the same way `dev setup`
already does (`docker_get_host_ip`, `ifconfig en0`), then:

1. `docker_update_gateway_ip` -- a new, narrow function in `services/docker.py` that updates only
   the IP-dependent keys in each of `jubilo-auth/.env`, `jubilo-music/.env`, `jubilo-church/.env`,
   and `jubilo-mobile/.env`, via the existing `_update_env_file` helper's merge semantics (anything
   not listed survives untouched -- the same guarantee `dev setup`'s own generators already rely
   on). Also rewrites the infra root `.env` (`DOCKER_HOST_IP`) and regenerates the gateway's SSL
   cert for the new IP (`mkcert ... {ip} localhost 127.0.0.1`) -- a stale cert for the old IP
   doesn't just look wrong, the TLS handshake against the new IP fails outright.
2. `docker_restart_for_ip_change` -- `docker compose up -d --force-recreate` on the three backend
   web services and their three workers (same `.env` either way, since each worker shares its web
   counterpart's `env_file:` line in `docker-compose.yml`), then a plain restart of the gateway so
   nginx re-reads the cert file that was just overwritten underneath its existing bind mount. No
   `docker_down`, no `docker_build`, no database involvement at all.

Verified against the real stack (confirmed mid-session to actually be on a different network,
`en0` resolving to a non-home IP): ran it, diffed every `.env` before/after (only the IP-dependent
lines changed, every `CLIENT_ID`/`CLIENT_SECRET` identical), and confirmed the gateway answers
over HTTPS on the new IP with a cert that validates against `rootCA.pem`.

## The mobile side needs Metro restarted, not the app rebuilt

A dev client connected to Metro doesn't have `EXPO_PUBLIC_*` baked into the native binary at all --
Expo inlines those into the JS bundle when Metro's own process starts (reading `.env` once at
that point), not into the compiled app shell. So after this command rewrites
`jubilo-mobile/.env`, Metro itself needs restarting (not reinstalling anything) before a reload
picks up the new values -- a plain in-app reload alone keeps serving whatever IP Metro had
cached from its last boot. The command's own output says this explicitly, since "why didn't
reloading the app pick it up" is the natural next question.

Confirmed directly this same session: the already-running `expo start --dev-client --clear`
process was killed and relaunched after `dev resetip` wrote the new IP, and its own startup log
confirmed it re-read `.env` and exported the new `EXPO_PUBLIC_*_BASE_URL` values before Metro
finished booting -- an in-app reload after that serves a bundle built with the new IP, no
native rebuild anywhere in the loop. A real EAS/production build is the one case this can't
help at all -- there, `EXPO_PUBLIC_*` really is compiled into the binary, and only an actual
rebuild changes it.
