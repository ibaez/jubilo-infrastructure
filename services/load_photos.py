"""
Photo-pipeline ceiling for jubilo-church, dev only -- `jubilo-cli load photos`.

Pictures are the one write path the invitation rollout and the convention
put at user scale: any current member can add up to five photos to an
event of their church or community (and, at a kingdom-tier event, any
member with a church), so a Sunday or a convention week is tens of
thousands of uploads, each one a multipart POST that the web process
accepts and a job the single rqworker (`jubilo_church_worker`) grinds
through -- decode, EXIF transpose, three LANCZOS resizes, three JPEG
encodes, three R2 PUTs, one R2 DELETE. Photo *lag* (how far behind the
worker falls) is decided by that worker's seconds per photo, which is
what this measures.

Two numbers come out, and they are not equally trustworthy:

  worker drain   photos/s the worker sustains and seconds per photo,
                 wall clock, against real Cloudflare R2. In dev the four
                 R2 operations ride the Mac's home uplink; in production
                 they ride Railway's. So the CPU part transfers (a dev
                 core vs a Railway core, the same caveat as every number
                 in design_docs/2026-10-04-load-testing-plan.md) and the
                 network part does not.
  cpu bench      the same Pillow work on the same photo, N times, inside
                 the worker container, no storage at all: the part that
                 transfers, isolated. The gap between the two is the R2
                 leg.

Ingest (the POST side) is reported too -- accept latency, uploads/s, the
backlog the worker had when ingest finished -- but as a behaviour check,
not a ceiling: PictureCreate writes the source bytes to R2 inside the
request, so dev's uploads/s is the home uplink again.

Unlike the k6 scenarios (GET-only) this creates data: an event, N
pictures, N x 4 R2 objects. It refuses any target but dev and deletes
everything it made unless KEEP=1 (the pictures and, if it created one,
the event). The upload is as the dev superuser -- kingdom-wide official
authority, so no per-member cap and no has-the-event-started rule, and
the worker does not care who uploaded.

  ./jubilo-cli load photos                      50 photos, 8 at a time
  ./jubilo-cli load photos COUNT=200 CONCURRENCY=16
  ./jubilo-cli load photos EVENT=17             an existing event instead of a throwaway one
  ./jubilo-cli load photos BENCH=0 KEEP=1       skip the CPU bench; leave the data

The test photo is what a phone sends: 4000x3000 JPEG, a few MB, with an
EXIF orientation tag so exif_transpose has real work to do. It is
generated once by Pillow inside the worker container (this repo's venv
has no Pillow on purpose) and cached at load/photo.jpg, gitignored.
"""

import base64
import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from services.load import (
	HTTP_TIMEOUT_SECONDS, INFRA_ROOT, LOAD_DIR, RESULTS_DIR, LoadFailure, _base_url, _mint_dev, _target, _verify,
)

PHOTO_FIXTURE = LOAD_DIR / "photo.jpg"
WORKER_SERVICE = "jubilo_church_worker"

# services/e2e.py's dev superuser -- `dev setup` creates it.
SUPERUSER_EMAIL = "su1@gmail.com"
SUPERUSER_PASSWORD = "su1"

DEFAULTS = {"COUNT": 50, "CONCURRENCY": 8, "EVENT": None, "BENCH": 5, "KEEP": 0, "POLL": 2}

# Per photo, on top of a floor: a serial worker at a few seconds a photo
# is the expected shape, so the timeout scales with COUNT.
DRAIN_TIMEOUT_FLOOR_SECONDS = 120
DRAIN_TIMEOUT_PER_PHOTO_SECONDS = 15

UPLOAD_TIMEOUT_SECONDS = 60

# EventCategory ids are fixed seed data (hundreds = tier; 4xx kingdom);
# 401 is the convention, the one kingdom-tier category the superuser can
# post to without any collective/church/community.
CONVENTION_CATEGORY_ID = 401

# Shared by the fixture generator and the CPU bench, both run inside the
# worker container, so the bench measures exactly the bytes that were
# uploaded. 4000x3000 is a 12 MP phone photo; the Mandelbrot set colorized
# gives it structure and a blend of Gaussian noise gives it the per-pixel
# detail (sensor grain) that decides a JPEG's size -- the fractal alone is
# 0.5 MB at this quality, with the noise 3.2 MB, a typical phone JPEG.
# Flat colour would compress to nothing and resize for free. Orientation 6
# = rotated 90 degrees, the common portrait case.
PHOTO_GENERATOR_SRC = """
import io
from PIL import Image, ImageOps

def make_photo():
	mandel = Image.effect_mandelbrot((4000, 3000), (-2.2, -1.5, 1.2, 1.5), 100)
	photo = ImageOps.colorize(mandel, black=(12, 24, 64), white=(250, 232, 184), mid=(198, 84, 40))
	photo = Image.blend(photo, Image.effect_noise((4000, 3000), 40).convert('RGB'), 0.12)
	exif = Image.Exif()
	exif[0x0112] = 6
	buffer = io.BytesIO()
	photo.save(buffer, format='JPEG', quality=92, exif=exif)
	return buffer.getvalue()
"""

