import os, json, requests, datetime, time
import concurrent.futures
import psycopg2 # For real DB connection
import hashlib
import hmac
import itertools
import random
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

# Load environment variables from .env.local
load_dotenv(".env.local")

# ==================================
# ⚠️ PROXY CONFIGURATION (LOCAL TESTING)
# ==================================
# IMPORTANT: Configure this to your local proxy client (e.g., Cloudflare WARP)
# Example for WARP SOCKS5 on port 40000:
# LOCAL_PROXY_SETTINGS = {"http": "socks5://127.0.0.1:40000", "https": "socks5://127.0.0.1:40000"}
LOCAL_PROXY_SETTINGS = None 

# Set headers for API calls
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}

# ==================================
# 🔧 CONFIGURATION & GLOBALS
# ==================================
# Pulling live values from the loaded environment
DATABASE_URL = os.getenv("DATABASE_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_GROUP_ID = os.getenv("TELEGRAM_GROUP_ID") 
PINCODES_STR = os.getenv("PINCODES_TO_CHECK", "110016") 
PINCODES_TO_CHECK = [p.strip() for p in PINCODES_STR.split(',') if p.strip()]

# --- Amazon PAAPI Credentials ---
AMAZON_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID")
AMAZON_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AMAZON_PARTNER_TAG = os.getenv("AMAZON_PARTNER_TAG")
AMAZON_HOST = "webservices.amazon.in"
AMAZON_REGION = "eu-west-1"
AMAZON_SERVICE = "ProductAdvertisingAPI"
AMAZON_ENDPOINT = "https://webservices.amazon.in/paapi5/getitems"


STORE_EMOJIS = {
    "croma": "🟢", "flipkart": "🟣", "amazon": "🟡", 
    "unicorn": "🦄", "iqoo": "📱", "vivo": "🤳", 
    "reliance_digital": "🌐", "vijay_sales": "🛍️"
}

# --- Load Topic IDs from environment variables ---
STORE_TOPIC_IDS = {
    "croma": os.getenv("CROMA_TOPIC_ID"),
    "flipkart": os.getenv("FLIPKART_TOPIC_ID"),
    "amazon": os.getenv("AMAZON_TOPIC_ID"),
    "unicorn": os.getenv("UNICORN_TOPIC_ID"),
    "iqoo": os.getenv("IQOO_TOPIC_ID"),
    "vivo": os.getenv("VIVO_TOPIC_ID"),
    "reliance_digital": os.getenv("RELIANCE_TOPIC_ID"),
    "vijay_sales": os.getenv("VIJAY_SALES_TOPIC_ID")
}

# ==================================
# 💬 TELEGRAM UTILITIES
# ==================================
def send_telegram_message(message, chat_id=TELEGRAM_GROUP_ID, thread_id=None):
    """Sends a single message to a specified chat ID and optional topic thread."""
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        print(f"[warn] Missing Telegram config for chat {chat_id}. Message was: {message[:50]}...")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    
    if thread_id:
        try:
            payload["message_thread_id"] = int(thread_id)
        except (ValueError, TypeError):
            print(f"[warn] Invalid thread_id: {thread_id}. Sending to main group.")

    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            print(f"[warn] Telegram send failed to chat {chat_id} (Thread: {thread_id}): {res.text}")
    except Exception as e:
        print(f"[error] Telegram message error to chat {chat_id} (Thread: {thread_id}): {e}")

# ==================================
# 🗄️ DATABASE (REAL CONNECTION)
# ==================================
def get_products_from_db():
    """Connects to the real database and fetches all tracked products."""
    print("[info] Connecting to database...")
    try:
        conn = psycopg2.connect(DATABASE_URL) 
        cursor = conn.cursor()
        # Fetching all required fields, including the new affiliate_link and part_number
        cursor.execute("SELECT name, url, product_id, store_type, affiliate_link, part_number FROM products")
        products = cursor.fetchall()
        conn.close()

        products_list = [
            {
                "name": row[0],
                "url": row[1],
                "productId": row[2],
                "storeType": row[3],
                "affiliateLink": row[4],
                "partNumber": row[5],
            }
            for row in products
        ]
        print(f"[info] Loaded {len(products_list)} products from database.")
        return products_list
    except Exception as e:
        print(f"[FATAL ERROR] Could not connect to or query database: {e}")
        print("Please check your DATABASE_URL in .env.local and connectivity.")
        return []

# ==================================
# 🔑 AMAZON V4 SIGNATURE HELPERS
# ==================================
def sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

def getSignatureKey(key, dateStamp, regionName, serviceName):
    kDate = sign(('AWS4' + key).encode('utf-8'), dateStamp)
    kRegion = sign(kDate, regionName)
    kService = sign(kRegion, serviceName)
    kSigning = sign(kService, 'aws4_request')
    return kSigning

# ==================================
# 🛒 STORE CHECKERS (API-ONLY)
# ==================================

# --- Flipkart Checker (Direct API Call) ---
def check_flipkart_product(product, pincode):
    """Checks stock for a single Flipkart product at one pincode using direct API."""
    
    API_URL = "https://2.rome.api.flipkart.com/api/3/product/serviceability"
    
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "Origin": "https://www.flipkart.com",
        "Referer": "https://www.flipkart.com",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N)",
        "X-User-Agent": "Mozilla/5.0 FKUA/msite/0.0.3/msite/Mobile",
        "flipkart_secure": "true",
        # Ensure Content-Type is set for POST requests
        "Content-Type": "application/json",
        "Accept": "application/json",
    })

    payload = {
        "requestContext": {
            "products": [{"productId": product["productId"]}],
            "marketplace": "FLIPKART"
        },
        "locationContext": {"pincode": pincode}
    }

    try:
        res = requests.post(
            API_URL, 
            headers=headers, 
            json=payload, 
            proxies=LOCAL_PROXY_SETTINGS, 
            timeout=20
        )
        res.raise_for_status()
        data = res.json()
        
        response = data.get("RESPONSE", {}).get(product["productId"], {})
        listing = response.get("listingSummary", {})
        available = listing.get("available", False)

        if available:
            price = listing.get("pricing", {}).get("finalPrice", {}).get("decimalValue", None)
            print(f"[FLIPKART] ✅ {product['name']} deliverable to {pincode}")
            return (
                f"[{product['name']}]({product['affiliateLink'] or product['url']})"
                f"\n📍 Pincode: {pincode}"
                + (f", 💰 Price: ₹{price}" if price else "")
            )

        print(f"[FLIPKART] ❌ {product['name']} not deliverable at {pincode}")
        return None

    except Exception as e:
        print(f"[error] Flipkart check failed (Proxy/Connection Error): {e}")
        return None

