# ISS Watch

Raspberry Pi Kiosk, der den **SEN 4K ISS-Live-Stream** dauerhaft auf einem HDMI-Display anzeigt.  
Zwischen 01:00 und 06:00 Uhr wird der Bildschirm automatisch abgeschaltet.

---

## Wie es funktioniert

### Normalbetrieb

`iss_watch.py` startet **mpv** im Vollbild mit dem SEN-YouTube-Live-Stream.  
Alle 60 Sekunden wird ein Screenshot gemacht und ausgewertet:

- **Badge-Erkennung:** Die farbige Badge oben links im Bild zeigt an ob SEN live, im Replay oder im Trailer-Modus ist — wird nur geloggt, kein Umschalten.
- **Frozen-Frame-Erkennung:** Zwei aufeinanderfolgende Frames werden pixelweise verglichen. Ist die mittlere Differenz unter 1,5 (von 255), gilt das Bild als eingefroren.

### Nachtmodus (01:00–06:00 Uhr)

Der Bildschirm wird per `vcgencmd display_power 0` komplett abgeschaltet (HDMI-Signal weg), mpv wird beendet. Um 06:00 Uhr schaltet sich der Bildschirm wieder an und der Stream startet automatisch neu.

> **Warum `vcgencmd`?** Auf dem Raspberry Pi 3 B+ steht die DPMS-Extension (xset dpms) nicht zur Verfügung. `vcgencmd display_power` ist der einzige zuverlässige Weg den HDMI-Ausgang komplett auszuschalten.

### Watchdog (alle 30 Sekunden)

| Situation | Reaktion |
|-----------|----------|
| mpv-Prozess tot | mpv komplett neu starten |
| Kein Playback-Fortschritt (3× in Folge) | Stream in mpv neu laden (`loadfile`) |

### Frozen-Frame-Recovery (stufenweise Eskalation)

Wenn 4 aufeinanderfolgende Screenshots als eingefroren erkannt werden:

| Stufe | Aktion |
|-------|--------|
| **Level 1** | `mpv.reload()` — Stream-URL in laufendem mpv neu laden |
| **Level 2** | `mpv.start()` — mpv komplett killen und neu starten |
| **Level 3** | `sys.exit(1)` — Script beendet sich, **systemd startet es automatisch neu** |

Sobald wieder ein nicht-eingefrorener Frame erkannt wird, wird der Recovery-Level auf 0 zurückgesetzt.

### WLAN deaktiviert

Der Pi ist per LAN angeschlossen. Beim Start wird WLAN per `nmcli` und `rfkill` deaktiviert und `nm-applet`/`polkit` werden beendet, um lästige Passwort-Popups dauerhaft zu verhindern.

---

## Logging

Alle Ereignisse werden in `iss-watch.log` (neben dem Skript) protokolliert und automatisch rotiert (max. ~16 MB, 7 Dateien à 2 MB).

```
2026-08-20 14:20:39 | INFO    | EVENT  [screen    ] Bildschirm AN
2026-08-20 14:20:43 | INFO    | EVENT  [mpv       ] Neustart #1 — https://www.youtube.com/@sen/live
2026-08-20 14:21:08 | INFO    | EVENT  [frozen    ] Frame-Diff 39.77 (Schwelle 1.5)
2026-08-20 14:21:08 | INFO    | EVENT  [badge     ] SEN-Modus: replay
2026-08-20 15:20:43 | INFO    | HEARTBEAT | mpv läuft: True | Restarts: 1 | Frozen-Count: 0
2026-08-20 01:00:xx | INFO    | EVENT  [screen    ] Bildschirm AUS
2026-08-20 06:00:xx | INFO    | EVENT  [screen    ] Bildschirm AN
```

Protokolliert wird:
- Bildschirm an/aus (Nachtmodus)
- mpv-Starts und Neustarts (nummeriert)
- Frame-Differenz bei jedem Screenshot-Check
- Badge-Farbe (live / replay / trailer / unknown)
- Watchdog-Ereignisse (kein Fortschritt, Prozess tot)
- Frozen-Frame-Eskalation (Level 1–3)
- Netzwerkstatus
- Stündlicher Heartbeat

**Live-Log:**
```bash
tail -f ~/iss/iss-watch.log
```

---

## Cookie-Automatisierung

YouTube erfordert eine angemeldete Session für den SEN-Live-Stream.  
Das Skript `update-iss-cookies.sh` exportiert wöchentlich automatisch frische Cookies aus Firefox und deployed sie auf den Pi.

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
- Raspberry Pi 3 B+ oder neuer
- HDMI-Display
- LAN-Verbindung (WLAN wird vom Script deaktiviert)
- X11-Desktop-Session (kein Wayland)

### Software (Pi)

**System-Pakete:**
```bash
sudo apt install -y mpv python3-opencv
```

**yt-dlp:**
```bash
sudo apt install yt-dlp
# oder aktuellste Version:
pip3 install yt-dlp --break-system-packages
```

> `numpy` wird nicht mehr separat benötigt — OpenCV bringt es mit.

---

## Installation

### 1. Dateien auf den Pi kopieren

```
~/iss/
├── iss_watch.py
├── iss-watch.service
└── iss-watch.desktop
```

```bash
scp iss_watch.py iss-watch.service iss-watch.desktop axel@iss:~/iss/
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

Wöchentlichen Cron-Job einrichten (einmalig auf dem Desktop):
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

**Script manuell starten (zum Testen):**
```bash
python3 ~/iss/iss_watch.py
```

---

## Konfiguration

Alle Parameter am Anfang von `iss_watch.py`:

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
| `SEN_STREAM` | `https://youtube.com/@sen/live` | YouTube-URL des SEN-Streams |
| `YT_COOKIES` | `~/projects/iss-watch/yt-cookies.txt` | Pfad zur YouTube-Cookie-Datei |
| `SLEEP_START` | `1` | Nachtmodus ab dieser Stunde (01:00) |
| `SLEEP_END` | `6` | Nachtmodus bis zu dieser Stunde (06:00) |
| `WATCHDOG_INTERVAL` | `30 s` | Takt des Watchdogs |
| `WATCHDOG_MAX_STUCK` | `3` | Aufeinanderfolgende Nicht-Fortschritt-Messungen bis Reload |
| `STARTUP_GRACE` | `90 s` | Schonfrist nach mpv-Neustart (kein Watchdog-Alarm) |
| `BADGE_INTERVAL` | `60 s` | Sekunden zwischen Screenshot-Checks |
| `FROZEN_DIFF` | `1.5` | Pixel-Differenz-Schwelle (0–255); Live-Stream liegt typ. bei >5 |
| `FROZEN_LIMIT` | `4` | Aufeinanderfolgende eingefrorene Frames bis Recovery |
| `LOG_MAX_BYTES` | `2 MB` | Maximale Größe einer Log-Datei |
| `LOG_BACKUP_COUNT` | `7` | Anzahl rotierter Log-Dateien (max. ~16 MB gesamt) |
| `LOG_HEARTBEAT` | `60 min` | Intervall des stündlichen Heartbeat-Eintrags |

---

## Dateien

| Datei | Beschreibung |
|-------|--------------|
| `iss_watch.py` | Hauptskript |
| `iss-watch.service` | systemd User-Service (`Restart=always`, `RestartSec=15`) |
| `iss-watch.desktop` | XDG Autostart für Desktop-Login |
| `update-iss-cookies.sh` | Exportiert Firefox-Cookies und kopiert sie auf den Pi |

---

## Quellen

- SEN 4K ISS-Kamera: [youtube.com/@sen/live](https://www.youtube.com/@sen/live)