FIXTURE_SCRIPT = PHOTO_GENERATOR_SRC + """
import base64, sys
data = make_photo()
sys.stdout.write('LOADPHOTO ' + base64.b64encode(data).decode() + '\\n')
"""

# Runs under `manage.py shell` so the real task helpers import; the real
# functions, not a re-implementation, so a change to the pipeline (a new
# copy size, a different quality) shows up here without anyone remembering
# to mirror it.
BENCH_SCRIPT = PHOTO_GENERATOR_SRC + """
import io, json, statistics, sys, time
from jubilo_church.church.tasks import _encoded, _fit
from jubilo_church.church.utils import PICTURE_MAX_DIMENSION_PX, PICTURE_THUMBNAIL_MAX_DIMENSION_PX, PICTURE_VIEWING_MAX_DIMENSION_PX

data = make_photo()
rounds = __ROUNDS__
timings = []
sizes = None
for _ in range(rounds):
	started = time.perf_counter()
	image = Image.open(io.BytesIO(data))
	fmt = image.format
	image = ImageOps.exif_transpose(image)
	if fmt == 'JPEG' and image.mode != 'RGB':
		image = image.convert('RGB')
	full = _fit(image, PICTURE_MAX_DIMENSION_PX)
	processed = _encoded(full, fmt)
	viewing = _encoded(_fit(full, PICTURE_VIEWING_MAX_DIMENSION_PX), fmt)
	thumbnail = _encoded(_fit(full, PICTURE_THUMBNAIL_MAX_DIMENSION_PX), fmt)
	timings.append(time.perf_counter() - started)
	sizes = {'source': len(data), 'processed': len(processed), 'viewing': len(viewing), 'thumbnail': len(thumbnail)}
sys.stdout.write('LOADBENCH ' + json.dumps({
	'rounds': rounds, 'median_seconds': statistics.median(timings), 'min_seconds': min(timings), 'max_seconds': max(timings),
	'sizes': sizes,
}) + '\\n')
"""


def _parse_args(args):
	"""KEY=VALUE arguments after the action, typed like DEFAULTS; unknown keys are an error."""
	values = dict(DEFAULTS)
	for extra in args:
		if "=" not in extra:
			raise LoadFailure(f"expected KEY=VALUE, got {extra!r}")
		key, _, raw = extra.partition("=")
		key = key.upper()
		if key not in DEFAULTS:
			raise LoadFailure(f"unknown option {key} -- known: {', '.join(DEFAULTS)}")
		try:
			values[key] = int(raw)
		except ValueError:
			raise LoadFailure(f"{key} must be an integer, got {raw!r}")
	if values["COUNT"] < 1 or values["CONCURRENCY"] < 1 or values["POLL"] < 1:
		raise LoadFailure("COUNT, CONCURRENCY and POLL must be at least 1")
	return values


def _percentile(samples, fraction):
	ordered = sorted(samples)
	return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def photo_stats(accepted, ready, ingest_started, ingest_finished):
	"""
	The numbers from the two timelines: `accepted` maps picture id -> (time
	the 201 came back, POST latency); `ready` maps picture id -> time the
	poll first saw processing_status 'ready'. Times are time.monotonic()
	readings. Pure, so the arithmetic has a test without a stack.
	"""
	count = len(accepted)
	latencies = [latency for _, latency in accepted.values()]
	first_accept = min(t for t, _ in accepted.values())
	ingest_wall = ingest_finished - ingest_started
	stats = {
		"count": count,
		"ingest": {
			"wall_seconds": ingest_wall,
			"uploads_per_second": count / ingest_wall if ingest_wall > 0 else None,
			"post_latency_seconds": {
				"median": statistics.median(latencies),
				"p95": _percentile(latencies, 0.95),
				"max": max(latencies),
			},
			# How many the worker still owed when the last POST returned:
			# the queue a burst forms, which is what members see as lag.
			"backlog_when_ingest_finished": sum(1 for pid in accepted if ready.get(pid, float("inf")) > ingest_finished),
		},
		"drain": None,
	}
	if ready and len(ready) == count:
		last_ready = max(ready.values())
		# From the first photo the worker could have started on to the
		# last one finished: one serial worker's throughput, R2 included.
		drain_wall = last_ready - first_accept
		stats["drain"] = {
			"wall_seconds": drain_wall,
			"photos_per_second": count / drain_wall if drain_wall > 0 else None,
			"seconds_per_photo": drain_wall / count,
			"lag_last_photo_seconds": last_ready - ingest_finished,
		}
	return stats


