"""
Kiosk-Ansichten für ISS Watch: ISS-Stream, Wetter-Dashboard und Aus.

- Konfiguration aus ~/.config/iss-watch/config.toml (alle Werte optional)
- Plattform-Adapter: Bildschirm an/aus (vcgencmd | wlr-randr) und
  Fenster nach vorn holen (wlrctl unter Wayland)
- ViewController: automatischer Wechsel (z. B. 60 s ISS, 30 s Wetter),
  Taster (ISS → Wetter → Aus → ISS) und Nachtmodus mit gemerkter Ansicht
"""
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime

VIEWS = ("iss", "weather", "off")
CONFIG_PATH = os.path.expanduser("~/.config/iss-watch/config.toml")


# ── Konfiguration ─────────────────────────────────────────────────────────────

@dataclass
class Config:
    stream_url: str = "https://www.youtube.com/@sen/live"
    cookies: str = "~/projects/iss-watch/yt-cookies.txt"
    ytdl_path: str = ""                      # leer = yt-dlp aus PATH
    disable_wifi: bool = True                # alter iss-Pi: WLAN aus; wetterraspi5: false
    display_backend: str = "auto"            # auto | vcgencmd | wlr-randr
    output: str = "HDMI-A-1"
    output_transform: str = "normal"         # wetterraspi5: "180" (kanshi)
    sleep_start: int = 1
    sleep_end: int = 6
    dashboard_url: str = ""                  # leer = keine Wetteransicht
    start_view: str = "iss"
    auto_cycle: bool = False
    cycle: list = field(default_factory=lambda: [["iss", 60], ["weather", 30]])
    button_gpio: int = 0                     # 0 = kein Taster

    @property
    def wayland(self) -> bool:
        if self.display_backend == "auto":
            return bool(os.environ.get("WAYLAND_DISPLAY"))
        return self.display_backend == "wlr-randr"


def load_config(path: str = CONFIG_PATH) -> Config:
    cfg = Config()
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = tomllib.load(f)
        for key, value in data.items():
            if not hasattr(cfg, key):
                raise ValueError(f"Unbekannte Option in {path}: {key}")
            setattr(cfg, key, value)
    cfg.cookies = os.path.expanduser(cfg.cookies)
    if cfg.start_view not in VIEWS:
        raise ValueError(f"start_view muss eine von {VIEWS} sein")
    for view, seconds in cfg.cycle:
        if view not in ("iss", "weather") or seconds <= 0:
            raise ValueError(f"Ungültiger Eintrag in cycle: {view}, {seconds}")
    return cfg


# ── Plattform-Adapter ─────────────────────────────────────────────────────────

class VcgencmdDisplay:
    """Raspberry Pi 3 / X11: HDMI-Signal per vcgencmd."""

    def on(self) -> None:
        subprocess.run(["vcgencmd", "display_power", "1"], capture_output=True)

    def off(self) -> None:
        subprocess.run(["vcgencmd", "display_power", "0"], capture_output=True)


class WlrRandrDisplay:
    """Wayland (labwc): Ausgang per wlr-randr. Die Drehung muss beim Einschalten
    mitgegeben werden, sonst steht das Bild auf dem Kopf."""

    def __init__(self, output: str, transform: str) -> None:
        self.output, self.transform = output, transform

    def on(self) -> None:
        subprocess.run(["wlr-randr", "--output", self.output, "--on",
                        "--transform", self.transform], capture_output=True)

    def off(self) -> None:
        subprocess.run(["wlr-randr", "--output", self.output, "--off"], capture_output=True)


class WlrctlWindows:
    """Holt ein Fenster per App-ID nach vorn (wlr-foreign-toplevel)."""

    APP_IDS = {"iss": "iss-mpv", "weather": "wetter-kiosk"}

    def focus(self, view: str) -> bool:
        res = subprocess.run(["wlrctl", "toplevel", "focus", f"app_id:{self.APP_IDS[view]}"],
                             capture_output=True)
        return res.returncode == 0


class NoWindows:
    """X11 ohne Wetteransicht: mpv ist das einzige Fenster."""

    def focus(self, view: str) -> bool:
        return view == "iss"


