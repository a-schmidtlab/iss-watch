#!/usr/bin/env python3
"""
ISS Watch — Raspberry Pi ISS Stream Controller
Primär: Sen 4K ISS-Kamera.
Alle 5 Minuten: kurzer Blick auf NASA ISS-Kamera (30s) falls live.
Watchdog: erkennt wenn mpv hängt oder kein Video mehr zeigt. 
"""
import subprocess
import time
import json
import socket
import os
import urllib.request
import numpy as np
import cv2
from datetime import datetime

# ── Konfiguration ──────────────────────────────────────────────────────────────

NASA_CHANNEL     = "https://www.youtube.com/channel/UCLA_DiR1FfKNvjuUpBHmylQ/live"
NASA_FALLBACK_ID = "zPH5KtjJFaQ"
SEN_STREAM       = "https://www.youtube.com/@sen/live"
MPV_SOCKET       = "/tmp/mpv-ipc"
NASA_CHECK_IMG   = "/tmp/nasa_check.png"

NASA_CHECK_INTERVAL = 5 * 60    # Alle 5 Minuten NASA prüfen
NASA_PEEK_DURATION  = 30        # Sekunden NASA zeigen wenn live
VIDEO_ID_TTL        = 2 * 3600
HLS_URL_TTL         = 90 * 60

LOS_WHITE_THRESHOLD = 0.08

WATCHDOG_INTERVAL   = 30        # Sekunden zwischen Watchdog-Checks
WATCHDOG_MAX_STUCK  = 3         # Wie oft darf playback-time gleich bleiben

DISPLAY_OUTPUT = "HDMI-A-1"

# ──────────────────────────────────────────────────────────────────────────────


class URLCache:
    def __init__(self):
        self.video_id    = None
        self.hls_url     = None
        self.id_fetched  = 0
        self.hls_fetched = 0

    def get_video_id(self):
        if not self.video_id:
            self.video_id  = NASA_FALLBACK_ID
            self.id_fetched = time.time()

        if time.time() - self.id_fetched < VIDEO_ID_TTL:
            return self.video_id

        print("[url] Prüfe NASA Video-ID...")
        try:
            result = subprocess.run(
                ["yt-dlp", "--get-id", "--no-warnings",
                 f"https://www.youtube.com/watch?v={self.video_id}"],
                capture_output=True, text=True, timeout=30
            )
            vid = result.stdout.strip()
            if vid and len(vid) == 11:
                self.id_fetched = time.time()
                print(f"[url] Video-ID aktiv: {vid}")
                return self.video_id
        except Exception as e:
            print(f"[url] Fehler ID-Check: {e}")

        print("[url] Hole neue ID vom NASA-Kanal...")
        try:
            result = subprocess.run(
                ["yt-dlp", "--get-id", "--no-warnings",
                 "--match-filter", "title~=High-Definition Views",
                 NASA_CHANNEL],
                capture_output=True, text=True, timeout=30
            )
            vid = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
            if vid and len(vid) == 11:
                print(f"[url] Neue Video-ID: {vid}")
                if vid != self.video_id:
                    self.hls_url    = None
                    self.hls_fetched = 0
                self.video_id  = vid
                self.id_fetched = time.time()
                return vid
        except Exception as e:
            print(f"[url] Fehler neue ID: {e}")

        self.id_fetched = time.time()
        return self.video_id

    def get_hls_url(self):
        if self.hls_url and time.time() - self.hls_fetched < HLS_URL_TTL:
            return self.hls_url

        vid = self.get_video_id()
        if not vid:
            return None

        print("[url] Hole HLS-URL...")
        try:
            result = subprocess.run(
                ["yt-dlp", "-g", "--no-warnings",
                 "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
                 f"https://www.youtube.com/watch?v={vid}"],
                capture_output=True, text=True, timeout=30
            )
            urls = result.stdout.strip().splitlines()
            if urls:
                self.hls_url     = urls[0]
                self.hls_fetched = time.time()
                print("[url] HLS-URL gecacht (gültig ~90min)")
                return self.hls_url
        except Exception as e:
            print(f"[url] Fehler HLS-URL: {e}")
        return self.hls_url

    def get_watch_url(self):
        vid = self.get_video_id()
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
        return NASA_CHANNEL

    def invalidate_hls(self):
        self.hls_url    = None
        self.hls_fetched = 0


