"""
Try the int4444/tiktok-api Metasec signer end-to-end on our captured device.
If the phone-captured /aweme/v1/search/item/ call returns a real aweme_list
when we sign via Metasec, then its algorithm is closer to phone's MSSDK v5.
"""
import sys
import time
import json
import urllib.parse
import urllib.request
import urllib.error
import gzip
from pathlib import Path

# int4444's Mobile/ package uses implicit relative imports without a package
# prefix (`from exception import ...`), so we need to add the relevant
# directories to sys.path. We load Mobile/metasec.py directly via importlib
# to avoid the name clash with Mobile/cipher/metasec.py (which imports a
# non-existent `tiktok.core.metasec`).
ROOT = Path("/tmp/tiktok-api_int4444_tiktok-api/Mobile")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "helpers"))
sys.path.insert(0, str(ROOT / "cipher"))
sys.path.insert(0, str(ROOT / "protobuf"))

# Pre-register local packages under unique names to avoid colliding with
# the system-wide `protobuf` PyPI package.
import importlib.util as _iu
import types as _types

def _load_dir_as_package(name: str, path: Path):
    pkg = _types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg

# Load protobuf/protobuf.py as `protobuf.protobuf` — but since helpers/argus.py
# imports `from protobuf.protobuf import ProtoBuf`, we need `protobuf` to
# resolve to the LOCAL directory first. Pre-populate sys.modules.
_load_dir_as_package("protobuf", ROOT / "protobuf")
_load_dir_as_package("helpers", ROOT / "helpers")
_load_dir_as_package("cipher", ROOT / "cipher")
# Now when argus.py does `from protobuf.protobuf import ProtoBuf`, Python
# will search ROOT/protobuf/ for protobuf.py because `protobuf.__path__` points there.

import importlib.util as _iu
_spec = _iu.spec_from_file_location("_int4444_metasec", ROOT / "metasec.py")
_mod = _iu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Metasec = _mod.Metasec

from replay_search import (
    DEVICE, MSSDK, USER_AGENT, COOKIE, X_TT_TOKEN,
    TIKTOK_HOST, TIKTOK_PATH, build_query, call_tiktok,
)


def sign_query_metasec(query: str, mssdk: dict, device: dict,
                       app_launch_time: int | None = None,
                       cookies: str | None = None,
                       dyn_version: int | None = None,
                       dyn_seed: str | None = None) -> dict:
    """Sign with int4444's Metasec. Returns X- prefixed dict."""
    signer = Metasec()
    full_url = f"https://{TIKTOK_HOST}{TIKTOK_PATH}?{query}"

    if app_launch_time is None:
        app_launch_time = int(time.time()) - 60

    out = signer.sign(
        url=full_url,
        app_id=int(mssdk["mssdk_app_id"]),
        app_version=device["version_name"],
        app_launch_time=app_launch_time,
        device_type=device["device_type"],
        sdk_version=mssdk["mssdk_version"],
        sdk_version_code=mssdk["mssdk_version_int"],
        license_id=int(mssdk["mssdk_license_id"]),
        device_id=device["device_id"],
        dyn_version=dyn_version,
        dyn_seed=dyn_seed,
        payload=None,
        cookies=cookies,
    )
    return {
        "X-Argus": out["x-argus"],
        "X-Ladon": out["x-ladon"],
        "X-Gorgon": out["x-gorgon"],
        "X-Khronos": str(out["x-khronos"]),
    }


def main():
    kw = sys.argv[1] if len(sys.argv) > 1 else "mario"
    query = build_query(kw, 0, 10)

    # Try without dyn_*
    print(f"[1] Metasec sign WITHOUT dyn_*")
    sig = sign_query_metasec(query, MSSDK, DEVICE, cookies=COOKIE)
    for k, v in sig.items(): print(f"    {k}: {v[:40]}... (len={len(v)})")
    status, body, _ = call_tiktok(query, sig)
    if status == 200:
        p = json.loads(body)
        print(f"    status=200 status_code={p.get('status_code')} "
              f"aweme_count={len(p.get('aweme_list') or [])} "
              f"server_time={(p.get('extra') or {}).get('server_stream_time')}ms")
    else:
        print(f"    status={status} body={body[:200]!r}")

    # Also try dyn_version=5 (phone is SDK v05)
    print(f"\n[2] Metasec sign WITH dyn_version=5 dyn_seed='default_seed'")
    sig = sign_query_metasec(query, MSSDK, DEVICE, cookies=COOKIE,
                             dyn_version=5, dyn_seed="default_seed")
    for k, v in sig.items(): print(f"    {k}: {v[:40]}... (len={len(v)})")
    status, body, _ = call_tiktok(query, sig)
    if status == 200:
        p = json.loads(body)
        print(f"    status=200 status_code={p.get('status_code')} "
              f"aweme_count={len(p.get('aweme_list') or [])} "
              f"server_time={(p.get('extra') or {}).get('server_stream_time')}ms")
    else:
        print(f"    status={status} body={body[:200]!r}")


if __name__ == "__main__":
    main()
