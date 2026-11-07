"""
Kiosk-Ansichten für ISS Watch: ISS-Stream, Wetter-Dashboard, Spreefeld Energie und Aus.

- Konfiguration aus ~/.config/iss-watch/config.toml (alle Werte optional)
- Plattform-Adapter: Bildschirm an/aus (vcgencmd | wlr-randr) und
  Fenster nach vorn holen (wlrctl unter Wayland)
- ViewController: automatischer Wechsel (z. B. 60 s ISS, 30 s Wetter),
  Taster (ISS → Wetter → Energie → ISS, Doppelklick Aus) und Nachtmodus mit gemerkter Ansicht
"""
import os
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from datetime import datetime

VIEWS = ("iss", "weather", "energy", "off")
CONFIG_PATH = os.path.expanduser("~/.config/iss-watch/config.toml")
# Ansicht über einen Skript-Neustart retten. Liegt im flüchtigen Laufzeitordner:
# Nach einem Neustart des Pi gilt wieder start_view.
RESUME_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "iss-watch.resume")
RESUME_MAX_AGE = 120


# ── Konfiguration ─────────────────────────────────────────────────────────────

@dataclass
class Config:
    stream_url: str = "https://www.youtube.com/@sen/live"
    cookies: str = "~/projects/iss-watch/yt-cookies.txt"
    ytdl_path: str = ""                      # leer = yt-dlp aus PATH
    disable_wifi: bool = True                # alter iss-Pi: WLAN aus; wetterraspi5: false
    display_backend: str = "auto"            # auto | vcgencmd | wlr-randr | ddc
    ddc_bus: int = 13                        # I²C-Bus des Monitors (ddcutil detect)
    output: str = "HDMI-A-1"
    output_transform: str = "normal"         # wetterraspi5: "180" (kanshi)
    sleep_start: int = 1
    sleep_end: int = 6
    dashboard_url: str = ""                  # leer = keine Wetteransicht
    energy_url: str = ""                     # leer = keine Energieansicht
    start_view: str = "iss"
    auto_cycle: bool = False
    cycle: list = field(default_factory=lambda: [["iss", 60], ["weather", 30]])
    button_gpio: int = 0                     # 0 = kein Taster

    @property
    def wayland(self) -> bool:
        if self.display_backend == "auto":
            return bool(os.environ.get("WAYLAND_DISPLAY"))
        return self.display_backend in ("wlr-randr", "ddc")


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

    def is_on(self) -> bool:
        res = subprocess.run(["vcgencmd", "display_power"], capture_output=True, text=True)
        return res.stdout.strip().endswith("=1")


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

    def is_on(self) -> bool:
        res = subprocess.run(["wlr-randr"], capture_output=True, text=True)
        current = None
        for line in res.stdout.splitlines():
            if line and not line[0].isspace():
                current = line.split()[0]
            elif current == self.output and line.strip().startswith("Enabled:"):
                return line.split(":", 1)[1].strip() == "yes"
        return False


class DdcDisplay:
    """Monitor per DDC in Standby schicken (VCP D6). Das HDMI-Signal bleibt an.

    Ohne Signal zeigt der Monitor am wetterraspi5 einen blauen Bildschirm, statt zu
    schlafen. CEC beantwortet er nicht. Ein Befehl dauert etwa eine Sekunde.
    """

    ON, STANDBY = "1", "4"

    def __init__(self, bus: int) -> None:
        self.bus = str(bus)

    def _set(self, value: str) -> None:
        subprocess.run(["ddcutil", "--bus", self.bus, "setvcp", "D6", value],
                       capture_output=True, timeout=15)

    def on(self) -> None:
        self._set(self.ON)

    def off(self) -> None:
        self._set(self.STANDBY)

    def is_on(self) -> bool:
        res = subprocess.run(["ddcutil", "--bus", self.bus, "getvcp", "D6"],
                             capture_output=True, text=True, timeout=15)
        return "DPM: On" in res.stdout


