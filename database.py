from __future__ import annotations

import hashlib
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


def segment_hash(segment: dict) -> str:
    """Canonical hash of a segment payload (the hash field itself excluded)."""
    payload = {k: segment.get(k) for k in ("slot_id", "actual_start", "actual_duration_minutes",
                                          "actual_program_id", "note", "played_at")}
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
              code TEXT PRIMARY KEY,
              name TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              captured_at TEXT NOT NULL,
              content_hash TEXT NOT NULL UNIQUE,
              entries_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingest_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              station_code TEXT NOT NULL,
              package_no TEXT NOT NULL,
              status TEXT NOT NULL
                CHECK(status IN ('receiving','pending_review','posted','voided')),
              segment_count INTEGER NOT NULL CHECK(segment_count > 0),
              manifest_json TEXT NOT NULL DEFAULT '{}',
              auth_snapshot_id INTEGER REFERENCES auth_snapshots(id),
              received_at TEXT NOT NULL,
              posted_at TEXT,
              voided_at TEXT,
              note TEXT NOT NULL DEFAULT '',
              UNIQUE(station_code, package_no)
            );
            CREATE TABLE IF NOT EXISTS ingest_segments (
              batch_id INTEGER NOT NULL REFERENCES ingest_batches(id) ON DELETE CASCADE,
              seq INTEGER NOT NULL,
              slot_id INTEGER REFERENCES slots(id),
              actual_start TEXT,
              actual_duration_minutes INTEGER,
              actual_program_id INTEGER,
              note TEXT NOT NULL DEFAULT '',
              played_at TEXT,
              expected_hash TEXT,
              content_hash TEXT NOT NULL,
              verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
              playout_log_id INTEGER REFERENCES playout_logs(id),
              PRIMARY KEY(batch_id, seq)
            );
            """
        )
        # Migrations for databases created before offline backhaul existed.
        self._ensure_column("playout_logs", "source_batch_id", "INTEGER REFERENCES ingest_batches(id)")
        self._ensure_column("playout_logs", "source_seq", "INTEGER")
        self._ensure_column("playout_logs", "auth_snapshot_id", "INTEGER REFERENCES auth_snapshots(id)")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_playout_batch_segment "
            "ON playout_logs(source_batch_id, source_seq) "
            "WHERE source_batch_id IS NOT NULL"
        )
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

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
        return int(cur.lastrowid)

    def replace_slot(self, slot_id: int, new_program_id: int) -> dict:
        """Replace a planned item and revalidate the resulting plan atomically.

        Pending (not yet booked) backhaul packages touching the date are voided
        immediately: their conclusions must be recomputed against the current
        plan. Already-booked playouts are untouched and keep being judged by the
        authorization snapshot pinned at playout time.
        """
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
            # Schedule changed: invalidate pending backhaul data for that day.
            self._void_pending_batches_for_date(slot["air_date"])
            air_date = slot["air_date"]
        # Recompute the reconciliation list against the current plan straight away.
        self.reconcile_date(air_date)
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
            snapshot_id = self._capture_snapshot()
            cur = self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,"
                "created_at,auth_snapshot_id) VALUES(?,?,?,?,?,?,?)",
                (slot_id, actual_start, actual_duration_minutes, actual_program_id, note,
                 datetime.now().isoformat(), snapshot_id),
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
                conclusions = self.playout_conclusions(slot, log, air_date)
                if not conclusions["program_matches"]:
                    exceptions.append((slot["id"], "wrong_program",
                                       f"计划节目 #{slot['program_id']}，实播节目 #{conclusions['actual_program_id']}"))
                delta = conclusions["duration_delta"]
                if abs(delta) > 30:
                    kind = "overrun" if delta > 0 else "underrun"
                    exceptions.append((slot["id"], kind, f"与计划相差 {delta:+d} 分钟"))
                if not conclusions["authorized"]:
                    exceptions.append((slot["id"], "out_of_license",
                                       f"实播节目超出播出时刻授权快照 #{conclusions['auth_snapshot_id']} 的地区或日期授权"))
            for slot_id, kind, detail in exceptions:
                self.conn.execute(
                    "INSERT INTO reconciliation_exceptions(air_date,slot_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                    (air_date, slot_id, kind, detail, datetime.now().isoformat()),
                )
        return self.get_exceptions(air_date)

    # ------------------------------------------------------------------
    # Authorization snapshots
    # ------------------------------------------------------------------

    def _live_auth_entries(self) -> list[dict]:
        """Freeze the *current* program/region/date-window authorizations."""
        rows = self.conn.execute(
            "SELECT p.id AS program_id, r.region, p.start_date, p.end_date, p.active "
            "FROM programs p JOIN program_regions r ON r.program_id = p.id WHERE p.active = 1 "
            "ORDER BY p.id, r.region"
        ).fetchall()
        return [{"program_id": row["program_id"], "region": row["region"],
                 "start_date": row["start_date"], "end_date": row["end_date"]} for row in rows]

    def _store_snapshot(self, entries: list[dict], captured_at: str) -> int:
        normalized = [
            {"program_id": int(e["program_id"]), "region": str(e["region"]),
             "start_date": str(e["start_date"]), "end_date": str(e["end_date"])}
            for e in sorted(entries, key=lambda e: (e["program_id"], e["region"]))
        ]
        content_hash = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        row = self.conn.execute("SELECT id FROM auth_snapshots WHERE content_hash=?", (content_hash,)).fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute(
            "INSERT INTO auth_snapshots(captured_at,content_hash,entries_json) VALUES(?,?,?)",
            (captured_at, content_hash, json.dumps(normalized, ensure_ascii=False)),
        )
        return int(cur.lastrowid)

    def _capture_snapshot(self, captured_at: str | None = None) -> int:
        return self._store_snapshot(self._live_auth_entries(), captured_at or datetime.now().isoformat())

    def _snapshot_entries(self, snapshot_id: int) -> list[dict]:
        row = self.conn.execute("SELECT entries_json FROM auth_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise DomainError(f"授权快照 #{snapshot_id} 不存在")
        return json.loads(row["entries_json"])

    def _resolve_log_snapshot_id(self, log: sqlite3.Row | dict) -> int:
        """Snapshot pinned at playout time; backfill legacy logs once, on first use."""
        snapshot_id = dict(log).get("auth_snapshot_id")
        if snapshot_id:
            return int(snapshot_id)
        snapshot_id = self._capture_snapshot(dict(log)["created_at"])
        self.conn.execute("UPDATE playout_logs SET auth_snapshot_id=? WHERE id=?", (snapshot_id, log["id"]))
        return snapshot_id

    def _authorized_under_snapshot(self, program_id: int, region: str, air_date: str,
                                   snapshot_id: int) -> bool:
        for entry in self._snapshot_entries(snapshot_id):
            if (entry["program_id"] == program_id and entry["region"] == region
                    and entry["start_date"] <= air_date <= entry["end_date"]):
                return True
        return False

    def playout_conclusions(self, slot: sqlite3.Row | dict, log: sqlite3.Row | dict,
                            air_date: str | None = None) -> dict:
        """Single source of truth shared by batches, snapshots and reconciliation."""
        slot, log = dict(slot), dict(log)
        air_date = air_date or slot["air_date"]
        snapshot_id = self._resolve_log_snapshot_id(log)
        actual_program_id = log["actual_program_id"] or slot["program_id"]
        return {
            "actual_program_id": actual_program_id,
            "program_matches": actual_program_id == slot["program_id"],
            "duration_delta": log["actual_duration_minutes"] - slot["duration_minutes"],
            "authorized": self._authorized_under_snapshot(actual_program_id, slot["region"], air_date, snapshot_id),
            "auth_snapshot_id": snapshot_id,
        }

    def get_snapshot(self, snapshot_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM auth_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if not row:
            raise DomainError("授权快照不存在")
        return {"id": row["id"], "captured_at": row["captured_at"],
                "content_hash": row["content_hash"], "entries": json.loads(row["entries_json"])}

    def playout_license_view(self, air_date: str) -> list[dict]:
        """Per-playout verdict for a date — identical verdict used in the batch view."""
        slots = {row["id"]: row for row in self.conn.execute(
            "SELECT * FROM slots WHERE air_date=?", (air_date,)).fetchall()}
        logs = self.conn.execute(
            "SELECT l.* FROM playout_logs l JOIN slots s ON s.id=l.slot_id "
            "WHERE s.air_date=? ORDER BY l.id", (air_date,)).fetchall()
        result = []
        for log in logs:
            slot = slots[log["slot_id"]]
            verdict = self.playout_conclusions(slot, log, air_date)
            result.append({
                "playout_log_id": log["id"], "slot_id": slot["id"],
                "actual_program_id": verdict["actual_program_id"],
                "planned_program_id": slot["program_id"],
                "auth_snapshot_id": verdict["auth_snapshot_id"],
                "program_matches": verdict["program_matches"],
                "duration_delta": verdict["duration_delta"],
                "authorized": verdict["authorized"],
                "source_batch_id": log["source_batch_id"], "source_seq": log["source_seq"],
            })
        return result

    def deauthorize_region(self, program_id: int, region: str) -> None:
        with self.transaction():
            self.conn.execute(
                "DELETE FROM program_regions WHERE program_id=? AND region=?", (program_id, region)
            )

    # ------------------------------------------------------------------
    # Offline backhaul batches (county transmitters)
    # ------------------------------------------------------------------

    @staticmethod
    def _exception_kinds(conclusions: dict) -> list[str]:
        """The exception kinds implied by a playout verdict — shared everywhere."""
        kinds: list[str] = []
        if not conclusions["program_matches"]:
            kinds.append("wrong_program")
        if abs(conclusions["duration_delta"]) > 30:
            kinds.append("overrun" if conclusions["duration_delta"] > 0 else "underrun")
        if not conclusions["authorized"]:
            kinds.append("out_of_license")
        return kinds

    @staticmethod
    def _validate_snapshot_entries(entries: object) -> list[dict]:
        if not isinstance(entries, list) or not entries:
            raise DomainError("授权快照必须包含非空的授权条目列表")
        normalized: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise DomainError("授权快照条目格式无效")
            try:
                program_id = int(entry["program_id"])
                region = str(entry["region"]).strip()
                start_date = str(entry["start_date"])
                end_date = str(entry["end_date"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainError("授权快照条目缺少 program_id/region/start_date/end_date") from exc
            if not region:
                raise DomainError("授权快照条目地区不能为空")
            try:
                start = datetime.strptime(start_date, "%Y-%m-%d").date()
                end = datetime.strptime(end_date, "%Y-%m-%d").date()
            except ValueError as exc:
                raise DomainError("授权快照日期必须使用 YYYY-MM-DD") from exc
            if end < start:
                raise DomainError("授权快照结束日期不能早于开始日期")
            normalized.append({"program_id": program_id, "region": region,
                               "start_date": start_date, "end_date": end_date})
        return normalized

    def receive_batch(self, station_code: str, package_no: str, segments: list[dict],
                      segment_count: int | None = None, manifest: dict | None = None,
                      auth_snapshot: dict | None = None, station_name: str = "") -> dict:
        """Receive (or continue) one offline backhaul package.

        Idempotent per (station, package_no): a posted package is never posted
        twice; retries only fill missing/unverified segments. A package with any
        failed segment is kept whole as pending_review and nothing is booked.
        """
        station_code = str(station_code or "").strip()
        package_no = str(package_no or "").strip()
        if not station_code or not package_no:
            raise DomainError("台站编号与包号不能为空")
        if not isinstance(segments, list) or not segments:
            raise DomainError("回传包至少包含一个分段")
        manifest = manifest if isinstance(manifest, dict) else {}
        manifest_hashes = manifest.get("hashes") or {}
        if not isinstance(manifest_hashes, dict):
            raise DomainError("manifest.hashes 必须是 段号->摘要 的映射")
        if segment_count is None:
            segment_count = manifest.get("segment_count")

        # Structural validation of the segments carried by this delivery.
        seen: set[int] = set()
        parsed: list[dict] = []
        for segment in segments:
            if not isinstance(segment, dict):
                raise DomainError("分段必须是对象")
            try:
                seq = int(segment["seq"])
                slot_id = int(segment["slot_id"])
                actual_start = str(segment["actual_start"])
                duration = int(segment["actual_duration_minutes"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DomainError("分段缺少 seq/slot_id/actual_start/actual_duration_minutes 或类型错误") from exc
            if seq in seen:
                raise DomainError(f"本次回传中段号 {seq} 重复")
            seen.add(seq)
            if seq < 0:
                raise DomainError("段号不能为负数")
            if duration < 0:
                raise DomainError(f"段 {seq} 实际时长不能为负数")
            try:
                _minutes(actual_start)
            except ValueError as exc:
                raise DomainError(f"段 {seq} 开始时间必须使用 HH:MM") from exc
            if not self.conn.execute("SELECT 1 FROM slots WHERE id=?", (slot_id,)).fetchone():
                raise DomainError(f"段 {seq} 引用的排期 #{slot_id} 不存在")
            actual_program_id = segment.get("actual_program_id")
            actual_program_id = int(actual_program_id) if actual_program_id is not None else None
            parsed.append({
                "seq": seq, "slot_id": slot_id, "actual_start": actual_start,
                "actual_duration_minutes": duration, "actual_program_id": actual_program_id,
                "note": str(segment.get("note", "")), "played_at": str(segment.get("played_at", "")),
            })

        with self.transaction():
            self.conn.execute(
                "INSERT INTO stations(code,name,created_at) VALUES(?,?,?) "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name WHERE excluded.name != ''",
                (station_code, station_name.strip(), datetime.now().isoformat()),
            )
            batch = self.conn.execute(
                "SELECT * FROM ingest_batches WHERE station_code=? AND package_no=?",
                (station_code, package_no),
            ).fetchone()

            if batch and batch["status"] == "posted":
                # Already booked: never create playout rows twice.
                return self._batch_summary(batch)

            # Freeze the playout-time authorization evidence up front, so later
            # schedule/window changes can never rewrite history.
            if isinstance(auth_snapshot, dict) and auth_snapshot.get("entries"):
                snapshot_id = self._store_snapshot(
                    self._validate_snapshot_entries(auth_snapshot["entries"]),
                    str(auth_snapshot.get("captured_at") or datetime.now().isoformat()),
                )
            elif batch is not None and batch["auth_snapshot_id"]:
                snapshot_id = int(batch["auth_snapshot_id"])
            else:
                # No carried snapshot (online fallback): freeze today's window.
                snapshot_id = self._capture_snapshot()

            if segment_count is None:
                if batch is not None:
                    segment_count = batch["segment_count"]
                else:
                    raise DomainError("首包必须提供 segment_count（或 manifest.segment_count），续传才能只补缺失段")
            segment_count = int(segment_count)
            if segment_count <= 0:
                raise DomainError("segment_count 必须大于0")
            if max(seen) >= segment_count:
                raise DomainError(f"段号超出包内总段数 {segment_count}")

            now = datetime.now().isoformat()
            if batch is None:
                cur = self.conn.execute(
                    "INSERT INTO ingest_batches(station_code,package_no,status,segment_count,manifest_json,"
                    "auth_snapshot_id,received_at,note) VALUES(?, ?, 'receiving', ?, ?, ?, ?, '')",
                    (station_code, package_no, segment_count,
                     json.dumps(manifest, ensure_ascii=False), snapshot_id, now),
                )
                batch_id = int(cur.lastrowid)
            else:
                batch_id = int(batch["id"])
                if batch["status"] != "voided" and int(batch["segment_count"]) != segment_count:
                    raise DomainError(
                        f"包 {package_no} 已声明总段数 {batch['segment_count']}，不能改为 {segment_count}"
                    )
                # A voided package resent by the station starts a fresh generation.
                self.conn.execute(
                    "UPDATE ingest_batches SET status='receiving', segment_count=?, manifest_json=?, "
                    "auth_snapshot_id=?, received_at=?, posted_at=NULL, voided_at=NULL, note='' WHERE id=?",
                    (segment_count, json.dumps(manifest, ensure_ascii=False), snapshot_id, now, batch_id),
                )
                if batch["status"] == "voided":
                    self.conn.execute("DELETE FROM ingest_segments WHERE batch_id=?", (batch_id,))

            for seg in parsed:
                content_hash = segment_hash(seg)
                expected = manifest_hashes.get(str(seg["seq"]), manifest_hashes.get(seg["seq"]))
                self.conn.execute(
                    "INSERT INTO ingest_segments(batch_id,seq,slot_id,actual_start,actual_duration_minutes,"
                    "actual_program_id,note,played_at,expected_hash,content_hash,verified) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                    # Verified segments are immutable; retries only fill missing
                    # ones or correct previously failed ones.
                    "ON CONFLICT(batch_id,seq) DO UPDATE SET "
                    "slot_id=CASE WHEN ingest_segments.verified=0 THEN excluded.slot_id ELSE ingest_segments.slot_id END,"
                    "actual_start=CASE WHEN ingest_segments.verified=0 THEN excluded.actual_start ELSE ingest_segments.actual_start END,"
                    "actual_duration_minutes=CASE WHEN ingest_segments.verified=0 THEN excluded.actual_duration_minutes ELSE ingest_segments.actual_duration_minutes END,"
                    "actual_program_id=CASE WHEN ingest_segments.verified=0 THEN excluded.actual_program_id ELSE ingest_segments.actual_program_id END,"
                    "note=CASE WHEN ingest_segments.verified=0 THEN excluded.note ELSE ingest_segments.note END,"
                    "played_at=CASE WHEN ingest_segments.verified=0 THEN excluded.played_at ELSE ingest_segments.played_at END,"
                    "expected_hash=COALESCE(ingest_segments.expected_hash, excluded.expected_hash),"
                    "content_hash=CASE WHEN ingest_segments.verified=0 THEN excluded.content_hash ELSE ingest_segments.content_hash END,"
                    "verified=CASE WHEN ingest_segments.verified=0 THEN excluded.verified ELSE 1 END",
                    (batch_id, seg["seq"], seg["slot_id"], seg["actual_start"], seg["actual_duration_minutes"],
                     seg["actual_program_id"], seg["note"], seg["played_at"],
                     str(expected) if expected is not None else None, content_hash, int(expected is None or str(expected) == content_hash)),
                )

            rows = self.conn.execute(
                "SELECT * FROM ingest_segments WHERE batch_id=?", (batch_id,)
            ).fetchall()
            verified_seqs = {row["seq"] for row in rows if row["verified"]}
            bad_seqs = sorted(row["seq"] for row in rows if not row["verified"])
            missing = sorted(set(range(segment_count)) - verified_seqs)
            if bad_seqs:
                # Whole package is retained for review; nothing is booked.
                self.conn.execute(
                    "UPDATE ingest_batches SET status='pending_review', note=? WHERE id=?",
                    (f"段 {','.join(map(str, bad_seqs))} 校验失败，整包保留待核", batch_id),
                )
            elif missing:
                self.conn.execute(
                    "UPDATE ingest_batches SET status='receiving', note=? WHERE id=?",
                    (f"缺失段 {','.join(map(str, missing))}", batch_id),
                )
            else:
                self._post_batch(batch_id)

            batch = self.conn.execute("SELECT * FROM ingest_batches WHERE id=?", (batch_id,)).fetchone()
            return self._batch_summary(batch)

    def _post_batch(self, batch_id: int) -> None:
        """All segments verified: book playout rows exactly once, pinned to the snapshot."""
        batch = self.conn.execute("SELECT * FROM ingest_batches WHERE id=?", (batch_id,)).fetchone()
        snapshot_id = int(batch["auth_snapshot_id"])
        rows = self.conn.execute(
            "SELECT * FROM ingest_segments WHERE batch_id=? ORDER BY seq", (batch_id,)
        ).fetchall()
        for seg in rows:
            self.conn.execute(
                "INSERT INTO playout_logs(slot_id,actual_start,actual_duration_minutes,actual_program_id,note,"
                "created_at,source_batch_id,source_seq,auth_snapshot_id) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT DO NOTHING",
                (seg["slot_id"], seg["actual_start"], seg["actual_duration_minutes"], seg["actual_program_id"],
                 seg["note"], seg["played_at"] or datetime.now().isoformat(),
                 batch_id, seg["seq"], snapshot_id),
            )
            log = self.conn.execute(
                "SELECT id FROM playout_logs WHERE source_batch_id=? AND source_seq=?",
                (batch_id, seg["seq"]),
            ).fetchone()
            self.conn.execute(
                "UPDATE ingest_segments SET playout_log_id=? WHERE batch_id=? AND seq=?",
                (log["id"], batch_id, seg["seq"]),
            )
        self.conn.execute(
            "UPDATE ingest_batches SET status='posted', posted_at=?, note='' WHERE id=?",
            (datetime.now().isoformat(), batch_id),
        )

    def _batch_summary(self, batch: sqlite3.Row) -> dict:
        batch = dict(batch)
        rows = self.conn.execute(
            "SELECT g.*, s.air_date, s.region FROM ingest_segments g JOIN slots s ON s.id=g.slot_id "
            "WHERE g.batch_id=? ORDER BY g.seq", (batch["id"],)).fetchall()
        segments = []
        batch_exception_kinds: set[str] = set()
        for row in rows:
            item = {"seq": row["seq"], "slot_id": row["slot_id"], "verified": bool(row["verified"]),
                    "playout_log_id": row["playout_log_id"]}
            if batch["status"] == "posted" and row["playout_log_id"]:
                slot = self.conn.execute("SELECT * FROM slots WHERE id=?", (row["slot_id"],)).fetchone()
                log = self.conn.execute("SELECT * FROM playout_logs WHERE id=?",
                                        (row["playout_log_id"],)).fetchone()
                verdict = self.playout_conclusions(slot, log, row["air_date"])
                kinds = self._exception_kinds(verdict)
                batch_exception_kinds.update(kinds)
                item.update({
                    "actual_program_id": verdict["actual_program_id"],
                    "planned_program_id": slot["program_id"],
                    "program_matches": verdict["program_matches"],
                    "duration_delta": verdict["duration_delta"],
                    "authorized": verdict["authorized"],
                    "auth_snapshot_id": verdict["auth_snapshot_id"],
                    "exception_kinds": kinds,
                })
            segments.append(item)
        present = {row["seq"] for row in rows if row["verified"]}
        return {
            "id": batch["id"], "station_code": batch["station_code"], "package_no": batch["package_no"],
            "status": batch["status"], "segment_count": batch["segment_count"],
            "missing_segments": sorted(set(range(batch["segment_count"])) - present),
            "auth_snapshot_id": batch["auth_snapshot_id"], "note": batch["note"],
            "received_at": batch["received_at"], "posted_at": batch["posted_at"],
            "voided_at": batch["voided_at"], "segments": segments,
            # Same conclusion the reconciliation list will produce for these playouts.
            "exception_kinds": sorted(batch_exception_kinds),
        }

    def get_batch(self, station_code: str, package_no: str) -> dict:
        batch = self.conn.execute(
            "SELECT * FROM ingest_batches WHERE station_code=? AND package_no=?",
            (str(station_code).strip(), str(package_no).strip()),
        ).fetchone()
        if not batch:
            raise DomainError("回传包不存在")
        return self._batch_summary(batch)

    def list_batches(self, station_code: str | None = None) -> list[dict]:
        if station_code:
            rows = self.conn.execute(
                "SELECT * FROM ingest_batches WHERE station_code=? ORDER BY id", (str(station_code).strip(),)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM ingest_batches ORDER BY id").fetchall()
        return [self._batch_summary(row) for row in rows]

    def _void_pending_batches_for_date(self, air_date: str) -> int:
        """Schedule changed: pending packages touching the date are voided immediately."""
        cur = self.conn.execute(
            "UPDATE ingest_batches SET status='voided', voided_at=?, "
            "note='编排改动，待核数据作废，需按当前节目单与授权快照重算' "
            "WHERE status IN ('receiving','pending_review') AND id IN ("
            "SELECT DISTINCT g.batch_id FROM ingest_segments g JOIN slots s ON s.id=g.slot_id "
            "WHERE s.air_date=?)",
            (datetime.now().isoformat(), air_date),
        )
        return cur.rowcount

    def get_exceptions(self, air_date: str) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions WHERE air_date=? ORDER BY slot_id, kind", (air_date,)
        ).fetchall()]

    def snapshot(self) -> dict:
        programs = [dict(row) for row in self.conn.execute("SELECT * FROM programs ORDER BY id").fetchall()]
        slots = [dict(row) for row in self.conn.execute(
            "SELECT s.*, p.title, p.kind FROM slots s JOIN programs p ON p.id=s.program_id ORDER BY s.air_date,s.start_time"
        ).fetchall()]
        batches = [self._batch_summary(row) for row in self.conn.execute(
            "SELECT * FROM ingest_batches ORDER BY id").fetchall()]
        auth_snapshots = [{"id": row["id"], "captured_at": row["captured_at"],
                           "content_hash": row["content_hash"]}
                          for row in self.conn.execute(
                              "SELECT id,captured_at,content_hash FROM auth_snapshots ORDER BY id").fetchall()]
        return {"programs": programs, "slots": slots, "batches": batches,
                "auth_snapshots": auth_snapshots, "exceptions": [dict(row) for row in self.conn.execute(
            "SELECT * FROM reconciliation_exceptions ORDER BY id DESC LIMIT 50"
        ).fetchall()]}
