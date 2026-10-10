import os
import time
from datetime import datetime, timezone
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

# ==============================================================================
# YAPILANDIRMA
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
MIN_CANDLE_PCT = float(os.getenv("MIN_CANDLE_PCT", 2.0))
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", 3.0))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 180))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 10))
DEBUG_LOG = os.getenv("DEBUG_LOG", "false").lower() == "true"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

API_BASE = "https://fapi.binance.com"

session = requests.Session()
session.mount("https://", HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS))

# Aynı mum için tekrar bildirim göndermemek için: {symbol: m2_open_time}
last_alerted = {}


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Uyarı] Telegram ayarlı değil (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID boş).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"[Hata] Telegram {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[Hata] Telegram bildirimi gönderilemedi: {e}")


def calculate_strategy_indicators(df):
    df = df.copy()
    df["taker_sell_base"] = df["volume"] - df["taker_buy_base"]
    df["delta"] = df["taker_buy_base"] - df["taker_sell_base"]
    return df


def get_usdt_symbols():
    try:
        r = session.get(f"{API_BASE}/fapi/v1/exchangeInfo", timeout=15)
        if r.status_code != 200:
            print(f"[Hata] exchangeInfo HTTP {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        return [
            s["symbol"]
            for s in data.get("symbols", [])
            if s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
        ]
    except Exception as e:
        print(f"[Hata] Sembol listesi alınamadı: {e}")
        return []


def analyze_symbol(symbol: str):
    """(durum, veri) döner. Durum: api_error, rate_limit, short_data,
    no_spike, no_direction, signal, exception"""
    try:
        res = session.get(
            f"{API_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": 40},
            timeout=10,
        )

        if res.status_code in (418, 429):
            return "rate_limit", f"HTTP {res.status_code}"
        if res.status_code != 200:
            return "api_error", f"HTTP {res.status_code}: {res.text[:100]}"

        klines = res.json()
        if not isinstance(klines, list) or len(klines) < 30:
            return "short_data", None

        df = pd.DataFrame(klines, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ])

        # Canlı (kapanmamış) mumu çıkar
        closed_df = df.iloc[:-1].copy()
        for col in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
            closed_df[col] = closed_df[col].astype(float)

        df_ind = calculate_strategy_indicators(closed_df)

        m1 = df_ind.iloc[-2]  # Hareket mumu
        m2 = df_ind.iloc[-1]  # Teyit mumu

        avg_volume = df_ind["volume"].iloc[-25:-2].mean()
        if avg_volume <= 0 or m1["open"] <= 0:
            return "short_data", None

        m1_body_pct = abs(m1["close"] - m1["open"]) / m1["open"] * 100.0
        vol_multi = m1["volume"] / avg_volume

        if vol_multi < VOLUME_MULTIPLIER or m1_body_pct < MIN_CANDLE_PCT:
            return "no_spike", None

        is_valid_long = (
            m1["close"] > m1["open"]
            and m1["delta"] > 0
            and m2["close"] > m1["close"]
            and m2["delta"] > 0
        )
        is_valid_short = (
            m1["close"] < m1["open"]
            and m1["delta"] < 0
            and m2["close"] < m1["close"]
            and m2["delta"] < 0
        )

        if DEBUG_LOG:
            print(
                f"[DEBUG] {symbol} hacim/gövde geçti | x{vol_multi:.1f} %{m1_body_pct:.2f} | "
                f"m1_delta={m1['delta']:.0f} m2_delta={m2['delta']:.0f} | "
                f"long={is_valid_long} short={is_valid_short}"
            )

        if not (is_valid_long or is_valid_short):
            return "no_direction", None

        return "signal", {
            "symbol": symbol,
            "direction": "LONG" if is_valid_long else "SHORT",
            "price": m2["close"],
            "m1_body": m1_body_pct,
            "m1_vol_multi": vol_multi,
            "m1_delta": m1["delta"],
            "m2_delta": m2["delta"],
            "m2_open_time": int(m2["open_time"]),
        }

    except Exception as e:
        return "exception", f"{symbol}: {type(e).__name__}: {e}"


def run_scanner():
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"\n[{now} UTC] 🔍 Saf Kırılım + Kesintisiz Delta Taraması Başlatıldı...")

    symbols = get_usdt_symbols()
    if not symbols:
        print("[!] Taranacak sembol bulunamadı.")
        return

    stats = Counter()
    errors = []
    signals = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze_symbol, s): s for s in symbols}
        for f in as_completed(futures):
            status, data = f.result()
            stats[status] += 1
            if status == "signal":
                signals.append(data)
            elif status in ("api_error", "rate_limit", "exception") and len(errors) < 5:
                errors.append(f"{futures[f]} -> {data}")

    print(
        f"[Tamamlandı] Taranan: {len(symbols)} | "
        f"Hacim/gövde filtresi geçen: {stats['no_direction'] + stats['signal']} | "
        f"Sinyal: {stats['signal']}"
    )
    print(f"[İstatistik] {dict(stats)}")

    if errors:
        print("[Hata örnekleri]")
        for e in errors:
            print(f"  - {e}")
    if stats["rate_limit"]:
        print("[!] Rate limit alındı: MAX_WORKERS'ı düşür veya SCAN_INTERVAL'ı artır.")

    for s in signals:
        # Aynı teyit mumu için tekrar bildirim gönderme
        if last_alerted.get(s["symbol"]) == s["m2_open_time"]:
            continue
        last_alerted[s["symbol"]] = s["m2_open_time"]

        is_long = s["direction"] == "LONG"
        badge = "🟢 SAF KIRILIM + DELTA SİNYALİ (LONG)" if is_long else "🔴 SAF KIRILIM + DELTA SİNYALİ (SHORT)"
        msg = (
            f"========================================\n"
            f"{badge}\n"
            f"Sembol: {s['symbol'].replace('USDT', '')}/USDT\n"
            f"Fiyat (2. Mum Kapanış): ${s['price']}\n"
            f"1. Mum Gövdesi: %{s['m1_body']:.2f}\n"
            f"Hacim Çarpanı: {s['m1_vol_multi']:.1f}x\n"
            f"1. Mum Delta: {s['m1_delta']:,.0f}\n"
            f"2. Mum Delta (Teyit): {s['m2_delta']:,.0f}\n"
            f"========================================"
        )
        print(msg)
        send_telegram_alert(msg)


def main():
    print("🚀 SAF DELTA BREAKOUT BOTU AKTİF (Filtresiz Mod)")
    print(
        f"Ayarlar -> Zaman Dilimi: {TIMEFRAME} | Tarama Aralığı: {SCAN_INTERVAL}s | "
        f"Min Gövde: %{MIN_CANDLE_PCT} | Hacim Katı: {VOLUME_MULTIPLIER}x"
    )
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Uyarı] Telegram değişkenleri boş, sinyaller sadece konsola yazılacak.")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"[Sistem Hatası] Tarama döngüsü hatası: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
