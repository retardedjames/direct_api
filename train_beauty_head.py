"""Fit the facial-beauty regressor used by classify_faces.py.

We don't ship a fragile pretrained beauty CNN. Instead we reuse the ArcFace
embedding that InsightFace `buffalo_l` already produces and fit a tiny head
(Ridge by default) mapping that 512-d embedding -> the SCUT-FBP5500 mean human
beauty rating (1..5). 5500 faces, fits in seconds, fully reproducible, no
extra model dependency at inference time (classify_faces just does one matmul).

Default data source is the HuggingFace mirror `MnLgt/scut-fbp5500`
(columns: image, beauty_score, ...) so it downloads headlessly with no manual
Google-Drive step. `--data-dir` still supports the original on-disk layout
(Images/*.jpg + train_test_files/All_labels.txt).

Usage:
    python train_beauty_head.py                       # HF mirror -> beauty_head.pkl
    python train_beauty_head.py --model mlp
    python train_beauty_head.py --data-dir SCUT-FBP5500_v2   # legacy on-disk
"""

import argparse
import os
import pickle
import sys

import cv2
import numpy as np


def _pil_to_bgr(pil):
    return np.array(pil.convert("RGB"))[:, :, ::-1].copy()


def iter_hf(hf_name):
    """Yield (bgr_image, score) from the HuggingFace parquet mirror."""
    from datasets import load_dataset, concatenate_datasets
    ds = load_dataset(hf_name)
    parts = [ds[s] for s in ds.keys()]
    full = concatenate_datasets(parts) if len(parts) > 1 else parts[0]
    print(f"[train] HF {hf_name}: {len(full)} rows, cols={full.column_names}",
          file=sys.stderr)
    for row in full:
        yield _pil_to_bgr(row["image"]), float(row["beauty_score"])


def iter_dir(data_dir):
    """Yield (bgr_image, score) from the original on-disk SCUT layout."""
    cand = [os.path.join(data_dir, "train_test_files", "All_labels.txt"),
            os.path.join(data_dir, "All_labels.txt")]
    path = next((p for p in cand if os.path.exists(p)), None)
    if path is None:
        sys.exit(f"no All_labels.txt under {data_dir} (looked at {cand})")
    img_dir = os.path.join(data_dir, "Images")
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 2:
                continue
            img = cv2.imread(os.path.join(img_dir, parts[0]))
            if img is not None:
                yield img, float(parts[1])


def embed(source, det_size):
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l",
                       providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(det_size, det_size))
    print(f"[train] providers={app.models['detection'].session.get_providers()}",
          file=sys.stderr)

    X, y, n, skipped = [], [], 0, 0
    for img, score in source:
        n += 1
        faces = app.get(img)
        if not faces:
            skipped += 1
            continue
        f = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        X.append(np.asarray(f.normed_embedding, dtype=np.float32))
        y.append(score)
        if n % 500 == 0:
            print(f"[train] embedded {n} (skipped {skipped})", file=sys.stderr)
    print(f"[train] usable {len(X)}/{n} (skipped {skipped} no-face)", file=sys.stderr)
    return np.vstack(X), np.asarray(y, dtype=np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hf", default="MnLgt/scut-fbp5500", help="HF dataset id")
    ap.add_argument("--data-dir", default=None, help="legacy on-disk SCUT dir (overrides --hf)")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "beauty_head.pkl"))
    ap.add_argument("--model", choices=("ridge", "mlp"), default="ridge")
    ap.add_argument("--det-size", type=int, default=640)
    args = ap.parse_args()

    from sklearn.model_selection import train_test_split
    from sklearn.linear_model import Ridge
    from sklearn.neural_network import MLPRegressor
    from scipy.stats import pearsonr

    source = iter_dir(args.data_dir) if args.data_dir else iter_hf(args.hf)
    X, y = embed(source, args.det_size)

    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=42)
    model = (Ridge(alpha=1.0) if args.model == "ridge"
             else MLPRegressor(hidden_layer_sizes=(256,), max_iter=500,
                               early_stopping=True, random_state=42))
    model.fit(Xtr, ytr)
    pred = model.predict(Xte)
    r, _ = pearsonr(pred, yte)
    print(f"[train] held-out Pearson r={r:.3f}  MAE={np.mean(np.abs(pred-yte)):.3f}  "
          f"(SCUT SOTA CNN ~0.90; embedding+Ridge typically ~0.85)", file=sys.stderr)

    model.fit(X, y)  # refit on all before saving
    with open(args.out, "wb") as fh:
        pickle.dump(model, fh)
    print(f"[train] wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
