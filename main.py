import os
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import html
import ccxt
import pandas as pd
import requests
import yaml
import re
from dotenv import load_dotenv
import copy
import collections.abc
import time

load_dotenv()

def deep_update(d, u):
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = deep_update(d.get(k, {}), v)
        else:
            d[k] = v
    return d



#CONFIG_FILE = "config_git.yaml"
CONFIG_FILE = os.getenv("BTC_AGENT_CONFIG", "config_git.yaml")

# ============================================================
# Safety guard
# ============================================================

def enforce_no_trading(config):
    """
    Hard safety gate.
    Bot ini tidak boleh melakukan trading, cancel order, atau withdrawal.
    Kalau config salah di-set, script langsung berhenti.
    """
    safety = config.get("safety", {})

    if safety.get("allow_trading", False):
        raise RuntimeError("Safety violation: allow_trading must remain false.")

    if safety.get("allow_withdrawal", False):
        raise RuntimeError("Safety violation: allow_withdrawal must remain false.")

    if safety.get("allow_order_cancel", False):
        raise RuntimeError("Safety violation: allow_order_cancel must remain false.")


# ============================================================
# Config and environment
# ============================================================

def load_config(filename="config_git.yaml"):
    with open(filename, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value

def esc(value):
    return html.escape(str(value), quote=False)


def get_asset_config(config):
    """
    Read asset-specific configuration.
    Returns defaults for BTC if asset section is not present (backward compatible).
    """
    asset_cfg = config.get("asset", {})
    symbol = config.get("symbol", "BTC/USDT")
    parts = symbol.split("/")
    default_base = parts[0] if len(parts) >= 1 else "BTC"
    default_quote = parts[1] if len(parts) >= 2 else "USDT"

    return {
        "base": asset_cfg.get("base", default_base),
        "quote": asset_cfg.get("quote", default_quote),
        "name": asset_cfg.get("name", default_base),
        "coingecko_id": asset_cfg.get("coingecko_id", "bitcoin"),
        "agent_name": asset_cfg.get("agent_name", f"{default_base} Discipline Agent"),
        "price_decimals": int(asset_cfg.get("price_decimals", 0)),
    }


def format_price(price, decimals=0):
    """Format price with appropriate decimal places."""
    if decimals <= 0:
        return f"${price:,.0f}"
    return f"${price:,.{decimals}f}"


# ============================================================
# CoinGecko Cache
# ============================================================

COINGECKO_PRICE_CACHE = {}

def prefetch_coingecko_prices(assets_list):
    global COINGECKO_PRICE_CACHE
    ids = []
    for asset_override in assets_list:
        asset_cfg = asset_override.get("asset", {})
        coingecko_id = asset_cfg.get("coingecko_id")
        if coingecko_id:
            ids.append(coingecko_id)
            
    if not ids:
        return
        
    ids_str = ",".join(ids)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_str}&vs_currencies=usd&include_24hr_change=true"
    try:
        print(f"[INFO] Prefetching CoinGecko prices for: {ids_str}...")
        response = requests.get(url, timeout=25)
        if response.status_code == 200:
            COINGECKO_PRICE_CACHE = response.json()
            print(f"[INFO] Prefetched CoinGecko prices successfully: {COINGECKO_PRICE_CACHE}")
        else:
            print(f"[WARN] Prefetch CoinGecko prices failed with status {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[WARN] Prefetch CoinGecko prices failed: {e}")


# ============================================================
# Telegram
# ============================================================

TELEGRAM_SAFE_MESSAGE_LIMIT = 3800


def strip_html_tags(text):
    return re.sub(r"</?[^>]+>", "", str(text))


def split_telegram_message(message, limit=TELEGRAM_SAFE_MESSAGE_LIMIT):
    """
    Split long Telegram messages safely.

    Strategy:
    - Prefer splitting by double-newline sections so HTML tags are less likely to break.
    - If one section is still too long, strip HTML and split by hard character limit.
    """
    message = str(message)

    if len(message) <= limit:
        return [message]

    sections = message.split("\n\n")
    chunks = []
    current = ""

    for section in sections:
        candidate = section if not current else current + "\n\n" + section

        if len(candidate) <= limit:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        if len(section) <= limit:
            current = section
            continue

        plain_section = strip_html_tags(section)

        for start in range(0, len(plain_section), limit):
            chunks.append(plain_section[start:start + limit])

    if current:
        chunks.append(current)

    return chunks