# --- Reliance Digital Checker (Direct API Call) ---
def check_reliance_digital_product(product, pincode):
    """Checks stock for a single Reliance Digital product at one pincode using direct API."""
    
    API_URL = "https://www.reliancedigital.in/ext/raven-api/inventory/multi/articles-v2"
    article_id = product["productId"]
    
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "Origin": "https://www.reliancedigital.in",
        "Referer": "https://www.reliancedigital.in/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json", # Mandatory for POST
        "Accept": "application/json", # Mandatory for API response
    })

    payload = {
        "articles": [
            {
                "article_id": str(article_id),
                "custom_json": {},
                "quantity": 1
            }
        ],
        "phone_number": "0",
        "pincode": str(pincode),
        "request_page": "pdp"
    }

    try:
        res = requests.post(
            API_URL, 
            headers=headers, 
            json=payload, 
            proxies=LOCAL_PROXY_SETTINGS, 
            timeout=20
        )
        res.raise_for_status()
        raw = res.json()
        
        # Extract stock status logic 
        article = raw.get("data", {}).get("articles", [{}])[0]
        error_type = article.get("error", {}).get("type")
        available = error_type is None # In stock when type is None

        if available:
            print(f"[RD] ✅ {product['name']} available at {pincode}")
            return (
                f"[{product['name']}]({product['affiliateLink'] or product['url']})"
                f"\n📍 Pincode: {pincode}"
            )

        print(f"[RD] ❌ {product['name']} unavailable at {pincode}")
        return None

    except Exception as e:
        print(f"[error] Reliance Digital check failed (Proxy/Connection Error): {e}")
        return None

