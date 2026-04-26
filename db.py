"""
SQLAlchemy models and DB utilities for TikTok scraper.
"""
import os
from datetime import datetime, timezone
from sqlalchemy import (
    create_engine, Column, BigInteger, Integer, Text, Boolean,
    DateTime, ForeignKey, UniqueConstraint, text
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import DeclarativeBase, relationship, Session

VPS_HOST = "150.136.40.239"
DB_USER  = "app1_user"
DB_PASS  = "app1dev"
DB_NAME  = "tiktoks"


def _get_engine():
    url = os.environ.get(
        "TIKTOKS_DATABASE_URL",
        f"postgresql://{DB_USER}:{DB_PASS}@{VPS_HOST}:5432/{DB_NAME}"
    )
    return create_engine(url, pool_pre_ping=True)


engine = _get_engine()


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "authors"

    uid             = Column(BigInteger, primary_key=True)
    sec_uid         = Column(Text, unique=True, nullable=False)
    unique_id       = Column(Text, nullable=False)
    nickname        = Column(Text)
    signature       = Column(Text)
    follower_count  = Column(Integer)
    following_count = Column(Integer)
    verification_type = Column(Integer)
    account_region  = Column(Text)
    updated_at      = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                             onupdate=lambda: datetime.now(timezone.utc))

    videos = relationship("Video", back_populates="author")


class Video(Base):
    __tablename__ = "videos"

    aweme_id        = Column(BigInteger, primary_key=True)
    author_uid      = Column(BigInteger, ForeignKey("authors.uid"))
    description     = Column(Text)
    desc_language   = Column(Text)
    create_time     = Column(DateTime(timezone=True))
    is_ads          = Column(Boolean)
    is_paid_content = Column(Boolean)

    # statistics
    likes               = Column(Integer)
    views               = Column(Integer)
    comments            = Column(Integer)
    shares              = Column(Integer)
    saves               = Column(Integer)
    downloads           = Column(Integer)
    forward_count       = Column(Integer)
    lose_count          = Column(Integer)
    lose_comment_count  = Column(Integer)
    repost_count        = Column(Integer)

    # video
    duration_ms         = Column(Integer)
    height              = Column(Integer)
    width               = Column(Integer)
    ratio               = Column(Text)
    video_uri           = Column(Text)

    # music
    music_play_url      = Column(Text)
    music_is_original   = Column(Boolean)

    scraped_at          = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    stats_updated_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                                 onupdate=lambda: datetime.now(timezone.utc))

    author = relationship("Author", back_populates="videos")
    search_results = relationship("SearchResult", back_populates="video")


class Search(Base):
    __tablename__ = "searches"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    keyword     = Column(Text, nullable=False)
    sort_type   = Column(Text, nullable=False, default="1")
    searched_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    results = relationship("SearchResult", back_populates="search")


class SearchResult(Base):
    __tablename__ = "search_results"
    __table_args__ = (UniqueConstraint("search_id", "video_id"),)

    id        = Column(Integer, primary_key=True, autoincrement=True)
    search_id = Column(Integer, ForeignKey("searches.id", ondelete="CASCADE"), nullable=False)
    video_id  = Column(BigInteger, ForeignKey("videos.aweme_id"), nullable=False)
    position  = Column(Integer)

    search = relationship("Search", back_populates="results")
    video  = relationship("Video", back_populates="search_results")


def create_tables():
    Base.metadata.create_all(engine)


def _parse_raw(raw: dict, now: datetime) -> tuple[dict | None, dict]:
    """Extract (author_vals, video_vals) from a raw aweme_info dict."""
    stats  = raw.get("statistics") or {}
    author = raw.get("author") or {}
    vid    = raw.get("video") or {}
    music  = raw.get("music") or {}
    paid   = (raw.get("paid_content_info") or {}).get("paid_collection_id", 0)

    uid = author.get("uid") or author.get("id")
    uid = int(uid) if uid else None

    author_vals = None
    if uid:
        author_vals = dict(
            uid               = uid,
            sec_uid           = author.get("sec_uid", ""),
            unique_id         = author.get("unique_id", ""),
            nickname          = author.get("nickname"),
            signature         = author.get("signature"),
            follower_count    = author.get("follower_count"),
            following_count   = author.get("following_count"),
            verification_type = author.get("verification_type"),
            account_region    = author.get("account_region"),
            updated_at        = now,
        )

    ct = raw.get("create_time")
    video_vals = dict(
        aweme_id           = int(raw.get("aweme_id", 0)),
        author_uid         = uid,
        description        = raw.get("desc"),
        desc_language      = raw.get("desc_language"),
        create_time        = datetime.fromtimestamp(ct, tz=timezone.utc) if ct else None,
        is_ads             = raw.get("is_ads"),
        is_paid_content    = bool(paid),
        likes              = stats.get("digg_count"),
        views              = stats.get("play_count"),
        comments           = stats.get("comment_count"),
        shares             = stats.get("share_count"),
        saves              = stats.get("collect_count"),
        downloads          = stats.get("download_count"),
        forward_count      = stats.get("forward_count"),
        lose_count         = stats.get("lose_count"),
        lose_comment_count = stats.get("lose_comment_count"),
        repost_count       = stats.get("repost_count"),
        duration_ms        = vid.get("duration"),
        height             = vid.get("height"),
        width              = vid.get("width"),
        ratio              = vid.get("ratio"),
        video_uri          = (vid.get("play_addr") or {}).get("uri"),
        music_play_url     = (music.get("play_url") or {}).get("uri"),
        music_is_original  = music.get("is_original_sound"),
        stats_updated_at   = now,
        scraped_at         = now,
    )
    return author_vals, video_vals


