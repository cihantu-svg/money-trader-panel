# -*- coding: utf-8 -*-
"""
SPIKE TOUCH BOT
Strateji: Yatay direnç/destek çizgisi + Min 5 fitil(spike) teması + Gövde kırılımı
Zaman: 15m | Borsa: Binance Futures (USDT-M)
"""
import os
import time
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

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
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SEC", "180"))  # 3 dk
TIMEFRAME = os.environ.get("SCAN_TIMEFRAME", "15m")
MAX_COINS = int(os.environ.get("MAX_COINS", "600"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", "5000000"))

# Strateji parametreleri
LEVEL_LOOKBACK = int(os.environ.get("LEVEL_LOOKBACK", "100"))      # Kaç mum geriye bakılacak
LEVEL_TOLERANCE_PCT = float(os.environ.get("LEVEL_TOLERANCE_PCT", "0.002"))  # %0.2 tolerans
MIN_TOUCHES = int(os.environ.get("MIN_TOUCHES", "5"))              # Min fitil teması
REQUIRE_BODY = os.environ.get("REQUIRE_BODY", "true").lower() == "true"
USE_VOLUME_FILTER = os.environ.get("USE_VOLUME_FILTER", "true").lower() == "true"
VOL_PERIOD = int(os.environ.get("VOL_PERIOD", "20"))
MIN_VOL_MULT = float(os.environ.get("MIN_VOL_MULT", "1.0"))
SIGNAL_COOLDOWN = int(os.environ.get("SIGNAL_COOLDOWN", "1800"))   # 30 dk

# Paralel tarama
SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "10"))
REQUESTS_PER_SEC = float(os.environ.get("REQUESTS_PER_SEC", "8"))
TELEGRAM_MSGS_PER_SEC = float(os.environ.get("TELEGRAM_MSGS_PER_SEC", "1.0"))

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SpikeTouchBot/1.0"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=SCAN_WORKERS, pool_maxsize=SCAN_WORKERS * 2)
SESSION.mount("https://", _adapter)

# Sinyal takip
sent_signals: dict = {}

# ============================================================
# COIN BAŞINA DURUM (Her coin için aktif direnç/destek seviyesi)
# ============================================================
coin_states: dict = {}

def get_coin_state(symbol: str):
    if symbol not in coin_states:
        coin_states[symbol] = {
            "resistance_level": None,   # Aktif direnç çizgisi
            "support_level": None,      # Aktif destek çizgisi
            "trend": 0,                 # 1=Long, -1=Short, 0=Nötr
        }
    return coin_states[symbol]

# Rate limiter
_rate_lock = threading.Lock()
_next_slot = [0.0]

