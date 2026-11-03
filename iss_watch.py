#!/usr/bin/env python3
"""
ISS Watch — Raspberry Pi ISS Stream Controller
Zeigt dauerhaft den SEN 4K ISS-Live-Stream.
Bildschirm aus: 01:00–06:00 via vcgencmd display_power.
"""
import subprocess
import sys
import time
import json
import socket
import os
import shutil
import logging
import logging.handlers
import urllib.request
import cv2
from datetime import datetime

# ── Konfiguration ─────────────────────────────────────────────────────────────

SEN_STREAM = "https://www.youtube.com/@sen/live"
MPV_SOCKET = "/tmp/mpv-ipc"
YT_COOKIES = os.path.expanduser("~/projects/iss-watch/yt-cookies.txt")

# Nachtmodus: Bildschirm aus zwischen SLEEP_START und SLEEP_END Uhr
SLEEP_START = 1   # 01:00
SLEEP_END   = 6   # 06:00

# Watchdog
WATCHDOG_INTERVAL  = 30    # Sekunden zwischen Watchdog-Checks
WATCHDOG_MAX_STUCK = 3     # N aufeinanderfolgende Nicht-Fortschritt-Messungen → Reload
STUCK_MIN_PROGRESS = WATCHDOG_INTERVAL * 0.3
STARTUP_GRACE      = 90   # Schonfrist nach Neustart (Sekunden)

# Badge + Frozen-Frame-Erkennung
BADGE_INTERVAL  = 60   # Sekunden zwischen Screenshot-Checks
FROZEN_DIFF     = 1.5  # Pixeldifferenz-Schwelle (0–255); Live-Stream typ. >5
FROZEN_LIMIT    = 4    # N eingefrorene Frames → Reload

SEN_SCREENSHOT = "/tmp/sen_frame.png"
SEN_PREV_SHOT  = "/tmp/sen_frame_prev.png"

# Logging
LOG_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iss-watch.log")
LOG_MAX_BYTES    = 2 * 1024 * 1024   # 2 MB pro Datei
LOG_BACKUP_COUNT = 7                  # max ~16 MB gesamt
LOG_HEARTBEAT    = 60 * 60            # Stündlicher Status-Eintrag

# ─────────────────────────────────────────────────────────────────────────────


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m{s % 60:02d}s"


# ── Logging ───────────────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("iss-watch")
    logger.setLevel(logging.DEBUG)
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


log = _setup_logging()


def log_event(cat: str, msg: str) -> None:
    log.info(f"EVENT  [{cat:<10}] {msg}")


def log_warn(cat: str, msg: str) -> None:
    log.warning(f"WARN   [{cat:<10}] {msg}")


def log_error(cat: str, msg: str) -> None:
    log.error(f"FEHLER [{cat:<10}] {msg}")


# ── Bildschirmsteuerung (Raspberry Pi) ────────────────────────────────────────

def screen_on() -> None:
    subprocess.run(["vcgencmd", "display_power", "1"], capture_output=True)
    log_event("screen", "Bildschirm AN")


def screen_off() -> None:
    subprocess.run(["vcgencmd", "display_power", "0"], capture_output=True)
    log_event("screen", "Bildschirm AUS")


def is_sleep_time() -> bool:
    """True zwischen SLEEP_START und SLEEP_END Uhr."""
    h = datetime.now().hour
    return SLEEP_START <= h < SLEEP_END


# ── MPV-Controller ────────────────────────────────────────────────────────────

