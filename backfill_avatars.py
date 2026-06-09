"""Backfill TikTok creator profile pictures via the PUBLIC profile page.

No internal search/user API, no cookie, no logged-in account — so there is no
account to blacklist; the only exposure is per-IP page-rate, which we keep
polite and can spread across the fleet's IPs via --shard.

Per author:
  1. GET https://www.tiktok.com/@<unique_id>   (public HTML, gzip)
  2. pull avatarLarger (fallback medium/thumb) out of the rehydration JSON
  3. download the JPEG from the (public, unsigned-fetch) CDN url
  4. upload the bytes to iDrive e2 (S3) at avatars/<uid>.jpg
  5. record state in the author_avatar table (resumable)

The CDN signature expires in ~2 days so we store the BYTES, plus the stable
path hash (avatar_id) as durable identity.

Priority: female-candidate authors first — everyone NOT marked is_female=false
in author_gender — ordered by follower_count desc. Pass --include-males to
also sweep the known-male accounts.

Env (required): IDRIVE_ACCESS_KEY, IDRIVE_SECRET_KEY
Env (optional): IDRIVE_ENDPOINT (default s3.us-east-1.idrivee2.com),
                AVATAR_BUCKET (default tt-avatars),
                TIKTOKS_DATABASE_URL

Usage:
  IDRIVE_ACCESS_KEY=.. IDRIVE_SECRET_KEY=.. \
    ./venv/bin/python3 backfill_avatars.py --limit 25          # small test
  ... backfill_avatars.py --shard 0 --of 5                     # one fleet IP
"""

import argparse
import gzip
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

import boto3
import psycopg2
from botocore.config import Config
from psycopg2.extras import execute_values

DSN = os.environ.get(
    "TIKTOKS_DATABASE_URL",
    "postgresql://app1_user:app1dev@150.136.40.239:5432/tiktoks",
)
ENDPOINT = "https://" + os.environ.get("IDRIVE_ENDPOINT", "s3.us-east-1.idrivee2.com")
BUCKET = os.environ.get("AVATAR_BUCKET", "tt-avatars")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_AV_RE = {sz: re.compile(rf'"avatar{sz}":"(.*?)"')
          for sz in ("Larger", "Medium", "Thumb")}
_HASH_RE = re.compile(r"/([0-9a-f]{16,})[~/]")