def _rate_limit_wait():
    with _rate_lock:
        now = time.time()
        wait = _next_slot[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        _next_slot[0] = max(now, _next_slot[0]) + 1.0 / REQUESTS_PER_SEC

_tg_lock = threading.Lock()
_tg_next_slot = [0.0]

def _telegram_rate_wait():
    with _tg_lock:
        now = time.time()
        wait = _tg_next_slot[0] - now
        if wait > 0:
            time.sleep(wait)
            now = time.time()
        _tg_next_slot[0] = max(now, _tg_next_slot[0]) + 1.0 / TELEGRAM_MSGS_PER_SEC

# ============================================================
# VERİ ÇEKME
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
    _rate_limit_wait()
    try:
        r = SESSION.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=15, verify=False
        )
        if r.status_code in (429, 418):
            log.warning(f"Rate limit uyarisi ({r.status_code}) {symbol}")
            return None
        if r.status_code != 200:
            return None
        data = r.json()
        if not isinstance(data, list) or len(data) < 50:
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
# STRATEJI - SEVIYE BULMA & TEMAS SAYMA
# ============================================================
def find_level(df: pd.DataFrame, side: str = "resistance", lookback: int = 100,
               tolerance_pct: float = 0.002, min_touches: int = 5):
    """
    Son 'lookback' mumda yatay direnç/destek seviyesi bulur.
    side='resistance' -> High'lar, side='support' -> Low'lar
    
    Donus: (level, touches) veya (None, 0)
    """
    if len(df) < lookback + 10:
        return None, 0

    sub = df.iloc[-lookback:].copy()
    
    if side == "resistance":
        prices = sub["High"].values
    else:
        prices = sub["Low"].values
    
    prices = [p for p in prices if p > 0]
    if len(prices) < min_touches:
        return None, 0
    
    # Basit clustering: sirala, yakin fiyatlari grupla
    sorted_prices = sorted(prices)
    clusters = []
    current = [sorted_prices[0]]
    
    for p in sorted_prices[1:]:
        ref = current[0] if current[0] != 0 else 0.0001
        if abs(p - ref) / ref <= tolerance_pct:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)
    
    best_level = None
    best_touches = 0
    
    for cluster in clusters:
        if len(cluster) < min_touches:
            continue
            
        level = sum(cluster) / len(cluster)
        touches = 0
        
        for idx in range(len(sub)):
            row = sub.iloc[idx]
            
            if side == "resistance":
                wick_tip = row["High"]
                body_top = max(row["Open"], row["Close"])
                # Fitil ucu seviyeye yakin mi?
                near = abs(wick_tip - level) / level <= tolerance_pct if level > 0 else False
                # Govde seviyenin ALTINDA mi? (fitil var, govde asmamis)
                body_below = body_top <= level * (1 + tolerance_pct * 0.3)
                
                if near and body_below:
                    touches += 1
                    
            else:  # support
                wick_tip = row["Low"]
                body_bottom = min(row["Open"], row["Close"])
                near = abs(wick_tip - level) / level <= tolerance_pct if level > 0 else False
                # Govde seviyenin USTUNDE mi?
                body_above = body_bottom >= level * (1 - tolerance_pct * 0.3)
                
                if near and body_above:
                    touches += 1
        
        if touches >= min_touches and touches > best_touches:
            best_level = level
            best_touches = touches
    
    return best_level, best_touches


def count_touches(df: pd.DataFrame, level: float, side: str, lookback: int, tolerance_pct: float):
    """Belirli bir seviye icin fitil temas sayisini hesaplar"""
    if len(df) < lookback or level <= 0:
        return 0
    sub = df.iloc[-lookback:]
    touches = 0
    for idx in range(len(sub)):
        row = sub.iloc[idx]
        if side == "resistance":
            near = abs(row["High"] - level) / level <= tolerance_pct
            below = max(row["Open"], row["Close"]) <= level * (1 + tolerance_pct * 0.3)
            if near and below:
                touches += 1
        else:
            near = abs(row["Low"] - level) / level <= tolerance_pct
            above = min(row["Open"], row["Close"]) >= level * (1 - tolerance_pct * 0.3)
            if near and above:
                touches += 1
    return touches


