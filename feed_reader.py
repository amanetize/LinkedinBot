"""
feed_reader.py — Async LinkedIn Feed Scanner

Cookies loaded from MongoDB first, file as fallback.
Saves cookies back to MongoDB after every successful session.
"""

import os, json, asyncio, random, re
from playwright.async_api import async_playwright
from dotenv import load_dotenv

try:
    from groq import Groq
except ImportError:
    raise SystemExit("Groq library not found. Run: pip install groq")

load_dotenv()

SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


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


def _clean_post_text_for_display(raw_text: str, author_name: str = "", author_title: str = "") -> str:
    if not raw_text or len(raw_text) < 20:
        return raw_text

    text = " ".join(raw_text.strip().split())

    for phrase in ("Feed postSuggested", "Feed post", "Verified Profile", "Premium Profile"):
        if text.lower().startswith(phrase.lower()):
            text = text[len(phrase):].lstrip()
        text = re.sub(re.escape(phrase), " ", text, flags=re.IGNORECASE)

    text = re.sub(r"^(?:\d+\s*[dhm]?\s*•\s*|\d+d\s*•\s*(?:Edited\s*•\s*)?Follow\s*)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:1st|2nd|3rd|\d+th)\s*", "", text, flags=re.IGNORECASE)

    if author_name:
        safe_author = re.escape(author_name)
        for pattern in [
            rf"^{safe_author}\s*(?:,|·|•|[-])\s*",
            rf"^{safe_author}\s+",
            rf"^(?:1st|2nd|3rd|\d+th)\s*{safe_author}\s*(?:,|·|•|[-])?\s*",
        ]:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    if author_title and len(author_title) > 5:
        text = re.sub(rf"^{re.escape(author_title)}\s*(?:,|·|•|[-])?\s*", "", text, flags=re.IGNORECASE)

    if author_name and author_title:
        match = re.match(rf"^{re.escape(author_name)}\s*[•·]\s*{re.escape(author_title)}", text, flags=re.IGNORECASE)
        if match:
            text = text[match.end():].lstrip()

    text = " ".join(text.split())
    return text.strip() if text else raw_text


async def _async_login(context, page):
    """Perform LinkedIn login and save fresh cookies to MongoDB."""
    email    = os.environ.get("LI_EMAIL")
    password = os.environ.get("LI_PASSWORD")

    if not email or not password:
        raise RuntimeError("LI_EMAIL and LI_PASSWORD must be set.")

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
    _save_cookies_to_db(cookies)
    print(f"[feed] ✓ Login successful, saved {len(cookies)} cookies to MongoDB.")


async def _extract_post_url(page, post_element):
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

        toast_links = await page.evaluate("""() => {
            let alerts = document.querySelectorAll('[role="alert"] a');
            for (let a of alerts) {
                if (a.textContent.trim().toLowerCase().includes('view post')) {
                    return a.href;
                }
            }
            return '';
        }""")

        await page.keyboard.press("Escape")
        await asyncio.sleep(0.5)

        if toast_links:
            base = toast_links.split("?")[0]
            return base + "/" if not base.endswith("/") else base

        return ""

    except Exception as e:
        print(f"[feed] URL extraction error: {e}")
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return ""


async def get_feed_posts(callback_func, max_targets=10):
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

        # Load cookies from MongoDB
        cookies = _load_cookies_from_db()
        if cookies:
            await context.add_cookies(cookies)
            print("[feed] Loaded cookies from MongoDB.")
        else:
            print("[feed] No cookies in MongoDB — fresh login required.")

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

            await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "feed_loaded.png"))

            # Save refreshed cookies after successful feed load
            fresh_cookies = await context.cookies()
            _save_cookies_to_db(fresh_cookies)
            print(f"[feed] ✓ Feed loaded. Cookies saved ({len(fresh_cookies)}). Starting scan...")

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

                new_posts_found = False
                for idx in range(count):
                    if found_count >= max_targets:
                        break

                    post     = posts.nth(idx)
                    comp_key = await post.get_attribute("componentkey")

                    if comp_key in processed_keys:
                        continue
                    processed_keys.add(comp_key)
                    new_posts_found = True

                    raw_text = await post.evaluate("el => el.textContent")
                    raw_text = " ".join(raw_text.split())

                    if len(raw_text) < 50:
                        continue

                    try:
                        resp = client.chat.completions.create(
                            model="llama-3.1-8b-instant",
                            messages=[{
                                "role": "user",
                                "content": (
                                    "Evaluate this LinkedIn post for networking value "
                                    "for an aspiring Data Scientist / AI Engineer.\n"
                                    "Return ONLY valid JSON with keys:\n"
                                    '  "worth": boolean\n'
                                    '  "reason": string — 1-2 sentence explanation\n'
                                    '  "author_name": string\n'
                                    '  "author_title": string\n\n'
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
                        print(f"[feed] ⏭ Skipped: {eval_data.get('author_name','?')} — {eval_data.get('reason','')[:60]}")
                        continue

                    post_url = await _extract_post_url(page, post)
                    if not post_url:
                        print("[feed] ⚠ Could not extract URL, skipping.")
                        continue

                    found_count  += 1
                    author        = eval_data.get("author_name", "Unknown")
                    author_title  = eval_data.get("author_title", "Professional")
                    display_text  = _clean_post_text_for_display(raw_text, author, author_title)
                    post_text     = display_text if len(display_text) > 50 else raw_text
                    print(f"[feed] 🎯 Target #{found_count}: {author}")

                    await callback_func({
                        "url":          post_url,
                        "text":         post_text,
                        "raw_text":     raw_text,
                        "author_name":  author,
                        "author_title": author_title,
                        "reason":       eval_data.get("reason", ""),
                    })

                    await asyncio.sleep(random.uniform(1.5, 3))

                if not new_posts_found or rnd % 2 == 0:
                    await page.mouse.wheel(0, random.randint(2000, 4000))
                    await asyncio.sleep(random.uniform(3, 5))

            print(f"[feed] Scan finished. Found {found_count} targets.")

        finally:
            await browser.close()
