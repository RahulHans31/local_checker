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
        # Fetching all required fields
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
        "Content-Type": "application/json",
        "Accept": "application/json",
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
        
        article = raw.get("data", {}).get("articles", [{}])[0]
        error_type = article.get("error", {}).get("type")
        available = error_type is None

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

# --- Croma Checker (API) ---
def check_croma_product(product, pincode):
    """Checks stock for a single Croma product at one pincode."""
    url = "https://api.croma.com/inventory/oms/v2/tms/details-pwa/"
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "oms-apim-subscription-key": "1131858141634e2abe2efb2b3a2a2a5d",
        "origin": "https://www.croma.com",
        "referer": "https://www.croma.com/",
    })

    payload = {
        "promise": {
            "allocationRuleID": "SYSTEM",
            "checkInventory": "Y",
            "organizationCode": "CROMA",
            "sourcingClassification": "EC",
            "promiseLines": {
                "promiseLine": [
                    {
                        "fulfillmentType": "HDEL",
                        "itemID": product["productId"],
                        "lineId": "1",
                        "requiredQty": "1",
                        "shipToAddress": {"zipCode": pincode},
                        "extn": {"widerStoreFlag": "N"},
                    }
                ]
            },
        }
    }

    try:
        res = requests.post(url, headers=headers, json=payload, proxies=LOCAL_PROXY_SETTINGS, timeout=10)
        res.raise_for_status()
        data = res.json()

        lines = (
            data.get("promise", {})
            .get("suggestedOption", {})
            .get("option", {})
            .get("promiseLines", {})
            .get("promiseLine", [])
        )

        if lines:
            print(f"[CROMA] ✅ {product['name']} deliverable to {pincode}")
            return f"[{product['name']}]({product['affiliateLink'] or product['url']})\n📍 Pincode: {pincode}"

        print(f"[CROMA] ❌ {product['name']} unavailable at {pincode}")
    except Exception as e:
        print(f"[error] Croma check failed for {product['name']}: {e}")
    return None

# --- iQOO API Checker (FINAL) ---
def check_iqoo_api(product):
    """Checks iQOO stock using the direct API endpoint."""
    product_id = product["productId"] # This is the SPU ID
    IQOO_API_URL = f"https://mshop.iqoo.com/in/api/product/activityInfo/all/{product_id}"
    
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "Referer": f"https://mshop.iqoo.com/in/product/{product_id}",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/5.36"
    })

    try:
        res = requests.get(IQOO_API_URL, headers=headers, proxies=LOCAL_PROXY_SETTINGS, timeout=10)
        res.raise_for_status()
        data = res.json()

        if data.get("success") != "1" or "data" not in data:
            print(f"[IQOO_API] ❌ {product['name']} failed. API success was not '1'.")
            return None

        sku_list = data.get("data", {}).get("activitySkuList", [])
        is_in_stock = any(sku.get("activityInfo", {}).get("reservableId") == -1 for sku in sku_list)

        if is_in_stock:
            print(f"[IQOO_API] ✅ {product['name']} is IN STOCK")
            return (
                f"[{product['name']}]({product['affiliateLink'] or product['url']})"
                f"\n💰 Price: N/A (API doesn't show price)"
            )
        else:
            print(f"[IQOO_API] ❌ {product['name']} is Out of Stock.")
            
    except Exception as e:
        print(f"[error] iQOO API check failed for {product_id}: {e}")
        return None

# --- Vivo API Checker (FINAL) ---
def check_vivo_api(product):
    """Checks Vivo stock using the direct API endpoint."""
    product_id = product["productId"] # This is the SPU ID
    VIVO_API_URL = f"https://mshop.vivo.com/in/api/product/activityInfo/all/{product_id}"
    
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "Referer": f"https://mshop.vivo.com/in/product/{product_id}",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/5.36"
    })

    try:
        res = requests.get(VIVO_API_URL, headers=headers, proxies=LOCAL_PROXY_SETTINGS, timeout=10)
        res.raise_for_status()
        data = res.json()

        if data.get("success") != "1" or "data" not in data:
            print(f"[VIVO_API] ❌ {product['name']} failed. API success was not '1'.")
            return None

        sku_list = data.get("data", {}).get("activitySkuList", [])
        is_in_stock = any(sku.get("activityInfo", {}).get("reservableId") == -1 for sku in sku_list)

        if is_in_stock:
            print(f"[VIVO_API] ✅ {product['name']} is IN STOCK")
            return (
                f"[{product['name']}]({product['affiliateLink'] or product['url']})"
                f"\n💰 Price: N/A (API doesn't show price)"
            )
        else:
            print(f"[VIVO_API] ❌ {product['name']} is Out of Stock.")
            
    except Exception as e:
        print(f"[error] Vivo API check failed for {product_id}: {e}")
        return None

