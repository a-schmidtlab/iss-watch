#!/usr/bin/env python3
"""
ISS Watch — Raspberry Pi ISS Stream Controller
Zeigt NASA ISS Stream, erkennt LOS und schaltet auf Sen um.
Bei LOS: Countdown via ISS-Bahndaten, Rückkehr nach TLE-AOS + Bildverifikation.
"""

import subprocess
import time
import json
import socket
import os
import math
import urllib.request
import numpy as np
import cv2
from datetime import datetime, timezone, timedelta
from sgp4.api import Satrec, jday

# ── Konfiguration ──────────────────────────────────────────────────────────────

NASA_CHANNEL    = "https://www.youtube.com/channel/UCLA_DiR1FfKNvjuUpBHmylQ/live"
NASA_FALLBACK_ID = "zPH5KtjJFaQ"  # ISS Erdkamera — stabiler Fallback
SEN_STREAM      = "https://www.youtube.com/@sen/live"
MPV_SOCKET      = "/tmp/mpv-ipc"
SCREENSHOT      = "/tmp/iss_frame.png"
NASA_CHECK_IMG  = "/tmp/nasa_check.png"

CHECK_SECS      = 15
TLE_TTL         = 6 * 3600
VIDEO_ID_TTL    = 2 * 3600
HLS_URL_TTL     = 90 * 60
TLE_URL         = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=TLE"

R_EARTH = 6371.0
H_ISS   = 420.0
H_TDRS  = 35786.0

TDRS_POSITIONS = [
    (-41.0,  0.0),
    (-171.0, 0.0),
    (-150.0, 0.0),
]

MAX_ANGLE = math.degrees(
    math.acos(R_EARTH / (R_EARTH + H_ISS)) +
    math.acos(R_EARTH / (R_EARTH + H_TDRS))
)

LOS_WHITE_THRESHOLD = 0.08

# ──────────────────────────────────────────────────────────────────────────────


class URLCache:
    """Cached und aktualisiert YouTube Video-ID und HLS-URL."""

    def __init__(self):
        self.video_id    = None
        self.hls_url     = None
        self.id_fetched  = 0
        self.hls_fetched = 0

    def get_video_id(self):
        # Erst prüfen ob bekannte ID noch läuft
        if not self.video_id:
            self.video_id = NASA_FALLBACK_ID
            self.id_fetched = time.time()

        if time.time() - self.id_fetched < VIDEO_ID_TTL:
            return self.video_id

        # TTL abgelaufen: prüfen ob aktuelle ID noch verfügbar
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

        # ID nicht mehr verfügbar → vom Kanal neue holen
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
        self.socket_path = socket_path
        self.proc = None

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
        self.send(["loadfile", url, "replace"])
        print(f"[mpv] Lade: {url[:70]}...")

    def show_text(self, text, duration_ms=14000):
        self.send(["show-text", text, duration_ms])

    def screenshot(self, path):
        if os.path.exists(path):
            os.remove(path)
        self.send(["screenshot-to-file", path, "video"])
        time.sleep(0.8)
        return os.path.exists(path)

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None


def analyze_frame_for_los(img_path):
    img = cv2.imread(img_path)
    if img is None:
        return False, 0.0
    h, w = img.shape[:2]
    roi = img[:int(h * 0.70), :int(w * 0.65)]
    bright_mask = np.all(roi > 220, axis=2)
    ratio = np.sum(bright_mask) / (roi.shape[0] * roi.shape[1])
    return ratio > LOS_WHITE_THRESHOLD, ratio


def detect_los(mpv):
    if not mpv.screenshot(SCREENSHOT):
        return False
    is_los, ratio = analyze_frame_for_los(SCREENSHOT)
    if is_los:
        print(f"[los] LOS erkannt (Helligkeit: {ratio:.2%})")
    return is_los


def verify_nasa_live(hls_url):
    """
    Grabbt einen Frame direkt vom NASA HLS-Stream per ffmpeg.
    Wird nur aufgerufen wenn TLE-AOS bevorsteht → CPU-Last minimal.
    Returns True wenn kein LOS-Text sichtbar (= Stream ist live).
    """
    if not hls_url:
        return False

    print("[verify] Prüfe NASA-Stream direkt via ffmpeg...")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", hls_url,
             "-frames:v", "1", "-q:v", "5", NASA_CHECK_IMG],
            capture_output=True, timeout=20
        )
        if not os.path.exists(NASA_CHECK_IMG):
            return False
        is_los, ratio = analyze_frame_for_los(NASA_CHECK_IMG)
        print(f"[verify] Ergebnis: {'LOS' if is_los else 'LIVE'} ({ratio:.2%})")
        return not is_los
    except subprocess.TimeoutExpired:
        print("[verify] Timeout")
        return False
    except Exception as e:
        print(f"[verify] Fehler: {e}")
        return False


