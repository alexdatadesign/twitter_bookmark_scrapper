#!/usr/bin/env python3
"""
Twitter/X Bookmark Scraper
──────────────────────────
Uses Playwright to extract bookmarked tweets, expand t.co links,
fetch X native Articles, and export to CSV/JSONL.

Quick start:
    python twitter_bookmark_scrapper.py              # login + scrape in one go
    python twitter_bookmark_scrapper.py --headless   # headless (needs prior session)

Options:
    --save-auth-only    Log in and save session without scraping
    --auth-file FILE    Session JSON path (default: twitter_auth.json)
    --max-scrolls N     Max scroll attempts (default: 100)
    --scroll-delay S    Seconds between scrolls (default: 2.0)
    --output STEM       Output filename stem (default: bookmarks)
    --format FORMAT     csv | jsonl | both (default: csv)
    --headless          Run browser headlessly (requires saved session)
    --no-articles       Skip fetching full text of X native Articles
"""

import argparse
import csv
import json
import logging
import os
import re
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout, Page, BrowserContext

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── URL Helpers ───────────────────────────────────────────────────────────────

LOGIN_SLUGS = ("x.com/login", "x.com/i/flow", "x.com/account/")


def _is_login_page(url: str) -> bool:
    return any(slug in url for slug in LOGIN_SLUGS)


def expand_tco_url(short_url: str, timeout: float = 10) -> str:
    try:
        resp = requests.head(
            short_url, allow_redirects=True, timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return resp.url
    except Exception:
        return short_url


def expand_urls_parallel(urls: list[str], workers: int = 10) -> dict[str, str]:
    tco_urls = [u for u in urls if "t.co/" in u]
    if not tco_urls:
        return {}
    mapping: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(expand_tco_url, u): u for u in tco_urls}
        for fut in as_completed(futures):
            mapping[futures[fut]] = fut.result()
    return mapping


def is_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return "/articles/" in parsed.path and parsed.netloc.lower().endswith(("x.com", "twitter.com"))


# ── Interactive Login ─────────────────────────────────────────────────────────


def interactive_login(context: BrowserContext, auth_file: str) -> bool:
    """
    Navigate to X login, wait for the user to log in, detect success
    via JavaScript URL polling, save session, and return True on success.
    Uses signal-based SIGINT so Ctrl+C saves before Chrome dies.
    """
    page = context.pages[0] if context.pages else context.new_page()
    page.goto("https://x.com/login", wait_until="domcontentloaded")

    log.info("Please log in to X/Twitter in the browser window.")
    log.info("The script will detect login automatically and continue.")

    stop_flag = False
    original_handler = signal.getsignal(signal.SIGINT)

    def _sigint_handler(signum, frame):
        nonlocal stop_flag
        if stop_flag:
            raise SystemExit(1)
        stop_flag = True
        log.info("Ctrl+C received – finishing up …")

    signal.signal(signal.SIGINT, _sigint_handler)

    logged_in = False
    last_log_t = 0.0
    try:
        while not stop_flag:
            all_pages = context.pages
            if not all_pages:
                break

            now = time.monotonic()

            # Get real URLs via JS (page.url doesn't reflect SPA navigation)
            urls = []
            for p in all_pages:
                try:
                    urls.append(p.evaluate("location.href"))
                except Exception:
                    pass

            if now - last_log_t >= 5:
                log.info("Waiting for login … (current: %s)", urls[-1] if urls else "?")
                last_log_t = now

            if any(not _is_login_page(u) for u in urls):
                logged_in = True
                log.info("Login detected ✓")
                try:
                    context.storage_state(path=auth_file)
                    log.info("Session saved to %s", auth_file)
                except Exception as exc:
                    log.warning("Failed to save session: %s", exc)
                    logged_in = False
                break

            time.sleep(0.5)

    except Exception as exc:
        log.debug("Login loop error: %s", exc)
    finally:
        signal.signal(signal.SIGINT, original_handler)

    if not logged_in and stop_flag:
        # Ctrl+C before login — try to save whatever we have
        try:
            context.storage_state(path=auth_file)
            log.info("Partial session saved to %s", auth_file)
        except Exception:
            pass

    return logged_in


# ── Article fetcher ───────────────────────────────────────────────────────────


def fetch_article_content(page: Page, article_url: str) -> str:
    """Open an X Article in a new tab and return its full body text."""
    try:
        log.info("Fetching article: %s", article_url)
        ap = page.context.new_page()
        ap.goto(article_url, wait_until="domcontentloaded", timeout=30_000)
        body = ap.query_selector('[data-testid="articleBody"]') or ap.query_selector("article")
        text = body.inner_text() if body else ""
        ap.close()
        return text.strip()
    except Exception as exc:
        log.warning("Failed to fetch article %s: %s", article_url, exc)
        return ""


# ── Bookmark Collection ──────────────────────────────────────────────────────


