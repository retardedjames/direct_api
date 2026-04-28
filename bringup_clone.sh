#!/usr/bin/env bash
# Bring up a TikTok account on a freshly cloned scraper VM.
#
# Assumes the VM was cloned from the "clean base" image:
# venv intact, playwright chromium cached, systemd template + linger
# + VNC :1 already configured, and accounts/ empty with no enabled
# tiktok-web-scraper@*.service.
#
# Usage:
#   ./bringup_clone.sh --account <name>
#
# What it does:
#   1. Pulls latest main (in case the image is behind).
#   2. Creates accounts/<name>/.
#   3. Launches refresh_web_cookie.py --fresh under tmux on DISPLAY=:1.
#   4. Prints the SSH-tunnel + VNC + ready-file + enable-service commands
#      you still need to run manually.
#
# Refuses to run if accounts/<name>/ exists or any scraper@*.service is
# already enabled — those are signs the image wasn't clean.

set -euo pipefail

ACCOUNT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --account) ACCOUNT="${2:-}"; shift 2 ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$ACCOUNT" ]]; then
  echo "usage: $0 --account <name>" >&2
  exit 2
fi

cd "$(dirname "$(readlink -f "$0")")"

if [[ -e "accounts/$ACCOUNT" ]]; then
  echo "[bringup] accounts/$ACCOUNT already exists. Refusing to clobber." >&2
  echo "[bringup] If this is a re-attempt, 'rm -rf accounts/$ACCOUNT' first." >&2
  exit 3
fi

ENABLED=$(systemctl --user list-unit-files 'tiktok-web-scraper@*' \
            --state=enabled --no-legend 2>/dev/null | awk '{print $1}' || true)
if [[ -n "$ENABLED" ]]; then
  echo "[bringup] enabled scraper unit(s) found on this clone:" >&2
  echo "$ENABLED" | sed 's/^/  /' >&2
  echo "[bringup] image was supposed to be clean. Aborting; investigate." >&2
  exit 4
fi

echo "[bringup] git pull..."
git fetch origin --quiet
git reset --hard origin/main >/dev/null

mkdir -p "accounts/$ACCOUNT"

tmux kill-session -t login 2>/dev/null || true
rm -f "/tmp/refresh_web_cookie.${ACCOUNT}.ready" /tmp/refresh.log

if ! ss -tln 2>/dev/null | grep -q '127.0.0.1:5901 '; then
  echo "[bringup] VNC :1 not listening — starting vncserver@1.service..."
  systemctl --user start vncserver@1.service 2>/dev/null \
    || vncserver :1 -localhost yes -geometry 1280x800 -depth 24
  sleep 2
fi

echo "[bringup] launching refresh_web_cookie.py --fresh under tmux..."
tmux new-session -d -s login \
  "DISPLAY=:1 XAUTHORITY=\$HOME/.Xauthority \
   .venv/bin/python refresh_web_cookie.py --account $ACCOUNT --fresh \
   2>&1 | tee /tmp/refresh.log"

sleep 3

PUBIP=$(curl -s --max-time 4 ifconfig.me 2>/dev/null \
        || hostname -I | awk '{print $1}')

cat <<EOF

[bringup] Chromium is open on the VM at tiktok.com.

Next steps:

  1. From your laptop, open the SSH tunnel (keep open):
       ssh -i ~/.ssh/jamescvermont -L 5901:localhost:5901 \\
           jamescvermont@$PUBIP

  2. VNC viewer → localhost:5901. Log into the TikTok account
     for this VM, do one search to warm cookies.

  3. Back in an SSH session on the VM, signal the script:
       touch /tmp/refresh_web_cookie.${ACCOUNT}.ready

  4. Watch (expect "[refresh] wrote ..." → "[verify] OK" → "[refresh] done."):
       tmux attach -t login

  5. Enable the scraper:
       systemctl --user enable --now tiktok-web-scraper@${ACCOUNT}.service
       journalctl --user -u tiktok-web-scraper@${ACCOUNT}.service -f

  6. Add this VM (IP $PUBIP, account $ACCOUNT) to the
     Fleet roster in CLAUDE.md.
EOF
