"""
poster.py — Async LinkedIn Comment Poster

Uses Playwright (async) to post comments on LinkedIn posts.
Handles cookie-based auth with automatic re-login on session expiry.
Screenshots are saved to debug_screenshots/ on failure for debugging.
"""

import os, json, asyncio, random
from playwright.async_api import async_playwright
from dotenv import load_dotenv

load_dotenv()

COOKIES_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "li_cookies.json")
SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


async def _async_login(context, page):
    """Perform LinkedIn login and save fresh cookies."""
    email    = os.environ.get("LI_EMAIL")
    password = os.environ.get("LI_PASSWORD")

    if not email or not password:
        raise RuntimeError("LI_EMAIL and LI_PASSWORD must be set in .env for re-login.")

    print("[poster][auth] Session expired — logging in...")
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
    print(f"[poster][auth] ✓ Re-login successful, saved {len(cookies)} cookies.")


async def post_comment(post_url: str, comment_text: str) -> bool:
    """
    Navigate to a LinkedIn post and leave a comment.
    Returns True on success, False on failure.
    """
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
        else:
            print("[poster] No cookies found — cannot post.")
            await browser.close()
            return False

        page = await context.new_page()

        try:
            # ── Navigate to the post ───────────────────────────────────
            await page.goto(post_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(3, 5))

            # Re-login if session expired
            if "/login" in page.url or "/authwall" in page.url:
                await _async_login(context, page)
                await page.goto(post_url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(3, 5))

            # ── Find the comment box ───────────────────────────────────
            comment_selectors = [
                'div.comments-comment-texteditor div.ql-editor[contenteditable="true"]',
                'div[role="textbox"][contenteditable="true"]',
                'div.ql-editor[data-placeholder]',
                'div.comments-comment-box__form div[contenteditable="true"]',
            ]

            comment_box = None
            for sel in comment_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        comment_box = loc
                        break
                except Exception:
                    continue

            # If not visible, click the "Comment" button first
            if not comment_box:
                try:
                    btn = page.locator('button[aria-label*="omment"], button:has-text("Comment")').first
                    await btn.click()
                    await asyncio.sleep(2)
                    for sel in comment_selectors:
                        try:
                            loc = page.locator(sel).first
                            if await loc.is_visible(timeout=3000):
                                comment_box = loc
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            if not comment_box:
                print("[poster] Could not find comment box.")
                await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "no_comment_box.png"))
                await browser.close()
                return False

            # ── Type the comment (human-like) ──────────────────────────
            await comment_box.click()
            await asyncio.sleep(0.5)

            for char in comment_text:
                await page.keyboard.type(char, delay=random.randint(30, 80))
                if random.random() < 0.03:
                    await asyncio.sleep(random.uniform(0.2, 0.5))

            await asyncio.sleep(1)

            # ── Submit ─────────────────────────────────────────────────
            submit_selectors = [
                'button.comments-comment-box__submit-button',
                'button[data-control-name="comment_submit"]',
                'button.comments-comment-box__submit-button--cr',
                'form.comments-comment-box__form button[type="submit"]',
            ]

            submitted = False
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                # Fallback: Ctrl+Enter often submits on LinkedIn
                await page.keyboard.press("Control+Enter")

            await asyncio.sleep(3)
            print(f"[poster] ✓ Comment posted on {post_url}")
            await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "comment_posted.png"))
            await browser.close()
            return True

        except Exception as e:
            print(f"[poster] ✗ Error: {e}")
            try:
                await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "poster_error.png"))
            except Exception:
                pass
            await browser.close()
            return False


