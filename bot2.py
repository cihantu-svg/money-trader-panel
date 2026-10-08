import requests
import pandas as pd
import time
import os
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────── AYARLAR (ENV'DEN OKUNUR) ───────────────
SMA_LEN           = int(os.environ.get("SMA_LEN", 100))
BODY_PCT          = float(os.environ.get("BODY_PCT", 5.0))
MIN_VOLUME_USDT   = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
TIMEFRAME         = os.environ.get("TIMEFRAME", "15m")

# RENDER KOTA KORUMASI: 15 dakikada bir calisacak sekilde sabitlendi (900 sn)
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
CANDLES_TO_CHECK  = 3  # Guvenlik icin son 3 kapanmis mum
MAX_WORKERS       = 15  # Render CPU dostu thread sayisi

BINANCE_FAPI      = "https://fapi.binance.com"
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

# SMA_LEN + 10: kline agirligi (limit<100 -> 1, 100-500 -> 2) zaten sabit,
# +5 yerine +10 kullanmanin ekstra maliyeti yok ama guvenlik payi daha genis
KLINES_LIMIT      = SMA_LEN + 10

# Global HTTP Session (Paket basliklarini kucultup baglanti yeniden kullanir)
http_session = requests.Session()

gonderilen_uyarilar = {}

# Rate limit'e carpinca butun thread'leri bir sure yavaslatmak icin ortak bayrak
rate_limit_lock = threading.Lock()
rate_limited_until = 0.0  # bu unix zamanina kadar yeni istek atmadan once bekle


def send_telegram(text):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        return
    try:
        http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram hatasi: {e}", flush=True)


def wait_if_rate_limited():
    """Baska bir thread rate limit'e carptiysa, bu thread de kisa sure bekler."""
    global rate_limited_until
    now = time.time()
    if now < rate_limited_until:
        time.sleep(rate_limited_until - now)


def check_symbol(symbol):
    global rate_limited_until

    wait_if_rate_limited()

    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": TIMEFRAME, "limit": KLINES_LIMIT}
    try:
        r = http_session.get(url, params=params, timeout=10)
    except Exception as e:
        print(f"[{symbol}] Istek hatasi: {e}", flush=True)
        return []

    if r.status_code in (429, 418):
        # 429 = rate limit asildi, 418 = IP gecici olarak ban'landi.
        # Ban'a girmemek icin butun thread'leri ortak bir bekleme suresine sokuyoruz.
        retry_after = r.headers.get("Retry-After")
        bekleme = float(retry_after) if retry_after else (30 if r.status_code == 429 else 120)

        with rate_limit_lock:
            yeni_bitis = time.time() + bekleme
            if yeni_bitis > rate_limited_until:
                rate_limited_until = yeni_bitis

        print(
            f"[RATE LIMIT] {symbol} - HTTP {r.status_code} - {bekleme:.0f}sn bekleniyor "
            f"(Retry-After header: {retry_after})",
            flush=True,
        )
        return []

    try:
        data = r.json()
    except Exception as e:
        print(f"[{symbol}] JSON parse hatasi: {e}", flush=True)
        return []

    if not isinstance(data, list) or len(data) < SMA_LEN + CANDLES_TO_CHECK + 1:
        return []

    # Canli (kapanmamis) mumu cikartiyoruz, sadece kapanmis mumlar kaliyor
    closed_data = data[:-1]

    df = pd.DataFrame(closed_data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)

    df["sma100"] = df["close"].rolling(SMA_LEN).mean()

    sonuclar = []
    for i in range(1, 1 + CANDLES_TO_CHECK):
        row = df.iloc[-i]
        sma100 = row["sma100"]

        if pd.isna(sma100):
            continue

        body_pct = abs(row["close"] - row["open"]) / row["open"] * 100
        touches_sma = row["low"] <= sma100 <= row["high"]

        if body_pct >= BODY_PCT and touches_sma:
            sonuclar.append({
                "symbol":    symbol,
                "open_time": int(row["open_time"]),
                "body_pct":  body_pct,
                "direction": "YÜKSELEN (Yeşil)" if row["close"] > row["open"] else "DÜŞEN (Kırmızı)",
                "close":     row["close"],
                "sma100":    sma100,
            })
    return sonuclar


def scan_once():
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] Tarama başladı...", flush=True)

    try:
        r = http_session.get(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr", timeout=10)
        filtered = [
            d["symbol"] for d in r.json()
            if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOLUME_USDT
        ]
    except Exception as e:
        print(f"Ticker hatası: {e}", flush=True)
        return

    bulunan = 0
    rate_limit_sayisi = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(check_symbol, sym): sym for sym in filtered}

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
                        f"🎯 **SMA{SMA_LEN} TEMAS + %{BODY_PCT} MUM**\n\n"
                        f"• **Sembol**: {sonuc['symbol']}\n"
                        f"• **Yön**: {sonuc['direction']}\n"
                        f"• **Gövde Boyu**: %{sonuc['body_pct']:.2f}\n"
                        f"• **Kapanış**: {sonuc['close']}\n"
                        f"• **SMA{SMA_LEN}**: {sonuc['sma100']:.6f}\n"
                        f"• **Zaman Dilimi**: {TIMEFRAME}"
                    )
                    print(f"[SİNYAL] {sonuc['symbol']} - %{sonuc['body_pct']:.2f}", flush=True)
                    send_telegram(mesaj)
            except Exception as e:
                print(f"{symbol} hatası: {e}", flush=True)

    if time.time() < rate_limited_until:
        rate_limit_sayisi = 1  # bu tur en az bir kez rate limit'e carpmis

    print(
        f"Tarama bitti. {bulunan} yeni sinyal bulundu."
        + (" [UYARI: bu turda rate limit'e carpildi]" if rate_limit_sayisi else ""),
        flush=True,
    )


def main():
    print(f"Bot Başlatıldı | TF={TIMEFRAME} | Tarama Aralığı={SCAN_INTERVAL_SEC}sn", flush=True)
    while True:
        try:
            scan_once()
        except Exception as e:
            print(f"Genel Hata: {e}", flush=True)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
