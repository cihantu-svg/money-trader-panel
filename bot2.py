# -*- coding: utf-8 -*-
"""
GOVDE %7 TARAMA BOT
Strateji: Sadece 1 saatlik mum govdesi (|Close-Open|/Open*100) BODY_PCT_MIN
yuzde ve uzeri olan coinleri TEK TEK (her biri ayri mesaj) bildirir.
Baska hicbir filtre/indikator yoktur.
Zaman dilimi: 1 saat
Tarama araligi: 5 dakika (ayarlanabilir)
Borsa: Binance Futures (USDT-M Perpetual)
"""
import os
import time
import logging
from datetime import datetime

import requests
import pandas as pd
import urllib3
import warnings

urllib3.disable_warnings()
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))
TIMEFRAME = os.environ.get("SCAN_TIMEFRAME", "1h")
MAX_COINS = int(os.environ.get("MAX_COINS", "200"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", "1000000"))
BODY_PCT_MIN = float(os.environ.get("BODY_PCT_MIN", "7.0"))
SIGNAL_COOLDOWN = int(os.environ.get("SIGNAL_COOLDOWN", "3600"))

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "GovdeTaramaBot/1.0"})

sent_signals: dict = {}

def get_symbols(min_volume: float = 0) -> list:
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=30, verify=False)
        if r.status_code != 200:
            log.error(f"Sembol listesi alinamadi: HTTP {r.status_code}")
            return []
        data = r.json()
        exclude = ("3L", "3S", "5L", "5S", "UP", "DOWN", "BULL", "BEAR")
        symbols = []
        for t in data:
            sym = str(t.get("symbol", "")).upper()
            if not sym.endswith("USDT") or "_" in sym:
                continue
            if any(sym[:-4].endswith(x) for x in exclude):
                continue
            qv = float(t.get("quoteVolume") or 0)
            if qv < min_volume:
                continue
            symbols.append({"symbol": sym, "volume_24h": qv, "price": float(t.get("lastPrice") or 0)})
        symbols.sort(key=lambda x: x["volume_24h"], reverse=True)
        return symbols
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_klines(symbol: str, interval: str = "1h", limit: int = 5):
    try:
        r = SESSION.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15, verify=False
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return None
        rows = []
        for k in data:
            try:
                rows.append({
                    "ts": pd.to_datetime(int(k[0]), unit="ms"),
                    "Open": float(k[1]), "High": float(k[2]),
                    "Low": float(k[3]), "Close": float(k[4]),
                    "Volume": float(k[5]),
                })
            except Exception:
                continue
        if len(rows) < 2:
            return None
        df = pd.DataFrame(rows).set_index("ts")
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception:
        return None


def check_signal(df: pd.DataFrame, symbol: str):
    """Son KAPANMIS mumun govdesi BODY_PCT_MIN ve uzerindeyse doner. Baska kosul yok."""
    if df is None or len(df) < 2:
        return None
    row = df.iloc[-2]
    cur_open, cur_close = float(row["Open"]), float(row["Close"])
    if cur_open <= 0:
        return None
    body_pct = abs(cur_close - cur_open) / cur_open * 100
    if body_pct < BODY_PCT_MIN:
        return None
    return {
        "direction": "YESIL" if cur_close > cur_open else "KIRMIZI",
        "price": cur_close,
        "govde_yuzde": round(body_pct, 2),
    }


def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram token veya chat_id eksik!")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False


def format_message(symbol: str, sig: dict) -> str:
    emoji = "\U0001F7E2" if sig["direction"] == "YESIL" else "\U0001F534"
    coin = symbol.replace("USDT", "/USDT")
    sep = "\u2501" * 12
    lines = [
        f"{emoji} <b>GOVDE %{BODY_PCT_MIN:.0f}+ TARAMA</b>",
        sep,
        f"\U0001F4CD <b>{coin}</b>",
        f"\u23F1 Zaman Dilimi: <b>{TIMEFRAME}</b>",
        f"\U0001F4B0 Fiyat: <b>{sig['price']}</b>",
        f"\U0001F4CA Govde Buyuklugu: <b>%{sig['govde_yuzde']}</b>",
        sep,
        f"\U0001F551 {datetime.now().strftime('%H:%M:%S  %d/%m/%Y')}",
    ]
    return "\n".join(lines)


def should_send(symbol: str) -> bool:
    now = time.time()
    if symbol in sent_signals and (now - sent_signals[symbol]) < SIGNAL_COOLDOWN:
        return False
    sent_signals[symbol] = now
    return True


def run_scan():
    log.info(f"Tarama basladi TF:{TIMEFRAME} GovdeMin:%{BODY_PCT_MIN} Max:{MAX_COINS}")
    symbols = get_symbols(min_volume=MIN_VOLUME)
    if not symbols:
        log.error("Coin listesi alinamadi!")
        return

    symbols = symbols[:MAX_COINS]
    found = 0
    for idx, coin in enumerate(symbols):
        symbol = coin["symbol"]
        try:
            df = get_klines(symbol, TIMEFRAME, limit=5)
            sig = check_signal(df, symbol)
            if sig and should_send(symbol):
                if send_telegram(format_message(symbol, sig)):
                    log.info(f"OK {symbol} govde %{sig['govde_yuzde']}")
                    found += 1
                time.sleep(0.3)
            time.sleep(0.1)
        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue
        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{len(symbols)}] tarandi {found} sinyal")

    log.info(f"Tarama tamamlandi {found} sinyal gonderildi")


def main():
    log.info("GOVDE %7 TARAMA BOT baslatildi")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return
    send_telegram(f"GOVDE %{BODY_PCT_MIN}+ TARAMA BOT BASLADI\nZaman dilimi: {TIMEFRAME}")
    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