class MPVController:
    def __init__(self, socket_path: str) -> None:
        self.socket_path  = socket_path
        self.proc         = None
        self._last_pos    = None
        self._stuck_count = 0
        self._started_at  = 0.0
        self._restarts    = 0

    def start(self) -> None:
        """Beendet laufende mpv-Instanz und startet neu mit SEN-Stream."""
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
            "--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--cache=yes",
            "--demuxer-max-bytes=150MiB",
            "--really-quiet",
        ]
        if os.path.exists(YT_COOKIES):
            cmd.append(f"--ytdl-raw-options=cookies={YT_COOKIES}")
        cmd.append(SEN_STREAM)

        env = {**os.environ, "DISPLAY": ":0"}
        self.proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self._restarts += 1
        self._reset_watchdog()

        # Warten bis IPC-Socket bereit ist (max 15s)
        for _ in range(30):
            time.sleep(0.5)
            if os.path.exists(self.socket_path):
                break

        log_event("mpv", f"Neustart #{self._restarts} — {SEN_STREAM}")

    def kill(self) -> None:
        """Beendet mpv sauber."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            time.sleep(1)
        subprocess.run(["pkill", "-x", "mpv"], capture_output=True)
        self.proc = None
        log_event("mpv", "Beendet")

    def send(self, command: list, timeout: int = 5):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect(self.socket_path)
                s.sendall((json.dumps({"command": command}) + "\n").encode())
                return json.loads(s.recv(4096).decode())
        except Exception:
            return None

    def reload(self) -> None:
        """Lädt SEN-Stream in laufendem mpv neu (schneller als Neustart)."""
        self._reset_watchdog()
        self.send(["loadfile", SEN_STREAM, "replace"])
        log_event("mpv", f"reload: {SEN_STREAM}")

    def screenshot(self, path: str) -> bool:
        """Aktuellen Video-Frame als PNG speichern."""
        result = self.send(["screenshot-to-file", path, "video"], timeout=20)
        if result and result.get("error") == "success":
            time.sleep(0.3)
            return os.path.exists(path)
        return False

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def is_playing(self) -> bool:
        """Watchdog: Hat der Stream Fortschritt? Gibt False → Reload nötig."""
        grace_left = STARTUP_GRACE - (time.time() - self._started_at)
        if grace_left > 0:
            log_event("watchdog", f"Schonfrist {grace_left:.0f}s")
            return True

        result = self.send(["get_property", "playback-time"])
        if result is None or result.get("error") == "property unavailable":
            log_warn("watchdog", "IPC nicht erreichbar")
            return False

        pos = result.get("data")
        if pos is None:
            log_warn("watchdog", "Keine Abspielposition — mpv idle")
            return False

        if (
            self._last_pos is not None
            and abs(pos - self._last_pos) < STUCK_MIN_PROGRESS
        ):
            self._stuck_count += 1
            log_warn(
                "watchdog",
                f"Kein Fortschritt ({self._stuck_count}/{WATCHDOG_MAX_STUCK}) "
                f"Δ={abs(pos - self._last_pos):.1f}s",
            )
            if self._stuck_count >= WATCHDOG_MAX_STUCK:
                return False
        else:
            self._stuck_count = 0

        self._last_pos = pos
        return True

    def _reset_watchdog(self) -> None:
        self._last_pos    = None
        self._stuck_count = 0
        self._started_at  = time.time()


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def wait_for_network(timeout: int = 120) -> bool:
    log_event("netzwerk", f"Warte auf Netzwerk (max {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(
                "http://connectivitycheck.gstatic.com/generate_204", timeout=5
            )
            log_event("netzwerk", f"OK nach {time.time() - start:.0f}s")
            return True
        except Exception:
            time.sleep(5)
    log_warn("netzwerk", "Kein Netzwerk — fahre trotzdem fort")
    return False


def suppress_dialogs() -> None:
    """Setzt Desktop auf schwarz, deaktiviert WLAN, tötet störende Dialoge."""
    env = {**os.environ, "DISPLAY": ":0"}
    subprocess.run(["xsetroot", "-solid", "black"], env=env, capture_output=True)
    subprocess.run(["nmcli", "radio", "wifi", "off"], capture_output=True)
    subprocess.run(["rfkill", "block", "wifi"], capture_output=True)
    subprocess.run(
        ["pkill", "-f", "polkit-mate-authentication-agent"], capture_output=True
    )
    subprocess.run(["pkill", "-f", "nm-applet"], capture_output=True)
    log_event("init", "Desktop bereinigt, WLAN deaktiviert")


def detect_badge(img_path: str) -> str:
    """
    SEN-Badge-Farbe oben links → 'live', 'replay', 'trailer', 'unknown'.
    Badge-Farben (BGR):
      Live:    Rot    (R>200, G~30,  B~30)
      Replay:  Orange (R>200, G~100, B~0)
      Trailer: Türkis (R~0,   G>150, B>150)
    """
    img = cv2.imread(img_path)
    if img is None:
        return "unknown"
    h, w = img.shape[:2]
    badge = img[int(h * 0.015):int(h * 0.085), int(w * 0.008):int(w * 0.065)]
    if badge.size == 0:
        return "unknown"
    bright = badge.max(axis=2) > 80
    if bright.sum() < 10:
        return "unknown"
    b, g, r = (float(c) for c in badge[bright].mean(axis=0))
    log_event("badge", f"BGR B={b:.0f} G={g:.0f} R={r:.0f}")
    if r > 150 and r - g > 110 and r - b > 80:
        return "live"
    if r > 150 and g > 60 and b < 100 and r - b > 80:
        return "replay"
    if b > 100 and g > 100 and b - r > 60:
        return "trailer"
    return "unknown"


def is_frame_frozen(new_path: str, prev_path: str) -> bool:
    """True wenn zwei aufeinanderfolgende Frames nahezu identisch sind."""
    if not os.path.exists(prev_path):
        return False
    a = cv2.imread(new_path)
    b = cv2.imread(prev_path)
    if a is None or b is None or a.shape != b.shape:
        return False
    diff = float(cv2.absdiff(a, b).mean())
    log_event("frozen", f"Frame-Diff {diff:.2f} (Schwelle {FROZEN_DIFF})")
    return diff < FROZEN_DIFF


# ── Hauptschleife ─────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("  ISS Watch gestartet")
    log.info(f"  Stream:     {SEN_STREAM}")
    log.info(f"  Nachtmodus: {SLEEP_START:02d}:00 – {SLEEP_END:02d}:00")
    log.info("=" * 60)

    suppress_dialogs()
    wait_for_network()

    mpv = MPVController(MPV_SOCKET)

    last_watchdog   = time.time()
    last_badge      = time.time() - BADGE_INTERVAL + 20  # ersten Check nach 20s
    last_heartbeat  = time.time()
    frozen_count    = 0
    recovery_level  = 0   # Eskalationsstufe bei Frozen-Frame (0=OK, 1=reload, 2=start, 3=sys.exit)
    screen_is_on    = True

    # Direkt beim Start Nachtmodus prüfen
    if is_sleep_time():
        log_event("screen", f"Nachtmodus aktiv ({SLEEP_START:02d}:00–{SLEEP_END:02d}:00)")
        screen_off()
        screen_is_on = False
    else:
        screen_on()
        mpv.start()

    while True:
        try:
            now     = time.time()
            in_sleep = is_sleep_time()

            # ── Nacht/Tag-Übergang ────────────────────────────────────────────
            if in_sleep and screen_is_on:
                log_event("screen", "Nachtmodus: mpv stoppen, Bildschirm aus")
                mpv.kill()
                screen_off()
                screen_is_on = False
                frozen_count = 0

            elif not in_sleep and not screen_is_on:
                log_event("screen", "Tagmodus: Bildschirm an, Stream starten")
                screen_on()
                screen_is_on = True
                wait_for_network(60)
                mpv.start()
                frozen_count   = 0
                recovery_level = 0
                last_watchdog  = now
                last_badge     = now + 20

            # Nachtmodus: Rest der Schleife überspringen
            if in_sleep:
                time.sleep(30)
                continue

            # ── Stündlicher Heartbeat ────────────────────────────────────────
            if now - last_heartbeat >= LOG_HEARTBEAT:
                last_heartbeat = now
                log.info(
                    f"HEARTBEAT | mpv läuft: {mpv.is_running()} | "
                    f"Restarts: {mpv._restarts} | Frozen-Count: {frozen_count}"
                )

            # ── Watchdog ─────────────────────────────────────────────────────
            if now - last_watchdog >= WATCHDOG_INTERVAL:
                last_watchdog = now

                if not mpv.is_running():
                    log_error("watchdog", "mpv-Prozess tot → Neustart")
                    wait_for_network(30)
                    mpv.start()
                    frozen_count   = 0
                    recovery_level = 0
                    time.sleep(5)
                    continue

                if not mpv.is_playing():
                    log_warn("watchdog", "Kein Playback → Stream neu laden")
                    wait_for_network(30)
                    mpv.reload()
                    frozen_count   = 0
                    recovery_level = 0
                    time.sleep(5)
                    continue

            # ── Badge + Frozen-Frame-Check ───────────────────────────────────
            if now - last_badge >= BADGE_INTERVAL:
                last_badge = now

                if mpv.screenshot(SEN_SCREENSHOT):
                    if is_frame_frozen(SEN_SCREENSHOT, SEN_PREV_SHOT):
                        frozen_count += 1
                        log_warn("frozen", f"Frame eingefroren ({frozen_count}/{FROZEN_LIMIT})")
                        if frozen_count >= FROZEN_LIMIT:
                            frozen_count    = 0
                            recovery_level += 1
                            wait_for_network(30)
                            if recovery_level == 1:
                                log_warn("frozen",
                                         "Dauerhaft eingefroren → Stream neu laden (Level 1)")
                                mpv.reload()
                            elif recovery_level == 2:
                                log_warn("frozen",
                                         "Reload hat nicht geholfen → mpv Neustart (Level 2)")
                                mpv.start()
                            else:
                                log_error("frozen",
                                          f"mpv Neustart hat nicht geholfen → "
                                          f"Script-Neustart via systemd (Level {recovery_level})")
                                sys.exit(1)   # systemd Restart=always startet Script neu
                            time.sleep(10)
                            continue
                    else:
                        if recovery_level > 0:
                            log_event("frozen",
                                      f"Stream wieder OK — Recovery-Level {recovery_level} → 0")
                        frozen_count   = 0
                        recovery_level = 0
                        try:
                            shutil.copy2(SEN_SCREENSHOT, SEN_PREV_SHOT)
                        except OSError:
                            pass

                    badge = detect_badge(SEN_SCREENSHOT)
                    log_event("badge", f"SEN-Modus: {badge}")
                else:
                    log_warn("badge", "Screenshot fehlgeschlagen")

            time.sleep(10)

        except KeyboardInterrupt:
            log.info("Beendet (Ctrl+C)")
            break
        except Exception as exc:
            log_error("main", f"{type(exc).__name__}: {exc}")
            time.sleep(10)

    mpv.kill()
    log.info("=" * 60)
    log.info("  ISS Watch beendet")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
