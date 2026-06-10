"""Export the attractive-female candidate list from the vision pass.

Engagement-primary recipe (validated 2026-06-10): the vision model is a
reliable *gender + real-adult-face* gate but a weak beauty *ranker* on messy
TikTok avatars, so we gate on the trustworthy signals and rank by popularity
(the "popular because attractive" thesis). The beauty_score is only a coarse
junk-floor — keep it LOW (~3.0); it drops distorted/stylized avatars but must
not be used to rank (e.g. very attractive creators can score ~3.0).

Gate (all configurable):
  vis_is_female AND age in [--age-min, --age-max] AND n_faces=1
  AND det_score >= --min-det        (>=0.78 strips cartoons/illustrations)
  AND face_area_frac >= --min-area   (real portrait, not a tiny face)
  AND beauty_score >= --beauty-floor (coarse junk-floor only)
Rank:
  --rank followers   -> ORDER BY follower_count DESC               (default)
  --rank blend       -> w*norm(beauty) + (1-w)*norm(log1p(followers))

Outputs (any subset): a CSV of handles, a replace-in-place DB table, and an
optional contact sheet so you can eyeball the pool.

Usage:
  python export_candidates.py                                  # CSV + table, top 100k
  python export_candidates.py --beauty-floor 3.0 --min-det 0.82 --limit 50000
  python export_candidates.py --max-followers 5000000          # drop mega-celebs
  IDRIVE_ACCESS_KEY=.. IDRIVE_SECRET_KEY=.. \
    python export_candidates.py --contact-sheet candidates.jpg --sheet-sample spread

Env (only for --contact-sheet): IDRIVE_ACCESS_KEY, IDRIVE_SECRET_KEY,
     AVATAR_BUCKET (default tt-avatars). DB via TIKTOKS_DATABASE_URL.
"""

import argparse
import csv
import io
import os
import sys

import psycopg2

DSN = os.environ.get(
    "TIKTOKS_DATABASE_URL",
    "postgresql://app1_user:app1dev@150.136.40.239:5432/tiktoks",
)
BUCKET = os.environ.get("AVATAR_BUCKET", "tt-avatars")


def build_query(args):
    gate = [
        "v.status='ok'", "v.vis_is_female",
        "v.age BETWEEN %(age_min)s AND %(age_max)s",
        "v.n_faces=1",
        "v.det_score >= %(min_det)s",
        "v.face_area_frac >= %(min_area)s",
        "v.beauty_score >= %(beauty_floor)s",
        "a.unique_id <> ''",
    ]
    if args.max_followers:
        gate.append("a.follower_count <= %(max_followers)s")
    if args.min_followers:
        gate.append("a.follower_count >= %(min_followers)s")
    where = " AND ".join(gate)

    if args.rank == "blend":
        # normalise beauty and log-followers to 0..1 across the gated set, blend.
        score = (f"%(blend_w)s * (v.beauty_score - mn.bmin)/NULLIF(mn.bmax-mn.bmin,0) "
                 f"+ (1-%(blend_w)s) * (ln(1+a.follower_count) - mn.fmin)/NULLIF(mn.fmax-mn.fmin,0)")
        sql = f"""
          WITH g AS (
            SELECT v.uid, a.unique_id, a.follower_count, v.beauty_score, v.age, v.det_score
            FROM author_vision v JOIN authors a ON a.uid=v.uid
            WHERE {where}
          ), mn AS (
            SELECT min(beauty_score) bmin, max(beauty_score) bmax,
                   min(ln(1+follower_count)) fmin, max(ln(1+follower_count)) fmax FROM g
          )
          SELECT g.uid, g.unique_id, g.follower_count, g.beauty_score, g.age, g.det_score,
                 ({score}) AS final_score
          FROM g, mn ORDER BY final_score DESC NULLS LAST LIMIT %(limit)s
        """
    else:
        sql = f"""
          SELECT v.uid, a.unique_id, a.follower_count, v.beauty_score, v.age, v.det_score,
                 a.follower_count::float8 AS final_score
          FROM author_vision v JOIN authors a ON a.uid=v.uid
          WHERE {where}
          ORDER BY a.follower_count DESC NULLS LAST LIMIT %(limit)s
        """
    params = dict(
        age_min=args.age_min, age_max=args.age_max, min_det=args.min_det,
        min_area=args.min_area, beauty_floor=args.beauty_floor,
        max_followers=args.max_followers, min_followers=args.min_followers,
        blend_w=args.blend_w, limit=args.limit,
    )
    return sql, params