def collect_bookmarks(page: Page, max_scrolls: int, scroll_delay: float,
                      no_articles: bool) -> list[dict]:
    """
    Navigate to the bookmarks page and scroll-collect all tweets.
    Expects a page within an authenticated browser context.
    Handles Ctrl+C gracefully — returns whatever was collected so far.
    """
    bookmarks: list[dict] = []
    seen_ids: set[str] = set()

    log.info("Navigating to bookmarks …")
    page.goto("https://x.com/i/bookmarks", wait_until="domcontentloaded")
    time.sleep(2)

    # Check for login redirect
    try:
        real_url = page.evaluate("location.href")
    except Exception:
        real_url = page.url

    if _is_login_page(real_url):
        log.error("Session expired or invalid. Run without --headless to log in again.")
        return []

    try:
        page.wait_for_selector('article[data-testid="tweet"]', timeout=30_000)
    except PlaywrightTimeout:
        try:
            real_url = page.evaluate("location.href")
        except Exception:
            real_url = page.url
        if _is_login_page(real_url):
            log.error("Session expired. Run without --headless to log in again.")
        else:
            log.error("Timed out waiting for tweets. Current URL: %s", real_url)
        return []
    except Exception as exc:
        log.error("Browser error: %s", exc)
        log.error("Session may be expired. Run without --headless to log in again.")
        return []

    # ── Signal handler for graceful Ctrl+C during scrolling ───────────────
    stop_scrolling = False
    original_handler = signal.getsignal(signal.SIGINT)

    def _stop_handler(signum, frame):
        nonlocal stop_scrolling
        if stop_scrolling:
            raise SystemExit(1)
        stop_scrolling = True
        log.info("Ctrl+C — stopping scroll, processing %d collected bookmarks …", len(bookmarks))

    signal.signal(signal.SIGINT, _stop_handler)

    # ── Scroll and collect ────────────────────────────────────────────────
    try:
        log.info("Bookmarks loaded. Starting scroll collection (Ctrl+C to stop early) …")
        no_new_count = 0
        for scroll_idx in range(1, max_scrolls + 1):
            if stop_scrolling:
                break

            articles = page.query_selector_all('article[data-testid="tweet"]')
            new_this_round = 0
            for art in articles:
                try:
                    tweet = _parse_tweet(art)
                except Exception as exc:
                    log.debug("Skipping unparseable tweet: %s", exc)
                    continue
                tid = tweet["tweet_url"] or tweet["text"][:80]
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
                bookmarks.append(tweet)
                new_this_round += 1

            if new_this_round:
                no_new_count = 0
                log.info(
                    "Scroll %d/%d — %d new (%d total)",
                    scroll_idx, max_scrolls, new_this_round, len(bookmarks),
                )
            else:
                no_new_count += 1
                if no_new_count >= 5:
                    log.info("No new tweets for 5 scrolls — done.")
                    break

            page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            time.sleep(scroll_delay)

        # ── Fetch X Articles ──────────────────────────────────────────────
        if not no_articles and not stop_scrolling:
            article_bookmarks = [b for b in bookmarks if b["article_url"]]
            if article_bookmarks:
                log.info("Fetching %d X Article(s) …", len(article_bookmarks))
                for b in article_bookmarks:
                    b["article_text"] = fetch_article_content(page, b["article_url"])
                    time.sleep(1)

    finally:
        signal.signal(signal.SIGINT, original_handler)

    log.info("Finished — collected %d bookmarks.", len(bookmarks))
    return bookmarks


def _parse_tweet(art) -> dict:
    """Extract structured data from a single tweet article element."""
    handle_el = art.query_selector('a[role="link"][href*="/"]')
    handle = (handle_el.get_attribute("href") or "").strip("/") if handle_el else ""
    name_el = art.query_selector('div[data-testid="User-Name"] a span')
    name = name_el.inner_text() if name_el else ""
    text_el = art.query_selector('div[data-testid="tweetText"]')
    text = text_el.inner_text() if text_el else ""
    time_el = art.query_selector("time")
    ts = time_el.get_attribute("datetime") if time_el else ""
    tweet_url = time_el.evaluate("el => el.closest('a') ? el.closest('a').href : ''") if time_el else ""

    raw_urls: set[str] = set(re.findall(r"https?://[^\s\"'<>\)]+", text))
    article_url = ""

    for el in art.query_selector_all('a[href^="http"]'):
        href = el.get_attribute("href") or ""
        if is_article_url(href):
            if not article_url:
                article_url = href
        elif "t.co" in href or urlparse(href).netloc.lower() not in {"x.com", "twitter.com"}:
            raw_urls.add(href)

    # ── Image URLs ────────────────────────────────────────────────────────
    image_urls: list[str] = []
    for img_el in art.query_selector_all('div[data-testid="tweetPhoto"] img'):
        src = img_el.get_attribute("src") or ""
        if src and "pbs.twimg.com/media" in src:
            # Get the highest quality version
            clean = re.sub(r'[&?]name=\w+', '', src)
            image_urls.append(clean + "?name=orig")

    return {
        "timestamp":    ts,
        "author_name":  name,
        "author_handle": f"@{handle}" if handle else "",
        "text":         text.replace("\n", " ").strip(),
        "tweet_url":    tweet_url,
        "article_url":  article_url,
        "article_text": "",
        "image_urls":   image_urls,
        "urls_raw":     sorted(raw_urls),
    }


