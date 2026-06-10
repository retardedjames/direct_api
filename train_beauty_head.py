"""Fit the facial-beauty regressor used by classify_faces.py.

We don't ship a fragile pretrained beauty CNN. Instead we reuse the ArcFace
embedding that InsightFace `buffalo_l` already produces and fit a tiny head
(Ridge by default) mapping that 512-d embedding -> the SCUT-FBP5500 mean human
beauty rating (1..5). 5500 faces, fits in seconds, fully reproducible, no
extra model dependency at inference time (classify_faces just does one matmul).

SCUT-FBP5500 is a licensed research dataset, downloaded manually. Get
`SCUT-FBP5500_v2` (e.g. the official HuggingFace / GitHub release) and unzip.
Expected layout:
    SCUT-FBP5500_v2/Images/*.jpg
    SCUT-FBP5500_v2/train_test_files/All_labels.txt   ("<imgname> <score>")

Usage:
    python train_beauty_head.py --data-dir SCUT-FBP5500_v2     # -> beauty_head.pkl
    python train_beauty_head.py --data-dir SCUT-FBP5500_v2 --model mlp
"""

import argparse
import os
import pickle
import sys

import cv2
import numpy as np


def load_labels(data_dir: str) -> list[tuple[str, float]]:
    cand = [
        os.path.join(data_dir, "train_test_files", "All_labels.txt"),
        os.path.join(data_dir, "All_labels.txt"),
    ]
    path = next((p for p in cand if os.path.exists(p)), None)
    if path is None:
        sys.exit(f"could not find All_labels.txt under {data_dir} (looked at {cand})")
    out = []
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], float(parts[1])))
    print(f"[train] {len(out)} labelled images from {path}", file=sys.stderr)
    return out


def embed_dataset(data_dir, labels, det_size):
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l",
                       providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(det_size, det_size))

    img_dir = os.path.join(data_dir, "Images")
    X, y, skipped = [], [], 0
    for i, (name, score) in enumerate(labels, 1):
        img = cv2.imread(os.path.join(img_dir, name))
        if img is None:
            skipped += 1
            continue
        faces = app.get(img)
        if not faces:
            skipped += 1
            continue
        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        X.append(np.asarray(face.normed_embedding, dtype=np.float32))
        y.append(score)
        if i % 500 == 0:
            print(f"[train] embedded {i}/{len(labels)} (skipped {skipped})",
                  file=sys.stderr)
    print(f"[train] usable {len(X)} / {len(labels)} (skipped {skipped})",
          file=sys.stderr)
    return np.vstack(X), np.asarray(y, dtype=np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "beauty_head.pkl"))
    ap.add_argument("--model", choices=("ridge", "mlp"), default="ridge")
    ap.add_argument("--det-size", type=int, default=640)
    args = ap.parse_args()

    from sklearn.model_selection import train_test_split
    from sklearn.linear_model import Ridge
    from sklearn.neural_network import MLPRegressor
    from scipy.stats import pearsonr

    labels = load_labels(args.data_dir)
    X, y = embed_dataset(args.data_dir, labels, args.det_size)

    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=42)
    if args.model == "ridge":
        model = Ridge(alpha=1.0)
    else:
        model = MLPRegressor(hidden_layer_sizes=(256,), max_iter=500,
                             early_stopping=True, random_state=42)
    model.fit(Xtr, ytr)

    pred = model.predict(Xte)
    r, _ = pearsonr(pred, yte)
    mae = float(np.mean(np.abs(pred - yte)))
    print(f"[train] held-out Pearson r={r:.3f}  MAE={mae:.3f}  "
          f"(SCUT-FBP5500 SOTA CNNs ~0.90; embedding+Ridge typically ~0.85)",
          file=sys.stderr)

    # refit on all data before saving
    model.fit(X, y)
    with open(args.out, "wb") as fh:
        pickle.dump(model, fh)
    print(f"[train] wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
