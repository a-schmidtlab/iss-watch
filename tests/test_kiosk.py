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

    def on(self): self.calls.append("on")
    def off(self): self.calls.append("off")
    def focus(self, view): self.calls.append(f"focus:{view}"); return True
    def is_running(self): return self.running
    def start(self): self.running = True; self.calls.append("start")
    def kill(self): self.running = False; self.calls.append("kill")


def make(hour=12, **cfg):
    config = kiosk.Config(dashboard_url="http://127.0.0.1:8080/kiosk.html", start_view="weather",
                          auto_cycle=True, cycle=[["iss", 60], ["weather", 30]], **cfg)
    clock, display, windows, mpv, browser = Clock(hour), Fake(), Fake(), Fake(), Fake()
    events = []
    vc = kiosk.ViewController(config, display, windows, mpv, browser,
                              lambda cat, msg: events.append(msg), clock)
    return vc, clock, display, windows, mpv, browser


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


def test_button_cycles_iss_weather_off():
    vc, clock, display, windows, mpv, browser = make()
    vc.start()
    clock.advance(1); vc.press()
    assert vc.view == "off" and not mpv.running and display.calls[-1] == "off"
    clock.advance(1); vc.press()
    assert vc.view == "iss" and mpv.running and display.calls[-1] == "on"
    clock.advance(1); vc.press()
    assert vc.view == "weather"


def test_button_lockout_300ms():
    vc, clock, *_ = make()
    vc.start()
    clock.advance(1); vc.press()
    assert vc.view == "off"
    clock.advance(0.1); vc.press()   # Prellen / Doppeldruck
    assert vc.view == "off"


def test_off_pauses_auto_cycle():
    vc, clock, *_ = make()
    vc.start()
    clock.advance(1); vc.press()      # Aus
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
    vc, clock, *_ = make(hour=3)
    vc.start()
    assert vc.view == "off"
    clock.advance(1); vc.press()
    assert vc.view == "iss"


def test_without_dashboard_only_iss_and_off():
    config = kiosk.Config(start_view="iss")
    clock, f = Clock(), Fake()
    vc = kiosk.ViewController(config, Fake(), Fake(), Fake(), Fake(), lambda *a: None, clock)
    vc.start()
    assert vc.view == "iss"
    clock.advance(1); vc.press()
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
