# 🔖 Twitter / X Bookmark Scraper

A Python CLI tool that uses Playwright to scrape all your Twitter/X bookmarks, expand `t.co` short links, fetch full text of **X native Articles**, capture **image URLs**, and export everything to **CSV** and/or **JSONL**.

No Twitter API key required. No credentials stored in code.

---

## Features

- ✅ **Unified flow**: Automatically prompts for login if session is missing or expired, then scrapes immediately.
- ✅ **Zero-interaction mode**: Headless support for scheduled/server runs (after initial login).
- ✅ **Graceful interruption**: Press `Ctrl+C` once to stop scrolling and save what has been collected so far.
- ✅ **Rich extraction**:
    - Expands `t.co` short links in parallel.
    - Fetches full body text of **X native Articles**.
    - Captures high-res **image URLs** (`?name=orig`).
    - Captures URLs from tweet text, link cards, and quoted tweets.
- ✅ **Flexible output**: CSV and/or JSONL formats with configurable scroll depth and delays.

---

## Requirements

- Python 3.12+
- Google Chrome installed

---

## Setup

### 1. Clone the repo

```bash
```bash
git clone https://github.com/your-username/twitter-bookmark-scraper.git
cd twitter-bookmark-scraper
```

### 2. Create a virtual environment and install dependencies

Using `uv` (recommended):

```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
playwright install chromium
```

Using plain `pip`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

> **Note:** `playwright install chromium` downloads the browser binaries. It must be run once after installing the package and is separate from `requirements.txt`.

---

## Quick Start

### The Easy Way (Login + Scrape)

Just run the script. If you aren't logged in, it will open a browser for you. Once you log in, it saves your session and starts scraping immediately:

```bash
python twitter_bookmark_scrapper.py
```

### Headless Mode (Server / Automation)

Once you have a valid `twitter_auth.json` from a previous run, you can run completely in the background:

```bash
python twitter_bookmark_scrapper.py --headless
```

---

## Usage

```bash
python twitter_bookmark_scrapper.py [OPTIONS]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--save-auth-only` | — | Login and save session, then exit without scraping |
| `--auth-file FILE` | `twitter_auth.json` | Path to session state JSON |
| `--max-scrolls N` | `100` | Max scroll attempts on the bookmarks page |
| `--scroll-delay S` | `2.0` | Seconds to wait between scrolls |
| `--output STEM` | `bookmarks` | Output filename without extension |
| `--format FORMAT` | `csv` | `csv`, `jsonl`, or `both` |
| `--headless` | off | Run browser headlessly (requires saved session) |
| `--no-articles` | off | Skip fetching full text of X native Articles |

### Examples

```bash
# Export to both CSV and JSONL
python twitter_bookmark_scrapper.py --format both

# Custom output name, faster scrolling, headless
python twitter_bookmark_scrapper.py --headless --output my_bookmarks --scroll-delay 1.0

# Skip article fetching
python twitter_bookmark_scrapper.py --no-articles

# Save session for a specific account without scraping
python twitter_bookmark_scrapper.py --save-auth-only --auth-file personal.json
```

---

## Output Format

### CSV columns

| Column | Description |
|---|---|
| `timestamp` | Tweet post time (ISO 8601) |
| `author_name` | Display name of the author |
| `author_handle` | `@username` |
| `text` | Full tweet text |
| `tweet_url` | Permalink to the tweet |
| `image_urls` | `\|`-separated list of high-res image URLs |
| `article_url` | URL of the X native Article (if present) |
| `article_text` | Full body text of the X Article (if fetched) |
| `urls_expanded` | `\|`-separated list of expanded external URLs |

### JSONL record

Each line is a JSON object. Lists (images/URLs) are stored as real JSON arrays.

```json
{
  "timestamp": "2024-11-01T10:23:00.000Z",
  "author_name": "Jane Doe",
  "author_handle": "@janedoe",
  "text": "Check out this deep-dive ...",
  "tweet_url": "https://x.com/janedoe/status/123456789",
  "image_urls": ["https://pbs.twimg.com/media/abc.jpg?name=orig"],
  "article_url": "https://x.com/janedoe/articles/987654321",
  "article_text": "Full article body text ...",
  "urls_expanded": ["https://github.com/some/repo"]
}
```

---

## How It Works

1.  **Session Management**:
    *   If no session file (default `twitter_auth.json`) exists, it opens a non-headless browser.
    *   It waits for you to log in, detecting success by watching for URL changes to non-login pages.
    *   Sessions are saved every 3 seconds to ensure `Ctrl+C` during login doesn't lose progress.
2.  **Scraping**:
    *   Navigates to `x.com/i/bookmarks`.
    *   Scrolls the page, parsing tweets as they appear.
    *   **Graceful Exit**: If you press `Ctrl+C` while scrolling, it stops and processes everything collected up to that point.
3.  **Expansion**:
    *   Extracts `t.co` links, links from "Cards", and quoted tweets.
    *   Opens X Articles in separate tabs to grab the full body text.
    *   Resolves all `t.co` redirects in parallel via HTTP HEAD requests.

---

## Caveats

- **X UI changes** — Twitter/X updates its frontend regularly. If scraping breaks, the CSS selectors in `_parse_tweet` and `fetch_article_content` may need updating.
- **Rate limiting** — Scrolling too fast may trigger Twitter's rate limits. Increase `--scroll-delay` if tweets stop loading mid-scroll.
- **Duplicate Detection** — The script avoids duplicates in a single run using tweet IDs. Repeated runs with the same output filename will overwrite the file.

---

## License

MIT
