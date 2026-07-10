# -*- coding: utf-8 -*-
"""
MAJOR KIRILIM BOT
Strateji: Fiyat SMA100 (Major) VE Span B'yi aynı anda %7+ yukarı/aşağı kırdığında
Telegram bildirimi gönderir.
Zaman dilimi: 15 dakika
Tarama aralığı: 5 dakika (ayarlanabilir)
Borsa: Binance Futures (USDT-M Perpetual)
"""
import os
import time
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
# ⚙️ AYARLAR (Render Environment Variables)
# ══════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))  # 5 dakika
TIMEFRAME = os.environ.get("SCAN_TIMEFRAME", "15m")  # 15 dakika
MAX_COINS = int(os.environ.get("MAX_COINS", "200"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", "1000000"))

# Strateji parametreleri
MAJOR_LEN = int(os.environ.get("MAJOR_LEN", "100"))       # SMA100
SPANB_LEN = int(os.environ.get("SPANB_LEN", "52"))        # Span B periyodu
BREAK_PCT = float(os.environ.get("BREAK_PCT", "7.0"))     # %7 kırılım eşiği
SIGNAL_COOLDOWN = int(os.environ.get("SIGNAL_COOLDOWN", "3600"))  # Aynı sinyal için 1 saat bekleme

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MajorKirilimBot/1.0"})

# Gönderilen sinyalleri takip et
sent_signals: dict = {}

# ══════════════════════════════════════════════════════════════
# 📊 TEKNİK GÖSTERGELER (Pine Script ile birebir)
# ══════════════════════════════════════════════════════════════
def calc_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()

def calc_spanb(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
    """
    Pine Script: spanB_raw = (ta.highest(high, spanb_len) + ta.lowest(low, spanb_len)) / 2
    """
    return (high.rolling(window=period).max() + low.rolling(window=period).min()) / 2

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([h - l, abs(h - c.shift()), abs(l - c.shift())], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

# ══════════════════════════════════════════════════════════════
# 🌐 BINANCE FUTURES API
# ══════════════════════════════════════════════════════════════
def get_symbols(min_volume: float = 0) -> list:
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=30, verify=False)
        if r.status_code != 200:
            log.error(f"Sembol listesi alınamadı: HTTP {r.status_code}")
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
            symbols.append({
                "symbol": sym,
                "volume_24h": qv,
                "price": float(t.get("lastPrice") or 0),
                "change_24h": float(t.get("priceChangePercent") or 0),
            })
        symbols.sort(key=lambda x: x["volume_24h"], reverse=True)
        return symbols
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_klines(symbol: str, interval: str = "15m", limit: int = 200):
    try:
        r = SESSION.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15, verify=False
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or len(data) < max(MAJOR_LEN, SPANB_LEN) + 5:
            return None
        rows = []
        for k in data:
            try:
                rows.append({
                    "ts": pd.to_datetime(int(k[0]), unit="ms"),
                    "Open": float(k[1]),
                    "High": float(k[2]),
                    "Low": float(k[3]),
                    "Close": float(k[4]),
                    "Volume": float(k[5]),
                })
            except Exception:
                continue
        if len(rows) < 50:
            return None
        df = pd.DataFrame(rows).set_index("ts")
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════
# 🎯 ANA STRATEJİ: MAJOR + SPANB KIRILIMI
# ══════════════════════════════════════════════════════════════
def check_signal(df: pd.DataFrame, symbol: str) -> list:
    """
    Koşul (AL):
      - Fiyat SMA100'ü (Major) %7+ yukarı kırdı (crossover + mesafe >= %7)
      - Fiyat Span B'yi (sarı çizgi) %7+ yukarı kırdı
      - Her iki koşul aynı mumda

    Koşul (SAT):
      - Fiyat SMA100'ü (Major) %7+ aşağı kırdı
      - Fiyat Span B'yi %7+ aşağı kırdı
      - Her iki koşul aynı mumda
    """
    if df is None or len(df) < MAJOR_LEN + 5:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    major = calc_sma(close, MAJOR_LEN)
    spanb = calc_spanb(high, low, SPANB_LEN)
    atr = calc_atr(df, 14)

    # Son iki mum (kapanmış mumlar — repaint yok)
    i, ip = -2, -3  # -2: son kapanmış mum, -3: ondan önceki

    def safe(s, idx, default=0.0):
        try:
            v = s.iloc[idx]
            return float(v) if not pd.isna(v) else default
        except Exception:
            return default

    cur_close = safe(close, i)
    prev_close = safe(close, ip)
    cur_major = safe(major, i)
    prev_major = safe(major, ip)
    cur_spanb = safe(spanb, i)
    prev_spanb = safe(spanb, ip)
    cur_atr = safe(atr, i, cur_close * 0.01)
    cur_vol = safe(df["Volume"], i)

    if cur_major <= 0 or cur_spanb <= 0:
        return []

    # Mesafe hesaplama (fiyat ile seviye arasındaki %)
    dist_major = (cur_close - cur_major) / cur_major * 100  # + = üstünde, - = altında
    dist_spanb = (cur_close - cur_spanb) / cur_spanb * 100

    # Crossover kontrolü (önceki mumda altında, şimdiki mumda üstünde — Pine Script ta.crossover)
    cross_major_up = (cur_close > cur_major) and (prev_close <= prev_major)
    cross_major_dn = (cur_close < cur_major) and (prev_close >= prev_major)
    cross_spanb_up = (cur_close > cur_spanb) and (prev_close <= prev_spanb)
    cross_spanb_dn = (cur_close < cur_spanb) and (prev_close >= prev_spanb)

    # Zaten crossover olmuş ve mesafe %7+ ise de kabul et (yeni mum oluşmadan önceki kırılım)
    above_major_pct = dist_major >= BREAK_PCT
    below_major_pct = dist_major <= -BREAK_PCT
    above_spanb_pct = dist_spanb >= BREAK_PCT
    below_spanb_pct = dist_spanb <= -BREAK_PCT

    # AL koşulu: Her iki çizgiyi de %7+ yukarı kırdı
    signal_al = (cross_major_up and above_major_pct and above_spanb_pct) or \
                (cross_spanb_up and above_major_pct and above_spanb_pct)

    # SAT koşulu: Her iki çizgiyi de %7+ aşağı kırdı
    signal_sat = (cross_major_dn and below_major_pct and below_spanb_pct) or \
                 (cross_spanb_dn and below_major_pct and below_spanb_pct)

    results = []

    if signal_al:
        hedef = cur_close + cur_atr * 2
        results.append({
            "direction": "AL",
            "type": "MAJOR+SPANB_KIRILIM_AL",
            "price": cur_close,
            "major": round(cur_major, 8),
            "spanb": round(cur_spanb, 8),
            "dist_major": round(dist_major, 2),
            "dist_spanb": round(dist_spanb, 2),
            "hedef": round(hedef, 8),
            "beklenti": round((hedef - cur_close) / cur_close * 100, 2),
            "vol": round(cur_vol, 2),
        })

    if signal_sat:
        hedef = cur_close - cur_atr * 2
        results.append({
            "direction": "SAT",
            "type": "MAJOR+SPANB_KIRILIM_SAT",
            "price": cur_close,
            "major": round(cur_major, 8),
            "spanb": round(cur_spanb, 8),
            "dist_major": round(dist_major, 2),
            "dist_spanb": round(dist_spanb, 2),
            "hedef": round(hedef, 8),
            "beklenti": round((cur_close - hedef) / cur_close * 100, 2),
            "vol": round(cur_vol, 2),
        })

    return results

# ══════════════════════════════════════════════════════════════
# 📨 TELEGRAM
# ══════════════════════════════════════════════════════════════
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
    yon = sig["direction"]
    emoji = "🟢" if yon == "AL" else "🔴"
    ok = "↑" if yon == "AL" else "↓"

    return (
        f"🚀 <b>MAJOR KIRILIM SİNYALİ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>{symbol}</b> {emoji} <b>{yon}</b>\n"
        f"⏱ Zaman Dilimi: <b>{TIMEFRAME}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Fiyat: <b>{sig['price']}</b>\n"
        f"🎯 Hedef: <b>{sig['hedef']}</b> (%{sig['beklenti']})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧡 Major (SMA{MAJOR_LEN}): {sig['major']}\n"
        f"   └ Mesafe: <b>%{sig['dist_major']:+.2f}</b> {ok}\n"
        f"🟡 Span B (periyot {SPANB_LEN}): {sig['spanb']}\n"
        f"   └ Mesafe: <b>%{sig['dist_spanb']:+.2f}</b> {ok}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Kırılım eşiği: %{BREAK_PCT}+\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )


def should_send(symbol: str, sig_type: str) -> bool:
    key = f"{symbol}_{sig_type}"
    now = time.time()
    if key in sent_signals and (now - sent_signals[key]) < SIGNAL_COOLDOWN:
        return False
    sent_signals[key] = now
    return True

# ══════════════════════════════════════════════════════════════
# 🔄 ANA TARAMA DÖNGÜSÜ
# ══════════════════════════════════════════════════════════════
def run_scan():
    log.info(f"Tarama başladı — TF:{TIMEFRAME} Eşik:%{BREAK_PCT} Max:{MAX_COINS} coin")

    symbols = get_symbols(min_volume=MIN_VOLUME)
    if not symbols:
        log.error("Coin listesi alınamadı!")
        return

    symbols = symbols[:MAX_COINS]
    total = len(symbols)
    found = 0

    for idx, coin in enumerate(symbols):
        symbol = coin["symbol"]
        try:
            # MAJOR_LEN + SPANB_LEN'den büyük olanı + güvenlik payı kadar mum çek
            limit = max(MAJOR_LEN, SPANB_LEN) + 20
            df = get_klines(symbol, TIMEFRAME, limit=limit)
            sigs = check_signal(df, symbol)

            for sig in sigs:
                if should_send(symbol, sig["type"]):
                    msg = format_message(symbol, sig)
                    if send_telegram(msg):
                        log.info(f"✅ {symbol} {sig['type']} — %{sig['dist_major']:+.1f} major / %{sig['dist_spanb']:+.1f} spanb")
                        found += 1
                    time.sleep(0.3)

            time.sleep(0.1)

        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{total}] tarandı — {found} sinyal")

    log.info(f"✅ Tarama tamamlandı — {found} sinyal gönderildi")


def main():
    log.info("=" * 55)
    log.info("🚀 MAJOR KIRILIM BOT başlatıldı")
    log.info(f"   Strateji : SMA{MAJOR_LEN} + SpanB({SPANB_LEN}) kırılım %{BREAK_PCT}+")
    log.info(f"   Zaman    : {TIMEFRAME}")
    log.info(f"   Aralık   : her {SCAN_INTERVAL} saniye")
    log.info(f"   Max coin : {MAX_COINS}")
    log.info(f"   Min hacim: ${MIN_VOLUME/1e6:.1f}M USDT/24h")
    log.info("=" * 55)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        log.error("   Render → Environment Variables bölümüne ekleyin.")
        return

    send_telegram(
        f"🤖 <b>MAJOR KIRILIM BOT BAŞLADI</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Strateji: SMA{MAJOR_LEN} + SpanB({SPANB_LEN}) kırılım ≥%{BREAK_PCT}\n"
        f"⏱ Zaman dilimi: {TIMEFRAME}\n"
        f"🔄 Tarama aralığı: her {SCAN_INTERVAL//60} dakika\n"
        f"🔢 Max coin: {MAX_COINS} (hacme göre sıralı)\n"
        f"💵 Min 24s hacim: ${MIN_VOLUME/1e6:.1f}M USDT\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Döngü hatası: {e}")
            send_telegram(f"⚠️ <b>Bot Hatası:</b> {e}")

        log.info(f"Sonraki tarama {SCAN_INTERVAL} saniye sonra...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
