# -*- coding: utf-8 -*-
"""
ÇOK GÜÇLÜ ROKET AL / SAT BOT
Pine Script'teki "Quantum Golden + Confluence" stratejisinin birebir Python portu.
15dk mumlar, Binance Futures USDT ciftleri, Telegram bildirim.
"""
import os, time, logging
from datetime import datetime
import requests
import pandas as pd
import numpy as np
import urllib3, warnings

urllib3.disable_warnings()
warnings.filterwarnings('ignore')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════════════
# ⚙️ AYARLAR
# ════════════════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))
TIMEFRAME = os.getenv("SCAN_TIMEFRAME", "15m")
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))

# Quantum Golden Ayarlari (Pine'dan birebir)
HASSASIYET = float(os.getenv("HASSASIYET", "0.1"))       # Min momentum esigi %
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))            # Hedef ATR periyodu

# Confluence Ayarlari
RSI_PERIOD_2 = int(os.getenv("RSI_PERIOD_2", "14"))
EMA_PERIOD_2 = int(os.getenv("EMA_PERIOD_2", "10"))
MOM_PERIOD_2 = int(os.getenv("MOM_PERIOD_2", "10"))
VOL_MA_PERIOD_2 = int(os.getenv("VOL_MA_PERIOD_2", "20"))
FIB_LEN_2 = int(os.getenv("FIB_LEN_2", "100"))

# Major Level Ayarlari
MAJOR_LINE_LEN = int(os.getenv("MAJOR_LINE_LEN", "100"))
MAJOR_BREAK_PCT = float(os.getenv("MAJOR_BREAK_PCT", "1.0"))
USE_MAJOR_FILTER = os.getenv("USE_MAJOR_FILTER", "false").lower() == "true"

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
last_signal = {}

# ════════════════════════════════════════════════════════════════════════════
# 📊 VERI CEKME
# ════════════════════════════════════════════════════════════════════════════
def get_symbols():
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=10)
        data = r.json()
        syms = [s["symbol"] for s in data["symbols"]
                if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
        return syms[:MAX_COINS]
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []

def get_klines(symbol, limit=300):
    try:
        r = SESSION.get(f"{BINANCE_BASE}/fapi/v1/klines",
                        params={"symbol": symbol, "interval": TIMEFRAME, "limit": limit},
                        timeout=10)
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time","open","high","low","close","volume","close_time",
            "qav","trades","tbv","tqv","ignore"])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception:
        return None

# ════════════════════════════════════════════════════════════════════════════
# 📈 TEKNIK INDIKATORLER
# ════════════════════════════════════════════════════════════════════════════
def rsi(series, length):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.ewm(alpha=1/length, adjust=False).mean()
    ma_down = down.ewm(alpha=1/length, adjust=False).mean()
    rs = ma_up / ma_down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()

def sma(series, length):
    return series.rolling(window=length).mean()

