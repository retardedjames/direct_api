#!/usr/bin/env bash
# clone_finalize.sh — after VNC signup is complete, capture the session,
# smoke-test the signer, and start continual_scraper.
#
# Usage on the VM:
#   bash ~/direct_api/scripts/clone_finalize.sh [vm_label]
#
# vm_label defaults to "vm$(date +%s | tail -c 4)" if not supplied — a
# locally-unique label for this VM. The label only matters for:
#   - the filename replay_search_<label>.py (gitignored)
#   - the TIKTOK_ACCOUNT env var (drives import dispatch)
#   - the NTFY_PREFIX env var (so notifications show which worker)
# It does NOT need to be tracked anywhere or coordinated with other VMs.

set -euo pipefail

log() { printf '\n\033[1;34m[finalize]\033[0m %s\n' "$*"; }
err() { printf '\n\033[1;31m[finalize ERROR]\033[0m %s\n' "$*" >&2; exit 1; }

LABEL="${1:-vm$(date +%s | tail -c 4)}"
[[ "$LABEL" =~ ^vm[0-9]+$ ]] || err "label must match /^vm[0-9]+$/, got '$LABEL'"

REPO=$HOME/direct_api
cd "$REPO"

OUT="$REPO/replay_search_${LABEL}.py"
log "writing identity → $OUT (label: $LABEL)"
sudo python3 scripts/capture_session.py "$LABEL" > "$OUT"
sudo chown "$(id -u):$(id -g)" "$OUT"
chmod 600 "$OUT"
LINES=$(wc -l < "$OUT")
[ "$LINES" -gt 30 ] || err "$OUT is only $LINES lines; capture probably failed"

# Smoke test — should print "[verdict] ACCEPTED aweme_count=30 sst=>200ms".
log "smoke test: searching 'mario'"
adb -s 127.0.0.1:5556 forward tcp:27042 tcp:27042 >/dev/null
SMOKE_OUT=$(TIKTOK_ACCOUNT="$LABEL" ADB_DEVICE=127.0.0.1:5556 \
    timeout 120 python3 replay_search_frida.py mario 2>&1 || true)
echo "$SMOKE_OUT" | grep -E "verdict|aweme_count|sst" | head -5
if ! grep -q "ACCEPTED" <<<"$SMOKE_OUT"; then
    echo "$SMOKE_OUT" | tail -30 >&2
    err "smoke test did not accept. See output above; common causes: missing session cookies (re-run after VNC signup), bad pacing, or fingerprint flagged."
fi

# Start the daemon. Conservative pacing for the first 24h on a new
# account; lower it later if the account survives.
log "starting continual_scraper (pacing 60-180s)"
sed -i 's/^INTER_TERM_SLEEP_MIN = .*/INTER_TERM_SLEEP_MIN = 60/' continual_scraper.py
sed -i 's/^INTER_TERM_SLEEP_MAX = .*/INTER_TERM_SLEEP_MAX = 180/' continual_scraper.py

mkdir -p logs
LOG="logs/continual_$(date +%Y%m%d_%H%M%S).log"
nohup env "TIKTOK_ACCOUNT=$LABEL" ADB_DEVICE=127.0.0.1:5556 \
    "NTFY_PREFIX=[$LABEL]" \
    python3 -u continual_scraper.py > "$LOG" 2>&1 &
disown
sleep 5

cat <<DONE

\033[1;32m=== finalize complete ===\033[0m
label:        $LABEL
identity:     $OUT
log:          $LOG
scraper pid:  $(pgrep -fa "continual_scraper" | grep -v "bash -c" | awk '{print $1}' | head -1)

ntfy will start emitting [$LABEL] 'keyword': saved N videos (...) on each
term completion. If notifications stop within an hour, the account got
flagged — see CLONE_SETUP.md "If something goes wrong".
DONE
