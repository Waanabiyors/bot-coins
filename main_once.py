import os
from datetime import datetime, timezone

import requests
import yaml
import pandas as pd
import ccxt

CONFIG_FILE = "config_git.yaml"

TRADING_FORBIDDEN = True


def enforce_no_trading(config):
    safety = config.get("safety", {})

    if safety.get("allow_trading", False):
        raise RuntimeError("Safety violation: allow_trading must remain false.")

    if safety.get("allow_withdrawal", False):
        raise RuntimeError("Safety violation: allow_withdrawal must remain false.")

    if safety.get("allow_order_cancel", False):
        raise RuntimeError("Safety violation: allow_order_cancel must remain false.")


def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def get_required_env(name):
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def send_telegram(message):
    bot_token = get_required_env("TELEGRAM_BOT_TOKEN")
    chat_id = get_required_env("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    response = requests.post(url, json=payload, timeout=25)
    if not response.ok:
        raise RuntimeError(f"Telegram error: {response.text}")

def build_tokocrypto_exchange(private=False):
    params = {
        "enableRateLimit": True,
    }

    if private:
        api_key = os.getenv("TOKOCRYPTO_API_KEY")
        api_secret = os.getenv("TOKOCRYPTO_API_SECRET")

        if not api_key or not api_secret:
            raise RuntimeError("Tokocrypto private API key/secret belum tersedia.")

        params["apiKey"] = api_key
        params["secret"] = api_secret

    return ccxt.tokocrypto(params)
    
def get_24h_ticker(symbol):
    """
    Price source: CoinGecko.
    We do NOT use ccxt.tokocrypto.fetch_ticker because it routes to api.binance.com
    and may timeout from WSL/GitHub Actions.
    """
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
        "source": "CoinGecko",
    }


def get_daily_klines(symbol, days=30):
    """
    Daily price source: CoinGecko.
    This avoids ccxt.tokocrypto OHLCV/ticker routing issues.
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

def fetch_tokocrypto_portfolio():
    """
    Read-only portfolio fetch.
    Tidak melakukan order, cancel, atau withdrawal.
    """
    exchange = build_tokocrypto_exchange(private=True)
    balance = exchange.fetch_balance()

    btc_total = float(balance.get("BTC", {}).get("total") or 0)
    usdt_total = float(balance.get("USDT", {}).get("total") or 0)

    return {
        "btc": btc_total,
        "usdt": usdt_total,
        "source": "Tokocrypto private read-only",
    }


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


def calculate_portfolio(config, price):
    data_sources = config.get("data_sources", {})
    use_private_balance = data_sources.get("use_tokocrypto_private_balance", False)

    if use_private_balance:
        try:
            remote = fetch_tokocrypto_portfolio()
            usdt = float(remote["usdt"])
            btc = float(remote["btc"])
            portfolio_source = remote["source"]
        except Exception as error:
            print(f"[WARN] Tokocrypto balance failed, fallback to config: {error}")
            usdt = float(config["portfolio"]["usdt"])
            btc = float(config["portfolio"]["btc"])
            portfolio_source = "config_git.yaml fallback"
    else:
        usdt = float(config["portfolio"]["usdt"])
        btc = float(config["portfolio"]["btc"])
        portfolio_source = "config_git.yaml"

    btc_value = btc * price
    total_value = usdt + btc_value
    btc_pct = (btc_value / total_value) * 100 if total_value > 0 else 0
    usdt_pct = 100 - btc_pct

    return {
        "usdt": usdt,
        "btc": btc,
        "btc_value": btc_value,
        "total_value": total_value,
        "btc_pct": btc_pct,
        "usdt_pct": usdt_pct,
        "source": portfolio_source,
    }

def fetch_tokocrypto_open_orders():
    try:
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

        return simplified

    except Exception as error:
        print(f"[WARN] Tokocrypto open orders failed: {error}")
        return []

def build_rule_summary_for_llm(market, portfolio, decision, open_orders):
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
            "source": portfolio.get("source"),
        },
        "decision": decision,
        "open_orders": open_orders[:5],
    }


def generate_gemini_explanation(config, market, portfolio, decision, open_orders):
    llm_cfg = config.get("llm", {})
    if not llm_cfg.get("enabled", False):
        return ""

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return ""

    model = llm_cfg.get("model", "gemini-2.5-flash-lite")
    max_output_tokens = int(llm_cfg.get("max_output_tokens", 300))
    temperature = float(llm_cfg.get("temperature", 0.2))

    context = build_rule_summary_for_llm(market, portfolio, decision, open_orders)

    prompt = f"""
