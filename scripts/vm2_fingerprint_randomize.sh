#!/usr/bin/env bash
# Randomize the Waydroid device fingerprint so TikTok sees a unique "new phone"
# per VM. Run BEFORE installing TT Lite — once TT Lite registers with
# ByteDance, install_id is cryptographically bound to whatever device state
# Android reports at that moment; re-randomizing afterwards means TT Lite
# will silent-reject.
#
# This script does the PERSISTENT identity work (no live session needed):
#   - waydroid_base.prop  — canonical pre-boot prop overrides (ro.* friendly)
#   - waydroid.cfg        — [properties] section, applied on session boot
#   - LXC config hwaddr   — eth0 MAC inside the container (strongest linker)
#
# The runtime identity bits (Settings.Secure.android_id, Settings.Global.device_name,
# bluetooth_address) are applied by vm2_apply_runtime_identity.sh AFTER a
# session is running. Split intentionally: runtime bits need adb, persistent
# bits need the container stopped.
#
# Run order on VM-2:
#   1. scripts/vm2_base_install.sh             (as user, one-time)
#   2. sudo scripts/vm2_fingerprint_randomize.sh  (this script — container stopped)
#   3. scripts/vm2_start_display.sh            (weston + VNC + session)
#   4. scripts/vm2_apply_runtime_identity.sh   (settings put via adb)
#   5. scripts/vm2_install_tt.sh               (APK splits + frida-server)
#
# Idempotent: re-running re-randomizes.

set -euo pipefail

log() { printf '\n\033[1;34m[vm2_fp]\033[0m %s\n' "$*"; }

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: run as root (sudo). Touches /var/lib/waydroid/**." >&2
    exit 1
fi

WAYDROID_DIR=/var/lib/waydroid
LXC_CONFIG=$WAYDROID_DIR/lxc/waydroid/config
BASE_PROP=$WAYDROID_DIR/waydroid_base.prop
CFG=$WAYDROID_DIR/waydroid.cfg
MANIFEST=$WAYDROID_DIR/vm_fingerprint.txt

for f in "$LXC_CONFIG" "$BASE_PROP" "$CFG"; do
    if [ ! -f "$f" ]; then
        echo "ERROR: expected $f — was waydroid init completed?" >&2
        exit 1
    fi
done

# ---------- stop container so edits aren't shadowed ----------
log "stopping waydroid-container (will restart at the end)"
systemctl stop waydroid-container || true
sleep 2

# ---------- 1. LXC eth0 MAC (the strongest per-clone linker) ----------
NEW_MAC=$(printf '02:%02x:%02x:%02x:%02x:%02x' \
    $((RANDOM % 256)) $((RANDOM % 256)) $((RANDOM % 256)) \
    $((RANDOM % 256)) $((RANDOM % 256)))
log "new eth0 MAC: $NEW_MAC"
if grep -q '^lxc.net.0.hwaddr' "$LXC_CONFIG"; then
    sed -i -E "s|^lxc\\.net\\.0\\.hwaddr\\s*=.*|lxc.net.0.hwaddr = $NEW_MAC|" "$LXC_CONFIG"
else
    echo "lxc.net.0.hwaddr = $NEW_MAC" >> "$LXC_CONFIG"
fi

# ---------- 2. persona ----------
# Samsung Galaxy A54 5G (SM-A546U1) — differs from VM-1's stock WayDroid
# persona. At N=3+ make this a random pick from a small pool.
BRAND=samsung
MANUFACTURER=samsung
MODEL=SM-A546U1
DEVICE=a54x
PRODUCT=a54xsq
PLATFORM=exynos850
BOOTLOADER=A546U1UEU8CXJ1
# Serial: 16 hex chars uppercase
SERIAL=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n' | tr '[:lower:]' '[:upper:]')
FINGERPRINT="samsung/a54xsq/a54x:13/TP1A.220624.014/${BOOTLOADER}:user/release-keys"

log "persona: $BRAND $MODEL (serial=$SERIAL)"

# ---------- 3. waydroid_base.prop overrides ----------
# waydroid_base.prop is sourced by Waydroid's init before Android boot; ro.*
# values set here are baked into the process's property set and can't be
# overridden by later setprop. Upsert each key instead of appending to avoid
# duplicates on re-run.
[ -f "${BASE_PROP}.vm-bak" ] || cp "$BASE_PROP" "${BASE_PROP}.vm-bak"

set_prop() {
    local key="$1" val="$2" file="$3"
    if grep -q "^${key}=" "$file"; then
        # escape & and | in val for sed
        local esc
        esc=$(printf '%s' "$val" | sed -e 's/[\/&|]/\\&/g')
        sed -i "s|^${key}=.*|${key}=${esc}|" "$file"
    else
        printf '%s=%s\n' "$key" "$val" >> "$file"
    fi
}

for kv in \
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
    "ro.board.platform=$PLATFORM" \
    "ro.hardware=$PLATFORM" \
    "ro.bootloader=$BOOTLOADER"; do
    key=${kv%%=*}; val=${kv#*=}
    set_prop "$key" "$val" "$BASE_PROP"
done

# ---------- 4. waydroid.cfg [properties] section ----------
# Belt-and-suspenders: anything set here gets applied via setprop at session
# boot. Harmless for values that waydroid_base.prop already baked in.
python3 - "$CFG" "$BRAND" "$MANUFACTURER" "$MODEL" "$DEVICE" "$PRODUCT" "$FINGERPRINT" "$SERIAL" <<'PY'
import configparser, sys
path, brand, mfr, model, device, product, fingerprint, serial = sys.argv[1:]
cp = configparser.RawConfigParser()
# Waydroid's .cfg uses "key = value" (with spaces) — RawConfigParser handles it.
cp.read(path)
if not cp.has_section("properties"):
    cp.add_section("properties")
for k, v in [
    ("ro.product.brand", brand),
    ("ro.product.manufacturer", mfr),
    ("ro.product.model", model),
    ("ro.product.device", device),
    ("ro.product.name", product),
    ("ro.build.fingerprint", fingerprint),
    ("ro.serialno", serial),
]:
    cp.set("properties", k, v)
with open(path, "w") as f:
    cp.write(f)
PY

# ---------- 5. manifest ----------
cat > "$MANIFEST" <<EOF
# VM device fingerprint — generated $(date -Is)
# Persistent identity bits; apply runtime bits via vm2_apply_runtime_identity.sh.
lxc_hwaddr=$NEW_MAC
brand=$BRAND
manufacturer=$MANUFACTURER
model=$MODEL
device=$DEVICE
product=$PRODUCT
platform=$PLATFORM
bootloader=$BOOTLOADER
serialno=$SERIAL
fingerprint=$FINGERPRINT
EOF
log "manifest: $MANIFEST"
cat "$MANIFEST"

# ---------- 6. restart container with the new config ----------
log "starting waydroid-container with new config"
systemctl start waydroid-container
sleep 3
systemctl is-active waydroid-container

log "done. Next: start the display stack to boot a session."
log "  scripts/vm2_start_display.sh"
log "  scripts/vm2_apply_runtime_identity.sh"
