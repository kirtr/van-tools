#!/usr/bin/env python3
"""Simarine Pico UDP monitor with HTTP API and SQLite logging.

Listens for Pico UDP broadcasts on port 43210, decodes sensor data,
serves current state via HTTP JSON API, and logs to SQLite periodically.

Environment variables:
    HTTP_PORT       - HTTP API port (default: 8080)
    LOG_INTERVAL_S  - SQLite log interval in seconds (default: 60)
    DB_PATH         - SQLite database path (default: simarine.db)
    UDP_PORT        - UDP listen port (default: 43210)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sqlite3
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlparse, parse_qs

from sensor_map import SENSOR_MAP, INTERNAL_ELEMENTS, is_disconnected

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

UDP_PORT: int = int(os.environ.get("UDP_PORT", "43210"))
HTTP_PORT: int = int(os.environ.get("HTTP_PORT", "8080"))
LOG_INTERVAL_S: int = int(os.environ.get("LOG_INTERVAL_S", "60"))
DB_PATH: str = os.environ.get("DB_PATH", "simarine.db")

HEADER_SIZE: int = 14
FIELD_SIZE: int = 7

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("simarine")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

state_lock = threading.Lock()
current_state: dict[str, Any] = {
    "timestamp": None,
    "raw_elements": {},
    "decoded": {},
    "packet_count": 0,
}

shutdown_event = threading.Event()

# ---------------------------------------------------------------------------
# Packet parsing
# ---------------------------------------------------------------------------


def parse_packet(data: bytes) -> dict[int, tuple[int, int]]:
    """Parse a Simarine UDP packet into element_id -> (a, b) pairs.

    Format: 14-byte header, then repeating 7-byte fields:
      [field_nr] [0x01 type] [a_hi] [a_lo] [b_hi] [b_lo] [0xff sep]
    """
    elements: dict[int, tuple[int, int]] = {}
    payload = data[HEADER_SIZE:]

    i = 0
    while i + FIELD_SIZE <= len(payload):
        field_nr = payload[i]
        # type_byte = payload[i + 1]  # always 0x01
        a = (payload[i + 2] << 8) | payload[i + 3]
        b = (payload[i + 4] << 8) | payload[i + 5]
        # sep = payload[i + 6]  # always 0xff
        elements[field_nr] = (a, b)
        i += FIELD_SIZE

    return elements


def decode_elements(elements: dict[int, tuple[int, int]]) -> dict[str, Any]:
    """Decode raw element values using the sensor map."""
    decoded: dict[str, Any] = {}

    for el_id, (a, b) in elements.items():
        if el_id in INTERNAL_ELEMENTS:
            continue
        if is_disconnected(a, b):
            continue

        if el_id in SENSOR_MAP:
            cfg = SENSOR_MAP[el_id]
            raw_val = a if cfg["field"] == "a" else b
            value = cfg["decode"](raw_val)
            decoded[cfg["name"]] = {
                "value": value,
                "unit": cfg["unit"],
                "element": el_id,
                "raw_a": a,
                "raw_b": b,
                "description": cfg["description"],
            }
        else:
            # Unknown element — expose raw values for calibration
            decoded[f"unknown_el{el_id}"] = {
                "value": None,
                "unit": "raw",
                "element": el_id,
                "raw_a": a,
                "raw_b": b,
                "description": "Unmapped element — raw values for identification",
            }

    return decoded


# ---------------------------------------------------------------------------
# UDP listener
# ---------------------------------------------------------------------------


def udp_listener() -> None:
    """Listen for Simarine UDP broadcasts and update shared state."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(2.0)
    sock.bind(("", UDP_PORT))
    log.info("UDP listener started on port %d", UDP_PORT)

    while not shutdown_event.is_set():
        try:
            data, addr = sock.recvfrom(1024)
        except socket.timeout:
            continue
        except OSError:
            if shutdown_event.is_set():
                break
            raise

        elements = parse_packet(data)
        decoded = decode_elements(elements)

        # Build raw element dict with hex-friendly values
        raw = {
            str(k): {"a": v[0], "b": v[1]}
            for k, v in sorted(elements.items())
        }

        with state_lock:
            current_state["timestamp"] = time.time()
            current_state["raw_elements"] = raw
            current_state["decoded"] = decoded
            current_state["packet_count"] += 1

    sock.close()
    log.info("UDP listener stopped")