# --- Unicorn Checker (API - FIXED PRODUCTS) ---
def check_unicorn_store():
    """Checks stock for a fixed set of iPhone 17 variants at Unicorn Store (hardcoded logic)."""
    
    COLOR_VARIANTS = {
        "Lavender": "313", "Sage": "311", "Mist Blue": "312", 
        "White": "314", "Black": "315",
    }
    STORAGE_256GB_ID = "250"
    
    BASE_URL = "https://fe01.beamcommerce.in/get_product_by_option_id"
    HEADERS = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "customer-id": "unicorn",
        "origin": "https://shop.unicornstore.in",
        "referer": "https://shop.unicornstore.in/",
    }
    CATEGORY_ID = "456" 
    FAMILY_ID = "94"
    GROUP_IDS = "57,58"
    
    messages_found = []

    for color_name, color_id in COLOR_VARIANTS.items():
        variant_name = f"iPhone 17 {color_name} 256GB"
        payload = {
            "category_id": CATEGORY_ID,
            "family_id": FAMILY_ID,
            "group_ids": GROUP_IDS,
            "option_ids": f"{color_id},{STORAGE_256GB_ID}"
        }

        try:
            res = requests.post(BASE_URL, headers=HEADERS, json=payload, proxies=LOCAL_PROXY_SETTINGS, timeout=10)
            res.raise_for_status()
            data = res.json()
            
            product_data = data.get("data", {}).get("product", {})
            quantity = product_data.get("quantity", 0)
            
            if int(quantity) > 0:
                price = f"₹{int(product_data.get('price', 0)):,}" if product_data.get('price') else "N/A"
                sku = product_data.get("sku", "N/A")
                product_url = "https://shop.unicornstore.in/iphone-17" 
                
                print(f"[UNICORN] ✅ {variant_name} is IN STOCK ({quantity} units)")
                messages_found.append(
                    f"[{variant_name} - {sku}]({product_url})"
                    f"\n💰 Price: {price}, Qty: {quantity}"
                )
            else:
                dispatch_note = product_data.get("custom_column_4", "Out of Stock").strip()
                print(f"[UNICORN] ❌ {variant_name} unavailable: {dispatch_note}")
                
        except Exception as e:
            print(f"[error] Unicorn check failed for {variant_name}: {e}")
            
    found_count = len(messages_found)
    if found_count > 0:
        header = f"🔥 *Stock Alert: Unicorn* {STORE_EMOJIS.get('unicorn', '📦')}\n\n"
        full_message = header + "\n---\n".join(messages_found)
        thread_id = STORE_TOPIC_IDS.get('unicorn')
        send_telegram_message(full_message, thread_id=thread_id)

    return {"total": len(COLOR_VARIANTS), "found": found_count}

