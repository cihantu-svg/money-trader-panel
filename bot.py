"""
SMA100 Temas + %5 Mum Boyu - CANLI TARAMA BOTU
Bu bir strateji/otomatik islem botu DEGILDIR - sadece tarama ve Telegram uyarisi yapar.
Islem karari kullaniciya aittir.

Kosullar (her ikisi de saglanmali):
1) Mum govdesi >= %5   ->  |close - open| / open * 100 >= BODY_PCT
2) Mum SMA100'e temas ediyor -> low <= SMA100 <= high (mumun araligi SMA100'u iceriyor)

Filtre: 24s hacim >= 3M USDT
Borsa/Piyasa: Binance Futures USDT-M perpetual
Zaman dilimi: 15 dakika
Tarama sikligi: her 5 dakikada bir
Cikti: Telegram anlik uyari (her mum icin sembol basina sadece 1 kez)
"""

import requests
import pandas as pd
import time
import os
from datetime import datetime, timezone

# ─────────────── AYARLAR ───────────────
SMA_LEN = 100
BODY_PCT = 5.0
MIN_VOLUME_USDT = 3_000_000
TIMEFRAME = "15m"
SCAN_INTERVAL_SEC = 300          # 5 dakikada bir tarama
KLINES_LIMIT = SMA_LEN + 10      # SMA100 icin yeterli gecmis veri

BINANCE_FAPI = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

# Ayni mum icin ayni sembole tekrar uyari atmamak icin
son_uyari_zamani = {}   # {symbol: candle_open_time_ms}


def send_telegram(text):
    if TELEGRAM_TOKEN == "BURAYA_TOKEN":
        print("[UYARI] Telegram token tanimlanmamis, sadece konsola yaziliyor.")
        print(text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram gonderim hatasi: {e}")


def get_usdt_perpetual_symbols():
    url = f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
    r = requests.get(url, timeout=15)
    data = r.json()
    symbols = []
    for s in data["symbols"]:
        if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING":
            symbols.append(s["symbol"])
    return symbols


def get_24h_volumes():
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    r = requests.get(url, timeout=15)
    data = r.json()
    vol_map = {}
    for d in data:
        try:
            vol_map[d["symbol"]] = float(d["quoteVolume"])
        except (KeyError, ValueError):
            continue
    return vol_map


def get_klines(symbol, interval, limit):
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=15)
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
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


def check_symbol(symbol):
    df = get_klines(symbol, TIMEFRAME, KLINES_LIMIT)
    if df is None or len(df) < SMA_LEN + 2:
        return None

    df["sma100"] = df["close"].rolling(SMA_LEN).mean()

    # Son KAPANMIS mum (canli/tamamlanmamis mum degil, sondan bir onceki)
    row = df.iloc[-2]
    sma100 = df["sma100"].iloc[-2]

    if pd.isna(sma100):
        return None

    body_pct = abs(row["close"] - row["open"]) / row["open"] * 100
    touches_sma = row["low"] <= sma100 <= row["high"]

    if body_pct >= BODY_PCT and touches_sma:
        return {
            "symbol": symbol,
            "open_time": int(row["open_time"]),
            "body_pct": body_pct,
            "direction": "YESIL (yukselen)" if row["close"] > row["open"] else "KIRMIZI (dusen)",
            "close": row["close"],
            "sma100": sma100,
        }
    return None


def scan_once():
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Tarama basliyor...")
    symbols = get_usdt_perpetual_symbols()
    volumes = get_24h_volumes()
    filtered = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    print(f"{len(filtered)} coin hacim filtresini gecti, taraniyor...")

    bulunan = 0
    for symbol in filtered:
        try:
            sonuc = check_symbol(symbol)
        except Exception as e:
            print(f"{symbol} hata: {e}")
            continue

        if sonuc is None:
            continue

        # Ayni mum icin daha once uyari attiysak tekrar atma
        if son_uyari_zamani.get(symbol) == sonuc["open_time"]:
            continue
        son_uyari_zamani[symbol] = sonuc["open_time"]

        bulunan += 1
        mesaj = (
            f"SMA100 TEMAS + %5 MUM\n\n"
            f"Sembol: {symbol}\n"
            f"Yon: {sonuc['direction']}\n"
            f"Mum Govdesi: %{sonuc['body_pct']:.2f}\n"
            f"Kapanis: {sonuc['close']}\n"
            f"SMA100: {sonuc['sma100']:.6f}\n"
            f"Zaman dilimi: {TIMEFRAME}"
        )
        print(mesaj)
        send_telegram(mesaj)

        time.sleep(0.1)

    print(f"Tarama bitti. {bulunan} yeni sinyal bulundu.")


def main():
    print("SMA100 Temas + %5 Mum Boyu tarama botu baslatildi.")
    while True:
        try:
            scan_once()
        except Exception as e:
            print(f"Genel tarama hatasi: {e}")
        time.sleep(SCAN_INTERVAL_SEC)


if __name__ == "__main__":
    main()
