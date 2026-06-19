#!/usr/bin/env bash
# Startet den SH-Mark-XL Orchestrator (Tier 1).
# Port 8088, weil 8000 auf diesem Host belegt ist.
# Nutzt automatisch HTTPS, wenn certs/cert.pem + certs/key.pem existieren —
# nötig, damit das Browser-Mikrofon (getUserMedia) im LAN funktioniert
# (Secure Context).  Cert erzeugen:  ./gen_cert.sh
set -e
cd "$(dirname "$0")"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8088}"

if [[ -f certs/cert.pem && -f certs/key.pem ]]; then
    echo "Orchestrator → https://${HOST}:${PORT}  (HTTPS — Mikrofon aktiv)"
    exec uvicorn app:app --host "$HOST" --port "$PORT" \
        --ssl-keyfile certs/key.pem --ssl-certfile certs/cert.pem "$@"
else
    echo "Orchestrator → http://${HOST}:${PORT}  (kein Cert → Mikrofon im LAN evtl. blockiert; ./gen_cert.sh)"
    exec uvicorn app:app --host "$HOST" --port "$PORT" "$@"
fi
