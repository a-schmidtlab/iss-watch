#!/usr/bin/env bash
# update-iss-cookies.sh
#
# Exportiert YouTube-Cookies aus dem lokal eingeloggten Firefox und
# deployed sie auf den ISS-Watch Pi. Läuft wöchentlich per Cron.
#
# Voraussetzung: Firefox muss ein gültiges YouTube-Login enthalten.
# Cron-Eintrag (crontab -e):
#   0 3 * * 1  /home/axel/SynologyDrive/EIGENE_DATEIEN/Axels_Programme/iss-watch/update-iss-cookies.sh

set -euo pipefail

PI_HOST="axel@192.168.178.56"
PI_KEY="$HOME/.ssh/id_ed25519"
PI_COOKIE_PATH="~/projects/iss-watch/yt-cookies.txt"
LOG="$HOME/.local/share/iss-watch-cookies.log"
TMPFILE="/tmp/yt-cookies-update-$$.txt"
rm -f "$TMPFILE"   # yt-dlp erwartet nicht-existierende Datei beim ersten Schreiben
trap "rm -f '$TMPFILE'" EXIT

mkdir -p "$(dirname "$LOG")"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') | $*" | tee -a "$LOG"
}

log "=== Cookie-Update gestartet ==="

# Cookies aus Firefox exportieren (funktioniert auch wenn Firefox offen ist)
# Exit-Code ignorieren — yt-dlp schreibt Cookies auch wenn der Stream-Abruf
# self fehlschlägt (z.B. wegen Format-Einschränkungen). Entscheidend ist nur
# ob die Cookies-Datei anschließend gültige Login-Einträge enthält.
/usr/bin/yt-dlp \
    --cookies-from-browser firefox \
    --cookies "$TMPFILE" \
    --skip-download --no-warnings \
    'https://www.youtube.com/@sen/live' 2>>"$LOG" || true

if [[ ! -s "$TMPFILE" ]]; then
    log "FEHLER: Cookie-Datei ist leer oder wurde nicht erstellt"
    exit 1
fi

# Prüfen ob echte Login-Cookies vorhanden sind (SAPISID = YouTube-Session)
if ! grep -q 'SAPISID' "$TMPFILE"; then
    log "FEHLER: Keine YouTube-Login-Cookies gefunden — bitte in Firefox bei YouTube einloggen"
    exit 1
fi

COOKIE_COUNT=$(grep -c 'youtube.com' "$TMPFILE" || true)
log "OK: $COOKIE_COUNT YouTube-Cookies exportiert"

# Cookies auf den Pi kopieren
if ! scp -i "$PI_KEY" -q "$TMPFILE" "${PI_HOST}:${PI_COOKIE_PATH}"; then
    log "FEHLER: SCP zum Pi fehlgeschlagen"
    exit 1
fi
log "OK: Cookies auf Pi deployed"

# Dienst auf dem Pi neu starten
if ! ssh -i "$PI_KEY" -q "$PI_HOST" "systemctl --user restart iss-watch"; then
    log "FEHLER: Service-Neustart auf Pi fehlgeschlagen"
    exit 1
fi
log "OK: iss-watch auf Pi neu gestartet"
log "=== Cookie-Update erfolgreich ==="
