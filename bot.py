import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import pandas as pd
import requests

# ─────────────── AYARLAR (ENV'DEN OKUNUR) ───────────────
TIMEFRAME = os.environ.get("TIMEFRAME", "15m")
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
MAX_WORKERS = 15

# Hacim, Çarpan ve Mum Boyu Sınırları
MIN_MUM_VOLUME_USDT = float(os.environ.get("MIN_MUM_VOLUME_USDT", 5_000_000))
VOLUME_MULTIPLIER = float(os.environ.get("VOLUME_MULTIPLIER", 10.0))
MIN_CANDLE_CHANGE_PCT = float(os.environ.get("MIN_CANDLE_CHANGE_PCT", 5.0))  # Min %5 mum boyu

BINANCE_FAPI = "https://fapi.binance.com"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

http_session = requests.Session()
gonderilen_uyarilar = {}
rate_limit_lock = threading.Lock()
rate_limited_until = 0.0


def send_telegram(text):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        return
    try:
        http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram hatasi: {e}", flush=True)


def wait_if_rate_limited():
    global rate_limited_until
    now = time.time()
    if now < rate_limited_until:
        time.sleep(rate_limited_until - now)


def check_symbol_whale(symbol):
    global rate_limited_until
    wait_if_rate_limited()

    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": TIMEFRAME, "limit": 22}
    
    try:
        r = http_session.get(url, params=params, timeout=10)
    except Exception:
        return []

    if r.status_code in (429, 418):
        retry_after = r.headers.get("Retry-After")
        bekleme = float(retry_after) if retry_after else 30
        with rate_limit_lock:
            if time.time() + bekleme > rate_limited_until:
                rate_limited_until = time.time() + bekleme
        return []

    try:
        data = r.json()
    except Exception:
        return []

    if not isinstance(data, list) or len(data) < 21:
        return []

    closed_data = data[:-1]

    df = pd.DataFrame(closed_data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for col in ["quote_volume", "taker_buy_quote", "close", "open", "high", "low"]:
        df[col] = df[col].astype(float)

    last_candle = df.iloc[-1]
    last_volume = last_candle["quote_volume"]

    # 1. ŞART: Minimum mum hacmi ($5M)
    if last_volume < MIN_MUM_VOLUME_USDT:
        return []

    # 2. ŞART: Mumun Fiyat Değişim Yüzdesi (En az %5 olmalı)
    # Mum boyu (Yüksek/Düşük farkı veya Açılış/Kapanış farkına bakılabilir; burada Açılış/Kapanış değişimi alındı)
    candle_change_pct = abs(last_candle["close"] - last_candle["open"]) / last_candle["open"] * 100
    if candle_change_pct < MIN_CANDLE_CHANGE_PCT:
        return []

    # Önceki 20 mumun ortalama hacmi
    prev_20_avg_volume = df.iloc[-21:-1]["quote_volume"].mean()
    if prev_20_avg_volume == 0:
        return []

    multiplier = last_volume / prev_20_avg_volume

    # 3. ŞART: Hacim ortalamanın katı (10x)
    if multiplier >= VOLUME_MULTIPLIER:
        taker_buy_quote = last_candle["taker_buy_quote"]
        taker_sell_quote = last_volume - taker_buy_quote
        net_delta = taker_buy_quote - taker_sell_quote

        delta_direction = "🟢 AGRESİF ALICI BASKISI (Long)" if net_delta > 0 else "🔴 AGRESİF SATICI BASKISI (Short)"

        return [{
            "symbol": symbol,
            "open_time": int(last_candle["open_time"]),
            "volume_usdt": last_volume,
            "multiplier": multiplier,
            "net_delta": net_delta,
            "delta_direction": delta_direction,
            "candle_change_pct": candle_change_pct,
            "close": last_candle["close"],
        }]

    return []


def scan_whales():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Whale Tracker Taraması Başladı...", flush=True)

    try:
        r = http_session.get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=10)
        filtered = [d["symbol"] for d in r.json() if d["symbol"].endswith("USDT")]
    except Exception as e:
        print(f"Ticker hatası: {e}", flush=True)
        return

    bulunan = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(check_symbol_whale, sym): sym for sym in filtered}

        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                sonuclar = future.result()
                if not sonuclar:
                    continue

                gonderilenler = gonderilen_uyarilar.setdefault(symbol, set())

                for sonuc in sonuclar:
                    if sonuc["open_time"] in gonderilenler:
                        continue

                    gonderilenler.add(sonuc["open_time"])
                    bulunan += 1

                    mesaj = (
                        f"🐋 **WHALE TRACKER: HACİM & DELTA PATLAMASI**\n\n"
                        f"• **Sembol**: `{sonuc['symbol']}`\n"
                        f"• **Baskı Yönü**: {sonuc['delta_direction']}\n"
                        f"• **Mum Boyu**: `%{sonuc['candle_change_pct']:.2f}`\n"
                        f"• **Mum Hacmi**: `${sonuc['volume_usdt']/1_000_000:.2f}M USDT`\n"
                        f"• **Hacim Artışı**: `{sonuc['multiplier']:.1f}x` (Son 20 mum ort.)\n"
                        f"• **Net Delta**: `${sonuc['net_delta']/1_000_000:.2f}M USDT`\n"
                        f"• **Kapanış**: `{sonuc['close']}`\n"
                        f"• **Zaman Dilimi**: `{TIMEFRAME}`"
                    )
                    print(f"[WHALE DETECTED] {sonuc['symbol']} - %{sonuc['candle_change_pct']:.1f} Hareket", flush=True)
                    send_telegram(mesaj)
            except Exception as e:
                print(f"{symbol} hatası: {e}", flush=True)

    print(f"Whale taraması bitti. {bulunan} yeni balina hareketi tespit edildi.", flush=True)


def main():
    print(
        f"Whale Tracker Başlatıldı | TF={TIMEFRAME} | "
        f"MinVol={MIN_MUM_VOLUME_USDT/1_000_000:.1f}M$ | "
        f"MinX={VOLUME_MULTIPLIER}x | "
        f"MinMum=%{MIN_CANDLE_CHANGE_PCT} | "
        f"Aralık={SCAN_INTERVAL_SEC}sn",
        flush=True,
    )
    while True:
        try:
            scan_whales()
        except Exception as e:
            print(f"Genel Hata: {e}", flush=True)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
