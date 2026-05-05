#!/usr/bin/env python3
"""
set_filing_monitor.py
────────────────────────────────────────────────────────────────────
Checks SET/SEC for new quarterly financial statements and MD&A reports
for every stock in set_watchlist.json.

Sends a LINE notification with a direct link when a new filing is found.
State is saved in set_filing_state.json to avoid duplicate alerts.

Run schedule: 2× daily (9 AM and 6 PM Bangkok time) via GitHub Actions.
"""

import os, sys, json, datetime, urllib.request, urllib.parse, time

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH  = os.path.join(SCRIPT_DIR, "set_config.json")
STATE_PATH   = os.path.join(SCRIPT_DIR, "set_filing_state.json")
WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "set_watchlist.json")
SIGNAL_PATH  = os.path.join(SCRIPT_DIR, "set_signal_state.json")

BKK = datetime.timezone(datetime.timedelta(hours=7))
def now_bkk():
    return datetime.datetime.now(BKK)

with open(CONFIG_PATH) as f:
    cfg = json.load(f)

LINE_TOKEN   = os.environ.get("LINE_TOKEN",   cfg.get("line_channel_access_token", ""))
LINE_USER_ID = os.environ.get("LINE_USER_ID", cfg.get("line_user_id", ""))

# ── Filing keyword filters ────────────────────────────────────────────────────
# Match SET news headlines that indicate a financial filing
FILING_KEYWORDS = [
    "งบการเงิน",          # financial statements
    "financial statement",
    "quarterly",
    "ไตรมาส",             # quarter
    "MD&A",
    "คำอธิบายและการวิเคราะห์",  # explanation and analysis
    "สอบทาน",             # reviewed (by auditor)
    "ตรวจสอบ",            # audited
    "annual report",
    "56-1",
    "one report",
    "งบปี",               # annual financial
]

# News types that indicate financial filings on SET
FILING_NEWS_TYPES = {"FS", "MD", "AR", "56"}  # Financial Statement, MD&A, Annual Report


