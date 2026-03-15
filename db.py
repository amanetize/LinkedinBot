"""
db.py — MongoDB Atlas helper

Collections:
  li_session       : LinkedIn cookies (shared across all Playwright jobs)
  pending_targets  : scraper_job → bot.py handoff (Approve/Skip queue)
  pending_posts    : bot.py → poster_job.py handoff (confirmed comments)
  commented_posts  : deduplication — URLs already commented on
  warm_leads       : weekly review list
  daily_count      : 10 comments/day limit
  activity_logs    : full audit trail

Schema principles:
  - No redundant fields; every field is read by at least one consumer.
  - pending_targets and pending_posts are ephemeral; documents are deleted on read.
  - activity_logs are append-only and never deleted automatically.
"""

from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, date, timezone, timedelta
import os, certifi
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.environ["MONGO_URI"]
DB_NAME   = "linkedin_bot"

_client = None

def get_db():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    return _client[DB_NAME]

def _now():
    return datetime.now(timezone.utc)


# ── LinkedIn session cookies ──────────────────────────────────────────────────

def save_cookies(cookies: list):
    db = get_db()
    db.li_session.replace_one(
        {"_id": "linkedin_cookies"},
        {"_id": "linkedin_cookies", "cookies": cookies, "saved_at": _now()},
        upsert=True,
    )
    print(f"[db] Saved {len(cookies)} LinkedIn cookies.")

def get_cookies() -> list:
    db  = get_db()
    doc = db.li_session.find_one({"_id": "linkedin_cookies"})
    if doc:
        print(f"[db] Loaded {len(doc['cookies'])} LinkedIn cookies.")
        return doc["cookies"]
    print("[db] No LinkedIn cookies in MongoDB.")
    return []


# ── Pending targets  (scraper_job → bot.py) ──────────────────────────────────

def save_pending_target(
    target_id: str,
    url: str,
    text: str,
    author_name: str,
    author_title: str,
    reason: str,
    log_id: str = None,
    existing_comments: list = None,
    connection_level: str = "",
    likes_count: int = 0,
    comments_count: int = 0,
    target_index: int = 0,
):
    db = get_db()
    db.pending_targets.replace_one(
        {"target_id": target_id},
        {
            "target_id":         target_id,
            "url":               url,
            "text":              text,
            "author_name":       author_name,
            "author_title":      author_title,
            "reason":            reason,
            "log_id":            log_id,
            "existing_comments": existing_comments or [],
            "connection_level":  connection_level,
            "likes_count":       likes_count,
            "comments_count":    comments_count,
            "target_index":      target_index,
            "created_at":        _now(),
        },
        upsert=True,
    )

def get_pending_target(target_id: str) -> dict:
    """Fetch and delete — document is consumed on read."""
    db  = get_db()
    doc = db.pending_targets.find_one({"target_id": target_id})
    if doc:
        doc.pop("_id", None)
        db.pending_targets.delete_one({"target_id": target_id})
    return doc


# ── Pending posts  (bot.py → poster_job.py) ──────────────────────────────────

def save_pending_post(
    post_id: str,
    url: str,
    comment: str,
    author_name: str,
    author_title: str = "",
    log_id: str = None,
    message_id: int = None,
    connection_level: str = "",
    likes_count: int = 0,
    comments_count: int = 0,
    text: str = "",
    target_index: str = "",
):
    db = get_db()
    db.pending_posts.replace_one(
        {"post_id": post_id},
        {
            "post_id":          post_id,
            "url":              url,
            "comment":          comment,
            "author_name":      author_name,
            "author_title":     author_title,
            "log_id":           log_id,
            "message_id":       message_id,
            "connection_level": connection_level,
            "likes_count":      likes_count,
            "comments_count":   comments_count,
            "text":             text,
            "target_index":     target_index,
            "created_at":       _now(),
        },
        upsert=True,
    )

def get_pending_post(post_id: str) -> dict:
    """Fetch and delete — document is consumed on read."""
    db  = get_db()
    doc = db.pending_posts.find_one({"post_id": post_id})
    if doc:
        doc.pop("_id", None)
        db.pending_posts.delete_one({"post_id": post_id})
    return doc


