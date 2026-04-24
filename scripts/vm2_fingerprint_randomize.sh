#!/usr/bin/env bash
# Randomize the Waydroid device fingerprint so TikTok sees a unique "new phone"
# per VM. Run BEFORE installing TT Lite — once TT Lite registers with
# ByteDance, install_id is cryptographically bound to whatever device state
# Android reports at that moment; re-randomizing afterwards means TT Lite
# will silent-reject.
#
# Covers two classes of leak identified in the Waydroid fingerprint memo:
#   - Fleet-wide Waydroid tells (board.platform=waydroid, product.model=WayDroid,
#     build.fingerprint=waydroid/..., etc.) — patched via build.prop overlay.
#   - Per-clone linkers (LXC eth0 MAC, Settings.Global.device_name, Secure.android_id,
#     bluetooth_address) — randomized at session/container boot.
#
# Run order on VM-2:
#   1. scripts/vm2_base_install.sh          (as user, one-time)
#   2. scripts/vm2_fingerprint_randomize.sh (as root, BEFORE installing TT Lite)
#   3. scripts/vm2_start_session.sh         (as user)
#   4. scripts/vm2_install_tt.sh            (as user)
#
# Idempotent: re-running just re-randomizes. No harm unless TT Lite is
# already installed and registered — in that case the signer will break.

set -euo pipefail

