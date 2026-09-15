import os
import json
import time
import random
import html
import threading
from datetime import datetime, timedelta, timezone
import dotenv
from flask import Flask
from curl_cffi import requests
from telegram_notifier import Notifier
from google import genai
from google.genai import types

# --- RENDER HEALTH-CHECK SERVER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "OLX Bot is active", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()
# ----------------------------------

SCRAPE_DELAY_MIN = 5
SCRAPE_DELAY_MAX = 10
REPEAT_DELAY = 600
MAX_AGE_DAYS = 14  # Cutoff for old listings

dotenv.load_dotenv()
notifier = Notifier()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

try:
    with open("shown_ids.json", "r") as f:
        shown_ids_list = json.loads(f.read())
        shown_ids = set(str(x) for x in shown_ids_list)
except Exception as e:
    print(f"[INIT] Warning: Could not read shown_ids.json ({e}). Initializing empty cache.", flush=True)
    shown_ids = set()

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Cache-Control': 'max-age=0'
}

def enforce_newest_sort(url: str) -> str:
    """Ensures search[order]=created_at:desc is appended to the OLX URL."""
    if "search%5Border%5D=" in url or "search[order]=" in url:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}search%5Border%5D=created_at%3Adesc"

def is_listing_too_old(offer: dict, max_days=MAX_AGE_DAYS) -> bool:
    """Checks created_time or pushup_time to skip listings older than MAX_AGE_DAYS."""
    raw_time = offer.get("created_time") or offer.get("pushup_time") or offer.get("created_at")
    if not raw_time:
        return False

    try:
        if isinstance(raw_time, str):
            created_dt = datetime.fromisoformat(raw_time)
        elif isinstance(raw_time, (int, float)):
            created_dt = datetime.fromtimestamp(raw_time, tz=timezone.utc)
        else:
            return False

        cutoff_date = datetime.now(created_dt.tzinfo) - timedelta(days=max_days)
        return created_dt < cutoff_date
    except Exception:
        return False

def analyze_listing_with_gemini(title: str, price: float, description: str = "") -> dict:
    """Uses Gemini 3.5 Flash-Lite with rigorous guitar valuation constraints."""
    if not gemini_client:
        return {"bargain_rating": 5, "discount_percentage": 0, "verdict": "Gemini client uninitialized"}

    prompt = f"""
    Analyze this OLX guitar listing to determine if it is a genuine, massive price anomaly or mispriced deal in the Polish used guitar market.

    Listing Title: {title}
    Listing Price: {price} PLN
    Description: {description}

    CRITICAL MARKET RULES:
    1. EXACT MODEL & SUB-SERIES IDENTIFICATION:
       - Distinguish budget/import lines from flagship/US/Japan lines carefully:
         * Ibanez: AZ Standard/AZS (2000-2500 PLN used) vs AZ Premium (3500-4500 PLN) vs AZ Prestige (5500-7000+ PLN).
         * G&L: Tribute Series (1800-2500 PLN used) vs USA Fullerton (5000+ PLN).
         * Gibson: Les Paul Faded/Tribute/Studio (3000-4500 PLN used) vs Standard/Traditional (7000+ PLN).
         * Fender: Squier / Player (1500-2500 PLN) vs American Professional (5500-7500 PLN).
    2. AMBIGUOUS TITLES DEFAULT RULE:
       - If a listing title lacks specific tier indicators (e.g. simply "G&L Stratocaster" or "Ibanez AZ"), default your baseline market value calculation to the LOWER-TIER/IMPORT model unless the description explicitly proves American/Prestige origin.
    3. ACCURATE DISCOUNT PERCENTAGE:
       - Calculate percentage discount strictly against typical Polish USED market value in PLN, NOT original retail MSRP.

    Return a valid JSON object ONLY with this exact schema:
    {{
        "bargain_rating": integer (1 to 10),
        "discount_percentage": integer or float,
        "verdict": "Short explanation specifying exact detected sub-series, realistic used market value in PLN, and discount rationale."
    }}
    """
    try:
        response = gemini_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"    [GEMINI ERROR] Failed to analyze listing: {e}", flush=True)
        return {"bargain_rating": 5, "discount_percentage": 0, "verdict": "API check skipped"}

INITIAL_RUN = True
print("[INIT] Starting scraper loop with HTML notification formatting...", flush=True)