def check_signal(df: pd.DataFrame, symbol: str) -> list:
    """
    Strateji:
    1. Son N mumda en cok fitil temasi olan yatay seviyeyi bul
    2. Min 5 temas varsa seviyeyi "aktif" olarak kaydet
    3. Son kapanmis mumda GOVDE ile kirilim varsa sinyal ver
    4. Kirilim sonrasi seviyeyi resetle
    """
    if df is None or len(df) < LEVEL_LOOKBACK + 20:
        return []

    state = get_coin_state(symbol)
    i = -2  # Son kapanmis mum (repaint yok)

    def safe(s, idx, default=0.0):
        try:
            v = s.iloc[idx]
            return float(v) if not pd.isna(v) else default
        except Exception:
            return default

    cur_close = safe(df["Close"], i)
    cur_open = safe(df["Open"], i)
    prev_close = safe(df["Close"], i - 1)
    cur_vol = safe(df["Volume"], i)

    if cur_close <= 0 or cur_open <= 0:
        return []

    results = []

    # Hacim filtresi
    vol_sma = df["Volume"].rolling(window=VOL_PERIOD).mean().iloc[i]
    volume_ok = not USE_VOLUME_FILTER or (vol_sma > 0 and cur_vol >= vol_sma * MIN_VOL_MULT)

    # ============================================================
    # LONG - DIRENC KIRILIMI
    # ============================================================
    res_level, _ = find_level(df, "resistance", LEVEL_LOOKBACK, LEVEL_TOLERANCE_PCT, MIN_TOUCHES)
    
    if res_level is not None:
        # Eski direncle ayni zone'da mi? (sallanmaları yumusat)
        if state["resistance_level"] is not None and state["resistance_level"] > 0:
            if abs(res_level - state["resistance_level"]) / state["resistance_level"] <= LEVEL_TOLERANCE_PCT:
                state["resistance_level"] = (state["resistance_level"] + res_level) / 2
            else:
                state["resistance_level"] = res_level
        else:
            state["resistance_level"] = res_level

    if state["resistance_level"] is not None:
        # Govde kirilimi kontrolu
        if REQUIRE_BODY:
            body_break = min(cur_close, cur_open) > state["resistance_level"]
        else:
            body_break = cur_close > state["resistance_level"]
        
        # Onceki mum direncin altinda/bitisiginde miydi?
        prev_below = prev_close <= state["resistance_level"]
        
        # Mevcut seviye icin guncel temas sayisi
        actual_touches = count_touches(df, state["resistance_level"], "resistance", 
                                       LEVEL_LOOKBACK, LEVEL_TOLERANCE_PCT)
        
        if (body_break and prev_below and actual_touches >= MIN_TOUCHES and 
            volume_ok and state["trend"] != 1):
            
            results.append({
                "direction": "AL",
                "type": "SPIKE_TOUCH_AL",
                "price": cur_close,
                "level": round(state["resistance_level"], 8),
                "touches": actual_touches,
                "volume_ratio": round(cur_vol / vol_sma, 2) if vol_sma > 0 else 0,
            })
            state["trend"] = 1
            log.info(f"{symbol} AL SINYALI! Direnc {actual_touches}x temas sonrasi kirildi: "
                     f"{cur_close} > {state['resistance_level']}")
            state["resistance_level"] = None  # RESET

    # ============================================================
    # SHORT - DESTEK KIRILIMI
    # ============================================================
    sup_level, _ = find_level(df, "support", LEVEL_LOOKBACK, LEVEL_TOLERANCE_PCT, MIN_TOUCHES)
    
    if sup_level is not None:
        if state["support_level"] is not None and state["support_level"] > 0:
            if abs(sup_level - state["support_level"]) / state["support_level"] <= LEVEL_TOLERANCE_PCT:
                state["support_level"] = (state["support_level"] + sup_level) / 2
            else:
                state["support_level"] = sup_level
        else:
            state["support_level"] = sup_level

    if state["support_level"] is not None:
        if REQUIRE_BODY:
            body_break = max(cur_close, cur_open) < state["support_level"]
        else:
            body_break = cur_close < state["support_level"]
        
        prev_above = prev_close >= state["support_level"]
        
        actual_touches = count_touches(df, state["support_level"], "support",
                                       LEVEL_LOOKBACK, LEVEL_TOLERANCE_PCT)
        
        if (body_break and prev_above and actual_touches >= MIN_TOUCHES and
            volume_ok and state["trend"] != -1):
            
            results.append({
                "direction": "SAT",
                "type": "SPIKE_TOUCH_SAT",
                "price": cur_close,
                "level": round(state["support_level"], 8),
                "touches": actual_touches,
                "volume_ratio": round(cur_vol / vol_sma, 2) if vol_sma > 0 else 0,
            })
            state["trend"] = -1
            log.info(f"{symbol} SAT SINYALI! Destek {actual_touches}x temas sonrasi kirildi: "
                     f"{cur_close} < {state['support_level']}")
            state["support_level"] = None  # RESET

    return results

