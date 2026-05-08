#!/usr/bin/env python3
"""
ISS Watch — Raspberry Pi ISS Stream Controller
Primär: Sen 4K ISS-Kamera (nur Live-Modus).
Fallback: EUMETSAT Meteosat Wettersatellit (Europa).
Watchdog: erkennt wenn mpv hängt oder kein Video mehr zeigt.
"""
import subprocess
import time
import json
import socket
import os
import logging
import logging.handlers
import urllib.request
import numpy as np
import cv2
from datetime import datetime

# ── Konfiguration ──────────────────────────────────────────────────────────────

SEN_STREAM     = "https://www.youtube.com/@sen/live"
MPV_SOCKET     = "/tmp/mpv-ipc"
YT_COOKIES     = os.path.expanduser("~/projects/iss-watch/yt-cookies.txt")

# EUMETSAT Wettersatellit — direkt vom Satellitenbetreiber (eumetview.eumetsat.int)
# VIS006Color = sichtbares Licht (schöner, aber nur tagsüber)
# IR039Color  = Infrarot (funktioniert Tag und Nacht)
WEATHER_IMAGE     = "/tmp/weather_satellite.jpg"
WEATHER_DAY_URL   = (
    "https://eumetview.eumetsat.int/static-images/latestImages/"
    "EUMETSAT_MSG_VIS006Color_CentralEurope.jpg"
)
WEATHER_NIGHT_URL = (
    "https://eumetview.eumetsat.int/static-images/latestImages/"
    "EUMETSAT_MSGIODC_IR039Color_Europe.jpg"
)
WEATHER_REFRESH   = 15 * 60    # Neues Satellitenbild alle 15 Minuten

# SEN-Modus-Erkennung via Badge-Farbe oben links im Video-Frame
SEN_SCREENSHOT     = "/tmp/sen_frame.png"
SEN_MODE_INTERVAL  = 30        # Alle 30s Badge prüfen (im SEN-Modus)
SEN_FALLBACK_RETRY = 5 * 60    # Alle 5 Min auf SEN testen (im Wetter-Modus)
SEN_UNKNOWN_LIMIT  = 6         # N unbekannte Badge-Ergebnisse → Wechsel zu Wetter

# Watchdog
WATCHDOG_INTERVAL   = 30
WATCHDOG_MAX_STUCK  = 3
STUCK_MIN_PROGRESS  = WATCHDOG_INTERVAL * 0.3
STARTUP_GRACE       = 60

DISPLAY_OUTPUT = "HDMI-A-1"

# Logging
LOG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iss-watch.log")
LOG_MAX_BYTES    = 2 * 1024 * 1024   # 2 MB pro Datei
LOG_BACKUP_COUNT = 7                  # 7 Backup-Dateien → max ~16 MB gesamt
LOG_HEARTBEAT    = 60 * 60            # Stündlicher Status-Eintrag

# ──────────────────────────────────────────────────────────────────────────────