def _worker_python(script, label):
	"""Runs `script` under manage.py shell in the running worker container and returns the line tagged `label`."""
	result = subprocess.run(
		["docker", "compose", "exec", "-T", WORKER_SERVICE, "python", "manage.py", "shell"],
		input=script, capture_output=True, text=True, cwd=INFRA_ROOT,
	)
	if result.returncode != 0:
		raise LoadFailure(f"{WORKER_SERVICE} script failed ({label}):\n{result.stderr[-2000:]}")
	for line in result.stdout.splitlines():
		if line.startswith(label + " "):
			return line[len(label) + 1:]
	raise LoadFailure(f"{WORKER_SERVICE} script printed no {label} line:\n{result.stdout[-2000:]}")


def _photo_fixture():
	if PHOTO_FIXTURE.exists():
		return PHOTO_FIXTURE.read_bytes()
	print(f"Generating the test photo in {WORKER_SERVICE} (once; cached at {PHOTO_FIXTURE})...")
	data = base64.b64decode(_worker_python(FIXTURE_SCRIPT, "LOADPHOTO"))
	PHOTO_FIXTURE.write_bytes(data)
	return data


def _cpu_bench(rounds):
	print(f"CPU bench: the worker's Pillow work on this photo, {rounds} rounds, no storage...")
	return json.loads(_worker_python(BENCH_SCRIPT.replace("__ROUNDS__", str(rounds)), "LOADBENCH"))


def _upload(church_api, headers, verify, event_id, data, index):
	started = time.monotonic()
	response = requests.post(
		f"{church_api}/picture",
		data={"event": event_id, "caption": f"load photos {index}"},
		files={"source_image": (f"load-{index}.jpg", data, "image/jpeg")},
		headers=headers, verify=verify, timeout=UPLOAD_TIMEOUT_SECONDS,
	)
	latency = time.monotonic() - started
	if response.status_code != 201:
		return None, latency, f"{response.status_code}: {response.text[:200]}"
	return response.json()["id"], latency, None


def _poll_until_drained(church_api, headers, verify, pending, poll_seconds, timeout_seconds):
	"""Polls each still-pending picture; returns {id: monotonic time first seen ready} and the ids that failed."""
	ready, failed = {}, {}
	deadline = time.monotonic() + timeout_seconds
	last_report = 0
	while pending and time.monotonic() < deadline:
		for picture_id in list(pending):
			response = requests.get(f"{church_api}/picture/{picture_id}", headers=headers, verify=verify, timeout=HTTP_TIMEOUT_SECONDS)
			response.raise_for_status()
			status = response.json()["processing_status"]
			if status == "ready":
				ready[picture_id] = time.monotonic()
				pending.remove(picture_id)
			elif status == "failed":
				failed[picture_id] = response.json().get("failed_message")
				pending.remove(picture_id)
		if pending and time.monotonic() - last_report >= 10:
			print(f"  {len(ready)} ready, {len(pending)} pending...")
			last_report = time.monotonic()
		if pending:
			time.sleep(poll_seconds)
	return ready, failed


def _delete_all(church_api, headers, verify, picture_ids, concurrency):
	def delete(picture_id):
		response = requests.delete(f"{church_api}/picture/{picture_id}", headers=headers, verify=verify, timeout=HTTP_TIMEOUT_SECONDS)
		return response.status_code == 204
	with ThreadPoolExecutor(max_workers=concurrency) as pool:
		return sum(1 for ok in pool.map(delete, picture_ids) if ok)


