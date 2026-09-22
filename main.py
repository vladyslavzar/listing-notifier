import os
import json
import time
import random
import html
import re
import itertools
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
import dotenv
from flask import Flask
from curl_cffi import requests
from telegram_notifier import Notifier
from google import genai
from google.genai import types
from google.genai.errors import APIError

# --- CONFIGURATION ---
POLL_INTERVAL = 50  # Real-time event loop runs every 50 seconds
SCRAPE_DELAY = 2    # Short pause between search queries
MIN_DISCOUNT_THRESHOLD = 30.0  # Require >= 30% discount to fire alert
MIN_SAVINGS_PLN = 300          # Require at least 300 PLN profit margin
UAH_TO_PLN_RATE = 0.10         # Currency conversion (1 UAH ~ 0.10 PLN)

# --- RENDER HEALTH-CHECK SERVER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "OLX Real-Time Monitor is active", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()
# ----------------------------------

dotenv.load_dotenv()
notifier = Notifier()

# --- GEMINI MULTI-KEY SETUP ---
raw_keys = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]

if api_keys:
    key_pool = itertools.cycle(api_keys)
    print(f"[INIT] Loaded {len(api_keys)} Gemini API key(s) into rotation.", flush=True)
else:
    key_pool = None
    print("[INIT] Warning: No Gemini API keys configured.", flush=True)

# --- IN-MEMORY RECENT ID CACHE ---
seen_ids = set()
recent_id_queue = deque(maxlen=3000)

def track_id(listing_id: str):
    """Adds ID to seen set with rolling deque eviction."""
    if len(recent_id_queue) >= 3000:
        oldest = recent_id_queue.popleft()
        seen_ids.discard(oldest)
    seen_ids.add(listing_id)
    recent_id_queue.append(listing_id)

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'pl-PL,pl;q=0.9,uk-UA;q=0.8,en;q=0.7',
    'Connection': 'keep-alive',
    'Cache-Control': 'max-age=0'
}

def clean_description(raw_desc: str) -> str:
    """Strips HTML tags and normalizes whitespace."""
    if not raw_desc:
        return ""
    clean_text = re.sub(r'<[^>]+>', ' ', str(raw_desc))
    return ' '.join(clean_text.split())

def enforce_newest_sort(url: str) -> str:
    """Ensures search[order]=created_at:desc is appended to the OLX query."""
    if "search%5Border%5D=" in url or "search[order]=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}search%5Border%5D=created_at%3Adesc"

def estimate_market_value_gemini(title: str, price_pln: float, description: str = "") -> dict:
    """Asks Gemini 2.5 Flash-Lite ONLY for estimated Polish secondhand market value in PLN."""
    if not key_pool:
        return {"estimated_market_value_pln": 0, "reasoning": "Gemini uninitialized"}

    prompt = f"""
    Act as an expert secondhand electric guitar valuer in Poland (OLX/Allegro market).

    Listing Title: {title}
    Listed Price Equivalent: {price_pln:.0f} PLN
    Description: {description[:800]}

    TASK:
    Identify the exact model/tier and estimate the realistic secondhand market value in Poland in PLN.
    - Differentiate tiers carefully (e.g. Squier vs Player vs Am Pro; Tribute vs USA; AZ Standard vs Prestige).
    - Base calculation on typical USED Polish market values in PLN, NOT MSRP.
    - If title is ambiguous, default to lower-tier/import value unless description proves higher tier.

    Return valid JSON ONLY matching this schema:
    {{
        "estimated_market_value_pln": integer,
        "reasoning": "Short 1-2 sentence valuation summary specifying identified tier and typical PLN secondhand value."
    }}
    """

    for _ in range(len(api_keys)):
        current_key = next(key_pool)
        try:
            client = genai.Client(api_key=current_key)
            response = client.models.generate_content(
                model='gemini-2.5-flash-lite',
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1
                ),
            )
            return json.loads(response.text)
        except APIError as e:
            if getattr(e, 'code', None) == 429 or "429" in str(e):
                print(f"    [GEMINI 429] Key exhaustion. Rotating key...", flush=True)
                continue
            print(f"    [GEMINI API ERROR] {e}", flush=True)
            break
        except Exception as e:
            print(f"    [GEMINI ERROR] Unexpected error: {e}", flush=True)
            break

    return {"estimated_market_value_pln": 0, "reasoning": "All Gemini API keys failed or rate-limited"}

