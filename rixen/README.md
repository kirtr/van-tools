# rixen-monitor

Edge collector for Rixen/Espar WiFi controller on intermittent AP network.

## What it does

- Watches dedicated USB WiFi interface (`wlx9cefd5f9631f`) for association to `Rixen000000`
- Probes controller at `http://10.10.10.10` only when network gates pass
- Exposes local API on `:8081` (`/`, `/health`, `/history`)
- Logs snapshots to SQLite (`/var/lib/rixen-monitor/rixen.db`)
- Tracks rough fuel estimate from inferred burner runtime (assumed gph configurable)

## Endpoints

- `GET /` current state
- `GET /health` service + source state
- `GET /history?hours=12` buffered history

## Service

Install unit:

```bash
sudo cp rixen-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rixen-monitor
```