# ---------------------------------------------------------------------------
# SQLite logging
# ---------------------------------------------------------------------------


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialize SQLite database with WAL mode for SD card friendliness."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            timestamp REAL PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_readings_ts
        ON readings(timestamp)
    """)
    conn.commit()
    return conn


def db_logger() -> None:
    """Periodically log current state to SQLite."""
    conn = init_db(DB_PATH)
    log.info("SQLite logger started (interval=%ds, path=%s)", LOG_INTERVAL_S, DB_PATH)

    while not shutdown_event.is_set():
        shutdown_event.wait(LOG_INTERVAL_S)
        if shutdown_event.is_set():
            break

        with state_lock:
            if current_state["timestamp"] is None:
                continue
            snapshot = {
                "timestamp": current_state["timestamp"],
                "decoded": current_state["decoded"],
                "raw_elements": current_state["raw_elements"],
            }

        try:
            conn.execute(
                "INSERT OR REPLACE INTO readings (timestamp, data) VALUES (?, ?)",
                (snapshot["timestamp"], json.dumps(snapshot)),
            )
            conn.commit()
            log.debug("Logged reading at %.0f", snapshot["timestamp"])
        except sqlite3.Error as e:
            log.error("SQLite write error: %s", e)

    conn.close()
    log.info("SQLite logger stopped")


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


class APIHandler(BaseHTTPRequestHandler):
    """Minimal JSON API handler."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Route HTTP logs through our logger."""
        log.debug("HTTP %s", format % args)

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._handle_current()
        elif path == "/history":
            params = parse_qs(parsed.query)
            hours = float(params.get("hours", ["24"])[0])
            self._handle_history(hours)
        elif path == "/health":
            self._send_json({"status": "ok"})
        else:
            self._send_json({"error": "not found"}, 404)

    def _handle_current(self) -> None:
        with state_lock:
            snapshot = {
                "timestamp": current_state["timestamp"],
                "iso_time": (
                    time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(current_state["timestamp"])
                    )
                    if current_state["timestamp"]
                    else None
                ),
                "packet_count": current_state["packet_count"],
                "decoded": current_state["decoded"],
                "raw_elements": current_state["raw_elements"],
            }
        self._send_json(snapshot)

    def _handle_history(self, hours: float) -> None:
        since = time.time() - (hours * 3600)
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, data FROM readings WHERE timestamp > ? ORDER BY timestamp",
                (since,),
            ).fetchall()
            conn.close()

            readings = []
            for row in rows:
                entry = json.loads(row["data"])
                entry["timestamp"] = row["timestamp"]
                readings.append(entry)

            self._send_json({
                "hours": hours,
                "count": len(readings),
                "readings": readings,
            })
        except sqlite3.Error as e:
            self._send_json({"error": str(e)}, 500)


def http_server() -> None:
    """Run the HTTP API server."""
    server = HTTPServer(("0.0.0.0", HTTP_PORT), APIHandler)
    server.timeout = 1.0
    log.info("HTTP server started on port %d", HTTP_PORT)

    while not shutdown_event.is_set():
        server.handle_request()

    server.server_close()
    log.info("HTTP server stopped")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Simarine Pico Monitor starting")
    log.info(
        "Config: UDP=%d HTTP=%d LOG_INTERVAL=%ds DB=%s",
        UDP_PORT, HTTP_PORT, LOG_INTERVAL_S, DB_PATH,
    )

    def handle_signal(signum: int, _frame: Any) -> None:
        sig_name = signal.Signals(signum).name
        log.info("Received %s, shutting down...", sig_name)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    threads = [
        threading.Thread(target=udp_listener, name="udp", daemon=True),
        threading.Thread(target=db_logger, name="db", daemon=True),
        threading.Thread(target=http_server, name="http", daemon=True),
    ]

    for t in threads:
        t.start()

    # Block main thread until shutdown
    shutdown_event.wait()

    # Give threads time to clean up
    for t in threads:
        t.join(timeout=5.0)

    log.info("Simarine Pico Monitor stopped")


if __name__ == "__main__":
    main()