def load_photos(service_name_list=None):
	target = _target()
	if target != "dev":
		raise LoadFailure("load photos creates pictures and R2 objects -- dev only, by decision (production data stays untouched)")
	options = _parse_args(service_name_list or [])
	count, concurrency = options["COUNT"], options["CONCURRENCY"]

	base_url = _base_url(target)
	verify = _verify(target)
	church_api = f"{base_url}/api/church"
	data = _photo_fixture()
	print(f"Test photo: {len(data) / (1024 * 1024):.2f} MB")

	print("Acquiring the dev superuser's token...")
	payload, _ = _mint_dev(base_url, verify, {"email": SUPERUSER_EMAIL, "password": SUPERUSER_PASSWORD})
	headers = {"Authorization": f"Bearer {payload['access_token']}"}

	stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
	created_event = options["EVENT"] is None
	if created_event:
		response = requests.post(
			f"{church_api}/event",
			json={
				"category": CONVENTION_CATEGORY_ID, "title": f"Load photos {stamp}",
				"event_start_date": time.strftime("%Y-%m-%d"), "event_end_date": time.strftime("%Y-%m-%d"),
			},
			headers=headers, verify=verify, timeout=HTTP_TIMEOUT_SECONDS,
		)
		if response.status_code != 201:
			raise LoadFailure(f"creating the throwaway event -> {response.status_code}: {response.text[:300]}")
		event_id = response.json()["id"]
		print(f"Throwaway kingdom-tier event {event_id} created.")
	else:
		event_id = options["EVENT"]

	print(f"Ingest: {count} uploads, {concurrency} at a time...")
	accepted, rejected = {}, []
	ingest_started = time.monotonic()
	with ThreadPoolExecutor(max_workers=concurrency) as pool:
		for picture_id, latency, error in pool.map(lambda i: _upload(church_api, headers, verify, event_id, data, i), range(count)):
			if error:
				rejected.append(error)
			else:
				accepted[picture_id] = (time.monotonic(), latency)
	ingest_finished = time.monotonic()
	print(f"  {len(accepted)} accepted, {len(rejected)} rejected in {ingest_finished - ingest_started:.1f}s")
	for error in rejected[:5]:
		print(f"  rejected: {error}")
	if not accepted:
		raise LoadFailure("no upload was accepted -- nothing to drain")

	timeout = DRAIN_TIMEOUT_FLOOR_SECONDS + DRAIN_TIMEOUT_PER_PHOTO_SECONDS * len(accepted)
	print(f"Drain: polling every {options['POLL']}s until the worker has processed all {len(accepted)} (up to {timeout}s)...")
	ready, failed = _poll_until_drained(church_api, headers, verify, set(accepted), options["POLL"], timeout)
	stats = photo_stats(accepted, ready, ingest_started, ingest_finished)
	stats["rejected"] = len(rejected)
	stats["failed"] = failed
	stats["still_pending"] = len(accepted) - len(ready) - len(failed)
	stats["concurrency"] = concurrency
	stats["photo_bytes"] = len(data)
	stats["event"] = event_id
	stats["bench"] = _cpu_bench(options["BENCH"]) if options["BENCH"] > 0 else None

	RESULTS_DIR.mkdir(exist_ok=True)
	summary = RESULTS_DIR / f"{stamp}-{target}-photos.json"
	summary.write_text(json.dumps(stats, indent=2))

	ingest = stats["ingest"]
	print()
	print(f"Ingest   {count} x {len(data) / (1024 * 1024):.2f} MB, {concurrency} concurrent: "
		f"{ingest['uploads_per_second']:.2f} uploads/s, POST median {ingest['post_latency_seconds']['median']:.2f}s "
		f"p95 {ingest['post_latency_seconds']['p95']:.2f}s; backlog when ingest finished: {ingest['backlog_when_ingest_finished']}")
	if stats["drain"]:
		drain = stats["drain"]
		print(f"Worker   {drain['photos_per_second']:.2f} photos/s = {drain['seconds_per_photo']:.2f} s/photo wall clock "
			f"(R2 included); the last photo was ready {drain['lag_last_photo_seconds']:.0f}s after the last upload")
	else:
		print(f"Worker   did not drain: {stats['still_pending']} still pending, {len(failed)} failed after {timeout}s")
		for picture_id, message in list(failed.items())[:5]:
			print(f"  picture {picture_id} failed: {message}")
	if stats["bench"]:
		bench = stats["bench"]
		sizes = bench["sizes"]
		print(f"CPU      {bench['median_seconds']:.2f} s/photo median of {bench['rounds']} (min {bench['min_seconds']:.2f}, max {bench['max_seconds']:.2f}), "
			f"no storage; outputs {sizes['processed'] // 1024} KB / {sizes['viewing'] // 1024} KB / {sizes['thumbnail'] // 1024} KB")
		if stats["drain"]:
			print(f"R2 leg   ~{stats['drain']['seconds_per_photo'] - bench['median_seconds']:.2f} s/photo (wall clock minus CPU; the home uplink in dev)")
	print(f"Summary written to {summary}")

	if options["KEEP"]:
		print(f"KEEP=1: leaving {len(accepted)} picture(s) on event {event_id}.")
		return
	print("Cleaning up...")
	deleted = _delete_all(church_api, headers, verify, list(accepted), concurrency)
	print(f"  {deleted}/{len(accepted)} picture(s) deleted")
	if created_event:
		response = requests.delete(f"{church_api}/event/{event_id}", headers=headers, verify=verify, timeout=HTTP_TIMEOUT_SECONDS)
		print(f"  event {event_id} {'deleted' if response.status_code == 204 else f'NOT deleted ({response.status_code})'}")
	if stats["still_pending"] or failed:
		raise LoadFailure("the worker did not process every photo -- see above")
