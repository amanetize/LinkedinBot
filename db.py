"""
db.py — MongoDB Atlas helper
Collections:
  - commented_posts : deduplication
  - warm_leads      : your weekly review list
  - daily_count     : enforces 10 comments/day hard limit
  - activity_logs   : in-depth logs for every scraped target and news (raw, tele msg, actions, versions)
"""

from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, date, timezone
import os, certifi
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.environ["MONGO_URI"]          # set in .env
DB_NAME   = "linkedin_bot"

_client = None

def get_db():
    global _client
    if _client is None:
        _client = MongoClient(
            MONGO_URI,
            tlsCAFile=certifi.where(),   # fixes SSL cert error on macOS / Python 3.8
        )
    return _client[DB_NAME]


# ── Activity logs (targets + news) ───────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def log_target_created(raw_text: str, url: str, author_name: str, author_title: str,
                       reason: str, tele_msg: str, target_id: str):
    """Insert a new target log. Returns inserted _id (as string for JSON-serializable use in bot state)."""
    db = get_db()
    doc = {
        "type":         "target",
        "target_id":    target_id,
        "created_at":   _now(),
        "scraped":      {
            "raw_text":    raw_text[:10000],
            "url":         url,
            "author_name": author_name,
            "author_title": author_title,
            "reason":      reason,
        },
        "tele_sent":    tele_msg,
        "actions":      [],
        "comment_versions": [],
        "final_action": None,
        "final_comment": None,
    }
    r = db.activity_logs.insert_one(doc)
    return str(r.inserted_id)


def log_target_action(log_id: str, action: str, **kwargs):
    """Append an action to a target log. action: skip, approve, queued, dropped, posted."""
    db = get_db()
    entry = {"at": _now(), "action": action, **kwargs}
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {"actions": entry}}
    )


def log_target_comment_version(log_id: str, draft: str):
    """Append a comment draft (each regen adds a new version)."""
    db = get_db()
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {"comment_versions": {"at": _now(), "draft": draft}}}
    )


def log_target_final(log_id: str, final_action: str, final_comment: str = None):
    """Set final outcome: skipped, queued, dropped, posted."""
    db = get_db()
    update = {"final_action": final_action}
    if final_comment is not None:
        update["final_comment"] = final_comment
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$set": update}
    )


def log_news_created(search_raw: str, draft_content: str, source: str, tele_msg: str, news_id: str):
    """Insert a new news log (first fetch or first draft). Returns inserted _id."""
    db = get_db()
    doc = {
        "type":        "news",
        "news_id":    news_id,
        "created_at": _now(),
        "search_raw": search_raw[:15000],
        "drafts":     [{"at": _now(), "source": source, "content": draft_content}],
        "tele_sent":  [tele_msg],
        "actions":    [{"at": _now(), "action": "fetch" if source == "fetch" else "draft"}],
        "final_action": None,
        "final_content": None,
    }
    r = db.activity_logs.insert_one(doc)
    return str(r.inserted_id)


def log_news_draft_added(log_id: str, draft_content: str, source: str, tele_msg: str = None):
    """Append a draft (regen or fetch) and optionally the tele message sent."""
    db = get_db()
    upd = {"$push": {"drafts": {"at": _now(), "source": source, "content": draft_content}, "actions": {"at": _now(), "action": source}}}
    if tele_msg is not None:
        upd["$push"]["tele_sent"] = tele_msg
    db.activity_logs.update_one({"_id": ObjectId(log_id)}, upd)


def log_news_action(log_id: str, action: str, content_posted: str = None):
    """Record final news action: post or drop; set final_content if posted."""
    db = get_db()
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {"actions": {"at": _now(), "action": action}}}
    )
    set_fields = {"final_action": action}
    if content_posted is not None:
        set_fields["final_content"] = content_posted
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$set": set_fields}
    )


# ── Deduplication ────────────────────────────────────────────

def already_commented(post_url: str) -> bool:
    db = get_db()
    return db.commented_posts.find_one({"post_url": post_url}) is not None


def mark_commented(post_url: str):
    db = get_db()
    db.commented_posts.insert_one({
        "post_url":     post_url,
        "commented_at": datetime.now(timezone.utc)
    })


# ── Daily limit ──────────────────────────────────────────────

def get_today_count() -> int:
    db  = get_db()
    doc = db.daily_count.find_one({"date": str(date.today())})
    return doc["count"] if doc else 0


def increment_today_count():
    db  = get_db()
    key = {"date": str(date.today())}
    db.daily_count.update_one(key, {"$inc": {"count": 1}}, upsert=True)


def daily_limit_reached(limit: int = 10) -> bool:
    return get_today_count() >= limit


# ── Warm leads ───────────────────────────────────────────────

def save_warm_lead(author_name: str, author_title: str,
                   post_snippet: str, comment: str):
    db = get_db()
    db.warm_leads.insert_one({
        "author_name":    author_name,
        "author_title":   author_title,
        "post_snippet":   post_snippet[:300],
        "comment_posted": comment,
        "date":           str(date.today())
    })


def get_warm_leads(days: int = 7):
    """Return leads from the last N days — call this for your weekly review."""
    from datetime import timedelta
    db        = get_db()
    cutoff    = str(date.today() - timedelta(days=days))
    leads     = list(db.warm_leads.find({"date": {"$gte": cutoff}},
                                         {"_id": 0}))
    return leads