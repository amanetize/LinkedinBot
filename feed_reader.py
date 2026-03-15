"""
feed_reader.py — Async LinkedIn Feed Scanner

All post data (author, title, connection level, counts, post text, post url)
is extracted by a single Groq vision+text call per post — no DOM churning.

Each qualifying post gets a temp screenshot sent to Telegram, then deleted.
Cookies loaded from / saved back to MongoDB.
"""

import os, json, asyncio, random, base64, tempfile
from pathlib import Path
from playwright.async_api import async_playwright
from dotenv import load_dotenv

try:
    from groq import Groq
except ImportError:
    raise SystemExit("Groq library not found. Run: pip install groq")

load_dotenv()

SCREENSHOTS_DIR = Path(__file__).parent / "debug_screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_cookies_from_db() -> list:
    try:
        from db import get_cookies
        return get_cookies()
    except Exception as e:
        print(f"[feed] DB cookie load error: {e}")
        return []

def _save_cookies_to_db(cookies: list):
    try:
        from db import save_cookies
        save_cookies(cookies)
    except Exception as e:
        print(f"[feed] DB cookie save error: {e}")


# ── AI extraction ─────────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """You are given the raw innerText of a LinkedIn feed post card, plus a screenshot.

Extract ALL of the following fields. Return ONLY valid JSON, no extra text.

{
  "worth":            boolean   -- true if valuable for an aspiring Data Scientist / AI Engineer to engage with,
  "reason":           string    -- 1-2 sentence explanation of why (or why not),
  "author_name":      string    -- full name of the person who wrote the post,
  "author_title":     string    -- job title / headline of the author,
  "connection_level": string    -- "1st", "2nd", "3rd", or "" if not shown,
  "likes_count":      integer   -- number of reactions/likes (0 if not shown),
  "comments_count":   integer   -- number of comments (0 if not shown),
  "post_text":        string    -- clean body text of the post only (strip author name, title,
                                   timestamps, "Follow", reaction counts, and LinkedIn chrome),
  "post_url":         string    -- the full URL of the post if visible anywhere in the text
                                   (look for linkedin.com/posts/ or linkedin.com/feed/update/);
                                   return "" if not found
}

