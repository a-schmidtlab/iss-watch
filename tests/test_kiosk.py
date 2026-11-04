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

    def is_ready(self): return self.running and self.ready

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


def test_button_lockout_500ms():
    vc, clock, *_ = make()
    vc.start()
    clock.advance(1); vc.press()
    assert vc.view == "off"
    clock.advance(0.1); assert vc.press() is False   # Prellen / Doppeldruck
    assert vc.view == "off"
    clock.advance(0.6); vc.press()
    assert vc.view == "iss"


def test_bounce_queued_during_slow_switch_is_ignored():
    """Preller kommt 80 ms nach dem Druck, wird aber erst nach dem langsamen
    Wechsel (mpv-Start, 3 s) verarbeitet: er darf nicht weiterschalten."""
    vc, clock, *_ = make()
    vc.start()                              # Wetter
    clock.advance(5); vc.press()            # → Aus
    pressed = clock.t + 1
    clock.t = pressed; vc.press(pressed)    # → ISS (mpv-Start dauert)
    clock.advance(3)
    assert vc.press(pressed + 0.08) is False
    assert vc.view == "iss"


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
    vc, clock, *_ = make(hour=3, auto_cycle=False)
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


def test_manual_only_no_auto_switch():
    vc, clock, *_ = make(auto_cycle=False)
    vc.start()
    clock.advance(3600); vc.tick()
    assert vc.view == "weather"


def test_presses_ignored_while_stream_loads():
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    vc.start()
    clock.advance(1); vc.press()                 # → Aus
    mpv.ready = False
    clock.advance(1); vc.press()                 # → ISS, Stream lädt
    assert vc.view == "iss"
    clock.advance(5); assert vc.press() is False  # noch kein Bild
    assert vc.view == "iss"
    mpv.ready = True
    clock.advance(1); assert vc.press() is True
    assert vc.view == "weather"


def test_stream_wait_is_limited():
    """Kommt der Stream nie (kein Internet), geht es nach 30 s trotzdem weiter."""
    vc, clock, display, windows, mpv, browser = make(auto_cycle=False)
    vc.start()
    clock.advance(1); vc.press()                 # Aus
    mpv.ready = False
    clock.advance(1); vc.press()                 # ISS
    clock.advance(31); assert vc.press() is True
    assert vc.view == "weather"


def test_press_at_night_cancels_night_mode_for_this_night():
    vc, clock, display, windows, mpv, browser = make(hour=2, auto_cycle=False)
    vc.start()
    assert vc.view == "off"
    clock.advance(1); vc.press()                 # → ISS um 02:00
    assert vc.view == "iss"
    clock.advance(600); vc.tick()                # weiter Nacht: bleibt an
    assert vc.view == "iss"
    clock.advance(1); vc.press()                 # → Wetter
    clock.t = datetime(2026, 11, 3, 6, 0, 5).timestamp(); vc.tick()
    assert vc.view == "weather"                  # 06:00: gewählte Ansicht bleibt
    clock.t = datetime(2026, 11, 4, 1, 0, 5).timestamp(); vc.tick()
    assert vc.view == "off"                      # nächste Nacht wieder normal
    clock.t = datetime(2026, 11, 4, 6, 0, 5).timestamp(); vc.tick()
    assert vc.view == "weather"


def test_night_off_by_hand_stays_off_in_the_morning():
    vc, clock, display, windows, mpv, browser = make(hour=2, auto_cycle=False)
    vc.start()
    for _ in range(3):                           # Aus → ISS → Wetter → Aus
        clock.advance(1); vc.press()
    assert vc.view == "off"
    clock.t = datetime(2026, 11, 3, 6, 0, 5).timestamp(); vc.tick()
    assert vc.view == "off"
