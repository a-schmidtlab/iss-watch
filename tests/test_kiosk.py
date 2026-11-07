"""Tests für den Zustandsautomaten der Ansichten (ohne Hardware)."""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kiosk  # noqa: E402


class Clock:
    def __init__(self, hour=12):
        self.t = datetime(2026, 11, 3, hour, 0, 0).timestamp()

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Fake:
    def __init__(self):
        self.calls = []
        self.running = False
        self.ready = True
        self.active = None

    def is_ready(self): return self.running and self.ready
    def is_on(self): return "off" not in self.calls[-1:]
    def is_active(self, view): return self.active == view

    def on(self): self.calls.append("on")
    def off(self): self.calls.append("off")
    def focus(self, view): self.calls.append(f"focus:{view}"); return True
    def is_running(self): return self.running
    def start(self): self.running = True; self.calls.append("start")
    def kill(self): self.running = False; self.calls.append("kill")


def make(hour=12, auto_cycle=True, **cfg):
    config = kiosk.Config(dashboard_url="http://127.0.0.1:8080/kiosk.html", start_view="weather",
                          auto_cycle=auto_cycle, cycle=[["iss", 60], ["weather", 30]], **cfg)
    clock, display, windows, mpv, browser = Clock(hour), Fake(), Fake(), Fake(), Fake()
    events = []
    vc = kiosk.ViewController(config, display, windows, mpv, browser,
                              lambda cat, msg: events.append(msg), clock, Fake())
    return vc, clock, display, windows, mpv, browser


def click(vc, clock):
    """Einfacher Klick: Druck, dann läuft das Doppelklick-Fenster ab."""
    clock.advance(1)
    assert vc.press()
    clock.advance(0.6)
    vc.tick()


def double(vc, clock):
    clock.advance(1)
    assert vc.press()
    clock.advance(0.3)
    assert vc.press()


def test_start_shows_weather_with_stream_running_behind():
    vc, clock, display, windows, mpv, browser = make()
    vc.start()
    assert vc.view == "weather"
    assert mpv.running and browser.running
    assert windows.calls[-1] == "focus:weather"
    assert display.calls == ["on"]


def test_auto_cycle_60s_iss_30s_weather():
    vc, clock, *_ = make()
    vc.start()                        # Wetter
    clock.advance(29); vc.tick()
    assert vc.view == "weather"
    clock.advance(1); vc.tick()
    assert vc.view == "iss"
    clock.advance(59); vc.tick()
    assert vc.view == "iss"
    clock.advance(1); vc.tick()
    assert vc.view == "weather"


def test_click_toggles_iss_and_weather():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    vc.start()
    click(vc, clock)
    assert vc.view == "iss" and mpv.running
    click(vc, clock)
    assert vc.view == "weather"
    click(vc, clock)
    assert vc.view == "iss"


def test_click_waits_for_double_click_window():
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    clock.advance(1); vc.press()
    clock.advance(0.3); vc.tick()
    assert vc.view == "weather"
    clock.advance(0.3); vc.tick()
    assert vc.view == "iss"


def test_double_click_turns_off_and_click_turns_on_last_view():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    vc.start()
    click(vc, clock)                         # ISS
    double(vc, clock)
    assert vc.view == "off" and not mpv.running and display.calls[-1] == "off"
    clock.advance(1); vc.tick()
    assert vc.view == "off"                  # bleibt aus, kein Klick hängt nach
    clock.advance(1); vc.press()
    assert vc.view == "iss" and mpv.running and display.calls[-1] == "on"


def test_double_click_from_weather_returns_to_weather():
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    double(vc, clock)
    assert vc.view == "off"
    clock.advance(1); vc.press()
    assert vc.view == "weather"


def test_two_slow_clicks_are_not_a_double_click():
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    click(vc, clock)
    click(vc, clock)
    assert vc.view == "weather"

def test_bounce_is_ignored():
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    clock.advance(1); vc.press()
    clock.advance(0.05); assert vc.press() is False   # Prellen, kein Doppelklick
    clock.advance(0.6); vc.tick()
    assert vc.view == "iss"

def test_bounce_queued_during_slow_switch_is_ignored():
    """Preller kommt 50 ms nach dem Druck, wird aber erst nach dem langsamen
    Wechsel (mpv-Start, 3 s) verarbeitet: er darf nicht weiterschalten."""
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    double(vc, clock)                       # → Aus
    pressed = clock.t + 1
    clock.t = pressed; vc.press(pressed)    # → Wetter (Wechsel dauert)
    clock.advance(3)
    assert vc.press(pressed + 0.05) is False
    assert vc.view == "weather"