while True:
    print("\n--- Starting new OLX scrape cycle ---", flush=True)

    try:
        with open("searches.json", "r") as f:
            searches = json.loads(f.read())
    except Exception as e:
        print(f"[ERROR] Failed to read searches.json: {e}", flush=True)
        time.sleep(REPEAT_DELAY)
        continue

    for index, search in enumerate(searches, start=1):
        raw_url = search.get('url')
        if not raw_url:
            continue

        url = enforce_newest_sort(raw_url)
        min_price = search.get('min_price', 0)
        max_price = search.get('max_price', 999999)
        required_keyword = search.get('required_keyword', '')

        print(f"[{index}/{len(searches)}] Fetching: {url}", flush=True)

        try:
            response = requests.get(url, headers=headers, impersonate="chrome120", timeout=15)
            print(f"    -> Response Status: {response.status_code}", flush=True)

            if response.status_code != 200:
                print(f"    [WARN] Skipped URL due to non-200 status code ({response.status_code})", flush=True)
                continue

            data = response.text
            start_tag = '__PRERENDERED_STATE__= "'
            end_tag = 'window.__TAURUS__'

            if start_tag not in data or end_tag not in data:
                print("    [WARN] Pre-rendered state JSON tags not found in response DOM.", flush=True)
                continue

            data = data[data.find(start_tag) + len(start_tag):]
            data = data[:data.find(end_tag)]
            data = data[:data.rfind('";')]
            data = data.encode().decode('unicode_escape')

            parsed_json = json.loads(data)
            offers = parsed_json.get("listing", {}).get("listing", {}).get("ads", [])

            matches_found = 0
            for offer in offers:
                if not isinstance(offer, dict):
                    continue

                offer_id = str(offer.get('id', ''))

                if offer_id in shown_ids:
                    continue

                if is_listing_too_old(offer):
                    shown_ids.add(offer_id)
                    continue

                title = offer.get('title', '')
                offer_url = offer.get('url', '')

                price_obj = offer.get("price") or {}
                regular_price = price_obj.get("regularPrice") or {}
                price = regular_price.get("value", 0)

                if not price:
                    continue

                if required_keyword and required_keyword.lower() not in title.lower():
                    continue
                if price < min_price or price > max_price:
                    continue

                if INITIAL_RUN:
                    shown_ids.add(offer_id)
                    print(f"    [SEEDING] Cached existing listing: {title} ({price} PLN)", flush=True)
                    continue

                print(f"    [AI ANALYZING] Checking misprice anomaly: {title}", flush=True)
                analysis = analyze_listing_with_gemini(title, price)

                rating = analysis.get('bargain_rating', 0)
                discount = analysis.get('discount_percentage', 0)
                verdict = analysis.get('verdict', '')

                if rating < 7 or discount < 30:
                    print(f"    [SKIPPED] Not a heavy bargain — Rating: {rating}/10 | Discount: {discount}%", flush=True)
                    continue

                matches_found += 1
                log_line = f"    [MATCH FOUND] {title} | {price} PLN | Rating: {rating}/10 | Discount: {discount}% | {offer_url}"
                print(log_line, flush=True)

                # Safe HTML escaping for dynamic string injection
                safe_title = html.escape(title)
                safe_verdict = html.escape(str(verdict))
                safe_url = html.escape(offer_url)

                message = (
                    f"🚨 <b>BARGAIN ALERT</b>: {safe_title}\n\n"
                    f"<b>Price</b>: {price} PLN\n"
                    f"<b>AI Rating</b>: {rating}/10 | <b>Discount</b>: {discount}%\n"
                    f"<b>Details</b>: {safe_verdict}\n\n"
                    f'<a href="{safe_url}">View Listing on OLX</a>'
                )

                try:
                    notifier.send_message(message)
                    # Only mark listing cached once successfully delivered
                    shown_ids.add(offer_id)
                except Exception as send_err:
                    print(f"    [TELEGRAM ERROR] Could not deliver alert: {send_err}", flush=True)

            with open("shown_ids.json", "w") as f:
                f.write(json.dumps(list(shown_ids)))

            if matches_found == 0 and not INITIAL_RUN:
                print("    -> Parsed successfully (No new matching listings).", flush=True)

        except Exception as err:
            print(f"    [ERROR] Exception processing URL: {err}", flush=True)

        time.sleep(random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX))

    INITIAL_RUN = False
    print(f"--- Cycle finished. Sleeping for {REPEAT_DELAY}s before next scan ---", flush=True)
    time.sleep(REPEAT_DELAY)