def fetch_page_one_offers(search_config: dict) -> list:
    """Fetches Page 1 of search result listings and returns parsed items."""
    raw_url = search_config.get('url')
    if not raw_url:
        return []

    url = enforce_newest_sort(raw_url)
    market = search_config.get('market', 'PL').upper()
    min_price = search_config.get('min_price', 0)
    max_price = search_config.get('max_price', 999999)
    required_keyword = search_config.get('required_keyword', '').lower()

    try:
        response = requests.get(url, headers=headers, impersonate="chrome120", timeout=12)
        if response.status_code != 200:
            return []

        data = response.text
        start_tag = '__PRERENDERED_STATE__= "'
        end_tag = 'window.__TAURUS__'

        if start_tag not in data or end_tag not in data:
            return []

        data = data[data.find(start_tag) + len(start_tag):]
        data = data[:data.find(end_tag)]
        data = data[:data.rfind('";')]
        data = data.encode().decode('unicode_escape')

        parsed_json = json.loads(data)
        offers = parsed_json.get("listing", {}).get("listing", {}).get("ads", [])

        parsed_items = []
        for offer in offers:
            if not isinstance(offer, dict):
                continue

            offer_id = f"{market}_{offer.get('id', '')}"
            title = offer.get('title', '')
            offer_url = offer.get('url', '')

            raw_description = offer.get('description') or offer.get('snippet') or offer.get('textContent') or ''
            description = clean_description(raw_description)

            price_obj = offer.get("price") or {}
            regular_price = price_obj.get("regularPrice") or {}
            raw_price = regular_price.get("value", 0)

            if not raw_price or raw_price <= 0:
                continue

            # Convert UAH to PLN equivalent if market is Ukraine
            if market == "UA":
                price_pln = raw_price * UAH_TO_PLN_RATE
                orig_price_str = f"{int(raw_price)} UAH"
            else:
                price_pln = float(raw_price)
                orig_price_str = f"{int(raw_price)} PLN"

            if required_keyword and required_keyword not in title.lower():
                continue
            if raw_price < min_price or raw_price > max_price:
                continue

            parsed_items.append({
                "id": offer_id,
                "title": title,
                "price_pln": price_pln,
                "orig_price_str": orig_price_str,
                "description": description,
                "url": offer_url,
                "market": "🇺🇦 OLX.ua" if market == "UA" else "🇵🇱 OLX.pl"
            })

        return parsed_items

    except Exception as err:
        print(f"    [FETCH ERROR] Failed reading search endpoint: {err}", flush=True)
        return []

def cold_start():
    """Populates seen_ids with current Page 1 listings upon startup without firing alerts."""
    print("[COLD START] Caching active Page 1 listings across all configured searches...", flush=True)
    try:
        with open("searches.json", "r") as f:
            searches = json.loads(f.read())
    except Exception as e:
        print(f"[COLD START ERROR] Could not read searches.json: {e}", flush=True)
        return

    cached_count = 0
    for search in searches:
        items = fetch_page_one_offers(search)
        for item in items:
            track_id(item["id"])
            cached_count += 1

    print(f"[COLD START COMPLETE] Pre-cached {cached_count} existing listing IDs. Ready for real-time events.\n", flush=True)

# --- MAIN REAL-TIME NOTIFIER LOOP ---
cold_start()

while True:
    try:
        with open("searches.json", "r") as f:
            searches = json.loads(f.read())
    except Exception as e:
        print(f"[ERROR] Could not read searches.json: {e}", flush=True)
        time.sleep(POLL_INTERVAL)
        continue

    for search in searches:
        latest_items = fetch_page_one_offers(search)

        for item in latest_items:
            item_id = item["id"]

            # Early exit: Stop evaluating as soon as an already processed ID is encountered
            if item_id in seen_ids:
                break

            # Instantly mark ID as seen
            track_id(item_id)

            title = item["title"]
            price_pln = item["price_pln"]

            print(f"[NEW LISTING DETECTED] ({item['market']}) {title} @ ~{int(price_pln)} PLN", flush=True)

            # Query Gemini for valuation estimate only
            analysis = estimate_market_value_gemini(title, price_pln, item["description"])
            estimated_val = analysis.get("estimated_market_value_pln", 0)
            reasoning = analysis.get("reasoning", "")

            if estimated_val > price_pln and estimated_val > 0:
                discount_pct = ((estimated_val - price_pln) / estimated_val) * 100
                savings_pln = estimated_val - price_pln

                print(f"    -> Valuation: ~{estimated_val} PLN | Discount: {discount_pct:.1f}% | Profit: ~{int(savings_pln)} PLN", flush=True)

                # Pure Python threshold validation
                if discount_pct >= MIN_DISCOUNT_THRESHOLD and savings_pln >= MIN_SAVINGS_PLN:
                    safe_title = html.escape(title)
                    safe_reasoning = html.escape(str(reasoning))
                    safe_url = html.escape(item["url"])

                    message = (
                        f"🚨 <b>BARGAIN ALERT — {item['market']}</b> ({discount_pct:.1f}% OFF)\n\n"
                        f"🎸 <b>Title:</b> {safe_title}\n"
                        f"💰 <b>Listed Price:</b> ~{int(price_pln)} PLN ({item['orig_price_str']})\n"
                        f"📈 <b>Est. Market Value:</b> ~{int(estimated_val)} PLN\n"
                        f"💵 <b>Est. Margin:</b> ~{int(savings_pln)} PLN\n\n"
                        f"💡 <b>Reasoning:</b> {safe_reasoning}\n\n"
                        f'<a href="{safe_url}">View Listing on OLX</a>'
                    )

                    try:
                        notifier.send_message(message)
                        print(f"    [ALERT DISPATCHED] Sent Telegram notification for {title}", flush=True)
                    except Exception as send_err:
                        print(f"    [TELEGRAM ERROR] Failed sending alert: {send_err}", flush=True)

        time.sleep(SCRAPE_DELAY)

    time.sleep(POLL_INTERVAL)