# --- Amazon API Checker (PAAPI v5) ---
def check_amazon_api(product):
    """Checks Amazon stock using the direct PAAPI v5."""
    asin = product["productId"]

    if not all([AMAZON_ACCESS_KEY, AMAZON_SECRET_KEY, AMAZON_PARTNER_TAG]):
        print("[error] Amazon API credentials (KEY, SECRET, TAG) are not set. Skipping.")
        return None

    t = datetime.datetime.utcnow()
    amz_date = t.strftime('%Y%m%dT%H%M%SZ')
    date_stamp = t.strftime('%Y%m%d')

    payload = {
        "ItemIds": [asin],
        "PartnerTag": AMAZON_PARTNER_TAG,
        "PartnerType": "Associates",
        "Marketplace": "www.amazon.in",
        "Resources": [
            "OffersV2.Listings.Availability",
            "ItemInfo.Title"
        ]
    }
    payload_str = json.dumps(payload)

    method = 'POST'
    target = 'com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems'
    content_type = 'application/json; charset=UTF-8'
    
    canonical_headers = (
        f'content-type:{content_type}\n'
        f'host:{AMAZON_HOST}\n'
        f'x-amz-date:{amz_date}\n'
        f'x-amz-target:{target}\n'
    )
    signed_headers = 'content-type;host;x-amz-date;x-amz-target'
    payload_hash = hashlib.sha256(payload_str.encode('utf-8')).hexdigest()
    
    canonical_request = (
        f'{method}\n'
        '/paapi5/getitems\n'
        '\n'
        f'{canonical_headers}\n'
        f'{signed_headers}\n'
        f'{payload_hash}'
    )

    algorithm = 'AWS4-HMAC-SHA256'
    credential_scope = f'{date_stamp}/{AMAZON_REGION}/{AMAZON_SERVICE}/aws4_request'
    canonical_request_hash = hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()
    
    string_to_sign = (
        f'{algorithm}\n'
        f'{amz_date}\n'
        f'{credential_scope}\n'
        f'{canonical_request_hash}'
    )

    signing_key = getSignatureKey(AMAZON_SECRET_KEY, date_stamp, AMAZON_REGION, AMAZON_SERVICE)
    signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    authorization_header = (
        f'{algorithm} '
        f'Credential={AMAZON_ACCESS_KEY}/{credential_scope}, '
        f'SignedHeaders={signed_headers}, '
        f'Signature={signature}'
    )

    headers = {
        'Content-Type': content_type,
        'X-Amz-Date': amz_date,
        'X-Amz-Target': target,
        'Authorization': authorization_header,
        'Content-Encoding': 'amz-1.0',
        'Host': AMAZON_HOST
    }

    try:
        res = requests.post(AMAZON_ENDPOINT, data=payload_str, headers=headers, proxies=LOCAL_PROXY_SETTINGS, timeout=10)
        res.raise_for_status()
        data = res.json()

        item = data.get("ItemsResult", {}).get("Items", [{}])[0]
        listing = item.get("OffersV2", {}).get("Listings", [{}])[0]
        availability = listing.get("Availability", {})
        availability_type = availability.get("Type", "OUT_OF_STOCK")

        if availability_type == "IN_STOCK":
            product_title = item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue", product["name"])
            print(f"[AMAZON_API] ✅ {product_title} is IN STOCK")
            return (
                f"[{product_title}]({product['affiliateLink'] or product['url']})"
            )
        else:
            print(f"[AMAZON_API] ❌ {product['name']} is {availability_type}")
            return None

    except Exception as e:
        print(f"[error] Amazon API check failed for {asin}: {e}")
        return None

# --- Placeholder Checkers for other stores (Unicorn, Vijay Sales, Croma, iQOO, Vivo) ---
# NOTE: The logic for these is complex and relies on specific APIs/scraping. 
# We'll use a simple placeholder here to keep the core script runnable.

def check_other_store_product(product, pincode=None):
    """Placeholder for non-Flipkart/RD stores."""
    # You would replace this with the full logic from your original check.py for each store
    # For now, we'll assume they are out of stock.
    return None 

# ==================================
# 🗺️ STORE CHECKER MAP (FINAL)
# ==================================
STORE_CHECKERS_MAP = {
    "flipkart": check_flipkart_product,
    "reliance_digital": check_reliance_digital_product,
    "amazon": check_amazon_api,                
    
    # Use placeholder for others:
    "croma": check_other_store_product,
    "unicorn": check_other_store_product,                      
    "iqoo": check_other_store_product,                      
    "vivo": check_other_store_product,
    "vijay_sales": check_other_store_product,
}

