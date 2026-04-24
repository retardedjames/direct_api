"""
Local MSSDK signer using the open-source SignerPy package.

Drop-in replacement for the RapidAPI-based call_signer() in replay_search.py
and scrape_keyword.py. Returns the same 4-key dict the old signer did:

    {"X-Argus": ..., "X-Ladon": ..., "X-Gorgon": ..., "X-Khronos": ...}

Usage:
    from local_signer import sign_query
    sig = sign_query(query, mssdk=MSSDK)
"""

import time
from typing import Optional

from SignerPy import sign as _signerpy_sign


def sign_query(query: str, mssdk: dict, unix: Optional[int] = None) -> dict:
    """Produce X-Argus / X-Ladon / X-Gorgon / X-Khronos locally.

    query: the fully-built query string (no leading '?'). Signature is
      computed over this exact string, so parameter order matters.
    mssdk: dict with mssdk_app_id, mssdk_license_id (str or int),
      mssdk_version, mssdk_version_int.
    unix: override the timestamp (for testing). Default is time.time().
    """
    if unix is None:
        unix = int(time.time())

    s = _signerpy_sign(
        params=query,
        aid=int(mssdk["mssdk_app_id"]),
        license_id=int(mssdk["mssdk_license_id"]),
        sdk_version_str=mssdk["mssdk_version"],
        sdk_version=mssdk["mssdk_version_int"],
        unix=unix,
        version=8404,  # matches phone capture's gorgon prefix
    )
    # SignerPy returns lowercase keys. The existing callers expect capitalised
    # X- keys (matching the RapidAPI response shape).
    return {
        "X-Argus": s["x-argus"],
        "X-Ladon": s["x-ladon"],
        "X-Gorgon": s["x-gorgon"],
        "X-Khronos": s["x-khronos"],
    }


if __name__ == "__main__":
    from replay_search import DEVICE, MSSDK, build_query
    q = build_query("mario", 0)
    sig = sign_query(q, MSSDK)
    for k, v in sig.items():
        print(f"{k} = {v}")