# ============================================================
# TELEGRAM & FORMAT
# ============================================================
def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram token veya chat_id eksik!")
        return False
    _telegram_rate_wait()
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
        bas = "AL SINYALI (DIRENC KIRILIMI)"
        emoji = "🟢"
    else:
        bas = "SAT SINYALI (DESTEK KIRILIMI)"
        emoji = "🔴"

    coin = symbol.replace("USDT", "/USDT")

    lines = [
        f"{emoji} {bas}",
        "-" * 24,
        f"Coin: {coin}",
        f"Zaman: {TIMEFRAME}",
        f"Strateji: {sig['touches']}x Fitil Temas + Govde Kırılımı",
        "-" * 24,
        f"Fiyat: {sig['price']}",
        f"Seviye: {sig['level']}",
        f"Temas Sayisi: {sig['touches']}",
        f"Hacim Orani: {sig['volume_ratio']}x",
        "-" * 24,
        datetime.now().strftime('%H:%M:%S %d/%m/%Y')
    ]
    return "\n".join(lines)

def should_send(symbol: str, sig_type: str) -> bool:
    key = f"{symbol}_{sig_type}"
    now = time.time()
    if key in sent_signals and (now - sent_signals[key]) < SIGNAL_COOLDOWN:
        return False
    sent_signals[key] = now
    return True

# ============================================================
# TARAMA & MAIN
# ============================================================
def _scan_one(coin):
    symbol = coin["symbol"]
    try:
        limit = max(LEVEL_LOOKBACK + 50, 150)
        df = get_klines(symbol, TIMEFRAME, limit=limit)
        sigs = check_signal(df, symbol)
        return symbol, sigs
    except Exception as e:
        log.error(f"{symbol} hata: {e}")
        return symbol, []

def run_scan():
    log.info(f"Tarama basladi TF:{TIMEFRAME} Lookback:{LEVEL_LOOKBACK} "
             f"MinTouches:{MIN_TOUCHES} Tol:{LEVEL_TOLERANCE_PCT}")

    symbols = get_symbols(min_volume=MIN_VOLUME)
    if not symbols:
        log.error("Coin listesi alinamadi!")
        return

    symbols = symbols[:MAX_COINS]
    total = len(symbols)
    found = 0
    scanned = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(_scan_one, coin): coin["symbol"] for coin in symbols}

        for future in as_completed(futures):
            symbol = futures[future]
            scanned += 1
            try:
                _, sigs = future.result()
            except Exception as e:
                log.error(f"{symbol} beklenmeyen hata: {e}")
                continue

            for sig in sigs:
                if should_send(symbol, sig["type"]):
                    msg = format_message(symbol, sig)
                    if send_telegram(msg):
                        log.info(f"OK {symbol} {sig['type']} seviye:{sig['level']} "
                                 f"temas:{sig['touches']}")
                        found += 1

            if scanned % 50 == 0:
                log.info(f"[{scanned}/{total}] tarandi {found} sinyal")

    elapsed = time.time() - t0
    log.info(f"Tarama tamamlandi {found} sinyal gonderildi ({elapsed:.1f}sn)")

def main():
    log.info("=" * 60)
    log.info("SPIKE TOUCH BOT baslatildi")
    log.info(f"  Strateji : Yatay Seviye + {MIN_TOUCHES}x Fitil Temas + Govde Kirilimi")
    log.info(f"  Zaman    : {TIMEFRAME}")
    log.info(f"  Lookback : {LEVEL_LOOKBACK} mum")
    log.info(f"  Tolerans : {LEVEL_TOLERANCE_PCT*100}%")
    log.info(f"  Min Vol  : {MIN_VOLUME} USDT")
    log.info(f"  Aralik   : her {SCAN_INTERVAL} saniye")
    log.info(f"  Max coin : {MAX_COINS}")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        f"SPIKE TOUCH BOT BASLADI\n"
        f"Strateji: Yatay Seviye + {MIN_TOUCHES}x Fitil Temas + Govde Kırılımı\n"
        f"Zaman: {TIMEFRAME} | Lookback: {LEVEL_LOOKBACK} | Tolerans: {LEVEL_TOLERANCE_PCT*100}%\n"
        f"Min Hacim: {MIN_VOLUME} USDT | Aralik: {SCAN_INTERVAL//60} dk\n"
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