# ==================================
# 🚀 CHECKER HELPERS (Adapted from api/check.py)
# ==================================
def check_store_products(store_type, products_to_check, pincodes):
    """Checks all products of a specific store type, running inner checks."""
    checker_func = STORE_CHECKERS_MAP.get(store_type)
    if not checker_func:
        return {"total": 0, "found": 0}

    messages_found = []
    
    # Logic for stores requiring pincode checks
    if store_type in ["croma", "flipkart", "reliance_digital", "vijay_sales"]:
        for product in products_to_check:
            # Add a small random delay before each request to be less aggressive
            time.sleep(random.uniform(1, 3))
            for pincode in pincodes:
                message = checker_func(product, pincode)
                if message:
                    messages_found.append(message)
                    break # Stop checking other pincodes once stock is found
    
    # Logic for stores that check against a single endpoint (Amazon, iQOO, Vivo, Unicorn)
    elif store_type in ["amazon", "iqoo", "vivo", "unicorn"]:
        for product in products_to_check:
             # Amazon is a single product check (no pincode needed)
            if store_type == "amazon":
                message = checker_func(product) 
            # Other single-point API stores need to be integrated fully.
            # Currently relying on placeholder:
            else:
                message = check_other_store_product(product)

            if message:
                messages_found.append(message)


    found_count = len(messages_found)
    
    if found_count > 0:
        header = f"🔥 *Stock Alert: {store_type.replace('_', ' ').title()}* {STORE_EMOJIS.get(store_type, '📦')}\n\n"
        full_message = header + "\n---\n".join(messages_found)
        
        thread_id = STORE_TOPIC_IDS.get(store_type)
        send_telegram_message(full_message, thread_id=thread_id)
    else:
        print(f"[STORE_SENDER] ❌ No stock found for {store_type.title()}. Skipping alert.")

    return {"total": len(products_to_check), "found": found_count}

def main_logic():
    start_time = time.time()
    print(f"[info] Starting local stock check with proxy: {LOCAL_PROXY_SETTINGS}")
    
    products = get_products_from_db()
    if not products:
        print("[info] Exiting main logic as no products were loaded.")
        return 

    # Separate products by store type
    products_by_store = {
        store_type: [p for p in products if p["storeType"] == store_type]
        for store_type in STORE_CHECKERS_MAP.keys()
    }
    
    # Initialize tracking
    total_tracked = sum(len(p_list) for p_list in products_by_store.values())
    tracked_stores = {store: {"total": len(products_by_store.get(store, [])), "found": 0} for store in STORE_CHECKERS_MAP.keys()}

    # Run checks concurrently
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_store = {}
        
        for store_type in STORE_CHECKERS_MAP.keys():
            if products_by_store.get(store_type):
                future = executor.submit(
                    check_store_products, 
                    store_type, 
                    products_by_store[store_type], 
                    PINCODES_TO_CHECK
                )
                future_to_store[future] = store_type

        # Collect results
        for future in concurrent.futures.as_completed(future_to_store):
            store_type = future_to_store[future]
            try:
                result = future.result()
                tracked_stores[store_type]["found"] = result.get("found", 0)
            except Exception as e:
                print(f"[ERROR] Concurrent check for {store_type} failed: {e}")

    # Compile final results
    total_found = sum(data['found'] for data in tracked_stores.values())
    duration = round(time.time() - start_time, 2)
    
    print("\n====================================")
    print(f"[INFO] ✅ Finished check in {duration}s.")
    print(f"Summary: Found {total_found}/{total_tracked} products available.")
    print("====================================\n")
    
    return duration

if __name__ == "__main__":
    MIN_DELAY = 30  # Minimum delay in seconds
    MAX_DELAY = 60  # Maximum delay in seconds
    
    print("--- Stock Checker Daemon Started ---")
    
    while True:
        try:
            # Run the main logic
            duration = main_logic()
            
            # Calculate sleep time
            sleep_duration = random.uniform(MIN_DELAY, MAX_DELAY)
            
            print(f"[INFO] Sleeping for {sleep_duration:.2f} seconds before next run...")
            time.sleep(sleep_duration)

        except KeyboardInterrupt:
            print("\n--- Stock Checker Stopped by User ---")
            break
        except Exception as e:
            print(f"[FATAL DAEMON ERROR] An unexpected error occurred: {e}")
            # Wait 5 minutes if there's a serious error, then retry
            print("[INFO] Waiting 5 minutes before restart attempt.")
            time.sleep(300)