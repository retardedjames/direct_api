"""
Burn a few RapidAPI sign calls to capture known-good signatures, saving
them with their full signing input (query, khronos, device, mssdk) so we
can diff against our self-signers OFFLINE without burning more quota.

Each record is saved to /home/james/tiktok-scraper/direct_api/oracles/<name>.json
and contains everything needed to recompute the signature deterministically.

Usage:
    python3 capture_oracles.py            # collects a default small batch
    python3 capture_oracles.py --n 5      # custom count
"""
import argparse
import json
import time
from pathlib import Path

from replay_search import (
    DEVICE, MSSDK, build_query, call_signer, call_tiktok,
)

ORACLE_DIR = Path(__file__).parent / "oracles"
ORACLE_DIR.mkdir(exist_ok=True)

# Vary the queries so we can see what parts of the sig are
# query-dependent vs query-independent.
DEFAULT_BATCH = [
    ("mario",       0, 10),
    ("kawaii desk", 0, 10),
    ("cat videos",  0, 10),
]


def capture_one(keyword: str, cursor: int, count: int, label: str):
    # Freeze all inputs so we can recompute deterministically later.
    unix_s = int(time.time())
    query = build_query(keyword, cursor, count)
    # build_query bakes current millis into _rticket and current seconds
    # into ts, so we need to extract those back out to reproduce later.
    import urllib.parse
    params = dict(urllib.parse.parse_qsl(query, keep_blank_values=True))
    _rticket = int(params["_rticket"])
    ts = int(params["ts"])

    sig = call_signer(query)

    # Also hit TikTok so we know the signature was server-accepted AND
    # produced non-empty results. If it doesn't, there's no point
    # using this sample as an oracle.
    status, body, hdrs = call_tiktok(query, sig)
    server_time = None
    aweme_count = None
    try:
        parsed = json.loads(body)
        aweme_count = len(parsed.get("aweme_list") or [])
        server_time = (parsed.get("extra") or {}).get("server_stream_time")
    except Exception:
        parsed = None

    record = {
        "label": label,
        "keyword": keyword,
        "cursor": cursor,
        "count": count,
        "unix_capture": unix_s,
        "_rticket": _rticket,
        "ts": ts,
        "query": query,
        "params": params,  # easy-to-diff key/value form
        "device": DEVICE,
        "mssdk": MSSDK,
        "rapidapi_sig": sig,  # {"X-Argus","X-Ladon","X-Gorgon","X-Khronos"}
        "tt_status": status,
        "tt_aweme_count": aweme_count,
        "tt_server_stream_time_ms": server_time,
    }
    path = ORACLE_DIR / f"{label}.json"
    path.write_text(json.dumps(record, indent=2))
    print(f"[{label}] sig x-gorgon={sig['X-Gorgon']}")
    print(f"         x-argus[:40]={sig['X-Argus'][:40]}... (len={len(sig['X-Argus'])})")
    print(f"         tiktok status={status} aweme={aweme_count} "
          f"server_time={server_time}ms -> {path}")
    return record


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=len(DEFAULT_BATCH),
                    help="how many of the default batch to collect")
    args = ap.parse_args()

    for i, (kw, cursor, count) in enumerate(DEFAULT_BATCH[: args.n]):
        label = f"oracle_{i:02d}_{kw.replace(' ', '_')}_c{cursor}"
        capture_one(kw, cursor, count, label)
        time.sleep(1.5)  # polite spacing


if __name__ == "__main__":
    main()
