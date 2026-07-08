# -*- coding: utf-8 -*-
"""
MONEY TRADER - TELEGRAM BOT (Binance Futures)
Her 5 dakikada bir 1 saatlik mumda tarama yapar.
SADECE Hacim Momentumu (VOLUM HACIM 5X) sinyali gonderir:
1H hacim, son 20 mum ortalamasinin 5 kati veya uzerine ciktiginda bildirim gonderir.
Render'da ayri bir background worker olarak calisir.
"""
import os
import time
import hashlib
import logging
from datetime import datetime

import requests
import pandas as pd
import numpy as np
import urllib3
import warnings

urllib3.disable_warnings()
warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# ⚙️ AYARLAR — Render'da Environment Variable olarak eklenecek
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))  # 5 dakika
TIMEFRAME = os.environ.get("SCAN_TIMEFRAME", "1h")
MAX_COINS = int(os.environ.get("MAX_COINS", "600"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", "1000000"))
VOLUME_SPIKE_MULT = float(os.environ.get("VOLUME_SPIKE_MULT", "5.0"))  # 5x hacim esigi

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MoneyTrader-TelegramBot/1.0"})

# Daha once gonderilen sinyalleri takip et (ayni sinyali tekrar gonderme)
# Key: symbol+type, Value: son gonderim zamani (unix timestamp)
sent_signals: dict = {}
SIGNAL_COOLDOWN = 3600  # ayni coin+sinyal icin 1 saat bekleme

# ══════════════════════════════════════════════════════════════
# 📊 TEKNİK GÖSTERGELER
# ══════════════════════════════════════════════════════════════
def calc_sma(series, period):
    return series.rolling(window=period).mean()

def calc_vol_ratio(df, period=20):
    return df['Volume'] / df['Volume'].rolling(period).mean().replace(0, np.nan)

# ══════════════════════════════════════════════════════════════
# 🌐 BINANCE FUTURES API
# ══════════════════════════════════════════════════════════════
def get_symbols(min_volume=0):
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=30, verify=False)
        if r.status_code != 200:
            log.error(f"Sembol listesi alinamadi: HTTP {r.status_code}")
            return []
        data = r.json()
        exclude = ("3L","3S","5L","5S","UP","DOWN","BULL","BEAR")
        symbols = []
        for t in data:
            sym = str(t.get("symbol","")).upper()
            if not sym.endswith("USDT") or "_" in sym:
                continue
            if any(sym[:-4].endswith(x) for x in exclude):
                continue
            qv = float(t.get("quoteVolume") or 0)
            if qv < min_volume:
                continue
            symbols.append({"symbol": sym, "volume_24h": qv})
        symbols.sort(key=lambda x: x["volume_24h"], reverse=True)
        return symbols
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []

def get_klines(symbol, interval="1h", limit=200):
    try:
        r = SESSION.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15, verify=False
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or len(data) < 50:
            return None
        rows = []
        for k in data:
            try:
                rows.append({
                    "timestamp": pd.to_datetime(int(k[0]), unit="ms"),
                    "Open": float(k[1]), "High": float(k[2]),
                    "Low": float(k[3]), "Close": float(k[4]),
                    "Volume": float(k[5])
                })
            except Exception:
                continue
        if len(rows) < 50:
            return None
        df = pd.DataFrame(rows).set_index("timestamp")
        return df[['Open','High','Low','Close','Volume']].dropna()
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════
# 🎯 SİNYAL TARAMA — SADECE VOLUM HACIM 5X
# ══════════════════════════════════════════════════════════════
SETTINGS = {
    "vol_ma_period": 20,
    "volume_spike_mult": VOLUME_SPIKE_MULT,
}

def scan_signal(df, timeframe="1h"):
    s = SETTINGS
    if df is None or len(df) < 50:
        return []

    close, volume = df['Close'], df['Volume']

    vol_ma = calc_sma(volume, s['vol_ma_period'])
    vol_ratio = calc_vol_ratio(df, s['vol_ma_period'])

    i, ip = -1, -2

    def f(series, fallback=0.0):
        v = series.iloc[i]
        return float(v) if not pd.isna(v) else fallback

    cur_close = f(close, 0)
    prev_close = float(close.iloc[ip]) if not pd.isna(close.iloc[ip]) else cur_close
    cur_vol = f(volume, 0)
    cur_vol_ma = f(vol_ma, cur_vol)
    cur_vol_r = f(vol_ratio, 1.0)

    results = []

    # VOLUM HACIM 5X — 1H hacim, ortalamanin 5 kati veya uzerine ciktiginda tetiklenir
    if cur_vol_r >= s['volume_spike_mult']:
        direction = "AL" if cur_close >= prev_close else "SAT"
        hedef = cur_close * (1.02 if direction == "AL" else 0.98)
        results.append({
            "type": "VOLUM_HACIM_5X",
            "source": "Hacim Momentumu 5X",
            "direction": direction,
            "price": cur_close,
            "hedef": round(hedef, 8),
            "beklenti": round(abs(hedef - cur_close) / cur_close * 100, 2),
            "vol_ratio": round(cur_vol_r, 2),
            "strength": 90,
            "timeframe": timeframe
        })

    return results

# ══════════════════════════════════════════════════════════════
# 📨 TELEGRAM
# ══════════════════════════════════════════════════════════════
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram token veya chat_id eksik!")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram gonderim hatasi: {e}")
        return False

