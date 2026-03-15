# van-tools

Monitoring and automation tools for the van build.

## simarine/ — Pico UDP Monitor

Real-time monitor for the Simarine Pico battery/tank monitoring system. Listens for UDP broadcast packets, decodes sensor data, serves a JSON HTTP API, and logs to SQLite.

### Hardware

- **Simarine Pico** rev.1 (fw 2.2.0.001) — main display unit
- **SC303 shunt module** (ID 3486) — battery voltage, current, SOC
- **ST107 tank module** (ID 8251) — water tank level (currently offline)
- 3×175W solar panels → Victron MPPT
- Sterling B2B 120A charger
- 20-gal water tank

### How It Works

The Pico broadcasts 219-byte UDP packets on port 43210 every ~1 second. Each packet has a 14-byte header followed by repeating 7-byte sensor fields:

```
[field_nr] [0x01 type] [a_hi] [a_lo] [b_hi] [b_lo] [0xff sep]
```

Values `a` and `b` are unsigned 16-bit big-endian integers. The sensor map decodes known elements:

| Element | Sensor | Decode | Unit |
|---------|--------|--------|------|
| 5 | Pico internal voltage | b / 1000 | V |
| 14 | House battery voltage (shunt) | b / 1000 | V |
| 15 | Water tank resistance | raw b | Ω |
| 26 | House battery SOC | a / 160 | % |
| 28 | House battery voltage (dup) | b / 1000 | V |
| 3 | Barometric pressure | raw b | raw |

Unknown elements are included as raw values in the API output for identification.

### Installation

```bash
# Clone onto the Pi
git clone git@github.com:kirtr/van-tools.git
cd van-tools

# Install systemd service
sudo cp simarine/simarine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now simarine
```

### Configuration

Environment variables (set in the service file or override):

| Variable | Default | Description |
|----------|---------|-------------|
| `UDP_PORT` | `43210` | Pico UDP broadcast port |
| `HTTP_PORT` | `8080` | HTTP API listen port |
| `LOG_INTERVAL_S` | `60` | SQLite write interval (seconds) |
| `DB_PATH` | `simarine.db` | SQLite database file path |

### API

#### `GET /` — Current State

Returns the latest decoded sensor values plus all raw element data.

```json
{
  "timestamp": 1710000000.0,
  "iso_time": "2025-03-09T12:00:00Z",
  "packet_count": 4200,
  "decoded": {
    "house_battery_voltage": {
      "value": 13.21,
      "unit": "V",
      "element": 14,
      "raw_a": 0,
      "raw_b": 13210,
      "description": "SC303 ch2 house battery voltage at shunt"
    },
    "house_battery_soc": {
      "value": 98.1,
      "unit": "%",
      "element": 26,
      "raw_a": 15696,
      "raw_b": 0,
      "description": "House battery state of charge"
    }
  },
  "raw_elements": {
    "0": {"a": 123, "b": 456},
    "5": {"a": 0, "b": 12340}
  }
}
```

#### `GET /history?hours=24` — Historical Readings

Returns logged readings from the past N hours.

```json
{
  "hours": 24,
  "count": 1440,
  "readings": [...]
}
```

#### `GET /health` — Health Check

```json
{"status": "ok"}
```

### Architecture

```
┌─────────────┐    UDP :43210     ┌──────────────────┐
│ Simarine Pico│ ─── broadcast ──→ │  monitor.py       │
└─────────────┘                   │                    │
                                  │  ┌─ udp_listener   │
                                  │  │  parse + decode  │
                                  │  │                  │
                                  │  ├─ http_server     │──→ JSON API :8080
                                  │  │                  │
                                  │  └─ db_logger       │──→ simarine.db (SQLite WAL)
                                  └──────────────────────┘
```

Three threads: UDP listener updates shared state, HTTP server reads it, DB logger snapshots it every 60s. SQLite uses WAL mode and NORMAL sync for SD card longevity.

### Development

```bash
# Run locally (needs UDP packets or a test harness)
cd simarine
python3 monitor.py

# Check logs
journalctl -u simarine -f
```

### Extending the Sensor Map

Edit `sensor_map.py` to add new elements as you identify them from the raw API output. Each entry maps an element ID to a name, unit, decode function, and which field (a or b) to use.