def atr(df, period):
    high_low = df["high"] - df["low"]
    high_close = np.abs(df["high"] - df["close"].shift())
    low_close = np.abs(df["low"] - df["close"].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

# ════════════════════════════════════════════════════════════════════════════
# 🚀 QUANTUM GOLDEN HESAPLAMA (Heikin Ashi)
# ════════════════════════════════════════════════════════════════════════════
def calculate_heikin_ashi(df):
    """Pine'daki Heikin Ashi hesaplamasi"""
    ha = pd.DataFrame(index=df.index)
    ha["close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha["open"] = pd.Series(np.nan, index=df.index)
    ha.loc[0, "open"] = (df.loc[0, "open"] + df.loc[0, "close"]) / 2
    for i in range(1, len(df)):
        ha.loc[i, "open"] = (ha.loc[i-1, "open"] + ha.loc[i-1, "close"]) / 2
    ha["high"] = pd.concat([df["high"], ha["open"], ha["close"]], axis=1).max(axis=1)
    ha["low"] = pd.concat([df["low"], ha["open"], ha["close"]], axis=1).min(axis=1)
    return ha

# ════════════════════════════════════════════════════════════════════════════
# 🔗 CONFLUENCE HESAPLAMA (Ikinci Indikator)
# ════════════════════════════════════════════════════════════════════════════
def calculate_confluence(df):
    """Pine'daki confluence_active hesaplamasi"""
    close = df["close"]
    volume = df["volume"]

    rsi_val = rsi(close, RSI_PERIOD_2)
    rsi_ema = ema(rsi_val, EMA_PERIOD_2)
    momentum = close - close.shift(MOM_PERIOD_2)
    vol_ma = sma(volume, VOL_MA_PERIOD_2)

    rsi_top = rsi_val.rolling(FIB_LEN_2).max()
    rsi_bot = rsi_val.rolling(FIB_LEN_2).min()
    fib_500 = rsi_top - (rsi_top - rsi_bot) * 0.5

    # Son kapanmis mum
    i, p = -1, -2
    rsi_now, rsi_prev = rsi_val.iloc[i], rsi_val.iloc[p]
    ema_now, ema_prev = rsi_ema.iloc[i], rsi_ema.iloc[p]
    mom_now = momentum.iloc[i]
    vol_now, vol_ma_now = volume.iloc[i], vol_ma.iloc[i]
    fib_prev = fib_500.iloc[p]

    if pd.isna(rsi_now) or pd.isna(vol_ma_now) or pd.isna(fib_prev):
        return False

    mom_bull = mom_now > 0
    mom_bear = mom_now < 0
    vol_high = vol_now > vol_ma_now

    # AL confluence
    rsi_cross_up = rsi_prev <= ema_prev and rsi_now > ema_now
    fibo_al = rsi_now > fib_prev and rsi_prev <= fib_prev
    buy_signal_2 = (rsi_cross_up and mom_bull and vol_high) or (fibo_al and mom_bull and vol_high)

    # SAT confluence
    rsi_cross_down = rsi_prev >= ema_prev and rsi_now < ema_now
    fibo_sat = rsi_now < fib_prev and rsi_prev >= fib_prev
    sell_signal_2 = (rsi_cross_down and mom_bear and vol_high) or (fibo_sat and mom_bear and vol_high)

    return {"buy": buy_signal_2, "sell": sell_signal_2}

# ════════════════════════════════════════════════════════════════════════════
# 📈 MAJOR LEVEL HESAPLAMA
# ════════════════════════════════════════════════════════════════════════════
def calculate_major_level(df):
    """Pine'daki majorLevel hesaplamasi"""
    major_level = sma(df["close"], MAJOR_LINE_LEN)
    close_now = df["close"].iloc[-1]
    major_now = major_level.iloc[-1]

    if pd.isna(major_now):
        return {"up": True, "down": True, "level": None}

    dist = abs(close_now - major_now) / major_now * 100
    major_break_up = close_now > major_now and dist >= MAJOR_BREAK_PCT
    major_break_dn = close_now < major_now and dist >= MAJOR_BREAK_PCT

    major_filter_long = not USE_MAJOR_FILTER or major_break_up
    major_filter_short = not USE_MAJOR_FILTER or major_break_dn

    return {
        "up": major_filter_long,
        "down": major_filter_short,
        "level": major_now,
        "dist": dist,
        "trend_up": close_now > major_now
    }

# ════════════════════════════════════════════════════════════════════════════
# 🚀 SINYAL KONTROL: ÇOK GÜÇLÜ ROKET AL / SAT
# ════════════════════════════════════════════════════════════════════════════
# Her coin icin son sinyal durumunu tut (tekrar onlemek icin)
son_sinyal_dict = {}  # {symbol: 0=none, 1=AL, -1=SAT}

def check_signal(df, symbol):
    """
    Pine'daki Quantum Golden + Confluence mantigi:
    AL: Heikin Ashi donusu + Confluence + Major Filter
    SAT: Heikin Ashi donusu (asagi) + Confluence + Major Filter
    """
    if df is None or len(df) < max(MAJOR_LINE_LEN, FIB_LEN_2) + 20:
        return None

    # Son kapanmis mum
    df_closed = df.iloc[:-1]
    if len(df_closed) < 2:
        return None

    # Heikin Ashi hesapla
    ha = calculate_heikin_ashi(df_closed)

    i = -1   # Son kapanmis mum
    p = -2   # Onceki kapanmis mum

    ha_close_now = ha["close"].iloc[i]
    ha_open_now = ha["open"].iloc[i]
    ha_close_prev = ha["close"].iloc[p]
    ha_open_prev = ha["open"].iloc[p]
    ha_high_prev = ha["high"].iloc[p]

    # Mum durumlari
    ha_is_up = ha_close_now > ha_open_now
    ha_is_down = ha_close_now < ha_open_now
    ha_was_down = ha_close_prev < ha_open_prev
    sert_yukselis = ha_close_now > ha_high_prev

    # Mum gucu hesaplama
    govde_degisim = ((ha_close_now - ha_open_now) / ha_open_now) * 100
    mutlak_degisim = abs(govde_degisim)

    avg_body = pd.Series([abs((ha["close"].iloc[j] - ha["open"].iloc[j]) / ha["open"].iloc[j] * 100) 
                          for j in range(-10, 0)]).mean()
    sinyal_gucu = min(100, (mutlak_degisim / (avg_body if avg_body > 0 else 1)) * 50)

    # Basari olasiligi
    olasilik = 65 + (sinyal_gucu / 4) + (12 if mutlak_degisim > HASSASIYET * 2.5 else 0)
    olasilik_son = min(98, olasilik)

    # ATR hedef hesaplama
    atr_val = atr(df_closed, ATR_PERIOD).iloc[i]
    close_now = df_closed["close"].iloc[i]

    # Confluence hesapla
    confluence = calculate_confluence(df_closed)
    if confluence is None:
        return None

    # Major Level hesapla
    major = calculate_major_level(df_closed)

    # Son sinyal durumu (tekrar onleme)
    son_sinyal = son_sinyal_dict.get(symbol, 0)

    # ═══════════════════════════════════════════════════════════════════
    # 🚀 ÇOK GÜÇLÜ ROKET AL
    # ═══════════════════════════════════════════════════════════════════
    signal_al = (son_sinyal != 1 and 
                 ha_is_up and 
                 (ha_was_down or sert_yukselis) and 
                 mutlak_degisim >= HASSASIYET and 
                 major["up"] and
                 confluence["buy"])

    if signal_al:
        son_sinyal_dict[symbol] = 1
        t_price = close_now + (atr_val * 2)
        beklenti_yuzde = ((t_price - close_now) / close_now) * 100
        is_strong = 85 <= olasilik_son <= 90

        return {
            "direction": "AL",
            "type": "💎 GÜÇLÜ AL" if is_strong else "⚡ AL",
            "is_confluence": True,
            "price": round(float(close_now), 4),
            "target": round(float(t_price), 4),
            "beklenti": round(float(beklenti_yuzde), 2),
            "sinyal_gucu": round(float(sinyal_gucu), 0),
            "olasilik": round(float(olasilik_son), 0),
            "major_level": round(float(major["level"]), 4) if major["level"] else None,
            "major_dist": round(float(major["dist"]), 2) if major["dist"] else None
        }

    # ═══════════════════════════════════════════════════════════════════
    # 🔻 ÇOK GÜÇLÜ ROKET SAT
    # ═══════════════════════════════════════════════════════════════════
    # SAT icin: Heikin Ashi asagi donus + Confluence sell + Major filter
    ha_was_up = ha_close_prev > ha_open_prev
    sert_dusus = ha_close_now < ha["low"].iloc[p]

    signal_sat = (son_sinyal != -1 and 
                  ha_is_down and 
                  (ha_was_up or sert_dusus) and 
                  mutlak_degisim >= HASSASIYET and 
                  major["down"] and
                  confluence["sell"])

    if signal_sat:
        son_sinyal_dict[symbol] = -1
        t_price = close_now - (atr_val * 2)
        beklenti_yuzde = ((close_now - t_price) / close_now) * 100
        is_strong = 85 <= olasilik_son <= 90

        return {
            "direction": "SAT",
            "type": "💥 GÜÇLÜ SAT" if is_strong else "🔻 SAT",
            "is_confluence": True,
            "price": round(float(close_now), 4),
            "target": round(float(t_price), 4),
            "beklenti": round(float(beklenti_yuzde), 2),
            "sinyal_gucu": round(float(sinyal_gucu), 0),
            "olasilik": round(float(olasilik_son), 0),
            "major_level": round(float(major["level"]), 4) if major["level"] else None,
            "major_dist": round(float(major["dist"]), 2) if major["dist"] else None
        }

    # Heikin Ashi kirmizi ise AL sinyalini resetle (Pine'daki mantik)
    if ha_is_down:
        son_sinyal_dict[symbol] = 0

    return None

# ════════════════════════════════════════════════════════════════════════════
# 📨 TELEGRAM
# ════════════════════════════════════════════════════════════════════════════
def should_send(symbol, direction):
    key = f"{symbol}_{direction}"
    now = time.time()
    if now - last_signal.get(key, 0) < SIGNAL_COOLDOWN:
        return False
    last_signal[key] = now
    return True

def send_telegram(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
                          timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False

def format_message(symbol, sig):
    emoji = "🚀" if sig["direction"] == "AL" else "🔻"
    coin = symbol.replace("USDT", "/USDT")
    sep = "━" * 16

    lines = [
        f"{emoji} <b>{sig['type']} - ÇOK GÜÇLÜ ROKET {sig['direction']}</b>",
        sep,
        f"📍 <b>{coin}</b>",
        f"⏱ Zaman Dilimi: <b>{TIMEFRAME}</b>",
        f"💰 Fiyat: <b>{sig['price']}</b>",
        f"🎯 Hedef: <b>{sig['target']}</b> (%{sig['beklenti']})",
        f"📊 Sinyal Gücü: <b>%{sig['sinyal_gucu']}</b>",
        f"✅ Başarı Olasılığı: <b>%{sig['olasilik']}</b>",
    ]

    if sig["major_level"]:
        trend = "📈 ÜSTÜNDE" if sig["direction"] == "AL" else "📉 ALTINDA"
        lines.append(f"📈 Major Level (SMA{MAJOR_LINE_LEN}): <b>{sig['major_level']}</b> ({trend})")

    lines.extend([
        sep,
        f"🕐 {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ])
return "\\n".join(lines)

# ════════════════════════════════════════════════════════════════════════════
# 🔄 TARAMA
# ════════════════════════════════════════════════════════════════════════════
def run_scan():
    symbols = get_symbols()
    log.info(f"🚀 ROKET TARAMA basladi TF:{TIMEFRAME} Coin:{len(symbols)}")
    found = 0

    for idx, symbol in enumerate(symbols):
        try:
            df = get_klines(symbol, limit=300)
            sig = check_signal(df, symbol)

            if sig and should_send(symbol, sig["direction"]):
                if send_telegram(format_message(symbol, sig)):
                    found += 1
                    log.info(f"🚀 SINYAL {symbol} {sig['direction']} Fiyat:{sig['price']} Hedef:{sig['target']} Olasilik:%{sig['olasilik']}")

            time.sleep(0.15)

        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{len(symbols)}] tarandi {found} sinyal")

    log.info(f"🚀 Tarama tamamlandi {found} sinyal gonderildi")

# ════════════════════════════════════════════════════════════════════════════
# 🚀 MAIN
# ════════════════════════════════════════════════════════════════════════════
def main():
    log.info("🚀 ÇOK GÜÇLÜ ROKET AL/SAT BOT baslatildi")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"🚀 <b>ÇOK GÜÇLÜ ROKET AL/SAT BOT BASLADI</b>
"
        f"Zaman dilimi: <b>{TIMEFRAME}</b>
"
        f"Major Level: <b>SMA{MAJOR_LINE_LEN}</b>
"
        f"Hassasiyet: <b>%{HASSASIYET}</b>
"
        f"Confluence: <b>RSI+EMA+Momentum+Hacim</b>"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