def test_off_pauses_auto_cycle():
    vc, clock, *_ = make()
    vc.start()
    double(vc, clock)                       # Aus
    clock.advance(600); vc.tick()
    assert vc.view == "off"

def test_night_mode_remembers_view():
    vc, clock, display, windows, mpv, browser = make(hour=0)
    clock.t = datetime(2026, 11, 3, 0, 59, 50).timestamp()
    vc.start()
    clock.advance(5); vc.tick()
    assert vc.view == "weather"
    clock.advance(10); vc.tick()      # 01:00:05
    assert vc.view == "off" and not mpv.running
    clock.t = datetime(2026, 11, 3, 6, 0, 1).timestamp(); vc.tick()
    assert vc.view == "weather" and mpv.running


def test_button_works_at_night():
    vc, clock, *_ = make(hour=3, auto_cycle=False)
    vc.start()
    assert vc.view == "off"
    clock.advance(1); vc.press()
    assert vc.view == "weather"

def test_without_dashboard_click_stays_on_iss():
    config = kiosk.Config(start_view="iss")
    clock, mpv = Clock(), Fake()
    vc = kiosk.ViewController(config, Fake(), Fake(), mpv, Fake(), lambda *a: None, clock)
    vc.start()
    click(vc, clock)
    assert vc.view == "iss"
    double(vc, clock)
    assert vc.view == "off"
    clock.advance(1); vc.press()
    assert vc.view == "iss"

def test_load_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('dashboard_url = "http://x/"\nauto_cycle = true\ncycle = [["iss", 60], ["weather", 30]]\n'
                    'disable_wifi = false\nbutton_gpio = 17\n', encoding="utf-8")
    cfg = kiosk.load_config(str(path))
    assert cfg.auto_cycle and cfg.button_gpio == 17 and not cfg.disable_wifi
    path.write_text("unbekannt = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unbekannt"):
        kiosk.load_config(str(path))
    assert kiosk.load_config(str(tmp_path / "fehlt.toml")).start_view == "iss"


def test_manual_only_no_auto_switch():
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    clock.advance(3600); vc.tick()
    assert vc.view == "weather"


def test_presses_ignored_while_stream_loads():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    vc.start()
    mpv.ready = False
    click(vc, clock)                         # → ISS, Stream lädt
    assert vc.view == "iss"
    clock.advance(5); assert vc.press() is False  # noch kein Bild
    mpv.ready = True
    click(vc, clock)
    assert vc.view == "weather"

def test_stream_wait_is_limited():
    """Kommt der Stream nie (kein Internet), geht es nach 30 s trotzdem weiter."""
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    vc.start()
    mpv.ready = False
    click(vc, clock)                         # ISS
    clock.advance(31)
    click(vc, clock)
    assert vc.view == "weather"

def test_press_at_night_cancels_night_mode_for_this_night():
    vc, clock, display, windows, mpv, browser = make(hour=2, auto_cycle=False)
    vc.start()
    assert vc.view == "off"
    clock.advance(1); vc.press()                 # an um 02:00 → Wetter
    assert vc.view == "weather"
    clock.advance(600); vc.tick()                # weiter Nacht: bleibt an
    assert vc.view == "weather"
    click(vc, clock)                             # → ISS
    clock.t = datetime(2026, 11, 3, 6, 0, 5).timestamp(); vc.tick()
    assert vc.view == "iss"                      # 06:00: gewählte Ansicht bleibt
    clock.t = datetime(2026, 11, 4, 1, 0, 5).timestamp(); vc.tick()
    assert vc.view == "off"                      # nächste Nacht wieder normal
    clock.t = datetime(2026, 11, 4, 6, 0, 5).timestamp(); vc.tick()
    assert vc.view == "iss"

def test_night_off_by_hand_stays_off_in_the_morning():
    vc, clock, display, windows, mpv, browser = make(hour=2, auto_cycle=False)
    vc.start()
    clock.advance(1); vc.press()                 # an
    double(vc, clock)                            # wieder aus
    assert vc.view == "off"
    clock.t = datetime(2026, 11, 3, 6, 0, 5).timestamp(); vc.tick()
    assert vc.view == "off"

