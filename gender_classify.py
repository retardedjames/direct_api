"""Populate the author_gender table from author display names (nicknames).

Goal: identify which creators are female. This first pass is conservative —
it only marks people whose first name is *unambiguously male* as is_female=FALSE,
using the gender-guesser dataset (Joerg Michael's ~48k-name, per-country data).
Everything else is left unknown (no row, or untouched) for later passes.

Why conservative: for the downstream goal (finding attractive females), wrongly
marking a real female as "not female" drops her from the pool — the costly error.
So we (a) only accept gender-guesser's strict 'male' bucket (not 'mostly_male'),
and (b) subtract a blocklist of common unisex names (alex, jordan, karol, ...)
that the dataset leans male on but that plenty of women use.

Keyed on the stable numeric `uid` (and we store `sec_uid` too) because TikTok
`unique_id` handles can change. Re-runnable: ON CONFLICT updates in place.

Usage:
  ./venv/bin/python3 gender_classify.py            # classify all, write males
  ./venv/bin/python3 gender_classify.py --dry-run  # report counts, write nothing
"""

import argparse
import os
import sys

import gender_guesser.detector as gd
import psycopg2
from psycopg2.extras import execute_values

DSN = os.environ.get(
    "TIKTOKS_DATABASE_URL",
    "postgresql://app1_user:app1dev@150.136.40.239:5432/tiktoks",
)

# Names gender-guesser returns as 'male' but which have substantial female use.
# Left as unknown rather than risk a false "not female" mark. Lowercase.
UNISEX_BLOCKLIST = {
    "alex", "jordan", "taylor", "jamie", "casey", "riley", "morgan", "jessie",
    "jesse", "sam", "charlie", "frankie", "ryan", "karol", "dana", "robin",
    "kim", "kai", "sage", "drew", "ali", "andrea", "nikita", "eden", "sasha",
    "blair", "rory", "shay", "devon", "devin", "dakota", "skyler", "skylar",
    "harley", "phoenix", "reese", "remy", "emery", "lennon", "marley", "shawn",
    "ariel", "angel", "lee", "noor", "yuki", "ren", "jean", "luca", "andy",
    "nico", "cameron", "kennedy", "quinn", "rowan", "sidney", "sydney", "toni",
}

# Latin letters only (Basic Latin + Latin-1 + Latin Extended-A/B), so we don't
# feed Japanese/Arabic/Korean/Cyrillic display names to a Latin-name classifier.
def first_name_token(nickname: str) -> str | None:
    if not nickname:
        return None
    for raw in nickname.strip().split():
        cleaned = "".join(ch for ch in raw if ch.isalpha())
        if len(cleaned) < 2:
            continue
        if not all(ord(ch) < 0x250 for ch in cleaned):
            continue  # not Latin script
        return cleaned.lower()
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and report counts but write nothing")
    args = ap.parse_args()

    detector = gd.Detector(case_sensitive=False)
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()

    cur.execute(
        "SELECT uid, sec_uid, unique_id, nickname FROM authors "
        "WHERE nickname IS NOT NULL AND nickname <> ''"
    )
    rows = cur.fetchall()
    print(f"[gender] scanning {len(rows):,} authors with a nickname", file=sys.stderr)

    males = []          # rows to write as is_female=FALSE
    n_unknown = n_female = n_mostly = n_blocked = n_nolatin = 0
    for uid, sec_uid, unique_id, nickname in rows:
        name = first_name_token(nickname)
        if name is None:
            n_nolatin += 1
            continue
        g = detector.get_gender(name)
        if g == "male":
            if name in UNISEX_BLOCKLIST:
                n_blocked += 1
                continue
            males.append((uid, sec_uid, unique_id, False, "gg_male_firstname",
                          name, "high"))
        elif g in ("female", "mostly_female"):
            n_female += 1
        elif g in ("mostly_male", "andy"):
            n_mostly += 1
        else:
            n_unknown += 1

    print(f"[gender] unambiguous male -> NOT female : {len(males):,}", file=sys.stderr)
    print(f"[gender] (skipped) female-leaning        : {n_female:,}", file=sys.stderr)
    print(f"[gender] (skipped) mostly_male/androgynous: {n_mostly:,}", file=sys.stderr)
    print(f"[gender] (skipped) unisex blocklist       : {n_blocked:,}", file=sys.stderr)
    print(f"[gender] (skipped) unknown name           : {n_unknown:,}", file=sys.stderr)
    print(f"[gender] (skipped) no Latin first name     : {n_nolatin:,}", file=sys.stderr)

    if args.dry_run:
        print("[gender] --dry-run: nothing written", file=sys.stderr)
        return

    execute_values(
        cur,
        """
        INSERT INTO author_gender
          (uid, sec_uid, unique_id, is_female, method, evidence, confidence)
        VALUES %s
        ON CONFLICT (uid) DO UPDATE SET
          is_female  = EXCLUDED.is_female,
          method     = EXCLUDED.method,
          evidence   = EXCLUDED.evidence,
          confidence = EXCLUDED.confidence,
          unique_id  = EXCLUDED.unique_id,
          updated_at = now()
        """,
        males,
        page_size=5000,
    )
    conn.commit()
    print(f"[gender] wrote {len(males):,} rows to author_gender", file=sys.stderr)


if __name__ == "__main__":
    main()