class MPVController:
    def __init__(self, socket_path):
        self.socket_path  = socket_path
        self.proc         = None
        self._last_pos    = None
        self._stuck_count = 0

    def start(self, url):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            time.sleep(2)
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

        cmd = [
            "mpv",
            f"--input-ipc-server={self.socket_path}",
            "--fullscreen",
            "--no-terminal",
            "--no-osc",
            "--ytdl-format=bestvideo[height<=1080]+bestaudio/best[height<=1080]",
            "--really-quiet",
            url,
        ]
        env = os.environ.copy()
        env["DISPLAY"] = ":0"
        self.proc = subprocess.Popen(cmd, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self._last_pos    = None
        self._stuck_count = 0
        for _ in range(20):
            time.sleep(0.5)
            if os.path.exists(self.socket_path):
                break
        print(f"[mpv] Gestartet: {url[:70]}...")

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
        self._last_pos    = None
        self._stuck_count = 0
        self.send(["loadfile", url, "replace"])
        print(f"[mpv] Lade: {url[:70]}...")

    def show_text(self, text, duration_ms=8000):
        self.send(["show-text", text, duration_ms])

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def is_playing(self):
        """
        Watchdog: prüft ob mpv tatsächlich noch Video abspielt.
        Gibt False zurück wenn IPC nicht erreichbar oder Bild eingefroren.
        """
        result = self.send(["get_property", "playback-time"])
        if result is None or result.get("error") == "property unavailable":
            print("[watchdog] IPC nicht erreichbar")
            return False

        pos = result.get("data")
        if pos is None:
            print("[watchdog] Keine Abspielposition")
            return False

        if self._last_pos is not None and abs(pos - self._last_pos) < 0.5:
            self._stuck_count += 1
            print(f"[watchdog] Bild eingefroren ({self._stuck_count}/{WATCHDOG_MAX_STUCK})")
            if self._stuck_count >= WATCHDOG_MAX_STUCK:
                return False
        else:
            self._stuck_count = 0

        self._last_pos = pos
        return True


def wait_for_network(timeout=60):
    """Wartet bis Netzwerk verfügbar ist."""
    print("[net] Warte auf Netzwerk...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen("http://www.google.com", timeout=3)
            print("[net] Netzwerk verfügbar")
            return True
        except Exception:
            time.sleep(3)
    print("[net] Netzwerk-Timeout")
    return False


def analyze_frame_for_los(img_path):
    img = cv2.imread(img_path)
    if img is None:
        return True, 0.0
    h, w = img.shape[:2]
    roi = img[:int(h * 0.70), :int(w * 0.65)]
    bright_mask = np.all(roi > 220, axis=2)
    ratio = np.sum(bright_mask) / (roi.shape[0] * roi.shape[1])
    return ratio > LOS_WHITE_THRESHOLD, ratio


def nasa_is_live(hls_url):
    """
    Grabbt einen Frame direkt vom NASA HLS-Stream per ffmpeg.
    Returns True wenn kein LOS-Text sichtbar (= Stream ist live).
    """
    if not hls_url:
        return False
    print("[check] Prüfe NASA-Stream...")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", hls_url,
             "-frames:v", "1", "-q:v", "5", NASA_CHECK_IMG],
            capture_output=True, timeout=20
        )
        if not os.path.exists(NASA_CHECK_IMG):
            return False
        is_los, ratio = analyze_frame_for_los(NASA_CHECK_IMG)
        print(f"[check] NASA: {'LOS' if is_los else 'LIVE'} ({ratio:.2%})")
        return not is_los
    except subprocess.TimeoutExpired:
        print("[check] Timeout")
        return False
    except Exception as e:
        print(f"[check] Fehler: {e}")
        return False


def get_target_brightness():
    hour = datetime.now().hour
    if 6 <= hour < 21:
        return 1.0
    elif 21 <= hour or hour < 1:
        return 0.6
    else:
        return 0.3


def set_brightness(brightness):
    try:
        subprocess.run(
            ["xrandr", "--output", DISPLAY_OUTPUT, "--brightness", str(brightness)],
            env={**os.environ, "DISPLAY": ":0"},
            capture_output=True, timeout=5
        )
        print(f"[brightness] {brightness}")
    except Exception as e:
        print(f"[brightness] Fehler: {e}")


def main():
    print("=" * 55)
    print("  ISS Watch gestartet")
    print("  Primär: Sen 4K | NASA-Check alle 5 Min")
    print("=" * 55)

    wait_for_network()

    mpv       = MPVController(MPV_SOCKET)
    url_cache = URLCache()

    mpv.start(SEN_STREAM)
    mode = "sen"

    last_nasa_check    = time.time() - NASA_CHECK_INTERVAL + 30
    last_watchdog      = time.time()
    current_brightness = None

    while True:
        try:
            now = time.time()

            # Helligkeit anpassen
            target = get_target_brightness()
            if target != current_brightness:
                set_brightness(target)
                current_brightness = target

            # Video-ID im Cache halten
            url_cache.get_video_id()

            # ── Watchdog ────────────────────────────────────────────────────
            if now - last_watchdog >= WATCHDOG_INTERVAL:
                last_watchdog = now
                if not mpv.is_running():
                    print("[watchdog] mpv Prozess tot → Neustart")
                    wait_for_network(30)
                    url = SEN_STREAM if mode == "sen" else url_cache.get_watch_url()
                    mpv.start(url)
                    time.sleep(5)
                    continue

                if not mpv.is_playing():
                    print("[watchdog] mpv spielt nicht mehr → Neustart")
                    wait_for_network(30)
                    url = SEN_STREAM if mode == "sen" else url_cache.get_watch_url()
                    mpv.start(url)
                    time.sleep(5)
                    continue

            # ── NASA-Peek alle 5 Minuten ────────────────────────────────────
            if mode == "sen" and now - last_nasa_check >= NASA_CHECK_INTERVAL:
                last_nasa_check = now
                hls  = url_cache.get_hls_url()
                live = nasa_is_live(hls)

                if live:
                    print(f"[peek] NASA live → zeige {NASA_PEEK_DURATION}s")
                    mpv.show_text(f"ISS Live-Kamera ({NASA_PEEK_DURATION}s)", 4000)
                    time.sleep(2)
                    url_cache.invalidate_hls()
                    mpv.load(url_cache.get_watch_url())
                    mode = "nasa_peek"
                    time.sleep(NASA_PEEK_DURATION)
                    print("[peek] Zurück zu Sen")
                    mpv.load(SEN_STREAM)
                    mode = "sen"
                    time.sleep(5)
                else:
                    print("[peek] NASA nicht live, bleibe auf Sen")

            time.sleep(10)

        except KeyboardInterrupt:
            print("\nBeendet.")
            break
        except Exception as e:
            print(f"[fehler] {e}")
            time.sleep(10)

    if mpv.proc:
        mpv.proc.terminate()


if __name__ == "__main__":
    main()