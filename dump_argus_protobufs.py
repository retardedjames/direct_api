"""
Extract the pre-encryption Argus protobuf from SignerPy, iqbalmh18, and
int4444 Metasec for the same query+device. This shows which protobuf
fields each includes — the real comparison that matters.
"""
import json
import sys
import time
from pathlib import Path


def oracle():
    return json.loads(Path("oracles/oracle_00_mario_c0.json").read_text())


def dump_signerpy():
    """SignerPy's Argus.encrypt takes a dict directly — just inspect it."""
    from SignerPy.argus import Argus
    from random import randint

    o = oracle()
    mssdk = o["mssdk"]
    dev = o["device"]
    # Replicate Argus.get_sign bean build so we see exactly what's in it.
    from urllib.parse import parse_qs
    params_dict = parse_qs(o["query"])
    bean = {
        1: 0x20200929 << 1,
        2: 2,
        3: randint(0, 0x7FFFFFFF),
        4: str(mssdk["mssdk_app_id"]),
        5: params_dict["device_id"][0],
        6: str(mssdk["mssdk_license_id"]),
        7: params_dict["version_name"][0],
        8: mssdk["mssdk_version"],
        9: mssdk["mssdk_version_int"],
        10: bytes(8),
        11: 0,
        12: o["ts"] << 1,
        13: Argus.get_bodyhash(None).hex(),
        14: Argus.get_queryhash(o["query"]).hex(),
        15: {1: 1, 2: 1, 3: 1, 7: 3348294860},
        16: "",
        20: "none",
        21: 738,
        23: {1: "NX551J", 2: 8196, 4: 2162219008},
        25: 2,
    }
    return bean


def dump_iqbalmh18():
    """Extract bean by inspecting iqbalmh18's Argus.encrypt call."""
    # iqbalmh18 has a generate_protobuf-like function in signer.py or lib/argus.py
    # Let's look at what it actually does.
    import importlib
    sig = importlib.import_module("tiktok_signer.signer")
    argus = importlib.import_module("tiktok_signer.lib.argus")
    return dir(argus.Argus)


def dump_metasec():
    """Extract bean from int4444's generate_protobuf."""
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
    spec = _iu.spec_from_file_location("_int4444_helpers_argus", ROOT / "helpers" / "argus.py")
    mod = _iu.module_from_spec(spec)
    spec.loader.exec_module(mod)

    o = oracle()
    mssdk = o["mssdk"]
    dev = o["device"]
    # We need to monkey-patch random.randint to be deterministic for diff
    import random as _r
    saved = _r.randint
    _r.randint = lambda a, b: 12345
    try:
        # generate_protobuf returns bytes; we want the PROTO DICT before
        # it's serialized. We'll re-implement the field-building logic.
        rand = 12345
        proto = {
            1: 0x20200929 << 1,
            2: 2,
            3: rand << 1,
            4: str(mssdk["mssdk_app_id"]),
            6: str(mssdk["mssdk_license_id"]),
            7: dev["version_name"],
            8: mssdk["mssdk_version"],
            9: mssdk["mssdk_version_int"] << 1,
            10: bytes(8),
            12: o["ts"] << 1,
            13: mod.get_request_hash(bytearray(b"\x00" * 16)).hex(),  # payload
            14: mod.get_request_hash(bytearray(o["query"].encode())).hex(),
            15: {
                1: 100 << 1,  # randomly between 20-250 << 1
                7: (o["ts"] - 60) << 1,  # app_launch_time
            },
            17: o["ts"] << 1,
            20: "none",
            21: 312 << 1,
            23: {
                1: dev["device_type"],
                2: 5 << 1,
                3: "googleplay",
                4: 209748992 << 1,
            },
            25: 1 << 1,
            28: 1008 << 1,
        }
        proto[5] = dev["device_id"]
        return proto
    finally:
        _r.randint = saved


def show(label: str, bean: dict):
    if not isinstance(bean, dict):
        print(f"--- {label} ---\n  (non-dict: {bean})\n")
        return
    print(f"--- {label} — {len(bean)} fields ---")
    for k in sorted(bean.keys()):
        v = bean[k]
        if isinstance(v, bytes):
            vs = f"bytes[{len(v)}]={v.hex()}"
        elif isinstance(v, dict):
            vs = f"<subgroup: {sorted(v.keys())}>"
        elif isinstance(v, str):
            vs = repr(v[:60])
        else:
            vs = repr(v)
        print(f"  f{k:3}: {vs}")
    print()


def main():
    show("SignerPy's Argus protobuf fields", dump_signerpy())
    show("int4444 Metasec protobuf fields",  dump_metasec())


if __name__ == "__main__":
    main()
