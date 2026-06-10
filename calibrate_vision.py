"""Phase-0 calibration: read what classify_faces.py wrote and decide the bar.

Two jobs:
  1. Print the funnel numbers — status breakdown, vision female-rate, age and
     beauty-score distributions, the no-face fraction (which sizes how badly
     the Phase-1 cover fallback is needed), and how many authors clear a
     candidate gate at a given beauty bar.
  2. Build contact sheets — grids of avatars sampled from the top / median /
     bottom beauty deciles among confident adult females — so you can eyeball
     where to set the beauty threshold instead of guessing.

Needs S3 read (same iDrive creds as backfill_avatars) + DB read.

Usage:
  IDRIVE_ACCESS_KEY=.. IDRIVE_SECRET_KEY=.. python calibrate_vision.py
  ... python calibrate_vision.py --bar 3.2 --min-area 0.04 --per-sheet 64
"""

import argparse
import io
import os
import sys

import boto3
import psycopg2
from botocore.config import Config
from PIL import Image

DSN = os.environ.get(
    "TIKTOKS_DATABASE_URL",
    "postgresql://app1_user:app1dev@150.136.40.239:5432/tiktoks",
)
ENDPOINT = "https://" + os.environ.get("IDRIVE_ENDPOINT", "s3.us-east-1.idrivee2.com")
BUCKET = os.environ.get("AVATAR_BUCKET", "tt-avatars")


def s3_client():
    return boto3.client(
        "s3", endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ["IDRIVE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["IDRIVE_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def stats(cur, bar, min_area):
    def scalar(q, a=None):
        cur.execute(q, a); return cur.fetchone()[0]

    total = scalar("SELECT count(*) FROM author_vision")
    print(f"\n=== author_vision: {total:,} rows scored ===")
    cur.execute("SELECT status, count(*) FROM author_vision GROUP BY status ORDER BY 2 DESC")
    for s, n in cur.fetchall():
        print(f"  status {s:8s}: {n:,} ({100*n/total:.1f}%)")

    n_ok = scalar("SELECT count(*) FROM author_vision WHERE status='ok'")
    n_noface = scalar("SELECT count(*) FROM author_vision WHERE status='noface'")
    n_female = scalar("SELECT count(*) FROM author_vision WHERE status='ok' AND vis_is_female")
    print(f"\n  no-face fraction      : {100*n_noface/total:.1f}%  "
          f"(this is how much the cover fallback would recover)")
    print(f"  female rate (of ok)   : {100*n_female/max(n_ok,1):.1f}%")

    print("\n  age distribution (female, ok):")
    cur.execute("""SELECT width_bucket(age, 13, 60, 9) b, count(*) FROM author_vision
                   WHERE status='ok' AND vis_is_female AND age IS NOT NULL GROUP BY b ORDER BY b""")
    for b, n in cur.fetchall():
        lo = 13 + (b - 1) * (47 / 9)
        print(f"    ~{lo:4.0f}+: {n:,}")

    has_beauty = scalar("SELECT count(*) FROM author_vision WHERE beauty_score IS NOT NULL")
    if has_beauty:
        print("\n  beauty_score deciles (female, ok):")
        cur.execute("""
            SELECT decile, min(beauty_score), max(beauty_score), count(*)
            FROM (SELECT beauty_score, ntile(10) OVER (ORDER BY beauty_score) decile
                  FROM author_vision WHERE status='ok' AND vis_is_female
                    AND beauty_score IS NOT NULL) t
            GROUP BY decile ORDER BY decile""")
        for d, lo, hi, n in cur.fetchall():
            print(f"    d{d:>2}: {lo:.2f}..{hi:.2f}  ({n:,})")

        gate = scalar("""SELECT count(*) FROM author_vision
                         WHERE status='ok' AND vis_is_female AND age>=18
                           AND n_faces=1 AND face_area_frac>=%s AND beauty_score>=%s""",
                      (min_area, bar))
        print(f"\n  >>> candidate gate (female, age>=18, single face, "
              f"area>={min_area}, beauty>={bar}): {gate:,}")
        print(f"      per-avatar yield: {100*gate/total:.1f}%  "
              f"-> for 100k you need ~{int(100_000/max(gate/total,1e-9)):,} avatars classified")
    else:
        print("\n  (no beauty_score yet — run train_beauty_head.py + re-classify)")


def contact_sheets(cur, s3, per_sheet, out_dir, min_area):
    os.makedirs(out_dir, exist_ok=True)
    buckets = {
        "top":    "ORDER BY beauty_score DESC",
        "median": "ORDER BY abs(beauty_score - (SELECT percentile_cont(0.5) "
                  "WITHIN GROUP (ORDER BY beauty_score) FROM author_vision "
                  "WHERE status='ok' AND vis_is_female))",
        "bottom": "ORDER BY beauty_score ASC",
    }
    for label, order in buckets.items():
        cur.execute(f"""SELECT uid FROM author_vision
            WHERE status='ok' AND vis_is_female AND age>=18 AND n_faces=1
              AND face_area_frac>=%s AND beauty_score IS NOT NULL
            {order} LIMIT %s""", (min_area, per_sheet))
        uids = [r[0] for r in cur.fetchall()]
        if not uids:
            print(f"[sheet] {label}: no rows", file=sys.stderr); continue
        thumb, cols = 128, 8
        rows = (len(uids) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * thumb, rows * thumb), (20, 20, 20))
        for i, uid in enumerate(uids):
            try:
                b = s3.get_object(Bucket=BUCKET, Key=f"avatars/{uid}.jpg")["Body"].read()
                im = Image.open(io.BytesIO(b)).convert("RGB").resize((thumb, thumb))
                sheet.paste(im, ((i % cols) * thumb, (i // cols) * thumb))
            except Exception as e:
                print(f"[sheet] skip {uid}: {str(e)[:60]}", file=sys.stderr)
        path = os.path.join(out_dir, f"beauty_{label}.jpg")
        sheet.save(path, quality=88)
        print(f"[sheet] wrote {path} ({len(uids)} faces)", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar", type=float, default=3.0, help="beauty threshold to evaluate")
    ap.add_argument("--min-area", type=float, default=0.03,
                    help="min largest-face area / image area to count as a portrait")
    ap.add_argument("--per-sheet", type=int, default=64)
    ap.add_argument("--out-dir", default="calibration_sheets")
    ap.add_argument("--no-sheets", action="store_true", help="numbers only, skip S3 image pulls")
    args = ap.parse_args()

    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    stats(cur, args.bar, args.min_area)
    if not args.no_sheets:
        contact_sheets(cur, s3_client(), args.per_sheet, args.out_dir, args.min_area)


if __name__ == "__main__":
    main()