def fetch_tle():
    try:
        with urllib.request.urlopen(TLE_URL, timeout=10) as r:
            lines = [l.strip() for l in r.read().decode().splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if line.startswith("1 25544"):
                tle1, tle2 = lines[i], lines[i + 1]
                print(f"[tle] TLE aktualisiert (Epoche: {tle1[18:32]})")
                return tle1, tle2
    except Exception as e:
        print(f"[tle] Fehler: {e}")
    return None, None


def iss_signal_status(tle1, tle2):
    try:
        sat = Satrec.twoline2rv(tle1, tle2)
        now = datetime.now(timezone.utc)
        timeline = []
        for step in range(0, 91 * 2):
            t = now + timedelta(seconds=step * 30)
            jd, fr = jday(t.year, t.month, t.day,
                          t.hour, t.minute,
                          t.second + t.microsecond / 1e6)
            e, r, _ = sat.sgp4(jd, fr)
            if e != 0:
                continue
            x, y, z = r
            norm = math.sqrt(x*x + y*y + z*z)
            lat  = math.degrees(math.asin(z / norm))
            gmst = math.fmod(280.46061837 + 360.98564736629 * (jd - 2451545.0 + fr), 360.0)
            lon  = math.fmod(math.degrees(math.atan2(y, x)) - gmst + 360.0, 360.0)
            if lon > 180:
                lon -= 360
            visible = False
            for tdrs_lon, tdrs_lat in TDRS_POSITIONS:
                dlat = math.radians(lat - tdrs_lat)
                dlon = math.radians(lon - tdrs_lon)
                a = (math.sin(dlat / 2) ** 2
                     + math.cos(math.radians(lat))
                     * math.cos(math.radians(tdrs_lat))
                     * math.sin(dlon / 2) ** 2)
                angle = math.degrees(2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)))
                if angle < MAX_ANGLE:
                    visible = True
                    break
            timeline.append((step * 0.5, visible))

        if not timeline:
            return True, 0
        if timeline[0][1]:
            return True, 0
        for minutes, visible in timeline:
            if visible:
                return False, minutes
        return False, 45
    except Exception as e:
        print(f"[sgp4] Fehler: {e}")
        return True, 0


def show_countdown(mpv, minutes_to_aos):
    mins = int(minutes_to_aos)
    secs = int((minutes_to_aos - mins) * 60)
    if mins > 0:
        text = f"ISS Live-Signal in ca. {mins} Min {secs:02d} Sek"
    elif secs > 0:
        text = f"ISS Live-Signal in ca. {secs} Sekunden"
    else:
        text = "ISS Live-Signal wird wiederhergestellt..."
    mpv.show_text(text, CHECK_SECS * 1000 - 500)
    print(f"[aos] {text}")


def main():
    print("=" * 55)
    print("  ISS Watch gestartet")
    print("=" * 55)

    mpv       = MPVController(MPV_SOCKET)
    url_cache = URLCache()

    tle1, tle2  = fetch_tle()
    tle_fetched = time.time()

    mpv.start(url_cache.get_watch_url())
    mode          = "nasa"
    los_confirmed = False
    aos_imminent  = False

    while True:
        try:
            now = time.time()

            # TLE aktualisieren
            if now - tle_fetched > TLE_TTL:
                new1, new2 = fetch_tle()
                if new1:
                    tle1, tle2 = new1, new2
                tle_fetched = now

            # Video-ID im Cache halten (läuft nur wenn TTL abgelaufen)
            url_cache.get_video_id()

            # mpv neu starten falls abgestürzt
            if not mpv.is_running():
                print("[mpv] Abgestürzt, starte neu...")
                time.sleep(5)
                url = url_cache.get_watch_url() if mode == "nasa" else SEN_STREAM
                mpv.start(url)
                time.sleep(5)
                continue

            # ── NASA-Modus ──────────────────────────────────────────────────
            if mode == "nasa":
                los = detect_los(mpv)

                if los and not los_confirmed:
                    los_confirmed = True
                    print("[los] LOS-Kandidat, bestätige beim nächsten Check...")

                elif los and los_confirmed:
                    print("[mode] LOS bestätigt → Sen-Kamera")
                    mode          = "sen"
                    los_confirmed = False
                    aos_imminent  = False
                    mpv.load(SEN_STREAM)
                    time.sleep(8)

                else:
                    los_confirmed = False

            # ── Sen-Modus ───────────────────────────────────────────────────
            elif mode == "sen":
                if tle1:
                    has_signal, minutes_to_aos = iss_signal_status(tle1, tle2)
                else:
                    has_signal, minutes_to_aos = False, 10

                # AOS-Fenster: TLE sagt Signal möglich → mit ffmpeg verifizieren
                if has_signal or minutes_to_aos < 2.0:
                    if not aos_imminent:
                        print(f"[aos] AOS in {minutes_to_aos:.1f} min — starte Verifikation")
                        aos_imminent = True

                    hls      = url_cache.get_hls_url()
                    is_live  = verify_nasa_live(hls)

                    if is_live:
                        print("[mode] NASA Stream wieder live → umschalten")
                        mode         = "nasa"
                        aos_imminent = False
                        url_cache.invalidate_hls()
                        mpv.load(url_cache.get_watch_url())
                        time.sleep(8)
                    else:
                        show_countdown(mpv, 0)

                else:
                    aos_imminent = False
                    show_countdown(mpv, minutes_to_aos)

            time.sleep(CHECK_SECS)

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