log() { printf '\n\033[1;34m[vm2_fp]\033[0m %s\n' "$*"; }

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: run as root (sudo). Touches /var/lib/waydroid/**." >&2
    exit 1
fi

LXC_CONFIG=/var/lib/waydroid/lxc/waydroid/config
ROOTFS=/var/lib/waydroid/rootfs
BUILDPROP=$ROOTFS/system/build.prop
WAYDROID_PROP=/var/lib/waydroid/waydroid.prop

for f in "$LXC_CONFIG" "$BUILDPROP"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: expected $f — did waydroid init + container start complete?" >&2
        exit 1
    fi
done

# ---------- stop the container so overlay edits aren't shadowed ----------
log "stopping waydroid session + container (will restart at the end)"
sudo -u "$(logname)" waydroid session stop 2>/dev/null || true
systemctl stop waydroid-container
sleep 2

# ---------- 1. LXC eth0 MAC ----------
# The LXC-baked MAC (`lxc.net.0.hwaddr`) was the strongest per-clone linker
# found in memory — apps read NetworkInterface.getHardwareAddress().
# Generate a locally-administered MAC (bit 1 of first octet = 1, bit 0 = 0).
NEW_MAC=$(printf '02:%02x:%02x:%02x:%02x:%02x' \
    $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)) \
    $((RANDOM % 256)) $((RANDOM % 256)))
log "new eth0 MAC: $NEW_MAC"
if grep -q '^lxc.net.0.hwaddr' "$LXC_CONFIG"; then
    sed -i -E "s|^lxc\\.net\\.0\\.hwaddr\\s*=.*|lxc.net.0.hwaddr = $NEW_MAC|" "$LXC_CONFIG"
else
    echo "lxc.net.0.hwaddr = $NEW_MAC" >> "$LXC_CONFIG"
fi

# ---------- 2. build.prop overlay ----------
# Pick a plausible real device: Samsung Galaxy A54 5G (SM-A546U1). Differs
# from VM-1's "moto g power - 2025" persona and from the stock WayDroid
# masquerade. FINGERPRINT is cosmetic-for-humans but TikTok does read it.
BRAND=samsung
MANUFACTURER=samsung
MODEL=SM-A546U1
DEVICE=a54x
PRODUCT=a54xsq
# Real-looking ro.serialno — 16 hex chars, uppercase
SERIAL=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n' | tr '[:lower:]' '[:upper:]')
# Android 13 release + a Samsung-style build id + date
FINGERPRINT="samsung/a54xsq/a54x:13/TP1A.220624.014/A546U1UEU8CXJ1:user/release-keys"

log "build.prop overlay: brand=$BRAND model=$MODEL serial=$SERIAL"

# Backup once, then overwrite patches idempotently.
[ -f "$BUILDPROP.vm2-bak" ] || cp "$BUILDPROP" "$BUILDPROP.vm2-bak"

# Use a helper that replaces a key if present, otherwise appends.
set_prop() {
    local key="$1" val="$2" file="$3"
    if grep -q "^${key}=" "$file"; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$file"
    else
        echo "${key}=${val}" >> "$file"
    fi
}

for prop in \
    "ro.product.brand=$BRAND" \
    "ro.product.manufacturer=$MANUFACTURER" \
    "ro.product.model=$MODEL" \
    "ro.product.device=$DEVICE" \
    "ro.product.name=$PRODUCT" \
    "ro.product.system.brand=$BRAND" \
    "ro.product.system.manufacturer=$MANUFACTURER" \
    "ro.product.system.model=$MODEL" \
    "ro.product.system.device=$DEVICE" \
    "ro.product.system.name=$PRODUCT" \
    "ro.product.vendor.brand=$BRAND" \
    "ro.product.vendor.manufacturer=$MANUFACTURER" \
    "ro.product.vendor.model=$MODEL" \
    "ro.product.vendor.device=$DEVICE" \
    "ro.product.vendor.name=$PRODUCT" \
    "ro.build.fingerprint=$FINGERPRINT" \
    "ro.system.build.fingerprint=$FINGERPRINT" \
    "ro.vendor.build.fingerprint=$FINGERPRINT" \
    "ro.odm.build.fingerprint=$FINGERPRINT" \
    "ro.serialno=$SERIAL" \
    "ro.boot.serialno=$SERIAL" \
    "ro.board.platform=exynos850" \
    "ro.hardware=exynos850" \
    "ro.bootloader=A546U1UEU8CXJ1"; do
    key=${prop%%=*}; val=${prop#*=}
    set_prop "$key" "$val" "$BUILDPROP"
done

# Waydroid also has a separate waydroid.prop that's consulted by some props.
# Mirror a subset there so getprop returns consistent values.
if [ -f "$WAYDROID_PROP" ]; then
    for prop in \
        "ro.product.brand=$BRAND" \
        "ro.product.manufacturer=$MANUFACTURER" \
        "ro.product.model=$MODEL" \
        "ro.build.fingerprint=$FINGERPRINT"; do
        key=${prop%%=*}; val=${prop#*=}
        set_prop "$key" "$val" "$WAYDROID_PROP"
    done
fi

# ---------- 3. restart container ----------
log "starting waydroid-container with new config"
systemctl start waydroid-container
sleep 5

# ---------- 4. settings-put randomizers (need live session) ----------
# android_id is a Settings.Secure value; bluetooth_address is Settings.Secure;
# device_name is Settings.Global. Need the Android runtime up, so we bring
# the session up just to run these then leave it running.
log "bringing up session to apply android_id / device_name / bluetooth_address"
sudo -u "$(logname)" env XDG_RUNTIME_DIR="/run/user/$(id -u "$(logname)")" \
    nohup waydroid show-full-ui > /tmp/waydroid_ui_fp.log 2>&1 &
WAYDROID_UI_PID=$!

adb kill-server >/dev/null 2>&1 || true
for i in $(seq 1 30); do
    adb connect 127.0.0.1:5556 >/dev/null 2>&1 || true
    if adb -s 127.0.0.1:5556 shell getprop sys.boot_completed 2>/dev/null | grep -q '^1'; then
        break
    fi
    sleep 3
done

NEW_ANDROID_ID=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
# Plausible device_name — "Galaxy A54" or user's first name pattern. Keep it
# plain; anything with "WayDroid" or "Emulator" is a giveaway.
NEW_DEVICE_NAME="Galaxy A54"
# bluetooth_address: 6 random bytes, colon-separated, uppercase.
NEW_BT_MAC=$(printf '%02X:%02X:%02X:%02X:%02X:%02X' \
    $((RANDOM % 256 | 2)) $((RANDOM % 256)) $((RANDOM % 256)) \
    $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)))

log "android_id=$NEW_ANDROID_ID device_name='$NEW_DEVICE_NAME' bt=$NEW_BT_MAC"
adb -s 127.0.0.1:5556 shell "settings put secure android_id $NEW_ANDROID_ID"
adb -s 127.0.0.1:5556 shell "settings put global device_name '$NEW_DEVICE_NAME'"
adb -s 127.0.0.1:5556 shell "settings put secure bluetooth_address $NEW_BT_MAC"

# ---------- 5. write a fingerprint manifest for future reference ----------
MANIFEST=/var/lib/waydroid/vm2_fingerprint.txt
cat > "$MANIFEST" <<EOF
# VM-2 device fingerprint — generated $(date -Is)
# DO NOT share this with other VMs.
lxc_hwaddr=$NEW_MAC
ro.product.brand=$BRAND
ro.product.manufacturer=$MANUFACTURER
ro.product.model=$MODEL
ro.product.device=$DEVICE
ro.product.name=$PRODUCT
ro.build.fingerprint=$FINGERPRINT
ro.serialno=$SERIAL
android_id=$NEW_ANDROID_ID
device_name=$NEW_DEVICE_NAME
bluetooth_address=$NEW_BT_MAC
EOF
log "manifest: $MANIFEST"
cat "$MANIFEST"

# ---------- 6. verification ----------
log "verification — TT Lite will see these values:"
adb -s 127.0.0.1:5556 shell 'echo "ro.product.model = $(getprop ro.product.model)"; echo "ro.product.manufacturer = $(getprop ro.product.manufacturer)"; echo "ro.product.brand = $(getprop ro.product.brand)"; echo "ro.build.fingerprint = $(getprop ro.build.fingerprint)"; echo "ro.serialno = $(getprop ro.serialno)"; echo "ro.board.platform = $(getprop ro.board.platform)"'
echo
echo "android_id = $(adb -s 127.0.0.1:5556 shell settings get secure android_id)"
echo "device_name = $(adb -s 127.0.0.1:5556 shell settings get global device_name)"
echo "eth0 MAC   = $(adb -s 127.0.0.1:5556 shell ip link show eth0 | grep -oE 'ether [0-9a-f:]+' | awk '{print $2}')"

log "done. DO NOT install TT Lite on a different account/device session than"
log "the one that will sign for it. Run scripts/vm2_install_tt.sh next."
