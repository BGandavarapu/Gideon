# Running the App

## Quick Start

### Windows (PowerShell) — recommended
```powershell
.\scripts\start_app.ps1
```

### Mac / Linux
```bash
./scripts/start_app.sh
```

The start scripts automatically kill any stale process on port 5001
before launching Flask, preventing the most common source of bugs
(code changes appearing not to take effect after a restart).

---

## What the Start Scripts Do

1. Find every PID listening on port 5001 via `netstat` / `lsof`.
2. Force-kill each stale PID and wait 1 second for the port to release.
3. `cd` to the project root and run `python web/app.py`.

---

## Manual Start (if scripts don't work)

Kill stale processes first, then start Flask.

**Windows PowerShell:**
```powershell
# Find stale PIDs
netstat -ano | Select-String ":5001"

# Kill each PID listed (replace XXXX with actual PID)
Stop-Process -Id XXXX -Force

# Start Flask
python web/app.py
```

**Mac / Linux:**
```bash
lsof -ti :5001 | xargs kill -9
python web/app.py
```

---

## Verify the Running Process

```bash
curl http://localhost:5001/api/health
```

Expected response:
```json
{
  "pid": 24740,
  "port": 5001,
  "started_at": "2026-03-31T23:14:00",
  "status": "ok",
  "uptime_seconds": 42
}
```

- **`pid`** — the OS process ID of the running Flask server.
- **`uptime_seconds`** — seconds since the process started.

If `pid` is the same after a supposed restart → the stale process
is still running. Kill it and use the start script.

---

## Warning Signs of Stale Processes

| Symptom | Likely Cause |
|---|---|
| Code changes don't take effect after restart | Old process still serving |
| `is_sample`/`domain` missing from API | Stale process serving old route |
| Multiple PIDs from `netstat -ano \| findstr :5001` | Multiple Flask processes |
| `uptime_seconds` is very large after "restart" | Restart didn't kill old process |

---

## Checking for Multiple Processes

**Windows:**
```powershell
netstat -ano | Select-String ":5001\s"
```
Should show **one** LISTENING PID. If you see more than one, use the
start script to clean up.

**Mac / Linux:**
```bash
lsof -i :5001
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5001` | Port Flask listens on |
| `DATABASE_URL` | `sqlite:///data/jobs.db` | SQLAlchemy connection string |
| `DATABASE_PATH` | — | SQLite path shorthand (alternative to DATABASE_URL) |

Override the port:
```powershell
$env:PORT = 5000; .\scripts\start_app.ps1
```
```bash
PORT=5000 ./scripts/start_app.sh
```
