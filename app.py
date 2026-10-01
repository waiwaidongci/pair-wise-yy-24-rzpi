from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from database import DomainError, RadioDB

BASE = Path(__file__).resolve().parent
DB_PATH = os.environ.get("RADIO_DB", str(BASE / "radio.db"))


class Handler(BaseHTTPRequestHandler):
    db = RadioDB(DB_PATH)

    def log_message(self, fmt, *args):
        return

    def _json(self, status: int, payload) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DomainError("请求体必须是合法 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if parsed.path in ("/", "/index.html"):
                data = (BASE / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path == "/api/state":
                return self._json(200, self.db.snapshot())
            if parsed.path == "/api/reconciliation":
                date = parse_qs(parsed.query).get("date", [""])[0]
                if not date:
                    raise DomainError("缺少 date 参数")
                return self._json(200, {"exceptions": self.db.get_exceptions(date)})
            if parsed.path == "/api/stations":
                return self._json(200, {"stations": self.db.list_stations()})
            if parsed.path == "/api/backhaul":
                qs = parse_qs(parsed.query)
                return self._json(200, {"packages": self.db.list_backhaul_packages(
                    qs.get("station_code", [""])[0] or None,
                    qs.get("status", [""])[0] or None,
                )})
            if parsed.path == "/api/authorization-snapshots":
                qs = parse_qs(parsed.query)
                return self._json(200, {"snapshots": self.db.get_authorization_snapshots(
                    qs.get("air_date", [""])[0] or None,
                    qs.get("station_code", [""])[0] or None,
                )})
            if len(parts) == 3 and parts[:2] == ["api", "backhaul"] and parts[2].isdigit():
                return self._json(200, self.db.get_backhaul_package(int(parts[2])))
            self._json(404, {"ok": False, "error": "接口不存在"})
        except DomainError as exc:
            self._json(400, {"ok": False, "error": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parsed.path == "/api/programs":
                program_id = self.db.add_program(
                    str(body.get("title", "")), str(body.get("kind", "music")),
                    int(body.get("duration_minutes", 0)), str(body.get("start_date", "")),
                    str(body.get("end_date", "")), body.get("sponsor"), int(body.get("cooldown_minutes", 0)),
                    body.get("regions") or [],
                )
                return self._json(201, {"ok": True, "id": program_id})
            if parsed.path == "/api/schedule":
                slot_id = self.db.schedule_slot(
                    str(body.get("air_date", "")), str(body.get("start_time", "")),
                    int(body.get("program_id", 0)), str(body.get("region", "")),
                )
                return self._json(201, {"ok": True, "id": slot_id, "slot": self.db.get_slot(slot_id)})
            if parsed.path == "/api/playout":
                log_id = self.db.record_playout(
                    int(body.get("slot_id", 0)), str(body.get("actual_start", "")),
                    int(body.get("actual_duration_minutes", 0)),
                    int(body["actual_program_id"]) if body.get("actual_program_id") else None,
                    str(body.get("note", "")),
                )
                return self._json(201, {"ok": True, "id": log_id})
            if parsed.path == "/api/reconcile":
                return self._json(200, {"ok": True, "exceptions": self.db.reconcile_date(str(body.get("date", "")))})
            if parsed.path == "/api/stations":
                station_id = self.db.add_station(str(body.get("code", "")), str(body.get("name", "")))
                return self._json(201, {"ok": True, "id": station_id})
            if parsed.path == "/api/backhaul":
                result = self.db.receive_backhaul(
                    str(body.get("station_code", "")), str(body.get("package_no", "")),
                    body.get("segments") or [],
                )
                return self._json(201, {"ok": True, **result})
            if parsed.path == "/api/backhaul/recalculate":
                return self._json(200, {"ok": True,
                                        "recalculated": self.db.recalculate_pending_backhaul(str(body.get("reason", "编排改动")))})
            if len(parts) == 4 and parts[:2] == ["api", "slots"] and parts[3] == "replace":
                return self._json(200, {"ok": True, "slot": self.db.replace_slot(int(parts[2]), int(body.get("new_program_id", 0)))})
            if len(parts) == 4 and parts[:2] == ["api", "programs"] and parts[3] == "regions":
                self.db.authorize_region(int(parts[2]), str(body.get("region", "")))
                return self._json(201, {"ok": True})
            self._json(404, {"ok": False, "error": "接口不存在"})
        except (DomainError, ValueError) as exc:
            self._json(400, {"ok": False, "error": str(exc)})


def main() -> None:
    RadioDB(DB_PATH).seed_demo()
    port = int(os.environ.get("PORT", "8111"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Radio scheduling service: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
