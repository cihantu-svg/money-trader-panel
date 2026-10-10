import os
import time
import math
from datetime import datetime, timezone
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

# ==============================================================================
# YAPILANDIRMA
# ==============================================================================
TIMEFRAME = os.getenv("TIMEFRAME", "1m")
MIN_CANDLE_PCT = float(os.getenv("MIN_CANDLE_PCT", 2.0))
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", 3.0))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", 60))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", 10))
DEBUG_LOG = os.getenv("DEBUG_LOG", "false").lower() == "true"
AVG_WINDOW = 23

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

API_BASE = "https://fapi.binance.com"


def tf_seconds(tf: str) -> int:
    return int(tf[:-1]) * {"m": 60, "h": 3600, "d": 86400}[tf[-1]]


# Bir taramada kaç mum çifti geriye bakılacak (taramalar arası kapanan mumları da yakalar)
LOOKBACK_PAIRS = int(os.getenv("LOOKBACK_PAIRS", math.ceil(SCAN_INTERVAL / tf_seconds(TIMEFRAME)) + 1))
# Çok eski sinyalleri göndermemek için üst sınır
LOOKBACK_PAIRS = max(1, min(LOOKBACK_PAIRS, 8))

session = requests.Session()
session.mount("https://", HTTPAdapter(pool_connections=MAX_WORKERS, pool_maxsize=MAX_WORKERS))

last_alerted = {}  # {symbol: m2_open_time}


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
            params={"symbol": symbol, "interval": TIMEFRAME, "limit": 60},
            timeout=10,
        )
        if res.status_code in (418, 429):
            return "rate_limit", f"HTTP {res.status_code}"
        if res.status_code != 200:
            return "api_error", f"HTTP {res.status_code}: {res.text[:100]}"

        klines = res.json()
        if not isinstance(klines, list) or len(klines) < AVG_WINDOW + LOOKBACK_PAIRS + 3:
            return "short_data", None

        df = pd.DataFrame(klines).iloc[:, :11].astype(float)
        df.columns = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote",
        ]

        # Canlı (kapanmamış) mumu çıkar
        now_ms = int(time.time() * 1000)
        df = df[df["close_time"] < now_ms].reset_index(drop=True)

        df["delta"] = 2.0 * df["taker_buy_base"] - df["volume"]  # alış - satış
        df["body_pct"] = (df["close"] - df["open"]).abs() / df["open"] * 100.0
        # Her mum için kendisinden ÖNCEKİ 23 mumun hacim ortalaması
        df["vol_avg"] = df["volume"].rolling(AVG_WINDOW).mean().shift(1)

        n = len(df)
        passed_spike = False

        # offset=0 -> en güncel çift, offset büyüdükçe geçmişe gider
        for offset in range(LOOKBACK_PAIRS):
            i2 = n - 1 - offset
            i1 = i2 - 1
            if i1 < AVG_WINDOW:
                break

            m1, m2 = df.iloc[i1], df.iloc[i2]
            if pd.isna(m1["vol_avg"]) or m1["vol_avg"] <= 0 or m1["open"] <= 0:
                continue

            vol_multi = m1["volume"] / m1["vol_avg"]
            if vol_multi < VOLUME_MULTIPLIER or m1["body_pct"] < MIN_CANDLE_PCT:
                continue
            passed_spike = True

            is_long = (
                m1["close"] > m1["open"] and m1["delta"] > 0
                and m2["close"] > m1["close"] and m2["delta"] > 0
            )
            is_short = (
                m1["close"] < m1["open"] and m1["delta"] < 0
                and m2["close"] < m1["close"] and m2["delta"] < 0
            )

            if DEBUG_LOG:
                print(
                    f"[DEBUG] {symbol} offset={offset} hacim/gövde geçti | x{vol_multi:.1f} "
                    f"%{m1['body_pct']:.2f} | m1_delta={m1['delta']:.0f} m2_delta={m2['delta']:.0f} "
                    f"| long={is_long} short={is_short}"
                )

            if is_long or is_short:
                return "signal", {
                    "symbol": symbol,
                    "direction": "LONG" if is_long else "SHORT",
                    "price": m2["close"],
                    "m1_body": m1["body_pct"],
                    "m1_vol_multi": vol_multi,
                    "m1_delta": m1["delta"],
                    "m2_delta": m2["delta"],
                    "m2_open_time": int(m2["open_time"]),
                    "candles_ago": offset,
                }

        return ("no_direction" if passed_spike else "no_spike"), None

    except Exception as e:
        return "exception", f"{symbol}: {type(e).__name__}: {e}"


def run_scanner():
    t0 = time.time()
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"\n[{now} UTC] 🔍 Tarama başladı | {TIMEFRAME} | geriye bakış: {LOOKBACK_PAIRS} çift")

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

    elapsed = time.time() - t0
    print(
        f"[Tamamlandı] {len(symbols)} sembol, {elapsed:.0f}s | "
        f"Hacim/gövde geçen: {stats['no_direction'] + stats['signal']} | Sinyal: {stats['signal']}"
    )
    print(f"[İstatistik] {dict(stats)}")
    if elapsed > SCAN_INTERVAL:
        print(f"[!] Tarama {elapsed:.0f}s sürdü, SCAN_INTERVAL ({SCAN_INTERVAL}s) değerini aşıyor.")
    if errors:
        print("[Hata örnekleri]")
        for e in errors:
            print(f"  - {e}")
    if stats["rate_limit"]:
        print("[!] Rate limit alındı: MAX_WORKERS'ı düşür veya SCAN_INTERVAL'ı artır.")

    for s in signals:
        if last_alerted.get(s["symbol"]) == s["m2_open_time"]:
            continue
        last_alerted[s["symbol"]] = s["m2_open_time"]

        is_long = s["direction"] == "LONG"
        badge = "🟢 SAF KIRILIM + DELTA SİNYALİ (LONG)" if is_long else "🔴 SAF KIRILIM + DELTA SİNYALİ (SHORT)"
        ago = f"{s['candles_ago']} mum önce" if s["candles_ago"] else "şimdi"
        msg = (
            f"========================================\n"
            f"{badge}\n"
            f"Sembol: {s['symbol'].replace('USDT', '')}/USDT\n"
            f"Zaman dilimi: {TIMEFRAME} (teyit: {ago})\n"
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
    print("🚀 SAF DELTA BREAKOUT BOTU AKTİF")
    print(
        f"Ayarlar -> Zaman Dilimi: {TIMEFRAME} | Tarama Aralığı: {SCAN_INTERVAL}s | "
        f"Min Gövde: %{MIN_CANDLE_PCT} | Hacim Katı: {VOLUME_MULTIPLIER}x | Geriye bakış: {LOOKBACK_PAIRS} çift"
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
