# ISS Watch

Raspberry Pi Kiosk für einen HDMI-Bildschirm mit drei Ansichten: **SEN 4K ISS-Live-Stream**,
**Wetter-Dashboard** und **Aus**. Ein Taster schaltet zyklisch weiter, zwischen 01:00 und
06:00 Uhr geht der Bildschirm automatisch aus. Läuft seit November 2026 auf dem
**wetterraspi5** (Raspberry Pi 5, Wayland/labwc) und hat den alten iss-Pi (Pi 3, X11) abgelöst.
Derselbe Code läuft weiter unter X11, dann ohne Wetteransicht.

---

## Ansichten und Taster

| Ansicht | Bildschirm |
|---------|------------|
| ISS | mpv im Vollbild, Ton immer stumm |
| Wetter | Chromium im Kiosk-Modus mit der lokalen Kiosk-Seite des Repos `wetterstation` |
| Aus | HDMI aus, mpv beendet |

- **Start:** immer die Ansicht aus `start_view`, auf dem wetterraspi5 Wetter. Im Nachtmodus Aus.
- **Taster** an **Pin 40 (GPIO21)** und **Pin 39 (GND)**, interner Pull-up, kein Widerstand nötig.
  Pin 1–10 belegt am wetterraspi5 der NVMe-HAT. Jeder Druck schaltet ISS → Wetter → Aus → ISS.
- **Prellen:** Ein Druck bis 500 ms nach dem letzten angenommenen Druck wird verworfen.
  Maßgeblich ist der Zeitpunkt des Drucks, nicht der Verarbeitung, denn ein Wechsel kann
  wegen des mpv-Starts Sekunden dauern.
- **Stream lädt:** Solange der Stream in der Ansicht ISS noch nicht abspielt, nimmt der Taster
  nichts an, höchstens 30 s lang. Ohne Internet hängt man so nicht fest.
- **Geschaltet wird nur von Hand.** Ein automatischer Wechsel ist möglich (`auto_cycle`),
  auf dem wetterraspi5 aber aus.
- **Nachtmodus:** Um 01:00 auf Aus, um 06:00 zurück zur vorherigen Ansicht. Ein Tastendruck in
  der Nacht hebt den Nachtmodus für diese Nacht auf, die gewählte Ansicht bleibt dann auch nach 06:00.
- mpv und Chromium laufen in beiden Ansichten nebeneinander, `wlrctl` holt das Fenster nach
  vorn. mpv dekodiert verdeckt weiter, deshalb schaltet es sofort um.

---

## Wie es funktioniert

### Normalbetrieb

`iss_watch.py` startet **mpv** im Vollbild mit dem SEN-YouTube-Live-Stream, `kiosk.py` steuert die Ansichten.  
Alle 60 Sekunden wird ein Screenshot gemacht und ausgewertet:

- **Badge-Erkennung:** Die farbige Badge oben links im Bild zeigt an ob SEN live, im Replay oder im Trailer-Modus ist — wird nur geloggt, kein Umschalten.
- **Frozen-Frame-Erkennung:** Zwei aufeinanderfolgende Frames werden pixelweise verglichen. Ist die mittlere Differenz unter 1,5 (von 255), gilt das Bild als eingefroren.

### Nachtmodus (01:00–06:00 Uhr)

Der HDMI-Ausgang wird abgeschaltet, mpv wird beendet: unter Wayland per `wlr-randr --output … --off`, beim Einschalten mit `--transform`, damit die Drehung des Bildschirms erhalten bleibt, unter X11 per `vcgencmd display_power`.

> **Warum `vcgencmd` unter X11?** Auf dem Raspberry Pi 3 B+ steht die DPMS-Extension (xset dpms) nicht zur Verfügung.

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

> **Bekannter Punkt (Stand 06.11.2026):** Auf der Nachtseite der Erde zeigt der Stream fast nur schwarz. Die Frame-Differenz fällt dann unter 1,5, obwohl das Bild nicht eingefroren ist: 280 von 1387 Messungen lagen unter der Schwelle. Das löst unnötige Reloads aus und kann bis zu Level 3 eskalieren, zuletzt am 05.11. um 11:00. Dabei startet das Skript neu und springt auf die Startansicht. Die Erkennung sollte künftig das Abspielen selbst prüfen oder Nachtbilder ausnehmen.

### WLAN

Mit `disable_wifi = true` wird WLAN beim Start per `nmcli` und `rfkill` abgeschaltet, um Passwort-Popups zu verhindern (alter iss-Pi). Auf dem wetterraspi5 ist WLAN die Reserve neben LAN und bleibt an.

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

