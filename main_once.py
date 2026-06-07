import os
from datetime import datetime, timezone

import requests
import yaml
import pandas as pd


CONFIG_FILE = "config_git.yaml"


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


def get_24h_ticker(symbol):
    """
    Try Binance first. If Binance fails because of timeout/restriction,
    fallback to CoinGecko.
    """

    # 1. Try Binance
    try:
        url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
        data = requests.get(url, timeout=12).json()

        if "lastPrice" not in data:
            raise RuntimeError(f"Unexpected Binance response: {data}")

        return {
            "price": float(data["lastPrice"]),
            "change_pct_24h": float(data["priceChangePercent"]),
            "high_24h": float(data["highPrice"]),
            "low_24h": float(data["lowPrice"]),
            "volume": float(data["volume"]),
            "source": "Binance",
        }

    except Exception as binance_error:
        print(f"[WARN] Binance ticker failed: {binance_error}")

    # 2. Fallback CoinGecko
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
    Try Binance daily klines first. If unavailable, fallback to CoinGecko.
    """

    # 1. Try Binance
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {"symbol": symbol, "interval": "1d", "limit": days}
        raw = requests.get(url, params=params, timeout=12).json()

        if not isinstance(raw, list):
            raise RuntimeError(f"Unexpected Binance klines response: {raw}")

        rows = []
        for item in raw:
            rows.append({
                "time": pd.to_datetime(item[0], unit="ms"),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            })

        return pd.DataFrame(rows)

    except Exception as binance_error:
        print(f"[WARN] Binance klines failed: {binance_error}")

    # 2. Fallback CoinGecko
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
    usdt = float(config["portfolio"]["usdt"])
    btc = float(config["portfolio"]["btc"])

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
    }


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


def build_message(config, market, portfolio, decision):
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
        f"BTC: {portfolio['btc_pct']:.1f}% | USDT: {portfolio['usdt_pct']:.1f}%\n"
        f"Total: {portfolio['total_value']:.2f} USDT\n\n"
        f"Signal: {decision['signal']}\n"
        f"Recommended action: {decision['action_usdt']:.2f} USDT\n"
        f"Reason: {decision['reason']}\n\n"
        f"Mental rule: jangan FOMO, jangan revenge trade."
        f"{repo_reminder}"
    )


def main():
    config = load_config()

    ticker = get_24h_ticker(config["symbol"])
    daily = get_daily_klines(config["symbol"], days=30)

    market = calculate_market_context(ticker, daily)
    portfolio = calculate_portfolio(config, market["price"])
    decision = decide_signal(config, market, portfolio)

    message = build_message(config, market, portfolio, decision)
    send_telegram(message)


if __name__ == "__main__":
    main()
