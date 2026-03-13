"""
refresh_cookies_job.py — Runs inside GitHub Actions (weekly schedule).

Logs into LinkedIn, saves fresh cookies to MongoDB.
Acts as a safety net in case cookies expire between sessions.
"""

import asyncio, os, random, requests
from playwright.async_api import async_playwright
from dotenv import load_dotenv
from db import save_cookies

load_dotenv()

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCREENSHOTS_DIR  = "debug_screenshots"

os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def send_message(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=15,
    )


async def refresh():
    email    = os.environ["LI_EMAIL"]
    password = os.environ["LI_PASSWORD"]

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
        page = await context.new_page()

        try:
            print("[refresh] Navigating to LinkedIn login...")
            await page.goto("https://www.linkedin.com/login", wait_until="networkidle")
            await asyncio.sleep(random.uniform(3, 5))

            await page.locator("#username").fill(email)
            await asyncio.sleep(random.uniform(0.4, 1.0))
            await page.locator("#password").fill(password)
            await asyncio.sleep(random.uniform(0.4, 0.9))
            await page.locator("button[type='submit']").click()

            await page.wait_for_load_state("load")
            await asyncio.sleep(random.uniform(4, 6))

            if "/login" in page.url or "/authwall" in page.url or "/checkpoint" in page.url:
                await page.screenshot(path=f"{SCREENSHOTS_DIR}/refresh_failed.png")
                raise RuntimeError(f"Login failed — stuck at {page.url}")

            # Navigate to feed to fully establish session
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
            await asyncio.sleep(random.uniform(4, 6))

            cookies = await context.cookies()
            save_cookies(cookies)

            await page.screenshot(path=f"{SCREENSHOTS_DIR}/refresh_success.png")
            print(f"[refresh] ✓ Saved {len(cookies)} fresh cookies to MongoDB.")
            send_message(f"✅ LinkedIn cookies refreshed — {len(cookies)} cookies saved to MongoDB.")

        except Exception as e:
            print(f"[refresh] ✗ Error: {e}")
            send_message(f"❌ Cookie refresh failed: {e}")
            raise

        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(refresh())
