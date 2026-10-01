import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import DomainError, RadioDB


class BackhaulFlowTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.db = RadioDB(self.path)
        self.db.add_station("huadong", "华东发射台")
        self.p1 = self.db.add_program("早间新闻", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        self.p2 = self.db.add_program("品牌广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠", 0, ["华东"])
        self.p3 = self.db.add_program("华北节目", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华北"])
        self.s1 = self.db.schedule_slot("2026-09-28", "09:00", self.p1, "华东")
        self.s2 = self.db.schedule_slot("2026-09-28", "10:00", self.p2, "华东")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _seg(self, slot, program, air_date="2026-09-28", start_time=None, duration=None):
        slot_row = self.db.get_slot(slot)
        return {
            "air_date": air_date,
            "start_time": start_time or slot_row["start_time"],
            "actual_start": start_time or slot_row["start_time"],
            "actual_duration_minutes": duration or slot_row["duration_minutes"],
            "actual_program_id": program,
        }

    def test_receive_accepted_and_retry_does_not_duplicate(self):
        pkg = self.db.receive_backhaul("huadong", "PKG-001", [self._seg(self.s1, self.p1)])
        self.assertEqual("accepted", pkg["package"]["status"])
        self.assertEqual(1, pkg["package"]["accepted_segments"])
        seg = pkg["segments"][0]
        self.assertEqual("accepted", seg["status"])
        self.assertIsNotNone(seg["playout_log_id"])
        self.assertIsNotNone(seg["snapshot_id"])
        # 已入账的实播不重复新增
        before = self.db.conn.execute("SELECT COUNT(*) FROM playout_logs").fetchone()[0]
        pkg2 = self.db.receive_backhaul("huadong", "PKG-001", [self._seg(self.s1, self.p1)])
        after = self.db.conn.execute("SELECT COUNT(*) FROM playout_logs").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual("accepted", pkg2["segments"][0]["status"])

    def test_missing_segment_keeps_package_pending_and_retry_continues(self):
        # 排期表里没有 11:00 的排期 -> 缺失段，整包待核
        pkg = self.db.receive_backhaul("huadong", "PKG-002", [
            self._seg(self.s1, self.p1),
            {"air_date": "2026-09-28", "start_time": "11:00", "actual_start": "11:00",
             "actual_duration_minutes": 30, "actual_program_id": self.p1},
        ])
        self.assertEqual("pending", pkg["package"]["status"])
        self.assertEqual(1, pkg["package"]["accepted_segments"])
        self.assertEqual(1, pkg["package"]["missing_segments"])
        missing = [s for s in pkg["segments"] if s["status"] == "missing"][0]
        self.assertIn("找不到", missing["fail_reason"])
        # 重试只续缺失段：已入账的段跳过，缺失段仍待处理
        pkg2 = self.db.receive_backhaul("huadong", "PKG-002", [
            self._seg(self.s1, self.p1),
            {"air_date": "2026-09-28", "start_time": "11:00", "actual_start": "11:00",
             "actual_duration_minutes": 30, "actual_program_id": self.p1},
        ])
        self.assertEqual("pending", pkg2["package"]["status"])
        self.assertEqual(1, pkg2["package"]["missing_segments"])

    def test_schedule_change_voids_and_recalculates_pending(self):
        pkg = self.db.receive_backhaul("huadong", "PKG-003", [
            {"air_date": "2026-09-28", "start_time": "11:00", "actual_start": "11:00",
             "actual_duration_minutes": 30, "actual_program_id": self.p1},
        ])
        self.assertEqual("pending", pkg["package"]["status"])
        # 编排改动：新增排期 -> 待核数据立即作废并按当前节目单重算
        self.db.schedule_slot("2026-09-28", "11:00", self.p1, "华东")
        after = self.db.get_backhaul_package(pkg["package"]["id"])
        self.assertEqual("accepted", after["package"]["status"])
        self.assertEqual(1, after["package"]["accepted_segments"])
        events = [(e["event_type"], e["detail"]) for e in after["events"]]
        self.assertTrue(any(t == "voided" for t, _ in events))
        self.assertTrue(any(t == "recalculated" for t, _ in events))

    def test_rights_violation_pending_snapshot_and_reconcile_agree(self):
        # p3 只授权华北，在华东播出 -> 越权
        pkg = self.db.receive_backhaul("huadong", "PKG-004", [self._seg(self.s2, self.p3)])
        self.assertEqual("pending", pkg["package"]["status"])
        seg = pkg["segments"][0]
        self.assertEqual("pending", seg["status"])
        self.assertIsNotNone(seg["snapshot_id"])
        snap = self.db.conn.execute(
            "SELECT * FROM authorization_snapshots WHERE id=?", (seg["snapshot_id"],)
        ).fetchone()
        self.assertEqual(0, snap["authorized"])
        self.assertEqual(1, snap["within_window"])
        excs = self.db.reconcile_date("2026-09-28")
        kinds = {(e["slot_id"], e["kind"]) for e in excs}
        self.assertIn((self.s2, "out_of_license"), kinds)
        # 回传包、授权快照、对账列表同一结论：待核 / 未授权 / 越权
        self.assertEqual("pending", seg["status"])
        self.assertEqual(0, snap["authorized"])
        self.assertTrue(any(e["slot_id"] == self.s2 and e["kind"] == "out_of_license" for e in excs))

    def test_frozen_snapshot_not_retroactive_after_revoke(self):
        # 播出时 p1 在华东已授权 -> 已入账
        pkg = self.db.receive_backhaul("huadong", "PKG-005", [self._seg(self.s1, self.p1)])
        self.assertEqual("accepted", pkg["package"]["status"])
        # 播出后撤销华东授权（后来窗口变化）
        self.db.conn.execute("DELETE FROM program_regions WHERE program_id=? AND region='华东'", (self.p1,))
        self.db.conn.commit()
        self.db.recalculate_pending_backhaul("人工：撤销授权")
        after = self.db.get_backhaul_package(pkg["package"]["id"])
        # 已播段仍按播出时刻快照判，不被后来窗口追溯
        self.assertEqual("accepted", after["package"]["status"])
        excs = self.db.reconcile_date("2026-09-28")
        self.assertFalse(any(e["slot_id"] == self.s1 and e["kind"] == "out_of_license" for e in excs))

    def test_accepted_segment_never_missed_in_reconcile(self):
        pkg = self.db.receive_backhaul("huadong", "PKG-006", [self._seg(self.s1, self.p1)])
        self.assertEqual("accepted", pkg["package"]["status"])
        excs = self.db.reconcile_date("2026-09-28")
        # 已入账的实播不能被当成漏播
        self.assertFalse(any(e["slot_id"] == self.s1 and e["kind"] == "missed" for e in excs))

    def test_same_package_no_independent_per_station(self):
        self.db.add_station("huabei", "华北发射台")
        p1_north = self.db.add_program("早间新闻华北版", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华北"])
        self.db.schedule_slot("2026-09-28", "09:00", p1_north, "华北")
        pkg_a = self.db.receive_backhaul("huadong", "SAME-001", [
            {**self._seg(self.s1, self.p1), "region": "华东"},
        ])
        pkg_b = self.db.receive_backhaul("huabei", "SAME-001", [
            {"air_date": "2026-09-28", "start_time": "09:00", "region": "华北", "actual_start": "09:00",
             "actual_duration_minutes": 30, "actual_program_id": p1_north},
        ])
        self.assertEqual("accepted", pkg_a["package"]["status"])
        self.assertEqual("accepted", pkg_b["package"]["status"])
        self.assertNotEqual(pkg_a["package"]["id"], pkg_b["package"]["id"])

    def test_unknown_station_rejected(self):
        with self.assertRaisesRegex(DomainError, "发射台不存在"):
            self.db.receive_backhaul("nonexistent", "PKG-X", [self._seg(self.s1, self.p1)])

    def test_empty_segments_rejected(self):
        with self.assertRaisesRegex(DomainError, "回传批次不能为空"):
            self.db.receive_backhaul("huadong", "PKG-Y", [])

    def test_retry_posts_previously_missing_segment(self):
        # 排期表里没有 14:00 的排期 -> 缺失段
        pkg = self.db.receive_backhaul("huadong", "PKG-008", [
            {"air_date": "2026-09-28", "start_time": "14:00", "actual_start": "14:00",
             "actual_duration_minutes": 30, "actual_program_id": self.p1},
        ])
        self.assertEqual("missing", pkg["segments"][0]["status"])
        # 编排改动补齐排期（自动重算），值班员再重试同一包号
        self.db.schedule_slot("2026-09-28", "14:00", self.p1, "华东")
        retried = self.db.receive_backhaul("huadong", "PKG-008", [
            {"air_date": "2026-09-28", "start_time": "14:00", "actual_start": "14:00",
             "actual_duration_minutes": 30, "actual_program_id": self.p1},
        ])
        self.assertEqual("accepted", retried["package"]["status"])
        self.assertEqual("accepted", retried["segments"][0]["status"])
        self.assertIsNotNone(retried["segments"][0]["playout_log_id"])

    def test_malformed_segment_values_handled(self):
        # 畸形时长 -> 缺失段，不抛异常
        pkg = self.db.receive_backhaul("huadong", "PKG-009", [
            {"air_date": "2026-09-28", "start_time": "09:00", "actual_start": "09:00",
             "actual_duration_minutes": "abc", "actual_program_id": self.p1},
        ])
        self.assertEqual("missing", pkg["segments"][0]["status"])
        # 畸形节目 id -> 待核段，不抛异常
        pkg2 = self.db.receive_backhaul("huadong", "PKG-010", [
            {"air_date": "2026-09-28", "start_time": "09:00", "actual_start": "09:00",
             "actual_duration_minutes": 30, "actual_program_id": "xyz"},
        ])
        self.assertEqual("pending", pkg2["segments"][0]["status"])

    def test_authorization_change_voids_pending(self):
        # p3 只授权华北，回传在华东播出 -> 越权待核
        pkg = self.db.receive_backhaul("huadong", "PKG-007", [self._seg(self.s2, self.p3)])
        self.assertEqual("pending", pkg["package"]["status"])
        # 授权改动：追加华东地区授权 -> 待核数据作废并重算
        # 但已播段按播出时刻快照判越权，越权结论不变（快照冻结）
        self.db.authorize_region(self.p3, "华东")
        after = self.db.get_backhaul_package(pkg["package"]["id"])
        # 该段已入账（有 playout_log），重算不重复新增，越权快照不变
        self.assertEqual(1, after["package"]["accepted_segments"] + after["package"]["pending_segments"])
        self.assertEqual("pending", after["segments"][0]["status"])
        events = [e["event_type"] for e in after["events"]]
        self.assertIn("voided", events)


if __name__ == "__main__":
    unittest.main()