def _fmt_dur(seconds):
    """Sekunden → lesbarer String, z.B. '2h05m30s'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


def _is_local_file(url):
    """True wenn es eine lokale Datei ist (kein Stream)."""
    return url.startswith("/") or url.startswith("file://")


# ── Logging-Setup ─────────────────────────────────────────────────────────────

def setup_logging():
    """
    Richtet den Logger ein:
    - Rotierende Log-Datei (neben dem Skript): iss-watch.log
    - Zusätzlich auf die Konsole (stdout)
    """
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("iss-watch")
    logger.setLevel(logging.DEBUG)

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


log = setup_logging()


# ── Anzeige-Zustand verfolgen ─────────────────────────────────────────────────

# Beschreibungen der Zustände für das Log
_STATE_LABELS = {
    "startup":        "Programmstart",
    "sen_live":       "SEN Live-Stream",
    "sen_loading":    "SEN lädt (Desktop sichtbar)",
    "weather":        "EUMETSAT Wetter-Satellit",
    "weather_update": "Wetter-Bild Aktualisierung",
    "sen_retry":      "SEN-Rückkehr Test (Desktop kurz sichtbar)",
    "idle":           "KEIN BILD (mpv idle / Desktop sichtbar)",
    "wifi_dialog":    "WLAN-Dialog unterdrückt",
}

_display_state      = "startup"
_display_state_since = time.time()


def set_display_state(new_state, reason=""):
    """Zustandswechsel protokollieren mit Dauer des vorherigen Zustands."""
    global _display_state, _display_state_since
    now = time.time()
    dur = now - _display_state_since

    old_label = _STATE_LABELS.get(_display_state, _display_state)
    new_label = _STATE_LABELS.get(new_state, new_state)

    if _display_state != "startup":
        log.info(
            f"ANZEIGE ENDE  | {old_label:<38} | Dauer: {_fmt_dur(dur)}"
        )

    reason_str = f"  ← {reason}" if reason else ""
    log.info(
        f"ANZEIGE START | {new_label:<38}{reason_str}"
    )

    _display_state      = new_state
    _display_state_since = now


def log_event(category, message):
    log.info(f"EVENT  [{category:<10}] {message}")


def log_warn(category, message):
    log.warning(f"WARN   [{category:<10}] {message}")


def log_error(category, message):
    log.error(f"FEHLER [{category:<10}] {message}")


# ── MPV-Controller ────────────────────────────────────────────────────────────

class MPVController:
    def __init__(self, socket_path):
        self.socket_path  = socket_path
        self.proc         = None
        self._last_pos    = None
        self._stuck_count = 0
        self._started_at  = 0
        self.image_mode   = False  # True wenn Standbild angezeigt wird
        self._restarts    = 0

    def start(self, url):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            time.sleep(2)
        subprocess.run(["pkill", "-x", "mpv"], capture_output=True)
        time.sleep(1)
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

        cmd = [
            "mpv",
            f"--input-ipc-server={self.socket_path}",
            "--fullscreen",
            "--no-terminal",
            "--no-osc",
            "--idle=yes",
            "--keep-open=no",
            "--image-display-duration=inf",   # Standbild dauerhaft halten
            "--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--cache=yes",
            "--demuxer-max-bytes=150MiB",
            "--really-quiet",
        ]
        if os.path.exists(YT_COOKIES):
            cmd.append(f"--ytdl-raw-options=cookies={YT_COOKIES}")
        cmd.append(url)

        env = os.environ.copy()
        env["DISPLAY"] = ":0"
        self.proc = subprocess.Popen(cmd, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self._reset_watchdog()
        self.image_mode = _is_local_file(url)
        self._restarts += 1
        for _ in range(20):
            time.sleep(0.5)
            if os.path.exists(self.socket_path):
                break
        log_event("mpv", f"Neustart #{self._restarts}: {url[:70]}")

    def send(self, command):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(3)
                s.connect(self.socket_path)
                msg = json.dumps({"command": command}) + "\n"
                s.sendall(msg.encode())
                resp = s.recv(4096).decode()
                return json.loads(resp)
        except Exception:
            return None

    def load(self, url):
        self._reset_watchdog()
        self.image_mode = _is_local_file(url)
        self.send(["loadfile", url, "replace"])
        log_event("mpv", f"loadfile: {url[:70]}")

    def show_text(self, text, duration_ms=8000):
        self.send(["show-text", text, duration_ms])

    def screenshot(self, path):
        """Aktuellen Video-Frame als PNG speichern."""
        result = self.send(["screenshot-to-file", path, "video"])
        if result and result.get("error") == "success":
            time.sleep(0.3)
            return os.path.exists(path)
        return False

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def is_playing(self):
        """
        Watchdog: Prüft ob aktiv gespielt wird.
        Im Standbild-Modus (image_mode=True): nur Prozesscheck.
        """
        if self.image_mode:
            return self.is_running()

        grace_remaining = STARTUP_GRACE - (time.time() - self._started_at)
        if grace_remaining > 0:
            log_event("watchdog", f"Schonfrist ({grace_remaining:.0f}s verbleibend)")
            return True

        result = self.send(["get_property", "playback-time"])
        if result is None or result.get("error") == "property unavailable":
            log_warn("watchdog", "IPC nicht erreichbar")
            return False

        pos = result.get("data")
        if pos is None:
            log_warn("watchdog", "Keine Abspielposition — mpv idle (Desktop sichtbar)")
            return False

        if self._last_pos is not None and abs(pos - self._last_pos) < STUCK_MIN_PROGRESS:
            self._stuck_count += 1
            log_warn("watchdog",
                     f"Bild eingefroren ({self._stuck_count}/{WATCHDOG_MAX_STUCK}) "
                     f"Δpos={abs(pos - self._last_pos):.1f}s")
            if self._stuck_count >= WATCHDOG_MAX_STUCK:
                return False
        else:
            self._stuck_count = 0

        self._last_pos = pos
        return True

    def _reset_watchdog(self):
        self._last_pos    = None
        self._stuck_count = 0
        self._started_at  = time.time()


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def wait_for_network(timeout=60):
    log_event("netzwerk", f"Warte auf Netzwerk (max {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen("http://www.google.com", timeout=3)
            log_event("netzwerk", f"Netzwerk verfügbar nach {time.time()-start:.0f}s")
            return True
        except Exception:
            time.sleep(3)
    log_warn("netzwerk", f"Kein Netzwerk nach {timeout}s — fahre trotzdem fort")
    return False


def detect_sen_mode(img_path):
    """
    Erkennt SEN-Sendemodus anhand der Badge-Farbe oben links im Video-Frame.

    Badge-Farben (OpenCV BGR):
      Live:    Rot    (R≈200, G≈30,  B≈30)
      Replay:  Orange (R≈230, G≈100, B≈0)
      Trailer: Türkis (R≈0,   G≈180, B≈180)

    Gibt zurück: "live", "replay", "trailer", "unknown"
    """
    img = cv2.imread(img_path)
    if img is None:
        return "unknown"

    h, w = img.shape[:2]
    badge = img[
        max(0, int(h * 0.015)) : min(h, int(h * 0.085)),
        max(0, int(w * 0.008)) : min(w, int(w * 0.065)),
    ]

    if badge.size == 0:
        return "unknown"

    bright = badge.max(axis=2) > 80
    if bright.sum() < 10:
        log_event("sen-badge", "Nur dunkle Pixel im Badge-Bereich — kein Signal?")
        return "unknown"

    b, g, r = (float(c) for c in badge[bright].mean(axis=0))
    log_event("sen-badge", f"Farbe BGR: B={b:.0f} G={g:.0f} R={r:.0f}")

    if r > 150 and r - g > 60 and r - b > 60:          # Rot → Live
        return "live"
    if r > 150 and g > 50 and b < 80 and r - b > 100:  # Orange → Replay
        return "replay"
    if b > 100 and g > 100 and b - r > 60:             # Türkis → Trailer
        return "trailer"

    return "unknown"


def get_weather_url():
    """Tagsüber sichtbares Licht (VIS006), nachts Infrarot (IR039)."""
    return WEATHER_DAY_URL if 7 <= datetime.now().hour < 19 else WEATHER_NIGHT_URL


def download_weather_image():
    """
    Lädt das aktuelle EUMETSAT Satellitenbild herunter.
    Gibt True zurück wenn Download erfolgreich oder altes Bild noch nutzbar.
    """
    url = get_weather_url()
    fname = url.split("/")[-1]
    log_event("wetter", f"Lade Satellitenbild: {fname}")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 iss-watch/1.0"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        with open(WEATHER_IMAGE, "wb") as f:
            f.write(data)
        log_event("wetter", f"Gespeichert: {fname} ({len(data) // 1024} KB)")
        return True
    except Exception as e:
        if os.path.exists(WEATHER_IMAGE):
            log_warn("wetter", f"Download-Fehler: {e} — nutze altes Bild")
            return True
        log_error("wetter", f"Download-Fehler und kein Bild vorhanden: {e}")
        return False


def get_target_brightness():
    hour = datetime.now().hour
    if 6 <= hour < 21:
        return 1.0
    elif 21 <= hour or hour < 1:
        return 0.6
    return 0.3


def set_brightness(brightness):
    try:
        subprocess.run(
            ["xrandr", "--output", DISPLAY_OUTPUT, "--brightness", str(brightness)],
            env={**os.environ, "DISPLAY": ":0"},
            capture_output=True, timeout=10,
        )
        log_event("helligkeit", str(brightness))
    except Exception as e:
        log_error("helligkeit", str(e))


def suppress_desktop_dialogs():
    """
    Deaktiviert WLAN (Pi ist per LAN angeschlossen), unterdrückt Popups
    und setzt den Desktop-Hintergrund auf Schwarz.

    Permanente WLAN-Deaktivierung: 'dtoverlay=disable-wifi' in /boot/firmware/config.txt
    """
    env = {**os.environ, "DISPLAY": ":0"}
    subprocess.run(["xsetroot", "-solid", "black"], env=env, capture_output=True)

    # WLAN per NetworkManager und rfkill deaktivieren
    r1 = subprocess.run(["nmcli", "radio", "wifi", "off"], capture_output=True)
    r2 = subprocess.run(["rfkill", "block", "wifi"], capture_output=True)
    nmcli_ok = r1.returncode == 0
    rfkill_ok = r2.returncode == 0
    log_event("wlan", f"Deaktiviert — nmcli: {'OK' if nmcli_ok else 'Fehler'}, "
              f"rfkill: {'OK' if rfkill_ok else 'Fehler'}")

    # MATE polkit-Auth-Agent und nm-applet töten
    p1 = subprocess.run(["pkill", "-f", "polkit-mate-authentication-agent"],
                        capture_output=True)
    p2 = subprocess.run(["pkill", "-f", "nm-applet"], capture_output=True)
    if p1.returncode == 0 or p2.returncode == 0:
        set_display_state("wifi_dialog", "polkit/nm-applet waren aktiv und wurden beendet")


def load_weather(mpv, now):
    """Wetter-Bild herunterladen, in mpv laden und neuen Zeitstempel zurückgeben."""
    if download_weather_image():
        mpv.load(WEATHER_IMAGE)
        return now
    return 0


# ── Hauptschleife ─────────────────────────────────────────────────────────────

def main():
    log.info("=" * 60)
    log.info("  ISS Watch gestartet")
    log.info(f"  Log-Datei: {LOG_FILE}")
    log.info("  Primär: Sen 4K Live  |  Fallback: EUMETSAT Wetter")
    log.info("=" * 60)

    set_display_state("startup")
    suppress_desktop_dialogs()
    wait_for_network()

    mpv = MPVController(MPV_SOCKET)
    set_display_state("sen_loading", "Programmstart")
    mpv.start(SEN_STREAM)
    mode = "sen"   # "sen" oder "weather"

    last_watchdog      = time.time()
    last_sen_mode_chk  = time.time() - SEN_MODE_INTERVAL + 15
    last_weather_retry = time.time()
    last_weather_dl    = 0
    last_heartbeat     = time.time()
    current_brightness = None
    sen_unknown_count  = 0

    while True:
        try:
            now = time.time()

            # Helligkeit anpassen
            target = get_target_brightness()
            if target != current_brightness:
                set_brightness(target)
                current_brightness = target

            # ── Stündlicher Heartbeat ────────────────────────────────────────
            if now - last_heartbeat >= LOG_HEARTBEAT:
                last_heartbeat = now
                state_label = _STATE_LABELS.get(_display_state, _display_state)
                dur = _fmt_dur(now - _display_state_since)
                log.info(f"HEARTBEAT     | Modus: {mode:<7} | Anzeige: {state_label} "
                         f"(seit {dur}) | mpv läuft: {mpv.is_running()}")

            # ── Watchdog ────────────────────────────────────────────────────
            if now - last_watchdog >= WATCHDOG_INTERVAL:
                last_watchdog = now

                if not mpv.is_running():
                    log_error("watchdog", "mpv-Prozess tot → Neustart")
                    set_display_state("idle", "mpv-Prozess abgestürzt")
                    wait_for_network(30)
                    if mode == "sen":
                        set_display_state("sen_loading", "Watchdog-Neustart nach Absturz")
                        mpv.start(SEN_STREAM)
                    else:
                        last_weather_dl = load_weather(mpv, now)
                        set_display_state("weather", "Watchdog-Neustart nach Absturz")
                    time.sleep(5)
                    continue

                if not mpv.is_playing():
                    log_warn("watchdog", "Kein Playback erkannt → Stream neu laden")
                    set_display_state("idle", "Watchdog: kein Playback")
                    wait_for_network(30)
                    if mode == "sen":
                        set_display_state("sen_loading", "Watchdog-Reload")
                        mpv.load(SEN_STREAM)
                    else:
                        last_weather_dl = load_weather(mpv, now)
                        set_display_state("weather", "Watchdog-Reload")
                    time.sleep(5)
                    continue

            # ── SEN-Modus prüfen (nur im SEN-Modus) ────────────────────────
            if mode == "sen" and now - last_sen_mode_chk >= SEN_MODE_INTERVAL:
                last_sen_mode_chk = now
                if mpv.screenshot(SEN_SCREENSHOT):
                    sen_mode = detect_sen_mode(SEN_SCREENSHOT)
                    log_event("sen-modus", f"Badge erkannt: {sen_mode}")

                    if sen_mode in ("replay", "trailer"):
                        log_warn("sen-modus",
                                 f"SEN ist nicht live ({sen_mode}) → Wechsel zu Wetter")
                        sen_unknown_count = 0
                        last_weather_dl    = load_weather(mpv, now)
                        last_weather_retry = now
                        set_display_state("weather", f"SEN-Badge: {sen_mode}")
                        mode = "weather"

                    elif sen_mode == "unknown":
                        sen_unknown_count += 1
                        log_warn("sen-modus",
                                 f"Badge unbekannt ({sen_unknown_count}/{SEN_UNKNOWN_LIMIT})")
                        if sen_unknown_count >= SEN_UNKNOWN_LIMIT:
                            log_warn("sen-modus",
                                     "Badge dauerhaft unlesbar → Wechsel zu Wetter")
                            sen_unknown_count = 0
                            last_weather_dl    = load_weather(mpv, now)
                            last_weather_retry = now
                            set_display_state("weather", "SEN-Badge unlesbar")
                            mode = "weather"

                    else:  # "live"
                        if _display_state != "sen_live":
                            set_display_state("sen_live", "Badge: live")
                        sen_unknown_count = 0

                else:
                    log_warn("sen-modus", "Screenshot fehlgeschlagen — überspringe")

            # ── Im Wetter-Modus ──────────────────────────────────────────────
            if mode == "weather":

                # Satellitenbild regelmäßig aktualisieren
                if now - last_weather_dl >= WEATHER_REFRESH:
                    log_event("wetter", "Aktualisiere Satellitenbild")
                    old_ts = last_weather_dl
                    last_weather_dl = load_weather(mpv, now)
                    if last_weather_dl > old_ts:
                        set_display_state("weather", "Satellitenbild aktualisiert")

                # Regelmäßig testen ob SEN wieder live ist
                if now - last_weather_retry >= SEN_FALLBACK_RETRY:
                    last_weather_retry = now
                    log_event("sen-retry", "Teste ob SEN wieder live ist...")
                    set_display_state("sen_retry", "alle 5 Minuten")
                    mpv.show_text("Prüfe ISS-Stream...", 4000)
                    mpv.load(SEN_STREAM)
                    time.sleep(30)  # Warten bis Stream lädt und Frame verfügbar

                    if mpv.screenshot(SEN_SCREENSHOT):
                        sen_mode = detect_sen_mode(SEN_SCREENSHOT)
                        log_event("sen-retry", f"Badge beim Retry: {sen_mode}")
                        if sen_mode == "live":
                            log_event("sen-retry", "SEN ist wieder live → Wechsel")
                            mode = "sen"
                            sen_unknown_count = 0
                            last_sen_mode_chk = now
                            set_display_state("sen_live", "SEN-Retry erfolgreich")
                        else:
                            log_event("sen-retry",
                                      f"SEN noch nicht live ({sen_mode}) → zurück zu Wetter")
                            last_weather_dl = load_weather(mpv, now)
                            set_display_state("weather", f"SEN-Retry: {sen_mode}")
                    else:
                        log_warn("sen-retry",
                                 "Kein Screenshot beim Retry → zurück zu Wetter")
                        last_weather_dl = load_weather(mpv, now)
                        set_display_state("weather", "SEN-Retry: kein Screenshot")

            time.sleep(10)

        except KeyboardInterrupt:
            log.info("Beendet durch Benutzer (Ctrl+C)")
            break
        except Exception as e:
            log_error("main", f"{type(e).__name__}: {e}")
            time.sleep(10)

    if mpv.proc:
        mpv.proc.terminate()
    set_display_state("idle", "Programm beendet")
    log.info("=" * 60)
    log.info("  ISS Watch beendet")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