def http_get(url: str, timeout: int = 25, binary: bool = False):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,*/*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return r.status, data


def extract_avatar_url(html: str) -> str | None:
    for sz in ("Larger", "Medium", "Thumb"):
        m = _AV_RE[sz].search(html)
        if m and m.group(1):
            # JSON / -> /  etc.
            return m.group(1).encode().decode("unicode_escape")
    return None


def jpeg_size(data: bytes):
    """Width/height from JPEG SOF markers, no PIL. Returns (w,h) or (None,None)."""
    try:
        i, n = 2, len(data)
        while i < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = (data[i + 5] << 8) | data[i + 6]
                w = (data[i + 7] << 8) | data[i + 8]
                return w, h
            seg = (data[i + 2] << 8) | data[i + 3]
            i += 2 + seg
    except Exception:
        pass
    return None, None


def s3_client():
    ak = os.environ["IDRIVE_ACCESS_KEY"]
    sk = os.environ["IDRIVE_SECRET_KEY"]
    return boto3.client(
        "s3", endpoint_url=ENDPOINT,
        aws_access_key_id=ak, aws_secret_access_key=sk,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def fetch_worklist(cur, shard, of, include_males, limit):
    male_clause = "" if include_males else \
        "AND (g.is_female IS DISTINCT FROM false)"
    cur.execute(f"""
        SELECT a.uid, a.unique_id
        FROM authors a
        LEFT JOIN author_gender g ON g.uid = a.uid
        WHERE a.unique_id <> ''
          AND NOT EXISTS (
            SELECT 1 FROM author_avatar av
            WHERE av.uid = a.uid AND av.status IN ('ok','notfound','private'))
          {male_clause}
          AND (a.uid %% %s) = %s
        ORDER BY a.follower_count DESC NULLS LAST
        LIMIT %s
    """, (of, shard, limit))
    return cur.fetchall()


def upsert(cur, row):
    execute_values(cur, """
        INSERT INTO author_avatar
          (uid, avatar_id, avatar_url, img_path, width, height, bytes, status, http_code)
        VALUES %s
        ON CONFLICT (uid) DO UPDATE SET
          avatar_id=EXCLUDED.avatar_id, avatar_url=EXCLUDED.avatar_url,
          img_path=EXCLUDED.img_path, width=EXCLUDED.width, height=EXCLUDED.height,
          bytes=EXCLUDED.bytes, status=EXCLUDED.status, http_code=EXCLUDED.http_code,
          fetched_at=now()
    """, [row])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1, help="number of shards (IPs)")
    ap.add_argument("--include-males", action="store_true")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="base seconds between profile fetches (jittered)")
    ap.add_argument("--max-blocks", type=int, default=8,
                    help="abort after this many consecutive 4xx blocks (protect IP)")
    args = ap.parse_args()

    s3 = s3_client()
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()

    work = fetch_worklist(cur, args.shard, args.of, args.include_males, args.limit)
    print(f"[avatars] shard {args.shard}/{args.of}: {len(work)} authors to fetch",
          file=sys.stderr)

    n_ok = n_nf = n_err = 0
    consecutive_blocks = 0
    for idx, (uid, handle) in enumerate(work, 1):
        status = http = None
        avatar_url = avatar_id = key = None
        w = h = nbytes = None
        try:
            code, html = http_get(f"https://www.tiktok.com/@{handle}")
            http = code
            avatar_url = extract_avatar_url(html.decode("utf-8", "replace"))
            if not avatar_url:
                status = "notfound"           # 200 but no user/avatar in payload
                n_nf += 1
            else:
                m = _HASH_RE.search(avatar_url)
                avatar_id = m.group(1) if m else None
                icode, img = http_get(avatar_url, binary=True)
                w, h = jpeg_size(img)
                nbytes = len(img)
                key = f"avatars/{uid}.jpg"
                s3.put_object(Bucket=BUCKET, Key=key, Body=img,
                              ContentType="image/jpeg")
                status = "ok"
                n_ok += 1
            consecutive_blocks = 0
        except urllib.error.HTTPError as e:
            http = e.code
            if e.code in (404, 410):
                status = "notfound"; n_nf += 1; consecutive_blocks = 0
            elif e.code in (403, 429):
                status = "error"; n_err += 1
                consecutive_blocks += 1
                back = min(60, 5 * consecutive_blocks) + random.random() * 3
                print(f"[avatars] BLOCK {e.code} on @{handle} -> backoff {back:.0f}s "
                      f"({consecutive_blocks}/{args.max_blocks})", file=sys.stderr)
                time.sleep(back)
            else:
                status = "error"; n_err += 1
        except Exception as e:
            status = "error"; n_err += 1
            print(f"[avatars] err @{handle}: {type(e).__name__}: {str(e)[:80]}",
                  file=sys.stderr)

        upsert(cur, (uid, avatar_id, avatar_url, key, w, h, nbytes, status, http))
        if idx % 25 == 0:
            conn.commit()
            print(f"[avatars] {idx}/{len(work)} ok={n_ok} notfound={n_nf} err={n_err}",
                  file=sys.stderr)

        if consecutive_blocks >= args.max_blocks:
            print(f"[avatars] ABORT: {consecutive_blocks} consecutive blocks — "
                  "IP likely throttled, stopping to stay safe", file=sys.stderr)
            break
        time.sleep(args.delay + random.random() * args.delay)

    conn.commit()
    print(f"[avatars] DONE ok={n_ok} notfound={n_nf} err={n_err}", file=sys.stderr)


if __name__ == "__main__":
    main()
