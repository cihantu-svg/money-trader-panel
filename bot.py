# -*- coding: utf-8 -*-
"""
MONEY TRADER - TELEGRAM BOT (Binance Futures)
Her 5 dakikada bir 1 saatlik mumda tarama yapar.
Yeni sinyal geldiğinde Telegram'a bildirim gönderir.
Render'da ayrı bir background worker olarak çalışır.
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
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL    = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))   # 5 dakika
TIMEFRAME        = os.environ.get("SCAN_TIMEFRAME", "1h")
MAX_COINS        = int(os.environ.get("MAX_COINS", "200"))
MIN_VOLUME       = float(os.environ.get("MIN_VOLUME_USDT", "1000000"))

BINANCE_BASE  = "https://fapi.binance.com"
SESSION       = requests.Session()
SESSION.headers.update({"User-Agent": "MoneyTrader-TelegramBot/1.0"})

# Daha önce gönderilen sinyalleri takip et (aynı sinyali tekrar gönderme)
# Key: symbol+type, Value: son gönderim zamanı (unix timestamp)
sent_signals: dict = {}
SIGNAL_COOLDOWN = 3600  # aynı coin+sinyal için 1 saat bekleme

# ══════════════════════════════════════════════════════════════
# 📊 TEKNİK GÖSTERGELER (app.py ile birebir aynı)
# ══════════════════════════════════════════════════════════════
def calc_rsi(series, period=14):
    delta    = series.diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_sma(series, period):
    return series.rolling(window=period).mean()

def calc_atr(df, period=14):
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([h-l, abs(h-c.shift()), abs(l-c.shift())], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def calc_momentum(series, period=10):
    return series.diff(period)

def calc_heikin_ashi(df):
    ha_c = (df['Open'] + df['High'] + df['Low'] + df['Close']) / 4
    ha_o = pd.Series(index=df.index, dtype=float)
    ha_o.iloc[0] = (df['Open'].iloc[0] + df['Close'].iloc[0]) / 2
    for i in range(1, len(df)):
        ha_o.iloc[i] = (ha_o.iloc[i-1] + ha_c.iloc[i-1]) / 2
    ha_h = pd.concat([df['High'], ha_o, ha_c], axis=1).max(axis=1)
    return pd.DataFrame({'haOpen': ha_o, 'haHigh': ha_h, 'haClose': ha_c}, index=df.index)

def calc_obv(df):
    obv = [0]
    for i in range(1, len(df)):
        if df['Close'].iloc[i] > df['Close'].iloc[i-1]:
            obv.append(obv[-1] + df['Volume'].iloc[i])
        elif df['Close'].iloc[i] < df['Close'].iloc[i-1]:
            obv.append(obv[-1] - df['Volume'].iloc[i])
        else:
            obv.append(obv[-1])
    return pd.Series(obv, index=df.index)

def calc_vol_ratio(df, period=20):
    return df['Volume'] / df['Volume'].rolling(period).mean().replace(0, np.nan)

# ══════════════════════════════════════════════════════════════
# 🌐 BINANCE FUTURES API
# ══════════════════════════════════════════════════════════════
def get_symbols(min_volume=0):
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=30, verify=False)
        if r.status_code != 200:
            log.error(f"Sembol listesi alınamadı: HTTP {r.status_code}")
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
                    "Low": float(k[3]),  "Close": float(k[4]),
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
# 🎯 SİNYAL TARAMA (app.py ile birebir aynı mantık)
# ══════════════════════════════════════════════════════════════
SETTINGS = {
    "rsi_period": 14, "ema_period": 10, "mom_period": 10,
    "vol_ma_period": 20, "fib_len": 100, "obv_ema_len": 3,
    "hassasiyet": 0.1, "atr_period": 14, "atr_mult": 2.0,
    "base_olasilik": 65.0, "guc_carpan": 50.0,
    "olasilik_artis_bolucu": 4.0, "ek_olasilik_bonusu": 12.0,
    "hassasiyet_carpan": 2.5, "max_olasilik": 98.0,
    "min_guc_seviyesi": 0.0,
    "major_len": 100, "major_break_pct": 1.0, "use_major_filter": True,
    "tavan_breakout_pct": 0.5, "tavan_min_vol_ratio": 1.2,
    "sat_breakout_pct": 0.5, "sat_min_vol_ratio": 1.2,
    "auto_zone_days": 50, "auto_peak_pct": 3.0, "auto_dip_pct": 3.0,
    "enable_tavan_al": True, "enable_sat": True,
    "enable_quantum_al": True, "enable_obv_al": True, "enable_obv_sat": True,
}

def scan_signal(df, timeframe="1h"):
    s = SETTINGS
    if df is None or len(df) < 50:
        return []

    close, high, low, volume = df['Close'], df['High'], df['Low'], df['Volume']

    rsi_val      = calc_rsi(close, s['rsi_period'])
    momentum_val = calc_momentum(close, s['mom_period'])
    vol_ma       = calc_sma(volume, s['vol_ma_period'])
    vol_ratio    = calc_vol_ratio(df, s['vol_ma_period'])
    atr_val      = calc_atr(df, s['atr_period'])
    major_level  = calc_sma(close, s['major_len'])

    rsi_top = rsi_val.rolling(window=s['fib_len']).max()
    rsi_bot = rsi_val.rolling(window=s['fib_len']).min()
    fib_500 = rsi_top - (rsi_top - rsi_bot) * 0.5

    ha      = calc_heikin_ashi(df)
    obv_val = calc_obv(df)
    obv_ema = calc_ema(obv_val, s['obv_ema_len'])
    pdh     = high.shift(1)
    pdl     = low.shift(1)

    i, ip = -1, -2

    def f(series, fallback=0.0):
        v = series.iloc[i]
        return float(v) if not pd.isna(v) else fallback

    cur_close  = f(close,   0)
    prev_close = float(close.iloc[ip])
    cur_rsi    = f(rsi_val, 50)
    prev_rsi   = float(rsi_val.iloc[ip]) if not pd.isna(rsi_val.iloc[ip]) else 50
    prev_fib   = float(fib_500.iloc[ip]) if not pd.isna(fib_500.iloc[ip]) else 50
    cur_fib    = f(fib_500, 50)
    cur_vol    = f(volume,  0)
    cur_vol_ma = f(vol_ma,  cur_vol)
    cur_vol_r  = f(vol_ratio, 1.0)
    cur_atr    = f(atr_val, cur_close * 0.02)
    cur_major  = f(major_level, cur_close)
    prev_major = float(major_level.iloc[ip]) if not pd.isna(major_level.iloc[ip]) else cur_close
    cur_pdh    = f(pdh, cur_close)
    cur_pdl    = f(pdl, cur_close)
    cur_obv    = float(obv_val.iloc[i])
    prev_obv   = float(obv_val.iloc[ip])
    cur_obv_e  = float(obv_ema.iloc[i])
    prev_obv_e = float(obv_ema.iloc[ip])

    ha_c  = float(ha['haClose'].iloc[i])
    ha_o  = float(ha['haOpen'].iloc[i])
    p_ha_c = float(ha['haClose'].iloc[ip])
    p_ha_o = float(ha['haOpen'].iloc[ip])
    p_ha_h = float(ha['haHigh'].iloc[ip])

    # Sinyaller
    fibo_al  = (cur_rsi > prev_fib) and (prev_rsi <= prev_fib)
    fibo_sat = (cur_rsi < prev_fib) and (prev_rsi >= prev_fib)
    mom_al   = (cur_rsi > 50) and (prev_rsi <= 50) and (cur_vol > cur_vol_ma)
    mom_sat  = (cur_rsi < 50) and (prev_rsi >= 50) and (cur_vol > cur_vol_ma)

    dist_major = abs(cur_close - cur_major) / cur_major * 100
    maj_break_up   = (cur_close > cur_major) and (prev_close <= prev_major)
    maj_break_down = (cur_close < cur_major) and (prev_close >= prev_major)
    maj_above      = cur_close > cur_major
    maj_below      = cur_close < cur_major
    maj_al  = maj_break_up  or (maj_above and dist_major >= s['major_break_pct'])
    maj_sat = maj_break_down or (maj_below and dist_major >= s['major_break_pct'])

    obv_up   = (cur_obv > cur_obv_e) and (prev_obv <= prev_obv_e)
    obv_down = (cur_obv < cur_obv_e) and (prev_obv >= prev_obv_e)

    govde  = ((ha_c - ha_o) / ha_o * 100) if ha_o > 0 else 0
    mutlak = abs(govde)
    q_al   = (ha_c > ha_o) and (p_ha_c <= p_ha_o or ha_c > p_ha_h) and mutlak >= s['hassasiyet']

    auto_high = high.rolling(50).max().iloc[i]
    auto_low  = low.rolling(50).min().iloc[i]
    peak_bot  = auto_high * (1 - s['auto_peak_pct'] / 100)
    dip_top   = auto_low  * (1 + s['auto_dip_pct']  / 100)
    tavan_k   = (cur_close > cur_pdh) and (prev_close <= cur_pdh) and \
                (cur_vol_r >= s['tavan_min_vol_ratio']) and not (cur_pdh >= peak_bot)
    destek_k  = (cur_close < cur_pdl) and (prev_close >= cur_pdl) and \
                (cur_vol_r >= s['sat_min_vol_ratio']) and not (cur_pdl <= dip_top)

    results = []

    def sig(t, src, d, strength=80, prob=80):
        hedef = cur_close + cur_atr*s['atr_mult'] if d=="AL" else cur_close - cur_atr*s['atr_mult']
        return {
            "type": t, "source": src, "direction": d,
            "price": cur_close, "hedef": round(hedef,8),
            "beklenti": round(abs(hedef-cur_close)/cur_close*100,2),
            "rsi": round(cur_rsi,2), "fib500": round(cur_fib,2),
            "major": round(cur_major,6), "dist_major": round(dist_major,2),
            "vol_ratio": round(cur_vol_r,2), "strength": strength,
            "timeframe": timeframe
        }

    if maj_al and fibo_al and mom_al:
        results.append(sig("MAJOR+FIBO+MOM_AL", "Major↑ + Fibo AL + Momentum", "AL", 98, 98))
    elif maj_al and fibo_al:
        results.append(sig("MAJOR+FIBO_AL", "Major Bölgesi + Fibo AL", "AL", 85, 85))
    elif mom_al and maj_al:
        results.append(sig("MOMENTUM_AL", "RSI50 Crossover + Hacim", "AL", 70, 70))
    elif fibo_al and maj_al:
        results.append(sig("FIBO_AL", "Fibo50 Crossover", "AL", 70, 70))

    if maj_sat and fibo_sat and mom_sat:
        results.append(sig("MAJOR+FIBO+MOM_SAT", "Major↓ + Fibo SAT + Momentum", "SAT", 98, 98))
    elif maj_sat and fibo_sat:
        results.append(sig("MAJOR+FIBO_SAT", "Major Bölgesi + Fibo SAT", "SAT", 85, 85))
    elif mom_sat and maj_sat:
        results.append(sig("MOMENTUM_SAT", "RSI50 Crossunder + Hacim", "SAT", 70, 70))
    elif fibo_sat and maj_sat:
        results.append(sig("FIBO_SAT", "Fibo50 Crossunder", "SAT", 70, 70))

    if obv_up and maj_al:
        results.append(sig("OBV_AL", "OBV Crossover", "AL", 70, 70))
    if obv_down and maj_sat:
        results.append(sig("OBV_SAT", "OBV Crossunder", "SAT", 70, 70))
    if q_al and maj_al:
        results.append(sig("QUANTUM_AL", "Heikin Ashi Momentum", "AL", 75, 75))
    if tavan_k:
        results.append(sig("TAVAN_KIRILIS", "Direnç Kırılımı", "AL", 80, 80))
    if destek_k:
        results.append(sig("DESTEK_KIRILIS", "Destek Kırılımı", "SAT", 80, 80))

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
        log.error(f"Telegram gönderim hatası: {e}")
        return False

def format_message(symbol: str, sig: dict) -> str:
    yön_emoji = "🟢" if sig['direction'] == "AL" else "🔴"
    tip_emoji = "🚀" if "MAJOR+FIBO+MOM" in sig['type'] else \
                "⭐" if "MAJOR+FIBO" in sig['type'] else "📈"

    return (
        f"{tip_emoji} <b>MONEY TRADER SİNYALİ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💎 <b>{symbol}</b> — {yön_emoji} <b>{sig['direction']}</b>\n"
        f"📊 Sinyal: <code>{sig['type']}</code>\n"
        f"💡 Kaynak: {sig['source']}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Fiyat: <b>{sig['price']}</b>\n"
        f"🎯 Hedef: <b>{sig['hedef']}</b> (%{sig['beklenti']})\n"
        f"📉 RSI: {sig['rsi']} | Fib50: {sig['fib500']}\n"
        f"🧡 Major: {sig['major']} (%{sig['dist_major']} uzakta)\n"
        f"📦 Hacim Oranı: {sig['vol_ratio']}x\n"
        f"⏱ Zaman Dilimi: {sig['timeframe']}\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )

def should_send(symbol: str, sig_type: str) -> bool:
    """Aynı sinyal son 1 saatte gönderildiyse tekrar gönderme."""
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
    log.info(f"Tarama başladı — TF: {TIMEFRAME}, Max: {MAX_COINS} coin")
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
            df = get_klines(symbol, TIMEFRAME, limit=200)
            signals = scan_signal(df, TIMEFRAME)

            for sig in signals:
                if should_send(symbol, sig['type']):
                    msg = format_message(symbol, sig)
                    if send_telegram(msg):
                        log.info(f"✅ Telegram: {symbol} {sig['type']}")
                        found += 1
                    time.sleep(0.3)

            time.sleep(0.1)
        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{total}] tarandı, {found} sinyal gönderildi")

    log.info(f"Tarama tamamlandı — {found} sinyal gönderildi")


def main():
    log.info("=" * 50)
    log.info("MONEY TRADER TELEGRAM BOT başlatıldı")
    log.info(f"Tarama aralığı: {SCAN_INTERVAL} saniye")
    log.info(f"Zaman dilimi: {TIMEFRAME}")
    log.info(f"Max coin: {MAX_COINS}")
    log.info("=" * 50)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID environment variable'ları eksik!")
        log.error("Render'da Environment Variables bölümüne ekleyin.")
        return

    # Başlangıç mesajı
    send_telegram(
        f"🤖 <b>MONEY TRADER BOT BAŞLADI</b>\n"
        f"⏱ Her {SCAN_INTERVAL//60} dakikada bir tarama\n"
        f"📊 Zaman dilimi: {TIMEFRAME}\n"
        f"🔢 Max coin: {MAX_COINS}\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Tarama döngüsü hatası: {e}")
            send_telegram(f"⚠️ Bot hatası: {e}")

        log.info(f"Sonraki tarama {SCAN_INTERVAL} saniye sonra...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
