# Deutsche TTS als Docker-Container (OS-unabhängig)

Diese Variante lässt das deutsche Kokoro-Modell (**Martin / Victoria / Eva**) in
einem Linux-Container laufen. Vorteil: **espeak-ng und alle Abhängigkeiten leben
im Container** — dein Windows bleibt sauber, kein .msi, keine ENV-Variablen.

MARK XL spricht den Container nur per HTTP an (OpenAI-kompatibler
`/v1/audio/speech`-Endpunkt) — genau wie EdgeTTS/ElevenLabs.

## Voraussetzungen
- **Docker Desktop** (mit WSL2-Backend) auf Windows installiert und gestartet.

## Starten
```powershell
cd docker/german-tts
docker compose up -d --build
```
Der erste Build lädt das Image + Modell (einige Minuten, je nach Verbindung).
Prüfen, ob er läuft:
```powershell
docker compose logs -f
```

## In MARK XL einstellen
Configure → **TEXT-TO-SPEECH** → **🐳 DE-Server** wählen:
- **Server-URL:** `http://localhost:8080`
- **Voice:** `dm_martin` (männlich) · `df_victoria` (weiblich) · `df_eva` (weiblich)

→ **Apply Changes**. Fertig — deutsche Sprachausgabe, voll lokal, kein espeak-ng auf dem Host.

## Schnelltest (ohne App)
```powershell
curl.exe -s -X POST http://localhost:8080/v1/audio/speech `
  -H "Content-Type: application/json" `
  -d '{"model":"kokoro","input":"Hallo, ich bin Jarvis.","voice":"dm_martin","response_format":"wav"}' `
  --output test.wav
```
Wenn `test.wav` abspielbar ist, funktioniert der Container.

## Stoppen
```powershell
docker compose down
```

## Hinweise
- **GPU:** Auf Windows ist GPU-Durchreichung an Docker fummelig (WSL2 +
  nvidia-container-toolkit). CPU reicht für Kokoro aber gut aus. Optionaler
  GPU-Block ist in `docker-compose.yml` auskommentiert.
- **Port belegt?** Ändere in `docker-compose.yml` die linke Seite von
  `"8080:8080"` (z. B. `"8123:8080"`) und trage in MARK XL `http://localhost:8123` ein.
- **Falls der Build fehlschlägt** (Upstream-Repo geändert), klone manuell:
  ```powershell
  git clone https://github.com/Godelaune/Kokoro-82M-ONNX-German-Martin
  cd Kokoro-82M-ONNX-German-Martin
  docker compose up -d
  ```
  und nutze die dort dokumentierte Port-/Endpunkt-Konfiguration.
