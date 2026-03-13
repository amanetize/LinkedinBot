"""
db.py — MongoDB Atlas helper
Collections:
  - commented_posts  : deduplication
  - warm_leads       : weekly review list
  - daily_count      : 10 comments/day limit
  - activity_logs    : full audit trail
  - pending_targets  : cross-process handoff (GitHub Actions → Koyeb bot)
  - li_session       : LinkedIn cookies (shared across all Playwright jobs)
"""

from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime, date, timezone
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
    """Save LinkedIn cookies to MongoDB. Called after every successful session."""
    db = get_db()
    db.li_session.replace_one(
        {"_id": "linkedin_cookies"},
        {"_id": "linkedin_cookies", "cookies": cookies, "saved_at": _now()},
        upsert=True,
    )
    print(f"[db] Saved {len(cookies)} LinkedIn cookies to MongoDB.")

def get_cookies() -> list:
    """Retrieve LinkedIn cookies from MongoDB. Returns [] if none saved."""
    db  = get_db()
    doc = db.li_session.find_one({"_id": "linkedin_cookies"})
    if doc:
        print(f"[db] Loaded {len(doc['cookies'])} LinkedIn cookies from MongoDB.")
        return doc["cookies"]
    print("[db] No LinkedIn cookies in MongoDB.")
    return []


# ── Pending targets (GitHub Actions → bot.py handoff) ────────────────────────

def save_pending_target(target_id: str, url: str, text: str,
                        author_name: str, author_title: str,
                        reason: str, log_id: str = None,
                        existing_comments: list = None):
    """scraper_job.py saves targets here. bot.py reads them on Approve."""
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
            "created_at":        _now(),
        },
        upsert=True,
    )

def get_pending_target(target_id: str) -> dict:
    """bot.py calls this on Approve. Returns target data and deletes from DB."""
    db  = get_db()
    doc = db.pending_targets.find_one({"target_id": target_id})
    if doc:
        doc.pop("_id", None)
        db.pending_targets.delete_one({"target_id": target_id})
    return doc


# ── Pending posts (bot.py → poster_job.py handoff) ───────────────────────────

def save_pending_post(post_id: str, url: str, comment: str,
                      author_name: str, log_id: str = None):
    """bot.py saves confirmed comments here. poster_job.py reads on run."""
    db = get_db()
    db.pending_posts.replace_one(
        {"post_id": post_id},
        {
            "post_id":     post_id,
            "url":         url,
            "comment":     comment,
            "author_name": author_name,
            "log_id":      log_id,
            "created_at":  _now(),
        },
        upsert=True,
    )

def get_pending_post(post_id: str) -> dict:
    """poster_job.py calls this. Returns post data and deletes from DB."""
    db  = get_db()
    doc = db.pending_posts.find_one({"post_id": post_id})
    if doc:
        doc.pop("_id", None)
        db.pending_posts.delete_one({"post_id": post_id})
    return doc


# ── Activity logs ─────────────────────────────────────────────────────────────

def log_target_created(raw_text: str, url: str, author_name: str, author_title: str,
                       reason: str, tele_msg: str, target_id: str):
    db = get_db()
    doc = {
        "type":         "target",
        "target_id":    target_id,
        "created_at":   _now(),
        "scraped": {
            "raw_text":     raw_text[:10000],
            "url":          url,
            "author_name":  author_name,
            "author_title": author_title,
            "reason":       reason,
        },
        "tele_sent":        tele_msg,
        "actions":          [],
        "comment_versions": [],
        "final_action":     None,
        "final_comment":    None,
    }
    r = db.activity_logs.insert_one(doc)
    return str(r.inserted_id)

def log_target_action(log_id: str, action: str, **kwargs):
    db = get_db()
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {"actions": {"at": _now(), "action": action, **kwargs}}}
    )

def log_target_comment_version(log_id: str, draft: str):
    db = get_db()
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {"comment_versions": {"at": _now(), "draft": draft}}}
    )

def log_target_final(log_id: str, final_action: str, final_comment: str = None):
    db = get_db()
    update = {"final_action": final_action}
    if final_comment is not None:
        update["final_comment"] = final_comment
    db.activity_logs.update_one({"_id": ObjectId(log_id)}, {"$set": update})

def log_news_created(search_raw: str, draft_content: str, source: str, tele_msg: str, news_id: str):
    db = get_db()
    doc = {
        "type":         "news",
        "news_id":      news_id,
        "created_at":   _now(),
        "search_raw":   search_raw[:15000],
        "drafts":       [{"at": _now(), "source": source, "content": draft_content}],
        "tele_sent":    [tele_msg],
        "actions":      [{"at": _now(), "action": "fetch" if source == "fetch" else "draft"}],
        "final_action": None,
        "final_content": None,
    }
    r = db.activity_logs.insert_one(doc)
    return str(r.inserted_id)

def log_news_draft_added(log_id: str, draft_content: str, source: str, tele_msg: str = None):
    db  = get_db()
    upd = {"$push": {
        "drafts":  {"at": _now(), "source": source, "content": draft_content},
        "actions": {"at": _now(), "action": source},
    }}
    if tele_msg is not None:
        upd["$push"]["tele_sent"] = tele_msg
    db.activity_logs.update_one({"_id": ObjectId(log_id)}, upd)

def log_news_action(log_id: str, action: str, content_posted: str = None):
    db = get_db()
    db.activity_logs.update_one(
        {"_id": ObjectId(log_id)},
        {"$push": {"actions": {"at": _now(), "action": action}}}
    )
    set_fields = {"final_action": action}
    if content_posted is not None:
        set_fields["final_content"] = content_posted
    db.activity_logs.update_one({"_id": ObjectId(log_id)}, {"$set": set_fields})


# ── Deduplication ─────────────────────────────────────────────────────────────

def already_commented(post_url: str) -> bool:
    db = get_db()
    return db.commented_posts.find_one({"post_url": post_url}) is not None

def mark_commented(post_url: str):
    db = get_db()
    db.commented_posts.insert_one({"post_url": post_url, "commented_at": _now()})


# ── Daily limit ───────────────────────────────────────────────────────────────

def get_today_count() -> int:
    db  = get_db()
    doc = db.daily_count.find_one({"date": str(date.today())})
    return doc["count"] if doc else 0

def increment_today_count():
    db = get_db()
    db.daily_count.update_one({"date": str(date.today())}, {"$inc": {"count": 1}}, upsert=True)

def daily_limit_reached(limit: int = 10) -> bool:
    return get_today_count() >= limit


# ── Warm leads ────────────────────────────────────────────────────────────────

def save_warm_lead(author_name: str, author_title: str, post_snippet: str, comment: str):
    db = get_db()
    db.warm_leads.insert_one({
        "author_name":    author_name,
        "author_title":   author_title,
        "post_snippet":   post_snippet[:300],
        "comment_posted": comment,
        "date":           str(date.today()),
    })

def get_warm_leads(days: int = 7):
    from datetime import timedelta
    db     = get_db()
    cutoff = str(date.today() - timedelta(days=days))
    return list(db.warm_leads.find({"date": {"$gte": cutoff}}, {"_id": 0}))