def write_table(cur, conn, table, rows):
    cur.execute(f"DROP TABLE IF EXISTS {table}")
    cur.execute(f"""CREATE TABLE {table} (
        rank int, uid bigint, unique_id text, follower_count bigint,
        beauty_score real, age real, det_score real, final_score float8)""")
    from psycopg2.extras import execute_values
    execute_values(cur, f"""INSERT INTO {table}
        (rank, uid, unique_id, follower_count, beauty_score, age, det_score, final_score)
        VALUES %s""",
        [(i + 1, *r) for i, r in enumerate(rows)])
    conn.commit()


def write_csv(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "unique_id", "follower_count", "beauty_score", "age", "det_score"])
        for i, (uid, handle, foll, beauty, age, det, _score) in enumerate(rows, 1):
            w.writerow([i, handle, foll, round(beauty, 3), round(age, 1), round(det, 3)])


def contact_sheet(rows, path, n, cols, sample):
    import boto3
    from botocore.config import Config
    from PIL import Image
    s3 = boto3.client(
        "s3", endpoint_url="https://" + os.environ.get("IDRIVE_ENDPOINT", "s3.us-east-1.idrivee2.com"),
        aws_access_key_id=os.environ["IDRIVE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["IDRIVE_SECRET_KEY"],
        region_name="us-east-1", config=Config(signature_version="s3v4"))
    uids = [r[0] for r in rows]
    if sample == "spread" and len(uids) > n:        # even sample across the rank
        uids = uids[::len(uids) // n][:n]
    else:                                            # 'top' = highest-ranked n
        uids = uids[:n]
    t = 128
    grid_rows = (len(uids) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * t, grid_rows * t), (20, 20, 20))
    ok = 0
    for i, uid in enumerate(uids):
        try:
            b = s3.get_object(Bucket=BUCKET, Key=f"avatars/{uid}.jpg")["Body"].read()
            sheet.paste(Image.open(io.BytesIO(b)).convert("RGB").resize((t, t)),
                        ((i % cols) * t, (i // cols) * t))
            ok += 1
        except Exception:
            pass
    sheet.save(path, quality=88)
    print(f"[export] contact sheet -> {path} ({ok}/{len(uids)} faces, sample={sample})",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-det", type=float, default=0.78)
    ap.add_argument("--min-area", type=float, default=0.06)
    ap.add_argument("--age-min", type=int, default=18)
    ap.add_argument("--age-max", type=int, default=40)
    ap.add_argument("--beauty-floor", type=float, default=3.0)
    ap.add_argument("--max-followers", type=int, default=0, help="0=no cap; drop mega-celebs above this")
    ap.add_argument("--min-followers", type=int, default=0)
    ap.add_argument("--rank", choices=("followers", "blend"), default="followers")
    ap.add_argument("--blend-w", type=float, default=0.25, help="beauty weight if --rank blend")
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--out", default="candidates.csv")
    ap.add_argument("--table", default="vision_candidates")
    ap.add_argument("--no-csv", action="store_true")
    ap.add_argument("--no-table", action="store_true")
    ap.add_argument("--contact-sheet", default=None)
    ap.add_argument("--sheet-n", type=int, default=120)
    ap.add_argument("--sheet-cols", type=int, default=12)
    ap.add_argument("--sheet-sample", choices=("top", "spread"), default="spread")
    args = ap.parse_args()

    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    sql, params = build_query(args)
    cur.execute(sql, params)
    rows = cur.fetchall()   # (uid, unique_id, follower_count, beauty, age, det, final_score)
    print(f"[export] {len(rows):,} candidates "
          f"(gate det>={args.min_det} area>={args.min_area} age {args.age_min}-{args.age_max} "
          f"beauty>={args.beauty_floor}, rank={args.rank})", file=sys.stderr)
    if not rows:
        return

    if not args.no_csv:
        write_csv(args.out, rows)
        print(f"[export] CSV -> {args.out}", file=sys.stderr)
    if not args.no_table:
        write_table(cur, conn, args.table, rows)
        print(f"[export] table -> {args.table} ({len(rows):,} rows)", file=sys.stderr)
    if args.contact_sheet:
        contact_sheet(rows, args.contact_sheet, args.sheet_n, args.sheet_cols, args.sheet_sample)


if __name__ == "__main__":
    main()