# --- Vijay Sales Static Checker (FINAL) ---
def check_vijay_sales_store():
    """Checks stock for the 5 fixed iPhone 17 variants on Vijay Sales."""
    PINCODES = PINCODES_TO_CHECK  
    
    # Hardcoded products for fixed variants
    PRODUCTS = {
        "iPhone 17 Mist Blue 256GB": {
            "vanNo": "245181",
            "url": "https://www.vijaysales.com/p/P245179/245181/apple-iphone-17-256gb-storage-mist-blue"
        },
        "iPhone 17 Black 256GB": {
            "vanNo": "245179",
            "url": "https://www.vijaysales.com/p/P245179/245179/apple-iphone-17-256gb-storage-black"
        },
        "iPhone 17 White 256GB": {
            "vanNo": "245180",
            "url": "https://www.vijaysales.com/p/P245179/245180/apple-iphone-17-256gb-storage-white"
        },
        "iPhone 17 Lavender 256GB": {
            "vanNo": "245182",
            "url": "https://www.vijaysales.com/p/P245179/245182/apple-iphone-17-256gb-storage-lavender"
        },
        "iPhone 17 Sage 256GB": {
            "vanNo": "245183",
            "url": "https://www.vijaysales.com/p/P245179/245183/apple-iphone-17-256gb-storage-sage"
        }
    }

    messages_found = []
    total_variants = len(PRODUCTS)

    for name, info in PRODUCTS.items():
        vanNo = info["vanNo"]
        url = info["url"]

        for pin in PINCODES:
            # Add a random delay before each request to be less aggressive
            time.sleep(random.uniform(1, 3)) 
            
            api_url = (
                f"https://mdm.vijaysales.com/web/api/oms/check-servicibility/v1"
                f"?pincode={pin}&vanNo={vanNo}&storeList=true"
            )

            headers = {
                "accept": "*/*",
                "origin": "https://www.vijaysales.com",
                "referer": "https://www.vijaysales.com/",
                "user-agent": DEFAULT_HEADERS["User-Agent"]
            }

            try:
                res = requests.get(api_url, headers=headers, proxies=LOCAL_PROXY_SETTINGS, timeout=10)
                data = res.json()

                detail = data.get("data", {}).get(str(vanNo), {})
                delivery = detail.get("isServiceable", False)
                pickup_list = detail.get("storePickupList", [])
                pickup = len(pickup_list) > 0

                if delivery or pickup:
                    print(f"[VS] ✅ {name} available at {pin}")
                    msg = (
                        f"[{name}]({url})\n"
                        f"📦 Delivery: {'YES' if delivery else 'NO'}, "
                        f"🏬 Pickup: {'YES' if pickup else 'NO'}\n"
                        f"📍 Pincode: {pin}"
                    )
                    messages_found.append(msg)
                    break 

                else:
                    print(f"[VS] ❌ {name} not at {pin}")

            except Exception as e:
                print(f"[error] Vijay Sales failed for {name}: {e}")
    
    found_count = len(messages_found)
    
    if found_count > 0:
        header = f"🔥 *Stock Alert: Vijay Sales* {STORE_EMOJIS.get('vijay_sales', '🛍️')}\n\n"
        full_message = header + "\n---\n".join(messages_found)
        thread_id = STORE_TOPIC_IDS.get('vijay_sales')
        send_telegram_message(full_message, thread_id=thread_id)

    return {"total": total_variants, "found": found_count}


# --- iQOO and VIVO Checkers use the same logic as the old implementation for simplicity ---

# --- iQOO API Checker (FINAL) ---
def check_iqoo_api(product):
    """Checks iQOO stock using the direct API endpoint."""
    product_id = product["productId"]
    IQOO_API_URL = f"https://mshop.iqoo.com/in/api/product/activityInfo/all/{product_id}"
    
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "Referer": f"https://mshop.iqoo.com/in/product/{product_id}",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/5.36"
    })

    try:
        res = requests.get(IQOO_API_URL, headers=headers, proxies=LOCAL_PROXY_SETTINGS, timeout=10)
        res.raise_for_status()
        data = res.json()
        if data.get("success") != "1" or "data" not in data:
            print(f"[IQOO_API] ❌ {product['name']} failed.")
            return None

        sku_list = data.get("data", {}).get("activitySkuList", [])
        is_in_stock = any(sku.get("activityInfo", {}).get("reservableId") == -1 for sku in sku_list)

        if is_in_stock:
            print(f"[IQOO_API] ✅ {product['name']} is IN STOCK")
            return (
                f"[{product['name']}]({product['affiliateLink'] or product['url']})"
                f"\n💰 Price: N/A (In Stock)"
            )
        else:
            print(f"[IQOO_API] ❌ {product['name']} is Out of Stock.")
    except Exception as e:
        print(f"[error] iQOO API check failed for {product_id}: {e}")
        return None

