# Baut den Sidecar als eigenständige Windows-Binary (PyInstaller) und legt sie für den
# Tauri-Build ab. Ausführen auf einem WINDOWS-Rechner mit Python + Rust installiert:
#   powershell -ExecutionPolicy Bypass -File build-sidecar.ps1

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Fix für Umlaute in der PowerShell-Ausgabe
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "? Installiere Build-Abhängigkeiten (pyinstaller, websockets)…"
python -m pip install --quiet pyinstaller websockets
if ($LASTEXITCODE -ne 0) { throw "Fehler bei der Installation der Pip-Pakete." }

# Stolperstein: 'typing'-Backport sicher entfernen ohne PowerShell-Absturz
# cmd /c fängt die Python-Warnung ab, sodass $ErrorActionPreference nicht auslöst
cmd /c "python -m pip uninstall -y typing 2>nul"

Write-Host "? Baue Standalone-Binary…"
Remove-Item -Recurse -Force build, dist, *.spec -ErrorAction SilentlyContinue

# PyInstaller ausführen und prüfen, ob es klappt
pyinstaller --onefile --name jarvis-client --clean --noconsole jarvis-client.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller-Build fehlgeschlagen." }

# Ziel-Triple ermitteln (z.B. x86_64-pc-windows-msvc)
$triple = (rustc -vV | Select-String '^host:').ToString().Split(' ')[1]
$dest = Join-Path $PSScriptRoot "..\desktop\src-tauri\sidecar-bin"

New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item "dist\jarvis-client.exe" (Join-Path $dest "jarvis-client-$triple.exe") -Force

Write-Host "? Fertig! Sidecar wurde erfolgreich nach $dest kopiert." -ForegroundColor Green
