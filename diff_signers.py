"""
Take a captured oracle (known-good RapidAPI signature) and re-sign the
SAME query at the same khronos with each self-signer we have. Compare
outputs byte-by-byte to find where they diverge.

This is offline — doesn't burn RapidAPI quota.

Usage:
    python3 diff_signers.py oracles/oracle_00_mario_c0.json
"""
import argparse
import base64
import json
import sys
from pathlib import Path


def load_oracle(path: str) -> dict:
    return json.loads(Path(path).read_text())


def sign_signerpy(oracle: dict) -> dict:
    from SignerPy import sign as _sign
    q = oracle["query"]
    mssdk = oracle["mssdk"]
    s = _sign(
        params=q,
        aid=int(mssdk["mssdk_app_id"]),
        license_id=int(mssdk["mssdk_license_id"]),
        sdk_version_str=mssdk["mssdk_version"],
        sdk_version=mssdk["mssdk_version_int"],
        unix=oracle["ts"],
        version=8404,
    )
    return {
        "X-Argus": s["x-argus"],
        "X-Ladon": s["x-ladon"],
        "X-Gorgon": s["x-gorgon"],
        "X-Khronos": s["x-khronos"],
    }


def sign_metasec(oracle: dict) -> dict:
    """int4444/tiktok-api Metasec. Loads via importlib hack."""
    import sys as _sys, types as _types, importlib.util as _iu
    from pathlib import Path as _P
    ROOT = _P("/tmp/tiktok-api_int4444_tiktok-api/Mobile")
    for p in (ROOT, ROOT / "helpers", ROOT / "cipher", ROOT / "protobuf"):
        if str(p) not in _sys.path:
            _sys.path.insert(0, str(p))
    for name, path in (
        ("protobuf", ROOT / "protobuf"),
        ("helpers", ROOT / "helpers"),
        ("cipher", ROOT / "cipher"),
    ):
        if name not in _sys.modules or not getattr(_sys.modules[name], "__path__", None):
            pkg = _types.ModuleType(name)
            pkg.__path__ = [str(path)]
            _sys.modules[name] = pkg
    spec = _iu.spec_from_file_location("_int4444_metasec", ROOT / "metasec.py")
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    q = oracle["query"]
    mssdk = oracle["mssdk"]
    dev = oracle["device"]
    import time
    signer = mod.Metasec()
    url = f"https://api19-normal-useast8.tiktokv.us/aweme/v1/search/item/?{q}"
    # Can't inject a fixed unix — Metasec calls time.time() internally.
    # Monkey-patch time.time to return oracle ts so comparison is apples-to-apples.
    real_time = time.time
    time.time = lambda: oracle["ts"]
    try:
        out = signer.sign(
            url=url,
            app_id=int(mssdk["mssdk_app_id"]),
            app_version=dev["version_name"],
            app_launch_time=oracle["ts"] - 60,
            device_type=dev["device_type"],
            sdk_version=mssdk["mssdk_version"],
            sdk_version_code=mssdk["mssdk_version_int"],
            license_id=int(mssdk["mssdk_license_id"]),
            device_id=dev["device_id"],
            payload=None,
            cookies=None,
        )
    finally:
        time.time = real_time
    return {
        "X-Argus": out["x-argus"],
        "X-Ladon": out["x-ladon"],
        "X-Gorgon": out["x-gorgon"],
        "X-Khronos": str(out["x-khronos"]),
    }


def sign_iqbalmh18(oracle: dict) -> dict:
    from tiktok_signer import TikTokSigner
    q = oracle["query"]
    mssdk = oracle["mssdk"]
    dev = oracle["device"]
    h = TikTokSigner.generate_headers(
        params=q,
        device_id=dev["device_id"],
        aid=int(mssdk["mssdk_app_id"]),
        lc_id=int(mssdk["mssdk_license_id"]),
        sdk_ver=mssdk["mssdk_version"],
        sdk_ver_code=mssdk["mssdk_version_int"],
        version_name=dev["version_name"],
        version_code=int(dev["version_code"]),
        cookie=None,
        unix=oracle["ts"],
    )
    return {
        "X-Argus": h.get("x-argus", ""),
        "X-Ladon": h.get("x-ladon", ""),
        "X-Gorgon": h.get("x-gorgon", ""),
        "X-Khronos": h.get("x-khronos", ""),
    }


def decode_b64_len(s: str) -> int:
    try:
        return len(base64.b64decode(s))
    except Exception:
        return -1


def show(label: str, sig: dict):
    print(f"--- {label} ---")
    for k in ("X-Gorgon", "X-Ladon", "X-Argus", "X-Khronos"):
        v = sig.get(k, "")
        if k in ("X-Argus", "X-Ladon"):
            print(f"  {k:9} (len={len(v):4}, decoded={decode_b64_len(v):4}): {v[:60]}...")
        else:
            print(f"  {k:9} (len={len(v):4}): {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("oracle")
    args = ap.parse_args()

    oracle = load_oracle(args.oracle)
    print(f"ORACLE: {oracle['label']}  keyword={oracle['keyword']!r}  "
          f"ts={oracle['ts']}  server_time={oracle['tt_server_stream_time_ms']}ms  "
          f"awemes={oracle['tt_aweme_count']}")
    print(f"Query (first 100 chars): {oracle['query'][:100]}")
    print()

    # 1. RapidAPI (known-good)
    show("RapidAPI (known-good)", oracle["rapidapi_sig"])

    # 2. SignerPy
    try:
        show("SignerPy 0.12.0", sign_signerpy(oracle))
    except Exception as e:
        print(f"SignerPy failed: {e}")

    # 3. iqbalmh18
    try:
        show("iqbalmh18 tiktok-signer 1.3.1", sign_iqbalmh18(oracle))
    except Exception as e:
        print(f"iqbalmh18 failed: {e}")

    # 4. int4444/Metasec
    try:
        show("int4444 Metasec", sign_metasec(oracle))
    except Exception as e:
        print(f"Metasec failed: {e}")


if __name__ == "__main__":
    main()
