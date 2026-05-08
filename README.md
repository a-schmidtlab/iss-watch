# ISS Watch

Raspberry Pi Kiosk, der die **Sen 4K ISS-Kamera** als Live-Stream anzeigt.  
Wenn SEN nicht im Live-Modus ist (Replay, Trailer oder Ausfall), wechselt das System automatisch auf ein aktuelles **EUMETSAT Meteosat Satellitenbild** (Europa).

---

## Wie es funktioniert

### Normalbetrieb

`iss_watch.py` startet **mpv** im Vollbild mit dem SEN-YouTube-Live-Stream.  
Alle 30 Sekunden wird ein Screenshot des laufenden Bildes gemacht und die Badge-Farbe oben links analysiert:

| Badge | Farbe | Aktion |
|-------|-------|--------|
| **Live** | Rot | SEN-Stream weiter anzeigen |
| **Replay** | Orange | → Wettersatellit |
| **Trailer** | Türkis | → Wettersatellit |
| Nicht erkennbar (6×) | — | → Wettersatellit |

### Wettersatellit-Fallback

Sobald SEN nicht live ist, wird ein aktuelles Satellitenbild direkt vom  
EUMETSAT-Betreiber geladen (`eumetview.eumetsat.int`):

- **Tagsüber (7–19 Uhr):** `EUMETSAT_MSG_VIS006Color_CentralEurope.jpg` — sichtbares Licht
- **Nachts:** `EUMETSAT_MSGIODC_IR039Color_Europe.jpg` — Infrarot

Das Bild wird alle 15 Minuten aktualisiert.  
Alle 5 Minuten wird geprüft, ob SEN wieder live ist.

### Watchdog

Alle 30 Sekunden prüft der Watchdog ob mpv noch Playback liefert.  
Bei Absturz oder eingefrorenem Bild (3× kein Fortschritt) wird der Stream  
automatisch neu geladen.

### WLAN deaktiviert

Der Pi ist per LAN angeschlossen. Beim Start wird WLAN per `nmcli` und `rfkill`  
deaktiviert, um nm-applet-Passwort-Popups dauerhaft zu verhindern.

---

## Logging

Alle Ereignisse werden in `iss-watch.log` (neben dem Skript) protokolliert  
und automatisch rotiert (max. ~16 MB):

```
2026-05-08 14:15:33 | INFO    | ANZEIGE START | SEN Live-Stream
2026-05-08 14:15:33 | INFO    | ANZEIGE ENDE  | SEN Live-Stream       | Dauer: 1h32m22s
2026-05-08 14:15:33 | INFO    | ANZEIGE START | EUMETSAT Wetter-Satellit  ← SEN-Badge: replay
2026-05-08 15:00:00 | INFO    | HEARTBEAT     | Modus: weather | ...
```

Protokolliert wird u. a.:
- Anzeigedauer jedes Inhalts (SEN Live, Wetter, Ladezeit/Desktop sichtbar)
- SEN Badge-Farben (zum Tuning der Erkennung)
- Watchdog-Ereignisse (eingefroren, IPC-Fehler, Neustart)
- Netzwerkausfälle und EUMETSAT Download-Fehler
- Stündlicher Heartbeat

**Live-Log:**
```bash
tail -f ~/iss/iss-watch.log
```

---

## Cookie-Automatisierung

YouTube erfordert eine angemeldete Session für den SEN-Live-Stream.  
Das Skript `update-iss-cookies.sh` exportiert wöchentlich automatisch  
frische Cookies aus Firefox und deployed sie auf den Pi.

**Einmalige Voraussetzung:** In Firefox bei YouTube/Google eingeloggt sein.

**Manueller Aufruf:**
```bash
./update-iss-cookies.sh
```

**Automatisch per Cron (jeden Montag 03:00 Uhr)** — wird bei Installation eingerichtet.

**Cookie-Log:**
```bash
cat ~/.local/share/iss-watch-cookies.log
```

---

## Voraussetzungen

