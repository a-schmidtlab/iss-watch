# ISS Watch

A Raspberry Pi kiosk that displays the NASA ISS live Earth camera 24/7. When the ISS loses contact with ground stations (Loss of Signal / LOS), it automatically switches to the Sen.com 4K ISS camera and shows a countdown to the next expected live signal, calculated from real orbital data.

![Flow diagram](https://i.imgur.com/placeholder.png)

---

## How It Works

### Normal Operation
`iss_watch.py` launches **mpv** fullscreen with the NASA "Live High-Definition Views from the ISS" YouTube stream. Every 15 seconds it grabs a frame from the running video and analyzes it for the NASA LOS screen.

### Loss of Signal Detection
The NASA LOS screen shows large white text on a dark background. The script crops the upper-left 70% × 65% of the frame (where the text appears) and counts the ratio of very bright pixels (RGB > 220 in all channels). If more than 8% of that region is bright white, LOS is declared — but only after **two consecutive detections** to prevent false positives from bright cloud cover.

```
NASA stream → grab frame every 15s → analyze brightness ratio
                                           │
                              > 8% white pixels?
                              ├─ No  → keep watching
                              └─ Yes (×2) → switch to Sen
```

### Fallback to Sen Camera
On confirmed LOS, mpv loads the **Sen.com** YouTube live stream, which provides a separate 4K camera mounted on the ISS exterior. Sen has its own independent downlink and is available ~20+ hours/day.

### AOS Prediction via Orbital Mechanics
While on Sen, the script calculates the ISS position every 15 seconds using:

- **TLE data** (Two-Line Elements) fetched from [Celestrak](https://celestrak.org) every 6 hours — these describe the ISS orbit with high precision
- **sgp4** propagator — standard orbital mechanics library used by NASA/NORAD
- **TDRS satellite positions** — the three NASA Tracking and Data Relay Satellites in geostationary orbit that provide the ISS with its communication link

The script computes the angular separation between the ISS and each TDRS satellite. When the ISS is within the geometric visibility cone of at least one TDRS, signal is theoretically possible. The time until that next window is displayed as a countdown overlay on the Sen stream.

```
ISS position (sgp4) ──→ angular distance to TDRS East/West/Spare
                                    │
                         < MAX_ANGLE (~101°)?
                         ├─ Yes → AOS window
                         └─ No  → minutes until next window → countdown
```

### AOS Verification
When the TLE prediction says AOS is less than 2 minutes away, the script switches to active verification: **ffmpeg** grabs a single frame directly from the NASA HLS stream (bypassing mpv entirely) and runs the same brightness analysis. This is more reliable than the TLE estimate alone, since NASA doesn't always go live immediately at the theoretical AOS. The ffmpeg check only runs in this narrow window, keeping CPU load minimal on the Pi 3.

```
TLE: AOS < 2 min → ffmpeg grab NASA frame → brightness check
                         │
                    No LOS text?
                    ├─ Yes → switch back to NASA
                    └─ No  → wait, show "restoring signal..."
```

### URL Management
YouTube HLS URLs expire after ~2 hours. The `URLCache` class handles this transparently:

- The NASA ISS Earth camera has a stable YouTube video ID (`zPH5KtjJFaQ`) — used as the primary source
- Every 2 hours, the script verifies the ID is still active
- If the stream is ever replaced, it falls back to querying the NASA channel for a stream matching "High-Definition Views"
- The raw HLS URL (needed for ffmpeg verification) is cached for 90 minutes and refreshed automatically

---

## Requirements

### Hardware
- Raspberry Pi 3 (or newer) with 64-bit Raspberry Pi OS
- Display connected via HDMI
- Internet connection
- X11 desktop session (not headless)

### Software Dependencies

**System packages:**
```
mpv
ffmpeg
python3-pip
python3-opencv
fonts-liberation
```

**Python packages:**
```
streamlink
sgp4
numpy
```

---

## Installation

### 1. Clone / copy files

Place all project files in `~/iss/`:
```
~/iss/
├── iss_watch.py
├── iss-watch.service
└── iss-watch.desktop  ← autostart
```

### 2. Install system dependencies

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
  mpv \
  ffmpeg \
  python3-pip \
  python3-opencv \
  fonts-liberation
```

### 3. Install Python dependencies

```bash
pip3 install streamlink sgp4 numpy --break-system-packages
```

### 4. Add yt-dlp to PATH

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

### 5. Test manually

```bash
python3 ~/iss/iss_watch.py
```

You should see the NASA ISS stream fullscreen. The terminal will log:
```
=======================================================
  ISS Watch gestartet
=======================================================
[tle] TLE aktualisiert (Epoche: ...)
[mpv] Gestartet: https://www.youtube.com/watch?v=zPH5KtjJFaQ...
```

### 6. Install systemd user service

```bash
mkdir -p ~/.config/systemd/user
cp ~/iss/iss-watch.service ~/.config/systemd/user/

loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user enable iss-watch
systemctl --user start iss-watch
```

### 7. Enable autostart on desktop login

```bash
mkdir -p ~/.config/autostart
cp ~/iss/iss-watch.desktop ~/.config/autostart/
```

This triggers the systemd service once the X11 desktop session is ready — more reliable than depending on `graphical-session.target` directly on Pi OS.

### 8. Keep yt-dlp updated (recommended)

YouTube changes its internal API regularly. Add a daily auto-update:

```bash
(crontab -l 2>/dev/null; echo "0 4 * * * /home/$USER/.local/bin/yt-dlp -U") | crontab -
```

---

## Files

| File | Description |
|------|-------------|
| `iss_watch.py` | Main script |
| `iss-watch.service` | systemd user service |
| `iss-watch.desktop` | XDG autostart entry |

---

## Monitoring

**Live logs:**
```bash
tail -f ~/iss/iss_watch.log
```

**Service status:**
```bash
systemctl --user status iss-watch
```

**Restart:**
```bash
systemctl --user restart iss-watch
```

---

## Configuration

All tunable parameters are at the top of `iss_watch.py`:

| Variable | Default | Description |
|----------|---------|-------------|
| `NASA_FALLBACK_ID` | `zPH5KtjJFaQ` | YouTube video ID of NASA ISS Earth camera |
| `CHECK_SECS` | `15` | Seconds between LOS checks |
| `LOS_WHITE_THRESHOLD` | `0.08` | Bright pixel ratio to declare LOS (0.0–1.0) |
| `TLE_TTL` | `6h` | How often to refresh orbital data |
| `VIDEO_ID_TTL` | `2h` | How often to verify YouTube video ID |
| `HLS_URL_TTL` | `90min` | How often to refresh raw HLS URL |

If you get false LOS detections (e.g. over bright Arctic ice or cloud fields), raise `LOS_WHITE_THRESHOLD` slightly to `0.10` or `0.12`.

---

## Sources

- NASA ISS live stream: [youtube.com/watch?v=zPH5KtjJFaQ](https://www.youtube.com/watch?v=zPH5KtjJFaQ)
- Sen 4K ISS camera: [youtube.com/@sen/live](https://www.youtube.com/@sen/live)
- ISS TLE data: [celestrak.org](https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE)
- sgp4 library: [pypi.org/project/sgp4](https://pypi.org/project/sgp4/)
