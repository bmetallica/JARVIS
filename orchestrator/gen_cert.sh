#!/usr/bin/env bash
# Erzeugt ein selbstsigniertes TLS-Zertifikat für den Orchestrator,
# damit das Browser-Mikrofon (getUserMedia) im LAN funktioniert.
# IP anpassen, falls der Host eine andere Adresse hat.
set -e
cd "$(dirname "$0")"
IP="${1:-192.168.66.224}"
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout certs/key.pem -out certs/cert.pem -days 825 \
  -subj "/CN=${IP}" \
  -addext "subjectAltName=IP:${IP},DNS:localhost,IP:127.0.0.1"
echo "Zertifikat für ${IP} erstellt in certs/."