class Browser:
    """Chromium im Kiosk-Modus mit dem lokalen Wetter-Dashboard."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.proc = None
        self.profile = os.path.expanduser("~/.local/share/iss-watch/chromium")

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        exe = shutil.which("chromium") or shutil.which("chromium-browser") or "chromium"
        cmd = [
            exe, "--kiosk", "--ozone-platform=wayland", "--class=wetter-kiosk",
            f"--user-data-dir={self.profile}", "--noerrdialogs", "--disable-infobars",
            "--no-first-run", "--disable-session-crashed-bubble", "--password-store=basic",
            "--disable-features=Translate,TranslateUI", "--lang=de", "--disable-pinch",
            "--overscroll-history-navigation=0", "--check-for-update-interval=31536000",
            self.url,
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        if self.is_running():
            self.proc.terminate()
        self.proc = None


# ── Ansichten ─────────────────────────────────────────────────────────────────

class ViewController:
    """Zustandsautomat der Ansichten.

    - Startansicht aus der Konfiguration (wetterraspi5: Wetter)
    - auto_cycle (optional): wechselt nach den Dauern in `cycle`, solange die
      Ansicht ISS oder Wetter ist. Auf dem wetterraspi5 aus (nur Taster).
    - Taster: ISS → Wetter → Aus → ISS. Ein Druck bis 500 ms nach dem letzten
      angenommenen Druck wird ignoriert (Prellen). Maßgeblich ist der Zeitpunkt
      des Drucks, nicht der Verarbeitung: ein Wechsel kann Sekunden dauern
      (mpv-Start), Preller warten so lange in der Queue.
    - Solange der Stream in der Ansicht ISS noch lädt, nimmt der Taster nichts an
      (höchstens STREAM_WAIT Sekunden, damit man ohne Stream nicht festhängt).
    - Nachtmodus: um sleep_start auf „Aus“, um sleep_end zurück zur gemerkten
      Ansicht. Ein Tastendruck in der Nacht hebt den Nachtmodus für diese Nacht
      auf: die gewählte Ansicht bleibt, auch über sleep_end hinaus.
    """

    LOCKOUT = 0.5
    STREAM_WAIT = 30

    def __init__(self, cfg: Config, display, windows, mpv, browser, log_event, now) -> None:
        self.cfg, self.display, self.windows = cfg, display, windows
        self.mpv, self.browser, self.log_event, self.now = mpv, browser, log_event, now
        self.view = None
        self.since = 0.0
        self.last_press = float("-inf")
        self.night = False
        self.night_override = False
        self.remembered = cfg.start_view
        self.has_weather = bool(cfg.dashboard_url)

    # Hilfen
    def _durations(self) -> dict:
        return {view: seconds for view, seconds in self.cfg.cycle}

    def _next_in_cycle(self, view: str) -> str:
        order = [v for v, _ in self.cfg.cycle]
        if view not in order:
            return order[0]
        return order[(order.index(view) + 1) % len(order)]

    def is_sleep_time(self) -> bool:
        h = datetime.fromtimestamp(self.now()).hour
        return self.cfg.sleep_start <= h < self.cfg.sleep_end

    # Ansicht setzen
    def set_view(self, view: str, reason: str) -> None:
        if view == "weather" and not self.has_weather:
            view = "iss"
        if view == self.view:
            return
        previous, self.view, self.since = self.view, view, self.now()
        if view == "off":
            self.mpv.kill()
            self.display.off()
        else:
            if previous in (None, "off"):
                self.display.on()
            if self.has_weather and not self.browser.is_running():
                self.browser.start()
            if not self.mpv.is_running():
                self.mpv.start()
            self.windows.focus(view)
        self.log_event("view", f"{previous or '-'} → {view} ({reason})")

    def start(self) -> None:
        if self.is_sleep_time():
            self.night = True
            self.set_view("off", "Start im Nachtmodus")
        else:
            self.set_view(self.cfg.start_view, "Start")

    def press(self, at: float = None) -> bool:
        """Taster: zur nächsten Ansicht. `at` = Zeitpunkt des Drucks (aus dem
        GPIO-Callback). Gibt False zurück, wenn der Druck als Preller verworfen wurde."""
        at = self.now() if at is None else at
        if at - self.last_press < self.LOCKOUT:
            return False
        if self.stream_loading():
            return False
        self.last_press = at
        if self.night and not self.night_override:
            self.night_override = True
            self.log_event("view", "Nachtmodus für diese Nacht aufgehoben (Taster)")
        order = list(VIEWS) if self.has_weather else ["iss", "off"]
        nxt = order[(order.index(self.view) + 1) % len(order)]
        self.set_view(nxt, "Taster")
        return True

    def stream_loading(self) -> bool:
        """True, solange in der Ansicht ISS der Stream noch nicht abspielt."""
        return (self.view == "iss" and self.now() - self.since < self.STREAM_WAIT
                and not self.mpv.is_ready())

    def tick(self) -> None:
        sleep = self.is_sleep_time()
        if sleep and not self.night:
            self.night = True
            self.remembered = self.view if self.view != "off" else self.cfg.start_view
            self.set_view("off", "Nachtmodus")
            return
        if not sleep and self.night:
            self.night = False
            if self.night_override:
                # In der Nacht von Hand geschaltet: Ansicht bleibt wie gewählt
                self.night_override = False
                return
            self.set_view(self.remembered, "Tagmodus")
            return
        if self.cfg.auto_cycle and self.has_weather and self.view in ("iss", "weather"):
            duration = self._durations().get(self.view)
            if duration and self.now() - self.since >= duration:
                self.set_view(self._next_in_cycle(self.view), "automatisch")

    def refocus(self) -> None:
        """Fenster erneut nach vorn holen (z. B. nach einem mpv-Neustart)."""
        if self.view in ("iss", "weather"):
            self.windows.focus(self.view)
