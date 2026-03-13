"""
feed_reader.py — Async LinkedIn Feed Scanner

Scrolls the LinkedIn feed, extracts posts from the DOM, and uses Groq LLM
to evaluate which posts are high-value networking targets for a Data Scientist.
Calls a callback for each qualified post so bot.py can send it to Telegram live.

URL Extraction Strategy:
  LinkedIn no longer puts post URNs in the DOM.  We click the 3-dot menu →
  "Copy link to post" → a toast appears with a "View post" <a> tag that
  contains the real post URL. We grab the href from that link.
"""

import os, json, asyncio, random, re
from playwright.async_api import async_playwright
from dotenv import load_dotenv

try:
    from groq import Groq
except ImportError:
    raise SystemExit("Groq library not found. Run: pip install groq")

load_dotenv()

# Point Playwright to the workspace cache so it survives on Koyeb
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/workspace/.playwright'

# ── Config ────────────────────────────────────────────────────────────────────
COOKIES_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "li_cookies.json")
SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_screenshots")

os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _clean_post_text_for_display(raw_text: str, author_name: str = "", author_title: str = "") -> str:
    """
    Strip LinkedIn feed UI chrome (Feed postSuggested, Verified Profile, author/title repeat,
    date • Follow) so we show only the actual post content.
    """
    if not raw_text or len(raw_text) < 20:
        return raw_text

    # Normalize early so regex handling is easier
    text = " ".join(raw_text.strip().split())

    # Remove feed chrome phrases only (exact phrases so we don't strip content)
    for phrase in (
        "Feed postSuggested",
        "Feed post",
        "Verified Profile",
        "Premium Profile",
    ):
        if text.lower().startswith(phrase.lower()):
            text = text[len(phrase):].lstrip()
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)

    # Remove leading timestamps / follow markers or common preamble forms
    text = re.sub(r"^(?:\d+\s*[dhm]?\s*•\s*|\d+d\s*•\s*(?:Edited\s*•\s*)?Follow\s*)", "", text, flags=re.IGNORECASE)

    # Strip ordinal prefix (1st/2nd/3rd/4th etc) and author hints
    text = re.sub(r"^(?:1st|2nd|3rd|\d+th)\s*", "", text, flags=re.IGNORECASE)

    if author_name:
        safe_author = re.escape(author_name)
        patterns = [
            rf"^{safe_author}\s*(?:,|·|•|[-])\s*",
            rf"^{safe_author}\s+",
            rf"^(?:1st|2nd|3rd|\d+th)\s*{safe_author}\s*(?:,|·|•|[-])?\s*",
            rf"^(?:1st|2nd|3rd|\d+th){safe_author}\s*(?:,|·|•|[-])?\s*",
        ]
        for pattern in patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    if author_title and len(author_title) > 5:
        safe_title = re.escape(author_title)
        text = re.sub(rf"^{safe_title}\s*(?:,|·|•|[-])?\s*", "", text, flags=re.IGNORECASE)

    # Remove repeated author + title prefix in the form "Name • Title" at beginning
    if author_name and author_title:
        candidate_re = rf"^{re.escape(author_name)}\s*[•·]\s*{re.escape(author_title)}"
        match = re.match(candidate_re, text, flags=re.IGNORECASE)
        if match:
            text = text[match.end():].lstrip()

    # Normalize whitespace and return fallback to raw_text if empty
    text = " ".join(text.split())
    return text.strip() if text else raw_text


# ── Async Auth ────────────────────────────────────────────────────────────────
async def _async_login(context, page):
    """Perform LinkedIn login and save fresh cookies."""
    email    = os.environ.get("LI_EMAIL")
    password = os.environ.get("LI_PASSWORD")

    if not email or not password:
        raise RuntimeError("LI_EMAIL and LI_PASSWORD must be set in .env")

    print("[feed] Logging in with email/password...")
    await page.goto("https://www.linkedin.com/login", wait_until="networkidle")
    await asyncio.sleep(random.uniform(3, 5))

    username = page.locator("#username")
    if await username.is_visible(timeout=5000):
        await username.fill(email)
        await asyncio.sleep(random.uniform(0.4, 1.0))
        await page.locator("#password").fill(password)
        await asyncio.sleep(random.uniform(0.4, 0.9))
        await page.locator("button[type='submit']").click()
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
        await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "login_failed.png"))
        raise RuntimeError(f"LinkedIn login failed — stuck at {page.url}")

    cookies = await context.cookies()
    with open(COOKIES_PATH, "w") as f:
        json.dump(cookies, f)
    print(f"[feed] ✓ Login successful, saved {len(cookies)} cookies.")


# ── URL Extraction ────────────────────────────────────────────────────────────
async def _extract_post_url(page, post_element):
    """
    Extract the post URL by clicking the 3-dot menu → "Copy link to post".
    The toast that appears contains a "View post" link with the real URL.
    Returns the URL string, or empty string on failure.
    """
    try:
        menu_btn = post_element.locator('button[aria-label*="control menu"]')
        if await menu_btn.count() == 0:
            return ""

        await menu_btn.first.click()
        await asyncio.sleep(1.5)

        copy_item = page.get_by_text("Copy link to post")
        if await copy_item.count() == 0:
            # Close menu and bail
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.5)
            return ""

        await copy_item.first.click()
        await asyncio.sleep(1.5)

        # Grab the URL from the toast's "View post" link
        toast_links = await page.evaluate("""() => {
            let alerts = document.querySelectorAll('[role="alert"] a');
            for (let a of alerts) {
                if (a.textContent.trim().toLowerCase().includes('view post')) {
                    return a.href;
                }
            }
            return '';
        }""")

        # Dismiss the toast by clicking elsewhere
        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        # Clean the URL (strip tracking params)
        if toast_links:
            base = toast_links.split("?")[0]
            return base + "/"  if not base.endswith("/") else base

        return ""

    except Exception as e:
        print(f"[feed] URL extraction error: {e}")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return ""