def send_telegram(message):
    bot_token = get_required_env("TELEGRAM_BOT_TOKEN")
    chat_id = get_required_env("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    chunks = split_telegram_message(message)

    total_chunks = len(chunks)

    for index, chunk in enumerate(chunks, start=1):
        if total_chunks > 1:
            prefix = f"<b>Multi-Asset Trading Agent</b> — Part {index}/{total_chunks}\n\n"

            if index == 1:
                chunk_to_send = chunk
            else:
                chunk_to_send = prefix + chunk
        else:
            chunk_to_send = chunk

        payload = {
            "chat_id": chat_id,
            "text": chunk_to_send,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        response = requests.post(url, json=payload, timeout=25)

        print(f"Telegram status code: {response.status_code}")
        print(f"Telegram response: {response.text}")

        if response.ok:
            continue

        print("[WARN] Telegram HTML send failed. Retrying this chunk as plain text.")

        plain_text = strip_html_tags(chunk_to_send)

        plain_payload = {
            "chat_id": chat_id,
            "text": plain_text[:TELEGRAM_SAFE_MESSAGE_LIMIT],
            "disable_web_page_preview": True,
        }

        plain_response = requests.post(url, json=plain_payload, timeout=25)

        print(f"Telegram plain status code: {plain_response.status_code}")
        print(f"Telegram plain response: {plain_response.text}")

        if not plain_response.ok:
            raise RuntimeError(f"Telegram error: {plain_response.text}")


# ============================================================
# Tokocrypto exchange builder
# ============================================================

def build_tokocrypto_exchange(private=False):
    params = {
        "enableRateLimit": True,
        "timeout": 12000,
    }

    if private:
        api_key = os.getenv("TOKOCRYPTO_API_KEY")
        api_secret = os.getenv("TOKOCRYPTO_API_SECRET")

        if not api_key or not api_secret:
            raise RuntimeError("Tokocrypto private API key/secret belum tersedia.")

        params["apiKey"] = api_key
        params["secret"] = api_secret

    exchange = ccxt.tokocrypto(params)
    
    # Override endpoints to bypass ISP block in Indonesia
    if hasattr(exchange, 'urls') and 'api' in exchange.urls and 'rest' in exchange.urls['api']:
        if 'binance' in exchange.urls['api']['rest']:
            exchange.urls['api']['rest']['binance'] = 'https://data-api.binance.vision/api/v3'
            
    return exchange


# ============================================================
# Market data
# ============================================================

def get_tokocrypto_price_via_ccxt(symbol):
    """
    Coba ambil harga BTC/USDT dari Tokocrypto via CCXT.
    Catatan: di environment kamu, ccxt.tokocrypto.fetch_ticker()
    pernah route ke api.binance.com dan timeout. Karena itu fungsi ini
    wajib dibungkus try/except oleh caller.
    """
    exchange = build_tokocrypto_exchange(private=False)
    ticker = exchange.fetch_ticker(symbol)

    last = ticker.get("last")
    high = ticker.get("high") or last
    low = ticker.get("low") or last
    percentage = ticker.get("percentage") or 0
    volume = ticker.get("baseVolume") or 0

    if last is None:
        raise RuntimeError(f"Tokocrypto ticker missing last price: {ticker}")

    return {
        "price": float(last),
        "change_pct_24h": float(percentage),
        "high_24h": float(high),
        "low_24h": float(low),
        "volume": float(volume),
        "source": "Tokocrypto via CCXT",
    }


def get_coingecko_price(coingecko_id="bitcoin"):
    # Check cache first
    global COINGECKO_PRICE_CACHE
    if coingecko_id in COINGECKO_PRICE_CACHE:
        coin_data = COINGECKO_PRICE_CACHE[coingecko_id]
        if "usd" in coin_data:
            price = float(coin_data["usd"])
            change_24h = float(coin_data.get("usd_24h_change", 0.0) or 0.0)
            print(f"[INFO] Using cached CoinGecko price for {coingecko_id}: price={price}, 24h_change={change_24h}%")
            return {
                "price": price,
                "change_pct_24h": change_24h,
                "high_24h": price,
                "low_24h": price,
                "volume": 0,
                "source": "CoinGecko fallback (cached)",
            }

    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        f"?ids={coingecko_id}"
        "&vs_currencies=usd"
        "&include_24hr_change=true"
    )

    # Sleep to avoid 429 if doing direct query
    time.sleep(2)
    response = requests.get(url, timeout=25)
    data = response.json()

    coin_data = data.get(coingecko_id, {})
    if not coin_data or "usd" not in coin_data:
        raise RuntimeError(f"CoinGecko missing usd price for {coingecko_id}: {data}")

    price = float(coin_data["usd"])
    change = float(coin_data.get("usd_24h_change", 0))

    return {
        "price": price,
        "change_pct_24h": change,
        "high_24h": price,
        "low_24h": price,
        "volume": 0,
        "source": "CoinGecko fallback",
    }


def get_24h_ticker(symbol, config=None):
    """
    Primary: Tokocrypto price.
    Fallback: CoinGecko.

    Kita tetap coba Tokocrypto dulu karena kamu memakai Tokocrypto.
    Tapi karena CCXT Tokocrypto public ticker di WSL kamu pernah timeout
    ke api.binance.com, fallback CoinGecko wajib.
    """
    asset = get_asset_config(config) if config else {"coingecko_id": "bitcoin"}
    coingecko_id = asset.get("coingecko_id", "bitcoin")

    try:
        return get_tokocrypto_price_via_ccxt(symbol)
    except Exception as error:
        print(f"[WARN] Tokocrypto price failed, fallback to CoinGecko: {error}")

    return get_coingecko_price(coingecko_id=coingecko_id)


def get_daily_klines(symbol, days=30, **kwargs):
    """
    Untuk historical daily data, sekarang kita pakai Binance public data API
    yang tidak diblokir (data-api.binance.vision) dan tidak gampang kena rate limit.
    Jika gagal, kita fallback ke CoinGecko.
    """
    coingecko_id = kwargs.get("coingecko_id", "bitcoin")
    binance_symbol = symbol.replace("/", "").replace("-", "")
    url = "https://data-api.binance.vision/api/v3/klines"
    params = {
        "symbol": binance_symbol,
        "interval": "1d",
        "limit": days,
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.ok:
            data = response.json()
            if data:
                rows = []
                for kline in data:
                    rows.append({
                        "time": pd.to_datetime(kline[0], unit="ms"),
                        "open": float(kline[1]),
                        "high": float(kline[2]),
                        "low": float(kline[3]),
                        "close": float(kline[4]),
                        "volume": float(kline[5]),
                    })
                df = pd.DataFrame(rows)
                return df.tail(days).reset_index(drop=True)
                
        print(f"[WARN] Binance klines failed for {symbol}: {response.status_code if 'response' in locals() else 'unknown'}")
        
    except Exception as error:
        print(f"[WARN] Failed fetching klines from Binance for {symbol}: {error}")
        
    print(f"[INFO] Fallback to CoinGecko for {symbol} daily klines...")
    try:
        cg_url = f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart"
        cg_params = {
            "vs_currency": "usd",
            "days": days,
            "interval": "daily",
        }
        time.sleep(6) # Sleep to avoid 429
        cg_response = requests.get(cg_url, params=cg_params, timeout=25)
        cg_data = cg_response.json()
        prices = cg_data.get("prices", [])
        
        if not prices:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
            
        rows = []
        for timestamp_ms, price in prices:
            rows.append({
                "time": pd.to_datetime(timestamp_ms, unit="ms"),
                "open": float(price),
                "high": float(price),
                "low": float(price),
                "close": float(price),
                "volume": 0,
            })
        df = pd.DataFrame(rows)
        return df.tail(days).reset_index(drop=True)
        
    except Exception as e:
        print(f"[WARN] CoinGecko fallback failed for {symbol}: {e}")
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

# ============================================================
# Intrahour price and possible order touch detection
# ============================================================

def get_recent_intrahour_price_window(config):
    """
    Fetch recent CoinGecko market chart data to detect whether BTC touched
    manual open-order levels between hourly bot runs.

    Important:
    This does NOT confirm Tokocrypto execution.
    It only detects that market price likely touched the configured order level.
    """
    check_cfg = config.get("intrahour_price_check", {})

    if not check_cfg.get("enabled", False):
        return {
            "enabled": False,
            "available": False,
            "source": "disabled",
            "message": "Intrahour price check disabled.",
            "rows": [],
        }

    source = check_cfg.get("source", "coingecko_market_chart")
    lookback_minutes = int(check_cfg.get("lookback_minutes", 90))

    if source != "coingecko_market_chart":
        return {
            "enabled": True,
            "available": False,
            "source": source,
            "message": f"Unsupported intrahour source: {source}",
            "rows": [],
        }

    asset = get_asset_config(config)
    symbol = config.get("symbol", "BTC/USDT")
    binance_symbol = symbol.replace("/", "").replace("-", "")
    url = "https://data-api.binance.vision/api/v3/klines"
    
    # If lookback is 90 mins, fetch 100 klines of 1m interval
    limit = max(10, lookback_minutes + 10)
    if limit > 1000:
        limit = 1000
        
    params = {
        "symbol": binance_symbol,
        "interval": "1m",
        "limit": limit,
    }

    try:
        response = requests.get(url, params=params, timeout=25)

        if not response.ok:
            return {
                "enabled": True,
                "available": False,
                "source": source,
                "message": f"Binance intrahour klines failed: {response.status_code} {response.text[:300]}",
                "rows": [],
            }

        data = response.json()

        if not data:
            return {
                "enabled": True,
                "available": False,
                "source": source,
                "message": "Binance intrahour klines returned no data.",
                "rows": [],
            }

        now_utc = datetime.now(timezone.utc)
        cutoff_utc = now_utc - pd.Timedelta(minutes=lookback_minutes)

        rows = []
        for kline in data:
            timestamp_ms = kline[0]
            price = kline[3] # low price of the 1m candle
            timestamp_utc = pd.to_datetime(timestamp_ms, unit="ms", utc=True)

            if timestamp_utc < cutoff_utc:
                continue

            rows.append({
                "time_utc": timestamp_utc,
                "price": float(price),
            })

        if not rows:
            return {
                "enabled": True,
                "available": False,
                "source": source,
                "message": "No Binance prices inside configured lookback window.",
                "rows": [],
            }

        df = pd.DataFrame(rows)

        return {
            "enabled": True,
            "available": True,
            "source": source,
            "lookback_minutes": lookback_minutes,
            "window_start_utc": df["time_utc"].min().isoformat(),
            "window_end_utc": df["time_utc"].max().isoformat(),
            "window_low": float(df["price"].min()),
            "window_high": float(df["price"].max()),
            "last_price_in_window": float(df["price"].iloc[-1]),
            "rows": rows,
        }

    except Exception as error:
        print(f"[WARN] Failed fetching intrahour klines from Binance for {symbol}: {error}")

    print(f"[INFO] Fallback to CoinGecko for {symbol} intrahour prices...")
    
    try:
        coingecko_id = asset.get("coingecko_id", "bitcoin")
        cg_url = f"https://api.coingecko.com/api/v3/coins/{coingecko_id}/market_chart"
        cg_params = {
            "vs_currency": "usd",
            "days": 1,
        }
        
        time.sleep(6)
        cg_response = requests.get(cg_url, params=cg_params, timeout=25)
        
        if not cg_response.ok:
            return {
                "enabled": True,
                "available": False,
                "source": source,
                "message": f"CoinGecko intrahour fallback failed: {cg_response.status_code} {cg_response.text[:300]}",
                "rows": [],
            }
            
        cg_data = cg_response.json()
        prices = cg_data.get("prices", [])
        
        if not prices:
            return {
                "enabled": True,
                "available": False,
                "source": source,
                "message": "CoinGecko intrahour fallback returned no prices.",
                "rows": [],
            }
            
        now_utc = datetime.now(timezone.utc)
        cutoff_utc = now_utc - pd.Timedelta(minutes=lookback_minutes)

        rows = []
        for timestamp_ms, price in prices:
            timestamp_utc = pd.to_datetime(timestamp_ms, unit="ms", utc=True)

            if timestamp_utc < cutoff_utc:
                continue

            rows.append({
                "time_utc": timestamp_utc,
                "price": float(price),
            })
            
        if not rows:
            return {
                "enabled": True,
                "available": False,
                "source": source,
                "message": "No CoinGecko fallback prices inside configured lookback window.",
                "rows": [],
            }
            
        df = pd.DataFrame(rows)
        return {
            "enabled": True,
            "available": True,
            "source": source,
            "lookback_minutes": lookback_minutes,
            "window_start_utc": df["time_utc"].min().isoformat(),
            "window_end_utc": df["time_utc"].max().isoformat(),
            "window_low": float(df["price"].min()),
            "window_high": float(df["price"].max()),
            "last_price_in_window": float(df["price"].iloc[-1]),
            "rows": rows,
        }
        
    except Exception as e:
        return {
            "enabled": True,
            "available": False,
            "source": source,
            "message": f"CoinGecko fallback exception: {e}",
            "rows": [],
        }


def detect_intrahour_order_events(config, intrahour_window, current_price):
    """
    Detect whether recent CoinGecko prices touched configured manual open-order levels.

    This returns POSSIBLE fills, not confirmed fills.
    True fill confirmation requires Tokocrypto private order/trade data.
    """
    check_cfg = config.get("intrahour_price_check", {})
    tolerance_pct = float(check_cfg.get("touch_tolerance_pct", 0.15))

    result = {
        "enabled": bool(check_cfg.get("enabled", False)),
        "available": bool(intrahour_window.get("available", False)),
        "source": intrahour_window.get("source", "unknown"),
        "lookback_minutes": intrahour_window.get("lookback_minutes"),
        "window_start_utc": intrahour_window.get("window_start_utc"),
        "window_end_utc": intrahour_window.get("window_end_utc"),
        "window_low": intrahour_window.get("window_low"),
        "window_high": intrahour_window.get("window_high"),
        "last_price_in_window": intrahour_window.get("last_price_in_window"),
        "message": intrahour_window.get("message", ""),
        "events": [],
        "has_possible_fill": False,
        "estimated_portfolio_if_touched": None,
    }

    if not result["enabled"] or not result["available"]:
        return result

    rows = intrahour_window.get("rows", [])
    if not rows:
        result["available"] = False
        result["message"] = "Intrahour window has no rows."
        return result

    manual_orders_cfg = config.get("manual_open_orders", {})
    if not manual_orders_cfg.get("enabled", False):
        return result

    open_orders = [
        order for order in manual_orders_cfg.get("orders", [])
        if order.get("status") == "open"
    ]

    touched_orders = []

    for order in open_orders:
        side = str(order.get("side", "")).lower()
        price = float(order.get("price", 0) or 0)
        amount = float(order.get("amount", 0) or 0)
        allocated_usdt = float(order.get("allocated_usdt", 0) or 0)

        if price <= 0:
            continue

        if allocated_usdt <= 0 and amount > 0:
            allocated_usdt = price * amount

        tolerance = price * (tolerance_pct / 100)

        touched_rows = []

        for row in rows:
            observed_price = float(row["price"])

            if side == "buy":
                touched = observed_price <= price + tolerance
            elif side == "sell":
                touched = observed_price >= price - tolerance
            else:
                touched = False

            if touched:
                touched_rows.append(row)

        if not touched_rows:
            continue

        first_touch = touched_rows[0]

        if side == "buy":
            best_price = min(float(row["price"]) for row in touched_rows)
        else:
            best_price = max(float(row["price"]) for row in touched_rows)

        event = {
            "side": side,
            "type": order.get("type"),
            "order_price": price,
            "amount": amount,
            "allocated_usdt": allocated_usdt,
            "status": "possible_fill_price_touched",
            "first_touch_utc": first_touch["time_utc"].isoformat(),
            "best_price_in_window": best_price,
            "note": (
                "CoinGecko recent price touched this manual open-order level. "
                "This is NOT confirmed Tokocrypto execution. Verify Tokocrypto and update config_git.yaml."
            ),
        }

        result["events"].append(event)
        touched_orders.append(event)
        result["has_possible_fill"] = True

    if check_cfg.get("estimate_portfolio_if_touched", True):
        result["estimated_portfolio_if_touched"] = estimate_portfolio_if_touched_orders_filled(
            config=config,
            touched_orders=touched_orders,
            current_price=current_price,
        )

    return result


def estimate_portfolio_if_touched_orders_filled(config, touched_orders, current_price):
    """
    Estimate portfolio state if touched manual buy orders were actually filled.

    This is only an estimate.
    It does not account for exact Tokocrypto fills, partial fills, or fees.
    """
    portfolio_cfg = config.get("portfolio", {})
    manual_orders_cfg = config.get("manual_open_orders", {})

    base_asset_qty = float(portfolio_cfg.get(config.get("asset", {}).get("base", "BTC").lower(), 0) or 0)
    base_usdt_total = float(portfolio_cfg.get("usdt", 0) or 0)

    touched_buy_orders = [
        event for event in touched_orders
        if event.get("side") == "buy"
    ]

    touched_allocated_usdt = sum(
        float(event.get("allocated_usdt", 0) or 0)
        for event in touched_buy_orders
    )

    touched_base_amount = sum(
        float(event.get("amount", 0) or 0)
        for event in touched_buy_orders
    )

    touched_prices = [
        float(event.get("order_price", 0) or 0)
        for event in touched_buy_orders
        if float(event.get("order_price", 0) or 0) > 0
    ]

    remaining_open_order_usdt = 0.0

    touched_order_keys = {
        (
            str(event.get("side")),
            float(event.get("order_price", 0) or 0),
            float(event.get("amount", 0) or 0),
        )
        for event in touched_buy_orders
    }

    if manual_orders_cfg.get("enabled", False):
        for order in manual_orders_cfg.get("orders", []):
            if order.get("status") != "open":
                continue

            side = str(order.get("side", "")).lower()
            price = float(order.get("price", 0) or 0)
            amount = float(order.get("amount", 0) or 0)
            allocated_usdt = float(order.get("allocated_usdt", 0) or 0)

            order_key = (side, price, amount)

            if order_key in touched_order_keys:
                continue

            if side == "buy":
                if allocated_usdt <= 0 and amount > 0 and price > 0:
                    allocated_usdt = price * amount
                remaining_open_order_usdt += allocated_usdt

    estimated_base = base_asset_qty + touched_base_amount
    estimated_usdt_total = max(base_usdt_total - touched_allocated_usdt, 0)
    estimated_usdt_used = remaining_open_order_usdt
    estimated_usdt_free = max(estimated_usdt_total - estimated_usdt_used, 0)

    estimated_base_value = estimated_base * current_price
    estimated_total_value = estimated_usdt_total + estimated_base_value
    estimated_base_pct = (
        (estimated_base_value / estimated_total_value) * 100
        if estimated_total_value > 0 else 0
    )
    estimated_usdt_pct = 100 - estimated_base_pct if estimated_total_value > 0 else 0

    avg_touched_price = (
        touched_allocated_usdt / touched_base_amount
        if touched_base_amount > 0 else 0
    )

    return {
        "not_confirmed": True,
        "must_verify_tokocrypto": True,
        "touched_order_count": len(touched_buy_orders),
        "touched_prices": touched_prices,
        "touched_base_amount": touched_base_amount,
        "touched_allocated_usdt": touched_allocated_usdt,
        "avg_touched_price": avg_touched_price,
        "base_asset_qty": base_asset_qty,
        "base_usdt_total": base_usdt_total,
        "estimated_base": estimated_base,
        "estimated_usdt_total": estimated_usdt_total,
        "estimated_usdt_free": estimated_usdt_free,
        "estimated_usdt_used_open_orders": estimated_usdt_used,
        "estimated_base_value": estimated_base_value,
        "estimated_total_value": estimated_total_value,
        "estimated_base_pct": estimated_base_pct,
        "estimated_usdt_pct": estimated_usdt_pct,
    }


def adjust_decision_for_intrahour_order_events(config, decision, intrahour_order_events):
    """
    If price touched one or more manual open-order levels, force a verification-first HOLD.
    This prevents the bot from giving misleading recommendations while config may be stale.
    """
    check_cfg = config.get("intrahour_price_check", {})

    if not check_cfg.get("force_verify_on_touch", True):
        return decision

    if not intrahour_order_events.get("has_possible_fill", False):
        return decision

    touched_levels = [
        f"{event['side']} @{event['order_price']:.0f}"
        for event in intrahour_order_events.get("events", [])
    ]

    touched_text = ", ".join(touched_levels)

    return {
        "signal": "HOLD / VERIFY POSSIBLE FILLED ORDER",
        "action_usdt": 0,
        "reason": (
            f"Harga CoinGecko recent window menyentuh level order manual ({touched_text}). "
            f"Bot belum bisa memastikan eksekusi tanpa private Tokocrypto API. "
            f"Verifikasi order di Tokocrypto dan update config_git.yaml sebelum keputusan baru."
        ),
    }


def format_intrahour_order_events(intrahour_order_events):
    if not intrahour_order_events.get("enabled", False):
        return "Recent price check: disabled"

    if not intrahour_order_events.get("available", False):
        message = intrahour_order_events.get("message", "unavailable")
        return f"Recent price check unavailable: {message}"

    lines = [
        (
            f"Recent price check: last {intrahour_order_events.get('lookback_minutes')} min "
            f"from {intrahour_order_events.get('source')}"
        ),
        (
            f"Window range: "
            f"${intrahour_order_events.get('window_low'):,.0f} - "
            f"${intrahour_order_events.get('window_high'):,.0f}"
        ),
    ]

    events = intrahour_order_events.get("events", [])

    if not events:
        lines.append("Order touch: no configured open-order level touched in this window.")
        return "\n".join(lines)

    lines.append("Order touch: POSSIBLE FILL DETECTED — NOT CONFIRMED")

    for event in events:
        lines.append(
            f"- {event['side']} limit @{event['order_price']:,.0f} touched; "
            f"best observed price ${event['best_price_in_window']:,.0f}; "
            f"first touch {event['first_touch_utc']}"
        )

    estimate = intrahour_order_events.get("estimated_portfolio_if_touched")

    if estimate:
        lines.append("")
        lines.append("Estimated portfolio if touched buy orders were filled:")
        lines.append(f"- Base estimate: {estimate['estimated_base']:.8f}")
        lines.append(f"- USDT total estimate: {estimate['estimated_usdt_total']:.2f}")
        lines.append(f"- USDT free estimate: {estimate['estimated_usdt_free']:.2f}")
        lines.append(f"- USDT still in open orders estimate: {estimate['estimated_usdt_used_open_orders']:.2f}")
        lines.append(f"- Base allocation estimate: {estimate['estimated_base_pct']:.1f}%")
        lines.append(f"- Total value estimate: {estimate['estimated_total_value']:.2f} USDT")

    lines.append("")
    lines.append("Status: NOT CONFIRMED. Verify Tokocrypto, then update config_git.yaml.")

    return "\n".join(lines)

# ============================================================
# Gemini-assisted decision guard
# ============================================================

def calculate_planned_base_exposure_pct(config, portfolio, market_price, extra_buy_usdt=0):
    """
    Planned BTC exposure:
    current BTC + BTC from still-open manual buy orders + optional extra buy.
    This prevents Gemini from recommending upside buys that make total planned exposure too aggressive.
    """
    manual_orders_cfg = config.get("manual_open_orders", {})
    open_order_base = 0.0

    if manual_orders_cfg.get("enabled", False):
        for order in manual_orders_cfg.get("orders", []):
            if order.get("status") != "open":
                continue

            side = str(order.get("side", "")).lower()
            if side != "buy":
                continue

            amount = float(order.get("amount", 0) or 0)
            open_order_base += amount

    extra_base = 0.0
    if market_price > 0 and extra_buy_usdt > 0:
        extra_base = extra_buy_usdt / market_price

    planned_base = float(portfolio.get(config.get("asset", {}).get("base", "BTC").lower(), 0) or 0) + open_order_base + extra_base
    planned_base_value = planned_base * market_price

    total_value = float(portfolio.get("total_value", 0) or 0)
    if total_value <= 0:
        return 0.0

    return planned_base_value / total_value * 100

def calculate_upside_base_pct_after_buy(portfolio, market_price, buy_usdt):
    """
    Upside BTC exposure:
    current BTC + immediate Gemini buy only.
    This intentionally excludes lower open buy orders because they are unlikely
    to fill if BTC continues moving upward.
    """
    base_now = float(portfolio.get("base_free", 0)) + float(portfolio.get("base_used", 0))
    total_value = float(portfolio.get("total_value", 0) or 0)

    if market_price <= 0 or total_value <= 0:
        return 0.0

    extra_base = 0.0
    if buy_usdt > 0:
        extra_base = buy_usdt / market_price

    base_value_after_buy = (base_now + extra_base) * market_price
    return base_value_after_buy / total_value * 100


def get_gemini_exposure_tier(config, market, has_open_orders):
    """
    Select exposure tier for Gemini buy candidate.

    Tiers:
    - early_confirmation: BTC above MA7, but not confirmed above MA20.
    - confirmed_breakout: BTC above MA20 confirmation threshold.
    - overheated: 24h pump too high; block fresh buy.

    This function includes safe defaults so the buy gate does not silently fail
    if exposure_policy is missing or not loaded correctly.
    """
    decision_cfg = config.get("gemini_decision", {})
    exposure_policy = decision_cfg.get("exposure_policy", {})
    asset = get_asset_config(config)
    base = asset.get("base", "BTC")

    default_exposure_policy = {
        "enabled": True,
        "early_confirmation": {
            "description": f"{base} above MA7 but still below confirmed MA20 breakout",
            "max_buy_usdt_with_open_orders": 15,
            "max_buy_usdt_without_open_orders": 25,
            "max_upside_base_pct_after_buy": 35,
            "max_downside_planned_base_pct": 55,
            "min_confidence_to_buy": 85,
        },
        "confirmed_breakout": {
            "description": f"{base} above MA20 with confirmation",
            "max_buy_usdt_with_open_orders": 25,
            "max_buy_usdt_without_open_orders": 40,
            "max_upside_base_pct_after_buy": 45,
            "max_downside_planned_base_pct": 60,
            "min_confidence_to_buy": 80,
        },
        "overheated": {
            "description": f"{base} pumping too fast; block fresh buy",
            "block_buy_if_24h_pump_above_pct": 4,
            "max_buy_usdt_with_open_orders": 0,
            "max_buy_usdt_without_open_orders": 0,
        },
    }

    if not isinstance(exposure_policy, dict):
        exposure_policy = {}

    if not exposure_policy.get("enabled", False):
        print(
            "[BUY_GATE] exposure_policy missing/disabled; using safe default exposure policy",
            flush=True,
        )
        exposure_policy = default_exposure_policy

    for tier_name in ["early_confirmation", "confirmed_breakout", "overheated"]:
        if tier_name not in exposure_policy:
            exposure_policy[tier_name] = default_exposure_policy[tier_name]

    price = float(market.get("price", 0) or 0)
    ma7 = float(market.get("ma_7", 0) or 0)
    ma20 = float(market.get("ma_20", 0) or 0)
    change_24h = float(market.get("change_24h", 0) or 0)

    print(
        "[BUY_GATE] exposure_policy debug | "
        f"enabled={exposure_policy.get('enabled')} | "
        f"has_early={bool(exposure_policy.get('early_confirmation'))} | "
        f"has_breakout={bool(exposure_policy.get('confirmed_breakout'))} | "
        f"has_overheated={bool(exposure_policy.get('overheated'))} | "
        f"price={price} | ma7={ma7} | ma20={ma20} | change_24h={change_24h}",
        flush=True,
    )

    if price <= 0 or ma7 <= 0 or ma20 <= 0:
        return {
            "name": "invalid_market_data",
            "can_buy": False,
            "settings": {},
            "reason": "Price, MA7, or MA20 is invalid. Fresh buy blocked.",
        }

    overheated_cfg = exposure_policy.get("overheated", {})
    pump_block_pct = float(overheated_cfg.get("block_buy_if_24h_pump_above_pct", 4))

    if change_24h >= pump_block_pct:
        return {
            "name": "overheated",
            "can_buy": False,
            "settings": overheated_cfg,
            "reason": (
                f"{base} 24h change is {change_24h:.2f}%, above overheated block "
                f"{pump_block_pct:.2f}%."
            ),
        }

    bullish_cfg = decision_cfg.get("bullish_confirmation", {})
    confirmation_pct = float(bullish_cfg.get("confirmation_above_ma20_pct", 1.5))
    confirmed_breakout_price = ma20 * (1 + confirmation_pct / 100)

    if price > confirmed_breakout_price and price > ma7:
        settings = exposure_policy.get("confirmed_breakout", {})
        return {
            "name": "confirmed_breakout",
            "can_buy": True,
            "settings": settings,
            "reason": (
                f"{base} is above MA7 and above confirmed MA20 threshold "
                f"({confirmed_breakout_price:.2f})."
            ),
        }

    if price > ma7:
        settings = exposure_policy.get("early_confirmation", {})
        return {
            "name": "early_confirmation",
            "can_buy": True,
            "settings": settings,
            "reason": f"{base} is above MA7 but has not confirmed above MA20 yet.",
        }

    return {
        "name": "no_bullish_confirmation",
        "can_buy": False,
        "settings": {},
        "reason": f"{base} is not above MA7. Fresh buy blocked.",
    }


def get_tiered_max_buy_usdt(exposure_tier, has_open_orders):
    if not exposure_tier or not exposure_tier.get("can_buy", False):
        return 0.0

    settings = exposure_tier.get("settings", {})

    if has_open_orders:
        return float(settings.get("max_buy_usdt_with_open_orders", 0) or 0)

    return float(settings.get("max_buy_usdt_without_open_orders", 0) or 0)

def parse_utc_datetime(value):
    if not value:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def get_manual_execution_entries(config):
    manual_exec_cfg = config.get("manual_executions", {})

    if not manual_exec_cfg.get("enabled", False):
        return []

    entries = manual_exec_cfg.get("entries", [])
    if not isinstance(entries, list):
        return []

    return entries


def get_latest_filled_manual_buy(config):
    entries = get_manual_execution_entries(config)

    filled_buys = []

    for entry in entries:
        if str(entry.get("status", "")).lower() != "filled":
            continue

        if str(entry.get("side", "")).lower() != "buy":
            continue

        executed_at = parse_utc_datetime(entry.get("executed_at_utc"))
        if executed_at is None:
            continue

        avg_fill_price = float(entry.get("avg_fill_price_usdt", 0) or 0)
        quote_spent = float(entry.get("quote_spent_usdt", 0) or 0)
        base_received = float(entry.get("base_received_btc", 0) or 0)

        if avg_fill_price <= 0 or quote_spent <= 0 or base_received <= 0:
            continue

        normalized = dict(entry)
        normalized["_executed_at_dt"] = executed_at
        normalized["_avg_fill_price_usdt"] = avg_fill_price
        normalized["_quote_spent_usdt"] = quote_spent
        normalized["_base_received_btc"] = base_received

        filled_buys.append(normalized)

    if not filled_buys:
        return None

    filled_buys.sort(key=lambda item: item["_executed_at_dt"], reverse=True)
    return filled_buys[0]

def evaluate_manual_buy_anti_repeat(config, market, exposure_tier, candidate_action_key):
    """
    Prevent repeated manual buys from the same signal/tier.

    This guard runs before Gemini receives BUY candidate actions.
    It only blocks a new buy candidate. It does not affect sell/hold actions.

    Bypass conditions (any one is enough to bypass cooldown):
    1. Tier upgrade: early_confirmation -> confirmed_breakout
    2. Breakout bypass: BTC rose > breakout_bypass_pct from last buy price
    3. Crash bypass: BTC dropped > crash_bypass_pct from last buy price

    BLOCK ONLY IF:
    - same tier AND same thesis AND price move insignificant AND cooldown active
    """
    manual_exec_cfg = config.get("manual_executions", {})
    anti_cfg = manual_exec_cfg.get("anti_repeat", {})

    result = {
        "blocked": False,
        "reason": "",
        "latest_buy": None,
        "hours_since_last_buy": None,
        "price_move_pct_from_last_buy": None,
        "bypass_reason": None,
    }

    if not manual_exec_cfg.get("enabled", False):
        return result

    if not anti_cfg.get("enabled", False):
        return result

    if not anti_cfg.get("block_before_gemini", True):
        return result

    latest_buy = get_latest_filled_manual_buy(config)
    if not latest_buy:
        return result

    result["latest_buy"] = latest_buy

    now_utc = datetime.now(timezone.utc)
    executed_at = latest_buy["_executed_at_dt"]
    hours_since = (now_utc - executed_at).total_seconds() / 3600
    result["hours_since_last_buy"] = hours_since

    current_price = float(market.get("price", 0) or 0)
    last_buy_price = float(latest_buy.get("_avg_fill_price_usdt", 0) or 0)

    price_move_pct = 0.0
    if current_price > 0 and last_buy_price > 0:
        price_move_pct = ((current_price - last_buy_price) / last_buy_price) * 100

    result["price_move_pct_from_last_buy"] = price_move_pct

    last_signal_key = str(latest_buy.get("source_signal_key", ""))
    last_tier = str(latest_buy.get("source_tier", ""))
    current_tier = str((exposure_tier or {}).get("name", ""))

    cooldown_hours = float(anti_cfg.get("cooldown_hours", 72) or 72)
    block_same_signal_key = bool(anti_cfg.get("block_same_signal_key", True))
    block_same_tier = bool(anti_cfg.get("block_same_tier", True))
    require_price_move_pct = float(
        anti_cfg.get("require_price_move_pct_for_same_tier_buy", 2.0) or 2.0
    )
    allow_new_buy_if_tier_upgrades = bool(
        anti_cfg.get("allow_new_buy_if_tier_upgrades", True)
    )

    # --- New bypass thresholds ---
    bypass_rise_pct = float(anti_cfg.get("bypass_if_price_rise_from_last_buy_pct", 0) or 0)
    bypass_drop_pct = float(anti_cfg.get("bypass_if_price_drop_from_last_buy_pct", 0) or 0)

    same_signal = (
        block_same_signal_key
        and last_signal_key
        and candidate_action_key == last_signal_key
    )

    same_tier = (
        block_same_tier
        and last_tier
        and current_tier
        and current_tier == last_tier
    )

    # --- Bypass condition 1: tier upgrade ---
    tier_upgraded = (
        allow_new_buy_if_tier_upgrades
        and last_tier == "early_confirmation"
        and current_tier == "confirmed_breakout"
    )

    if tier_upgraded:
        result["bypass_reason"] = (
            f"Cooldown bypassed: tier upgraded from {last_tier} to {current_tier}. "
            f"Market thesis has changed."
        )
        return result

    # --- Bypass condition 2: significant breakout (price moved UP) ---
    if bypass_rise_pct > 0 and price_move_pct >= bypass_rise_pct:
        result["bypass_reason"] = (
            f"Cooldown bypassed: BTC rose {price_move_pct:.2f}% from last buy "
            f"(${last_buy_price:,.0f} -> ${current_price:,.0f}), "
            f"exceeding bypass_if_price_rise_from_last_buy_pct threshold of {bypass_rise_pct:.1f}%. "
            f"Market context has significantly changed."
        )
        return result

    # --- Bypass condition 3: significant crash (price moved DOWN) ---
    if bypass_drop_pct > 0 and price_move_pct <= -abs(bypass_drop_pct):
        result["bypass_reason"] = (
            f"Cooldown bypassed: BTC dropped {price_move_pct:.2f}% from last buy "
            f"(${last_buy_price:,.0f} -> ${current_price:,.0f}), "
            f"exceeding bypass_if_price_drop_from_last_buy_pct threshold of -{bypass_drop_pct:.1f}%. "
            f"Discount is too significant to ignore."
        )
        return result

    # --- Standard blocking logic (unchanged) ---
    meaningful_pullback = price_move_pct <= -abs(require_price_move_pct)

    if same_signal and hours_since < cooldown_hours:
        result["blocked"] = True
        result["reason"] = (
            f"Manual buy anti-repeat: {candidate_action_key} was already executed "
            f"{hours_since:.1f}h ago. Cooldown is {cooldown_hours:.1f}h. "
            f"Price move from last buy: {price_move_pct:.2f}% "
            f"(bypass requires >{bypass_rise_pct:.1f}% up or "
            f">{bypass_drop_pct:.1f}% down or tier upgrade)."
        )
        return result

    if same_tier and not meaningful_pullback:
        result["blocked"] = True
        result["reason"] = (
            f"Manual buy anti-repeat: last filled manual buy was also tier "
            f"{last_tier}. Current price move from last buy is {price_move_pct:.2f}%, "
            f"but same-tier repeat requires at least -{require_price_move_pct:.2f}% pullback "
            f"or tier upgrade or significant breakout/crash bypass."
        )
        return result

    return result

def get_active_manual_pullback_orders(config):
    manual_exec_cfg = config.get("manual_executions", {})
    entries = manual_exec_cfg.get("entries", [])

    if not manual_exec_cfg.get("enabled", False):
        return []

    if not isinstance(entries, list):
        return []

    active = []

    for entry in entries:
        status = str(entry.get("status", "")).lower()
        side = str(entry.get("side", "")).lower()
        source_signal_key = str(entry.get("source_signal_key", ""))

        if status != "open":
            continue

        if side != "buy":
            continue

        if source_signal_key != "PLACE_PULLBACK_LIMIT_BUY":
            continue

        active.append(entry)

    return active


def get_highest_open_buy_order_price(config):
    manual_orders_cfg = config.get("manual_open_orders", {})

    if not manual_orders_cfg.get("enabled", False):
        return 0.0

    prices = []

    for order in manual_orders_cfg.get("orders", []):
        if order.get("status") != "open":
            continue

        side = str(order.get("side", "")).lower()
        if side != "buy":
            continue

        price = float(order.get("price", 0) or 0)
        if price > 0:
            prices.append(price)

    if not prices:
        return 0.0

    return max(prices)


def calculate_base_pct_after_limit_buy(portfolio, current_price, limit_price, buy_usdt):
    base_now = float(portfolio.get("base_free", 0)) + float(portfolio.get("base_used", 0))
    total_value = float(portfolio.get("total_value", 0) or 0)

    if current_price <= 0 or limit_price <= 0 or total_value <= 0 or buy_usdt <= 0:
        return 0.0

    extra_base = buy_usdt / limit_price
    base_value_after_fill = (base_now + extra_base) * current_price

    return base_value_after_fill / total_value * 100


def calculate_planned_base_exposure_pct_for_limit_buy(config, portfolio, current_price, limit_price, buy_usdt):
    manual_orders_cfg = config.get("manual_open_orders", {})
    open_order_base = 0.0

    if manual_orders_cfg.get("enabled", False):
        for order in manual_orders_cfg.get("orders", []):
            if order.get("status") != "open":
                continue

            side = str(order.get("side", "")).lower()
            if side != "buy":
                continue

            amount = float(order.get("amount", 0) or 0)
            open_order_base += amount

    extra_base = 0.0
    if limit_price > 0 and buy_usdt > 0:
        extra_base = buy_usdt / limit_price

    planned_base = float(portfolio.get(config.get("asset", {}).get("base", "BTC").lower(), 0) or 0) + open_order_base + extra_base
    planned_base_value = planned_base * current_price

    total_value = float(portfolio.get("total_value", 0) or 0)
    if total_value <= 0:
        return 0.0

    return planned_base_value / total_value * 100


def build_pullback_limit_candidate_action(
    config,
    market,
    portfolio,
    open_orders,
    exposure_tier,
    has_open_orders,
    anti_repeat=None,
):
    """
    Build adaptive manual pullback limit candidate.

    This does not place any order.
    It only allows Gemini to recommend a manual limit order below current price.

    Purpose:
    - Make recommendations more adaptive when old ladder orders are far below current price.
    - Avoid repeated immediate buys.
    - Avoid chasing by requiring a real pullback limit below current price.
    """
    pullback_cfg = config.get("adaptive_pullback_limit", {})

    if not pullback_cfg.get("enabled", False):
        return None

    if not pullback_cfg.get("manual_only", True):
        return None

    if has_open_orders and not pullback_cfg.get("allow_with_open_orders", True):
        return None

    if not exposure_tier or not exposure_tier.get("can_buy", False):
        return None

    action_key = pullback_cfg.get("action_key", "PLACE_PULLBACK_LIMIT_BUY")

    active_pullback_orders = get_active_manual_pullback_orders(config)

    if pullback_cfg.get("block_if_active_pullback_order_exists", True):
        max_active = int(pullback_cfg.get("max_active_pullback_orders", 1) or 1)

        if len(active_pullback_orders) >= max_active:
            return {
                "key": "HOLD_ACTIVE_PULLBACK_LIMIT_EXISTS",
                "type": "hold",
                "signal": "HOLD / ACTIVE PULLBACK LIMIT EXISTS",
                "max_buy_usdt": 0,
                "max_sell_base_pct_of_holdings": 0,
                "active_pullback_order_count": len(active_pullback_orders),
                "reason": (
                    f"Adaptive pullback limit is blocked because {len(active_pullback_orders)} "
                    f"active pullback limit order already exists in manual_executions."
                ),
            }

    price = float(market.get("price", 0) or 0)
    if price <= 0:
        return None

    tier_name = str(exposure_tier.get("name", ""))
    offset_cfg = pullback_cfg.get("limit_offset_pct_from_current", {})

    if isinstance(offset_cfg, dict):
        offset_pct = float(offset_cfg.get(tier_name, offset_cfg.get("early_confirmation", 1.25)) or 1.25)
    else:
        offset_pct = float(offset_cfg or 1.25)

    min_offset = float(pullback_cfg.get("min_limit_offset_pct_from_current", 0.75) or 0.75)
    max_offset = float(pullback_cfg.get("max_limit_offset_pct_from_current", 3.00) or 3.00)

    offset_pct = max(min_offset, min(offset_pct, max_offset))

    suggested_limit_price = price * (1 - offset_pct / 100)

    latest_buy = get_latest_filled_manual_buy(config)
    required_below_last_buy_pct = float(
        pullback_cfg.get("require_limit_below_last_manual_buy_pct", 2.0) or 2.0
    )

    if latest_buy:
        last_buy_price = float(latest_buy.get("_avg_fill_price_usdt", 0) or 0)

        if last_buy_price > 0:
            max_allowed_from_last_buy = last_buy_price * (1 - required_below_last_buy_pct / 100)

            if suggested_limit_price > max_allowed_from_last_buy:
                suggested_limit_price = max_allowed_from_last_buy

    highest_open_buy = get_highest_open_buy_order_price(config)
    min_gap_above_highest_open_buy_pct = float(
        pullback_cfg.get("min_gap_above_highest_open_buy_pct", 2.0) or 2.0
    )

    if highest_open_buy > 0:
        minimum_allowed_price = highest_open_buy * (1 + min_gap_above_highest_open_buy_pct / 100)

        if suggested_limit_price <= minimum_allowed_price:
            return {
                "key": "HOLD_PULLBACK_LIMIT_TOO_CLOSE_TO_OLD_LADDER",
                "type": "hold",
                "signal": "HOLD / PULLBACK LIMIT TOO CLOSE TO OLD LADDER",
                "max_buy_usdt": 0,
                "max_sell_base_pct_of_holdings": 0,
                "highest_open_buy_price": highest_open_buy,
                "minimum_allowed_price": minimum_allowed_price,
                "suggested_limit_price": suggested_limit_price,
                "reason": (
                    f"Adaptive pullback limit is blocked because suggested limit "
                    f"${suggested_limit_price:,.0f} is too close to existing highest ladder "
                    f"${highest_open_buy:,.0f}. Minimum allowed is ${minimum_allowed_price:,.0f}."
                ),
            }

    if has_open_orders:
        order_usdt = float(pullback_cfg.get("max_order_usdt_with_open_orders", 15) or 15)
    else:
        order_usdt = float(pullback_cfg.get("max_order_usdt_without_open_orders", 20) or 20)

    if order_usdt <= 0:
        return None

    min_free = float(
        pullback_cfg.get(
            "min_usdt_free_after_order",
            config.get("gemini_decision", {}).get("min_usdt_free_after_buy", 150),
        ) or 150
    )

    if portfolio["usdt_free"] - order_usdt < min_free:
        return {
            "key": "HOLD_PULLBACK_LIMIT_BLOCKED_BY_RESERVE",
            "type": "hold",
            "signal": "HOLD / PULLBACK LIMIT BLOCKED BY RESERVE",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "reason": (
                f"Adaptive pullback limit is blocked because placing {order_usdt:.2f} USDT "
                f"would reduce USDT free balance below reserve {min_free:.2f}."
            ),
        }

    upside_pct_after_fill = calculate_base_pct_after_limit_buy(
        portfolio=portfolio,
        current_price=price,
        limit_price=suggested_limit_price,
        buy_usdt=order_usdt,
    )

    downside_planned_pct_after_fill = calculate_planned_base_exposure_pct_for_limit_buy(
        config=config,
        portfolio=portfolio,
        current_price=price,
        limit_price=suggested_limit_price,
        buy_usdt=order_usdt,
    )

    tier_settings = exposure_tier.get("settings", {})

    max_upside_pct = float(
        pullback_cfg.get(
            "max_upside_base_pct_after_fill",
            tier_settings.get("max_upside_base_pct_after_buy", 35),
        ) or 35
    )

    max_downside_pct = float(
        pullback_cfg.get(
            "max_downside_planned_base_pct_after_fill",
            tier_settings.get("max_downside_planned_base_pct", 55),
        ) or 55
    )

    if upside_pct_after_fill > max_upside_pct:
        return {
            "key": "HOLD_PULLBACK_LIMIT_BLOCKED_BY_UPSIDE_EXPOSURE",
            "type": "hold",
            "signal": "HOLD / PULLBACK LIMIT BLOCKED BY UPSIDE EXPOSURE",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "upside_base_pct_after_fill": upside_pct_after_fill,
            "max_upside_base_pct_after_fill": max_upside_pct,
            "reason": (
                f"Adaptive pullback limit is blocked because BTC allocation after fill "
                f"would be {upside_pct_after_fill:.1f}%, above cap {max_upside_pct:.1f}%."
            ),
        }

    if downside_planned_pct_after_fill > max_downside_pct:
        return {
            "key": "HOLD_PULLBACK_LIMIT_BLOCKED_BY_DOWNSIDE_PLANNED_EXPOSURE",
            "type": "hold",
            "signal": "HOLD / PULLBACK LIMIT BLOCKED BY DOWNSIDE PLANNED EXPOSURE",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "downside_planned_base_pct_after_fill": downside_planned_pct_after_fill,
            "max_downside_planned_base_pct_after_fill": max_downside_pct,
            "reason": (
                f"Adaptive pullback limit is blocked because downside planned BTC allocation "
                f"would be {downside_planned_pct_after_fill:.1f}%, above cap {max_downside_pct:.1f}%."
            ),
        }

    min_confidence = int(
        pullback_cfg.get(
            "min_confidence_to_place",
            tier_settings.get("min_confidence_to_buy", config.get("gemini_decision", {}).get("min_confidence_to_buy", 80)),
        )
    )

    estimated_base_if_filled = order_usdt / suggested_limit_price if suggested_limit_price > 0 else 0

    return {
        "key": action_key,
        "type": "buy",
        "order_style": "pullback_limit",
        "signal": "PLACE MANUAL LIMIT BUY / PULLBACK ENTRY",
        "max_buy_usdt": order_usdt,
        "max_sell_base_pct_of_holdings": 0,
        "recommended_order_type": "limit",
        "recommended_limit_price": suggested_limit_price,
        "pullback_offset_pct_from_current": ((price - suggested_limit_price) / price * 100),
        "estimated_base_if_filled": estimated_base_if_filled,
        "expiry_guidance_hours": int(pullback_cfg.get("expiry_guidance_hours", 24) or 24),
        "exposure_tier": tier_name,
        "tier_reason": exposure_tier.get("reason"),
        "min_confidence_to_buy": min_confidence,
        "max_upside_base_pct_after_buy": max_upside_pct,
        "max_downside_planned_base_pct": max_downside_pct,
        "upside_base_pct_after_buy": upside_pct_after_fill,
        "downside_planned_base_pct_after_buy_and_open_orders": downside_planned_pct_after_fill,
        "reason": (
            f"Adaptive pullback limit is allowed under {tier_name} tier. "
            f"Place a manual limit buy only if BTC pulls back to approximately "
            f"${suggested_limit_price:,.0f}. "
            f"Upside BTC allocation after fill: {upside_pct_after_fill:.1f}% / cap {max_upside_pct:.1f}%. "
            f"Downside planned BTC allocation if open orders fill: {downside_planned_pct_after_fill:.1f}% / cap {max_downside_pct:.1f}%."
        ),
    }

def calculate_base_pct_after_sell(portfolio, market_price, sell_btc):
    base_now = float(portfolio.get("base_free", 0)) + float(portfolio.get("base_used", 0))
    usdt_now = float(portfolio.get("usdt", 0) or 0)

    remaining_btc = max(base_now - sell_btc, 0)
    sell_value = sell_btc * market_price

    estimated_usdt = usdt_now + sell_value
    estimated_base_value = remaining_btc * market_price
    estimated_total_value = estimated_usdt + estimated_base_value

    if estimated_total_value <= 0:
        return 0.0

    return estimated_base_value / estimated_total_value * 100


def calculate_sell_btc_for_target_pct(portfolio, market_price, target_base_pct):
    """
    Estimate BTC amount to sell to reach target BTC allocation.
    This is approximate and ignores fees/slippage.
    """
    base_now = float(portfolio.get("base_free", 0)) + float(portfolio.get("base_used", 0))
    usdt_now = float(portfolio.get("usdt", 0) or 0)

    if base_now <= 0 or market_price <= 0:
        return 0.0

    target_fraction = target_base_pct / 100
    if target_fraction <= 0 or target_fraction >= 1:
        return 0.0

    # After selling x BTC:
    # BTC value = (base_now - x) * price
    # USDT = usdt_now + x * price
    # total value remains approximately constant before fees
    total_value = usdt_now + base_now * market_price
    target_base_value = total_value * target_fraction
    target_btc = target_base_value / market_price

    sell_btc = base_now - target_btc
    return max(sell_btc, 0.0)


def calculate_base_cost_basis_from_manual_lots(config, portfolio):
    """
    Calculate BTC weighted average entry from manual lots in config.

    This is safer than asking Gemini to guess.
    Gemini should only analyze the computed result.
    """
    cost_cfg = config.get("base_cost_basis", {})
    asset = get_asset_config(config)
    base = asset.get("base", "BTC")

    result = {
        "available": False,
        "method": cost_cfg.get("method", "manual_lots"),
        "avg_entry_price": 0.0,
        "covered_btc": 0.0,
        "portfolio_btc": float(portfolio.get(base.lower(), 0) or 0),
        "coverage_pct": 0.0,
        "min_coverage_pct": float(cost_cfg.get("min_coverage_pct", 90) or 90),
        "total_cost_usdt": 0.0,
        "message": "",
    }

    if not cost_cfg.get("enabled", False):
        result["message"] = f"{base} cost basis disabled."
        return result

    lots = cost_cfg.get("manual_lots", [])
    if not lots:
        result["message"] = f"No manual {base} lots configured."
        return result

    total_btc = 0.0
    total_cost = 0.0

    for lot in lots:
        side = str(lot.get("side", "")).lower()
        if side != "buy":
            continue

        amount_base = float(lot.get("amount_base", 0) or 0)
        price_usdt = float(lot.get("price_usdt", 0) or 0)
        fee_base = float(lot.get("fee_base", 0) or 0)

        if amount_base <= 0 or price_usdt <= 0:
            continue

        net_btc = max(amount_base - fee_base, 0)
        if net_btc <= 0:
            continue

        total_btc += net_btc
        total_cost += amount_base * price_usdt

    portfolio_base = result["portfolio_btc"]

    if portfolio_base <= 0:
        result["message"] = f"Portfolio {base} is zero."
        return result

    if total_btc <= 0 or total_cost <= 0:
        result["message"] = "Manual lots do not contain valid buy entries."
        return result

    avg_entry = total_cost / total_btc
    coverage_pct = min((total_btc / portfolio_base) * 100, 100)

    result["avg_entry_price"] = avg_entry
    result["covered_btc"] = total_btc
    result["coverage_pct"] = coverage_pct
    result["total_cost_usdt"] = total_cost

    if coverage_pct < result["min_coverage_pct"]:
        result["available"] = False
        result["message"] = (
            f"{base} cost basis coverage is {coverage_pct:.1f}%, below required "
            f"{result['min_coverage_pct']:.1f}%."
        )
        return result

    result["available"] = True
    result["message"] = (
        f"{base} cost basis available from manual lots. "
        f"Coverage: {coverage_pct:.1f}%, avg entry: {avg_entry:.2f} USDT."
    )
    return result


def build_sell_candidate_actions(config, market, portfolio):
    """
    Build sell-related candidate actions.
    Sell is intentionally conservative:
    - take-profit sell requires computed cost basis from manual lots,
    - rebalance sell requires BTC overweight,
    - risk-reduction sell is disabled by default.
    """
    sell_cfg = config.get("sell_strategy", {})

    actions = []

    if not sell_cfg.get("enabled", False):
        return actions

    base_pct = float(portfolio.get("base_pct", 0) or 0)
    base_now = float(portfolio.get(config.get("asset", {}).get("base", "BTC").lower(), 0) or 0)
    price = float(market.get("price", 0) or 0)

    if base_now <= 0 or price <= 0:
        return actions

    cost_basis = calculate_base_cost_basis_from_manual_lots(
        config=config,
        portfolio=portfolio,
    )

    min_base_pct_after_sell = float(sell_cfg.get("min_base_pct_after_sell", 50))
    max_single_sell_pct = float(sell_cfg.get("max_single_sell_base_pct_of_holdings", 25))

    if base_pct < min_base_pct_after_sell:
        actions.append({
            "key": "HOLD_DO_NOT_SELL_UNDERALLOCATED",
            "type": "hold",
            "signal": "HOLD / DO NOT SELL UNDERALLOCATED",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "cost_basis": cost_basis,
            "reason": (
                "Base allocation is below the minimum BTC allocation allowed after sell. "
                "Selling now may slow recovery."
            ),
        })
        return actions

    if sell_cfg.get("allow_take_profit_sell", False):
        tp_cfg = sell_cfg.get("take_profit", {})

        if tp_cfg.get("enabled", False) and cost_basis.get("available", False):
            avg_entry = float(cost_basis.get("avg_entry_price", 0) or 0)

            if avg_entry > 0:
                profit_pct = ((price - avg_entry) / avg_entry) * 100
                min_profit_pct = float(tp_cfg.get("min_profit_pct_from_avg_entry", 8))

                if profit_pct >= min_profit_pct:
                    sell_pct = min(
                        float(tp_cfg.get("sell_pct_of_holdings", 20)),
                        max_single_sell_pct,
                    )

                    sell_btc = base_now * (sell_pct / 100)
                    base_pct_after_sell = calculate_base_pct_after_sell(
                        portfolio=portfolio,
                        market_price=price,
                        sell_btc=sell_btc,
                    )

                    if base_pct_after_sell >= min_base_pct_after_sell:
                        actions.append({
                            "key": "SELL_SMALL_TAKE_PROFIT",
                            "type": "sell",
                            "signal": "SELL SMALL / TAKE PROFIT",
                            "max_buy_usdt": 0,
                            "max_sell_base_pct_of_holdings": sell_pct,
                            "estimated_sell_btc": sell_btc,
                            "estimated_sell_usdt": sell_btc * price,
                            "estimated_base_pct_after_sell": base_pct_after_sell,
                            "cost_basis": cost_basis,
                            "profit_pct_from_avg_entry": profit_pct,
                            "reason": (
                                f"{base} is approximately {profit_pct:.1f}% above computed average entry. "
                                "Small planned take-profit is allowed."
                            ),
                        })

    if sell_cfg.get("allow_rebalance_sell", False):
        rb_cfg = sell_cfg.get("rebalance", {})

        if rb_cfg.get("enabled", False):
            sell_if_above = float(rb_cfg.get("sell_if_base_pct_above", 75))
            target_after = float(rb_cfg.get("target_base_pct_after_sell", 65))

            if base_pct > sell_if_above:
                sell_btc = calculate_sell_btc_for_target_pct(
                    portfolio=portfolio,
                    market_price=price,
                    target_base_pct=target_after,
                )

                max_sell_btc = base_now * (max_single_sell_pct / 100)
                sell_btc = min(sell_btc, max_sell_btc)

                if sell_btc > 0:
                    sell_pct = (sell_btc / base_now) * 100
                    base_pct_after_sell = calculate_base_pct_after_sell(
                        portfolio=portfolio,
                        market_price=price,
                        sell_btc=sell_btc,
                    )

                    if base_pct_after_sell >= min_base_pct_after_sell:
                        actions.append({
                            "key": "SELL_SMALL_REBALANCE_OVERWEIGHT",
                            "type": "sell",
                            "signal": "SELL SMALL / REBALANCE BTC OVERWEIGHT",
                            "max_buy_usdt": 0,
                            "max_sell_base_pct_of_holdings": sell_pct,
                            "estimated_sell_btc": sell_btc,
                            "estimated_sell_usdt": sell_btc * price,
                            "estimated_base_pct_after_sell": base_pct_after_sell,
                            "cost_basis": cost_basis,
                            "reason": (
                                f"Base allocation is {base_pct:.1f}%, above rebalance threshold "
                                f"{sell_if_above:.1f}%."
                            ),
                        })

    if sell_cfg.get("allow_risk_reduction_sell", False):
        rr_cfg = sell_cfg.get("risk_reduction", {})

        if rr_cfg.get("enabled", False):
            ma20 = float(market.get("ma_20", 0) or 0)
            below_ma20_pct = float(rr_cfg.get("sell_if_price_below_ma20_pct", 3))
            threshold_price = ma20 * (1 - below_ma20_pct / 100)

            if ma20 > 0 and price < threshold_price:
                sell_pct = min(
                    float(rr_cfg.get("sell_pct_of_holdings", 15)),
                    max_single_sell_pct,
                )

                sell_btc = base_now * (sell_pct / 100)
                base_pct_after_sell = calculate_base_pct_after_sell(
                    portfolio=portfolio,
                    market_price=price,
                    sell_btc=sell_btc,
                )

                if base_pct_after_sell >= min_base_pct_after_sell:
                    actions.append({
                        "key": "SELL_SMALL_RISK_REDUCTION",
                        "type": "sell",
                        "signal": "SELL SMALL / RISK REDUCTION",
                        "max_buy_usdt": 0,
                        "max_sell_base_pct_of_holdings": sell_pct,
                        "estimated_sell_btc": sell_btc,
                        "estimated_sell_usdt": sell_btc * price,
                        "estimated_base_pct_after_sell": base_pct_after_sell,
                        "cost_basis": cost_basis,
                        "reason": (
                            f"{base} is below the configured MA20 breakdown threshold. "
                            "Small risk-reduction sell is allowed."
                        ),
                    })

    return actions

def build_gemini_candidate_actions(config, market, portfolio, open_orders, intrahour_order_events):
    """
    Build the exact action menu Gemini is allowed to choose from.
    Gemini must not invent actions outside this list.

    Debug note:
    BUY_GATE prints are intentionally only written to GitHub Actions logs.
    They do not affect Telegram message or trading decision.
    """
    decision_cfg = config.get("gemini_decision", {})
    asset = get_asset_config(config)
    base = asset.get("base", "BTC")

    actions = [
        {
            "key": "HOLD",
            "type": "hold",
            "signal": "HOLD",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "reason": "Default safe action.",
        }
    ]

    print(
        "[BUY_GATE] start | "
        f"enabled={decision_cfg.get('enabled', False)} | "
        f"allowed_to_recommend_buy={decision_cfg.get('allowed_to_recommend_buy', False)} | "
        f"price={market.get('price')} | "
        f"ma7={market.get('ma_7')} | "
        f"ma20={market.get('ma_20')} | "
        f"change_24h={market.get('change_24h')} | "
        f"base_pct={portfolio.get('base_pct')} | "
        f"usdt_free={portfolio.get('usdt_free')}",
        flush=True,
    )

    if not decision_cfg.get("enabled", False):
        print("[BUY_GATE] blocked: gemini_decision disabled", flush=True)
        return actions

    if (
        decision_cfg.get("block_buy_if_possible_fill_detected", True)
        and intrahour_order_events
        and intrahour_order_events.get("has_possible_fill", False)
    ):
        print("[BUY_GATE] blocked: possible fill detected", flush=True)

        actions.append({
            "key": "HOLD_VERIFY_POSSIBLE_FILL",
            "type": "hold",
            "signal": "HOLD / VERIFY POSSIBLE FILLED ORDER",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "reason": "Possible order touch detected. Verify Tokocrypto before any new decision.",
        })
        return actions

    if portfolio["base_pct"] >= config["portfolio"]["target_base_max_pct"]:
        print(
            "[BUY_GATE] note: base_pct already above target max | "
            f"base_pct={portfolio['base_pct']:.2f} | "
            f"target_max={config['portfolio']['target_base_max_pct']}",
            flush=True,
        )

        actions.append({
            "key": "HOLD_TOO_MUCH_BTC",
            "type": "hold",
            "signal": "HOLD / TOO MUCH BTC",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "reason": "Base allocation is already above target max.",
        })

    has_open_orders = bool(open_orders)

    print(
        "[BUY_GATE] open orders | "
        f"has_open_orders={has_open_orders} | "
        f"open_order_count={len(open_orders or [])}",
        flush=True,
    )

    if has_open_orders:
        actions.append({
            "key": "HOLD_OPEN_ORDERS_ACTIVE",
            "type": "hold",
            "signal": "HOLD / OPEN ORDERS ACTIVE",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "reason": "Open buy orders are already active. Avoid accidental double entry unless bullish confirmation is strong.",
        })

        actions.append({
            "key": "HOLD_REVIEW_LADDER",
            "type": "hold",
            "signal": "HOLD / REVIEW LADDER",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "reason": "BTC may be improving, but existing downside ladder should be reviewed before adding exposure.",
        })

    sell_actions = build_sell_candidate_actions(
        config=config,
        market=market,
        portfolio=portfolio,
    )
    actions.extend(sell_actions)

    if not decision_cfg.get("allowed_to_recommend_buy", False):
        print("[BUY_GATE] blocked: allowed_to_recommend_buy=false", flush=True)
        return actions

    if decision_cfg.get("block_buy_if_panic_or_dump", True):
        if market["change_24h"] <= config["risk"]["dump_24h_pct"]:
            print(
                "[BUY_GATE] blocked: daily dump / panic guard | "
                f"change_24h={market['change_24h']:.2f} | "
                f"dump_threshold={config['risk']['dump_24h_pct']}",
                flush=True,
            )
            return actions

    if decision_cfg.get("require_btc_below_target_min", True):
        if portfolio["base_pct"] >= config["portfolio"]["target_base_min_pct"]:
            print(
                "[BUY_GATE] blocked: base_pct already >= target min | "
                f"base_pct={portfolio['base_pct']:.2f} | "
                f"target_min={config['portfolio']['target_base_min_pct']}",
                flush=True,
            )
            return actions

    if has_open_orders and not decision_cfg.get("allow_buy_with_open_orders", False):
        print("[BUY_GATE] blocked: open orders active and allow_buy_with_open_orders=false", flush=True)
        return actions

    exposure_tier = get_gemini_exposure_tier(
        config=config,
        market=market,
        has_open_orders=has_open_orders,
    )

    print(
        "[BUY_GATE] exposure tier | "
        f"tier={exposure_tier.get('name') if exposure_tier else None} | "
        f"can_buy={exposure_tier.get('can_buy') if exposure_tier else None} | "
        f"reason={exposure_tier.get('reason') if exposure_tier else None}",
        flush=True,
    )

    if not exposure_tier:
        print("[BUY_GATE] blocked: no exposure tier selected", flush=True)
        return actions

    if not exposure_tier.get("can_buy", False):
        print(
            "[BUY_GATE] blocked: exposure tier cannot buy | "
            f"tier={exposure_tier.get('name')} | "
            f"reason={exposure_tier.get('reason')}",
            flush=True,
        )

        actions.append({
            "key": "HOLD_OVERHEATED_NO_FOMO",
            "type": "hold",
            "signal": "HOLD / OVERHEATED NO FOMO",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "exposure_tier": exposure_tier.get("name"),
            "reason": exposure_tier.get("reason", f"{base} is overheated. Fresh buy blocked."),
        })
        return actions

    max_buy = get_tiered_max_buy_usdt(
        exposure_tier=exposure_tier,
        has_open_orders=has_open_orders,
    )

    print(
        "[BUY_GATE] tier max buy | "
        f"tier={exposure_tier.get('name')} | "
        f"max_buy={max_buy}",
        flush=True,
    )

    if max_buy <= 0:
        print("[BUY_GATE] blocked: max_buy <= 0", flush=True)
        return actions

    min_free = float(decision_cfg.get("min_usdt_free_after_buy", 150))
    usdt_free_after_buy = portfolio["usdt_free"] - max_buy

    print(
        "[BUY_GATE] reserve check | "
        f"usdt_free_after_buy={usdt_free_after_buy:.2f} | "
        f"min_free={min_free:.2f}",
        flush=True,
    )

    if usdt_free_after_buy < min_free:
        print("[BUY_GATE] blocked: reserve would be violated", flush=True)
        return actions

    tier_settings = exposure_tier.get("settings", {})

    upside_pct = calculate_upside_base_pct_after_buy(
        portfolio=portfolio,
        market_price=market["price"],
        buy_usdt=max_buy,
    )

    downside_planned_pct = calculate_planned_base_exposure_pct(
        config=config,
        portfolio=portfolio,
        market_price=market["price"],
        extra_buy_usdt=max_buy,
    )

    max_upside_pct = float(tier_settings.get("max_upside_base_pct_after_buy", 35))
    max_downside_pct = float(tier_settings.get("max_downside_planned_base_pct", 55))
    tier_min_confidence = int(tier_settings.get("min_confidence_to_buy", decision_cfg.get("min_confidence_to_buy", 80)))

    print(
        "[BUY_GATE] exposure check | "
        f"upside_pct={upside_pct:.2f} | "
        f"max_upside_pct={max_upside_pct:.2f} | "
        f"downside_planned_pct={downside_planned_pct:.2f} | "
        f"max_downside_pct={max_downside_pct:.2f} | "
        f"tier_min_confidence={tier_min_confidence}",
        flush=True,
    )

    if upside_pct > max_upside_pct:
        return actions

    if downside_planned_pct > max_downside_pct:
        return actions

    anti_repeat = evaluate_manual_buy_anti_repeat(
        config=config,
        market=market,
        exposure_tier=exposure_tier,
        candidate_action_key="BUY_SMALL_CONFIRMATION_WITH_LADDER",
    )

    if anti_repeat.get("blocked", False):
        print(
            "[BUY_GATE] blocked by manual execution anti-repeat | "
            f"reason={anti_repeat.get('reason')}",
            flush=True,
        )

        actions.append({
            "key": "HOLD_RECENT_MANUAL_BUY_ANTI_REPEAT",
            "type": "hold",
            "signal": "HOLD / RECENT MANUAL BUY ANTI-REPEAT",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "latest_manual_buy": anti_repeat.get("latest_buy"),
            "hours_since_last_buy": anti_repeat.get("hours_since_last_buy"),
            "price_move_pct_from_last_buy": anti_repeat.get("price_move_pct_from_last_buy"),
            "reason": anti_repeat.get("reason"),
        })

        pullback_action = build_pullback_limit_candidate_action(
            config=config,
            market=market,
            portfolio=portfolio,
            open_orders=open_orders,
            exposure_tier=exposure_tier,
            has_open_orders=has_open_orders,
            anti_repeat=anti_repeat,
        )

        if pullback_action:
            print(
                "[BUY_GATE] adaptive pullback candidate after anti-repeat | "
                f"key={pullback_action.get('key')} | "
                f"limit={pullback_action.get('recommended_limit_price')}",
                flush=True,
            )
            actions.append(pullback_action)

        return actions

    actions.append({
        "key": "BUY_SMALL_CONFIRMATION_WITH_LADDER",
        "type": "buy",
        "signal": "BUY SMALL / BULLISH CONFIRMATION WITH LADDER ACTIVE",
        "max_buy_usdt": max_buy,
        "max_sell_base_pct_of_holdings": 0,
        "exposure_tier": exposure_tier.get("name"),
        "tier_reason": exposure_tier.get("reason"),
        "min_confidence_to_buy": tier_min_confidence,
        "max_upside_base_pct_after_buy": max_upside_pct,
        "max_downside_planned_base_pct": max_downside_pct,
        "upside_base_pct_after_buy": upside_pct,
        "downside_planned_base_pct_after_buy_and_open_orders": downside_planned_pct,
        "reason": (
            f"Tiered exposure policy allows a small buy under {exposure_tier.get('name')} tier. "
            f"Upside BTC allocation after buy: {upside_pct:.1f}% / cap {max_upside_pct:.1f}%. "
            f"Downside planned BTC allocation if open orders fill: {downside_planned_pct:.1f}% / cap {max_downside_pct:.1f}%."
        ),
    })

    pullback_action = build_pullback_limit_candidate_action(
        config=config,
        market=market,
        portfolio=portfolio,
        open_orders=open_orders,
        exposure_tier=exposure_tier,
        has_open_orders=has_open_orders,
        anti_repeat=anti_repeat,
    )

    if pullback_action:
        print(
            "[BUY_GATE] adaptive pullback candidate added | "
            f"key={pullback_action.get('key')} | "
            f"limit={pullback_action.get('recommended_limit_price')}",
            flush=True,
        )
        actions.append(pullback_action)

    return actions


def apply_gemini_decision_with_guards(
    config,
    base_decision,
    gemini_review,
    candidate_actions,
    market,
    portfolio,
):
    """
    Apply Gemini recommendation only if config allows final-decision override
    and deterministic guards approve it.
    """
    decision_cfg = config.get("gemini_decision", {})

    if not decision_cfg.get("enabled", False):
        return base_decision

    if not decision_cfg.get("affect_final_decision", False):
        return base_decision

    if not gemini_review or not gemini_review.get("available", False):
        return base_decision

    action_key = gemini_review.get("recommended_action_key", "HOLD")
    confidence = int(float(gemini_review.get("confidence_score", 0) or 0))
    requested_buy = float(gemini_review.get("recommended_buy_usdt", 0) or 0)
    requested_sell_pct = float(gemini_review.get("recommended_sell_base_pct_of_holdings", 0) or 0)

    action_map = {
        action["key"]: action
        for action in candidate_actions
    }

    if action_key not in action_map:
        return {
            "signal": "HOLD / GEMINI ACTION REJECTED",
            "action_usdt": 0,
            "reason": "Gemini recommended an action outside allowed candidate actions.",
        }

    selected_action = action_map[action_key]
    action_type = selected_action.get("type", "hold")

    if action_type == "hold":
        return {
            "signal": selected_action["signal"],
            "action_usdt": 0,
            "reason": selected_action.get("reason", "Gemini selected a non-buy/non-sell action."),
        }

    if action_type == "buy":
        max_buy = float(selected_action.get("max_buy_usdt", 0) or 0)

        if max_buy <= 0:
            return base_decision

        min_confidence = int(selected_action.get(
            "min_confidence_to_buy",
            decision_cfg.get("min_confidence_to_buy", 80),
        ))

        if confidence < min_confidence:
            return {
                "signal": "HOLD / GEMINI BUY CONFIDENCE TOO LOW",
                "action_usdt": 0,
                "reason": (
                    f"Gemini buy confidence {confidence}/100 is below required "
                    f"{min_confidence}/100 for this exposure tier."
                ),
            }

        buy_usdt = min(requested_buy, max_buy)

        if buy_usdt <= 0:
            buy_usdt = max_buy

        min_free = float(decision_cfg.get("min_usdt_free_after_buy", 150))
        if portfolio["usdt_free"] - buy_usdt < min_free:
            return {
                "signal": "HOLD / GEMINI BUY BLOCKED BY RESERVE",
                "action_usdt": 0,
                "reason": "Buying would reduce USDT free balance below emergency reserve.",
            }

        upside_pct = calculate_upside_base_pct_after_buy(
            portfolio=portfolio,
            market_price=market["price"],
            buy_usdt=buy_usdt,
        )

        downside_planned_pct = calculate_planned_base_exposure_pct(
            config=config,
            portfolio=portfolio,
            market_price=market["price"],
            extra_buy_usdt=buy_usdt,
        )

        max_upside_pct = float(selected_action.get("max_upside_base_pct_after_buy", 35))
        max_downside_pct = float(selected_action.get("max_downside_planned_base_pct", 55))

        if upside_pct > max_upside_pct:
            return {
                "signal": "HOLD / GEMINI BUY BLOCKED BY UPSIDE EXPOSURE",
                "action_usdt": 0,
                "reason": (
                    f"Upside BTC allocation after buy would become {upside_pct:.1f}%, "
                    f"above tier cap {max_upside_pct:.1f}%."
                ),
            }

        if downside_planned_pct > max_downside_pct:
            return {
                "signal": "HOLD / GEMINI BUY BLOCKED BY DOWNSIDE PLANNED EXPOSURE",
                "action_usdt": 0,
                "reason": (
                    f"Downside planned BTC allocation if open orders fill would become "
                    f"{downside_planned_pct:.1f}%, above tier cap {max_downside_pct:.1f}%."
                ),
            }

        decision_result = {
            "signal": selected_action["signal"],
            "action_usdt": buy_usdt,
            "reason": (
                f"Gemini selected {action_key} with {confidence}/100 confidence. "
                f"Risk guard approved under {selected_action.get('exposure_tier', 'unknown')} tier. "
                f"Upside BTC allocation after buy: {upside_pct:.1f}%. "
                f"Downside planned BTC allocation if open orders fill: {downside_planned_pct:.1f}%."
            ),
        }

        if selected_action.get("order_style") == "pullback_limit":
            limit_price = float(selected_action.get("recommended_limit_price", 0) or 0)
            estimated_base = float(selected_action.get("estimated_base_if_filled", 0) or 0)

            decision_result.update({
                "order_style": "pullback_limit",
                "recommended_order_type": "limit",
                "recommended_limit_price": limit_price,
                "estimated_base_if_filled": estimated_base,
                "expiry_guidance_hours": selected_action.get("expiry_guidance_hours"),
                "reason": (
                    f"Gemini selected {action_key} with {confidence}/100 confidence. "
                    f"Risk guard approved an adaptive manual pullback limit under "
                    f"{selected_action.get('exposure_tier', 'unknown')} tier. "
                    f"Recommended manual limit: {buy_usdt:.2f} USDT @ ${limit_price:,.0f}. "
                    f"Estimated BTC if filled: {estimated_base:.8f}. "
                    f"BTC allocation after fill: {upside_pct:.1f}%. "
                    f"Downside planned BTC allocation if all buy orders fill: {downside_planned_pct:.1f}%."
                ),
            })

        return decision_result

    if action_type == "sell":
        min_confidence = int(decision_cfg.get("min_confidence_to_sell", 80))
        if confidence < min_confidence:
            return {
                "signal": "HOLD / GEMINI SELL CONFIDENCE TOO LOW",
                "action_usdt": 0,
                "reason": (
                    f"Gemini sell confidence {confidence}/100 is below required "
                    f"{min_confidence}/100."
                ),
            }

        base_now = float(portfolio.get(config.get("asset", {}).get("base", "BTC").lower(), 0) or 0)
        if base_now <= 0:
            return {
                "signal": "HOLD / NO BTC TO SELL",
                "action_usdt": 0,
                "reason": "Portfolio BTC balance is zero or unavailable.",
            }

        max_sell_pct = float(selected_action.get("max_sell_base_pct_of_holdings", 0) or 0)
        sell_pct = min(requested_sell_pct, max_sell_pct)

        if sell_pct <= 0:
            sell_pct = max_sell_pct

        if sell_pct <= 0:
            return {
                "signal": "HOLD / GEMINI SELL BLOCKED",
                "action_usdt": 0,
                "reason": "Selected sell action has zero allowed sell size.",
            }

        sell_btc = base_now * (sell_pct / 100)
        sell_usdt_estimate = sell_btc * market["price"]

        sell_cfg = config.get("sell_strategy", {})
        min_base_pct_after_sell = float(sell_cfg.get("min_base_pct_after_sell", 50))

        base_pct_after_sell = calculate_base_pct_after_sell(
            portfolio=portfolio,
            market_price=market["price"],
            sell_btc=sell_btc,
        )

        if base_pct_after_sell < min_base_pct_after_sell:
            return {
                "signal": "HOLD / GEMINI SELL BLOCKED BY BTC ALLOCATION",
                "action_usdt": 0,
                "reason": (
                    f"BTC allocation after sell would become {base_pct_after_sell:.1f}%, "
                    f"below required minimum {min_base_pct_after_sell:.1f}%."
                ),
            }

        return {
            "signal": selected_action["signal"],
            "action_usdt": -sell_usdt_estimate,
            "sell_btc": sell_btc,
            "sell_usdt_estimate": sell_usdt_estimate,
            "sell_pct_of_holdings": sell_pct,
            "base_pct_after_sell": base_pct_after_sell,
            "reason": (
                f"Gemini selected {action_key} with {confidence}/100 confidence. "
                f"Risk guard approved. Estimated sell: {sell_btc:.8f} BTC "
                f"(~{sell_usdt_estimate:.2f} USDT). "
                f"Estimated BTC allocation after sell: {base_pct_after_sell:.1f}%."
            ),
        }

    return base_decision

def format_gemini_decision_text(config, gemini_review, candidate_actions, final_decision):
    decision_cfg = config.get("gemini_decision", {})

    if not decision_cfg.get("enabled", False):
        return "Gemini analysis: disabled"

    if not gemini_review or not gemini_review.get("available", False):
        error = ""
        if gemini_review:
            error = gemini_review.get("error", "")

        if error:
            return (
                "Status: unavailable\n"
                "Final decision source: guardrail baseline\n"
                f"Error: {error}"
            )

        return (
            "Status: unavailable\n"
            "Final decision source: guardrail baseline"
        )

    action_key = gemini_review.get("recommended_action_key", "HOLD")
    buy_usdt = float(gemini_review.get("recommended_buy_usdt", 0) or 0)
    sell_pct = float(gemini_review.get("recommended_sell_base_pct_of_holdings", 0) or 0)
    confidence = gemini_review.get("confidence_score", 0)

    mode = (
        "enabled - can affect final decision"
        if decision_cfg.get("affect_final_decision", False)
        else "advice only - cannot affect final decision"
    )

    selected_action = None
    for action in candidate_actions:
        if action.get("key") == action_key:
            selected_action = action
            break

    exposure_tier = "-"
    tier_reason = ""

    if selected_action:
        exposure_tier = selected_action.get("exposure_tier", "-")
        tier_reason = selected_action.get("tier_reason", "")

    lines = [
        f"Mode: {mode}",
        f"Selected action: {action_key}",
        f"Confidence: {confidence}/100",
        f"Recommended buy: {buy_usdt:.2f} USDT",
        f"Recommended sell: {sell_pct:.1f}% of BTC holdings",
        f"Exposure tier: {exposure_tier}",
    ]

    if tier_reason:
        lines.append(f"Tier reason: {tier_reason}")

    lines.append(f"Final signal after guard: {final_decision.get('signal')}")

    if not decision_cfg.get("affect_final_decision", False):
        lines.append("Note: Gemini is not changing the final decision because affect_final_decision=false.")

    return "\n".join(lines)


def format_decision_action(decision):
    signal = str(decision.get("signal", "")).upper()

    if "SELL" in signal and decision.get("sell_btc") is not None:
        return (
            f"Sell estimate: {decision.get('sell_btc', 0):.8f} BTC "
            f"(~{decision.get('sell_usdt_estimate', 0):.2f} USDT)"
        )

    if decision.get("order_style") == "pullback_limit":
        return (
            f"Limit buy: {decision.get('action_usdt', 0):.2f} USDT "
            f"@ ${float(decision.get('recommended_limit_price', 0) or 0):,.0f}"
        )

    return f"{decision.get('action_usdt', 0):.2f} USDT"


def format_ai_review_from_gemini_decision_json(ai_data):
    """
    Render Gemini decision JSON into Telegram-safe HTML.
    This replaces free-form Gemini HTML so one Gemini call can produce both
    the machine-readable recommendation and the analyst review.
    """
    defaults = {
        "agreement_with_rule": "cautious_agree",
        "confidence_score": 0,
        "market_thesis": "",
        "portfolio_diagnosis": "",
        "recovery_assessment": "",
        "risk_assessment": "",
        "suggested_manual_plan": "",
        "invalidation": "",
        "mental_note": "",
    }

    for key, value in defaults.items():
        ai_data.setdefault(key, value)

    try:
        confidence_score = float(ai_data.get("confidence_score", 0))
        if 0 <= confidence_score <= 1:
            confidence_score *= 100
        confidence_score = int(round(confidence_score))
    except Exception:
        confidence_score = ai_data.get("confidence_score", 0)

    return (
        f"<b>AI Analyst Review</b>\n\n"
        f"<b>Rule Agreement</b>\n"
        f"{esc(ai_data.get('agreement_with_rule'))}\n\n"
        f"<b>Confidence</b>\n"
        f"<b>{esc(confidence_score)}/100</b>\n\n"
        f"<b>Market Thesis</b>\n"
        f"{esc(ai_data.get('market_thesis'))}\n\n"
        f"<b>Portfolio Diagnosis</b>\n"
        f"{esc(ai_data.get('portfolio_diagnosis'))}\n\n"
        f"<b>Recovery Assessment</b>\n"
        f"{esc(ai_data.get('recovery_assessment'))}\n\n"
        f"<b>Risk Assessment</b>\n"
        f"{esc(ai_data.get('risk_assessment'))}\n\n"
        f"<b>Suggested Manual Plan</b>\n"
        f"{esc(ai_data.get('suggested_manual_plan'))}\n\n"
        f"<b>Invalidation</b>\n"
        f"{esc(ai_data.get('invalidation'))}\n\n"
        f"<b>Mental Note</b>\n"
        f"{esc(ai_data.get('mental_note'))}"
    )

# ============================================================
# Tokocrypto read-only private data
# ============================================================

def fetch_tokocrypto_portfolio(config=None):
    """
    Read-only portfolio fetch.
    Tidak melakukan order, cancel, atau withdrawal.
    """
    asset = get_asset_config(config) if config else {"base": "BTC", "quote": "USDT"}
    base = asset.get("base", "BTC")
    quote = asset.get("quote", "USDT")

    exchange = build_tokocrypto_exchange(private=True)
    balance = exchange.fetch_balance()

    base_info = balance.get(base, {})
    quote_info = balance.get(quote, {})

    base_free = float(base_info.get("free") or 0)
    base_used = float(base_info.get("used") or 0)
    base_total = float(base_info.get("total") or 0)

    quote_free = float(quote_info.get("free") or 0)
    quote_used = float(quote_info.get("used") or 0)
    quote_total = float(quote_info.get("total") or 0)

    return {
        "base_free": base_free,
        "base_used": base_used,
        "base_total": base_total,
        "usdt_free": quote_free,
        "usdt_used": quote_used,
        "usdt_total": quote_total,
        "source": "Tokocrypto private read-only",
    }


def fetch_tokocrypto_open_orders(config=None):
    try:
        print("DEBUG: trying Tokocrypto open orders...", flush=True)
        symbol = config.get("symbol", "BTC/USDT") if config else "BTC/USDT"

        exchange = build_tokocrypto_exchange(private=True)
        orders = exchange.fetch_open_orders(symbol)

        simplified = []
        for order in orders:
            simplified.append({
                "side": order.get("side"),
                "type": order.get("type"),
                "price": order.get("price"),
                "amount": order.get("amount"),
                "status": order.get("status"),
            })

        print(f"DEBUG: Tokocrypto open orders success: {len(simplified)} orders", flush=True)
        return simplified, ""

    except Exception as error:
        error_message = str(error)
        print(f"[WARN] Tokocrypto open orders failed: {error_message}", flush=True)
        return [], error_message


# ============================================================
# Market context
# ============================================================

def calculate_market_context(ticker, daily):
    price = ticker["price"]
    close = daily["close"]

    high_7d = daily.tail(7)["high"].max()
    low_7d = daily.tail(7)["low"].min()
    high_30d = daily.tail(30)["high"].max()
    low_30d = daily.tail(30)["low"].min()

    ma_7 = close.tail(7).mean()
    ma_20 = close.tail(20).mean()

    change_7d = ((price / close.iloc[-7]) - 1) * 100 if len(close) >= 7 else 0
    change_30d = ((price / close.iloc[0]) - 1) * 100 if len(close) >= 30 else 0

    from_7d_high = ((price / high_7d) - 1) * 100
    from_7d_low = ((price / low_7d) - 1) * 100
    from_30d_high = ((price / high_30d) - 1) * 100

    above_ma_7 = ((price / ma_7) - 1) * 100
    above_ma_20 = ((price / ma_20) - 1) * 100

    if price > ma_7 > ma_20:
        regime = "bullish_recovery"
    elif price < ma_7 < ma_20:
        regime = "bearish"
    else:
        regime = "sideways"

    return {
        "price": price,
        "source": ticker.get("source", "unknown"),
        "change_24h": ticker["change_pct_24h"],
        "change_7d": change_7d,
        "change_30d": change_30d,
        "high_7d": high_7d,
        "low_7d": low_7d,
        "high_30d": high_30d,
        "low_30d": low_30d,
        "from_7d_high": from_7d_high,
        "from_7d_low": from_7d_low,
        "from_30d_high": from_30d_high,
        "ma_7": ma_7,
        "ma_20": ma_20,
        "above_ma_7": above_ma_7,
        "above_ma_20": above_ma_20,
        "regime": regime,
    }


# ============================================================
# Portfolio calculation
# ============================================================

def calculate_portfolio(config, price):
    data_sources = config.get("data_sources", {})
    use_private_balance = data_sources.get("use_tokocrypto_private_balance", False)

    portfolio_error = ""

    if use_private_balance:
        try:
            print("DEBUG: trying Tokocrypto private balance...", flush=True)
            print(f"DEBUG: TOKOCRYPTO_API_KEY exists: {bool(os.getenv('TOKOCRYPTO_API_KEY'))}", flush=True)
            print(f"DEBUG: TOKOCRYPTO_API_SECRET exists: {bool(os.getenv('TOKOCRYPTO_API_SECRET'))}", flush=True)

            remote = fetch_tokocrypto_portfolio()

            btc = float(remote["base_total"])
            usdt = float(remote["usdt_total"])

            base_free = float(remote["base_free"])
            base_used = float(remote["base_used"])
            usdt_free = float(remote["usdt_free"])
            usdt_used = float(remote["usdt_used"])

            portfolio_source = remote["source"]
            print("DEBUG: Tokocrypto private balance success", flush=True)

        except Exception as error:
            portfolio_error = str(error)
            print(f"[WARN] Tokocrypto balance failed, fallback to config: {portfolio_error}", flush=True)

            usdt = float(config["portfolio"].get("usdt", 0) or 0)
            asset = get_asset_config(config)
            base_key = asset.get("base", "BTC").lower()
            btc = float(config["portfolio"].get(base_key, 0) or 0)

            base_free = btc
            base_used = 0
            usdt_free = usdt
            usdt_used = 0

            portfolio_source = "config_git.yaml fallback"
    else:
        usdt = float(config["portfolio"].get("usdt", 0) or 0)
        asset = get_asset_config(config)
        base_key = asset.get("base", "BTC").lower()
        btc = float(config["portfolio"].get(base_key, 0) or 0)

        manual_orders_cfg = config.get("manual_open_orders", {})
        manual_orders_enabled = manual_orders_cfg.get("enabled", False)
        manual_orders = manual_orders_cfg.get("orders", []) if manual_orders_enabled else []

        manual_usdt_used = 0.0
        for order in manual_orders:
            if order.get("status") == "open" and order.get("side") == "buy":
                manual_usdt_used += float(order.get("allocated_usdt", 0) or 0)

        base_free = btc
        base_used = 0
        usdt_used = manual_usdt_used
        usdt_free = max(usdt - usdt_used, 0)

        if manual_orders_enabled and manual_usdt_used > 0:
            portfolio_source = "config_git.yaml + manual open orders"
            portfolio_error = (
                "Tokocrypto private balance disabled in GitHub mode; "
                "using config portfolio and manual open orders."
            )
        else:
            portfolio_source = "config_git.yaml"
            #portfolio_error = "Tokocrypto private balance disabled in config_git.yaml"
            portfolio_error = "Tokocrypto private balance disabled in GitHub mode; using config portfolio."

    base_value = btc * price
    total_value = usdt + base_value
    base_pct = (base_value / total_value) * 100 if total_value > 0 else 0
    usdt_pct = 100 - base_pct

    return {
        "usdt": usdt,
        base_key: btc,
        "base_free": base_free,
        "base_used": base_used,
        "usdt_free": usdt_free,
        "usdt_used": usdt_used,
        "base_value": base_value,
        "total_value": total_value,
        "base_pct": base_pct,
        "usdt_pct": usdt_pct,
        "source": portfolio_source,
        "error": portfolio_error,
    }


# ============================================================
# Decision engine
# ============================================================

def decide_signal(config, market, portfolio):
    risk = config["risk"]
    strategy = config["strategy"]
    mental = config["mental"]

    target_min = config["portfolio"]["target_base_min_pct"]
    target_max = config["portfolio"]["target_base_max_pct"]
    max_buy = config["portfolio"]["max_single_buy_usdt"]
    reserve = config["portfolio"]["emergency_usdt_reserve"]

    base_pct = portfolio["base_pct"]
    available_usdt_after_reserve = max(0, portfolio["usdt_free"] - reserve)

    if mental["state"] == "panic" and strategy["no_trade_when_panic"]:
        return {
            "signal": "NO TRADE",
            "action_usdt": 0,
            "reason": "Mental state = panic. Prioritas sekarang adalah stabilitas. Jangan trade dulu.",
        }

    if market["change_24h"] >= risk["pump_24h_pct"]:
        return {
            "signal": "DO NOT FOMO",
            "action_usdt": 0,
            "reason": "BTC naik tajam dalam 24 jam. Hindari market buy besar. Tunggu pullback/retest.",
        }

    if base_pct >= target_max:
        return {
            "signal": "HOLD / TOO MUCH BTC",
            "action_usdt": 0,
            "reason": (
                f"Alokasi BTC sudah {base_pct:.1f}%, mendekati/di atas batas target "
                f"{target_max}%. Jangan tambah BTC."
            ),
        }

    if market["change_24h"] <= risk["dump_24h_pct"]:
        return {
            "signal": "WAIT / NO PANIC",
            "action_usdt": 0,
            "reason": "BTC sedang dump harian. Jangan langsung tangkap pisau jatuh. Tunggu stabilisasi.",
        }

    if portfolio["usdt_used"] > 0:
        return {
            "signal": "HOLD / OPEN ORDERS ACTIVE",
            "action_usdt": 0,
            "reason": (
                f"Ada {portfolio['usdt_used']:.2f} USDT yang sudah terkunci di open buy orders. "
                f"Jangan tambah manual agar tidak dobel entry."
            ),
        }

    if (
        strategy["allow_dip_buy"]
        and market["from_7d_high"] <= risk["deep_dip_from_7d_high_pct"]
        and available_usdt_after_reserve > 0
        and base_pct < target_max
    ):
        action = min(max_buy, available_usdt_after_reserve)
        return {
            "signal": "BUY SMALL - DEEP DIP",
            "action_usdt": action,
            "reason": (
                f"BTC turun {market['from_7d_high']:.1f}% dari 7d high. "
                f"Alokasi BTC masih {base_pct:.1f}%. Boleh buy kecil, bukan all-in."
            ),
        }

    if (
        strategy["allow_dip_buy"]
        and market["from_7d_high"] <= risk["dip_from_7d_high_pct"]
        and market["from_7d_low"] <= risk["near_7d_low_pct"]
        and available_usdt_after_reserve > 0
        and base_pct < target_min
    ):
        action = min(max_buy * 0.75, available_usdt_after_reserve)
        return {
            "signal": "BUY SMALL - NEAR RANGE LOW",
            "action_usdt": action,
            "reason": (
                "BTC dekat low 7 hari dan sedang diskon dari 7d high. "
                f"Alokasi BTC masih rendah ({base_pct:.1f}%)."
            ),
        }

    if (
        strategy["allow_confirmation_buy"]
        and market["regime"] == "bullish_recovery"
        and market["above_ma_20"] >= risk["confirmation_above_ma_pct"]
        and available_usdt_after_reserve > 0
        and base_pct < target_min
    ):
        action = min(max_buy, available_usdt_after_reserve)
        return {
            "signal": "CONFIRMATION BUY SMALL",
            "action_usdt": action,
            "reason": (
                "BTC berada di atas MA7 dan MA20, indikasi recovery. "
                f"Alokasi BTC masih {base_pct:.1f}%, boleh tambah kecil."
            ),
        }

    if market["regime"] == "bearish":
        return {
            "signal": "HOLD / BEARISH",
            "action_usdt": 0,
            "reason": "Regime masih bearish. Simpan USDT, tunggu diskon lebih jelas atau reversal valid.",
        }

    return {
        "signal": "HOLD",
        "action_usdt": 0,
        "reason": "Tidak ada setup kuat. Jangan overtrade.",
    }


# ============================================================
# GitHub repo reminder
# ============================================================

def get_repo_activity_info():
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        return {"available": False, "message": ""}

    url = f"https://api.github.com/repos/{repo}/commits"
    params = {"per_page": 1}

    headers = {}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(url, params=params, headers=headers, timeout=25)
    if not response.ok:
        return {
            "available": False,
            "message": f"Repo activity check failed: {response.status_code}",
        }

    data = response.json()
    if not data:
        return {
            "available": False,
            "message": "Repo activity check failed: no commits found.",
        }

    last_commit_iso = data[0]["commit"]["committer"]["date"]
    last_commit_dt = datetime.fromisoformat(last_commit_iso.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    days_since = (now - last_commit_dt).days

    return {
        "available": True,
        "last_commit_date": last_commit_dt.date().isoformat(),
        "days_since_last_commit": days_since,
    }


def build_repo_reminder(config):
    repo_cfg = config.get("github_repo_health", {})
    remind_after = int(repo_cfg.get("remind_after_days", 55))
    critical_after = int(repo_cfg.get("critical_after_days", 57))

    info = get_repo_activity_info()
    if not info.get("available"):
        msg = info.get("message", "")
        if msg:
            return f"\n\nRepo reminder: {msg}"
        return ""

    days = info["days_since_last_commit"]
    last_date = info["last_commit_date"]

    if days >= critical_after:
        return (
            f"\n\nGitHub repo reminder: CRITICAL. "
            f"Repo terakhir update {days} hari lalu ({last_date}). "
            f"Segera buat commit kecil agar workflow tidak mendekati disable 60 hari."
        )

    if days >= remind_after:
        return (
            f"\n\nGitHub repo reminder: repo terakhir update {days} hari lalu ({last_date}). "
            f"Disarankan buat commit kecil sebelum hari ke-60."
        )

    return ""

# ============================================================
# Gemini explanation
# ============================================================

LLM_USAGE_STATE_FILE = "data/llm_usage_state.json"

GEMINI_QUOTA_TIMEZONE = "America/Los_Angeles"


def get_gemini_quota_date():
    """
    Gemini API RPD quota resets at midnight Pacific Time.
    Use America/Los_Angeles so DST is handled automatically.
    """
    pacific_now = datetime.now(ZoneInfo(GEMINI_QUOTA_TIMEZONE))
    return pacific_now.date().isoformat()

def get_llm_state_file(config=None):
    if config is None:
        return LLM_USAGE_STATE_FILE

    llm_cfg = config.get("llm", {})
    quota_cfg = llm_cfg.get("quota_guard", {})
    return quota_cfg.get("state_file", LLM_USAGE_STATE_FILE)

def load_llm_usage_state(config=None):
    state_file = get_llm_state_file(config)
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)

    quota_date = get_gemini_quota_date()

    if not os.path.exists(state_file):
        return {
            "quota_date_pacific": quota_date,
            "grounded_runs_today": 0,
            "last_grounding_bucket_utc": "",
            "models": {},
        }

    try:
        with open(state_file, "r", encoding="utf-8") as file:
            state = json.load(file)
    except Exception:
        state = {}

    # Backward compatibility:
    # Old state used date_utc. New state uses quota_date_pacific
    # because Gemini RPD resets at midnight Pacific Time.
    saved_quota_date = state.get("quota_date_pacific")

    if saved_quota_date != quota_date:
        return {
            "quota_date_pacific": quota_date,
            "grounded_runs_today": 0,
            "last_grounding_bucket_utc": "",
            "models": {},
        }

    state.setdefault("quota_date_pacific", quota_date)
    state.setdefault("grounded_runs_today", 0)
    state.setdefault("last_grounding_bucket_utc", "")
    state.setdefault("models", {})
    return state


def save_llm_usage_state(state, config=None):
    state_file = get_llm_state_file(config)
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)

    with open(state_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


def parse_utc_datetime(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def get_grounding_bucket_utc(interval_hours):
    now = datetime.now(timezone.utc)
    bucket_hour = (now.hour // interval_hours) * interval_hours
    return f"{now.date().isoformat()}T{bucket_hour:02d}:00Z"


def get_llm_model_config(llm_cfg, model_key):
    models_cfg = llm_cfg.get("models", {})
    model_cfg = models_cfg.get(model_key)

    if not model_cfg:
        raise KeyError(f"Missing llm.models.{model_key} config")

    model = model_cfg.get("model")
    if not model:
        raise KeyError(f"Missing model name for llm.models.{model_key}")

    max_output_tokens = int(model_cfg.get("max_output_tokens", 2000))
    daily_limit = int(model_cfg.get("daily_limit", 20))

    return {
        "model": model,
        "max_output_tokens": max_output_tokens,
        "daily_limit": daily_limit,
    }


def get_fallback_model_configs(llm_cfg):
    fallback_models = llm_cfg.get("fallback_models", [])

    if fallback_models:
        configs = []
        for item in fallback_models:
            model = item.get("model")
            if not model:
                continue

            configs.append({
                "model": model,
                "max_output_tokens": int(item.get("max_output_tokens", 1500)),
                "daily_limit": int(item.get("daily_limit", 20)),
            })

        if configs:
            return configs

    fallback_cfg = get_llm_model_config(llm_cfg, "fallback")
    return [fallback_cfg]


def get_model_usage_entry(state, model):
    models = state.setdefault("models", {})
    entry = models.setdefault(model, {
        "requests_today": 0,
        "success_today": 0,
        "errors_today": 0,
        "last_used_utc": "",
        "last_status": "",
        "last_mode": "",
        "last_response_status": "",
        "last_error": "",
        "cooldown_until_utc": "",
    })
    return entry


def is_model_available(config, model, daily_limit):
    llm_cfg = config.get("llm", {})
    quota_cfg = llm_cfg.get("quota_guard", {})

    if not quota_cfg.get("enabled", True):
        return True, "quota guard disabled"

    state = load_llm_usage_state(config)
    entry = get_model_usage_entry(state, model)

    cooldown_until = parse_utc_datetime(entry.get("cooldown_until_utc"))
    now = datetime.now(timezone.utc)

    if cooldown_until and now < cooldown_until:
        return False, f"model cooldown until {cooldown_until.isoformat()}"

    reserve = int(quota_cfg.get("reserve_requests_per_model", 0))
    usable_limit = max(0, int(daily_limit) - reserve)

    requests_today = int(entry.get("requests_today", 0))
    if requests_today >= usable_limit:
        return False, f"daily model limit reached: {requests_today}/{usable_limit}"

    return True, f"model available: {requests_today}/{usable_limit}"


def record_llm_model_usage(
    config,
    model,
    mode,
    use_grounding=False,
    status="ok",
    response_status=None,
    error_message="",
):
    state = load_llm_usage_state(config)
    entry = get_model_usage_entry(state, model)

    entry["requests_today"] = int(entry.get("requests_today", 0)) + 1
    entry["last_used_utc"] = datetime.now(timezone.utc).isoformat()
    entry["last_status"] = str(status)
    entry["last_mode"] = str(mode)

    if response_status is not None:
        entry["last_response_status"] = response_status

    if error_message:
        entry["last_error"] = str(error_message)[:500]

    if status == "ok":
        entry["success_today"] = int(entry.get("success_today", 0)) + 1
        entry["cooldown_until_utc"] = ""
    else:
        entry["errors_today"] = int(entry.get("errors_today", 0)) + 1

    if response_status in [429, 503]:
        cooldown_minutes = int(
            config.get("llm", {})
            .get("quota_guard", {})
            .get("rate_limit_cooldown_minutes", 15)
        )
        cooldown_until = datetime.now(timezone.utc) + pd.Timedelta(minutes=cooldown_minutes)
        entry["cooldown_until_utc"] = cooldown_until.isoformat()

    if use_grounding:
        state["grounded_runs_today"] = int(state.get("grounded_runs_today", 0)) + 1

        interval_hours = int(
            config.get("llm", {})
            .get("grounding", {})
            .get("interval_hours", 4)
        )
        if interval_hours > 0:
            state["last_grounding_bucket_utc"] = get_grounding_bucket_utc(interval_hours)

    save_llm_usage_state(state, config)





def is_action_signal(decision):
    signal = decision.get("signal", "").upper()

    passive_keywords = [
        "HOLD",
        "NO TRADE",
        "DO NOT FOMO",
        "WAIT",
    ]

    return not any(keyword in signal for keyword in passive_keywords)


def should_use_google_grounding(config, market, decision):
    llm_cfg = config.get("llm", {})
    grounding_cfg = llm_cfg.get("grounding", {})

    if not grounding_cfg.get("enabled", False):
        return False, "grounding disabled"

    if not grounding_cfg.get("use_google_search", False):
        return False, "google search disabled"

    state = load_llm_usage_state(config)
    max_grounded = int(grounding_cfg.get("max_grounded_runs_per_day", 6))

    if int(state.get("grounded_runs_today", 0)) >= max_grounded:
        return False, "daily grounding cap reached"

    now_hour = datetime.now(timezone.utc).hour
    interval_hours = int(grounding_cfg.get("interval_hours", 4))

    if (
        grounding_cfg.get("use_on_interval", True)
        and interval_hours > 0
        and now_hour % interval_hours == 0
    ):
        current_bucket = get_grounding_bucket_utc(interval_hours)
        last_bucket = state.get("last_grounding_bucket_utc", "")

        if last_bucket == current_bucket:
            return False, f"grounding already used for bucket {current_bucket}"

        return True, f"grounding interval hit: every {interval_hours}h UTC"

    abs_24h = abs(float(market.get("change_24h", 0)))
    high_vol_threshold = float(
        config.get("llm", {})
        .get("deep_review", {})
        .get("high_volatility_24h_abs_pct", 4)
    )

    if grounding_cfg.get("use_on_high_volatility", True) and abs_24h >= high_vol_threshold:
        return True, f"grounding due to high volatility: {abs_24h:.2f}%"

    if grounding_cfg.get("use_on_action_signal", True) and is_action_signal(decision):
        return True, f"grounding due to action signal: {decision.get('signal')}"

    return False, "grounding not needed"


def choose_gemini_model(config, market, portfolio, decision):
    """
    Hourly deep-first quota-aware routing.

    Priority:
    1. Grounding every configured interval, if enabled and quota available.
    2. Deep primary model, usually Gemini 3.5 Flash.
    3. Deep secondary model, usually Gemini 2.5 Flash.
    4. Fallback model as quota fallback before request.

    Nama model tidak di-hardcode di code.
    Semua nama model dan daily limit dibaca dari config_git.yaml.
    """
    llm_cfg = config.get("llm", {})

    use_grounding, grounding_reason = should_use_google_grounding(config, market, decision)

    if use_grounding:
        grounding_cfg = get_llm_model_config(llm_cfg, "grounding")
        grounding_model = grounding_cfg["model"]
        grounding_available, grounding_availability_reason = is_model_available(
            config,
            grounding_model,
            grounding_cfg["daily_limit"],
        )

        if grounding_available:
            return {
                "mode": "grounding",
                "model": grounding_model,
                "max_output_tokens": grounding_cfg["max_output_tokens"],
                "daily_limit": grounding_cfg["daily_limit"],
                "use_grounding": True,
                "routing_reason": grounding_reason,
                "quota_reason": grounding_availability_reason,
                "ai_source": "planned_main_html",
            }

        print(f"[WARN] Grounding model unavailable: {grounding_availability_reason}")

    hourly_deep_cfg = llm_cfg.get("hourly_deep", {})
    if hourly_deep_cfg.get("enabled", True):
        primary_cfg = get_llm_model_config(llm_cfg, "deep_primary")
        primary_model = primary_cfg["model"]
        primary_available, primary_reason = is_model_available(
            config,
            primary_model,
            primary_cfg["daily_limit"],
        )

        if primary_available:
            return {
                "mode": "deep_primary",
                "model": primary_model,
                "max_output_tokens": primary_cfg["max_output_tokens"],
                "daily_limit": primary_cfg["daily_limit"],
                "use_grounding": False,
                "routing_reason": "hourly deep analysis using primary model",
                "quota_reason": primary_reason,
                "ai_source": "planned_main_html",
            }

        secondary_cfg = get_llm_model_config(llm_cfg, "deep_secondary")
        secondary_model = secondary_cfg["model"]
        secondary_available, secondary_reason = is_model_available(
            config,
            secondary_model,
            secondary_cfg["daily_limit"],
        )

        if secondary_available:
            return {
                "mode": "deep_secondary",
                "model": secondary_model,
                "max_output_tokens": secondary_cfg["max_output_tokens"],
                "daily_limit": secondary_cfg["daily_limit"],
                "use_grounding": False,
                "routing_reason": f"primary model unavailable: {primary_reason}",
                "quota_reason": secondary_reason,
                "ai_source": "planned_main_html",
            }

        print(f"[WARN] Deep primary unavailable: {primary_reason}")
        print(f"[WARN] Deep secondary unavailable: {secondary_reason}")

    fallback_configs = get_fallback_model_configs(llm_cfg)

    for fallback_cfg in fallback_configs:
        fallback_model = fallback_cfg["model"]
        fallback_available, fallback_reason = is_model_available(
            config,
            fallback_model,
            fallback_cfg["daily_limit"],
        )

        if fallback_available:
            return {
                "mode": "quota_fallback",
                "model": fallback_model,
                "max_output_tokens": fallback_cfg["max_output_tokens"],
                "daily_limit": fallback_cfg["daily_limit"],
                "use_grounding": False,
                "routing_reason": "all preferred models unavailable; using fallback as main request",
                "quota_reason": fallback_reason,
                "ai_source": "planned_main_html",
            }

    raise RuntimeError("No available Gemini model quota left for this run.")


def build_rule_summary_for_llm(
    config,
    market,
    portfolio,
    decision,
    open_orders,
    intrahour_order_events=None,
    candidate_actions=None,
):
    cost_basis = calculate_base_cost_basis_from_manual_lots(
        config=config,
        portfolio=portfolio,
    )

    exposure_tier = get_gemini_exposure_tier(
        config=config,
        market=market,
        has_open_orders=bool(open_orders),
    )

    return {
        "market": {
            "price": market["price"],
            "source": market.get("source"),
            "regime": market["regime"],
            "change_24h": market["change_24h"],
            "change_7d": market["change_7d"],
            "change_30d": market["change_30d"],
            "from_7d_high": market["from_7d_high"],
            "from_7d_low": market.get("from_7d_low"),
            "ma_7": market["ma_7"],
            "ma_20": market["ma_20"],
            "above_ma_7": market.get("above_ma_7"),
            "above_ma_20": market.get("above_ma_20"),
        },
        "portfolio": {
            "btc": portfolio[config.get("asset", {}).get("base", "BTC").lower()],
            "usdt": portfolio["usdt"],
            "base_pct": portfolio["base_pct"],
            "usdt_pct": portfolio["usdt_pct"],
            "total_value": portfolio["total_value"],
            "usdt_free": portfolio["usdt_free"],
            "usdt_used": portfolio["usdt_used"],
            "source": portfolio.get("source"),
            "target_base_min_pct": config["portfolio"]["target_base_min_pct"],
            "target_base_max_pct": config["portfolio"]["target_base_max_pct"],
            "emergency_usdt_reserve": config["portfolio"]["emergency_usdt_reserve"],
        },
        "base_cost_basis_computed": cost_basis,
        "exposure_tier_computed": exposure_tier,
        "base_rule_decision": decision,
        "open_orders": open_orders[:5],
        "intrahour_order_events": intrahour_order_events or {},
        "candidate_actions": candidate_actions or [],
        "gemini_decision_config": config.get("gemini_decision", {}),
        "sell_strategy": config.get("sell_strategy", {}),
        "base_cost_basis_config": config.get("base_cost_basis", {}),
    }

def salvage_gemini_decision_from_partial_json(raw_text, action_keys):
    """
    Recover a minimal Gemini decision from malformed or truncated JSON.

    Conservative recovery rules:
    - Recover only if an allowed action key is clearly present.
    - Prefer explicit JSON fields if available.
    - If Gemini rambles before JSON, recover only when the text contains
      a recommendation-like phrase near an allowed action key.
    - Buy/sell/confidence still go through deterministic risk guards later.
    """
    if not raw_text:
        return None

    text = str(raw_text)

    def extract_number(field_name, default=0.0):
        pattern = rf'"{re.escape(field_name)}"\s*:\s*([-+]?\d+(?:\.\d+)?)'
        match = re.search(pattern, text)
        if match:
            try:
                return float(match.group(1))
            except Exception:
                return default

        loose_patterns = [
            rf"{re.escape(field_name)}\s*(?:is|=|:)?\s*([-+]?\d+(?:\.\d+)?)",
            rf"{re.escape(field_name.replace('_', ' '))}\s*(?:is|=|:)?\s*([-+]?\d+(?:\.\d+)?)",
        ]

        for loose_pattern in loose_patterns:
            loose_match = re.search(loose_pattern, text, flags=re.IGNORECASE)
            if loose_match:
                try:
                    return float(loose_match.group(1))
                except Exception:
                    return default

        return default

    def extract_string(field_name, default=""):
        pattern = rf'"{re.escape(field_name)}"\s*:\s*"([^"]*)"'
        match = re.search(pattern, text)
        if not match:
            return default

        return match.group(1)

    explicit_action_match = re.search(
        r'"recommended_action_key"\s*:\s*"([^"]+)"',
        text,
    )

    action_key = None

    if explicit_action_match:
        candidate_key = explicit_action_match.group(1)
        if candidate_key in action_keys:
            action_key = candidate_key

    if action_key is None:
        recommendation_markers = [
            "i recommended",
            "recommended",
            "selected action",
            "choose",
            "chosen action",
            "final action",
        ]

        lower_text = text.lower()
        has_recommendation_marker = any(marker in lower_text for marker in recommendation_markers)

        if not has_recommendation_marker:
            return None

        matched_keys = [
            key for key in action_keys
            if key and key in text
        ]

        # Conservative: if multiple allowed action keys are mentioned,
        # do not guess which one was intended.
        if len(matched_keys) != 1:
            return None

        action_key = matched_keys[0]

    if action_key not in action_keys:
        return None

    recommended_buy = extract_number("recommended_buy_usdt", 0.0)
    recommended_sell_pct = extract_number("recommended_sell_base_pct_of_holdings", 0.0)
    confidence_score = extract_number("confidence_score", 0.0)

    # Extra fallback for common malformed text:
    # "I recommended 15.0" or "buy 15 USDT"
    if recommended_buy <= 0 and action_key.startswith("BUY"):
        buy_match = re.search(
            r"(?:buy|recommended)\s+(?:btc\s+worth\s+)?([-+]?\d+(?:\.\d+)?)\s*(?:usdt|usd)?",
            text,
            flags=re.IGNORECASE,
        )
        if buy_match:
            try:
                recommended_buy = float(buy_match.group(1))
            except Exception:
                recommended_buy = 0.0

    # If confidence is missing, keep it at 0.
    # This means buy/sell will be blocked by guard unless confidence was visible.
    if 0 <= confidence_score <= 1:
        confidence_score *= 100

    agreement = extract_string("agreement_with_rule", "cautious_agree")

    return {
        "recommended_action_key": action_key,
        "recommended_buy_usdt": recommended_buy,
        "recommended_sell_base_pct_of_holdings": recommended_sell_pct,
        "agreement_with_rule": agreement,
        "confidence_score": int(round(confidence_score)),
        "market_thesis": (
            "Gemini returned malformed or partial JSON. The machine-readable action fields "
            "were recovered from the response text, but the full market thesis was incomplete."
        ),
        "portfolio_diagnosis": (
            "Use the deterministic portfolio section above as the source of truth."
        ),
        "recovery_assessment": (
            "Recovered decision is still passed through deterministic risk guards before "
            "affecting final decision."
        ),
        "risk_assessment": (
            "Malformed Gemini output is treated cautiously. No action is allowed unless the "
            "recommended action key exists in allowed candidate actions and confidence/exposure "
            "guards pass."
        ),
        "suggested_manual_plan": (
            "Follow the final Decision section only. The bot remains manual-only."
        ),
        "invalidation": (
            "If Gemini repeatedly returns malformed JSON, reduce output size or route to a fallback model."
        ),
        "mental_note": (
            "Malformed AI output is not automatically trusted; deterministic guards still decide."
        ),
    }


def fetch_macro_grounding_context(config, api_key):
    """
    Step 1 of the Two-Step Grounding Pipeline.
    Fetches the macro news context using Gemini 2.5 Flash with Google Search enabled.
    This guarantees the search results are extracted cleanly into text, preventing JSON malformation
    in the main analyst call.
    """
    llm_cfg = config.get("llm", {})
    grounding_cfg = get_llm_model_config(llm_cfg, "grounding")
    model = grounding_cfg.get("model", "gemini-2.5-flash")

    # Enforce user rule: grounding must use 2.5 flash or lite, do NOT use 3.5 flash
    if "3.5" in model:
        model = "gemini-2.5-flash"

    asset = get_asset_config(config)
    asset_name = asset.get("name", "Bitcoin")

    prompt = (
        f"Search the web for the latest {asset_name} macro news, events, and market sentiment today. "
        "Summarize the current market context in 3 concise bullet points. "
        "Focus on facts, not price predictions."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}]
    }

    print(f"[GROUNDING] Fetching macro news via {model}...", flush=True)
    try:
        response = requests.post(url, json=payload, timeout=30)
        
        record_llm_model_usage(
            config=config,
            model=model,
            mode="grounding_researcher",
            use_grounding=True,
            status="ok" if response.ok else "http_error",
            response_status=response.status_code,
            error_message=response.text if not response.ok else "",
        )

        if not response.ok:
            print(f"[WARN] Grounding failed with {model} (HTTP {response.status_code}). Trying fallback...")
            fallback_model = "gemini-2.5-flash-lite"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{fallback_model}:generateContent?key={api_key}"
            
            print(f"[GROUNDING] Fetching macro news via fallback {fallback_model}...", flush=True)
            response = requests.post(url, json=payload, timeout=30)
            
            record_llm_model_usage(
                config=config,
                model=fallback_model,
                mode="grounding_researcher",
                use_grounding=True,
                status="ok" if response.ok else "http_error",
                response_status=response.status_code,
                error_message=response.text if not response.ok else "",
            )
            
            if not response.ok:
                return "Grounding unavailable (API error)."

        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return "Grounding unavailable (no candidates)."
            
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return "Grounding unavailable (no parts)."
            
        text = parts[0].get("text", "").strip()
        return text if text else "Grounding unavailable (empty text)."
        
    except Exception as e:
        print(f"[WARN] Grounding exception: {e}")
        return "Grounding unavailable (exception)."


def generate_gemini_explanation(
    config,
    market,
    portfolio,
    decision,
    open_orders,
    intrahour_order_events=None,
    candidate_actions=None,
):
    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("enabled", False):
        return {
            "available": False,
            "ai_explanation": "",
            "recommended_action_key": "HOLD",
            "recommended_buy_usdt": 0,
            "recommended_sell_base_pct_of_holdings": 0,
            "confidence_score": 0,
            "error": "llm disabled",
        }

    ai_error_fallback = (
        f"<b>AI Analyst Review</b>\n\n"
        f"<b>Rule Agreement</b>\n"
        f"unavailable\n\n"
        f"<b>Confidence</b>\n"
        f"<b>0/100</b>\n\n"
        f"<b>Market Thesis</b>\n"
        f"Gemini analyst output is unavailable for this run.\n\n"
        f"<b>Portfolio Diagnosis</b>\n"
        f"Use the deterministic portfolio and rule-engine sections above as the source of truth.\n\n"
        f"<b>Recovery Assessment</b>\n"
        f"Recovery analysis remains based on the rule-engine recovery tracker.\n\n"
        f"<b>Risk Assessment</b>\n"
        f"Do not act on missing or malformed Gemini output.\n\n"
        f"<b>Suggested Manual Plan</b>\n"
        f"Follow the rule-engine signal shown above.\n\n"
        f"<b>Invalidation</b>\n"
        f"Check the terminal or GitHub Actions log, then re-run the agent.\n\n"
        f"<b>Mental Note</b>\n"
        f"Missing AI output is not a trading signal."
    )

    def unavailable(reason):
        return {
            "available": False,
            "ai_explanation": ai_error_fallback,
            "recommended_action_key": "HOLD",
            "recommended_buy_usdt": 0,
            "recommended_sell_base_pct_of_holdings": 0,
            "confidence_score": 0,
            "error": reason,
        }

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[WARN] GEMINI_API_KEY is missing")
        config["_last_llm_routing"] = {
            "mode": "unavailable",
            "model": "none",
            "use_grounding": False,
            "routing_reason": "GEMINI_API_KEY is missing",
            "quota_reason": "",
            "ai_source": "unavailable",
        }
        return unavailable("GEMINI_API_KEY is missing")

    candidate_actions = candidate_actions or [
        {
            "key": "HOLD",
            "type": "hold",
            "signal": "HOLD",
            "max_buy_usdt": 0,
            "max_sell_base_pct_of_holdings": 0,
            "reason": "Default safe action.",
        }
    ]

    try:
        routing = choose_gemini_model(config, market, portfolio, decision)
    except Exception as error:
        print(f"[WARN] Gemini routing failed: {error}")
        config["_last_llm_routing"] = {
            "mode": "unavailable",
            "model": "none",
            "use_grounding": False,
            "routing_reason": f"Gemini routing failed: {error}",
            "quota_reason": "",
            "ai_source": "unavailable",
        }
        return unavailable(f"Gemini routing failed: {error}")

    model = routing["model"]
    max_output_tokens = routing["max_output_tokens"]
    use_grounding = routing["use_grounding"]
    routing_reason = routing["routing_reason"]
    temperature = float(llm_cfg.get("temperature", 0.2))

    analyst_model = model
    analyst_max_tokens = max_output_tokens

    context = build_rule_summary_for_llm(
        config=config,
        market=market,
        portfolio=portfolio,
        decision=decision,
        open_orders=open_orders,
        intrahour_order_events=intrahour_order_events,
        candidate_actions=candidate_actions,
    )

    def set_last_routing(base_routing, ai_source, request_status=""):
        stored = dict(base_routing)
        stored["ai_source"] = ai_source
        stored["request_status"] = request_status
        config["_last_llm_routing"] = stored

    action_keys = [action["key"] for action in candidate_actions]

    grounding_instruction = ""
    macro_context = ""
    if use_grounding:
        macro_context = fetch_macro_grounding_context(config, api_key)
        grounding_instruction = f"""
You have access to the latest Macro Market News (provided by the Researcher Bot below).
Use this news context to assess broad market sentiment and justify your thesis.

Do not overreact to one headline.
Do not cite rumors as facts.
Do not turn news sentiment into an aggressive trading recommendation.

Macro Market News:
{macro_context}
"""

    asset = get_asset_config(config)
    base = asset.get("base", "BTC")

    prompt = f"""
You are a decision-support analyst for a manual-only {base} Discipline Agent.

The bot is NOT allowed to trade.
The user manually executes decisions.
You must choose exactly one action from candidate_actions.
Do not invent a new action.
Do not recommend more than max_buy_usdt from the selected action.
Do not recommend more than max_sell_base_pct_of_holdings from the selected action.
Do not override hard risk guards.
Do not recommend leverage, futures, margin, all-in, auto-trading, order cancellation, or withdrawal.

Model routing:
- Mode used: {routing.get("mode")}
- Model used: {model}
- Routing reason: {routing_reason}
- Google Search grounding enabled: {use_grounding}

{grounding_instruction}

Your role:
Analyze market, portfolio, manual config, open orders, recent price check, recovery gap, buy opportunity, sell opportunity, and risk.
Be sharper than a simple HOLD explainer, but remain disciplined.

Candidate action keys you may choose:
{action_keys}

Decision rules:
- recommended_action_key must be exactly one candidate action key.
- recommended_buy_usdt must be 0 if selected action max_buy_usdt is 0.
- recommended_sell_base_pct_of_holdings must be 0 if selected action max_sell_base_pct_of_holdings is 0.
- If choosing a buy action, recommended_buy_usdt must be small and <= max_buy_usdt.
- If choosing a sell action, recommended_sell_base_pct_of_holdings must be <= max_sell_base_pct_of_holdings.
- If open orders are active, a buy action is allowed only when BUY_SMALL_CONFIRMATION_WITH_LADDER is present in candidate_actions.
- Treat BUY_SMALL_CONFIRMATION_WITH_LADDER as upside participation, not FOMO.
- Existing lower limit orders remain a downside ladder.
- If choosing a buy action, respect the selected exposure_tier.
- For early_confirmation tier, be stricter: buy only if momentum is constructive but not overheated.
- For confirmed_breakout tier, a larger buy is allowed only if the action max_buy_usdt allows it.
- Explain both upside_base_pct_after_buy and downside_planned_base_pct_after_buy_and_open_orders if choosing a buy action.
- Do not recommend buy if exposure_tier is overheated or HOLD_OVERHEATED_NO_FOMO is present.
- Sell actions are for planned take-profit or rebalancing only, not panic.
- Do not estimate or invent average entry price yourself.
- Use base_cost_basis_computed only if available=true.
- If base_cost_basis_computed.available=false, do not claim the portfolio is in profit based on cost basis.
- If Base allocation is below target and sell action is not explicitly available, do not recommend sell.
- If HOLD_DO_NOT_SELL_UNDERALLOCATED is available, respect that selling may slow recovery.
- If HOLD_OPEN_ORDERS_ACTIVE is better, explain why waiting is better.
- If HOLD_REVIEW_LADDER is better, explain that market may be improving but ladder should be reviewed.
- If intrahour_order_events indicates possible_fill_price_touched, do not recommend buy; tell user to verify Tokocrypto and update config.
- Do not claim a possible fill is confirmed.

Allocation interpretation:
- If base_pct is below target_base_min_pct, say Base allocation is BELOW TARGET for recovery mode.
- If base_pct is between target_base_min_pct and target_base_max_pct, say it is within target range.
- If base_pct is above target_base_max_pct, say it is too aggressive.

Return ONLY valid JSON.
No Markdown.
No HTML.
No code fences.
Do not write analysis before the JSON.
Do not write reasoning outside the JSON.
Do not write "wait", "let's check", or any self-dialogue.
Start your response with {{ and end your response with }}.

Use exactly this schema:
{{
  "recommended_action_key": "one candidate action key",
  "recommended_buy_usdt": 0,
  "recommended_sell_base_pct_of_holdings": 0,
  "agreement_with_rule": "agree | cautious_agree | disagree_but_do_not_override",
  "confidence_score": 0,
  "market_thesis": "concise market thesis",
  "portfolio_diagnosis": "portfolio allocation diagnosis",
  "recovery_assessment": "recovery realism assessment",
  "risk_assessment": "main risk assessment",
  "suggested_manual_plan": "manual-only plan",
  "invalidation": "specific invalidation condition",
  "mental_note": "short mental note"
}}

Data:
{context}
"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{analyst_model}:generateContent?key={api_key}"
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": analyst_max_tokens,
            "responseMimeType": "application/json",
        },
    }

    # The JSON-generating API call now executes strictly WITHOUT tools
    # to prevent Gemini from malforming the JSON with markdown citations.
    # Grounding context was already injected into the prompt via the Researcher Bot.

    try:
        response = requests.post(url, json=payload, timeout=45)

        # The main call (Analyst) does not use grounding tools directly anymore,
        # so use_grounding=False is passed here to reflect the actual API payload,
        # while the grounding quota was already recorded by the Researcher Bot.
        record_llm_model_usage(
            config=config,
            model=analyst_model,
            mode=routing.get("mode", "unknown"),
            use_grounding=False,
            status="ok" if response.ok else "http_error",
            response_status=response.status_code,
            error_message=response.text if not response.ok else "",
        )

        if not response.ok:
            print(f"[WARN] Gemini error with {analyst_model}: {response.status_code} {response.text}")
            
            fallback_configs = llm_cfg.get("fallback_models", [])
            for fb_cfg in fallback_configs:
                fb_model = fb_cfg["model"]
                fb_url = f"https://generativelanguage.googleapis.com/v1beta/models/{fb_model}:generateContent?key={api_key}"
                
                payload["generationConfig"]["maxOutputTokens"] = fb_cfg.get("max_output_tokens", 1500)
                
                print(f"[WARN] Retrying Analyst generation with fallback model: {fb_model}...", flush=True)
                response = requests.post(fb_url, json=payload, timeout=45)
                
                record_llm_model_usage(
                    config=config,
                    model=fb_model,
                    mode="fallback",
                    use_grounding=False,
                    status="ok" if response.ok else "http_error",
                    response_status=response.status_code,
                    error_message=response.text if not response.ok else "",
                )
                
                if response.ok:
                    print(f"[SUCCESS] Fallback to {fb_model} succeeded!", flush=True)
                    break

        if not response.ok:
            print(f"[ERROR] All models failed. Last error: {response.status_code}")
            set_last_routing(routing, "unavailable", f"http_error_{response.status_code}")
            return unavailable(f"main request HTTP error {response.status_code}")

        data = response.json()

        candidates = data.get("candidates", [])
        if not candidates:
            set_last_routing(routing, "unavailable", "no_candidates")
            return unavailable("main response has no candidates")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            set_last_routing(routing, "unavailable", "no_parts")
            return unavailable("main response has no parts")

        raw_text = parts[0].get("text", "").strip()
        if not raw_text:
            set_last_routing(routing, "unavailable", "empty_text")
            return unavailable("main response text is empty")

        ai_source = "main_json"
        request_status = "success"

        try:
            ai_data = extract_json_object(raw_text)
        except Exception as parse_error:
            print(f"[WARN] Gemini decision JSON parse failed: {parse_error}")
            print(f"[WARN] Raw Gemini output: {raw_text[:500]}")

            recovered_ai_data = salvage_gemini_decision_from_partial_json(
                raw_text=raw_text,
                action_keys=action_keys,
            )

            if recovered_ai_data:
                print(
                    "[WARN] Gemini partial JSON recovered into minimal guarded decision",
                    flush=True,
                )
                ai_data = recovered_ai_data
                ai_source = "partial_json_recovery"
                request_status = "json_parse_recovered"
            else:
                set_last_routing(routing, "unavailable", "json_parse_failed")
                return unavailable(f"Gemini JSON parse failed: {parse_error}")

        recommended_key = ai_data.get("recommended_action_key", "HOLD")
        if recommended_key not in action_keys:
            ai_data["recommended_action_key"] = "HOLD"
            ai_data["recommended_buy_usdt"] = 0
            ai_data["recommended_sell_base_pct_of_holdings"] = 0
            ai_data["risk_assessment"] = (
                str(ai_data.get("risk_assessment", "")) +
                " Gemini originally recommended an action outside allowed actions, so it was forced to HOLD."
            ).strip()

        try:
            recommended_buy = float(ai_data.get("recommended_buy_usdt", 0) or 0)
        except Exception:
            recommended_buy = 0

        try:
            recommended_sell_pct = float(ai_data.get("recommended_sell_base_pct_of_holdings", 0) or 0)
        except Exception:
            recommended_sell_pct = 0

        try:
            confidence_score = float(ai_data.get("confidence_score", 0) or 0)
            if 0 <= confidence_score <= 1:
                confidence_score *= 100
            confidence_score = int(round(confidence_score))
        except Exception:
            confidence_score = 0

        ai_explanation = format_ai_review_from_gemini_decision_json(ai_data)

        result = {
            "available": True,
            "ai_explanation": ai_explanation,
            "recommended_action_key": ai_data.get("recommended_action_key", "HOLD"),
            "recommended_buy_usdt": recommended_buy,
            "recommended_sell_base_pct_of_holdings": recommended_sell_pct,
            "confidence_score": confidence_score,
            "agreement_with_rule": ai_data.get("agreement_with_rule", ""),
            "market_thesis": ai_data.get("market_thesis", ""),
            "portfolio_diagnosis": ai_data.get("portfolio_diagnosis", ""),
            "recovery_assessment": ai_data.get("recovery_assessment", ""),
            "risk_assessment": ai_data.get("risk_assessment", ""),
            "suggested_manual_plan": ai_data.get("suggested_manual_plan", ""),
            "invalidation": ai_data.get("invalidation", ""),
            "mental_note": ai_data.get("mental_note", ""),
        }

        set_last_routing(routing, ai_source, request_status)
        return result

    except Exception as error:
        print(f"[WARN] Gemini failed: {error}")

        record_llm_model_usage(
            config=config,
            model=model,
            mode=routing.get("mode", "unknown"),
            use_grounding=use_grounding,
            status="exception",
            response_status=None,
            error_message=str(error),
        )

        set_last_routing(routing, "unavailable", "exception")
        return unavailable(f"main request exception: {error}")
    

def extract_json_object(text):
    """
    Ambil JSON object dari output Gemini.
    Tetap aman walau Gemini membungkus output dengan ```json ... ```.
    """
    if not text:
        raise ValueError("Empty Gemini response")

    text = text.strip()

    # Remove code fences if any
    text = re.sub(r"^```(?:json|JSON)?\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()
    text = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()

    # Extract first JSON object
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object found in Gemini response: {text[:200]}")

    json_text = text[start:end + 1]
    return json.loads(json_text)

# ============================================================
# Recovery and scenario analysis
# ============================================================

def calculate_recovery_status(config, portfolio):
    recovery_cfg = config.get("recovery", {})
    initial_capital = float(recovery_cfg.get("initial_capital_usdt", 0))

    if initial_capital <= 0:
        return {
            "enabled": False,
            "message": "Recovery tracker disabled: initial_capital_usdt not set.",
        }

    current_value = float(portfolio["total_value"])
    gap = initial_capital - current_value
    gap_pct = (gap / current_value) * 100 if current_value > 0 else 0
    recovered_pct = (current_value / initial_capital) * 100 if initial_capital > 0 else 0

    if gap <= 0:
        status = "RECOVERED"
        message = (
            f"Recovery status: RECOVERED\n"
            f"Initial capital: {initial_capital:.2f} USDT\n"
            f"Current value: {current_value:.2f} USDT\n"
            f"Surplus: {abs(gap):.2f} USDT"
        )
    else:
        status = "NOT_RECOVERED"
        message = (
            f"Recovery status: NOT RECOVERED\n"
            f"Initial capital: {initial_capital:.2f} USDT\n"
            f"Current value: {current_value:.2f} USDT\n"
            f"Gap to break-even: {gap:.2f} USDT ({gap_pct:.1f}% from current portfolio)\n"
            f"Recovered: {recovered_pct:.1f}%"
        )

    return {
        "enabled": True,
        "status": status,
        "initial_capital": initial_capital,
        "current_value": current_value,
        "gap": gap,
        "gap_pct": gap_pct,
        "recovered_pct": recovered_pct,
        "message": message,
    }


def calculate_price_scenarios(portfolio, price_levels, config):
    asset = get_asset_config(config)
    base = asset.get("base", "BTC").lower()
    base_amt = float(portfolio.get(base, 0) or 0)
    usdt = float(portfolio.get("usdt", 0) or 0)

    scenarios = []
    for price in price_levels:
        value = usdt + (base_amt * price)
        scenarios.append({
            "price": price,
            "portfolio_value": value,
        })

    return scenarios


def build_scenario_text(portfolio, config, market):
    current_price = float(market.get("price", 0) or 0)
    if current_price <= 0:
        return "Scenario if no new transaction:\n- N/A"

    price_levels = [
        current_price * 0.5,
        current_price * 0.8,
        current_price,
        current_price * 1.2,
        current_price * 1.5,
        current_price * 2.0
    ]
    scenarios = calculate_price_scenarios(portfolio, price_levels, config)

    asset = get_asset_config(config)
    base = asset.get("base", "BTC")
    price_decimals = asset.get("price_decimals", 2)

    lines = ["Scenario if no new transaction:"]
    for item in scenarios:
        formatted_price = format_price(item['price'], price_decimals)
        lines.append(
            f"- {base} {formatted_price}: portfolio ≈ {item['portfolio_value']:.2f} USDT"
        )

    return "\n".join(lines)
    
# ============================================================
# Message formatting
# ============================================================

def format_open_orders(open_orders):
    if not open_orders:
        return "Open orders: 0"

    lines = [f"Open orders: {len(open_orders)}"]
    for order in open_orders[:5]:
        side = order.get("side")
        order_type = order.get("type")
        price = order.get("price")
        amount = order.get("amount")
        status = order.get("status")
        lines.append(f"- {side} {order_type} @ {price} | amount {amount} | {status}")

    return "\n".join(lines)

def format_action_key_line(action, config):
    key = action.get("key", "UNKNOWN")
    action_type = action.get("type", "hold")
    max_buy = float(action.get("max_buy_usdt", 0) or 0)
    max_sell_pct = float(action.get("max_sell_base_pct_of_holdings", 0) or 0)
    asset = get_asset_config(config) if config else {"base": "BTC"}
    base = asset.get("base", "BTC")

    if key == "HOLD_RECENT_MANUAL_BUY_ANTI_REPEAT":
        hours_since = action.get("hours_since_last_buy")
        price_move = action.get("price_move_pct_from_last_buy")

        details = []

        if hours_since is not None:
            details.append(f"{float(hours_since):.1f}h since last buy")

        if price_move is not None:
            details.append(f"{float(price_move):.2f}% from last buy")

        suffix = f" | {', '.join(details)}" if details else ""
        return f"- {key}{suffix}"

    if key == "PLACE_PULLBACK_LIMIT_BUY":
        limit_price = float(action.get("recommended_limit_price", 0) or 0)
        tier = action.get("exposure_tier", "-")
        return f"- {key} | limit buy {max_buy:.2f} USDT @ ${limit_price:,.0f} | tier: {tier}"

    if action_type == "buy":
        tier = action.get("exposure_tier", "-")
        return f"- {key} | buy up to {max_buy:.2f} USDT | tier: {tier}"

    if action_type == "sell":
        return f"- {key} | sell up to {max_sell_pct:.1f}% of {base} holdings"

    return f"- {key}"


def format_allowed_actions(candidate_actions, config):
    if not candidate_actions:
        return "Allowed actions: none"

    lines = ["Allowed actions:"]
    for action in candidate_actions:
        lines.append(format_action_key_line(action, config))

    return "\n".join(lines)


def find_buy_candidate(candidate_actions):
    for action in candidate_actions or []:
        if action.get("key") == "BUY_SMALL_CONFIRMATION_WITH_LADDER":
            return action

    return None


def find_pullback_limit_candidate(candidate_actions):
    for action in candidate_actions or []:
        if action.get("key") == "PLACE_PULLBACK_LIMIT_BUY":
            return action

    return None





def format_guardrail_check(config, market, portfolio, base_decision,
                           candidate_actions, open_orders, intrahour_order_events):
    """
    Human-readable guardrail summary.

    This replaces the old "Rule Engine" section.
    Rule engine is now presented as baseline protection + allowed action builder,
    not as the final recommendation when Gemini final decision mode is enabled.
    """
    candidate_actions = candidate_actions or []
    open_orders = open_orders or []
    intrahour_order_events = intrahour_order_events or {}

    has_open_orders = bool(open_orders)
    possible_fill = bool(intrahour_order_events.get("has_possible_fill", False))

    target_min = float(config["portfolio"]["target_base_min_pct"])
    target_max = float(config["portfolio"]["target_base_max_pct"])
    base_pct = float(portfolio.get("base_pct", 0) or 0)
    
    asset = get_asset_config(config)
    base = asset.get("base", "BTC")

    if base_pct < target_min:
        allocation_status = "below target"
    elif base_pct > target_max:
        allocation_status = "above target"
    else:
        allocation_status = "within target"

    buy_candidate = find_buy_candidate(candidate_actions)
    pullback_candidate = find_pullback_limit_candidate(candidate_actions)

    anti_repeat_action = None
    for action in candidate_actions or []:
        if action.get("key") == "HOLD_RECENT_MANUAL_BUY_ANTI_REPEAT":
            anti_repeat_action = action
            break

    if buy_candidate:
        buy_gate_status = "open"
        exposure_tier = buy_candidate.get("exposure_tier", "-")
        upside_pct = buy_candidate.get("upside_base_pct_after_buy")
        downside_pct = buy_candidate.get("downside_planned_base_pct_after_buy_and_open_orders")

        exposure_text = (
            f"Exposure tier: {exposure_tier}\n"
            f"Upside {base} allocation after buy: {upside_pct:.1f}%\n"
            f"Downside planned {base} allocation if open orders fill: {downside_pct:.1f}%"
        )
    elif pullback_candidate and anti_repeat_action:
        buy_gate_status = "immediate buy closed; pullback limit available"
        exposure_tier = pullback_candidate.get("exposure_tier", "-")
        limit_price = float(pullback_candidate.get("recommended_limit_price", 0) or 0)
        order_usdt = float(pullback_candidate.get("max_buy_usdt", 0) or 0)
        upside_pct = pullback_candidate.get("upside_base_pct_after_buy")
        downside_pct = pullback_candidate.get("downside_planned_base_pct_after_buy_and_open_orders")

        exposure_text = (
            f"{anti_repeat_action.get('reason', 'Recent manual buy already executed.')}\n"
            f"Adaptive pullback candidate: limit buy {order_usdt:.2f} USDT @ ${limit_price:,.0f}\n"
            f"Exposure tier: {exposure_tier}\n"
            f"{base} allocation after pullback fill: {upside_pct:.1f}%\n"
            f"Downside planned {base} allocation if all orders fill: {downside_pct:.1f}%"
        )
    elif pullback_candidate:
        buy_gate_status = "adaptive pullback limit available"
        exposure_tier = pullback_candidate.get("exposure_tier", "-")
        limit_price = float(pullback_candidate.get("recommended_limit_price", 0) or 0)
        order_usdt = float(pullback_candidate.get("max_buy_usdt", 0) or 0)
        upside_pct = pullback_candidate.get("upside_base_pct_after_buy")
        downside_pct = pullback_candidate.get("downside_planned_base_pct_after_buy_and_open_orders")

        exposure_text = (
            f"Adaptive pullback candidate: limit buy {order_usdt:.2f} USDT @ ${limit_price:,.0f}\n"
            f"Exposure tier: {exposure_tier}\n"
            f"{base} allocation after pullback fill: {upside_pct:.1f}%\n"
            f"Downside planned {base} allocation if all orders fill: {downside_pct:.1f}%"
        )
    elif anti_repeat_action:
        buy_gate_status = "closed by manual execution anti-repeat"
        exposure_text = anti_repeat_action.get("reason", "Recent manual buy already executed.")
    else:
        buy_gate_status = "closed"
        exposure_text = "Exposure tier: no buy candidate"

    lines = [
        "Safety: manual-only; no auto trade, no cancel, no withdrawal.",
        f"Baseline guard: {base_decision.get('signal')}",
        (
            "Baseline meaning: open orders block extra buy by default, "
            "unless a guarded bullish-confirmation candidate is available."
        ),
        f"Open orders active: {'yes' if has_open_orders else 'no'}",
        f"Possible fill detected: {'yes - verify Tokocrypto first' if possible_fill else 'no'}",
        f"Base allocation status: {allocation_status} ({base_pct:.1f}% vs target {target_min:.0f}-{target_max:.0f}%)",
        f"Buy gate: {buy_gate_status}",
        exposure_text,
        format_allowed_actions(candidate_actions, config),
    ]

    return "\n".join(lines)


def format_manual_execution_plan(config, market, decision):
    signal = str(decision.get("signal", "")).upper()
    action_usdt = float(decision.get("action_usdt", 0) or 0)
    asset = get_asset_config(config)
    base = asset.get("base", "BTC")

    execution_cfg = config.get("manual_execution", {})

    buy_order_type_preference = str(
        execution_cfg.get("buy_order_type_preference", "limit")
    ).lower()

    limit_buy_offset_pct = float(
        execution_cfg.get("limit_buy_offset_pct", 0.15) or 0.15
    )

    allow_market_buy_for_small_size = bool(
        execution_cfg.get("allow_market_buy_for_small_size", True)
    )

    market_buy_max_usdt = float(
        execution_cfg.get("market_buy_max_usdt", 15) or 15
    )

    max_acceptable_market_spread_pct = float(
        execution_cfg.get("max_acceptable_market_spread_pct", 0.10) or 0.10
    )

    limit_expiry_guidance_minutes = int(
        execution_cfg.get("limit_expiry_guidance_minutes", 15) or 15
    )

    market_price = float(market.get("price", 0) or 0)

    if decision.get("order_style") == "pullback_limit" and action_usdt > 0:
        limit_price = float(decision.get("recommended_limit_price", 0) or 0)
        estimated_base = float(decision.get("estimated_base_if_filled", 0) or 0)
        expiry_hours = decision.get("expiry_guidance_hours", 24)

        return (
            f"Manual plan: place a LIMIT BUY order only.\n"
            f"Order size: {action_usdt:.2f} USDT.\n"
            f"Limit price: ${limit_price:,.0f}.\n"
            f"Estimated {base} if filled: {estimated_base:.8f} {base}.\n"
            f"This is a pullback entry, not an immediate buy.\n"
            f"Do not market buy this signal.\n"
            f"Do not exceed the recommended size.\n"
            f"Do not place another adaptive pullback order while this one is open.\n"
            f"If placed, add it to manual_open_orders and manual_executions with status: open.\n"
            f"If not filled within ~{expiry_hours}h, review it manually on the next bot reports.\n"
            f"Do not cancel existing 58k/60k ladder orders unless you manually decide to revise the ladder."
        )

    if "BUY" in signal and action_usdt > 0:
        estimated_base = action_usdt / market_price if market_price > 0 else 0
        suggested_limit_price = market_price * (1 - limit_buy_offset_pct / 100) if market_price > 0 else 0

        if buy_order_type_preference == "market":
            order_type_line = (
                "Preferred order type: MARKET BUY, only because config says market is preferred."
            )
        else:
            order_type_line = (
                "Preferred order type: LIMIT BUY."
            )

        market_buy_note = "Market buy: not preferred."

        if allow_market_buy_for_small_size and action_usdt <= market_buy_max_usdt:
            market_buy_note = (
                f"Market buy: acceptable only if Tokocrypto spread is very tight "
                f"(≤ {max_acceptable_market_spread_pct:.2f}%) and you want immediate fill."
            )

        return (
            f"Manual plan: buy {base} worth {action_usdt:.2f} USDT only.\n"
            f"{order_type_line}\n"
            f"Suggested limit price: ${suggested_limit_price:,.0f} "
            f"(~{limit_buy_offset_pct:.2f}% below report price ${market_price:,.0f}).\n"
            f"Estimated {base} if filled near report price: {estimated_base:.8f} {base}.\n"
            f"{market_buy_note}\n"
            f"If the limit order is not filled within ~{limit_expiry_guidance_minutes} minutes, do not chase; wait for the next bot run.\n"
            "Do not exceed the recommended size.\n"
            "Do not cancel existing 58k/60k ladder orders.\n"
            "After execution, update config_git.yaml portfolio values."
        )

    if "SELL" in signal and decision.get("sell_btc") is not None:
        return (
            f"Manual plan: sell approximately {decision.get('sell_btc', 0):.8f} {base} "
            f"(~{decision.get('sell_usdt_estimate', 0):.2f} USDT).\n"
            "Preferred order type: LIMIT SELL near current bid/ask area.\n"
            "Do not use market sell unless you intentionally accept slippage.\n"
            "Do not exceed the recommended sell size.\n"
            "After execution, update config_git.yaml portfolio values."
        )

    if "VERIFY POSSIBLE FILLED ORDER" in signal:
        return (
            "Manual plan: verify Tokocrypto first.\n"
            "Do not buy again until the possible fill is confirmed or rejected.\n"
            "Update config_git.yaml if any order was actually filled."
        )

    return (
        "Manual plan: no new transaction.\n"
        "Keep existing ladder orders unless you manually decide to revise them.\n"
        "Wait for the next clean signal."
    )


def build_message(config, market, portfolio, decision,
                  open_orders=None, ai_explanation="", open_orders_error="",
                  intrahour_order_events=None,
                  base_decision=None,
                  candidate_actions=None,
                  gemini_review=None):
    repo_reminder = build_repo_reminder(config)
    open_orders_text = format_open_orders(open_orders or [])
    intrahour_events_text = format_intrahour_order_events(intrahour_order_events or {})

    recovery = calculate_recovery_status(config, portfolio)
    recovery_text = recovery.get("message", "")
    scenario_text = build_scenario_text(portfolio, config, market)

    portfolio_note = ""
    if portfolio.get("error"):
        portfolio_note = (
            f"<b>Portfolio note:</b> "
            f"{esc(portfolio.get('error'))}\n"
        )

    open_orders_error_text = ""
    if open_orders_error:
        open_orders_error_text = (
            f"<b>Open orders note:</b> {esc(open_orders_error)}\n"
        )

    ai_text = f"\n\n{ai_explanation}" if ai_explanation else ""

    base_decision = base_decision or decision
    candidate_actions = candidate_actions or []
    gemini_review = gemini_review or {}

    guardrail_text = format_guardrail_check(
        config=config,
        market=market,
        portfolio=portfolio,
        base_decision=base_decision,
        candidate_actions=candidate_actions,
        open_orders=open_orders or [],
        intrahour_order_events=intrahour_order_events or {},
    )

    gemini_decision_text = format_gemini_decision_text(
        config=config,
        gemini_review=gemini_review,
        candidate_actions=candidate_actions,
        final_decision=decision,
    )

    manual_execution_plan = format_manual_execution_plan(
    config=config,
    market=market,
    decision=decision,
)

    llm_routing = config.get("_last_llm_routing")

    if not llm_routing:
        try:
            llm_routing = choose_gemini_model(config, market, portfolio, decision)
        except Exception as error:
            llm_routing = {
                "mode": "unavailable",
                "model": "none",
                "use_grounding": False,
                "routing_reason": f"routing unavailable: {error}",
                "quota_reason": "",
                "ai_source": "unavailable",
                "daily_limit": 0,
            }

    usage_state = load_llm_usage_state(config)
    model_name = llm_routing.get("model", "none")
    model_entry = usage_state.get("models", {}).get(model_name, {})
    requests_today = model_entry.get("requests_today", 0)
    daily_limit = llm_routing.get("daily_limit", "?")

    llm_text = (
        f"<b>LLM Routing</b>\n"
        f"Mode: {esc(llm_routing.get('mode', 'unknown'))}\n"
        f"Model: {esc(model_name)}\n"
        f"AI source: {esc(llm_routing.get('ai_source', 'unknown'))}\n"
        f"Grounding: {esc(llm_routing.get('use_grounding', False))}\n"
        f"Reason: {esc(llm_routing.get('routing_reason', ''))}\n"
        f"Quota: {esc(requests_today)}/{esc(daily_limit)} requests today\n\n"
    )

    asset = get_asset_config(config)
    base = asset.get("base", "BTC")
    quote = asset.get("quote", "USDT")
    agent_name = asset.get("agent_name", f"{base} Discipline Agent")

    return (
        f"<b>{esc(agent_name)}</b>\n\n"

        f"<b>Market</b>\n"
        f"{base}/{quote}: <b>{format_price(market['price'], asset['price_decimals'])}</b>\n"
        f"Source: {esc(market.get('source', 'unknown'))}\n"
        f"Regime: <b>{esc(market['regime'])}</b>\n"
        f"24h: {market['change_24h']:.2f}% | "
        f"7d: {market['change_7d']:.2f}% | "
        f"30d: {market['change_30d']:.2f}%\n"
        f"7d range: {format_price(market['low_7d'], asset['price_decimals'])} - {format_price(market['high_7d'], asset['price_decimals'])}\n"
        f"From 7d high: {market['from_7d_high']:.2f}%\n"
        f"MA7: {format_price(market['ma_7'], asset['price_decimals'])} | MA20: {format_price(market['ma_20'], asset['price_decimals'])}\n\n"

        f"<b>Portfolio</b>\n"
        f"Source: {esc(portfolio.get('source', 'unknown'))}\n"
        f"{portfolio_note}"
        f"{base}: {portfolio['base_pct']:.1f}% | {quote}: {portfolio['usdt_pct']:.1f}%\n"
        f"{base} total: {portfolio[base.lower()]:.8f}\n"
        f"{quote} free: {portfolio['usdt_free']:.2f}\n"
        f"{quote} used/open orders: {portfolio['usdt_used']:.2f}\n"
        f"{quote} total: {portfolio['usdt']:.2f}\n"
        f"Total value: <b>{portfolio['total_value']:.2f} {quote}</b>\n\n"

        f"<b>Recovery</b>\n"
        f"{esc(recovery_text)}\n\n"

        f"<b>Scenario</b>\n"
        f"{esc(scenario_text)}\n\n"

        f"<b>Open Orders</b>\n"
        f"{esc(open_orders_text)}\n"
        f"{open_orders_error_text}\n"

        f"<b>Recent Price Check</b>\n"
        f"{esc(intrahour_events_text)}\n\n"

        f"<b>Guardrail Check</b>\n"
        f"{esc(guardrail_text)}\n\n"

        f"<b>Gemini Analysis</b>\n"
        f"{esc(gemini_decision_text)}\n\n"

        f"<b>Final Decision</b>\n"
        f"Signal: <b>{esc(decision['signal'])}</b>\n"
        f"Recommended action: <b>{format_decision_action(decision)}</b>\n"
        f"Reason: {esc(decision['reason'])}\n\n"

        f"<b>Manual Execution Plan</b>\n"
        f"{esc(manual_execution_plan)}\n\n"

        f"<b>Mental rule</b>\n"
        f"Jangan FOMO, jangan revenge trade.\n\n"
        f"{llm_text}"
        f"{esc(repo_reminder)}"
        f"{ai_text}"
    )


# ============================================================
# Journal logging
# ============================================================

def append_signal_log(config, market, portfolio, decision):
    os.makedirs("data", exist_ok=True)

    log_file = "data/signal_log.csv"
    timestamp = datetime.now(timezone.utc).isoformat()

    asset = get_asset_config(config)
    base = asset.get("base", "BTC")

    row = {
        "timestamp": timestamp,
        "price": market["price"],
        "source": market.get("source", "unknown"),
        "regime": market["regime"],
        "change_24h": market["change_24h"],
        "change_7d": market["change_7d"],
        "change_30d": market["change_30d"],
        "base_pct": portfolio["base_pct"],
        "usdt_pct": portfolio["usdt_pct"],
        "base_total": portfolio[base.lower()],
        "usdt_total": portfolio["usdt"],
        "usdt_free": portfolio["usdt_free"],
        "usdt_used": portfolio["usdt_used"],
        "total_value": portfolio["total_value"],
        "signal": decision["signal"],
        "action_usdt": decision["action_usdt"],
        "reason": decision["reason"],
    }

    df = pd.DataFrame([row])
    header = not os.path.exists(log_file)
    df.to_csv(log_file, mode="a", header=header, index=False)

# ============================================================
# Main
# ============================================================

def process_asset(config):
    asset = get_asset_config(config)
    base = asset.get("base", "Unknown")
    name = asset.get("name", "Unknown")

    try:
        coingecko_id = asset.get("coingecko_id", "bitcoin")
        ticker = get_24h_ticker(config["symbol"], config)
        daily = get_daily_klines(config["symbol"], days=30, coingecko_id=coingecko_id)

        market = calculate_market_context(ticker, daily)
        portfolio = calculate_portfolio(config, market["price"])

        intrahour_window = get_recent_intrahour_price_window(config)
        intrahour_order_events = detect_intrahour_order_events(
            config=config,
            intrahour_window=intrahour_window,
            current_price=market["price"],
        )

        base_decision = decide_signal(config, market, portfolio)
        base_decision = adjust_decision_for_intrahour_order_events(
            config=config,
            decision=base_decision,
            intrahour_order_events=intrahour_order_events,
        )

        open_orders = []
        open_orders_error = ""

        if config.get("data_sources", {}).get("use_tokocrypto_open_orders", False):
            open_orders, open_orders_error = fetch_tokocrypto_open_orders()
        else:
            manual_orders_cfg = config.get("manual_open_orders", {})
            if manual_orders_cfg.get("enabled", False):
                open_orders = []
                for order in manual_orders_cfg.get("orders", []):
                    if order.get("status") == "open":
                        open_orders.append({
                            "side": order.get("side"),
                            "type": order.get("type"),
                            "price": order.get("price"),
                            "amount": order.get("amount"),
                            "status": order.get("status"),
                        })

        candidate_actions = build_gemini_candidate_actions(
            config=config,
            market=market,
            portfolio=portfolio,
            open_orders=open_orders,
            intrahour_order_events=intrahour_order_events,
        )

        gemini_review = generate_gemini_explanation(
            config=config,
            market=market,
            portfolio=portfolio,
            decision=base_decision,
            open_orders=open_orders,
            intrahour_order_events=intrahour_order_events,
            candidate_actions=candidate_actions,
        )

        decision = apply_gemini_decision_with_guards(
            config=config,
            base_decision=base_decision,
            gemini_review=gemini_review,
            candidate_actions=candidate_actions,
            market=market,
            portfolio=portfolio,
        )

        append_signal_log(config, market, portfolio, decision)

        manual_execution_plan = format_manual_execution_plan(
            config=config,
            market=market,
            decision=decision,
        )

        guardrail_text = format_guardrail_check(
            config=config,
            market=market,
            portfolio=portfolio,
            base_decision=base_decision,
            candidate_actions=candidate_actions,
            open_orders=open_orders or [],
            intrahour_order_events=intrahour_order_events or {},
        )

        gemini_decision_text = format_gemini_decision_text(
            config=config,
            gemini_review=gemini_review,
            candidate_actions=candidate_actions,
            final_decision=decision,
        )

        scenario_text = build_scenario_text(portfolio, config, market)
        recovery = calculate_recovery_status(config, portfolio)

        llm_routing = config.get("_last_llm_routing")
        if not llm_routing:
            try:
                llm_routing = choose_gemini_model(config, market, portfolio, decision)
            except Exception as error:
                llm_routing = {
                    "mode": "unavailable",
                    "model": "none",
                    "use_grounding": False,
                    "routing_reason": f"routing unavailable: {error}",
                    "quota_reason": "",
                    "ai_source": "unavailable",
                    "daily_limit": 0,
                }

        return {
            "base": base,
            "name": name,
            "config": config,
            "market": market,
            "portfolio": portfolio,
            "decision": decision,
            "open_orders": open_orders,
            "open_orders_error": open_orders_error,
            "intrahour_order_events": intrahour_order_events,
            "base_decision": base_decision,
            "candidate_actions": candidate_actions,
            "gemini_review": gemini_review,
            "manual_execution_plan": manual_execution_plan,
            "guardrail_text": guardrail_text,
            "gemini_decision_text": gemini_decision_text,
            "scenario_text": scenario_text,
            "recovery": recovery,
            "llm_routing": llm_routing,
        }
    except Exception as e:
        print(f"[ERROR] Failed processing asset {base}: {e}")
        import traceback; traceback.print_exc()
        return {
            "base": base,
            "name": name,
            "error": str(e)
        }


def build_single_asset_message(data):
    if "error" in data:
        return f"<b>{esc(data.get('name', 'Unknown'))}</b>\nError: {esc(data['error'])}"
    return build_message(
        config=data["config"],
        market=data["market"],
        portfolio=data["portfolio"],
        decision=data["decision"],
        open_orders=data["open_orders"],
        ai_explanation=data["gemini_review"].get("ai_explanation", ""),
        open_orders_error=data["open_orders_error"],
        intrahour_order_events=data["intrahour_order_events"],
        base_decision=data["base_decision"],
        candidate_actions=data["candidate_actions"],
        gemini_review=data["gemini_review"],
    )


def build_combined_message(assets_data_list, global_config):
    lines = ["<b>Multi-Asset Trading Agent</b>\nStatus: RUNNING\n"]

    # 1. MARKET OVERVIEW
    lines.append("<b>" + "═" * 10 + " MARKET OVERVIEW " + "═" * 10 + "</b>")
    for data in assets_data_list:
        base = data["base"]
        if "error" in data:
            lines.append(f"• <b>{base}</b>: Error - {esc(data['error'])}")
            continue
        
        market = data["market"]
        config = data["config"]
        asset = get_asset_config(config)
        decimals = asset.get("price_decimals", 0)
        quote = asset.get("quote", "USDT")
        
        price_str = format_price(market["price"], decimals)
        change_str = f"{market['change_24h']:.2f}%"
        regime = esc(market["regime"])
        source = esc(market.get("source", "unknown"))
        
        ma7 = format_price(market["ma_7"], decimals)
        ma20 = format_price(market["ma_20"], decimals)
        low7d = format_price(market["low_7d"], decimals)
        high7d = format_price(market["high_7d"], decimals)
        from_7d_high = market["from_7d_high"]
        
        lines.append(
            f"• <b>{base}/{quote}</b>: <b>{price_str}</b> | 24h: {change_str} | Regime: <b>{regime}</b> ({source})\n"
            f"  MA7: {ma7} | MA20: {ma20} | 7d range: {low7d} - {high7d} | From 7d: {from_7d_high:.2f}%"
        )
    lines.append("")

    # 2. PORTFOLIO STATUS
    lines.append("<b>" + "═" * 10 + " PORTFOLIO STATUS " + "═" * 10 + "</b>")
    first_success = next((d for d in assets_data_list if "error" not in d), None)
    if first_success:
        usdt_free = first_success["portfolio"]["usdt_free"]
        usdt_used = first_success["portfolio"]["usdt_used"]
        usdt_total = first_success["portfolio"]["usdt"]
        portfolio_source = esc(first_success["portfolio"].get("source", "unknown"))
    else:
        usdt_free = usdt_used = usdt_total = 0.0
        portfolio_source = "unknown"
        
    lines.append(
        f"Source: {portfolio_source}\n"
        f"USDT: <b>{usdt_free:.2f}</b> Free | <b>{usdt_used:.2f}</b> Used | <b>{usdt_total:.2f}</b> Total\n\n"
        f"<b>Asset Holdings:</b>"
    )
    
    total_token_value = 0.0
    for data in assets_data_list:
        base = data["base"]
        if "error" in data:
            lines.append(f"• <b>{base}</b>: Error calculating holdings.")
            continue
            
        portfolio = data["portfolio"]
        qty = portfolio[base.lower()]
        price = data["market"]["price"]
        val = qty * price
        total_token_value += val
        
        pct = portfolio["base_pct"]
        target_min = float(data["config"]["portfolio"]["target_base_min_pct"])
        target_max = float(data["config"]["portfolio"]["target_base_max_pct"])
        
        if pct < target_min:
            status = "below target"
        elif pct > target_max:
            status = "above target"
        else:
            status = "within target"
            
        lines.append(
            f"• <b>{base}</b>: {qty:.8f} (<b>{val:.2f} USDT</b> | {pct:.1f}%)\n"
            f"  Target: {target_min:.0f}-{target_max:.0f}% | Status: <i>{status}</i>"
        )
        
    total_account_value = usdt_total + total_token_value
    lines.append(f"\n<b>Total Account Value: {total_account_value:.2f} USDT</b>\n")

    # 3. RECOVERY TRACKER
    lines.append("<b>" + "═" * 10 + " RECOVERY TRACKER " + "═" * 10 + "</b>")
    initial_capital = 0.0
    if first_success:
        initial_capital = float(first_success["config"].get("recovery", {}).get("initial_capital_usdt", 0))
        
    if initial_capital > 0:
        gap = initial_capital - total_account_value
        gap_pct = (gap / total_account_value) * 100 if total_account_value > 0 else 0
        recovered_pct = (total_account_value / initial_capital) * 100
        
        if gap <= 0:
            lines.append(
                f"Recovery status: <b>RECOVERED</b>\n"
                f"Initial capital: {initial_capital:.2f} USDT\n"
                f"Current value: {total_account_value:.2f} USDT\n"
                f"Surplus: <b>{abs(gap):.2f} USDT</b>"
            )
        else:
            lines.append(
                f"Recovery status: <b>NOT RECOVERED</b>\n"
                f"Initial capital: {initial_capital:.2f} USDT\n"
                f"Current value: {total_account_value:.2f} USDT\n"
                f"Gap to break-even: <b>{gap:.2f} USDT</b> ({gap_pct:.1f}% from current portfolio)\n"
                f"Recovered: <b>{recovered_pct:.1f}%</b>"
            )
    else:
        lines.append("Recovery tracker: disabled.")
    lines.append("")

    # 4. GUARDRAILS & BUY GATES
    lines.append("<b>" + "═" * 10 + " GUARDRAILS & BUY GATES " + "═" * 10 + "</b>")
    for data in assets_data_list:
        base = data["base"]
        if "error" in data:
            lines.append(f"• <b>{base}</b>: Error fetching guardrails.")
            continue
            
        guardrail = data["guardrail_text"].replace("\n", "\n  ")
        lines.append(f"• <b>{base} Guardrails:</b>\n  {esc(guardrail)}")
    lines.append("")

    # 5. GEMINI AI REVIEW
    lines.append("<b>" + "═" * 10 + " GEMINI AI REVIEW " + "═" * 10 + "</b>")
    for data in assets_data_list:
        base = data["base"]
        if "error" in data:
            lines.append(f"• <b>{base}</b>: Error during Gemini analysis.")
            continue
            
        decision = data["decision"]
        confidence = decision.get("confidence", "?")
        ai_exp = data["gemini_review"].get("ai_explanation", "No analysis returned.")
        ai_exp_indented = ai_exp.replace("\n", "\n  ")
        lines.append(
            f"• <b>{base} Analysis:</b> (Confidence: {confidence}/100)\n"
            f"  {esc(ai_exp_indented)}"
        )
    lines.append("")

    # 6. FINAL DECISIONS & ACTIONS
    lines.append("<b>" + "═" * 10 + " FINAL DECISIONS " + "═" * 10 + "</b>")
    for data in assets_data_list:
        base = data["base"]
        if "error" in data:
            lines.append(f"• <b>{base}</b>: Error making final decision.")
            continue
            
        decision = data["decision"]
        manual_plan = data["manual_execution_plan"].replace("\n", "\n  ")
        
        lines.append(
            f"• <b>{base} Decision:</b>\n"
            f"  Signal: <b>{esc(decision['signal'])}</b>\n"
            f"  Recommended action: <b>{format_decision_action(decision)}</b>\n"
            f"  Reason: {esc(decision['reason'])}\n"
            f"  Manual Plan:\n  {esc(manual_plan)}"
        )
    lines.append("")

    # 7. SYSTEM ROUTING
    lines.append("<b>" + "═" * 10 + " SYSTEM ROUTING " + "═" * 10 + "</b>")
    for data in assets_data_list:
        base = data["base"]
        if "error" in data:
            continue
            
        routing = data.get("llm_routing") or {}
        lines.append(
            f"• <b>{base}</b>: Model: <code>{esc(routing.get('model', 'none'))}</code> | "
            f"Mode: <code>{esc(routing.get('mode', 'unknown'))}</code> | "
            f"Reason: <i>{esc(routing.get('routing_reason', ''))}</i>"
        )
        
    return "\n".join(lines)


def main():
    print("Multi-Asset Agent started")
    config_name = os.getenv("BTC_AGENT_CONFIG", "config_git.yaml")
    try:
        multi_config = load_config(config_name)
    except Exception as e:
        print(f"[FATAL] Could not load {config_name}: {e}")
        return

    # Extract global settings
    global_config = copy.deepcopy(multi_config)
    assets_list = global_config.pop("assets", [])

    prefetch_coingecko_prices(assets_list)

    if not assets_list:
        print(f"[INFO] No 'assets' list found in {config_name}. Running in single-asset mode.")
        try:
            enforce_no_trading(multi_config)
            asset_data = process_asset(multi_config)
            final_message = build_single_asset_message(asset_data)
        except Exception as e:
            print(f"[ERROR] Failed processing single-asset: {e}")
            final_message = f"<b>Error processing single-asset</b>\n{esc(str(e))}"
    else:
        assets_data_list = []
        for asset_override in assets_list:
            symbol = asset_override.get("symbol", "Unknown")
            print(f"\n--- Processing {symbol} ---")
            try:
                # Merge global config with specific asset override
                asset_config = copy.deepcopy(global_config)
                deep_update(asset_config, asset_override)
                
                enforce_no_trading(asset_config)
                
                asset_data = process_asset(asset_config)
                assets_data_list.append(asset_data)
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"[ERROR] Failed processing {symbol}: {e}")
                assets_data_list.append({
                    "base": symbol.split("/")[0],
                    "name": symbol.split("/")[0],
                    "error": str(e)
                })

        final_message = build_combined_message(assets_data_list, global_config)

    print("\n[INFO] Sending report to Telegram...")
    print("----- FINAL REPORT PREVIEW -----")
    try:
        print(final_message)
    except UnicodeEncodeError:
        try:
            print(final_message.encode('ascii', errors='replace').decode('ascii'))
        except Exception:
            print("[INFO] (Could not print full emoji preview in local console due to terminal encoding. Telegram will display it perfectly.)")
    print("--------------------------------")
    send_telegram(final_message)
    print("[INFO] Done!")

    print("Multi-Asset Agent finished")

if __name__ == "__main__":
    main()