Auf dem alten iss-Pi brauchte YouTube eine angemeldete Session. Auf dem wetterraspi5 läuft der Stream bisher auch ohne Cookies. `update-iss-cookies.sh` exportiert bei Bedarf frische Cookies aus Firefox und kopiert sie auf den wetterraspi5.

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
- Raspberry Pi 5 mit Wayland (labwc), oder Raspberry Pi 3 B+ mit X11 (dann ohne Wetteransicht)
- HDMI-Display
- LAN-Verbindung
- optional Taster gegen GND

### Software (Pi)

**System-Pakete:**
```bash
sudo apt install -y mpv python3-opencv python3-gpiozero python3-lgpio chromium wlrctl wlr-randr
```

**yt-dlp** im eigenen venv, die Debian-Version veraltet schnell:
```bash
python3 -m venv ~/.local/share/iss-watch/venv
~/.local/share/iss-watch/venv/bin/pip install -U yt-dlp
```

> `numpy` wird nicht mehr separat benötigt — OpenCV bringt es mit.

---

## Installation

### 1. Dateien auf den Pi kopieren

Auf dem wetterraspi5 erledigt das `deploy/bootstrap-pi.sh` im Repo `wetterstation`. Von Hand:

```bash
scp iss_watch.py kiosk.py wetterraspi5:iss/
scp config.example.toml wetterraspi5:.config/iss-watch/config.toml
```

### 2. Systemd User-Service einrichten

```bash
mkdir -p ~/.config/systemd/user
cp iss-watch.service ~/.config/systemd/user/
systemctl --user daemon-reload
```

### 3. Autostart bei Desktop-Login

```bash
mkdir -p ~/.config/autostart
cp iss-watch.desktop ~/.config/autostart/
```

Der Eintrag übergibt `WAYLAND_DISPLAY` an den User-Service und startet ihn. Unter labwc gehört das
**nicht** nach `~/.config/labwc/autostart`: Diese Datei würde die Systemdatei ersetzen, dann
starten kanshi (Bildschirmdrehung) und das Panel nicht mehr.

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

### 5. yt-dlp aktuell halten (empfohlen)

```bash
(crontab -l 2>/dev/null; echo "0 4 * * *  ~/.local/share/iss-watch/venv/bin/pip install -qU yt-dlp") | crontab -
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

`~/.config/iss-watch/config.toml`, alle Werte optional, Vorlage: `config.example.toml`.

| Option | Standard | Beschreibung |
|--------|----------|--------------|
| `dashboard_url` | leer | Wetter-Dashboard; leer = keine Wetteransicht |
| `start_view` | `iss` | Ansicht nach jedem Start: `iss`, `weather` oder `off` |
| `auto_cycle`, `cycle` | `false` | automatischer Wechsel mit Dauern in Sekunden |
| `button_gpio` | `0` | GPIO des Tasters, 0 = kein Taster |
| `disable_wifi` | `true` | WLAN beim Start abschalten |
| `display_backend` | `auto` | `auto`, `vcgencmd` oder `wlr-randr` |
| `output`, `output_transform` | `HDMI-A-1`, `normal` | Ausgang und Drehung (`180` am wetterraspi5) |
| `sleep_start`, `sleep_end` | `1`, `6` | Nachtmodus |
| `stream_url`, `cookies`, `ytdl_path` | SEN, Cookie-Pfad, leer | Stream und yt-dlp |

Am Anfang von `iss_watch.py`:

| Variable | Standard | Beschreibung |
|----------|----------|--------------|
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
| `iss_watch.py` | Hauptskript: mpv, Watchdog, Frozen-Frame-Erkennung, Hauptschleife |
| `kiosk.py` | Konfiguration, Display-Adapter, Browser, Zustandsautomat der Ansichten |
| `config.example.toml` | Vorlage für die Konfiguration (Werte des wetterraspi5) |
| `tests/test_kiosk.py` | Tests des Zustandsautomaten ohne Hardware |
| `iss-watch.service` | systemd User-Service (`Restart=always`, `RestartSec=15`) |
| `iss-watch.desktop` | XDG Autostart für Desktop-Login |
| `update-iss-cookies.sh` | Exportiert Firefox-Cookies und kopiert sie auf den wetterraspi5 |

---

## Quellen

- SEN 4K ISS-Kamera: [youtube.com/@sen/live](https://www.youtube.com/@sen/live)
