import os
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
import requests

# ─────────────── AYARLAR (ENV'DEN OKUNUR) ───────────────
SMA_LEN          = int(os.environ.get("SMA_LEN", 100))
BODY_PCT         = float(os.environ.get("BODY_PCT", 5.0))
MIN_VOLUME_USDT  = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
TIMEFRAME        = os.environ.get("TIMEFRAME", "15m")

SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 180))
MAX_WORKERS       = 15

BINANCE_FAPI      = "https://fapi.binance.com"
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

KLINES_LIMIT      = SMA_LEN + 10

http_session = requests.Session()

# Takip sözlükleri: Hangi sembolün hangi mumda ilk sinyal veya devam sinyali aldığını tutar
ilk_sinyal_takibi = {}    # {symbol: open_time}
devam_sinyal_takibi = {}  # {symbol: set(open_time)}

rate_limit_lock = threading.Lock()
rate_limited_until = 0.0


def send_telegram(text):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        return
    try:
        http_session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram hatasi: {e}", flush=True)


def wait_if_rate_limited():
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
        return None

    if r.status_code in (429, 418):
        retry_after = r.headers.get("Retry-After")
        bekleme = float(retry_after) if retry_after else (30 if r.status_code == 429 else 120)
        with rate_limit_lock:
            yeni_bitis = time.time() + bekleme
            if yeni_bitis > rate_limited_until:
                rate_limited_until = yeni_bitis
        return None

    try:
        data = r.json()
    except Exception:
        return None

    if not isinstance(data, list) or len(data) < SMA_LEN + 2:
        return None

    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)

    df["sma100"] = df["close"].rolling(SMA_LEN).mean()

    row = df.iloc[-1]       # Canlı / Anlık mum
    prev_row = df.iloc[-2]  # Önceki kapanmış mum

    sma100 = row["sma100"]
    prev_sma100 = prev_row["sma100"]

    if pd.isna(sma100) or pd.isna(prev_sma100):
        return None

    body_pct = abs(row["close"] - row["open"]) / row["open"] * 100
    direction = "YÜKSELEN (Yeşil)" if row["close"] > row["open"] else "DÜŞEN (Kırmızı)"
    prev_direction = "YÜKSELEN (Yeşil)" if prev_row["close"] > prev_row["open"] else "DÜŞEN (Kırmızı)"

    # 1. İlk Sinyal Koşulları (SMA Temas/Kırılım + %5 Gövde)
    direct_touch = (row["low"] <= sma100 <= row["high"])
    near_touch = abs(row["low"] - sma100) / sma100 <= 0.003 or abs(row["high"] - sma100) / sma100 <= 0.003
    bullish_break = (prev_row["close"] < prev_sma100) and (row["close"] > sma100)
    bearish_break = (prev_row["close"] > prev_sma100) and (row["close"] < sma100)
    touches_sma = direct_touch or near_touch or bullish_break or bearish_break

    is_ilk_sinyal = (body_pct >= BODY_PCT) and touches_sma

    # 2. Devam Mumu Koşulları (Önceki mumda sinyal alınmışsa ve bu mum da %5 büyük gövdeyle aynı yönde devam ediyorsa)
    # Not: Devam mumunda tekrar SMA teması aranmaz, çünkü fiyat çoktan uzaklaşmıştır (soluksuz gidiyordur).
    last_signal_open_time = ilk_sinyal_takibi.get(symbol)
    is_devam_mumu = False
    
    if not is_ilk_sinyal and body_pct >= BODY_PCT:
        # Eğer bir önceki mum (prev_row) ilk sinyali verdiyse VEYA yakın zamanda aktif bir sinyal zinciri varsa
        if last_signal_open_time is not None:
            # Önceki mumun open_time'ı ile şimdiki mumun open_time arasında ardışıklık kontrolü veya genel trend devamı
            if int(prev_row["open_time"]) == last_signal_open_time and direction == prev_direction:
                is_devam_mumu = True

    if is_ilk_sinyal or is_devam_mumu:
        return {
            "symbol": symbol,
            "open_time": int(row["open_time"]),
            "body_pct": body_pct,
            "direction": direction,
            "close": row["close"],
            "sma100": sma100,
            "tip": "DEVAM" if is_devam_mumu else "ILK"
        }

    return None


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

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(check_symbol, sym): sym for sym in filtered}

        for future in as_completed(future_to_symbol):
            symbol = future_to_symbol[future]
            try:
                sonuc = future.result()
                if not sonuc:
                    continue

                open_time = sonuc["open_time"]
                tip = sonuc["tip"]

                # Mükerrer kontrolü
                if tip == "ILK":
                    if ilk_sinyal_takibi.get(symbol) == open_time:
                        continue
                    ilk_sinyal_takibi[symbol] = open_time
                    baslik = f"🎯 <b>AÇIK MUM | İLK SMA{SMA_LEN} KIRILIM SİNYALİ</b>"
                else:  # DEVAM
                    devam_set = devam_sinyal_takibi.setdefault(symbol, set())
                    if open_time in devam_set:
                        continue
                    devam_set.add(open_time)
                    # İLK HARFLERİ BÜYÜK VE DİKKAT ÇEKİCİ ŞEKİLDE
                    baslik = "🔥🚨 <b>DEVAM MUMU! (SOLUKSUZ GİDİŞ)</b> 🚨🔥"

                bulunan += 1

                mesaj = (
                    f"{baslik}\n\n"
                    f"• <b>Sembol</b>: {sonuc['symbol']}\n"
                    f"• <b>Yön</b>: {sonuc['direction']}\n"
                    f"• <b>Gövde Boyu</b>: %{sonuc['body_pct']:.2f}\n"
                    f"• <b>Anlık Fiyat</b>: {sonuc['close']}\n"
                    f"• <b>SMA{SMA_LEN}</b>: {sonuc['sma100']:.6f}\n"
                    f"• <b>Zaman Dilimi</b>: {TIMEFRAME}"
                )
                print(f"[SİNYAL - {tip}] {sonuc['symbol']} - %{sonuc['body_pct']:.2f}", flush=True)
                send_telegram(mesaj)

            except Exception as e:
                print(f"{symbol} işleme hatası: {e}", flush=True)

    print(f"Tarama bitti. {bulunan} yeni sinyal gönderildi.", flush=True)


def main():
    print(f"Bot Başlatıldı (İlk + DEVAM Mumu Modu) | TF={TIMEFRAME} | Aralık={SCAN_INTERVAL_SEC}sn", flush=True)
    while True:
        try:
            scan_once()
        except Exception as e:
            print(f"Genel Hata: {e}", flush=True)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
