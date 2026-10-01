import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import DomainError, RadioDB, segment_hash


STATION = "county-xh-01"
AIR_DATE = "2026-09-28"
REGION = "华东"


def make_segments(slot_ids, program_ids=None, duration=None, played_at="2026-09-28T09:00:00"):
    program_ids = program_ids or [None] * len(slot_ids)
    segments = []
    for i, (slot_id, pid) in enumerate(zip(slot_ids, program_ids)):
        seg = {
            "seq": i,
            "slot_id": slot_id,
            "actual_start": f"{9 + i:02d}:00",
            "actual_duration_minutes": duration if duration is not None else 30,
            "actual_program_id": pid,
            "note": "断网期间按缓存单播出",
            "played_at": played_at,
        }
        seg["hash"] = segment_hash(seg)
        segments.append(seg)
    return segments


def manifest(segments, **extra):
    return {"segment_count": extra.get("segment_count", len(segments)),
            "hashes": {str(s["seq"]): s["hash"] for s in segments}}


def auth_entries(program_ids, region=REGION, start="2026-01-01", end="2026-12-31"):
    return {"captured_at": "2026-09-28T08:59:00",
            "entries": [{"program_id": pid, "region": region,
                         "start_date": start, "end_date": end} for pid in program_ids]}


class OfflineBackhaulTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.p1 = self.db.add_program("早间新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, [REGION])
        self.p2 = self.db.add_program("品牌广告", "ad", 30, "2026-01-01", "2026-12-31", "青柠", 0, [REGION])
        self.s1 = self.db.schedule_slot(AIR_DATE, "09:00", self.p1, REGION)
        self.s2 = self.db.schedule_slot(AIR_DATE, "10:00", self.p2, REGION)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def send(self, segments, package="PKG-1", count=None, snapshot_program_ids=None, **kw):
        snap = auth_entries(snapshot_program_ids) if snapshot_program_ids is not None else None
        return self.db.receive_batch(STATION, package, segments,
                                    segment_count=count if count is not None else len(segments),
                                    manifest=manifest(segments, segment_count=count or len(segments)),
                                    auth_snapshot=snap, **kw)

    def test_partial_delivery_then_retry_only_missing(self):
        full = make_segments([self.s1, self.s2], [self.p1, self.p2])
        first = self.send([full[0]], count=2, snapshot_program_ids=[self.p1, self.p2])
        self.assertEqual("receiving", first["status"])
        self.assertEqual([1], first["missing_segments"])
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) FROM playout_logs").fetchone()[0])

        # Retry continues with just the missing segment.
        second = self.send([full[1]], count=2, snapshot_program_ids=[self.p1, self.p2])
        self.assertEqual("posted", second["status"])
        self.assertEqual([], second["missing_segments"])
        self.assertEqual(2, self.db.conn.execute("SELECT COUNT(*) FROM playout_logs").fetchone()[0])

    def test_posted_package_is_idempotent_and_never_double_books(self):
        full = make_segments([self.s1, self.s2], [self.p1, self.p2])
        self.send(full, snapshot_program_ids=[self.p1, self.p2])
        again = self.send(full, snapshot_program_ids=[self.p1, self.p2])
        self.assertEqual("posted", again["status"])
        self.assertEqual(2, self.db.conn.execute("SELECT COUNT(*) FROM playout_logs").fetchone()[0])

        # Re-posting a corrected duplicate of an already verified segment must not mutate it.
        tampered = [dict(full[0], note="被篡改的重发")]
        self.send(tampered, count=2, snapshot_program_ids=[self.p1, self.p2])
        note = self.db.conn.execute(
            "SELECT note FROM playout_logs WHERE source_batch_id IN "
            "(SELECT id FROM ingest_batches WHERE package_no='PKG-1') AND source_seq=0"
        ).fetchone()[0]
        self.assertEqual("断网期间按缓存单播出", note)

    def test_hash_failure_keeps_whole_package_pending_review(self):
        full = make_segments([self.s1, self.s2], [self.p1, self.p2])
        m = manifest(full)
        m["hashes"]["1"] = "deadbeef"
        result = self.db.receive_batch(
            STATION, "PKG-2", full, segment_count=2, manifest=m,
            auth_snapshot=auth_entries([self.p1, self.p2]),
        )
        self.assertEqual("pending_review", result["status"])
        self.assertIn("1", result["note"])
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) FROM playout_logs").fetchone()[0])

        # Retry with the corrected segment posts the package; the good segment is untouched.
        fixed = [full[1]]
        retried = self.db.receive_batch(
            STATION, "PKG-2", fixed, segment_count=2, manifest=manifest(fixed),
            auth_snapshot=auth_entries([self.p1, self.p2]),
        )
        self.assertEqual("posted", retried["status"])
        self.assertEqual(2, self.db.conn.execute("SELECT COUNT(*) FROM playout_logs").fetchone()[0])

    def test_schedule_change_voids_pending_and_recomputes_but_keeps_history_pinned(self):
        # Package sits incomplete (断网中，只回来了第一段)。
        full = make_segments([self.s1, self.s2], [self.p1, self.p2])
        pending = self.send([full[0]], package="PKG-3", count=2,
                            snapshot_program_ids=[self.p1, self.p2])
        self.assertEqual("receiving", pending["status"])

        # A fully posted package already exists for the morning slot.
        earlier = make_segments([self.s1], [self.p1])
        self.send(earlier, package="PKG-0", snapshot_program_ids=[self.p1])

        # 编排改动：09:00 换成广告（新节目单）。
        self.db.replace_slot(self.s1, self.p2)
        batch = self.db.get_batch(STATION, "PKG-3")
        self.assertEqual("voided", batch["status"])
        self.assertIn("作废", batch["note"])

        # Resending under the same package number starts fresh and now shows
        # wrong_program against the *current* plan (slot plans p2, playout p1).
        resent = self.send(full, package="PKG-3", snapshot_program_ids=[self.p1, self.p2])
        self.assertEqual("posted", resent["status"])
        seg0 = next(s for s in resent["segments"] if s["seq"] == 0)
        self.assertFalse(seg0["program_matches"])
        self.assertIn("wrong_program", seg0["exception_kinds"])

        # The historical PKG-0 playout: p1 WAS licensed at playout time and that
        # verdict is pinned — the reschedule does not retroactively strip it.
        # wrong_program against the *current* plan (now p2) is still expected.
        historical = self.db.get_batch(STATION, "PKG-0")
        hist_seg = historical["segments"][0]
        self.assertFalse(hist_seg["program_matches"])
        self.assertIn("wrong_program", hist_seg["exception_kinds"])
        self.assertTrue(hist_seg["authorized"])
        self.assertNotIn("out_of_license", hist_seg["exception_kinds"])

    def test_later_window_change_never_retroactively_out_of_license(self):
        segs = make_segments([self.s1], [self.p1])
        self.send(segs, package="PKG-4", snapshot_program_ids=[self.p1])

        # 回网后授权窗口收紧：p1 不再授权华东。
        self.db.deauthorize_region(self.p1, REGION)

        # All three views still agree: it was authorized at playout time.
        batch = self.db.get_batch(STATION, "PKG-4")
        self.assertTrue(batch["segments"][0]["authorized"])
        self.assertNotIn("out_of_license", batch["exception_kinds"])

        license_view = self.db.playout_license_view(AIR_DATE)
        row = next(r for r in license_view if r["source_batch_id"] == batch["id"])
        self.assertTrue(row["authorized"])
        self.assertEqual(batch["segments"][0]["auth_snapshot_id"], row["auth_snapshot_id"])

        exceptions = self.db.reconcile_date(AIR_DATE)
        self.assertNotIn("out_of_license", {e["kind"] for e in exceptions})

    def test_out_of_license_uses_carried_playout_snapshot_consistently(self):
        # Station's cached snapshot predates a later grant: p2 not in 华东 yet.
        segs = make_segments([self.s2], [self.p2])
        posted = self.send(segs, package="PKG-5", snapshot_program_ids=[self.p1])
        self.assertEqual("posted", posted["status"])
        seg = posted["segments"][0]
        self.assertFalse(seg["authorized"])
        self.assertIn("out_of_license", seg["exception_kinds"])

        # Same verdict in the license view and the reconciliation list.
        license_row = self.db.playout_license_view(AIR_DATE)[0]
        self.assertFalse(license_row["authorized"])
        self.assertEqual(seg["auth_snapshot_id"], license_row["auth_snapshot_id"])
        exceptions = {(e["slot_id"], e["kind"]) for e in self.db.reconcile_date(AIR_DATE)}
        self.assertIn((self.s2, "out_of_license"), exceptions)

    def test_replay_is_not_flagged_as_missed_or_double_booked(self):
        segs = make_segments([self.s1], [self.p1])
        self.send(segs, package="PKG-6", snapshot_program_ids=[self.p1])
        self.send(segs, package="PKG-6", snapshot_program_ids=[self.p1])
        exceptions = self.db.reconcile_date(AIR_DATE)
        # s1 played (replay must not look like a missed slot); s2 genuinely has no playout.
        self.assertNotIn((self.s1, "missed"), {(e["slot_id"], e["kind"]) for e in exceptions})
        self.assertEqual(1, self.db.conn.execute(
            "SELECT COUNT(*) FROM playout_logs WHERE slot_id=? AND source_batch_id IS NOT NULL",
            (self.s1,)).fetchone()[0])

    def test_declared_segment_count_cannot_change_midway(self):
        full = make_segments([self.s1, self.s2], [self.p1, self.p2])
        self.send([full[0]], package="PKG-7", count=2, snapshot_program_ids=[self.p1, self.p2])
        with self.assertRaisesRegex(DomainError, "总段数"):
            self.send([full[1]], package="PKG-7", count=3, snapshot_program_ids=[self.p1, self.p2])

    def test_unknown_slot_is_a_malformed_delivery(self):
        bad = make_segments([9999])
        with self.assertRaisesRegex(DomainError, "排期"):
            self.send(bad, package="PKG-8")


if __name__ == "__main__":
    unittest.main()