# ── Main Scanner ──────────────────────────────────────────────────────────────
async def get_feed_posts(callback_func, max_targets=10):
    """
    Scrape LinkedIn feed and call callback_func(data) for every qualified target.

    data = {
        "url":          str,   # LinkedIn post URL
        "text":         str,   # raw post text
        "author_name":  str,   # extracted by AI
        "author_title": str,   # extracted by AI
        "reason":       str,   # why it's a good target
    }
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set in environment.")

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

        # Load cookies
        if os.path.exists(COOKIES_PATH):
            with open(COOKIES_PATH, "r") as f:
                await context.add_cookies(json.load(f))
            print("[feed] Loaded saved cookies.")
        else:
            print("[feed] No cookies — fresh login required.")

        page = await context.new_page()

        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(5, 7))

            # Re-login if session expired
            if "/login" in page.url or "/authwall" in page.url:
                print("[feed] Session expired — re-authenticating...")
                await _async_login(context, page)
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(5, 7))
                if "/login" in page.url or "/authwall" in page.url:
                    raise RuntimeError("Feed still redirecting after login.")

            await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "feed_loaded.png"))

            # Refresh cookies so they stay valid for future runs
            fresh_cookies = await context.cookies()
            with open(COOKIES_PATH, "w") as f:
                json.dump(fresh_cookies, f)
            print(f"[feed] ✓ Feed loaded. Cookies refreshed ({len(fresh_cookies)}). Starting scan...")

            found_count = 0
            processed_keys = set()  # Track processed posts by componentkey

            for rnd in range(30):   # Up to 30 scroll rounds
                if found_count >= max_targets:
                    break

                # Find all post containers
                posts = page.locator('div[role="listitem"][componentkey]')
                count = await posts.count()

                if count == 0:
                    await page.mouse.wheel(0, random.randint(2000, 4000))
                    await asyncio.sleep(random.uniform(3, 5))
                    continue

                # Process each unscraped post
                new_posts_found = False
                for idx in range(count):
                    if found_count >= max_targets:
                        break

                    post = posts.nth(idx)
                    comp_key = await post.get_attribute("componentkey")

                    if comp_key in processed_keys:
                        continue
                    processed_keys.add(comp_key)
                    new_posts_found = True

                    # Extract text
                    raw_text = await post.evaluate("el => el.textContent")
                    raw_text = " ".join(raw_text.split())  # normalize whitespace

                    if len(raw_text) < 50:
                        continue

                    # Promoted/sponsored posts are not filtered; they're evaluated like normal posts.

                    # ── AI Evaluation ──────────────────────────────────
                    try:
                        resp = client.chat.completions.create(
                            model="llama-3.1-8b-instant",
                            messages=[{
                                "role": "user",
                                "content": (
                                    "Evaluate this LinkedIn post for networking value "
                                    "for an aspiring Data Scientist / AI Engineer.\n"
                                    "Return ONLY valid JSON with keys:\n"
                                    '  "worth": boolean — is this a strong networking target?\n'
                                    '  "reason": string — 1-2 sentence explanation\n'
                                    '  "author_name": string — extract the post author\'s full name\n'
                                    '  "author_title": string — extract their job title/headline\n\n'
                                    f"Post text:\n{raw_text[:3000]}"
                                ),
                            }],
                            response_format={"type": "json_object"},
                            temperature=0.1,
                        )
                        eval_data = json.loads(resp.choices[0].message.content)
                    except Exception as e:
                        print(f"[feed] Groq evaluation error: {e}")
                        continue

                    if not eval_data.get("worth"):
                        author = eval_data.get("author_name", "?")
                        print(f"[feed] ⏭ Skipped: {author} — {eval_data.get('reason', 'N/A')[:60]}")
                        continue

                    # ── Extract Post URL (via 3-dot menu) ─────────────
                    post_url = await _extract_post_url(page, post)
                    if not post_url:
                        print(f"[feed] ⚠ Could not extract URL, skipping.")
                        continue

                    found_count += 1
                    author = eval_data.get("author_name", "Unknown")
                    author_title = eval_data.get("author_title", "Professional")
                    display_text = _clean_post_text_for_display(raw_text, author, author_title)
                    # Use cleaned text for display and for comment generation; keep it readable
                    post_text = display_text if len(display_text) > 50 else raw_text
                    print(f"[feed] 🎯 Target #{found_count}: {author}")

                    await callback_func({
                        "url":          post_url,
                        "text":         post_text,
                        "raw_text":     raw_text,
                        "author_name":  author,
                        "author_title": author_title,
                        "reason":       eval_data.get("reason", ""),
                    })

                    # Small delay between posts
                    await asyncio.sleep(random.uniform(1.5, 3))

                # Scroll down if we processed some posts
                if not new_posts_found or rnd % 2 == 0:
                    await page.mouse.wheel(0, random.randint(2000, 4000))
                    await asyncio.sleep(random.uniform(3, 5))

            print(f"[feed] Scan finished. Found {found_count} targets.")

        finally:
            await browser.close()


# ── Standalone test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    results = []

    async def _collect(data):
        results.append(data)
        print(f"  → {data['author_name']} — {data['author_title']}")
        print(f"    URL: {data['url']}")
        print(f"    Why: {data['reason']}\n")

    async def main():
        await get_feed_posts(_collect, max_targets=5)

        print(f"\n{'═' * 70}")
        print(f"  Found {len(results)} high-value targets.")
        print(f"{'═' * 70}\n")

        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed_results.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"Results saved to {out_path}")

    asyncio.run(main())