def test_press_during_running_switch_is_dropped():
    """Wer während eines langsamen Wechsels drückt, schaltet nicht hinterher weiter."""
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    click(vc, clock)                        # → ISS
    vc.busy_until = clock.t + 3             # Wechsel dauert noch
    assert vc.press(clock.t + 1) is False
    assert vc.view == "iss"

def test_guard_brings_back_the_right_window():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    events = []
    vc.log_event = lambda cat, msg: events.append(cat)
    vc.start()
    windows.active = "iss"                  # mpv hat sich vor das Wetter gelegt
    windows.calls.clear()
    vc.guard()
    assert windows.calls == ["focus:weather"] and "guard" in events
    windows.active = "weather"
    windows.calls.clear()
    vc.guard()
    assert windows.calls == []


def test_guard_keeps_display_off():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    vc.start()
    double(vc, clock)                       # → Aus
    display.calls.append("on")              # Monitor kam von selbst zurück
    vc.guard()
    assert display.calls[-1] == "off"

def test_resume_after_script_restart():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    vc.start("iss")
    assert vc.view == "iss"


def test_resume_at_night_keeps_override():
    vc, clock, display, windows, mpv, browser = make(hour=3, auto_cycle=False)
    vc.start("weather")
    assert vc.view == "weather" and vc.night and vc.night_override
    clock.advance(600); vc.tick()
    assert vc.view == "weather"


def test_load_resume_only_once_and_not_stale(tmp_path):
    path = str(tmp_path / "resume")
    with open(path, "w") as f:
        f.write("iss")
    now = os.path.getmtime(path)
    assert kiosk.load_resume(now + 5, path) == "iss"
    assert kiosk.load_resume(now + 5, path) is None
    with open(path, "w") as f:
        f.write("iss")
    assert kiosk.load_resume(os.path.getmtime(path) + 500, path) is None


def test_guard_checks_display_only_every_30s_in_off():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    checks = []
    display.is_on = lambda: checks.append(1) or False
    vc.start()
    double(vc, clock)                       # → Aus
    vc.guard(); vc.guard()
    clock.advance(5); vc.guard()
    assert len(checks) == 1
    clock.advance(30); vc.guard()
    assert len(checks) == 2


def test_ddc_backend_keeps_wayland_windows():
    assert kiosk.Config(display_backend="ddc").wayland


ENERGY = "http://127.0.0.1:8080/energie-kiosk.html"


def click(vc, clock):
    clock.advance(1)
    assert vc.press(clock())
    clock.advance(vc.DOUBLE_CLICK + 0.1)
    vc.tick()


def test_click_cycles_through_all_three_views():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False, energy_url=ENERGY)
    vc.start()
    assert vc.energy_browser.running
    seen = []
    for _ in range(3):
        click(vc, clock)
        seen.append(vc.view)
        mpv.ready = True
        windows.calls.clear()
    assert seen == ["energy", "iss", "weather"]


def test_without_energy_url_click_stays_two_way():
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    assert vc.energy_browser.running is False
    click(vc, clock)
    assert vc.view == "iss"
    clock.advance(40)
    click(vc, clock)
    assert vc.view == "weather"


def test_double_click_from_energy_turns_off_and_click_returns():
    vc, clock, display, *_ = make(auto_cycle=False, energy_url=ENERGY)
    vc.start()
    click(vc, clock)
    assert vc.view == "energy"
    clock.advance(1)
    vc.press(clock())
    clock.advance(0.2)
    vc.press(clock())
    assert vc.view == "off"
    clock.advance(1)
    vc.press(clock())
    assert vc.view == "energy"


def test_night_returns_to_energy():
    vc, clock, *_ = make(auto_cycle=False, energy_url=ENERGY)
    vc.start()
    click(vc, clock)
    clock.t = datetime(2026, 11, 4, 2, 0, 0).timestamp()
    vc.tick()
    assert vc.view == "off"
    clock.t = datetime(2026, 11, 4, 7, 0, 0).timestamp()
    vc.tick()
    assert vc.view == "energy"


def test_energy_view_without_url_falls_back():
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    vc.set_view("energy", "Test")
    assert vc.view == "weather"


def test_resume_accepts_energy(tmp_path):
    path = tmp_path / "resume"
    path.write_text("energy", encoding="utf-8")
    assert kiosk.load_resume(os.path.getmtime(path), str(path)) == "energy"
