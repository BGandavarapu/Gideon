# Resume Auto-Tailor — clean start script (Windows PowerShell)
# Kills any process using port 5001 before starting Flask.
# Usage: .\scripts\start_app.ps1
#        PORT=5000 .\scripts\start_app.ps1   (if you need a different port)

$port = if ($env:PORT) { [int]$env:PORT } else { 5001 }

Write-Host "Checking for processes on port $port..." -ForegroundColor Cyan

# netstat -ano lists  Proto  LocalAddr  ForeignAddr  State  PID
# We match lines whose local address ends with :<port> followed by whitespace.
$stalePids = netstat -ano |
    Select-String "\:$port\s" |
    ForEach-Object {
        # The PID is always the last whitespace-delimited token on the line.
        ($_.Line.Trim() -split '\s+')[-1]
    } |
    Where-Object { $_ -match '^\d+$' -and [int]$_ -gt 0 } |
    Sort-Object -Unique

if ($stalePids) {
    Write-Host "Found stale process(es): $($stalePids -join ', ')" -ForegroundColor Yellow
    foreach ($stalePid in $stalePids) {
        try {
            Stop-Process -Id ([int]$stalePid) -Force -ErrorAction Stop
            Write-Host "  Killed PID $stalePid" -ForegroundColor Green
        } catch {
            Write-Host "  Could not kill PID $stalePid : $_" -ForegroundColor Red
        }
    }
    # Give the OS a moment to release the port binding.
    Start-Sleep -Seconds 1
} else {
    Write-Host "No stale processes found on port $port" -ForegroundColor Green
}

Write-Host "Starting Flask app on port $port..." -ForegroundColor Cyan
Set-Location (Split-Path $PSScriptRoot -Parent)
python web/app.py
