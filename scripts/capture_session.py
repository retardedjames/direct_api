#!/usr/bin/env python3
"""
Read a clone's TT Lite shared_prefs files and emit replay_search_vmN.py.

Usage (run on the new VM):
    sudo python3 capture_session.py vm5 > ~/direct_api/replay_search_vm5.py

The file is gitignored. Bound to this VM's install_id — copying to another
clone will silent-reject.

Pulls:
  - cookies (sid_guard, sessionid, sid_tt, uid_tt, store-*, odin_tt,
    cmpl_token, sessionid_ss, uid_tt_ss, tt_session_tlb_tag,
    store-country-sign, store-country-code-src, tt-target-idc) from
    /data/data/com.tiktok.lite.go/shared_prefs/ttnetCookieStore.xml
  - device_id, install_id from applog_stats.xml
  - cdid from com.ss.android.deviceregister.utils.Cdid.xml
  - openudid (and clientudid) from push_multi_process_config.xml's ssids
  - X-Tt-Token from token_shared_preference.xml
  - os_version, os_api, model, brand, build_fingerprint via getprop
"""
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

PKG = "com.tiktok.lite.go"
SP_DIR = f"/data/data/{PKG}/shared_prefs"
LXC = ["sudo", "lxc-attach", "-P", "/var/lib/waydroid/lxc", "-n", "waydroid", "--"]


def lxc_cat(path):
    return subprocess.check_output(LXC + ["cat", path], text=True)


def adb(*args):
    return subprocess.check_output(
        ["adb", "-s", "127.0.0.1:5556", "shell", *args], text=True
    ).strip()


def parse_simple_xml(xml_text):
    """Parse Android shared_prefs XML into {name: value}."""
    root = ET.fromstring(xml_text)
    out = {}
    for el in root:
        name = el.get("name")
        if name is None:
            continue
        if el.tag == "string":
            out[name] = el.text or ""
        elif el.tag in ("int", "long", "boolean"):
            out[name] = el.get("value", "")
    return out


def decode_serialized_cookie(hex_str):
    """
    SerializableHttpCookie.writeObject hex blob (Java ObjectOutputStream).
    Each string field is `74 <2-byte-BE-len> <utf8 bytes>` (TC_STRING marker
    0x74 then writeUTF). Sequence within a cookie blob:
        [class header...] xpt 74 LL LL <name>
                              74 LL LL <value>
                              74 LL LL <domain>
                              [long expiry, byte path, ...]
    Return (name, value).
    """
    blob = bytes.fromhex(hex_str)
    strings = []
    i = 0
    while i < len(blob) - 2:
        if blob[i] == 0x74:  # TC_STRING
            ln = (blob[i + 1] << 8) | blob[i + 2]
            start = i + 3
            end = start + ln
            if end <= len(blob):
                try:
                    s = blob[start:end].decode("utf-8")
                except UnicodeDecodeError:
                    i += 1
                    continue
                # Java class names contain dots/dollar/slashes — skip them.
                if "." in s or "$" in s:
                    i = end
                    continue
                strings.append(s)
                i = end
                continue
        i += 1
    if len(strings) < 2:
        return None, None
    return strings[0], strings[1]


def collect_cookies(xml_text):
    """Return {cookie_name: cookie_value}, last-write-wins on duplicates."""
    root = ET.fromstring(xml_text)
    out = {}
    for el in root:
        if el.tag != "string":
            continue
        hex_blob = (el.text or "").strip()
        if not hex_blob:
            continue
        name, value = decode_serialized_cookie(hex_blob)
        if name and value is not None:
            out[name] = value
    return out