async def scrape_comments(post_url: str, max_comments: int = 15) -> list:
    """
    Navigate to a LinkedIn post and extract existing comment texts.
    Returns a list of comment strings. Returns [] on failure.
    """
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

        if os.path.exists(COOKIES_PATH):
            with open(COOKIES_PATH, "r") as f:
                await context.add_cookies(json.load(f))

        page = await context.new_page()

        try:
            await page.goto(post_url, wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(4, 6))

            if "/login" in page.url or "/authwall" in page.url:
                await _async_login(context, page)
                await page.goto(post_url, wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(4, 6))

            # Extract comment texts using multiple strategies
            comments = await page.evaluate("""(maxComments) => {
                let results = [];

                // Strategy 1: Find elements commonly used for comment text
                let selectors = [
                    'div[class*="comment"] span[dir="ltr"]',
                    'div[class*="comment"] div[dir="ltr"]',
                    'article span[dir="ltr"]',
                    'div[role="textbox"][contenteditable="false"]',
                    'div.comments-comment-item__main-content',
                    'span.comments-comment-item__inline-show-more-text',
                ];

                for (let sel of selectors) {
                    let els = document.querySelectorAll(sel);
                    if (els.length > 0) {
                        for (let el of els) {
                            let text = (el.textContent || '').trim();
                            // Filter: must look like a real comment (not a name, not a button)
                            if (text.length > 15 && text.length < 2000 && !results.includes(text)) {
                                results.push(text);
                            }
                            if (results.length >= maxComments) return results;
                        }
                        if (results.length > 0) return results;
                    }
                }

                // Strategy 2: Look for any visible text blocks near the comments section
                // Walk all visible text-bearing elements below the post
                let allSpans = document.querySelectorAll('main span, main p');
                let seenPost = false;
                for (let el of allSpans) {
                    let text = (el.textContent || '').trim();
                    // Heuristic: skip short labels and the post itself
                    if (text.length > 30 && text.length < 1500) {
                        if (seenPost && !results.includes(text)) {
                            results.push(text);
                        }
                    }
                    // Mark when we've passed the main post area
                    if (text.length > 200) seenPost = true;
                    if (results.length >= maxComments) break;
                }

                return results;
            }""", max_comments)

            print(f"[poster] Scraped {len(comments)} existing comments from {post_url[:60]}")
            await browser.close()
            return comments

        except Exception as e:
            print(f"[poster] Comment scraping error: {e}")
            await browser.close()
            return []


async def create_post(post_content: str) -> bool:
    """
    Create a new LinkedIn post (not a comment).
    Returns True on success, False on failure.
    """
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

        if os.path.exists(COOKIES_PATH):
            with open(COOKIES_PATH, "r") as f:
                await context.add_cookies(json.load(f))
        else:
            print("[poster] No cookies found — cannot post.")
            await browser.close()
            return False

        page = await context.new_page()

        try:
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(4, 6))

            if "/login" in page.url or "/authwall" in page.url:
                await _async_login(context, page)
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(4, 6))

            # Click "Start a post" button
            start_selectors = [
                'button:has-text("Start a post")',
                'button[aria-label*="Start a post"]',
                'button[class*="share-box"]',
                'div[class*="share-box"] button',
            ]

            clicked = False
            for sel in start_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                print("[poster] Could not find 'Start a post' button.")
                await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "no_start_post.png"))
                await browser.close()
                return False

            await asyncio.sleep(random.uniform(2, 3))

            # Find the post editor
            editor_selectors = [
                'div[role="textbox"][contenteditable="true"]',
                'div.ql-editor[contenteditable="true"]',
                'div[data-placeholder*="What do you want to talk about"]',
            ]

            editor = None
            for sel in editor_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        editor = loc
                        break
                except Exception:
                    continue

            if not editor:
                print("[poster] Could not find post editor.")
                await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "no_editor.png"))
                await browser.close()
                return False

            await editor.click()
            await asyncio.sleep(0.5)

            # Type the post content with human-like delays
            for char in post_content:
                await page.keyboard.type(char, delay=random.randint(25, 70))
                if random.random() < 0.02:
                    await asyncio.sleep(random.uniform(0.3, 0.6))

            await asyncio.sleep(random.uniform(1, 2))

            # Click the Post button
            post_selectors = [
                'button:has-text("Post")',
                'button[aria-label="Post"]',
                'button[class*="share-actions__primary"]',
            ]

            posted = False
            for sel in post_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=3000):
                        await btn.click()
                        posted = True
                        break
                except Exception:
                    continue

            if not posted:
                print("[poster] Could not find Post button.")
                await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "no_post_btn.png"))
                await browser.close()
                return False

            await asyncio.sleep(4)
            print("[poster] ✓ LinkedIn post created successfully.")
            await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "post_created.png"))
            await browser.close()
            return True

        except Exception as e:
            print(f"[poster] ✗ Post creation error: {e}")
            try:
                await page.screenshot(path=os.path.join(SCREENSHOTS_DIR, "create_post_error.png"))
            except Exception:
                pass
            await browser.close()
            return False