def format_message(symbol: str, sig: dict) -> str:
    yön_emoji = "🟢" if sig['direction'] == "AL" else "🔴"

    return (
        f"📦 <b>VOLUM HACIM 5X</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>{symbol}</b> — {yön_emoji} <b>{sig['direction']}</b>\n"
        f"💡 Kaynak: {sig['source']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Fiyat: <b>{sig['price']}</b>\n"
        f"🎯 Hedef: <b>{sig['hedef']}</b> (%{sig['beklenti']})\n"
        f"📦 Hacim Orani: <b>{sig['vol_ratio']}x</b>\n"
        f"⏱ Zaman Dilimi: {sig['timeframe']}\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )

def should_send(symbol: str, sig_type: str) -> bool:
    """Ayni sinyal son 1 saatte gonderildiyse tekrar gonderme."""
    key = f"{symbol}_{sig_type}"
    now = time.time()
    if key in sent_signals:
        if now - sent_signals[key] < SIGNAL_COOLDOWN:
            return False
    sent_signals[key] = now
    return True

# ══════════════════════════════════════════════════════════════
# 🔄 ANA TARAMA DÖNGÜSÜ
# ══════════════════════════════════════════════════════════════
def run_scan():
    log.info(f"Tarama basladi — TF: {TIMEFRAME}, Max: {MAX_COINS} coin, Esik: {VOLUME_SPIKE_MULT}x hacim")
    symbols = get_symbols(min_volume=MIN_VOLUME)
    if not symbols:
        log.error("Coin listesi alinamadi!")
        return

    symbols = symbols[:MAX_COINS]
    total = len(symbols)
    found = 0

    for idx, coin in enumerate(symbols):
        symbol = coin["symbol"]
        try:
            df = get_klines(symbol, TIMEFRAME, limit=200)
            signals = scan_signal(df, TIMEFRAME)

            for sig in signals:
                if should_send(symbol, sig['type']):
                    msg = format_message(symbol, sig)
                    if send_telegram(msg):
                        log.info(f"✅ Telegram: {symbol} {sig['type']} ({sig['vol_ratio']}x)")
                        found += 1
                    time.sleep(0.3)

            time.sleep(0.1)
        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{total}] tarandi, {found} sinyal gonderildi")

    log.info(f"Tarama tamamlandi — {found} sinyal gonderildi")

def main():
    log.info("=" * 50)
    log.info("MONEY TRADER TELEGRAM BOT baslatildi")
    log.info(f"Tarama araligi: {SCAN_INTERVAL} saniye")
    log.info(f"Zaman dilimi: {TIMEFRAME}")
    log.info(f"Max coin: {MAX_COINS}")
    log.info(f"Hacim esigi: {VOLUME_SPIKE_MULT}x")
    log.info("=" * 50)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID environment variable'lari eksik!")
        log.error("Render'da Environment Variables bolumune ekleyin.")
        return

    # Baslangic mesaji
    send_telegram(
        f"🤖 <b>MONEY TRADER BOT BAŞLADI</b>\n"
        f"⏱ Her {SCAN_INTERVAL//60} dakikada bir tarama\n"
        f"📊 Zaman dilimi: {TIMEFRAME}\n"
        f"🔢 Max coin: {MAX_COINS}\n"
        f"📦 Filtre: Sadece VOLUM HACIM {VOLUME_SPIKE_MULT}x ve uzeri\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Tarama dongusu hatasi: {e}")
            send_telegram(f"⚠️ Bot hatasi: {e}")

        log.info(f"Sonraki tarama {SCAN_INTERVAL} saniye sonra...")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
