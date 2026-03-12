# MongoDB Collections & Schema

**Database:** `linkedin_bot` (set via `MONGO_URI` in `.env`)

---

## 1. `commented_posts`

**Purpose:** Deduplication — avoid commenting on the same post twice.

| Field         | Type     | Description                |
|---------------|----------|----------------------------|
| `post_url`    | string   | LinkedIn post URL          |
| `commented_at`| datetime | UTC when comment was posted |

**Index:** Use `post_url` for lookups (`already_commented`, `mark_commented`).

---

## 2. `daily_count`

**Purpose:** Enforce the 10 comments/day limit.

| Field  | Type   | Description                    |
|--------|--------|--------------------------------|
| `date` | string | `YYYY-MM-DD` (e.g. `"2026-03-12"`) |
| `count`| int    | Number of comments posted that day |

One document per day; `count` is incremented with `$inc` on each successful post.

---

## 3. `warm_leads`

**Purpose:** Weekly review list — people you commented to (for follow-up).

| Field           | Type   | Description                    |
|-----------------|--------|--------------------------------|
| `author_name`   | string | Post author’s name            |
| `author_title`  | string | Their job title/headline      |
| `post_snippet`  | string | First 300 chars of post text  |
| `comment_posted`| string | The comment you posted       |
| `date`          | string | `YYYY-MM-DD`                  |

---

## 4. `activity_logs`

**Purpose:** In-depth logs for every scraped target and every news draft (raw data, Telegram messages, actions, comment versions).

Documents have a **`type`** field: either `"target"` or `"news"`.

### 4a. Target document (`type: "target"`)

One document per target card sent to Telegram (scraped post → approve/skip → comment flow).

| Field              | Type     | Description |
|--------------------|----------|-------------|
| `type`             | string   | `"target"`  |
| `target_id`        | string   | Bot’s short id (e.g. `"a1b2c3d4"`) |
| `created_at`       | datetime | UTC when the target card was sent |
| `scraped`           | object   | See below   |
| `tele_sent`        | string   | Exact Telegram message (target card text) |
| `actions`          | array    | List of `{ "at": datetime, "action": string }` — `"skip"`, `"approve"`, `"queued"`, `"dropped"`, `"posted"` |
| `comment_versions` | array    | Each draft shown: `{ "at": datetime, "draft": string }` (first + every Regenerate) |
| `final_action`     | string?  | `"skipped"` \| `"queued"` \| `"dropped"` \| `"posted"` |
| `final_comment`    | string?  | The comment text if queued or posted |

**`scraped` object:**

| Field          | Type   | Description                          |
|----------------|--------|--------------------------------------|
| `raw_text`     | string | Full scraped post text (max 10000)   |
| `url`          | string | Post URL                             |
| `author_name`  | string | Author name                          |
| `author_title` | string | Author title/headline                |
| `reason`       | string | AI “why this target” reason           |

### 4b. News document (`type: "news"`)

One document per news “session” (initial fetch or a “Fetch again” — each Fetch again creates a new document).

| Field           | Type     | Description |
|-----------------|----------|-------------|
| `type`          | string   | `"news"`    |
| `news_id`       | string   | Bot’s short id for this draft session |
| `created_at`    | datetime | UTC when this session was created |
| `search_raw`    | string   | Raw Tavily search context (max 15000) |
| `drafts`        | array    | Each draft: `{ "at": datetime, "source": "fetch" \| "regen", "content": string }` |
| `tele_sent`     | array    | Telegram messages sent (one per draft shown) |
| `actions`       | array    | `{ "at": datetime, "action": string }` — `"fetch"`, `"regen"`, `"post"`, `"drop"` |
| `final_action`  | string?  | `"posted"` \| `"dropped"` |
| `final_content` | string?  | The content that was posted to LinkedIn (if posted) |

---

## Summary

| Collection        | Purpose                          |
|-------------------|----------------------------------|
| `commented_posts` | Don’t comment same post twice    |
| `daily_count`     | 10 comments/day limit            |
| `warm_leads`      | Weekly review (who you commented)|
| `activity_logs`   | Full audit: targets + news (raw, tele, actions, versions) |

All datetimes are stored in **UTC** (`datetime.now(timezone.utc)`).
