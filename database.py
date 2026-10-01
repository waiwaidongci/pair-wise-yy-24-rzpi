from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path


class DomainError(ValueError):
    """A business-rule violation that should be shown to the API caller."""


PROGRAM_KINDS = {"music", "ad", "talk", "live"}
WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _overlap(a_start: str, a_duration: int, b_start: str, b_duration: int) -> bool:
    start_a, start_b = _minutes(a_start), _minutes(b_start)
    return start_a < start_b + b_duration and start_b < start_a + a_duration


class RadioDB:
    """SQLite-backed radio scheduling service.

    The service keeps planning and actual playout separate. A replacement is
    accepted only when the complete plan remains valid; reconciliation never
    rewrites the plan, it records discrepancies for operators.
    """

    def __init__(self, path: str = "radio.db") -> None:
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS programs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              kind TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              start_date TEXT NOT NULL,
              end_date TEXT NOT NULL,
              sponsor TEXT,
              cooldown_minutes INTEGER NOT NULL DEFAULT 0 CHECK(cooldown_minutes >= 0),
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
              UNIQUE(title, start_date, end_date)
            );
            CREATE TABLE IF NOT EXISTS program_regions (
              program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
              region TEXT NOT NULL,
              PRIMARY KEY(program_id, region)
            );
            CREATE TABLE IF NOT EXISTS blocked_windows (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              region TEXT NOT NULL,
              weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
              start_time TEXT NOT NULL,
              end_time TEXT NOT NULL,
              reason TEXT NOT NULL,
              CHECK(start_time < end_time)
            );
            CREATE TABLE IF NOT EXISTS sponsor_policies (
              sponsor TEXT PRIMARY KEY,
              min_gap_minutes INTEGER NOT NULL CHECK(min_gap_minutes >= 0)
            );
            CREATE TABLE IF NOT EXISTS slots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              duration_minutes INTEGER NOT NULL CHECK(duration_minutes > 0),
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'planned'
                CHECK(status IN ('planned','replaced','cancelled')),
              replaced_from INTEGER REFERENCES programs(id),
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_slots_date_region ON slots(air_date, region);
            CREATE TABLE IF NOT EXISTS playout_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              actual_start TEXT NOT NULL,
              actual_duration_minutes INTEGER NOT NULL CHECK(actual_duration_minutes >= 0),
              actual_program_id INTEGER REFERENCES programs(id),
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reconciliation_exceptions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              air_date TEXT NOT NULL,
              slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
              kind TEXT NOT NULL,
              detail TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(air_date, slot_id, kind)
            );
            CREATE TABLE IF NOT EXISTS stations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              code TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backhaul_packages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              station_id INTEGER NOT NULL REFERENCES stations(id),
              package_no TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','accepted','void')),
              total_segments INTEGER NOT NULL DEFAULT 0,
              accepted_segments INTEGER NOT NULL DEFAULT 0,
              pending_segments INTEGER NOT NULL DEFAULT 0,
              missing_segments INTEGER NOT NULL DEFAULT 0,
              recalc_round INTEGER NOT NULL DEFAULT 0,
              void_reason TEXT,
              voided_at TEXT,
              received_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(station_id, package_no)
            );
            CREATE TABLE IF NOT EXISTS backhaul_segments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              package_id INTEGER NOT NULL REFERENCES backhaul_packages(id) ON DELETE CASCADE,
              segment_no INTEGER NOT NULL,
              slot_id INTEGER REFERENCES slots(id) ON DELETE SET NULL,
              air_date TEXT NOT NULL,
              start_time TEXT NOT NULL,
              region TEXT NOT NULL,
              actual_start TEXT,
              actual_duration_minutes INTEGER
                CHECK(actual_duration_minutes IS NULL OR actual_duration_minutes >= 0),
              actual_program_id INTEGER REFERENCES programs(id),
              status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('accepted','pending','missing')),
              fail_reason TEXT,
              playout_log_id INTEGER REFERENCES playout_logs(id) ON DELETE SET NULL,
              snapshot_id INTEGER REFERENCES authorization_snapshots(id) ON DELETE SET NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(package_id, segment_no)
            );
            CREATE TABLE IF NOT EXISTS authorization_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              station_id INTEGER REFERENCES stations(id) ON DELETE SET NULL,
              program_id INTEGER NOT NULL REFERENCES programs(id),
              region TEXT NOT NULL,
              air_date TEXT NOT NULL,
              actual_start TEXT NOT NULL,
              authorized INTEGER NOT NULL CHECK(authorized IN (0,1)),
              within_window INTEGER NOT NULL CHECK(within_window IN (0,1)),
              source TEXT NOT NULL DEFAULT 'backhaul',
              detail TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backhaul_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              package_id INTEGER NOT NULL REFERENCES backhaul_packages(id) ON DELETE CASCADE,
              event_type TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        existing = self.conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0]
        if existing:
            return
        music = self.add_program("晨间轻音乐", "music", 30, "2026-01-01", "2026-12-31", "青柠饮品", 45, ["华东"])
        news = self.add_program("城市早报", "talk", 30, "2026-01-01", "2026-12-31", None, 0, ["华东"])
        ad = self.add_program("青柠饮品广告", "ad", 5, "2026-01-01", "2026-12-31", "青柠饮品", 60, ["华东"])
        self.add_sponsor_policy("青柠饮品", 90)
        self.add_blocked_window("华东", 0, "08:00", "08:30", "周一设备检修")
        self.schedule_slot("2026-09-28", "09:00", music, "华东")
        self.schedule_slot("2026-09-28", "10:00", news, "华东")
        self.schedule_slot("2026-09-28", "11:00", ad, "华东")

    def add_program(self, title: str, kind: str, duration_minutes: int, start_date: str, end_date: str,
                    sponsor: str | None = None, cooldown_minutes: int = 0,
                    regions: list[str] | None = None) -> int:
        if not title.strip():
            raise DomainError("节目名称不能为空")
        if kind not in PROGRAM_KINDS:
            raise DomainError(f"不支持的节目类型: {kind}")
        if duration_minutes <= 0:
            raise DomainError("节目时长必须大于0")
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        if end < start:
            raise DomainError("授权结束日期不能早于开始日期")
        if cooldown_minutes < 0:
            raise DomainError("冷却时间不能为负数")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO programs(title,kind,duration_minutes,start_date,end_date,sponsor,cooldown_minutes) VALUES(?,?,?,?,?,?,?)",
                (title.strip(), kind, duration_minutes, start_date, end_date, (sponsor or "").strip() or None, cooldown_minutes),
            )
            program_id = int(cur.lastrowid)
            for region in regions or []:
                self.conn.execute("INSERT INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))
        return program_id

    def authorize_region(self, program_id: int, region: str) -> None:
        if not region.strip():
            raise DomainError("地区不能为空")
        with self.transaction():
            if not self.conn.execute("SELECT 1 FROM programs WHERE id=?", (program_id,)).fetchone():
                raise DomainError("节目不存在")
            self.conn.execute("INSERT OR IGNORE INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region.strip()))
            # 授权改动：待核数据立即作废并按当前授权快照重算
            self._recalculate_pending("授权改动：追加地区授权")

    def add_sponsor_policy(self, sponsor: str, min_gap_minutes: int) -> None:
        if not sponsor.strip() or min_gap_minutes < 0:
            raise DomainError("赞助商和最小间隔必须有效")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO sponsor_policies(sponsor,min_gap_minutes) VALUES(?,?) "
                "ON CONFLICT(sponsor) DO UPDATE SET min_gap_minutes=excluded.min_gap_minutes",
                (sponsor.strip(), min_gap_minutes),
            )

    def add_blocked_window(self, region: str, weekday: int, start_time: str, end_time: str, reason: str) -> int:
        if weekday not in range(7) or _minutes(start_time) >= _minutes(end_time):
            raise DomainError("禁播时段参数无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO blocked_windows(region,weekday,start_time,end_time,reason) VALUES(?,?,?,?,?)",
                (region.strip(), weekday, start_time, end_time, reason.strip() or "禁播"),
            )
        return int(cur.lastrowid)

    def _validate_slot(self, air_date: str, start_time: str, duration: int, program_id: int,
                       region: str, ignore_slot_id: int | None = None) -> None:
        try:
            day = datetime.strptime(air_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        try:
            _minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        if duration <= 0:
            raise DomainError("排期时长必须大于0")
        program = self.conn.execute("SELECT * FROM programs WHERE id=? AND active=1", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在或未启用")
        if program["duration_minutes"] != duration:
            raise DomainError(f"排期时长必须等于节目时长 {program['duration_minutes']} 分钟")
        if not (program["start_date"] <= air_date <= program["end_date"]):
            raise DomainError("播出日期超出授权窗口")
        if not self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program_id, region)
        ).fetchone():
            raise DomainError(f"节目未授权在{region}播出")
        end_minutes = _minutes(start_time) + duration
        blocked = self.conn.execute(
            "SELECT * FROM blocked_windows WHERE region=? AND weekday=?",
            (region, day.weekday()),
        ).fetchall()
        for window in blocked:
            if _minutes(window["start_time"]) < end_minutes and _minutes(start_time) < _minutes(window["end_time"]):
                raise DomainError(f"与禁播时段冲突: {window['reason']}")
        sql = "SELECT * FROM slots WHERE air_date=? AND region=? AND status!='cancelled'"
        params: list[object] = [air_date, region]
        if ignore_slot_id is not None:
            sql += " AND id!=?"
            params.append(ignore_slot_id)
        for existing in self.conn.execute(sql, params).fetchall():
            if _overlap(start_time, duration, existing["start_time"], existing["duration_minutes"]):
                raise DomainError(f"与排期 #{existing['id']} 时间重叠")
        if program["cooldown_minutes"]:
            previous = self.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND region=? AND program_id=? AND status!='cancelled' AND id!=? "
                "AND start_time < ? ORDER BY start_time DESC LIMIT 1",
                (air_date, region, program_id, ignore_slot_id or -1, start_time),
            ).fetchone()
            if previous:
                gap = _minutes(start_time) - (_minutes(previous["start_time"]) + previous["duration_minutes"])
                if gap < program["cooldown_minutes"]:
                    raise DomainError(f"与上一期节目间隔不足冷却时间 {program['cooldown_minutes']} 分钟")
        if program["sponsor"]:
            policy = self.conn.execute("SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?", (program["sponsor"],)).fetchone()
            if policy:
                gap = policy["min_gap_minutes"]
                all_sponsored = self.conn.execute(
                    "SELECT s.*, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id "
                    "WHERE s.air_date=? AND s.region=? AND s.status!='cancelled' AND p.sponsor=? AND s.id!=?",
                    (air_date, region, program["sponsor"], ignore_slot_id or -1),
                ).fetchall()
                for other in all_sponsored:
                    if _overlap(start_time, duration, other["start_time"], other["duration_minutes"]):
                        raise DomainError(f"与赞助商 {program['sponsor']} 的其他节目冲突")
                    distance = abs(_minutes(start_time) - (_minutes(other["start_time"]) + other["duration_minutes"]))
                    if distance < gap:
                        raise DomainError(f"与赞助商 {program['sponsor']} 的节目间隔不足 {gap} 分钟")

    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str) -> int:
        program = self.conn.execute("SELECT duration_minutes FROM programs WHERE id=?", (program_id,)).fetchone()
        if not program:
            raise DomainError("节目不存在")
        with self.transaction():
            self._validate_slot(air_date, start_time, int(program["duration_minutes"]), program_id, region)
            cur = self.conn.execute(
                "INSERT INTO slots(air_date,start_time,duration_minutes,program_id,region,created_at) VALUES(?,?,?,?,?,?)",
                (air_date, start_time, int(program["duration_minutes"]), program_id, region, datetime.now().isoformat()),
            )
            # 编排改动：待核数据立即作废并按当前节目单重算
            self._recalculate_pending("编排改动：新增排期")
        return int(cur.lastrowid)

    def replace_slot(self, slot_id: int, new_program_id: int) -> dict:
        """Replace a planned item and revalidate the resulting plan atomically."""
        with self.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=? AND status='planned'", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("只能替换尚未播出且状态为 planned 的排期")
            program = self.conn.execute("SELECT * FROM programs WHERE id=?", (new_program_id,)).fetchone()
            if not program:
                raise DomainError("替换节目不存在")
            self._validate_slot(slot["air_date"], slot["start_time"], int(program["duration_minutes"]), new_program_id, slot["region"], slot_id)
            self.conn.execute(
                "UPDATE slots SET program_id=?, duration_minutes=?, replaced_from=?, status='replaced' WHERE id=?",
                (new_program_id, int(program["duration_minutes"]), slot["program_id"], slot_id),
            )
            # 编排改动：待核数据立即作废并按当前节目单重算
            self._recalculate_pending("编排改动：替换排期")
        return self.get_slot(slot_id)

    def get_slot(self, slot_id: int) -> dict:
        row = self.conn.execute(
            "SELECT s.*, p.title, p.kind, p.sponsor FROM slots s JOIN programs p ON p.id=s.program_id WHERE s.id=?",
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        return dict(row)

    def record_playout(self, slot_id: int, actual_start: str, actual_duration_minutes: int,
                       actual_program_id: int | None = None, note: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM slots WHERE id=?", (slot_id,)).fetchone():
            raise DomainError("排期不存在")
        if actual_duration_minutes < 0:
            raise DomainError("实际时长不能为负数")
        _minutes(actual_start)
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,created_at) VALUES(?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes, actual_program_id, note, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def reconcile_date(self, air_date: str) -> list[dict]:
        """Compare the latest playout per slot with the plan and persist exceptions."""
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        with self.transaction():
            self.conn.execute("DELETE FROM reconciliation_exceptions WHERE air_date=?", (air_date,))
            slots = self.conn.execute(
                "SELECT s.*, p.title, p.sponsor, p.kind FROM slots s JOIN programs p ON p.id=s.program_id "
                "WHERE s.air_date=? AND s.status!='cancelled' ORDER BY s.start_time", (air_date,)
            ).fetchall()
            exceptions: list[tuple[int, str, str]] = []
            for slot in slots:
                log = self.conn.execute(
                    "SELECT * FROM playout_logs WHERE slot_id=? ORDER BY id DESC LIMIT 1", (slot["id"],)
                ).fetchone()
                if not log:
                    exceptions.append((slot["id"], "missed", "没有实播记录"))
                    continue
                actual_program_id = log["actual_program_id"] or slot["program_id"]
                if actual_program_id != slot["program_id"]:
                    exceptions.append((slot["id"], "wrong_program", f"计划节目 #{slot['program_id']}，实播节目 #{actual_program_id}"))
                delta = log["actual_duration_minutes"] - slot["duration_minutes"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot["id"], kind, f"与计划相差 {delta:+d} 分钟"))
                actual = self.conn.execute(
                    "SELECT p.* FROM programs p WHERE p.id=?", (actual_program_id,)
                ).fetchone()
                if actual:
                    # 已播段按播出时刻的授权快照判越权，不拿后来窗口追溯
                    snapshot = self.conn.execute(
                        "SELECT asnap.* FROM authorization_snapshots asnap "
                        "JOIN backhaul_segments bs ON bs.snapshot_id=asnap.id "
                        "WHERE bs.playout_log_id=? ORDER BY asnap.id DESC LIMIT 1",
                        (log["id"],),
                    ).fetchone()
                    if snapshot:
                        if not snapshot["authorized"] or not snapshot["within_window"]:
                            exceptions.append((slot["id"], "out_of_license",
                                                f"实播节目在播出时刻未授权（授权快照判定）：{snapshot['detail']}"))
                    else:
                        region_ok = self.conn.execute(
                            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (actual_program_id, slot["region"])
                        ).fetchone()
                        if not region_ok or not (actual["start_date"] <= air_date <= actual["end_date"]):
                            exceptions.append((slot["id"], "out_of_license", "实播节目超出地区或日期授权"))
            for slot_id, kind, detail in exceptions:
                self.conn.execute(
                    "INSERT INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                    (air_date, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(air_date)

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    # ------------------------------------------------------------------
    # 发射台与离线回传批次
    # ------------------------------------------------------------------

    def add_station(self, code: str, name: str) -> int:
        if not code.strip() or not name.strip():
            raise DomainError("发射台编号和名称不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO stations(code,name,created_at) VALUES(?,?,?)",
                    (code.strip(), name.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError(f"发射台编号已存在: {code}") from exc
        return int(cur.lastrowid)

    def list_stations(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM backhaul_packages bp WHERE bp.station_id=s.id) AS package_count "
            "FROM stations s ORDER BY s.code"
        ).fetchall()]

    def receive_backhaul(self, station_code: str, package_no: str, segments: list[dict]) -> dict:
        """Receive an offline backhaul batch.

        Each station is accepted once per package number. A retry only
        re-processes segments that have not been posted yet; segments that
        already produced a playout log are never duplicated. Validation
        failures keep the package in 'pending' (待核).
        """
        if not station_code.strip():
            raise DomainError("发射台编号不能为空")
        if not package_no.strip():
            raise DomainError("包号不能为空")
        if not segments:
            raise DomainError("回传批次不能为空")
        station = self.conn.execute(
            "SELECT * FROM stations WHERE code=?", (station_code.strip(),)
        ).fetchone()
        if not station:
            raise DomainError(f"发射台不存在: {station_code}")
        now = datetime.now().isoformat()
        with self.transaction():
            pkg = self.conn.execute(
                "SELECT * FROM backhaul_packages WHERE station_id=? AND package_no=?",
                (station["id"], package_no.strip()),
            ).fetchone()
            is_retry = pkg is not None
            if not pkg:
                cur = self.conn.execute(
                    "INSERT INTO backhaul_packages(station_id,package_no,status,total_segments,received_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (station["id"], package_no.strip(), "pending", len(segments), now, now),
                )
                pkg = self.conn.execute(
                    "SELECT * FROM backhaul_packages WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            for idx, seg in enumerate(segments, start=1):
                seg_no = int(seg.get("segment_no") or idx)
                if is_retry:
                    existing = self.conn.execute(
                        "SELECT * FROM backhaul_segments WHERE package_id=? AND segment_no=?",
                        (pkg["id"], seg_no),
                    ).fetchone()
                    if existing and existing["playout_log_id"] is not None:
                        # 已入账的实播不重复新增
                        continue
                self._validate_and_post_segment(pkg, seg_no, seg, station["id"], now)
            self.conn.execute(
                "INSERT INTO backhaul_events(package_id,event_type,detail,created_at) VALUES(?,?,?,?)",
                (pkg["id"], "received" if not is_retry else "retried",
                 f"提交 {len(segments)} 段", now),
            )
            self._refresh_package_status(pkg["id"])
        return self.get_backhaul_package(pkg["id"])

    def _validate_and_post_segment(self, pkg: sqlite3.Row, seg_no: int, seg: dict,
                                   station_id: int, now: str) -> dict:
        air_date = str(seg.get("air_date", ""))
        start_time = str(seg.get("start_time", ""))
        region = str(seg.get("region", "") or "").strip()
        actual_start = str(seg.get("actual_start", ""))
        actual_duration = seg.get("actual_duration_minutes")
        actual_program_id = seg.get("actual_program_id")
        try:
            datetime.strptime(air_date, "%Y-%m-%d")
        except ValueError:
            return self._upsert_segment(pkg, seg_no, seg, "missing", "播出日期格式无效", None, None, now)
        try:
            _minutes(start_time)
        except ValueError:
            return self._upsert_segment(pkg, seg_no, seg, "missing", "开始时间格式无效", None, None, now)
        try:
            _minutes(actual_start)
        except ValueError:
            return self._upsert_segment(pkg, seg_no, seg, "missing", "实际开始时间格式无效", None, None, now)
        try:
            duration = int(actual_duration)
        except (TypeError, ValueError):
            return self._upsert_segment(pkg, seg_no, seg, "missing", "实际时长格式无效", None, None, now)
        if duration < 0:
            return self._upsert_segment(pkg, seg_no, seg, "missing", "实际时长不能为负数", None, None, now)
        # 按当前节目单匹配排期
        slot = None
        if region:
            slot = self.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND start_time=? AND region=? AND status!='cancelled' LIMIT 1",
                (air_date, start_time, region),
            ).fetchone()
        else:
            candidates = self.conn.execute(
                "SELECT * FROM slots WHERE air_date=? AND start_time=? AND status!='cancelled'",
                (air_date, start_time),
            ).fetchall()
            if len(candidates) == 1:
                slot = candidates[0]
                region = str(slot["region"])
            elif len(candidates) > 1:
                return self._upsert_segment(pkg, seg_no, seg, "missing",
                                            "该时间有多个排期，请指定地区", None, None, now)
        if not slot:
            return self._upsert_segment(pkg, seg_no, seg, "missing",
                                        "当前节目单中找不到对应排期", None, None, now)
        region = str(slot["region"])
        program_id: int | None = None
        if actual_program_id:
            try:
                program_id = int(actual_program_id)
            except (TypeError, ValueError):
                program_id = None
        if not program_id:
            return self._upsert_segment(pkg, seg_no, seg, "pending", "缺少实播节目", slot["id"], None, now, region=region)
        program = self.conn.execute(
            "SELECT * FROM programs WHERE id=?", (program_id,)
        ).fetchone()
        if not program:
            return self._upsert_segment(pkg, seg_no, seg, "pending", "实播节目不存在", slot["id"], None, now, region=region)
        # 授权快照在播出时刻冻结，之后窗口变化不追溯
        region_ok = self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_id=? AND region=?", (program["id"], region)
        ).fetchone()
        within_window = program["start_date"] <= air_date <= program["end_date"]
        authorized = 1 if (region_ok and within_window) else 0
        parts: list[str] = []
        if not region_ok:
            parts.append(f"未授权地区 {region}")
        if not within_window:
            parts.append("超出授权日期窗口")
        detail = "；".join(parts) if parts else f"授权地区 {region} 且在授权窗口内"
        cur = self.conn.execute(
            "INSERT INTO authorization_snapshots"
            "(station_id,program_id,region,air_date,actual_start,authorized,within_window,source,detail,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (station_id, program["id"], region, air_date, actual_start, authorized,
             1 if within_window else 0, "backhaul", detail, now),
        )
        snapshot_id = int(cur.lastrowid)
        # 段已播出，实播入账（已入账的不会重复新增）
        cur = self.conn.execute(
            "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (slot["id"], actual_start, duration, program["id"],
             f"离线回传包 {pkg['package_no']} 段 {seg_no}", now),
        )
        log_id = int(cur.lastrowid)
        status = "accepted" if authorized else "pending"
        fail_reason = None if authorized else f"实播节目在播出时刻未授权：{detail}"
        return self._upsert_segment(pkg, seg_no, seg, status, fail_reason, slot["id"], log_id, now,
                                    snapshot_id=snapshot_id, region=region)

    def _upsert_segment(self, pkg: sqlite3.Row, seg_no: int, seg: dict, status: str,
                        fail_reason: str | None, slot_id: int | None, log_id: int | None,
                        now: str, snapshot_id: int | None = None, region: str | None = None) -> dict:
        air_date = str(seg.get("air_date", ""))
        start_time = str(seg.get("start_time", ""))
        region = region if region is not None else str(seg.get("region", "") or "")
        actual_start = str(seg.get("actual_start", "") or "")
        try:
            actual_duration = int(seg.get("actual_duration_minutes")) if seg.get("actual_duration_minutes") is not None else None
        except (TypeError, ValueError):
            actual_duration = None
        try:
            actual_program_id = int(seg.get("actual_program_id")) if seg.get("actual_program_id") else None
        except (TypeError, ValueError):
            actual_program_id = None
        existing = self.conn.execute(
            "SELECT id FROM backhaul_segments WHERE package_id=? AND segment_no=?", (pkg["id"], seg_no)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE backhaul_segments SET slot_id=?,air_date=?,start_time=?,region=?,actual_start=?,"
                "actual_duration_minutes=?,actual_program_id=?,status=?,fail_reason=?,playout_log_id=?,"
                "snapshot_id=?,updated_at=? WHERE id=?",
                (slot_id, air_date, start_time, region, actual_start, actual_duration,
                 actual_program_id, status, fail_reason, log_id, snapshot_id, now, existing["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO backhaul_segments(package_id,segment_no,slot_id,air_date,start_time,region,"
                "actual_start,actual_duration_minutes,actual_program_id,status,fail_reason,playout_log_id,"
                "snapshot_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pkg["id"], seg_no, slot_id, air_date, start_time, region, actual_start,
                 actual_duration, actual_program_id, status, fail_reason, log_id, snapshot_id, now, now),
            )
        return {"segment_no": seg_no, "status": status, "fail_reason": fail_reason,
                "playout_log_id": log_id, "snapshot_id": snapshot_id}

    def _refresh_package_status(self, pkg_id: int) -> None:
        counts = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM backhaul_segments WHERE package_id=? GROUP BY status",
            (pkg_id,),
        ).fetchall()
        d = {str(r["status"]): int(r["n"]) for r in counts}
        total = sum(d.values())
        accepted = d.get("accepted", 0)
        status = "accepted" if total > 0 and accepted == total else "pending"
        self.conn.execute(
            "UPDATE backhaul_packages SET status=?,total_segments=?,accepted_segments=?,"
            "pending_segments=?,missing_segments=?,updated_at=? WHERE id=?",
            (status, total, accepted, d.get("pending", 0), d.get("missing", 0),
             datetime.now().isoformat(), pkg_id),
        )

    def recalculate_pending_backhaul(self, reason: str = "编排改动") -> dict:
        """Void pending packages and recalculate them against the current playlist.

        Already-posted segments keep their frozen authorization snapshot; only
        segments that were never posted are re-evaluated against the current
        playlist and authorization state.
        """
        with self.transaction():
            return self._recalculate_pending(reason)

    def _recalculate_pending(self, reason: str) -> dict:
        now = datetime.now().isoformat()
        pkgs = self.conn.execute(
            "SELECT * FROM backhaul_packages WHERE status='pending' ORDER BY id"
        ).fetchall()
        voided = 0
        resolved = 0
        for pkg in pkgs:
            self.conn.execute(
                "UPDATE backhaul_packages SET status='void',void_reason=?,voided_at=?,"
                "recalc_round=recalc_round+1,updated_at=? WHERE id=?",
                (reason, now, now, pkg["id"]),
            )
            self.conn.execute(
                "INSERT INTO backhaul_events(package_id,event_type,detail,created_at) VALUES(?,?,?,?)",
                (pkg["id"], "voided", f"待核数据作废：{reason}", now),
            )
            voided += 1
            segs = self.conn.execute(
                "SELECT * FROM backhaul_segments WHERE package_id=? AND playout_log_id IS NULL ORDER BY segment_no",
                (pkg["id"],),
            ).fetchall()
            for seg in segs:
                result = self._validate_and_post_segment(
                    pkg, int(seg["segment_no"]), self._seg_dict(seg), int(pkg["station_id"]), now,
                )
                if result["status"] == "accepted":
                    resolved += 1
            self.conn.execute(
                "INSERT INTO backhaul_events(package_id,event_type,detail,created_at) VALUES(?,?,?,?)",
                (pkg["id"], "recalculated",
                 f"按当前节目单和授权快照重算，{len(segs)} 段待处理，{resolved} 段转已入账", now),
            )
            self._refresh_package_status(pkg["id"])
        return {"voided_packages": voided, "resolved_segments": resolved}

    @staticmethod
    def _seg_dict(seg: sqlite3.Row) -> dict:
        return {
            "air_date": seg["air_date"],
            "start_time": seg["start_time"],
            "region": seg["region"],
            "actual_start": seg["actual_start"],
            "actual_duration_minutes": seg["actual_duration_minutes"],
            "actual_program_id": seg["actual_program_id"],
        }

    def get_backhaul_package(self, package_id: int) -> dict:
        pkg = self.conn.execute(
            "SELECT bp.*, s.code AS station_code, s.name AS station_name "
            "FROM backhaul_packages bp JOIN stations s ON s.id=bp.station_id WHERE bp.id=?",
            (package_id,),
        ).fetchone()
        if not pkg:
            raise DomainError("回传批次不存在")
        segments = [dict(r) for r in self.conn.execute(
            "SELECT bs.*, al.title AS actual_title FROM backhaul_segments bs "
            "LEFT JOIN programs al ON al.id=bs.actual_program_id "
            "WHERE bs.package_id=? ORDER BY bs.segment_no",
            (package_id,),
        ).fetchall()]
        events = [dict(r) for r in self.conn.execute(
            "SELECT * FROM backhaul_events WHERE package_id=? ORDER BY id", (package_id,)
        ).fetchall()]
        return {"package": dict(pkg), "segments": segments, "events": events}

    def list_backhaul_packages(self, station_code: str | None = None,
                               status: str | None = None) -> list[dict]:
        sql = ("SELECT bp.*, s.code AS station_code, s.name AS station_name "
               "FROM backhaul_packages bp JOIN stations s ON s.id=bp.station_id WHERE 1=1")
        params: list[object] = []
        if station_code:
            sql += " AND s.code=?"
            params.append(station_code.strip())
        if status:
            sql += " AND bp.status=?"
            params.append(status)
        sql += " ORDER BY bp.id DESC"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_authorization_snapshots(self, air_date: str | None = None,
                                     station_code: str | None = None) -> list[dict]:
        sql = ("SELECT asnap.*, s.code AS station_code, p.title AS program_title "
               "FROM authorization_snapshots asnap "
               "LEFT JOIN stations s ON s.id=asnap.station_id "
               "JOIN programs p ON p.id=asnap.program_id WHERE 1=1")
        params: list[object] = []
        if air_date:
            sql += " AND asnap.air_date=?"
            params.append(air_date)
        if station_code:
            sql += " AND s.code=?"
            params.append(station_code.strip())
        sql += " ORDER BY asnap.id"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def snapshot(self) -> dict:
        programs = [dict(row) for row in self.conn.execute("SELECT * FROM programs ORDER BY id").fetchall()]
        slots = [dict(row) for row in self.conn.execute(
            "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id ORDER BY s.air_date,s.start_time"
        ).fetchall()]
        return {"programs": programs, "slots": slots, "exceptions": [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
        ).fetchall()]}