### Hardware
- Raspberry Pi 3 oder neuer (64-bit Raspberry Pi OS)
- HDMI-Display
- LAN-Verbindung (WLAN wird deaktiviert)
- X11-Desktop-Session

### Software (Pi)

**System-Pakete:**
```bash
sudo apt install -y mpv ffmpeg python3-pip python3-opencv fonts-liberation
```

**Python-Pakete:**
```bash
pip3 install numpy --break-system-packages
```

**yt-dlp:**
```bash
sudo apt install yt-dlp
# oder aktuellste Version:
pip3 install yt-dlp --break-system-packages
```

---

## Installation

### 1. Dateien auf den Pi kopieren

```
~/iss/
├── iss_watch.py
├── iss-watch.service
└── iss-watch.desktop
```

### 2. Systemd User-Service einrichten

```bash
mkdir -p ~/.config/systemd/user
cp ~/iss/iss-watch.service ~/.config/systemd/user/

loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user enable iss-watch
systemctl --user start iss-watch
```

### 3. Autostart bei Desktop-Login

```bash
mkdir -p ~/.config/autostart
cp ~/iss/iss-watch.desktop ~/.config/autostart/
```

### 4. YouTube-Cookies einrichten (einmalig)

Auf dem Desktop-Rechner — Firefox muss bei YouTube eingeloggt sein:

```bash
./update-iss-cookies.sh
```

Dann den wöchentlichen Cron-Job einrichten (einmalig auf dem Desktop):
```bash
SCRIPT="$(pwd)/update-iss-cookies.sh"
(crontab -l 2>/dev/null; echo "0 3 * * 1  $SCRIPT >> /dev/null 2>&1") | crontab -
```

### 5. WLAN permanent deaktivieren (optional, empfohlen)

```bash
echo "dtoverlay=disable-wifi" | sudo tee -a /boot/firmware/config.txt
```

### 6. yt-dlp täglich aktualisieren (empfohlen)

```bash
(crontab -l 2>/dev/null; echo "0 4 * * *  yt-dlp -U") | crontab -
```

---

## Betrieb

**Service-Status:**
```bash
systemctl --user status iss-watch
```

**Neustart:**
```bash
systemctl --user restart iss-watch
```

**Live-Log:**
```bash
tail -f ~/iss/iss-watch.log
```

---

## Konfiguration

Alle Parameter am Anfang von `iss_watch.py`:

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `SEN_STREAM` | `@sen/live` | YouTube-URL des SEN-Streams |
| `WEATHER_DAY_URL` | EUMETSAT VIS006 | Satellitenbild tagsüber |
| `WEATHER_NIGHT_URL` | EUMETSAT IR039 | Satellitenbild nachts |
| `WEATHER_REFRESH` | 15 min | Aktualisierungsintervall Wetterbild |
| `SEN_MODE_INTERVAL` | 30 s | Wie oft SEN-Badge geprüft wird |
| `SEN_FALLBACK_RETRY` | 5 min | Wie oft auf SEN-Rückkehr geprüft wird |
| `SEN_UNKNOWN_LIMIT` | 6 | Unlesbare Badges bis Fallback |
| `WATCHDOG_INTERVAL` | 30 s | Watchdog-Takt |
| `WATCHDOG_MAX_STUCK` | 3 | Eingefrorene Frames bis Neustart |
| `LOG_BACKUP_COUNT` | 7 | Anzahl rotierter Log-Dateien |

---

## Dateien

| Datei | Beschreibung |
|-------|--------------|
| `iss_watch.py` | Hauptskript |
| `iss-watch.service` | systemd User-Service |
| `iss-watch.desktop` | XDG Autostart |
| `update-iss-cookies.sh` | Automatisches Cookie-Update (Desktop → Pi) |

---

## Quellen

- SEN 4K ISS-Kamera: [youtube.com/@sen/live](https://www.youtube.com/@sen/live)
- EUMETSAT Satellitenbilder: [eumetview.eumetsat.int](https://eumetview.eumetsat.int/static-images/latestImages/)
