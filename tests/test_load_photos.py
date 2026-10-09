"""
Unit tests for the stack-free parts of `jubilo-cli load photos`: the
option parsing and the arithmetic that turns the two timelines (uploads
accepted, pictures ready) into the reported numbers. The rest of that
command needs the running dev stack and real R2, which is what it is for.

Run from the repo root, with the venv active:

	python -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.load import LoadFailure  # noqa: E402
from services.load_photos import DEFAULTS, _parse_args, photo_stats  # noqa: E402


class ParseArgsTests(unittest.TestCase):
	def test_defaults_when_no_arguments(self):
		self.assertEqual(_parse_args([]), DEFAULTS)

	def test_overrides_are_typed_and_case_insensitive(self):
		values = _parse_args(["count=200", "CONCURRENCY=16", "event=17", "keep=1", "bench=0"])
		self.assertEqual(values["COUNT"], 200)
		self.assertEqual(values["CONCURRENCY"], 16)
		self.assertEqual(values["EVENT"], 17)
		self.assertEqual(values["KEEP"], 1)
		self.assertEqual(values["BENCH"], 0)

	def test_rejects_unknown_key_bad_shape_and_non_integer(self):
		with self.assertRaises(LoadFailure):
			_parse_args(["PEAK=300"])
		with self.assertRaises(LoadFailure):
			_parse_args(["COUNT"])
		with self.assertRaises(LoadFailure):
			_parse_args(["COUNT=many"])

	def test_rejects_zero_count_or_concurrency(self):
		with self.assertRaises(LoadFailure):
			_parse_args(["COUNT=0"])
		with self.assertRaises(LoadFailure):
			_parse_args(["CONCURRENCY=0"])


class PhotoStatsTests(unittest.TestCase):
	# Four uploads over 4 s (one accepted each second, 0.5 s POST latency);
	# a serial worker at 2 s a photo finishes them at 2, 4, 6, 8 s.
	ACCEPTED = {1: (101.0, 0.5), 2: (102.0, 0.5), 3: (103.0, 0.5), 4: (104.0, 0.5)}
	READY = {1: 103.0, 2: 105.0, 3: 107.0, 4: 109.0}

	def test_ingest_rate_latency_and_backlog(self):
		stats = photo_stats(self.ACCEPTED, self.READY, ingest_started=100.0, ingest_finished=104.0)
		self.assertEqual(stats["count"], 4)
		self.assertEqual(stats["ingest"]["wall_seconds"], 4.0)
		self.assertEqual(stats["ingest"]["uploads_per_second"], 1.0)
		self.assertEqual(stats["ingest"]["post_latency_seconds"]["median"], 0.5)
		self.assertEqual(stats["ingest"]["post_latency_seconds"]["p95"], 0.5)
		# At 104.0 only picture 1 (ready at 103.0) was done; three were owed.
		self.assertEqual(stats["ingest"]["backlog_when_ingest_finished"], 3)

	def test_drain_is_first_accept_to_last_ready(self):
		stats = photo_stats(self.ACCEPTED, self.READY, ingest_started=100.0, ingest_finished=104.0)
		drain = stats["drain"]
		self.assertEqual(drain["wall_seconds"], 8.0)
		self.assertEqual(drain["photos_per_second"], 0.5)
		self.assertEqual(drain["seconds_per_photo"], 2.0)
		self.assertEqual(drain["lag_last_photo_seconds"], 5.0)

	def test_no_drain_numbers_when_not_every_photo_got_ready(self):
		partial = {1: 103.0, 2: 105.0}
		stats = photo_stats(self.ACCEPTED, partial, ingest_started=100.0, ingest_finished=104.0)
		self.assertIsNone(stats["drain"])
		# Unfinished photos count as backlog, not as done.
		self.assertEqual(stats["ingest"]["backlog_when_ingest_finished"], 3)

	def test_p95_picks_the_slow_tail(self):
		accepted = {i: (100.0 + i, 0.1 * i) for i in range(1, 21)}
		ready = {i: 200.0 + i for i in accepted}
		stats = photo_stats(accepted, ready, ingest_started=100.0, ingest_finished=121.0)
		self.assertAlmostEqual(stats["ingest"]["post_latency_seconds"]["p95"], 1.9)
		self.assertAlmostEqual(stats["ingest"]["post_latency_seconds"]["max"], 2.0)


if __name__ == "__main__":
	unittest.main()