class WlrctlWindows:
    """Holt ein Fenster per App-ID nach vorn (wlr-foreign-toplevel)."""

    APP_IDS = {"iss": "iss-mpv", "weather": "wetter-kiosk", "energy": "energie-kiosk"}

    def focus(self, view: str) -> bool:
        res = subprocess.run(["wlrctl", "toplevel", "focus", f"app_id:{self.APP_IDS[view]}"],
                             capture_output=True)
        return res.returncode == 0

    def is_active(self, view: str) -> bool:
        res = subprocess.run(["wlrctl", "toplevel", "find", f"app_id:{self.APP_IDS[view]}",
                              "state:active"], capture_output=True)
        return res.returncode == 0


class NoWindows:
    """X11 ohne Wetteransicht: mpv ist das einzige Fenster."""

    def focus(self, view: str) -> bool:
        return view == "iss"

    def is_active(self, view: str) -> bool:
        return True


class Browser:
    """Chromium im Kiosk-Modus mit einer lokalen Seite. Je Fenster ein eigenes Profil,
    sonst führt Chromium die Instanzen zu einer zusammen und die App-ID geht verloren."""

    def __init__(self, url: str, app_id: str = "wetter-kiosk", profile: str = "chromium") -> None:
        self.url, self.app_id = url, app_id
        self.proc = None
        self.profile = os.path.expanduser(f"~/.local/share/iss-watch/{profile}")

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        exe = shutil.which("chromium") or shutil.which("chromium-browser") or "chromium"
        cmd = [
            exe, "--kiosk", "--ozone-platform=wayland", f"--class={self.app_id}",
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
    - Taster: Klick wechselt reihum ISS → Wetter → Energie, Doppelklick schaltet aus, ein Klick
      in „Aus“ schaltet wieder ein (zuletzt gezeigte Ansicht). Ein Druck bis 120 ms nach
      dem letzten Druck gilt als Prellen, ein Druck während eines laufenden Wechsels wird
      verworfen. Maßgeblich ist der Zeitpunkt des Drucks, nicht der Verarbeitung: ein
      Wechsel kann Sekunden dauern (mpv-Start).
    - Solange der Stream in der Ansicht ISS noch lädt, nimmt der Taster nichts an
      (höchstens STREAM_WAIT Sekunden, damit man ohne Stream nicht festhängt).
    - Nachtmodus: um sleep_start auf „Aus“, um sleep_end zurück zur gemerkten
      Ansicht. Ein Tastendruck in der Nacht hebt den Nachtmodus für diese Nacht
      auf: die gewählte Ansicht bleibt, auch über sleep_end hinaus.
    - guard(): prüft laufend, ob der Bildschirm wirklich die Ansicht zeigt. Ein neu
      geöffnetes mpv-Fenster legt sich sonst unbemerkt vor das Wetter.
    """

    BOUNCE = 0.12
    DOUBLE_CLICK = 0.5
    STREAM_WAIT = 30
    OFF_CHECK = 30    # Sekunden zwischen Prüfungen in „Aus“ (DDC ist langsam)

    def __init__(self, cfg: Config, display, windows, mpv, browser, log_event, now,
                 energy_browser=None) -> None:
        self.cfg, self.display, self.windows = cfg, display, windows
        self.mpv, self.browser, self.log_event, self.now = mpv, browser, log_event, now
        self.view = None
        self.since = 0.0
        self.last_press = float("-inf")
        self.pending_click = None
        self.last_view = cfg.start_view if cfg.start_view != "off" else "iss"
        self.busy_until = float("-inf")
        self.last_off_check = float("-inf")
        self.night = False
        self.night_override = False
        self.remembered = cfg.start_view
        self.has_weather = bool(cfg.dashboard_url)
        self.energy_browser = energy_browser
        self.has_energy = bool(cfg.energy_url and energy_browser)

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
        if view == "energy" and not self.has_energy:
            view = "weather"
        if view == "weather" and not self.has_weather:
            view = "iss"
        if view == self.view:
            return
        previous, self.view, self.since = self.view, view, self.now()
        if view != "off":
            self.last_view = view
        if view == "off":
            self.mpv.kill()
            self.display.off()
        else:
            if previous in (None, "off"):
                self.display.on()
            if self.has_weather and not self.browser.is_running():
                self.browser.start()
            if self.has_energy and not self.energy_browser.is_running():
                self.energy_browser.start()
            if not self.mpv.is_running():
                self.mpv.start()
            self.windows.focus(view)
        self.busy_until = self.now()
        self.log_event("view", f"{previous or '-'} → {view} ({reason})")

    def start(self, resume: str = None) -> None:
        """resume: Ansicht vor einem Skript-Neustart, sonst gilt start_view."""
        sleep = self.is_sleep_time()
        if resume:
            self.night = sleep
            self.night_override = sleep and resume != "off"
            self.set_view(resume, "Fortsetzung nach Skript-Neustart")
        elif sleep:
            self.night = True
            self.set_view("off", "Start im Nachtmodus")
        else:
            self.set_view(self.cfg.start_view, "Start")

    def press(self, at: float = None) -> bool:
        """Taster, at = Zeitpunkt des Drucks (aus dem GPIO-Callback).

        In „Aus“ schaltet ein Klick sofort wieder ein. Sonst entscheidet tick(), ob es
        beim einfachen Klick bleibt, ein zweiter Klick im Zeitfenster schaltet aus.
        Gibt False zurück, wenn der Druck verworfen wurde.
        """
        at = self.now() if at is None else at
        if at - self.last_press < self.BOUNCE or at < self.busy_until:
            return False
        if self.stream_loading():
            return False
        self.last_press = at
        if self.night and not self.night_override:
            self.night_override = True
            self.log_event("view", "Nachtmodus für diese Nacht aufgehoben (Taster)")
        if self.view == "off":
            self.set_view(self.last_view, "Klick")
        elif self.pending_click is not None and at - self.pending_click <= self.DOUBLE_CLICK:
            self.pending_click = None
            self.set_view("off", "Doppelklick")
        else:
            self.pending_click = at
        return True

    def _finish_click(self) -> None:
        """Kein zweiter Klick im Zeitfenster: zur nächsten Ansicht wechseln."""
        if self.pending_click is None or self.now() - self.pending_click <= self.DOUBLE_CLICK:
            return
        self.pending_click = None
        order = ["iss", "weather"] + (["energy"] if self.has_energy else [])
        following = order[(order.index(self.view) + 1) % len(order)] if self.view in order else "weather"
        self.set_view(following, "Klick")

    def stream_loading(self) -> bool:
        """True, solange in der Ansicht ISS der Stream noch nicht abspielt."""
        return (self.view == "iss" and self.now() - self.since < self.STREAM_WAIT
                and not self.mpv.is_ready())

    def tick(self) -> None:
        self._finish_click()
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

    def guard(self) -> None:
        """Bringt den Bildschirm wieder in Einklang mit der Ansicht."""
        if self.view == "off":
            if self.now() - self.last_off_check < self.OFF_CHECK:
                return
            self.last_off_check = self.now()
            if self.display.is_on():
                self.display.off()
                self.log_event("guard", "Bildschirm war an, obwohl Ansicht Aus → wieder aus")
        elif self.view is not None and not self.windows.is_active(self.view):
            if self.windows.focus(self.view):
                self.log_event("guard", f"Falsches Fenster vorn → {self.view} geholt")


def save_resume(view: str) -> None:
    """Merkt die Ansicht vor einem gewollten Skript-Neustart."""
    with open(RESUME_PATH, "w", encoding="utf-8") as f:
        f.write(view)


def load_resume(now: float, path: str = None) -> str:
    """Liest die gemerkte Ansicht einmalig. Zu alte Einträge gelten nicht."""
    path = path or RESUME_PATH
    try:
        with open(path, encoding="utf-8") as f:
            view = f.read().strip()
        age = now - os.path.getmtime(path)
        os.remove(path)
    except OSError:
        return None
    return view if view in VIEWS and age < RESUME_MAX_AGE else None
