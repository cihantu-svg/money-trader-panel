# -*- coding: utf-8 -*-
"""
MAJOR KIRILIM BOT
Strateji: Fiyat SMA100 (Major) VEYA Span B (sari cizgi) crossover ile kirar
+ hacim onayi (son mum hacmi ortalamanin VOL_MULT kati ustunde) olursa
Telegram bildirimi gonderir.
Zaman dilimi: 15 dakika
Tarama araligi: 5 dakika (ayarlanabilir)
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

# ============================================================
# AYARLAR (Render Environment Variables)
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))  # 5 dakika
TIMEFRAME = os.environ.get("SCAN_TIMEFRAME", "15m")  # 15 dakika
MAX_COINS = int(os.environ.get("MAX_COINS", "200"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", "1000000"))

# Strateji parametreleri
MAJOR_LEN = int(os.environ.get("MAJOR_LEN", "100"))   # SMA100
SPANB_LEN = int(os.environ.get("SPANB_LEN", "52"))    # Span B periyodu
BREAK_PCT = float(os.environ.get("BREAK_PCT", "7.0")) # (artik kullanilmiyor - referans)
VOL_MA_LEN = int(os.environ.get("VOL_MA_LEN", "20"))  # Hacim ortalamasi periyodu
VOL_MULT = float(os.environ.get("VOL_MULT", "2.0"))   # Hacim onay carpani
MAX_LINE_GAP_PCT = float(os.environ.get("MAX_LINE_GAP_PCT", "1.0"))  # SMA100 ile Span B arasi max mesafe %
SIGNAL_COOLDOWN = int(os.environ.get("SIGNAL_COOLDOWN", "3600"))  # 1 saat bekleme

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MajorKirilimBot/1.0"})

# Gonderilen sinyalleri takip et
sent_signals: dict = {}

# ============================================================
# TEKNIK GOSTERGELER (Pine Script ile birebir)
# ============================================================
def calc_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()

def calc_spanb(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
    return (high.rolling(window=period).max() + low.rolling(window=period).min()) / 2

def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df['High'], df['Low'], df['Close']
    tr = pd.concat([h - l, abs(h - c.shift()), abs(l - c.shift())], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

# ============================================================
# BINANCE FUTURES API
# ============================================================
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

# ============================================================
# ANA STRATEJI: KIRILIM (CROSSOVER) + HACIM ONAYI
# ============================================================
def check_signal(df: pd.DataFrame, symbol: str) -> list:
    """
    AL : Fiyat SMA100 VEYA Span B yukari kirdi (crossover) + hacim onayi
    SAT: Fiyat SMA100 VEYA Span B asagi kirdi (crossover) + hacim onayi
    (%7 mesafe sarti KALDIRILDI - sadece kirilim + hacim)
    """
    if df is None or len(df) < MAJOR_LEN + 5:
        return []

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    vol = df["Volume"]

    major = calc_sma(close, MAJOR_LEN)
    spanb = calc_spanb(high, low, SPANB_LEN)
    atr = calc_atr(df, 14)
    vol_ma = vol.rolling(window=VOL_MA_LEN).mean()

    i, ip = -2, -3  # son iki kapanmis mum (repaint yok)

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
    cur_vol = safe(vol, i)
    cur_vol_ma = safe(vol_ma, i, 0.0)

    if cur_major <= 0 or cur_spanb <= 0:
        return []

    dist_major = (cur_close - cur_major) / cur_major * 100
    dist_spanb = (cur_close - cur_spanb) / cur_spanb * 100
    # SMA100 (Major) ile Span B (Sari) cizgileri arasindaki mesafe (fiyata gore %)
    line_gap_pct = abs(cur_major - cur_spanb) / cur_close * 100
    line_gap_ok = line_gap_pct <= MAX_LINE_GAP_PCT

    cross_major_up = (cur_close > cur_major) and (prev_close <= prev_major)
    cross_major_dn = (cur_close < cur_major) and (prev_close >= prev_major)
    cross_spanb_up = (cur_close > cur_spanb) and (prev_close <= prev_spanb)
    cross_spanb_dn = (cur_close < cur_spanb) and (prev_close >= prev_spanb)

    vol_ok = (cur_vol_ma > 0) and (cur_vol >= cur_vol_ma * VOL_MULT)
    vol_ratio = round(cur_vol / cur_vol_ma, 2) if cur_vol_ma > 0 else 0.0

    signal_al = (cross_major_up or cross_spanb_up) and vol_ok and line_gap_ok
    signal_sat = (cross_major_dn or cross_spanb_dn) and vol_ok and line_gap_ok

    results = []

    if signal_al:
        hedef = cur_close + cur_atr * 2
        results.append({
            "direction": "AL",
            "type": "KIRILIM_AL",
            "kirilim": "SMA100 (Major)" if cross_major_up else "Span B (Sari)",
            "price": cur_close,
            "major": round(cur_major, 8),
            "spanb": round(cur_spanb, 8),
            "dist_major": round(dist_major, 2),
            "dist_spanb": round(dist_spanb, 2),
            "hedef": round(hedef, 8),
            "beklenti": round((hedef - cur_close) / cur_close * 100, 2),
            "vol": round(cur_vol, 2),
            "vol_ratio": vol_ratio,
            "line_gap": round(line_gap_pct, 2),
        })

    if signal_sat:
        hedef = cur_close - cur_atr * 2
        results.append({
            "direction": "SAT",
            "type": "KIRILIM_SAT",
            "kirilim": "SMA100 (Major)" if cross_major_dn else "Span B (Sari)",
            "price": cur_close,
            "major": round(cur_major, 8),
            "spanb": round(cur_spanb, 8),
            "dist_major": round(dist_major, 2),
            "dist_spanb": round(dist_spanb, 2),
            "hedef": round(hedef, 8),
            "beklenti": round((cur_close - hedef) / cur_close * 100, 2),
            "vol": round(cur_vol, 2),
            "vol_ratio": vol_ratio,
            "line_gap": round(line_gap_pct, 2),
        })

    return results

# ============================================================
# TELEGRAM
# ============================================================
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
    if yon == "AL":
        bas = "\U0001F7E2 <b>AL SINYALI</b> \U0001F4C8"
    else:
        bas = "\U0001F534 <b>SAT SINYALI</b> \U0001F4C9"

    coin = symbol.replace("USDT", "/USDT")

    return (
        f"{bas}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001F4CD <b>{coin}</b>\n"
        f"\u23F1 Zaman Dilimi: <b>{TIMEFRAME}</b>\n"
        f"\U0001F3AF Kirilim: <b>{sig.get('kirilim', '-')}</b>\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001F4B2 Fiyat: <b>{sig['price']}</b>\n"
        f"\U0001F3C1 Hedef: <b>{sig['hedef']}</b>  (%{sig['beklenti']})\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001F4CA Hacim: <b>{sig['vol_ratio']}x</b> (ortalama ustu)\n"
        f"\U0001F4CF Cizgi Araligi: <b>%{sig.get('line_gap', 0)}</b> (SMA100 \u2194 Span B)\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001F551 {datetime.now().strftime('%H:%M:%S  %d/%m/%Y')}"
    )


def should_send(symbol: str, sig_type: str) -> bool:
    key = f"{symbol}_{sig_type}"
    now = time.time()
    if key in sent_signals and (now - sent_signals[key]) < SIGNAL_COOLDOWN:
        return False
    sent_signals[key] = now
    return True

# ============================================================
# ANA TARAMA DONGUSU
# ============================================================
def run_scan():
    log.info(f"Tarama basladi TF:{TIMEFRAME} VolMult:{VOL_MULT}x Max:{MAX_COINS}")

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
            limit = max(MAJOR_LEN, SPANB_LEN) + 20
            df = get_klines(symbol, TIMEFRAME, limit=limit)
            sigs = check_signal(df, symbol)

            for sig in sigs:
                if should_send(symbol, sig["type"]):
                    msg = format_message(symbol, sig)
                    if send_telegram(msg):
                        log.info(f"OK {symbol} {sig['type']} vol {sig['vol_ratio']}x")
                        found += 1
                    time.sleep(0.3)

            time.sleep(0.1)

        except Exception as e:
            log.error(f"{symbol} hata: {e}")
            continue

        if (idx + 1) % 50 == 0:
            log.info(f"[{idx+1}/{total}] tarandi {found} sinyal")

    log.info(f"Tarama tamamlandi {found} sinyal gonderildi")


def main():
    log.info("=" * 55)
    log.info("MAJOR KIRILIM BOT baslatildi")
    log.info(f"  Strateji : SMA{MAJOR_LEN} / SpanB({SPANB_LEN}) kirilim + hacim {VOL_MULT}x")
    log.info(f"  Zaman    : {TIMEFRAME}")
    log.info(f"  Aralik   : her {SCAN_INTERVAL} saniye")
    log.info(f"  Max coin : {MAX_COINS}")
    log.info("=" * 55)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"MAJOR KIRILIM BOT BASLADI\n"
        f"Strateji: SMA{MAJOR_LEN} / SpanB({SPANB_LEN}) kirilim + hacim >= {VOL_MULT}x\n"
        f"Zaman dilimi: {TIMEFRAME}\n"
        f"Tarama araligi: her {SCAN_INTERVAL//60} dakika\n"
        f"Max coin: {MAX_COINS}\n"
        f"{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"Dongu hatasi: {e}")
            send_telegram(f"Bot Hatasi: {e}")

        log.info(f"Sonraki tarama {SCAN_INTERVAL} saniye sonra...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
