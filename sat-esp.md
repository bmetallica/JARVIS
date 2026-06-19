# Satellit — ESP32-S3 Firmware (sat-esp)

> Vollständiger Plan für den ESP32-Sprachsatelliten von SH-Jarvis auf dem
> **Waveshare ESP32-S3-AUDIO-Board** + dem nötigen Audio-Stream-Endpoint im Orchestrator.
> Verwandt: [`sat-pi.md`](./sat-pi.md), [`aktuellerstand.md`](./aktuellerstand.md).
> Quellen Board: Waveshare Wiki „ESP32-S3-AUDIO-Board", CNX-Software, Amazon B0FP1VL37J.

## 1. Zielbild
Günstiger Dauerläufer-„Smart-Speaker": lokales Wake-Word „Jarvis", danach Audio **streamen** zum
Orchestrator (STT/LLM/TTS/Identität server-seitig), Antwort über den Lautsprecher, RGB-LEDs als Statusanzeige,
voller Rückkanal (Timer/Benachrichtigungen/Lautstärke). Voll in den Agenten integriert wie ein Pi-Satellit.

## 2. Board (Waveshare ESP32-S3-AUDIO-Board)
- **ESP32-S3R8**, Dual-Core LX7 @240 MHz, 512 KB SRAM, **8 MB PSRAM**, **16 MB Flash**, WiFi 2.4 GHz b/g/n + BLE5.
- **ES7210** 4-Kanal-Audio-ADC (Dual-Mic-Array, **Noise Reduction + Echo Cancellation** in Hardware/AFE).
- **ES8311** Audio-DAC → Onboard-Lautsprecher **mit Verstärker** (⚠️ Übersteuerung vermeiden, siehe §10).
- **7× programmierbare RGB-LEDs** (WS2812-artig, „surround").
- Codecs werden per I²C konfiguriert, Audio über I²S. (Genaue GPIO-Pins aus dem Waveshare-Wiki/Beispielcode übernehmen.)

## 3. Framework-Entscheidung
**ESP-IDF (v5.x) + esp-adf (Codec-Treiber ES8311/ES7210) + esp-sr (AFE: AEC/NS/VAD) + microWakeWord.**
- Vollständige Kontrolle über das eigene WebSocket-Protokoll zum Orchestrator (nötig für Rückkanal/Lautstärke/LEDs/OTA).
- Alternative **ESPHome** wäre schneller, ist aber auf seine eigene (Home-Assistant-/Wyoming-)Pipeline ausgelegt → für die tiefe Eigen-Integration verwerfen wir das.

## 4. Wake-Word „Jarvis"
**microWakeWord** (TFLite-Micro, läuft auf ESP32-S3 mit PSRAM) mit dem offenen **`hey_jarvis`**-Modell — frei, on-device, kein Cloud-Key.
- Alternative: Espressifs **WakeNet** (ESP-SR) — sehr effizient, aber „Jarvis" als Custom-Wort braucht den (kostenpflichtigen) Espressif-Trainingsdienst → daher microWakeWord.
- AFE (esp-sr) liefert davor **AEC** (Echo-Unterdrückung, damit Wake-Word auch **während** der Wiedergabe greift → Barge-In) + Noise-Suppression + VAD.

## 5. Audio-Pipeline
```
ES7210 (Dual-Mic, 16 kHz) → esp-sr AFE (AEC/NS) → microWakeWord
   └─ "Jarvis" → LED listening + kurzer Ton
        └─ VAD schneidet Sprachende → PCM-Frames per WS streamen
Server → TTS-Audio-Frames (PCM 16/24 kHz mono) → I²S → ES8311 → Lautsprecher (LED speaking)
Barge-In: Wake-Word während Wiedergabe (dank AEC) → Wiedergabe stoppen, neu zuhören.
```

## 6. Orchestrator: NEUER Audio-Stream-Endpoint (Server-Arbeit)
Neuer WebSocket **`/ws/satellite`** (ein Socket je Gerät, trägt Steuer-JSON **und** Binär-Audio):
- **ESP → Server:** `hello{device_id,name,fw,token}` · `audio_start{sr,codec}` · *(binäre PCM-Frames)* · `audio_end` · `event{button,…}` · `heartbeat`
- **Server → ESP:** `welcome{session_id}` · `state{listening|thinking|speaking|idle}` · `tts_start{sr,codec}` · *(binäre Audio-Frames)* · `tts_end` · `set_volume{level}` · `led{state|rgb}` · `notify{message}` / `timer_alarm{message}` · `ota{url}`
- Server-Logik je Äußerung: Audio puffern → **STT** (faster-whisper) + **Sprecher-Erkennung** → **`/api/chat`-Pipeline** (Tools, adaptiv) → Antwort **satzweise TTS** → Frames zurückstreamen. Re-nutzt `session_hub` (Identität, Verlauf, Push), `tools`, `biometrics`.
- Damit ist der ESP eine vollwertige Session: Timer-Alarme/Benachrichtigungen werden über denselben Socket gepusht (Rückkanal).
- Audioformat: Start mit **PCM 16 kHz mono** (einfach, ~32 KB/s); später optional **Opus** (esp-adf-Decoder) zur Bandbreitenersparnis.

## 7. WLAN-Einrichtung (wie Tasmota)
**SoftAP-Provisioning** beim ersten Start / nach Factory-Reset:
- ESP spannt AP **`Jarvis-Setup-XXXX`** auf → Captive-Portal (Webformular).
- Eingabe: **WLAN SSID/Passwort**, **Orchestrator-URL**, **Raumname** (z. B. „Wohnzimmer"), optional Pairing-Token.
- Speicherung in **NVS**. LED-Status „SETUP" (z. B. pulsierend blau). Danach Reboot → Normalbetrieb.
- (Optional zusätzlich „Improv-WiFi" über BLE für App-Komfort.)

## 8. LEDs (7× RGB) — Statusbild
| Zustand | LED |
|---|---|
| SETUP/AP | blau pulsierend |
| IDLE | sehr schwaches Atmen (oder aus) |
| LISTENING | grün, „Füllstand"/Ring aktiv |
| THINKING | gelb/cyan rotierend |
| SPEAKING | orange, pulsierend zur Sprache |
| VOLUME | kurze Balken-Anzeige beim Einstellen (n von 7 LEDs) |
| ERROR/Offline | rot |
LED-Zustand kann auch der Server per `led{…}`-Push setzen (volle Integration).

## 9. Rückkanal / Agent-Integration
- Beim Start `hello` → Registrierung als Session `client_type:"satellite"` (taucht später in der Admin-Geräteliste auf, Heartbeat).
- **Timer/Benachrichtigungen** → Push → werden auf genau diesem Gerät gesprochen + LED.
- **Sprecher-Erkennung** greift automatisch (Audio geht an den Server) → Gedächtnis/Rechte pro Person.

## 10. Lautstärke (Sprachbefehl + Schutz des Verstärkers) ⚠️
- Sprachbefehl „**Jarvis, Lautstärke 1–10**" → Server-Tool **`set_device_volume(level)`** → Push `set_volume{level}` an dieses Gerät.
- **Mapping & Schutz (kritisch):** Level 1–10 → ES8311-Gain. **HARTE Obergrenze 90 %** (Level 10 ⇒ 90 %, nie höher — sonst Übersteuerung/Defekt des Verstärkers). **Default nach dem Flashen: 50 %**. In NVS persistiert.
  - Implementierung: zentrale `set_volume(percent)` clampt immer auf `min(percent, 90)`; UI/Voice rechnet Level→Prozent. Auch eine kurze Quittung („Lautstärke 7") + LED-Balken.
- Lokaler Taster (falls vorhanden) für Lauter/Leiser nutzt dieselbe geclampte Funktion.

## 11. Was sonst noch dazugehört (ergänzt)
- **OTA-Updates** (ESP-IDF HTTPS-OTA; Server-Push `ota{url}`); A/B-Partitionen im 16-MB-Flash.
- **Auto-Discovery**: mDNS nach dem Orchestrator suchen ODER URL aus Provisioning; **Reconnect mit Backoff**; Offline-Verhalten.
- **NTP** (für Töne/Logs/lokale Zeit).
- **Heartbeat/Status** an den Orchestrator → Admin-Geräteliste (online/offline, FW-Version, Raum).
- **Factory-Reset** (Taster lang halten) → NVS löschen → zurück in SoftAP.
- **Pairing/Token-Sicherheit** zwischen ESP und Orchestrator (Gerät authentifizieren).
- **Quittungs-/Fehlertöne**, **Mute** (Mikro-Stumm, LED rot-statisch, Datenschutz).
- **Partitionstabelle** (16 MB): app0/app1 (OTA), nvs, spiffs/littlefs (Wake-Word-Modell, Töne).
- **Erststart-Lautstärke 50 %** schon im Default-NVS/Code verankern (vor erstem Ton!).
- **Mehrere Geräte**: eindeutige `device_id` (MAC) + Raumname.
- TLS: selbstsigniertes Orchestrator-Cert im Firmware-Bundle pinnen.

## 12. Build & Flash
- ESP-IDF v5.x, `idf.py set-target esp32s3`, `menuconfig` (PSRAM aktivieren, Partition-Table, esp-sr/AFE).
- Komponenten: `esp-adf` (es8311/es7210), `esp-sr` (AFE), `microWakeWord`-Modell (hey_jarvis), `esp_websocket_client`, `led_strip` (WS2812), `esp_https_ota`, `wifi_provisioning`.
- `idf.py build flash monitor`. Default-Config (Volume 50 %, Wake „jarvis") fest hinterlegt.

## 13. Umsetzungsreihenfolge
1. **Orchestrator:** `/ws/satellite`-Endpoint (Audio-In→STT→Chat→TTS-Out) + `set_device_volume`-Tool/Push. (Lässt sich vorab mit einem PC-Skript testen, das Audio streamt.)
2. ESP-Grundgerüst: WLAN-Provisioning (SoftAP) + WS-Verbindung + `hello`/`heartbeat` + LED-Zustände.
3. Audio: ES7210-Aufnahme + AFE + microWakeWord „Jarvis" + Streaming; ES8311-Wiedergabe der TTS-Frames.
4. Rückkanal (Timer/notify), Lautstärke-Sprachbefehl (mit 90 %-Cap/50 %-Default).
5. OTA, Factory-Reset, Töne, Feinschliff. Fertiges, flashbares Image + Kurzanleitung.
