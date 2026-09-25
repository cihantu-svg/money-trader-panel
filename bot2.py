"""
SMA100 Temas + Mum Boyu - CANLI TARAMA BOTU (v2 - Son 2 Mum Kontrolu)
Kosullar:
1) Mum govdesi >= BODY_PCT
2) Mumun low-high araligi SMA100 degerini iceriyor (temas)
Filtre: 24s hacim >= MIN_VOLUME_USDT
Borsa: Binance Futures USDT-M perpetual

DEGISIKLIK (v2): Sadece son kapanan tek mum yerine, son 2 kapanan mum
kontrol ediliyor. Boylece bir tarama turu rate-limit/yavas tur yuzunden
bir mumu kacirsa bile, bir sonraki tur o mumu hala gorup yakalayabiliyor.
Ekstra API cagrisi yok - zaten cekilen veriden 1 mum yerine 2 mum okunuyor.
Dedup artik sembol basina TEK zaman degil, sembol basina bir KUME (set)
ile yapiliyor - her mum kendi open_time'iyla ayri ayri takip ediliyor.
"""

import requests
import pandas as pd
import time
import os
from datetime import datetime, timezone

# ─────────────── AYARLAR (ENV'DEN OKUNUR) ───────────────
SMA_LEN           = int(os.environ.get("SMA_LEN", 100))
BODY_PCT          = float(os.environ.get("BODY_PCT", 5.0))
MIN_VOLUME_USDT   = float(os.environ.get("MIN_VOLUME_USDT", 3_000_000))
TIMEFRAME         = os.environ.get("TIMEFRAME", "15m")
SCAN_INTERVAL_SEC = int(os.environ.get("SCAN_INTERVAL_SEC", 300))
CANDLES_TO_CHECK  = 2  # son kac kapanmis mum kontrol edilsin

BINANCE_FAPI  = "https://fapi.binance.com"
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

KLINES_LIMIT = SMA_LEN + 10

# Sembol -> {open_time1, open_time2, ...} seklinde, her mumu ayri takip eder
gonderilen_uyarilar = {}
MAX_HAFIZA_PER_SYMBOL = 50  # eski open_time'lari temizlemek icin ust sinir


def send_telegram(text):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        print("[UYARI] Telegram token tanimlanmamis.", flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram gonderim hatasi: {e}", flush=True)


def get_usdt_perpetual_symbols():
    url = f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
    r = requests.get(url, timeout=15)
    symbols = []
    for s in r.json()["symbols"]:
        if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING":
            symbols.append(s["symbol"])
    return symbols


def get_24h_volumes():
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    r = requests.get(url, timeout=15)
    vol_map = {}
    for d in r.json():
        try:
            vol_map[d["symbol"]] = float(d["quoteVolume"])
        except (KeyError, ValueError):
            continue
    return vol_map


def get_klines(symbol):
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": TIMEFRAME, "limit": KLINES_LIMIT}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"[{symbol}] Kline istegi hatasi: {e}", flush=True)
        return None
    if not isinstance(data, list) or len(data) < SMA_LEN + 2:
        return None
    df = pd.DataFrame(data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def check_symbol(symbol):
    """
    Son CANDLES_TO_CHECK kapanmis mumu kontrol eder (canli/tamamlanmamis
    mum haric - o yuzden index her zaman -2'den baslar, -1 canli mumdur).
    Kosulu saglayan HER mum icin ayri bir sonuc dondurur (liste).
    """
    df = get_klines(symbol)
    if df is None or len(df) < SMA_LEN + 1 + CANDLES_TO_CHECK:
        return []

    df["sma100"] = df["close"].rolling(SMA_LEN).mean()

    sonuclar = []
    # -2 = en son kapanmis mum, -3 = ondan onceki, vs. (-1 canli/tamamlanmamis mum)
    for i in range(2, 2 + CANDLES_TO_CHECK):
        row = df.iloc[-i]
        sma100 = df["sma100"].iloc[-i]

        if pd.isna(sma100):
            continue

        body_pct = abs(row["close"] - row["open"]) / row["open"] * 100
        touches_sma = row["low"] <= sma100 <= row["high"]

        if body_pct >= BODY_PCT and touches_sma:
            sonuclar.append({
                "symbol":    symbol,
                "open_time": int(row["open_time"]),
                "body_pct":  body_pct,
                "direction": "YESIL (yukselen)" if row["close"] > row["open"] else "KIRMIZI (dusen)",
                "close":     row["close"],
                "sma100":    sma100,
            })
    return sonuclar


def scan_once():
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Tarama basliyor...", flush=True)
    symbols  = get_usdt_perpetual_symbols()
    volumes  = get_24h_volumes()
    filtered = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    print(f"{len(filtered)} coin hacim filtresini gecti.", flush=True)

    bulunan = 0
    for symbol in filtered:
        try:
            sonuclar = check_symbol(symbol)
        except Exception as e:
            print(f"{symbol} hata: {e}", flush=True)
            continue

        if not sonuclar:
            continue

        gonderilenler = gonderilen_uyarilar.setdefault(symbol, set())

        for sonuc in sonuclar:
            if sonuc["open_time"] in gonderilenler:
                continue  # bu mum icin zaten uyari gonderilmis

            gonderilenler.add(sonuc["open_time"])
            # hafiza sisirmesin diye eski kayitlari sinirla
            if len(gonderilenler) > MAX_HAFIZA_PER_SYMBOL:
                en_eski = min(gonderilenler)
                gonderilenler.discard(en_eski)

            bulunan += 1
            mesaj = (
                f"SMA{SMA_LEN} TEMAS + %{BODY_PCT} MUM\n\n"
                f"Sembol   : {sonuc['symbol']}\n"
                f"Yon      : {sonuc['direction']}\n"
                f"Govde    : %{sonuc['body_pct']:.2f}\n"
                f"Kapanis  : {sonuc['close']}\n"
                f"SMA{SMA_LEN}  : {sonuc['sma100']:.6f}\n"
                f"Timeframe: {TIMEFRAME}"
            )
            print(mesaj, flush=True)
            send_telegram(mesaj)

        time.sleep(0.1)

    print(f"Tarama bitti. {bulunan} yeni sinyal bulundu.", flush=True)


def main():
    print(f"Bot baslatildi | BODY_PCT=%{BODY_PCT} | SMA={SMA_LEN} | TF={TIMEFRAME} | MIN_VOL={MIN_VOLUME_USDT:,.0f} | Kontrol edilen mum sayisi={CANDLES_TO_CHECK}", flush=True)
    while True:
        try:
            scan_once()
        except Exception as e:
            print(f"Genel hata: {e}", flush=True)
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
