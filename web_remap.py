"""
Reshape a web-search item (camelCase, www.tiktok.com/api/search/item/full/)
into the mobile-API schema (snake_case, /aweme/v1/search/item/) that
db.save_search expects.

Only the fields db._parse_raw reads are remapped; everything else is dropped.
The web schema has no signature/desc_language/paid_collection_id; those go to
None/False on the mobile side.
"""

from typing import Any


def _to_int(x: Any) -> int | None:
    """statsV2 fields come back as strings ('1234') vs stats fields as ints.
    Prefer statsV2 when available since it's higher-precision (e.g. counts
    above ~1M are bucketed in stats.* but exact in statsV2.*)."""
    if x is None:
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _stat(item: dict, field: str) -> int | None:
    """Read a numeric stat preferring statsV2 (string, exact) over stats
    (int, sometimes rounded). field is the camelCase name e.g. 'diggCount'."""
    v2 = (item.get("statsV2") or {}).get(field)
    if v2 is not None:
        n = _to_int(v2)
        if n is not None:
            return n
    return _to_int((item.get("stats") or {}).get(field))


def web_to_mobile(it: dict) -> dict:
    """Take one element of `item_list` from the web search response and emit
    a dict shaped like an `aweme_list` element from the mobile API."""
    author = it.get("author") or {}
    video = it.get("video") or {}
    music = it.get("music") or {}

    # author.id is the numeric uid (string in JSON); db._parse_raw int()s it
    return {
        "aweme_id": it.get("id"),
        "desc": it.get("desc"),
        "desc_language": it.get("textLanguage"),
        "create_time": it.get("createTime"),
        "is_ads": it.get("isAd"),
        "paid_content_info": {},  # web schema has no equivalent

        "statistics": {
            "digg_count":     _stat(it, "diggCount"),
            "play_count":     _stat(it, "playCount"),
            "comment_count":  _stat(it, "commentCount"),
            "share_count":    _stat(it, "shareCount"),
            "collect_count":  _stat(it, "collectCount"),
            "download_count": None,
            "forward_count":  None,
            "lose_count":     None,
            "lose_comment_count": None,
            "repost_count":   _stat(it, "repostCount"),
        },

        "author": {
            "uid":               author.get("id"),
            "sec_uid":           author.get("secUid", ""),
            "unique_id":         author.get("uniqueId", ""),
            "nickname":          author.get("nickname"),
            "signature":         author.get("signature"),
            "follower_count":    (it.get("authorStats") or {}).get("followerCount"),
            "following_count":   (it.get("authorStats") or {}).get("followingCount"),
            "verification_type": 1 if author.get("verified") else 0,
            "account_region":    None,
        },

        "video": {
            # web schema reports duration in seconds; mobile API used ms,
            # which is what db.py's column name (duration_ms) expects.
            "duration": (video.get("duration") * 1000) if video.get("duration") is not None else None,
            "height":   video.get("height"),
            "width":    video.get("width"),
            "ratio":    video.get("ratio"),
            "play_addr": {"uri": video.get("playAddr")},
        },

        "music": {
            "play_url": {"uri": music.get("playUrl")},
            "is_original_sound": music.get("original"),
        },
    }
