#!/usr/bin/env python3
"""Rixen/Espar edge collector for intermittent AP devices.

- Watches a dedicated WiFi interface for Rixen AP association
- Probes the controller when reachable
- Exposes local HTTP API for Home Assistant ingestion
- Buffers snapshots in SQLite
- Tracks rough fuel estimate from inferred burner runtime

Designed for robustness when AP is offline/sleeping.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

INTERFACE = os.environ.get("RIXEN_INTERFACE", "wlx9cefd5f9631f")
SSID = os.environ.get("RIXEN_SSID", "Rixen000000")
CONTROLLER_HOST = os.environ.get("RIXEN_HOST", "10.10.10.10")
POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "15"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8081"))
DB_PATH = os.environ.get("DB_PATH", "/var/lib/rixen-monitor/rixen.db")
STATE_PATH = os.environ.get("STATE_PATH", "/var/lib/rixen-monitor/runtime_state.json")
FUEL_RATE_GPH = float(os.environ.get("FUEL_RATE_GPH", "0.08"))
STALE_AFTER_S = int(os.environ.get("STALE_AFTER_S", "600"))
HTTP_TIMEOUT_S = float(os.environ.get("HTTP_TIMEOUT_S", "3.0"))

PROBE_PATHS = [
    "/api/status",
    "/api/state",
    "/status",
    "/state",
    "/json",
    "/",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("rixen-monitor")

shutdown_event = threading.Event()
state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

current_state: dict[str, Any] = {
    "timestamp": None,
    "iso_time": None,
    "source_state": "init",  # init|blocked|ok|error|stale
    "error_reason": None,
    "network": {
        "interface": INTERFACE,
        "ssid_target": SSID,
        "link_up": False,
        "associated": False,
        "current_ssid": None,
        "ipv4": None,
        "route_present": False,
        "controller_host": CONTROLLER_HOST,
    },
    "probe": {
        "url": None,
        "status_code": None,
        "content_type": None,
    },
    "controller": {
        "reachable": False,
        "payload_kind": None,  # json|text|none
        "burner_active": None,
        "water_temp_c": None,
        "setpoint_c": None,
        "ambient_temp_c": None,
        "pump_active": None,
        "raw_excerpt": None,
    },
    "fuel": {
        "rate_gph_assumed": FUEL_RATE_GPH,
        "burn_seconds_total": 0.0,
        "estimated_gallons_total": 0.0,
    },
    "last_seen": None,
    "last_seen_iso": None,
    "age_seconds": None,
    "sample_count": 0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cmd(args: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=5)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def get_iface_link_up(iface: str) -> bool:
    rc, out, _ = run_cmd(["ip", "-o", "link", "show", iface])
    if rc != 0 or not out:
        return False
    # Treat interface as administratively up when the flag list contains UP,
    # even if carrier/state is DOWN while AP is absent.
    m = re.search(r"<([^>]+)>", out)
    if not m:
        return False
    flags = {f.strip() for f in m.group(1).split(",")}
    return "UP" in flags


def get_iface_ipv4(iface: str) -> str | None:
    rc, out, _ = run_cmd(["ip", "-o", "-4", "addr", "show", "dev", iface])
    if rc != 0 or not out:
        return None
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", out)
    return m.group(1) if m else None


def get_iface_ssid(iface: str) -> str | None:
    rc, out, _ = run_cmd(["iw", "dev", iface, "link"])
    if rc != 0 or not out:
        return None
    m = re.search(r"SSID:\s+(.+)$", out, re.MULTILINE)
    return m.group(1).strip() if m else None


def route_present_for_host(host: str, iface: str) -> bool:
    rc, out, _ = run_cmd(["ip", "route", "get", host])
    if rc != 0:
        return False
    return f" dev {iface} " in f" {out} "


def probe_controller(host: str) -> dict[str, Any]:
    last_err = None
    for path in PROBE_PATHS:
        url = f"http://{host}{path}"
        req = Request(url, headers={"User-Agent": "rixen-monitor/1.0"})
        try:
            with urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:  # noqa: S310
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                text = raw.decode("utf-8", errors="replace")
                return {
                    "ok": True,
                    "url": url,
                    "status_code": getattr(resp, "status", None),
                    "content_type": ctype,
                    "text": text,
                }
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"

    return {
        "ok": False,
        "url": None,
        "status_code": None,
        "content_type": None,
        "text": None,
        "error": last_err or "probe_failed",
    }


BOOL_TRUE = {"1", "true", "on", "running", "active", "enabled", "heat", "heating"}
BOOL_FALSE = {"0", "false", "off", "idle", "inactive", "disabled", "standby"}


@dataclass
class ParsedController:
    burner_active: bool | None = None
    pump_active: bool | None = None
    water_temp_c: float | None = None
    setpoint_c: float | None = None
    ambient_temp_c: float | None = None
    raw_excerpt: str | None = None
    payload_kind: str = "none"


def _to_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if v == 1:
            return True
        if v == 0:
            return False
    if isinstance(v, str):
        s = v.strip().lower()
        if s in BOOL_TRUE:
            return True
        if s in BOOL_FALSE:
            return False
    return None


def _extract_temp_from_text(text: str, keywords: list[str]) -> float | None:
    for kw in keywords:
        m = re.search(rf"{kw}[^\d-]*(-?\d+(?:\.\d+)?)\s*°?\s*([CF])?", text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            unit = (m.group(2) or "C").upper()
            if unit == "F":
                val = (val - 32.0) * 5.0 / 9.0
            return round(val, 2)
    return None


def parse_controller_payload(text: str, ctype: str) -> ParsedController:
    parsed = ParsedController(raw_excerpt=(text[:300].replace("\n", " ").strip() or None))

    if "json" in (ctype or "").lower() or text.strip().startswith("{"):
        try:
            payload = json.loads(text)
            parsed.payload_kind = "json"
            flat = _flatten(payload)

            for key, val in flat.items():
                lk = key.lower()
                b = _to_bool(val)
                if parsed.burner_active is None and any(k in lk for k in ["burner", "flame", "heater", "combustion"]):
                    parsed.burner_active = b
                if parsed.pump_active is None and "pump" in lk:
                    parsed.pump_active = b
                if parsed.water_temp_c is None and any(k in lk for k in ["water_temp", "boiler_temp", "coolant_temp", "tank_temp"]):
                    parsed.water_temp_c = _to_celsius(val)
                if parsed.setpoint_c is None and any(k in lk for k in ["setpoint", "target_temp", "desired_temp"]):
                    parsed.setpoint_c = _to_celsius(val)
                if parsed.ambient_temp_c is None and any(k in lk for k in ["ambient", "room_temp", "inside_temp"]):
                    parsed.ambient_temp_c = _to_celsius(val)

            return parsed
        except Exception:  # noqa: BLE001
            pass

    parsed.payload_kind = "text"
    lower = text.lower()

    on_patterns = [
        r"burner\s*[:=]\s*(on|running|active|1|true)",
        r"heater\s*[:=]\s*(on|running|active|1|true)",
        r"flame\s*[:=]\s*(on|running|active|1|true)",
        r"status\s*[:=]\s*heating",
    ]
    off_patterns = [
        r"burner\s*[:=]\s*(off|idle|inactive|0|false)",
        r"heater\s*[:=]\s*(off|idle|inactive|0|false)",
        r"flame\s*[:=]\s*(off|idle|inactive|0|false)",
        r"status\s*[:=]\s*idle",
    ]
    if any(re.search(p, lower) for p in on_patterns):
        parsed.burner_active = True
    elif any(re.search(p, lower) for p in off_patterns):
        parsed.burner_active = False

    parsed.water_temp_c = _extract_temp_from_text(text, ["water", "boiler", "coolant"])
    parsed.setpoint_c = _extract_temp_from_text(text, ["setpoint", "target", "desired"])
    parsed.ambient_temp_c = _extract_temp_from_text(text, ["ambient", "inside", "room"])

    return parsed


def _to_celsius(v: Any) -> float | None:
    try:
        x = float(v)
    except Exception:  # noqa: BLE001
        return None
    if x > 80:  # likely Fahrenheit
        x = (x - 32.0) * 5.0 / 9.0
    return round(x, 2)


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            out.update(_flatten(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            out.update(_flatten(v, p))
    else:
        out[prefix] = obj
    return out


def ensure_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS samples (
            ts REAL PRIMARY KEY,
            source_state TEXT NOT NULL,
            error_reason TEXT,
            burner_active INTEGER,
            water_temp_c REAL,
            setpoint_c REAL,
            ambient_temp_c REAL,
            burn_seconds_total REAL,
            fuel_gallons_total REAL,
            payload TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
    conn.commit()
    return conn


def load_runtime_counters() -> tuple[float, float]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("burn_seconds_total", 0.0)), float(data.get("estimated_gallons_total", 0.0))
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


def save_runtime_counters(burn_seconds_total: float, gallons_total: float) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            {
                "burn_seconds_total": round(burn_seconds_total, 3),
                "estimated_gallons_total": round(gallons_total, 6),
                "updated_at": time.time(),
            },
            f,
            indent=2,
        )
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Collector thread
# ---------------------------------------------------------------------------


def collector_loop() -> None:
    conn = ensure_db()
    burn_seconds_total, gallons_total = load_runtime_counters()
    last_ts = time.time()

    with state_lock:
        current_state["fuel"]["burn_seconds_total"] = burn_seconds_total
        current_state["fuel"]["estimated_gallons_total"] = gallons_total

    log.info("Collector started: iface=%s ssid=%s host=%s interval=%.1fs", INTERFACE, SSID, CONTROLLER_HOST, POLL_INTERVAL_S)

    while not shutdown_event.is_set():
        now = time.time()
        dt = max(0.0, now - last_ts)
        last_ts = now

        link_up = get_iface_link_up(INTERFACE)
        current_ssid = get_iface_ssid(INTERFACE)
        ipv4 = get_iface_ipv4(INTERFACE)
        associated = (current_ssid == SSID)
        route_ok = route_present_for_host(CONTROLLER_HOST, INTERFACE) if ipv4 else False

        snapshot: dict[str, Any] = {
            "timestamp": now,
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "source_state": "blocked",
            "error_reason": None,
            "network": {
                "interface": INTERFACE,
                "ssid_target": SSID,
                "link_up": link_up,
                "associated": associated,
                "current_ssid": current_ssid,
                "ipv4": ipv4,
                "route_present": route_ok,
                "controller_host": CONTROLLER_HOST,
            },
            "probe": {
                "url": None,
                "status_code": None,
                "content_type": None,
            },
            "controller": {
                "reachable": False,
                "payload_kind": "none",
                "burner_active": None,
                "water_temp_c": None,
                "setpoint_c": None,
                "ambient_temp_c": None,
                "pump_active": None,
                "raw_excerpt": None,
            },
            "last_seen": current_state.get("last_seen"),
            "last_seen_iso": current_state.get("last_seen_iso"),
            "age_seconds": None,
            "sample_count": int(current_state.get("sample_count", 0)) + 1,
            "fuel": {
                "rate_gph_assumed": FUEL_RATE_GPH,
                "burn_seconds_total": burn_seconds_total,
                "estimated_gallons_total": gallons_total,
            },
        }

        # Connectivity gate ladder
        if not link_up:
            snapshot["error_reason"] = "iface_down"
        elif not associated:
            snapshot["error_reason"] = "not_associated"
        elif not ipv4:
            snapshot["error_reason"] = "no_ip"
        elif not route_ok:
            snapshot["error_reason"] = "no_route"
        else:
            # Probe controller only when all gates pass
            probe = probe_controller(CONTROLLER_HOST)
            snapshot["probe"]["url"] = probe.get("url")
            snapshot["probe"]["status_code"] = probe.get("status_code")
            snapshot["probe"]["content_type"] = probe.get("content_type")

            if not probe.get("ok"):
                snapshot["source_state"] = "error"
                snapshot["error_reason"] = probe.get("error", "probe_failed")
            else:
                parsed = parse_controller_payload(probe.get("text") or "", probe.get("content_type") or "")
                snapshot["source_state"] = "ok"
                snapshot["error_reason"] = None
                snapshot["controller"] = {
                    "reachable": True,
                    "payload_kind": parsed.payload_kind,
                    "burner_active": parsed.burner_active,
                    "water_temp_c": parsed.water_temp_c,
                    "setpoint_c": parsed.setpoint_c,
                    "ambient_temp_c": parsed.ambient_temp_c,
                    "pump_active": parsed.pump_active,
                    "raw_excerpt": parsed.raw_excerpt,
                }
                snapshot["last_seen"] = now
                snapshot["last_seen_iso"] = snapshot["iso_time"]

        # Burner runtime/fuel estimate
        burner_active = snapshot["controller"].get("burner_active") is True
        if burner_active:
            burn_seconds_total += dt
            gallons_total = burn_seconds_total / 3600.0 * FUEL_RATE_GPH

        snapshot["fuel"]["burn_seconds_total"] = round(burn_seconds_total, 2)
        snapshot["fuel"]["estimated_gallons_total"] = round(gallons_total, 5)

        last_seen = snapshot.get("last_seen")
        if isinstance(last_seen, (int, float)):
            age = int(now - float(last_seen))
            snapshot["age_seconds"] = age
            if snapshot["source_state"] != "ok" and age > STALE_AFTER_S:
                snapshot["source_state"] = "stale"

        with state_lock:
            current_state.clear()
            current_state.update(snapshot)

        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO samples (
                    ts, source_state, error_reason, burner_active,
                    water_temp_c, setpoint_c, ambient_temp_c,
                    burn_seconds_total, fuel_gallons_total, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    snapshot["source_state"],
                    snapshot["error_reason"],
                    1 if burner_active else (0 if snapshot["controller"].get("burner_active") is False else None),
                    snapshot["controller"].get("water_temp_c"),
                    snapshot["controller"].get("setpoint_c"),
                    snapshot["controller"].get("ambient_temp_c"),
                    snapshot["fuel"]["burn_seconds_total"],
                    snapshot["fuel"]["estimated_gallons_total"],
                    json.dumps(snapshot),
                ),
            )
            conn.commit()
            save_runtime_counters(burn_seconds_total, gallons_total)
        except Exception as exc:  # noqa: BLE001
            log.error("DB write failed: %s", exc)

        sleep_left = POLL_INTERVAL_S
        while sleep_left > 0 and not shutdown_event.is_set():
            step = min(0.5, sleep_left)
            time.sleep(step)
            sleep_left -= step

    conn.close()
    log.info("Collector stopped")


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.debug("HTTP %s", fmt % args)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        raw = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            with state_lock:
                self._send_json(dict(current_state))
            return

        if path == "/health":
            with state_lock:
                st = current_state.get("source_state", "init")
                self._send_json({"status": "ok", "source_state": st})
            return

        if path == "/history":
            q = parse_qs(parsed.query)
            hours = float(q.get("hours", ["12"])[0])
            since = time.time() - hours * 3600
            try:
                conn = sqlite3.connect(DB_PATH)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT ts, payload FROM samples WHERE ts > ? ORDER BY ts",
                    (since,),
                ).fetchall()
                conn.close()
                readings = []
                for r in rows:
                    obj = json.loads(r["payload"])
                    obj["timestamp"] = r["ts"]
                    readings.append(obj)
                self._send_json({"hours": hours, "count": len(readings), "readings": readings})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": str(exc)}, 500)
            return

        self._send_json({"error": "not found"}, 404)


def http_server() -> None:
    server = HTTPServer(("0.0.0.0", HTTP_PORT), APIHandler)
    server.timeout = 1.0
    log.info("HTTP server started on :%d", HTTP_PORT)
    while not shutdown_event.is_set():
        server.handle_request()
    server.server_close()
    log.info("HTTP server stopped")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Rixen/Espar monitor starting")

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("Received %s, shutting down", signal.Signals(signum).name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    threads = [
        threading.Thread(target=collector_loop, name="collector", daemon=True),
        threading.Thread(target=http_server, name="http", daemon=True),
    ]

    for t in threads:
        t.start()

    shutdown_event.wait()
    for t in threads:
        t.join(timeout=5)

    log.info("Rixen/Espar monitor stopped")


if __name__ == "__main__":
    main()