def main():
    if len(sys.argv) != 2:
        print("usage: capture_session.py vmN", file=sys.stderr)
        sys.exit(2)
    vm = sys.argv[1]

    cookies = collect_cookies(lxc_cat(f"{SP_DIR}/ttnetCookieStore.xml"))

    must_have = ("sid_guard", "sessionid", "sid_tt", "uid_tt")
    missing = [c for c in must_have if c not in cookies]
    if missing:
        print(
            f"ERROR: missing required session cookies: {missing}\n"
            f"Did you complete VNC signup in TT Lite? "
            f"Available cookies: {sorted(cookies)}",
            file=sys.stderr,
        )
        sys.exit(3)

    applog = parse_simple_xml(lxc_cat(f"{SP_DIR}/applog_stats.xml"))
    cdid_xml = parse_simple_xml(
        lxc_cat(f"{SP_DIR}/com.ss.android.deviceregister.utils.Cdid.xml")
    )
    push_xml = parse_simple_xml(
        lxc_cat(f"{SP_DIR}/push_multi_process_config.xml")
    )
    token_xml = parse_simple_xml(
        lxc_cat(f"{SP_DIR}/token_shared_preference.xml")
    )

    device_id = applog["device_id"]
    install_id = applog["install_id"]
    cdid = cdid_xml["cdid"]
    x_tt_token = token_xml["X-Tt-Token"]

    ssids_raw = push_xml.get("ssids", "")
    ssids = dict(re.findall(r'"([^"]+)":"([^"]+)"', ssids_raw))
    openudid = ssids.get("openudid", "")
    if not openudid:
        print(f"ERROR: openudid missing from ssids: {ssids_raw!r}", file=sys.stderr)
        sys.exit(4)

    model = adb("getprop", "ro.product.model")
    brand = adb("getprop", "ro.product.brand")
    fingerprint = adb("getprop", "ro.build.fingerprint")
    os_version = adb("getprop", "ro.build.version.release")
    os_api = adb("getprop", "ro.build.version.sdk")
    build_tag = fingerprint.split("/")[4].split(":")[0] if fingerprint.count("/") >= 4 else "TP1A.220624.014"

    cookie_order = [
        "install_id",
        "store-idc",
        "store-country-code",
        "store-country-code-src",
        "tt-target-idc",
        "odin_tt",
        "cmpl_token",
        "sid_guard",
        "uid_tt",
        "uid_tt_ss",
        "sid_tt",
        "sessionid",
        "sessionid_ss",
        "tt_session_tlb_tag",
        "store-country-sign",
    ]
    cookie_pairs = []
    cookie_pairs.append(f"install_id={install_id}")
    for name in cookie_order[1:]:
        if name in cookies:
            cookie_pairs.append(f"{name}={cookies[name]}")
    cookie_str = "; ".join(cookie_pairs)

    user_agent = (
        f"com.tiktok.lite.go/430553 (Linux; U; Android {os_version}; en_US; "
        f"{model}; Build/{build_tag};tt-ok/3.12.13.51.lite-ul)"
    )

    out = f'''"""
{vm.upper()} identity file — gitignored, never commit.
Generated by scripts/capture_session.py.
"""

TIKTOK_HOST = "api19-normal-useast8.tiktokv.us"
TIKTOK_PATH = "/aweme/v1/search/item/"

DEVICE = {{
    "aid": "1340",
    "app_name": "musically_go",
    "app_package": "com.tiktok.lite.go",
    "version_code": "430553",
    "version_name": "43.5.53",
    "manifest_version_code": "430553",
    "update_version_code": "430553",
    "ab_version": "43.5.53",
    "build_number": "43.5.53",
    "device_id": "{device_id}",
    "iid": "{install_id}",
    "openudid": "{openudid}",
    "cdid": "{cdid}",
    "device_brand": "{brand}",
    "device_type": "{model}",
    "device_platform": "android",
    "os": "android",
    "os_version": "{os_version}",
    "os_api": "{os_api}",
    "resolution": "1078*2103",
    "dpi": "180",
    "host_abi": "arm64-v8a",
    "channel": "googleplay",
    "sys_region": "US",
    "op_region": "US",
    "region": "US",
    "locale": "en-US",
    "language": "en",
    "app_language": "en",
    "timezone_name": "America/New_York",
    "timezone_offset": "-18000",
    "ac": "wifi",
    "ac2": "wifi",
    "ssmix": "a",
    "app_type": "normal",
}}

MSSDK = {{
    "mssdk_app_id": 1340,
    "mssdk_license_id": "224921550",
    "mssdk_version": "v05.01.05-alpha.5-ov-android",
    "mssdk_version_int": 83952928,
}}

USER_AGENT = (
    "{user_agent}"
)

COOKIE = (
    "{cookie_str}"
)

X_TT_TOKEN = (
    "{x_tt_token}"
)

SEARCH_PARAM_ORDER = [
    "cursor", "sort_type", "enter_from", "count", "source", "keyword",
    "query_correct_type", "is_filter_search", "search_source", "search_id",
    "request_tag_from",
    "_rticket", "manifest_version_code", "app_language", "app_type", "iid",
    "app_package", "channel", "device_type", "language", "host_abi", "locale",
    "resolution", "openudid", "update_version_code", "ac2", "cdid",
    "sys_region", "os_api", "timezone_name", "dpi", "ac", "os", "device_id",
    "os_version", "timezone_offset", "version_code", "app_name", "ab_version",
    "version_name", "device_brand", "op_region", "ssmix", "device_platform",
    "build_number", "region", "aid", "ts",
]
'''
    print(out)


if __name__ == "__main__":
    main()
