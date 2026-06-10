"""Vision pass over creator avatars: gender + age + facial-beauty score.

The name-only `gender_classify.py` is a coarse prefilter; this is the step that
actually reads the face. For every author whose avatar we already pulled into
S3 (`backfill_avatars.py` -> `tt-avatars/avatars/<uid>.jpg`) we run:

  1. InsightFace `buffalo_l` (RetinaFace detect + genderage): per detected
     face we get bbox, det_score, sex (M/F), age, and a 512-d ArcFace
     embedding. We keep the single largest face. Recording `n_faces` and
     `face_area_frac` (largest-face area / image area) lets a later query
     demand "exactly one clear adult female face", which also throws out
     logos, group shots, and minors for free.
  2. A beauty regressor fit on SCUT-FBP5500 (see train_beauty_head.py) applied
     to that ArcFace embedding -> a raw 1..5 beauty score. The embedding is
     already computed in step 1, so this is one cheap matmul. Threshold is set
     later at calibration, not here.

Results upsert into `author_vision` (keyed on uid, resumable). We also store
the embedding (`emb`, 512 float32 LE) for future dedup / "find more like her".

Runs on a rented vast.ai GPU; falls back to CPU automatically (slower but fine
for a `--limit` smoke test). Throughput on a 3090: thousands of faces/sec, so
the whole 586k candidate pool is < 1 hr of GPU, ~cents.

--- vast.ai bring-up runbook ---
  # rent the cheapest CUDA box (RTX 3090-class, < $0.20/hr), then:
  pip install insightface onnxruntime-gpu opencv-python-headless \
              boto3 psycopg2-binary scikit-learn numpy
  # bring the repo (this file + train_beauty_head.py + beauty_head.pkl) over,
  # then fit the beauty head once (needs the SCUT-FBP5500 dataset) OR copy a
  # pre-fit beauty_head.pkl up:
  python train_beauty_head.py --data-dir SCUT-FBP5500_v2   # -> beauty_head.pkl
  # classify (DB is open to 0.0.0.0/0, so write straight to it):
  IDRIVE_ACCESS_KEY=.. IDRIVE_SECRET_KEY=.. \
    python classify_faces.py --shard 0 --of 1
  # smoke test first:
  IDRIVE_ACCESS_KEY=.. IDRIVE_SECRET_KEY=.. \
    python classify_faces.py --limit 200

Env (required): IDRIVE_ACCESS_KEY, IDRIVE_SECRET_KEY
Env (optional): IDRIVE_ENDPOINT, AVATAR_BUCKET (default tt-avatars),
                TIKTOKS_DATABASE_URL
"""

import argparse
import os
import sys
import time

import boto3
import cv2
import numpy as np
import psycopg2
from botocore.config import Config
from psycopg2.extras import execute_values

DSN = os.environ.get(
    "TIKTOKS_DATABASE_URL",
    "postgresql://app1_user:app1dev@150.136.40.239:5432/tiktoks",
)
ENDPOINT = "https://" + os.environ.get("IDRIVE_ENDPOINT", "s3.us-east-1.idrivee2.com")
BUCKET = os.environ.get("AVATAR_BUCKET", "tt-avatars")

MODEL_VERSION = "buffalo_l+scut_ridge_v1"


