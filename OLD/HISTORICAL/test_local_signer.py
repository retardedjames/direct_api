"""
End-to-end test: sign locally with SignerPy, hit TikTok directly, report result.

Usage:
    python3 test_local_signer.py mario
    python3 test_local_signer.py "kawaii desk"
"""

import argparse
import json
import sys
import time

from replay_search import (
    DEVICE, MSSDK, build_query, call_tiktok,
)
from local_signer import sign_query


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keyword")
    ap.add_argument("--cursor", type=int, default=0)
    ap.add_argument("--count", type=int, default=10,
                    help="phone uses 10; 30 is confirmed upper limit")
    args = ap.parse_args()

    query = build_query(args.keyword, args.cursor, args.count)
    print(f"[query-len] {len(query)} chars", file=sys.stderr)

    sig = sign_query(query, MSSDK)
    print(f"[sig] X-Khronos={sig['X-Khronos']} X-Gorgon={sig['X-Gorgon'][:20]}...",
          file=sys.stderr)
    print(f"      X-Argus={sig['X-Argus'][:40]}...", file=sys.stderr)
    print(f"      X-Ladon={sig['X-Ladon'][:40]}...", file=sys.stderr)

    status, body, hdrs = call_tiktok(query, sig)
    print(f"[tiktok] status={status} bytes={len(body)}", file=sys.stderr)

    try:
        parsed = json.loads(body)
    except Exception as e:
        parsed = None
        print(f"[err] JSON parse failed: {e}", file=sys.stderr)
        print(body[:800].decode("utf-8", "replace"), file=sys.stderr)
        sys.exit(1)

    status_code = parsed.get("status_code")
    status_msg = parsed.get("status_msg")
    awemes = parsed.get("aweme_list") or []
    print(f"[tiktok] status_code={status_code} status_msg={status_msg!r} "
          f"aweme_count={len(awemes)}", file=sys.stderr)

    if awemes:
        stats0 = awemes[0].get("statistics") or {}
        print(f"[aweme0] id={awemes[0].get('aweme_id')} "
              f"likes={stats0.get('digg_count')} "
              f"desc={(awemes[0].get('desc') or '')[:60]!r}", file=sys.stderr)
    else:
        # On failure, print the body — it will contain a reason
        print("[tiktok] no awemes. Body head:", file=sys.stderr)
        print(json.dumps(parsed, indent=2)[:1200], file=sys.stderr)


if __name__ == "__main__":
    main()