You are a crypto decision-support explainer.
You are NOT allowed to recommend auto-trading.
You must respect the rule-engine signal.
Do not override the signal.
Do not tell the user to all-in.
Do not suggest leverage, futures, margin, or high-risk behavior.

Explain the following BTC/USDT decision in Indonesian, concise but clear.
Focus on risk, position sizing, and mental discipline.

Data:
{context}

Output format:
AI Explanation:
- Market:
- Portfolio:
- Action:
- Mental note:
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

    try:
        response = requests.post(url, json=payload, timeout=30)
        if not response.ok:
            print(f"[WARN] Gemini error: {response.status_code} {response.text}")
            return ""

        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return ""

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return ""

        return parts[0].get("text", "").strip()

    except Exception as error:
        print(f"[WARN] Gemini failed: {error}")
        return ""


def decide_signal(config, market, portfolio):
    risk = config["risk"]
    strategy = config["strategy"]
    mental = config["mental"]

    target_min = config["portfolio"]["target_btc_min_pct"]
    target_max = config["portfolio"]["target_btc_max_pct"]
    max_buy = config["portfolio"]["max_single_buy_usdt"]
    reserve = config["portfolio"]["emergency_usdt_reserve"]

    btc_pct = portfolio["btc_pct"]
    available_usdt_after_reserve = max(0, portfolio["usdt"] - reserve)

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
            "reason": f"Alokasi BTC sudah {btc_pct:.1f}%, mendekati/di atas batas target {target_max}%. Jangan tambah BTC.",
        }

    if market["change_24h"] <= risk["dump_24h_pct"]:
        return {
            "signal": "WAIT / NO PANIC",
            "action_usdt": 0,
            "reason": "BTC sedang dump harian. Jangan langsung tangkap pisau jatuh. Tunggu stabilisasi.",
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
            f"Segera buat commit kecil agar scheduled workflow tidak mendekati disable 60 hari."
        )

    if days >= remind_after:
        return (
            f"\n\nGitHub repo reminder: repo terakhir update {days} hari lalu ({last_date}). "
            f"Disarankan buat commit kecil sebelum hari ke-60."
        )

    return ""

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
                  open_orders=None, ai_explanation=""):
    repo_reminder = build_repo_reminder(config)

    return (
        f"BTC Discipline Agent\n\n"
        f"BTC/USDT: ${market['price']:,.0f}\n"
        f"Source: {market.get('source', 'unknown')}\n"
        f"Regime: {market['regime']}\n"
        f"24h: {market['change_24h']:.2f}% | "
        f"7d: {market['change_7d']:.2f}% | "
        f"30d: {market['change_30d']:.2f}%\n"
        f"7d range: ${market['low_7d']:,.0f} - ${market['high_7d']:,.0f}\n"
        f"From 7d high: {market['from_7d_high']:.2f}%\n"
        f"MA7: ${market['ma_7']:,.0f} | MA20: ${market['ma_20']:,.0f}\n\n"
        f"Portfolio:\n"
        f"Source: {portfolio.get('source', 'unknown')}\n"
        f"BTC: {portfolio['btc_pct']:.1f}% | USDT: {portfolio['usdt_pct']:.1f}%\n"
        f"Total: {portfolio['total_value']:.2f} USDT\n\n"
        f"Signal: {decision['signal']}\n"
        f"Recommended action: {decision['action_usdt']:.2f} USDT\n"
        f"Reason: {decision['reason']}\n\n"
        f"Mental rule: jangan FOMO, jangan revenge trade."
        f"{repo_reminder}"
        f"\n\nOpen orders: {len(open_orders or [])}"
        f"{repo_reminder}"
        f"\n\n{ai_explanation if ai_explanation else ''}"
    )


def main():
    config = load_config()
    enforce_no_trading(config)

    ticker = get_24h_ticker(config["symbol"])
    daily = get_daily_klines(config["symbol"], days=30)

    market = calculate_market_context(ticker, daily)
    portfolio = calculate_portfolio(config, market["price"])
    decision = decide_signal(config, market, portfolio)

    open_orders = []
    if config.get("data_sources", {}).get("use_tokocrypto_open_orders", False):
        open_orders = fetch_tokocrypto_open_orders()

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
    )

    send_telegram(message)