def save_search(keyword: str, sort_type: str, aweme_infos: list[dict]) -> int:
    """
    Record a scrape run: always creates a new Search row (history is preserved),
    upserts all video/author data (never deleted), and links video IDs to this run.
    Returns count of unique videos saved.
    """
    now = datetime.now(timezone.utc)

    seen_ids: set[int] = set()
    author_batch: list[dict] = []
    video_batch: list[dict] = []
    seen_authors: set[int] = set()

    for raw in aweme_infos:
        aweme_id = int(raw.get("aweme_id", 0))
        if not aweme_id or aweme_id in seen_ids:
            continue
        seen_ids.add(aweme_id)
        av, vv = _parse_raw(raw, now)
        if av and av["uid"] not in seen_authors:
            seen_authors.add(av["uid"])
            author_batch.append(av)
        video_batch.append(vv)

    if not seen_ids:
        return 0

    with Session(engine) as session:
        if author_batch:
            stmt = pg_insert(Author).values(author_batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["uid"],
                set_={c: stmt.excluded[c] for c in author_batch[0] if c != "uid"},
            )
            session.execute(stmt)

        stmt = pg_insert(Video).values(video_batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["aweme_id"],
            set_={c: stmt.excluded[c] for c in video_batch[0]
                  if c not in ("aweme_id", "scraped_at")},
        )
        session.execute(stmt)

        search = Search(keyword=keyword, sort_type=sort_type, searched_at=now)
        session.add(search)
        session.flush()

        ordered_ids = [int(r.get("aweme_id", 0)) for r in aweme_infos]
        seen_pos: set[int] = set()
        sr_batch = []
        pos = 0
        for aweme_id in ordered_ids:
            if not aweme_id or aweme_id in seen_pos:
                continue
            seen_pos.add(aweme_id)
            sr_batch.append({"search_id": search.id, "video_id": aweme_id, "position": pos})
            pos += 1

        if sr_batch:
            session.execute(
                pg_insert(SearchResult).values(sr_batch).on_conflict_do_nothing()
            )

        session.commit()
        return len(seen_ids)


# ---------------------------------------------------------------------------
# Queue helpers for the `terms` table (owned/populated externally; schema:
# id, term, type, status, added_at, started_at, completed_at, videos_saved,
# done_old_way). Used by continual_scraper.py.
# ---------------------------------------------------------------------------


def reclaim_stale_terms(stale_after_minutes: int = 30) -> int:
    """Reset `in_progress` rows whose started_at is older than the cutoff
    back to `pending`. Handles crashed/killed scraper runs."""
    sql = text("""
        UPDATE terms
           SET status = 'pending',
               started_at = NULL
         WHERE status = 'in_progress'
           AND type = 'search'
           AND started_at < now() - (:mins || ' minutes')::interval
        RETURNING id
    """)
    with engine.begin() as conn:
        rows = conn.execute(sql, {"mins": str(stale_after_minutes)}).fetchall()
    return len(rows)


def claim_next_term() -> dict | None:
    """Atomically claim one pending search term: flip status to in_progress
    and return its row. Uses FOR UPDATE SKIP LOCKED so parallel workers
    never collide. Returns None if nothing to claim."""
    sql = text("""
        UPDATE terms
           SET status = 'in_progress',
               started_at = now()
         WHERE id = (
             SELECT id FROM terms
              WHERE status = 'pending' AND type = 'search'
              ORDER BY id
              FOR UPDATE SKIP LOCKED
              LIMIT 1
         )
        RETURNING id, term, type, status, added_at, started_at
    """)
    with engine.begin() as conn:
        row = conn.execute(sql).mappings().fetchone()
    return dict(row) if row else None


def mark_term_done(term_id: int, videos_saved: int) -> None:
    sql = text("""
        UPDATE terms
           SET status = 'done',
               completed_at = now(),
               videos_saved = :vs
         WHERE id = :id
    """)
    with engine.begin() as conn:
        conn.execute(sql, {"id": term_id, "vs": videos_saved})


def mark_term_failed(term_id: int) -> None:
    sql = text("""
        UPDATE terms
           SET status = 'failed',
               completed_at = now()
         WHERE id = :id
    """)
    with engine.begin() as conn:
        conn.execute(sql, {"id": term_id})


def release_term(term_id: int) -> None:
    """Return an in_progress row to the pending pool (e.g. on Ctrl-C before
    the scrape finishes). Does not touch rows in other states."""
    sql = text("""
        UPDATE terms
           SET status = 'pending',
               started_at = NULL
         WHERE id = :id AND status = 'in_progress'
    """)
    with engine.begin() as conn:
        conn.execute(sql, {"id": term_id})


if __name__ == "__main__":
    print("Creating tables...")
    create_tables()
    print("Done.")
