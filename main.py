import os
import json
from datetime import datetime, timezone
import html
import ccxt
import pandas as pd
import requests
import yaml
import re
from dotenv import load_dotenv


load_dotenv()


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

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value

def esc(value):
    return html.escape(str(value), quote=False)


# ============================================================
# Telegram
# ============================================================

def send_telegram(message):
    bot_token = get_required_env("TELEGRAM_BOT_TOKEN")
    chat_id = get_required_env("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    response = requests.post(url, json=payload, timeout=25)

    print(f"Telegram status code: {response.status_code}")
    print(f"Telegram response: {response.text}")

    if not response.ok:
        raise RuntimeError(f"Telegram error: {response.text}")
    if not response.ok:
       print("[WARN] Telegram HTML parse failed. Retrying as plain text.")

       plain_message = re.sub(r"</?[^>]+>", "", message)
       
       plain_payload = {
           "chat_id": chat_id,
           "text": plain_message,
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

    return ccxt.tokocrypto(params)


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


def get_coingecko_price():
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin"
        "&vs_currencies=usd"
        "&include_24hr_change=true"
    )

    data = requests.get(url, timeout=25).json()

    price = float(data["bitcoin"]["usd"])
    change = float(data["bitcoin"].get("usd_24h_change", 0))

    return {
        "price": price,
        "change_pct_24h": change,
        "high_24h": price,
        "low_24h": price,
        "volume": 0,
        "source": "CoinGecko fallback",
    }


def get_24h_ticker(symbol):
    """
    Primary: Tokocrypto price.
    Fallback: CoinGecko.

    Kita tetap coba Tokocrypto dulu karena kamu memakai Tokocrypto.
    Tapi karena CCXT Tokocrypto public ticker di WSL kamu pernah timeout
    ke api.binance.com, fallback CoinGecko wajib.
    """
    try:
        return get_tokocrypto_price_via_ccxt("BTC/USDT")
    except Exception as error:
        print(f"[WARN] Tokocrypto price failed, fallback to CoinGecko: {error}")

    return get_coingecko_price()


def get_daily_klines(symbol, days=30):
    """
    Untuk historical daily data, kita pakai CoinGecko agar stabil.
    Tokocrypto/CCXT OHLCV bisa bermasalah karena routing endpoint.
    """
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    params = {
        "vs_currency": "usd",
        "days": days,
        "interval": "daily",
    }

    data = requests.get(url, params=params, timeout=25).json()
    prices = data["prices"]

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


# ============================================================
# Tokocrypto read-only private data
# ============================================================

def fetch_tokocrypto_portfolio():
    """
    Read-only portfolio fetch.
    Tidak melakukan order, cancel, atau withdrawal.
    """
    exchange = build_tokocrypto_exchange(private=True)
    balance = exchange.fetch_balance()

    btc_info = balance.get("BTC", {})
    usdt_info = balance.get("USDT", {})

    btc_free = float(btc_info.get("free") or 0)
    btc_used = float(btc_info.get("used") or 0)
    btc_total = float(btc_info.get("total") or 0)

    usdt_free = float(usdt_info.get("free") or 0)
    usdt_used = float(usdt_info.get("used") or 0)
    usdt_total = float(usdt_info.get("total") or 0)

    return {
        "btc_free": btc_free,
        "btc_used": btc_used,
        "btc_total": btc_total,
        "usdt_free": usdt_free,
        "usdt_used": usdt_used,
        "usdt_total": usdt_total,
        "source": "Tokocrypto private read-only",
    }


def fetch_tokocrypto_open_orders():
    try:
        print("DEBUG: trying Tokocrypto open orders...", flush=True)

        exchange = build_tokocrypto_exchange(private=True)
        orders = exchange.fetch_open_orders("BTC/USDT")

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

            btc = float(remote["btc_total"])
            usdt = float(remote["usdt_total"])

            btc_free = float(remote["btc_free"])
            btc_used = float(remote["btc_used"])
            usdt_free = float(remote["usdt_free"])
            usdt_used = float(remote["usdt_used"])

            portfolio_source = remote["source"]
            print("DEBUG: Tokocrypto private balance success", flush=True)

        except Exception as error:
            portfolio_error = str(error)
            print(f"[WARN] Tokocrypto balance failed, fallback to config: {portfolio_error}", flush=True)

            usdt = float(config["portfolio"]["usdt"])
            btc = float(config["portfolio"]["btc"])

            btc_free = btc
            btc_used = 0
            usdt_free = usdt
            usdt_used = 0

            portfolio_source = "config_git.yaml fallback"
    else:
        usdt = float(config["portfolio"]["usdt"])
        btc = float(config["portfolio"]["btc"])

        manual_orders_cfg = config.get("manual_open_orders", {})
        manual_orders_enabled = manual_orders_cfg.get("enabled", False)
        manual_orders = manual_orders_cfg.get("orders", []) if manual_orders_enabled else []

        manual_usdt_used = 0.0
        for order in manual_orders:
            if order.get("status") == "open" and order.get("side") == "buy":
                manual_usdt_used += float(order.get("allocated_usdt", 0) or 0)

        btc_free = btc
        btc_used = 0
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

    btc_value = btc * price
    total_value = usdt + btc_value
    btc_pct = (btc_value / total_value) * 100 if total_value > 0 else 0
    usdt_pct = 100 - btc_pct

    return {
        "usdt": usdt,
        "btc": btc,
        "btc_free": btc_free,
        "btc_used": btc_used,
        "usdt_free": usdt_free,
        "usdt_used": usdt_used,
        "btc_value": btc_value,
        "total_value": total_value,
        "btc_pct": btc_pct,
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

    target_min = config["portfolio"]["target_btc_min_pct"]
    target_max = config["portfolio"]["target_btc_max_pct"]
    max_buy = config["portfolio"]["max_single_buy_usdt"]
    reserve = config["portfolio"]["emergency_usdt_reserve"]

    btc_pct = portfolio["btc_pct"]
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

    if btc_pct >= target_max:
        return {
            "signal": "HOLD / TOO MUCH BTC",
            "action_usdt": 0,
            "reason": (
                f"Alokasi BTC sudah {btc_pct:.1f}%, mendekati/di atas batas target "
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
        and btc_pct < target_max
    ):
        action = min(max_buy, available_usdt_after_reserve)
        return {
            "signal": "BUY SMALL - DEEP DIP",
            "action_usdt": action,
            "reason": (
                f"BTC turun {market['from_7d_high']:.1f}% dari 7d high. "
                f"Alokasi BTC masih {btc_pct:.1f}%. Boleh buy kecil, bukan all-in."
            ),
        }

    if (
        strategy["allow_dip_buy"]
        and market["from_7d_high"] <= risk["dip_from_7d_high_pct"]
        and market["from_7d_low"] <= risk["near_7d_low_pct"]
        and available_usdt_after_reserve > 0
        and btc_pct < target_min
    ):
        action = min(max_buy * 0.75, available_usdt_after_reserve)
        return {
            "signal": "BUY SMALL - NEAR RANGE LOW",
            "action_usdt": action,
            "reason": (
                "BTC dekat low 7 hari dan sedang diskon dari 7d high. "
                f"Alokasi BTC masih rendah ({btc_pct:.1f}%)."
            ),
        }

    if (
        strategy["allow_confirmation_buy"]
        and market["regime"] == "bullish_recovery"
        and market["above_ma_20"] >= risk["confirmation_above_ma_pct"]
        and available_usdt_after_reserve > 0
        and btc_pct < target_min
    ):
        action = min(max_buy, available_usdt_after_reserve)
        return {
            "signal": "CONFIRMATION BUY SMALL",
            "action_usdt": action,
            "reason": (
                "BTC berada di atas MA7 dan MA20, indikasi recovery. "
                f"Alokasi BTC masih {btc_pct:.1f}%, boleh tambah kecil."
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


def get_llm_state_file(config=None):
    if config is None:
        return LLM_USAGE_STATE_FILE

    llm_cfg = config.get("llm", {})
    quota_cfg = llm_cfg.get("quota_guard", {})
    return quota_cfg.get("state_file", LLM_USAGE_STATE_FILE)


def load_llm_usage_state(config=None):
    state_file = get_llm_state_file(config)
    os.makedirs(os.path.dirname(state_file) or ".", exist_ok=True)

    today = datetime.now(timezone.utc).date().isoformat()

    if not os.path.exists(state_file):
        return {
            "date_utc": today,
            "grounded_runs_today": 0,
            "last_grounding_bucket_utc": "",
            "models": {},
        }

    try:
        with open(state_file, "r", encoding="utf-8") as file:
            state = json.load(file)
    except Exception:
        state = {}

    if state.get("date_utc") != today:
        return {
            "date_utc": today,
            "grounded_runs_today": 0,
            "last_grounding_bucket_utc": "",
            "models": {},
        }

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

    if response_status == 429:
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


def calculate_recovery_gap_pct(config, portfolio):
    recovery_cfg = config.get("recovery", {})
    initial_capital = float(recovery_cfg.get("initial_capital_usdt", 0))

    if initial_capital <= 0:
        return 0

    current_value = float(portfolio["total_value"])
    gap = initial_capital - current_value

    if gap <= 0 or current_value <= 0:
        return 0

    return (gap / current_value) * 100


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


def build_rule_summary_for_llm(config, market, portfolio, decision, open_orders):
    return {
        "market": {
            "price": market["price"],
            "source": market.get("source"),
            "regime": market["regime"],
            "change_24h": market["change_24h"],
            "change_7d": market["change_7d"],
            "change_30d": market["change_30d"],
            "from_7d_high": market["from_7d_high"],
            "ma_7": market["ma_7"],
            "ma_20": market["ma_20"],
        },
        "portfolio": {
            "btc_pct": portfolio["btc_pct"],
            "usdt_pct": portfolio["usdt_pct"],
            "total_value": portfolio["total_value"],
            "usdt_free": portfolio["usdt_free"],
            "usdt_used": portfolio["usdt_used"],
            "source": portfolio.get("source"),
            "target_btc_min_pct": config["portfolio"]["target_btc_min_pct"],
            "target_btc_max_pct": config["portfolio"]["target_btc_max_pct"],
        },
        "decision": decision,
        "open_orders": open_orders[:5],
    }


def generate_gemini_explanation(config, market, portfolio, decision, open_orders):
    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("enabled", False):
        return ""

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
        return ai_error_fallback

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
        return ai_error_fallback

    model = routing["model"]
    max_output_tokens = routing["max_output_tokens"]
    use_grounding = routing["use_grounding"]
    routing_reason = routing["routing_reason"]

    temperature = float(llm_cfg.get("temperature", 0.2))

    context = build_rule_summary_for_llm(config, market, portfolio, decision, open_orders)

    def set_last_routing(base_routing, ai_source, request_status=""):
        stored = dict(base_routing)
        stored["ai_source"] = ai_source
        stored["request_status"] = request_status
        config["_last_llm_routing"] = stored

    def run_fallback_json(reason):
        """
        Fallback JSON dipakai kalau:
        - main model HTTP error,
        - main model quota/rate-limit error,
        - response utama kosong,
        - response malformed,
        - response tidak punya END_REVIEW.

        Fallback dicoba berantai sesuai llm.fallback_models di config_git.yaml.
        """
        print(f"[WARN] Trying Gemini fallback JSON because: {reason}")

        fallback_configs = get_fallback_model_configs(llm_cfg)

        for fallback_cfg in fallback_configs:
            fallback_model = fallback_cfg["model"]
            fallback_tokens = fallback_cfg["max_output_tokens"]
            fallback_daily_limit = fallback_cfg["daily_limit"]

            fallback_available, fallback_reason = is_model_available(
                config,
                fallback_model,
                fallback_daily_limit,
            )

            if not fallback_available:
                print(f"[WARN] Skipping fallback model {fallback_model}: {fallback_reason}")
                continue

            fallback_routing = {
                "mode": "fallback_json",
                "model": fallback_model,
                "max_output_tokens": fallback_tokens,
                "daily_limit": fallback_daily_limit,
                "use_grounding": False,
                "routing_reason": f"fallback JSON because: {reason}",
                "quota_reason": fallback_reason,
                "ai_source": "fallback_json",
            }

            fallback_url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{fallback_model}:generateContent?key={api_key}"
            )

            fallback_payload = {
                "contents": [
                    {
                        "parts": [
                            {
                                "text": (
                                    "You are a portfolio-aware BTC market analyst for a manual-only BTC Discipline Agent. "
                                    "Return ONLY valid JSON. No Markdown. No HTML. No code fences. "
                                    "Do not override the rule-engine signal. "
                                    "Do not suggest leverage, futures, margin, auto-trading, or all-in. "
                                    "You may agree or cautiously disagree with the rule-engine signal, but do not override it. "
                                    "If open orders are active, do not recommend extra manual BTC buy orders. "
                                    "If BTC allocation is below target but open orders are active, usually use cautious_agree. "
                                    "Use this JSON schema exactly: "
                                    "{"
                                    "\"agreement_with_rule\":\"agree | cautious_agree | disagree_but_do_not_override\","
                                    "\"confidence_score\":0,"
                                    "\"market_thesis\":\"concise market thesis\","
                                    "\"portfolio_diagnosis\":\"portfolio allocation diagnosis\","
                                    "\"recovery_assessment\":\"recovery realism assessment\","
                                    "\"risk_assessment\":\"main risk assessment\","
                                    "\"suggested_manual_plan\":\"manual-only plan\","
                                    "\"invalidation\":\"specific invalidation condition\","
                                    "\"mental_note\":\"short mental note\""
                                    "} "
                                    f"Data: {context}"
                                )
                            }
                        ]
                    }
                ],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": fallback_tokens,
                    "responseMimeType": "application/json",
                }
            }

            try:
                fallback_response = requests.post(fallback_url, json=fallback_payload, timeout=30)

                record_llm_model_usage(
                    config=config,
                    model=fallback_model,
                    mode="fallback_json",
                    use_grounding=False,
                    status="ok" if fallback_response.ok else "http_error",
                    response_status=fallback_response.status_code,
                    error_message=fallback_response.text if not fallback_response.ok else "",
                )

                if not fallback_response.ok:
                    print(
                        f"[WARN] Gemini fallback error with {fallback_model}: "
                        f"{fallback_response.status_code} {fallback_response.text}"
                    )
                    continue

                fallback_data = fallback_response.json()
                candidates = fallback_data.get("candidates", [])
                if not candidates:
                    print("[WARN] Gemini fallback returned no candidates")
                    continue

                parts = candidates[0].get("content", {}).get("parts", [])
                if not parts:
                    print("[WARN] Gemini fallback returned no parts")
                    continue

                raw_text = parts[0].get("text", "").strip()
                if not raw_text:
                    print("[WARN] Gemini fallback returned empty text")
                    continue

                try:
                    ai_data = extract_json_object(raw_text)
                    set_last_routing(fallback_routing, "fallback_json", "success")
                    return format_ai_review_from_json(ai_data)
                except Exception as parse_error:
                    print(f"[WARN] Gemini fallback JSON parse failed: {parse_error}")
                    print(f"[WARN] Raw fallback output: {raw_text[:500]}")
                    continue

            except Exception as fallback_error:
                print(f"[WARN] Gemini fallback failed with {fallback_model}: {fallback_error}")

                record_llm_model_usage(
                    config=config,
                    model=fallback_model,
                    mode="fallback_json",
                    use_grounding=False,
                    status="exception",
                    response_status=None,
                    error_message=str(fallback_error),
                )

                continue

        config["_last_llm_routing"] = {
            "mode": "unavailable",
            "model": "none",
            "use_grounding": False,
            "routing_reason": f"all fallback models failed after: {reason}",
            "quota_reason": "",
            "ai_source": "unavailable",
        }
        return ai_error_fallback

    grounding_instruction = ""
    if use_grounding:
        grounding_instruction = """
You have Google Search grounding enabled.
Use it only to assess broad current market context such as:
- crypto market sentiment,
- Bitcoin-related macro or ETF headlines,
- global risk appetite,
- major risk-on/risk-off catalysts.

Do not overreact to one headline.
Do not cite rumors as facts.
Do not turn news sentiment into an aggressive trading recommendation.
"""

    prompt = f"""
You are a crypto decision-support analyst for a manual-only BTC Discipline Agent.

The bot is NOT allowed to trade.
The user manually executes decisions.
You must respect the rule-engine signal.
Do not override the signal.
Do not tell the user to all-in.
Do not suggest leverage, futures, margin, or high-risk behavior.

Model routing:
- Mode used: {routing.get("mode")}
- Model used: {model}
- Routing reason: {routing_reason}
- Google Search grounding enabled: {use_grounding}

{grounding_instruction}

Your task:
Act as a portfolio-aware BTC market analyst, not merely a rule explainer.
Analyze the BTC/USDT situation based on:
- rule-engine signal,
- market regime,
- portfolio allocation,
- recovery gap,
- open orders,
- risk and mental discipline.

Make the analysis deeper than a simple HOLD explanation.
Include:
- market structure,
- portfolio exposure,
- open-order ladder implication,
- recovery pressure,
- behavioral risk,
- what would change the next decision.

You may agree or cautiously disagree with the rule-engine signal, but you must NOT override it.
The final decision remains manual-only and rule-engine guarded.
Do NOT recommend auto-trading.
Do NOT suggest leverage, futures, margin, or all-in behavior.

Use natural English, concise but analytical.
Do NOT repeat the full numeric report.
Do NOT list all open orders again.
Do NOT summarize every raw number.
Mention only the most important numbers if necessary.
Give interpretation only.

If open orders are active:
- Explain that extra manual BTC buy orders should NOT be placed.
- The reason is to avoid double entry because existing buy orders are already active.
- Do NOT say "no additional USDT actions"; the issue is not USDT, the issue is extra manual BTC buying.

Allocation interpretation:
- If btc_pct is below target_btc_min_pct, say BTC allocation is BELOW TARGET for recovery mode.
- If btc_pct is between target_btc_min_pct and target_btc_max_pct, say it is within target range.
- If btc_pct is above target_btc_max_pct, say it is too aggressive.

Agreement rules:
- agreement_with_rule must be one of: "agree", "cautious_agree", "disagree_but_do_not_override".
- confidence_score must be an integer from 0 to 100.
- If the portfolio is underallocated to BTC but open orders are active, usually use "cautious_agree".
- If the rule signal is HOLD because open orders are active, do not recommend extra manual BTC buying.

Return ONLY Telegram-compatible HTML.
Do not use Markdown.
Do not use JSON.
Do not use code fences.
Do not write any intro sentence.
Start exactly with <b>AI Analyst Review</b>.

Use exactly these sections:
<b>AI Analyst Review</b>
<b>Rule Agreement</b>
<b>Confidence</b>
<b>Market Thesis</b>
<b>Portfolio Diagnosis</b>
<b>Recovery Assessment</b>
<b>Risk Assessment</b>
<b>Suggested Manual Plan</b>
<b>Invalidation</b>
<b>Mental Note</b>

Format:
<b>AI Analyst Review</b>

<b>Rule Agreement</b>
agree | cautious_agree | disagree_but_do_not_override

<b>Confidence</b>
70/100

<b>Market Thesis</b>
...

<b>Portfolio Diagnosis</b>
...

<b>Recovery Assessment</b>
...

<b>Risk Assessment</b>
...

<b>Suggested Manual Plan</b>
...

<b>Invalidation</b>
...

<b>Mental Note</b>
...

END_REVIEW

Keep each section concise.
Total response under 320 words.
Do not stop before END_REVIEW.

Data:
{context}
"""

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
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
            "maxOutputTokens": max_output_tokens,
        },
    }

    if use_grounding:
        payload["tools"] = [
            {
                "google_search": {}
            }
        ]

    try:
        response = requests.post(url, json=payload, timeout=45)

        record_llm_model_usage(
            config=config,
            model=model,
            mode=routing.get("mode", "unknown"),
            use_grounding=use_grounding,
            status="ok" if response.ok else "http_error",
            response_status=response.status_code,
            error_message=response.text if not response.ok else "",
        )

        if not response.ok:
            print(f"[WARN] Gemini error with {model}: {response.status_code} {response.text}")
            return run_fallback_json(f"main request HTTP error {response.status_code}")

        data = response.json()

        candidates = data.get("candidates", [])
        if not candidates:
            return run_fallback_json("main response has no candidates")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return run_fallback_json("main response has no parts")

        raw_text = parts[0].get("text", "").strip()
        if not raw_text:
            return run_fallback_json("main response text is empty")

        end_markers = ["END_REVIEW", "End Review", "END REVIEW"]
        if not any(marker in raw_text for marker in end_markers):
            print(f"[WARN] Gemini HTML output incomplete: {raw_text[:500]}")
            return run_fallback_json("main HTML output missing END_REVIEW")

        for marker in end_markers:
            raw_text = raw_text.replace(marker, "")

        cleaned_text = clean_ai_explanation(raw_text)

        if not cleaned_text:
            return run_fallback_json("cleaned main HTML output is empty")

        set_last_routing(routing, "main_html", "success")
        return cleaned_text

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

        return run_fallback_json(f"main request exception: {error}")


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


def format_ai_review_from_json(ai_data):
    """
    Render structured Gemini JSON into Telegram-safe HTML.
    Gemini sekarang bukan hanya explainer, tapi analyst layer.
    """
    required_keys = [
        "agreement_with_rule",
        "confidence_score",
        "market_thesis",
        "portfolio_diagnosis",
        "recovery_assessment",
        "risk_assessment",
        "suggested_manual_plan",
        "invalidation",
        "mental_note",
    ]

    for key in required_keys:
        ai_data.setdefault(key, "")

    try:
        confidence_score = float(ai_data["confidence_score"])
        if 0 <= confidence_score <= 1:
            confidence_score = confidence_score * 100
        confidence_score = int(round(confidence_score))
    except Exception:
        confidence_score = ai_data["confidence_score"]

    return (
        f"<b>AI Analyst Review</b>\n\n"
        f"<b>Rule Agreement</b>\n"
        f"{esc(ai_data['agreement_with_rule'])}\n\n"
        f"<b>Confidence</b>\n"
        f"<b>{esc(confidence_score)}/100</b>\n\n"
        f"<b>Market Thesis</b>\n"
        f"{esc(ai_data['market_thesis'])}\n\n"
        f"<b>Portfolio Diagnosis</b>\n"
        f"{esc(ai_data['portfolio_diagnosis'])}\n\n"
        f"<b>Recovery Assessment</b>\n"
        f"{esc(ai_data['recovery_assessment'])}\n\n"
        f"<b>Risk Assessment</b>\n"
        f"{esc(ai_data['risk_assessment'])}\n\n"
        f"<b>Suggested Manual Plan</b>\n"
        f"{esc(ai_data['suggested_manual_plan'])}\n\n"
        f"<b>Invalidation</b>\n"
        f"{esc(ai_data['invalidation'])}\n\n"
        f"<b>Mental Note</b>\n"
        f"{esc(ai_data['mental_note'])}"
    )


def clean_ai_explanation(text):
    """
    Membersihkan output Gemini agar rapi di Telegram HTML mode.
    Tujuan:
    - Hapus pembuka seperti 'Berikut adalah...'
    - Hapus Markdown code fences ```html ... ```
    - Convert Markdown bold ke HTML bold
    - Hapus bullet Markdown
    - Escape teks berbahaya tanpa merusak tag HTML Telegram yang kita izinkan
    """
    if not text:
        return ""

    text = text.strip()

    # Remove common Gemini preamble
    preamble_patterns = [
        r"^Berikut adalah.*?:\s*",
        r"^Berikut penjelasan.*?:\s*",
        r"^Tentu,.*?:\s*",
        r"^Sure,.*?:\s*",
        r"^Here is.*?:\s*",
    ]

    for pattern in preamble_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.DOTALL).strip()

    # Remove markdown code fences: ```html, ```HTML, ```
    text = re.sub(r"^```(?:html|HTML)?\s*", "", text).strip()
    text = re.sub(r"\s*```$", "", text).strip()

    # If Gemini wraps the full output inside code fences somewhere in the text
    text = text.replace("```html", "")
    text = text.replace("```HTML", "")
    text = text.replace("```", "")

    # Convert Markdown bold to Telegram HTML bold
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text)

    # Remove Markdown bullet style
    text = re.sub(r"^\*\s+", "- ", text, flags=re.MULTILINE)

    # Remove excessive indentation
    text = re.sub(r"^[ \t]+", "", text, flags=re.MULTILINE)

    # Reduce excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Keep only Telegram-safe HTML tags we intentionally allow.
    # Escape all angle brackets first, then unescape allowed tags.
    allowed_tags = [
        "b", "/b",
        "i", "/i",
        "u", "/u",
        "s", "/s",
        "code", "/code",
        "pre", "/pre",
    ]

    # Temporarily protect allowed tags
    protected = {}
    for i, tag in enumerate(allowed_tags):
        raw = f"<{tag}>"
        key = f"__TAG_{i}__"
        protected[key] = raw
        text = text.replace(raw, key)

    # Escape remaining HTML
    text = html.escape(text, quote=False)

    # Restore allowed tags
    for key, raw in protected.items():
        text = text.replace(key, raw)

    # Balance simple Telegram HTML tags to prevent Telegram parse errors.
    # Gemini sometimes outputs <b> without </b>.
    for tag in ["b", "i", "u", "s", "code", "pre"]:
        open_count = len(re.findall(fr"<{tag}>", text))
        close_count = len(re.findall(fr"</{tag}>", text))

        if open_count > close_count:
            text += "".join(f"</{tag}>" for _ in range(open_count - close_count))

    return text.strip()


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