def s3_client():
    return boto3.client(
        "s3", endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ["IDRIVE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["IDRIVE_SECRET_KEY"],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def load_face_app(det_size: int):
    """buffalo_l on GPU if available, else CPU. ctx_id=0 works for both;
    onnxruntime picks the first available provider."""
    from insightface.app import FaceAnalysis
    providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    app = FaceAnalysis(name="buffalo_l", providers=providers)
    app.prepare(ctx_id=0, det_size=(det_size, det_size))
    active = app.models["detection"].session.get_providers()
    print(f"[classify] insightface ready, providers={active}", file=sys.stderr)
    return app


def load_beauty_head(path: str):
    if not os.path.exists(path):
        print(f"[classify] WARN no beauty head at {path}; beauty_score=NULL "
              f"(gender pass still runs)", file=sys.stderr)
        return None
    import pickle
    with open(path, "rb") as fh:
        head = pickle.load(fh)
    print(f"[classify] loaded beauty head {path}", file=sys.stderr)
    return head


def fetch_worklist(cur, shard, of, limit):
    """Authors with a successfully-downloaded avatar and no vision row yet,
    most-followed first (so early batches are the most useful to calibrate on).
    """
    cur.execute("""
        SELECT a.uid
        FROM authors a
        JOIN author_avatar av ON av.uid = a.uid AND av.status = 'ok'
        WHERE NOT EXISTS (SELECT 1 FROM author_vision v WHERE v.uid = a.uid)
          AND (a.uid %% %s) = %s
        ORDER BY a.follower_count DESC NULLS LAST
        LIMIT %s
    """, (of, shard, limit))
    return [r[0] for r in cur.fetchall()]


def pick_largest_face(faces):
    best, best_area = None, -1.0
    for f in faces:
        x1, y1, x2, y2 = f.bbox
        area = max(0.0, (x2 - x1)) * max(0.0, (y2 - y1))
        if area > best_area:
            best, best_area = f, area
    return best, best_area


def classify_one(img_bytes, app, head):
    """Return a dict of author_vision fields (minus uid) for one image."""
    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return {"status": "error", "n_faces": 0}
    h, w = img.shape[:2]
    faces = app.get(img)
    if not faces:
        return {"status": "noface", "n_faces": 0}

    face, area = pick_largest_face(faces)
    emb = np.asarray(face.normed_embedding, dtype=np.float32)
    sex = getattr(face, "sex", None) or ("M" if int(face.gender) == 1 else "F")
    beauty = None
    if head is not None:
        beauty = float(head.predict(emb.reshape(1, -1))[0])

    return {
        "status": "ok",
        "n_faces": len(faces),
        "vis_is_female": sex == "F",
        "sex": sex,
        "age": float(face.age),
        "det_score": float(face.det_score),
        "face_area_frac": float(area / (w * h)) if w and h else None,
        "beauty_score": beauty,
        "emb": emb.tobytes(),
    }


COLS = ("uid", "source", "status", "n_faces", "vis_is_female", "sex",
        "gender_conf", "age", "det_score", "face_area_frac", "beauty_score",
        "emb", "model_version")


def row_tuple(uid, source, res):
    return (
        uid, source, res["status"], res.get("n_faces"), res.get("vis_is_female"),
        res.get("sex"), None, res.get("age"), res.get("det_score"),
        res.get("face_area_frac"), res.get("beauty_score"),
        psycopg2.Binary(res["emb"]) if res.get("emb") is not None else None,
        MODEL_VERSION,
    )


def flush(cur, conn, rows):
    if not rows:
        return
    execute_values(cur, f"""
        INSERT INTO author_vision ({",".join(COLS)})
        VALUES %s
        ON CONFLICT (uid) DO UPDATE SET
          source=EXCLUDED.source, status=EXCLUDED.status, n_faces=EXCLUDED.n_faces,
          vis_is_female=EXCLUDED.vis_is_female, sex=EXCLUDED.sex,
          gender_conf=EXCLUDED.gender_conf, age=EXCLUDED.age,
          det_score=EXCLUDED.det_score, face_area_frac=EXCLUDED.face_area_frac,
          beauty_score=EXCLUDED.beauty_score, emb=EXCLUDED.emb,
          model_version=EXCLUDED.model_version, scored_at=now()
    """, rows)
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1, help="number of shards")
    ap.add_argument("--source", choices=("avatar", "cover"), default="avatar")
    ap.add_argument("--det-size", type=int, default=640)
    ap.add_argument("--batch", type=int, default=200, help="DB commit batch size")
    ap.add_argument("--beauty-head", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "beauty_head.pkl"))
    args = ap.parse_args()

    prefix = "avatars" if args.source == "avatar" else "covers"

    s3 = s3_client()
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()
    work = fetch_worklist(cur, args.shard, args.of, args.limit)
    print(f"[classify] shard {args.shard}/{args.of}: {len(work)} avatars to score",
          file=sys.stderr)
    if not work:
        return

    app = load_face_app(args.det_size)
    head = load_beauty_head(args.beauty_head)

    rows = []
    n_ok = n_female = n_noface = n_err = 0
    t0 = time.time()
    for idx, uid in enumerate(work, 1):
        try:
            obj = s3.get_object(Bucket=BUCKET, Key=f"{prefix}/{uid}.jpg")
            res = classify_one(obj["Body"].read(), app, head)
        except Exception as e:
            res = {"status": "error", "n_faces": 0}
            print(f"[classify] err uid={uid}: {type(e).__name__}: {str(e)[:80]}",
                  file=sys.stderr)
        if res["status"] == "ok":
            n_ok += 1
            n_female += int(bool(res.get("vis_is_female")))
        elif res["status"] == "noface":
            n_noface += 1
        else:
            n_err += 1
        rows.append(row_tuple(uid, args.source, res))
        if len(rows) >= args.batch:
            flush(cur, conn, rows); rows = []
            rate = idx / (time.time() - t0)
            print(f"[classify] {idx}/{len(work)} ok={n_ok} female={n_female} "
                  f"noface={n_noface} err={n_err} ({rate:.0f}/s)", file=sys.stderr)
    flush(cur, conn, rows)
    dt = time.time() - t0
    print(f"[classify] DONE {len(work)} in {dt:.0f}s | ok={n_ok} female={n_female} "
          f"noface={n_noface} err={n_err}", file=sys.stderr)


if __name__ == "__main__":
    main()