# ── Output ────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "timestamp", "author_name", "author_handle",
    "text", "tweet_url", "image_urls",
    "article_url", "article_text", "urls_expanded",
]


def _build_rows(bookmarks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Expand t.co links and return (csv_rows, jsonl_rows)."""
    all_tco = list({u for b in bookmarks for u in b["urls_raw"] if "t.co/" in u})
    log.info("Expanding %d unique t.co links …", len(all_tco))
    url_map = expand_urls_parallel(all_tco)
    log.info("Expanded %d / %d links.", len(url_map), len(all_tco))

    csv_rows, jsonl_rows = [], []
    for b in bookmarks:
        expanded = [url_map.get(u, u) for u in b["urls_raw"]]
        images = b.get("image_urls", [])
        base = {k: b[k] for k in ("timestamp", "author_name", "author_handle",
                                   "text", "tweet_url", "article_url", "article_text")}
        csv_rows.append({**base,
                         "image_urls": " | ".join(images),
                         "urls_expanded": " | ".join(expanded)})
        jsonl_rows.append({**base,
                           "image_urls": images,
                           "urls_expanded": expanded})
    return csv_rows, jsonl_rows


def save_output(bookmarks: list[dict], stem: str, fmt: str) -> None:
    """Expand URLs and write output files."""
    csv_rows, jsonl_rows = _build_rows(bookmarks)

    if fmt in ("csv", "both"):
        path = f"{stem}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            w.writerows(csv_rows)
        log.info("Saved → %s", path)

    if fmt in ("jsonl", "both"):
        path = f"{stem}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for row in jsonl_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        log.info("Saved → %s", path)


# ── CLI ───────────────────────────────────────────────────────────────────────


def main() -> None:
    p = argparse.ArgumentParser(
        description="Scrape Twitter/X bookmarks to CSV / JSONL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--save-auth-only", action="store_true",
                   help="Log in and save session, then exit without scraping")
    p.add_argument("--auth-file", default="twitter_auth.json",
                   help="Path to session state JSON (default: twitter_auth.json)")
    p.add_argument("--max-scrolls", type=int, default=100,
                   help="Max scroll attempts (default: 100)")
    p.add_argument("--scroll-delay", type=float, default=2.0,
                   help="Seconds between scrolls (default: 2.0)")
    p.add_argument("--output", default="bookmarks",
                   help="Output filename stem without extension (default: bookmarks)")
    p.add_argument("--format", dest="fmt", choices=["csv", "jsonl", "both"], default="csv",
                   help="Output format (default: csv)")
    p.add_argument("--headless", action="store_true",
                   help="Run browser headlessly (requires saved session)")
    p.add_argument("--no-articles", action="store_true",
                   help="Skip fetching full text of X native Articles")
    args = p.parse_args()

    has_session = os.path.exists(args.auth_file)
    need_login = not has_session and not args.headless

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            storage_state=args.auth_file if has_session else None,
            viewport={"width": 1280, "height": 900},
        )

        # ── Step 1: Login if needed ───────────────────────────────────────
        if need_login or args.save_auth_only:
            ok = interactive_login(context, args.auth_file)
            if not ok:
                log.error("Login failed or was cancelled.")
                context.close()
                browser.close()
                sys.exit(1)
            if args.save_auth_only:
                log.info("Session saved. Exiting (--save-auth-only).")
                context.close()
                browser.close()
                sys.exit(0)

        elif args.headless and not has_session:
            log.error("No session file found at '%s'.", args.auth_file)
            log.error("Run without --headless first to log in.")
            context.close()
            browser.close()
            sys.exit(1)

        # ── Step 2: Scrape bookmarks ──────────────────────────────────────
        page = context.pages[0] if context.pages else context.new_page()
        bookmarks = collect_bookmarks(
            page,
            max_scrolls=args.max_scrolls,
            scroll_delay=args.scroll_delay,
            no_articles=args.no_articles,
        )

        # ── Step 3: If scrape failed and we're not headless, try login ────
        if not bookmarks and not args.headless:
            log.info("Scrape failed — attempting interactive login …")
            ok = interactive_login(context, args.auth_file)
            if ok:
                page = context.pages[0] if context.pages else context.new_page()
                bookmarks = collect_bookmarks(
                    page,
                    max_scrolls=args.max_scrolls,
                    scroll_delay=args.scroll_delay,
                    no_articles=args.no_articles,
                )

        context.close()
        browser.close()

    if not bookmarks:
        log.warning("No bookmarks collected.")
        return

    stem = args.output.removesuffix(".csv").removesuffix(".jsonl")
    save_output(bookmarks, stem, args.fmt)


if __name__ == "__main__":
    main()