def send_line(msg):
    try:
        body = json.dumps({
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": msg}]
        }).encode()
        req = urllib.request.Request(
            "https://api.line.me/v2/bot/message/push",
            data=body,
            headers={"Authorization": f"Bearer {LINE_TOKEN}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"[LINE] {'✅ Sent' if r.status == 200 else f'❌ {r.status}'}")
    except Exception as e:
        print(f"[LINE] ❌ {e}")


def fetch_json(url, headers=None):
    h = {"User-Agent": "Mozilla/5.0 (compatible; SETBot/1.0)",
         "Accept": "application/json",
         "Referer": "https://www.set.or.th/"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get_stock_symbol(name):
    """Convert display name to ticker symbol (strip .BK suffix for SET API)."""
    # Try signal state first
    if os.path.exists(SIGNAL_PATH):
        with open(SIGNAL_PATH) as f:
            state = json.load(f)
        n = name.upper()
        for ticker, s in state.items():
            if ticker.startswith("_"):
                continue
            if s.get("name", "").upper() == n or ticker.upper() == n or ticker.upper() == n + ".BK":
                # Return the ticker without .BK suffix for SET API
                return ticker.replace(".BK", ""), ticker
    # Fallback: use name as-is
    return name.upper(), name.upper() + ".BK"


def fetch_set_news(symbol_no_suffix, limit=20):
    """Fetch recent company news from SET portal API."""
    url = (f"https://www.set.or.th/api/set/news/search"
           f"?symbol={urllib.parse.quote(symbol_no_suffix)}"
           f"&newsType=C&lang=en&limit={limit}&page=1")
    try:
        data = fetch_json(url)
        return data.get("newsInfoList", data.get("newsList", []))
    except Exception as e:
        print(f"  [SET API] {symbol_no_suffix}: {e}")
        return []


def fetch_sec_filings(symbol_no_suffix, limit=10):
    """Fetch financial filings from SEC iDISC as fallback."""
    url = (f"https://market.sec.or.th/public/idisc/en/Newsroom/FS"
           f"?keyword={urllib.parse.quote(symbol_no_suffix)}&limit={limit}")
    try:
        data = fetch_json(url)
        items = data.get("data", data.get("items", []))
        return items
    except Exception as e:
        print(f"  [SEC API] {symbol_no_suffix}: {e}")
        return []


def is_filing(news_item):
    """Return True if this news item looks like a financial filing."""
    # Check news category/type
    news_type = (news_item.get("newsType", "") or
                 news_item.get("type", "") or
                 news_item.get("category", "")).upper()
    if any(ft in news_type for ft in FILING_NEWS_TYPES):
        return True

    # Check headline keywords
    headline = ((news_item.get("headline", "") or
                 news_item.get("title", "") or
                 news_item.get("subject", "")) + " " +
                (news_item.get("newsType", "") or "")).lower()
    if any(kw.lower() in headline for kw in FILING_KEYWORDS):
        return True

    return False


def make_filing_url(news_item):
    """Build a direct link to the filing on SET portal."""
    news_id = (news_item.get("newsId") or
               news_item.get("id") or
               news_item.get("newsNo", ""))
    if news_id:
        return f"https://www.set.or.th/en/market/news-and-alert/news/{news_id}"
    # Fallback: link to company news page
    symbol = news_item.get("symbol", "")
    if symbol:
        return f"https://www.set.or.th/en/market/product/stock/quote/{symbol}/news"
    return "https://www.set.or.th/en/market/news-and-alert/news"


def format_filing_message(stock_name, news_item):
    """Format a LINE notification for a new filing."""
    headline = (news_item.get("headline") or
                news_item.get("title") or
                news_item.get("subject") or
                "New filing")
    news_date = (news_item.get("newsDatetime") or
                 news_item.get("datetime") or
                 news_item.get("date") or "")
    if news_date and "T" in news_date:
        news_date = news_date[:10]  # keep date only

    url = make_filing_url(news_item)

    lines = [
        f"📄 New Filing: {stock_name}",
        f"",
        f"📋 {headline}",
    ]
    if news_date:
        lines.append(f"📅 {news_date}")
    lines.append(f"")
    lines.append(f"🔗 {url}")
    lines.append(f"")
    lines.append(f"Tap the link to view the full report.")
    return "\n".join(lines)


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"seen_ids": {}, "last_checked": ""}


def save_state(state):
    state["last_checked"] = now_bkk().isoformat()
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def main():
    # Load watchlist
    if not os.path.exists(WATCHLIST_PATH):
        print("No watchlist found — nothing to monitor.")
        return

    with open(WATCHLIST_PATH) as f:
        wl = json.load(f)

    stocks = wl.get("stocks", [])
    if not stocks:
        print("Watchlist is empty.")
        return

    print(f"Checking filings for {len(stocks)} watchlist stock(s)...")

    state = load_state()
    seen_ids = state.get("seen_ids", {})
    new_filings_found = 0

    for stock_name in stocks:
        symbol, full_ticker = get_stock_symbol(stock_name)
        print(f"\n  {stock_name} ({symbol})")

        if stock_name not in seen_ids:
            seen_ids[stock_name] = []

        # Try SET news API first
        news_items = fetch_set_news(symbol)

        # Fallback to SEC if SET returned nothing
        if not news_items:
            news_items = fetch_sec_filings(symbol)

        if not news_items:
            print(f"    No news returned.")
            time.sleep(0.5)
            continue

        for item in news_items:
            item_id = str(item.get("newsId") or item.get("id") or item.get("newsNo") or "")
            if not item_id:
                continue
            if item_id in seen_ids[stock_name]:
                continue
            if not is_filing(item):
                continue

            # New filing found!
            print(f"    ✅ New filing: {item.get('headline', item.get('title', ''))}")
            msg = format_filing_message(stock_name, item)
            send_line(msg)
            seen_ids[stock_name].append(item_id)
            new_filings_found += 1
            time.sleep(0.3)

        # On first run: seed state with current IDs to avoid flooding old filings
        if len(seen_ids[stock_name]) == 0:
            print(f"    First run — seeding {len(news_items)} existing item IDs")
            for item in news_items:
                item_id = str(item.get("newsId") or item.get("id") or item.get("newsNo") or "")
                if item_id:
                    seen_ids[stock_name].append(item_id)

        time.sleep(0.5)  # be polite to the API

    state["seen_ids"] = seen_ids
    save_state(state)

    print(f"\nDone. {new_filings_found} new filing(s) notified.")


if __name__ == "__main__":
    main()