# ── Activity logs  (append-only audit trail) ─────────────────────────────────

def log_target_created(
    raw_text: str,
    url: str,
    author_name: str,
    author_title: str,
    reason: str,
    target_id: str,
) -> str:
    """Create a new audit log entry for a scraped target. Returns the log _id string."""
    db = get_db()
    doc = {
        "type":       "target",
        "target_id":  target_id,
        "created_at": _now(),
        "post": {
            "url":          url,
            "raw_text":     raw_text[:10_000],
            "author_name":  author_name,
            "author_title": author_title,
            "reason":       reason,
        },
        "comment_drafts": [],   # list of {at, draft}
        "actions":        [],   # list of {at, action, **extra}
        "final_action":   None, # "posted" | "skipped" | "dropped" | "queued"
        "final_comment":  None,
    }
    r = db.activity_logs.insert_one(doc)
    return str(r.inserted_id)

def log_target_action(log_id: str, action: str, **kwargs):
    db = get_db()
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {"actions": {"at": _now(), "action": action, **kwargs}}},
    )

def log_target_comment_version(log_id: str, draft: str):
    db = get_db()
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {"comment_drafts": {"at": _now(), "draft": draft}}},
    )

def log_target_final(log_id: str, final_action: str, final_comment: str = None):
    db = get_db()
    update = {"final_action": final_action}
    if final_comment is not None:
        update["final_comment"] = final_comment
    db.activity_logs.update_one({"_id": ObjectId(log_id)}, {"$set": update})


def log_news_created(
    search_raw: str,
    draft_content: str,
    source: str,
    news_id: str,
) -> str:
    db = get_db()
    doc = {
        "type":          "news",
        "news_id":       news_id,
        "created_at":    _now(),
        "search_raw":    search_raw[:15_000],
        "drafts":        [{"at": _now(), "source": source, "content": draft_content}],
        "actions":       [{"at": _now(), "action": "fetch"}],
        "final_action":  None,
        "final_content": None,
    }
    r = db.activity_logs.insert_one(doc)
    return str(r.inserted_id)

def log_news_draft_added(log_id: str, draft_content: str, source: str):
    db = get_db()
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {
            "drafts":  {"at": _now(), "source": source, "content": draft_content},
            "actions": {"at": _now(), "action": source},
        }},
    )

def log_news_action(log_id: str, action: str, content_posted: str = None):
    db  = get_db()
    upd = {"$push": {"actions": {"at": _now(), "action": action}}}
    db.activity_logs.update_one({"_id": ObjectId(log_id)}, upd)
    set_fields = {"final_action": action}
    if content_posted is not None:
        set_fields["final_content"] = content_posted
    db.activity_logs.update_one({"_id": ObjectId(log_id)}, {"$set": set_fields})


# ── Deduplication ─────────────────────────────────────────────────────────────

def already_commented(post_url: str) -> bool:
    return get_db().commented_posts.find_one({"post_url": post_url}) is not None

def mark_commented(post_url: str):
    get_db().commented_posts.insert_one({"post_url": post_url, "commented_at": _now()})


# ── Daily limit ───────────────────────────────────────────────────────────────

def get_today_count() -> int:
    doc = get_db().daily_count.find_one({"date": str(date.today())})
    return doc["count"] if doc else 0

def increment_today_count():
    get_db().daily_count.update_one(
        {"date": str(date.today())}, {"$inc": {"count": 1}}, upsert=True
    )

def daily_limit_reached(limit: int = 10) -> bool:
    return get_today_count() >= limit


# ── Warm leads ────────────────────────────────────────────────────────────────

def save_warm_lead(author_name: str, author_title: str, post_snippet: str, comment: str):
    get_db().warm_leads.insert_one({
        "author_name":    author_name,
        "author_title":   author_title,
        "post_snippet":   post_snippet[:300],
        "comment_posted": comment,
        "date":           str(date.today()),
    })

def get_warm_leads(days: int = 7) -> list:
    cutoff = str(date.today() - timedelta(days=days))
    return list(get_db().warm_leads.find({"date": {"$gte": cutoff}}, {"_id": 0}))
