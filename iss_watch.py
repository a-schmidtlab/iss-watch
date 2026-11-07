#!/usr/bin/env python3
"""
ISS Watch — Raspberry Pi ISS Stream Controller
Zeigt den SEN 4K ISS-Live-Stream und optional ein Wetter-Dashboard
(Ansichten ISS / Wetter / Aus, siehe kiosk.py und config.example.toml).
Bildschirm aus: 01:00–06:00 (vcgencmd unter X11, wlr-randr unter Wayland).
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
import queue
import threading
import urllib.request
import cv2

import kiosk

# ── Konfiguration ─────────────────────────────────────────────────────────────

# Stream, Cookies, Nachtmodus, Ansichten: ~/.config/iss-watch/config.toml (kiosk.py)
CFG        = kiosk.load_config()
SEN_STREAM = CFG.stream_url
MPV_SOCKET = "/tmp/mpv-ipc"
YT_COOKIES = CFG.cookies

# Watchdog
WATCHDOG_INTERVAL  = 30    # Sekunden zwischen Watchdog-Checks
WATCHDOG_MAX_STUCK = 3     # N aufeinanderfolgende Nicht-Fortschritt-Messungen → Reload
STUCK_MIN_PROGRESS = WATCHDOG_INTERVAL * 0.3
STARTUP_GRACE      = 90   # Schonfrist nach Neustart (Sekunden)

# Badge + Frozen-Frame-Erkennung
BADGE_INTERVAL  = 60   # Sekunden zwischen Screenshot-Checks
FROZEN_DIFF     = 1.5  # Pixeldifferenz-Schwelle (0–255); Live-Stream typ. >5
FROZEN_LIMIT    = 4    # N eingefrorene Frames → Reload

GUARD_INTERVAL = 1     # Sekunden zwischen Prüfungen, ob der Bildschirm die Ansicht zeigt

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

class Display:
    """Bildschirm an/aus mit Log, je nach Plattform vcgencmd oder wlr-randr."""

    def __init__(self, cfg: kiosk.Config) -> None:
        if cfg.display_backend == "ddc":
            self.impl = kiosk.DdcDisplay(cfg.ddc_bus)
        elif cfg.wayland:
            self.impl = kiosk.WlrRandrDisplay(cfg.output, cfg.output_transform)
        else:
            self.impl = kiosk.VcgencmdDisplay()

    def on(self) -> None:
        self.impl.on()
        log_event("screen", "Bildschirm AN")

    def off(self) -> None:
        self.impl.off()
        log_event("screen", "Bildschirm AUS")

    def is_on(self) -> bool:
        return self.impl.is_on()


# ── MPV-Controller ────────────────────────────────────────────────────────────

class MPVController:
    def __init__(self, socket_path: str) -> None:
        self.lock         = threading.RLock()  # Ansichten und Überwachung teilen sich mpv
        self.socket_path  = socket_path
        self.proc         = None
        self._last_pos    = None
        self._stuck_count = 0
        self._started_at  = 0.0
        self._restarts    = 0

    def start(self) -> None:
        """Beendet laufende mpv-Instanz und startet neu mit SEN-Stream."""
        with self.lock:
            self._start()

    def _start(self) -> None:
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
            # Fenster bleibt beim Neuladen bestehen, sonst legt es sich neu nach vorn
            "--force-window=immediate",
            "--keep-open=no",
            "--mute=yes",
            "--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--cache=yes",
            "--demuxer-max-bytes=150MiB",
            "--really-quiet",
        ]
        if CFG.wayland:
            cmd.append("--wayland-app-id=iss-mpv")
        if CFG.ytdl_path:
            cmd.append(f"--script-opts=ytdl_hook-ytdl_path={os.path.expanduser(CFG.ytdl_path)}")
        if os.path.exists(YT_COOKIES):
            cmd.append(f"--ytdl-raw-options=cookies={YT_COOKIES}")
        cmd.append(SEN_STREAM)

        env = dict(os.environ) if CFG.wayland else {**os.environ, "DISPLAY": ":0"}
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
        with self.lock:
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
        with self.lock:
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

    def is_ready(self) -> bool:
        """Stream spielt: Datei geladen (playback-time vorhanden) und nicht am Puffern."""
        if not self.is_running():
            return False
        pos = self.send(["get_property", "playback-time"], timeout=1)
        idle = self.send(["get_property", "core-idle"], timeout=1)
        return bool(pos and pos.get("data") is not None and idle and idle.get("data") is False)

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
    """Desktop schwarz (X11), WLAN optional aus, störende Dialoge beenden."""
    if not CFG.wayland:
        env = {**os.environ, "DISPLAY": ":0"}
        subprocess.run(["xsetroot", "-solid", "black"], env=env, capture_output=True)
    if CFG.disable_wifi:
        subprocess.run(["nmcli", "radio", "wifi", "off"], capture_output=True)
        subprocess.run(["rfkill", "block", "wifi"], capture_output=True)
        subprocess.run(
            ["pkill", "-f", "polkit-mate-authentication-agent"], capture_output=True
        )
        subprocess.run(["pkill", "-f", "nm-applet"], capture_output=True)
    log_event("init", "Desktop bereinigt" + (", WLAN deaktiviert" if CFG.disable_wifi else ", WLAN bleibt an"))


def setup_button(events: "queue.Queue") -> object:
    """Taster an GPIO (gegen GND, interner Pull-up). Jeder Druck → Ereignis in der Queue."""
    if not CFG.button_gpio:
        return None
    try:
        from gpiozero import Button
    except ImportError:
        log_warn("taster", "gpiozero nicht installiert — Taster deaktiviert")
        return None
    button = Button(CFG.button_gpio, pull_up=True, bounce_time=0.05)
    # Zeitpunkt des Drucks mitgeben: die Sperrzeit gilt ab dem Druck, nicht ab der Verarbeitung
    button.when_pressed = lambda: events.put(time.time())
    log_event("taster", f"Taster an GPIO{CFG.button_gpio} bereit")
    return button


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


# ── Stream-Überwachung ────────────────────────────────────────────────────────

class StreamMonitor(threading.Thread):
    """Watchdog und Frozen-Frame-Erkennung in einem eigenen Thread.

    Screenshots, Reload und mpv-Neustart dauern Sekunden. In der Hauptschleife
    würden sie den Taster aufhalten. In „Aus“ läuft kein Stream, die Überwachung ruht.
    """

    def __init__(self, mpv: MPVController, views: kiosk.ViewController) -> None:
        super().__init__(daemon=True, name="stream-monitor")
        self.mpv, self.views = mpv, views
        self.fatal = threading.Event()   # Level 3: Hauptschleife beendet das Skript
        self.last_heartbeat = time.time()
        self._reset(time.time())

    def _reset(self, now: float) -> None:
        self.last_watchdog  = now
        self.last_badge     = now - BADGE_INTERVAL + 20  # ersten Check nach 20s
        self.frozen_count   = 0
        self.recovery_level = 0   # Eskalationsstufe (0=OK, 1=reload, 2=start, 3=Skript-Neustart)

    def _recover(self, reload_only: bool = False) -> None:
        wait_for_network(30)
        with self.mpv.lock:
            if self.views.view == "off":
                return
            if reload_only:
                self.mpv.reload()
            else:
                self.mpv.start()

    def run(self) -> None:
        paused = True
        while not self.fatal.is_set():
            try:
                now = time.time()
                if self.views.view in (None, "off"):
                    paused = True
                elif paused:
                    paused = False
                    self._reset(now)
                else:
                    self._step(now)
            except Exception as exc:  # noqa: BLE001 – Überwachung darf nie still sterben
                log_error("monitor", f"{type(exc).__name__}: {exc}")
                time.sleep(10)
            time.sleep(1)

    def _step(self, now: float) -> None:
        if now - self.last_heartbeat >= LOG_HEARTBEAT:
            self.last_heartbeat = now
            log.info(
                f"HEARTBEAT | Ansicht: {self.views.view} | mpv läuft: {self.mpv.is_running()} | "
                f"Restarts: {self.mpv._restarts} | Frozen-Count: {self.frozen_count}"
            )

        if now - self.last_watchdog >= WATCHDOG_INTERVAL:
            self.last_watchdog = now
            if not self.mpv.is_running():
                log_error("watchdog", "mpv-Prozess tot → Neustart")
                self._recover()
                self._reset(time.time())
                return
            if not self.mpv.is_playing():
                log_warn("watchdog", "Kein Playback → Stream neu laden")
                self._recover(reload_only=True)
                self._reset(time.time())
                return

        if now - self.last_badge >= BADGE_INTERVAL:
            self.last_badge = now
            self._check_frame()

    def _check_frame(self) -> None:
        # mpv dekodiert auch verdeckt weiter, der Check läuft in beiden Ansichten
        if not self.mpv.screenshot(SEN_SCREENSHOT):
            log_warn("badge", "Screenshot fehlgeschlagen")
            return
        if is_frame_frozen(SEN_SCREENSHOT, SEN_PREV_SHOT):
            self.frozen_count += 1
            log_warn("frozen", f"Frame eingefroren ({self.frozen_count}/{FROZEN_LIMIT})")
            if self.frozen_count >= FROZEN_LIMIT:
                self.frozen_count    = 0
                self.recovery_level += 1
                if self.recovery_level == 1:
                    log_warn("frozen", "Dauerhaft eingefroren → Stream neu laden (Level 1)")
                    self._recover(reload_only=True)
                elif self.recovery_level == 2:
                    log_warn("frozen", "Reload hat nicht geholfen → mpv Neustart (Level 2)")
                    self._recover()
                else:
                    log_error("frozen", "mpv Neustart hat nicht geholfen → "
                                        f"Script-Neustart via systemd (Level {self.recovery_level})")
                    self.fatal.set()
                return
        else:
            if self.recovery_level > 0:
                log_event("frozen", f"Stream wieder OK — Recovery-Level {self.recovery_level} → 0")
            self.frozen_count   = 0
            self.recovery_level = 0
            try:
                shutil.copy2(SEN_SCREENSHOT, SEN_PREV_SHOT)
            except OSError:
                pass
        log_event("badge", f"SEN-Modus: {detect_badge(SEN_SCREENSHOT)}")


# ── Hauptschleife ─────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("  ISS Watch gestartet")
    log.info(f"  Stream:     {SEN_STREAM}")
    log.info(f"  Nachtmodus: {CFG.sleep_start:02d}:00 – {CFG.sleep_end:02d}:00")
    log.info(f"  Plattform:  {'Wayland' if CFG.wayland else 'X11'}")
    if CFG.dashboard_url:
        cycle = ", ".join(f"{v} {s}s" for v, s in CFG.cycle) if CFG.auto_cycle else "aus"
        log.info(f"  Wetter:     {CFG.dashboard_url} | Wechsel: {cycle}")
    log.info("=" * 60)

    suppress_dialogs()
    wait_for_network()

    mpv = MPVController(MPV_SOCKET)
    display = Display(CFG)
    windows = kiosk.WlrctlWindows() if CFG.wayland else kiosk.NoWindows()
    browser = kiosk.Browser(CFG.dashboard_url)
    energy_browser = kiosk.Browser(CFG.energy_url, "energie-kiosk", "chromium-energie")
    views = kiosk.ViewController(CFG, display, windows, mpv, browser, log_event, time.time,
                                 energy_browser)
    events: "queue.Queue[float]" = queue.Queue()
    button = setup_button(events)  # noqa: F841 – Referenz halten, sonst kein Callback

    views.start(kiosk.load_resume(time.time()))
    monitor = StreamMonitor(mpv, views)
    monitor.start()

    last_guard = time.time()
    while True:
        try:
            # ── Taster, Nachtmodus, automatischer Wechsel ─────────────────────
            while not events.empty():
                pressed_at = events.get_nowait()
                if not views.press(pressed_at):
                    reason = "Stream lädt noch" if views.stream_loading() else "Preller oder laufender Wechsel"
                    log_event("taster", f"Druck ignoriert ({reason})")
            views.tick()

            now = time.time()

            if monitor.fatal.is_set():
                kiosk.save_resume(views.view)
                sys.exit(1)   # systemd Restart=always startet Script neu

            # ── Browser am Leben halten ──────────────────────────────────────
            if views.view != "off" and CFG.dashboard_url and not browser.is_running():
                log_warn("browser", "Chromium beendet → Neustart")
                browser.start()
            if views.view != "off" and CFG.energy_url and not energy_browser.is_running():
                log_warn("browser", "Chromium Energie beendet → Neustart")
                energy_browser.start()

            # ── Bildschirm zeigt, was die Ansicht sagt ────────────────────────
            if now - last_guard >= GUARD_INTERVAL:
                last_guard = now
                views.guard()

            time.sleep(0.1)

        except KeyboardInterrupt:
            log.info("Beendet (Ctrl+C)")
            break
        except Exception as exc:
            log_error("main", f"{type(exc).__name__}: {exc}")
            time.sleep(10)

    mpv.kill()
    browser.stop()
    log.info("=" * 60)
    log.info("  ISS Watch beendet")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
