#!/usr/bin/env bash
# Apply the runtime identity bits (Settings.* values) that can only be set
# while an Android session is live. Run AFTER scripts/vm2_start_display.sh.
#
# Writes:
#   /var/lib/waydroid/vm_fingerprint_runtime.txt — pairs with the persistent
#   manifest produced by vm2_fingerprint_randomize.sh.
#
# Idempotent: re-running re-randomizes android_id/bluetooth_address; keeps
# the same device_name unless you edit below.

set -euo pipefail

log() { printf '\n\033[1;34m[vm2_identity]\033[0m %s\n' "$*"; }

if ! adb devices | grep -q '127.0.0.1:5556.*device'; then
    echo "ERROR: adb not connected to 127.0.0.1:5556. Run scripts/vm2_start_display.sh first." >&2
    exit 1
fi

# ---------- randomized values ----------
NEW_ANDROID_ID=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
NEW_DEVICE_NAME="Galaxy A54"
NEW_BT_MAC=$(printf '%02X:%02X:%02X:%02X:%02X:%02X' \
    $((RANDOM % 256 | 2)) $((RANDOM % 256)) $((RANDOM % 256)) \
    $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)))

log "android_id=$NEW_ANDROID_ID device_name='$NEW_DEVICE_NAME' bt=$NEW_BT_MAC"

adb -s 127.0.0.1:5556 shell "settings put secure android_id $NEW_ANDROID_ID"
adb -s 127.0.0.1:5556 shell "settings put global device_name '$NEW_DEVICE_NAME'"
adb -s 127.0.0.1:5556 shell "settings put secure bluetooth_address $NEW_BT_MAC"

# ---------- write manifest ----------
MANIFEST=/var/lib/waydroid/vm_fingerprint_runtime.txt
sudo tee "$MANIFEST" >/dev/null <<EOF
# VM runtime identity — generated $(date -Is)
android_id=$NEW_ANDROID_ID
device_name=$NEW_DEVICE_NAME
bluetooth_address=$NEW_BT_MAC
EOF

log "verification (what TT Lite will read):"
echo "ro.product.model  = $(adb -s 127.0.0.1:5556 shell getprop ro.product.model)"
echo "ro.product.manufacturer = $(adb -s 127.0.0.1:5556 shell getprop ro.product.manufacturer)"
echo "ro.build.fingerprint = $(adb -s 127.0.0.1:5556 shell getprop ro.build.fingerprint)"
echo "ro.serialno = $(adb -s 127.0.0.1:5556 shell getprop ro.serialno)"
echo "android_id = $(adb -s 127.0.0.1:5556 shell settings get secure android_id)"
echo "device_name = $(adb -s 127.0.0.1:5556 shell settings get global device_name)"
echo "eth0 MAC = $(adb -s 127.0.0.1:5556 shell ip link show eth0 | grep -oE 'ether [0-9a-f:]+' | awk '{print $2}')"

log "done. Install TT Lite next: scripts/vm2_install_tt.sh"