def calculate_price_scenarios(portfolio, price_levels):
    btc = float(portfolio["btc"])
    usdt = float(portfolio["usdt"])

    scenarios = []
    for price in price_levels:
        value = usdt + (btc * price)
        scenarios.append({
            "price": price,
            "portfolio_value": value,
        })

    return scenarios


def build_scenario_text(portfolio):
    price_levels = [50000, 55000, 60000, 65000, 70000, 75000]
    scenarios = calculate_price_scenarios(portfolio, price_levels)

    lines = ["Scenario if no new transaction:"]
    for item in scenarios:
        lines.append(
            f"- BTC ${item['price']:,.0f}: portfolio ≈ {item['portfolio_value']:.2f} USDT"
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


def build_message(config, market, portfolio, decision,
                  open_orders=None, ai_explanation="", open_orders_error=""):
    repo_reminder = build_repo_reminder(config)
    open_orders_text = format_open_orders(open_orders or [])

    recovery = calculate_recovery_status(config, portfolio)
    recovery_text = recovery.get("message", "")
    scenario_text = build_scenario_text(portfolio)

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

    return (
        f"<b>BTC Discipline Agent</b>\n\n"

        f"<b>Market</b>\n"
        f"BTC/USDT: <b>${market['price']:,.0f}</b>\n"
        f"Source: {esc(market.get('source', 'unknown'))}\n"
        f"Regime: <b>{esc(market['regime'])}</b>\n"
        f"24h: {market['change_24h']:.2f}% | "
        f"7d: {market['change_7d']:.2f}% | "
        f"30d: {market['change_30d']:.2f}%\n"
        f"7d range: ${market['low_7d']:,.0f} - ${market['high_7d']:,.0f}\n"
        f"From 7d high: {market['from_7d_high']:.2f}%\n"
        f"MA7: ${market['ma_7']:,.0f} | MA20: ${market['ma_20']:,.0f}\n\n"

        f"<b>Portfolio</b>\n"
        f"Source: {esc(portfolio.get('source', 'unknown'))}\n"
        f"{portfolio_note}"
        f"BTC: {portfolio['btc_pct']:.1f}% | USDT: {portfolio['usdt_pct']:.1f}%\n"
        f"BTC total: {portfolio['btc']:.8f}\n"
        f"USDT free: {portfolio['usdt_free']:.2f}\n"
        f"USDT used/open orders: {portfolio['usdt_used']:.2f}\n"
        f"USDT total: {portfolio['usdt']:.2f}\n"
        f"Total value: <b>{portfolio['total_value']:.2f} USDT</b>\n\n"

        f"<b>Recovery</b>\n"
        f"{esc(recovery_text)}\n\n"

        f"<b>Scenario</b>\n"
        f"{esc(scenario_text)}\n\n"

        f"<b>Open Orders</b>\n"
        f"{esc(open_orders_text)}\n"
        f"{open_orders_error_text}\n"

        f"<b>Decision</b>\n"
        f"Signal: <b>{esc(decision['signal'])}</b>\n"
        f"Recommended action: <b>{decision['action_usdt']:.2f} USDT</b>\n"
        f"Reason: {esc(decision['reason'])}\n\n"

        f"<b>Mental rule</b>\n"
        f"Jangan FOMO, jangan revenge trade.\n\n"
        f"{llm_text}"
        f"{esc(repo_reminder)}"
        f"{ai_text}"
    )

# ============================================================
# Journal logging
# ============================================================

def append_signal_log(market, portfolio, decision):
    os.makedirs("data", exist_ok=True)

    log_file = "data/signal_log.csv"
    timestamp = datetime.now(timezone.utc).isoformat()

    row = {
        "timestamp": timestamp,
        "price": market["price"],
        "source": market.get("source", "unknown"),
        "regime": market["regime"],
        "change_24h": market["change_24h"],
        "change_7d": market["change_7d"],
        "change_30d": market["change_30d"],
        "btc_pct": portfolio["btc_pct"],
        "usdt_pct": portfolio["usdt_pct"],
        "btc_total": portfolio["btc"],
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

def main():
    print("BTC Discipline Agent started")

    config = load_config()
    enforce_no_trading(config)

    ticker = get_24h_ticker(config["symbol"])
    daily = get_daily_klines(config["symbol"], days=30)

    market = calculate_market_context(ticker, daily)
    portfolio = calculate_portfolio(config, market["price"])
    decision = decide_signal(config, market, portfolio)
    append_signal_log(market, portfolio, decision)

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

    ai_explanation = generate_gemini_explanation(
        config=config,
        market=market,
        portfolio=portfolio,
        decision=decision,
        open_orders=open_orders,
    )

    message = build_message(
        config=config,
        market=market,
        portfolio=portfolio,
        decision=decision,
        open_orders=open_orders,
        ai_explanation=ai_explanation,
        open_orders_error=open_orders_error,
    )

    send_telegram(message)

    print("BTC Discipline Agent finished")


if __name__ == "__main__":
    main()