# --- Vivo API Checker (FINAL) ---
def check_vivo_api(product):
    """Checks Vivo stock using the direct API endpoint."""
    product_id = product["productId"]
    VIVO_API_URL = f"https://mshop.vivo.com/in/api/product/activityInfo/all/{product_id}"
    
    headers = DEFAULT_HEADERS.copy()
    headers.update({
        "Referer": f"https://mshop.vivo.com/in/product/{product_id}",
        "User-Agent": "Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Mobile Safari/5.36"
    })

    try:
        res = requests.get(VIVO_API_URL, headers=headers, proxies=LOCAL_PROXY_SETTINGS, timeout=10)
        res.raise_for_status()
        data = res.json()

        if data.get("success") != "1" or "data" not in data:
            print(f"[VIVO_API] ❌ {product['name']} failed.")
            return None

        sku_list = data.get("data", {}).get("activitySkuList", [])
        is_in_stock = any(sku.get("activityInfo", {}).get("reservableId") == -1 for sku in sku_list)

        if is_in_stock:
            print(f"[VIVO_API] ✅ {product['name']} is IN STOCK")
            return (
                f"[{product['name']}]({product['affiliateLink'] or product['url']})"
                f"\n💰 Price: N/A (In Stock)"
            )
        else:
            print(f"[VIVO_API] ❌ {product['name']} is Out of Stock.")
            
    except Exception as e:
        print(f"[error] Vivo API check failed for {product_id}: {e}")
        return None


# ==================================
# 🗺️ STORE CHECKER MAP (FINAL)
# ==================================
STORE_CHECKERS_MAP = {
    "flipkart": check_flipkart_product,
    "reliance_digital": check_reliance_digital_product,
    "amazon": check_amazon_api,                
    "croma": check_croma_product,
    "iqoo": check_iqoo_api,                      
    "vivo": check_vivo_api,
}
# Note: unicorn and vijay_sales are handled separately in main_logic.

# ==================================
# 🚀 CHECKER CORE LOGIC
# ==================================
def check_store_products(store_type, products_to_check, pincodes):
    """
    Checks all products of a specific store type against pincodes (if applicable)
    and sends a Telegram message if stock is found.
    """
    checker_func = STORE_CHECKERS_MAP.get(store_type)
    if not checker_func:
        return {"total": 0, "found": 0}

    messages_found = []
    
    # 1. Logic for stores requiring Pincode checks (Flipkart, RD, Croma)
    if store_type in ["flipkart", "reliance_digital", "croma"]:
        for product in products_to_check:
            # Add a small random delay before each product check
            time.sleep(random.uniform(1, 3))
            for pincode in pincodes:
                message = checker_func(product, pincode)
                if message:
                    messages_found.append(message)
                    break # Stop checking other pincodes once stock is found
    
    # 2. Logic for stores that rely on single-endpoint checks (Amazon, iQOO, Vivo)
    elif store_type in ["amazon", "iqoo", "vivo"]:
        for product in products_to_check:
            # Amazon check doesn't use pincode, others are single-point API calls
            message = checker_func(product) 
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

    # --- 1. Filter Database Products by Store Type ---
    db_tracked_stores = ["flipkart", "reliance_digital", "amazon", "croma", "iqoo", "vivo"]
    products_by_store = {
        store_type: [p for p in products if p["storeType"] == store_type]
        for store_type in db_tracked_stores
    }
    
    # --- 2. Setup Initial Tracking Summary ---
    tracked_stores = {}
    for store_type in db_tracked_stores:
        tracked_stores[store_type] = {"total": len(products_by_store.get(store_type, [])), "found": 0}

    # Add static checkers to the tracking summary
    tracked_stores["unicorn"] = {"total": 5, "found": 0}
    tracked_stores["vijay_sales"] = {"total": 5, "found": 0}
    total_tracked = sum(data['total'] for data in tracked_stores.values())


    # --- 3. Run Checks Concurrently ---
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_store = {}
        
        # A. Submit tasks for DB-tracked stores
        for store_type in db_tracked_stores:
            if products_by_store.get(store_type):
                future = executor.submit(
                    check_store_products, 
                    store_type, 
                    products_by_store[store_type], 
                    PINCODES_TO_CHECK
                )
                future_to_store[future] = store_type

        # B. Submit tasks for statically tracked stores
        future_to_store[executor.submit(check_unicorn_store)] = "unicorn"
        future_to_store[executor.submit(check_vijay_sales_store)] = "vijay_sales"


        # C. Collect results
        for future in concurrent.futures.as_completed(future_to_store):
            store_type = future_to_store[future]
            try:
                result = future.result()
                tracked_stores[store_type]["found"] = result.get("found", 0)
            except Exception as e:
                print(f"[ERROR] Concurrent check for {store_type} failed: {e}")

    # --- 4. Compile Final Results ---
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