Raw post text:
"""

_EVAL_MODEL   = "openai/gpt-oss-20b"       # fast structured extraction fallback
_VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


async def _ai_extract_post(client: Groq, raw_text: str, screenshot_b64) -> dict:
    prompt = _EXTRACT_PROMPT + raw_text[:4000]

    # Try vision model first if we have a screenshot
    if screenshot_b64:
        try:
            resp = client.chat.completions.create(
                model=_VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=800,
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            print(f"[feed] Vision model error, falling back to text-only: {e}")

    # Text-only fallback
    try:
        resp = client.chat.completions.create(
            model=_EVAL_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=800,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        print(f"[feed] Text-only extraction error: {e}")
        return {}


async def _screenshot_element(post_element):
    """
    Screenshot just the post element.
    Returns (base64_string, temp_file_path).
    Caller must delete the temp file after use.
    """
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()
        await post_element.screenshot(path=tmp_path)
        with open(tmp_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        return b64, tmp_path
    except Exception as e:
        print(f"[feed] Screenshot error: {e}")
        return None, None


def _cleanup_temp(path):
    if path:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


# ── LinkedIn login ────────────────────────────────────────────────────────────

async def _async_login(context, page):
    email    = os.environ.get("LI_EMAIL")
    password = os.environ.get("LI_PASSWORD")
    if not email or not password:
        raise RuntimeError("LI_EMAIL and LI_PASSWORD must be set.")

    print("[feed] Logging in...")
    await page.goto("https://www.linkedin.com/login", wait_until="networkidle")
    await asyncio.sleep(random.uniform(3, 5))

    username_loc = page.locator("#username")
    if await username_loc.is_visible(timeout=5000):
        await username_loc.fill(email)
    else:
        try:
            await page.click("text=Sign in using another account", timeout=5000)
        except Exception:
            pass
        await page.wait_for_selector("#username", state="visible", timeout=15000)
        await page.locator("#username").fill(email)

    await asyncio.sleep(random.uniform(0.4, 1.0))
    await page.locator("#password").fill(password)
    await asyncio.sleep(random.uniform(0.4, 0.9))
    await page.locator("button[type='submit']").click()

    await page.wait_for_load_state("load")
    await asyncio.sleep(random.uniform(4, 6))

    if "/login" in page.url or "/authwall" in page.url or "/checkpoint" in page.url:
        await page.screenshot(path=str(SCREENSHOTS_DIR / "login_failed.png"))
        raise RuntimeError(f"LinkedIn login failed — stuck at {page.url}")

    cookies = await context.cookies()
    _save_cookies_to_db(cookies)
    print(f"[feed] ✓ Login successful, saved {len(cookies)} cookies.")


# ── URL extraction fallback ───────────────────────────────────────────────────

async def _extract_post_url_via_menu(page, post_element) -> str:
    """Open the post control menu, click 'Copy link to post', read URL from toast."""
    try:
        menu_btn = post_element.locator('button[aria-label*="control menu"]')
        if await menu_btn.count() == 0:
            return ""
        await menu_btn.first.click()
        await asyncio.sleep(1.5)

        copy_item = page.get_by_text("Copy link to post")
        if await copy_item.count() == 0:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            return ""
        await copy_item.first.click()
        await asyncio.sleep(1.5)

        toast_url = await page.evaluate("""() => {
            for (let a of document.querySelectorAll('[role="alert"] a')) {
                if (a.textContent.trim().toLowerCase().includes('view post')) return a.href;
            }
            return '';
        }""")

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        if toast_url:
            base = toast_url.split("?")[0]
            return base + "/" if not base.endswith("/") else base
        return ""

    except Exception as e:
        print(f"[feed] URL menu extraction error: {e}")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return ""


# ── Main feed scanner ─────────────────────────────────────────────────────────

async def get_feed_posts(callback_func, max_targets: int = 10):
    """
    Scan the LinkedIn feed. For each post deemed worth engaging:
      - AI extracts all fields from innerText + screenshot in one call
      - data['screenshot_path'] is a temp PNG file path
      - caller (scraper_job.py) must send the photo then delete the file
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set.")

    client = Groq(api_key=api_key)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        )

        cookies = _load_cookies_from_db()
        if cookies:
            await context.add_cookies(cookies)
            print("[feed] Loaded cookies from MongoDB.")
        else:
            print("[feed] No cookies — fresh login required.")

        page = await context.new_page()

        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(5, 7))

            if "/login" in page.url or "/authwall" in page.url:
                print("[feed] Session expired — re-authenticating...")
                await _async_login(context, page)
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(5, 7))
                if "/login" in page.url or "/authwall" in page.url:
                    raise RuntimeError("Feed still redirecting after login.")

            await page.screenshot(path=str(SCREENSHOTS_DIR / "feed_loaded.png"))
            fresh_cookies = await context.cookies()
            _save_cookies_to_db(fresh_cookies)
            print(f"[feed] ✓ Feed loaded. Saved {len(fresh_cookies)} cookies. Scanning...")

            found_count    = 0
            processed_keys = set()

            for rnd in range(30):
                if found_count >= max_targets:
                    break

                posts = page.locator('div[role="listitem"][componentkey]')
                count = await posts.count()

                if count == 0:
                    await page.mouse.wheel(0, random.randint(2000, 4000))
                    await asyncio.sleep(random.uniform(3, 5))
                    continue

                new_this_round = False

                for idx in range(count):
                    if found_count >= max_targets:
                        break

                    post     = posts.nth(idx)
                    comp_key = await post.get_attribute("componentkey")
                    if comp_key in processed_keys:
                        continue
                    processed_keys.add(comp_key)
                    new_this_round = True

                    raw_text = await post.evaluate("el => el.innerText")
                    raw_text = " ".join(raw_text.split())

                    if len(raw_text) < 50:
                        continue

                    # Screenshot the element (used by both AI and Telegram)
                    screenshot_b64, screenshot_path = await _screenshot_element(post)

                    # One AI call: evaluate + extract all fields
                    data = await _ai_extract_post(client, raw_text, screenshot_b64)

                    if not data:
                        print(f"[feed] ⚠ AI extraction returned nothing for post #{idx}")
                        _cleanup_temp(screenshot_path)
                        continue

                    if not data.get("worth"):
                        print(f"[feed] ⏭ Skipped: {data.get('author_name','?')} — {data.get('reason','')[:70]}")
                        _cleanup_temp(screenshot_path)
                        continue

                    # Prefer AI-extracted URL; fall back to menu extraction
                    post_url = (data.get("post_url") or "").strip()
                    if not post_url or "linkedin.com" not in post_url:
                        post_url = await _extract_post_url_via_menu(page, post)

                    if not post_url:
                        print("[feed] ⚠ Could not get post URL, skipping.")
                        _cleanup_temp(screenshot_path)
                        continue

                    found_count += 1
                    author = data.get("author_name", "Unknown")
                    conn   = data.get("connection_level", "")
                    likes  = int(data.get("likes_count") or 0)
                    cmts   = int(data.get("comments_count") or 0)

                    print(f"[feed] 🎯 Target #{found_count}: {author} ({conn}) — {likes}❤️ {cmts}💬")

                    await callback_func({
                        "url":              post_url,
                        "text":             (data.get("post_text") or raw_text)[:1500],
                        "raw_text":         raw_text,
                        "author_name":      author,
                        "author_title":     data.get("author_title", ""),
                        "connection_level": conn,
                        "likes_count":      likes,
                        "comments_count":   cmts,
                        "reason":           data.get("reason", ""),
                        "screenshot_path":  screenshot_path,  # temp file; caller must delete
                    })

                    await asyncio.sleep(random.uniform(1.5, 3))

                if not new_this_round or rnd % 2 == 0:
                    await page.mouse.wheel(0, random.randint(2000, 4000))
                    await asyncio.sleep(random.uniform(3, 5))

            print(f"[feed] Scan finished. Found {found_count} targets.")

        finally:
            await browser.close()
