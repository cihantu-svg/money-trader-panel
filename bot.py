# -*- coding: utf-8 -*-
"""
5DK HACIM + MOMENTUM SCANNER BOT
(MONEY TRADER - "Hacim + Momentum Yükselişi (AL)" sinyalinin Pine Script'ten
 birebir Python'a uyarlanmis hali)

SINYAL MANTIGI (Pine ile ayni):
- volRatio = volume / SMA(volume, 20)   -> hacim, 20 periyotluk ortalamaya orani
- Hacim sarti: volRatio >= MIN_VOLUME_RATIO  (varsayilan 1.07 -> ortalamanin en az %7 uzeri)
- rsiVal = RSI(close, 14)  (Wilder RSI, Pine'deki ta.rsi ile ayni yontem)
- Momentum sarti: RSI, 50 seviyesini YUKARI KESMIS olmali (crossover)
  -> onceki mumda RSI <= 50, mevcut mumda RSI > 50
- Coin'in son 24s USDT hacmi MIN_QUOTE_VOLUME_24H altindaysa sinyal uretilmez
- Kosullar saglaninca Telegram'a bildirim atar
- SADECE KAPANMIS mumlar kullanilir (repaint yok)
"""
import os
import time
import logging
from datetime import datetime
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "120"))
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "3600"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "8"))

TIMEFRAME = os.getenv("TIMEFRAME", "5m")

# --- HACIM AYARLARI (Pine: volSma20 = ta.sma(volume, 20)) ---
VOLUME_SMA_PERIOD = int(os.getenv("VOLUME_SMA_PERIOD", "20"))
MIN_VOLUME_INCREASE_PCT = float(os.getenv("MIN_VOLUME_INCREASE_PCT", "7.0"))  # ortalamadan en az % kac fazla (Pine'deki vol_mult_up'in karsiligi)
MIN_VOLUME_RATIO = 1.0 + (MIN_VOLUME_INCREASE_PCT / 100.0)  # ornek: %7 -> 1.07x

# --- MOMENTUM AYARLARI (Pine: rsiVal = ta.rsi(close, 14), crossover(rsiVal, 50)) ---
RSI_LEN = int(os.getenv("RSI_LEN", "14"))
RSI_LEVEL = float(os.getenv("RSI_LEVEL", "50"))

# --- MUM BOYU (GOVDE) SARTI ---
# Sadece hacim + RSI kesisimi zayif fiyat hareketlerinde de tetiklenebiliyordu.
# Bu yuzden mumun govdesi (fiyat hareketi) de en az MIN_CANDLE_BODY_PCT olmali.
MIN_CANDLE_BODY_PCT = float(os.getenv("MIN_CANDLE_BODY_PCT", "3.0"))

# --- LIKIDITE FILTRESI ---
USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))  # 5 milyon USDT

# --- SINYAL SAYISI SINIRI ---
# Esik ne olursa olsun, piyasa hareketliyse cok sinyal gelebilir.
# Bu yuzden her taramada SADECE en guclu (hacim orani en yuksek) MAX_SIGNALS_PER_SCAN
# kadar sinyal gonderilir, gerisi elenir. 0 = sinir yok (hepsini gonder).
MAX_SIGNALS_PER_SCAN = int(os.getenv("MAX_SIGNALS_PER_SCAN", "5"))

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}

# RSI icin yeterli isinma (warm-up) mumu + hacim SMA'si icin pay
KLINES_LIMIT = max(RSI_LEN, VOLUME_SMA_PERIOD) * 5 + 20


# ══════════════════════════════════════════════════════════════════
# BINANCE VERI CEKME
# ══════════════════════════════════════════════════════════════════
def get_symbols():
    try:
        session = requests.Session()
        r = session.get(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=10)
        data = r.json()
        syms = [s["symbol"] for s in data["symbols"]
                if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
        return syms[:MAX_COINS]
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_klines(symbol, interval, limit=200):
    try:
        session = requests.Session()
        r = session.get(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        raw = r.json()
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "tbv", "tqv", "ignore"])
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception:
        return None


def get_klines_closed(symbol, interval, limit=200):
    """Sadece KAPANMIS mumlari doner - repaint/titresim onlenir."""
    df = get_klines(symbol, interval, limit=limit + 1)
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


def get_all_24h_quote_volumes():
    """TUM sembollerin 24s USDT hacmini TEK istekte ceker."""
    try:
        session = requests.Session()
        r = session.get(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
        data = r.json()
        return {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}
    except Exception as e:
        log.error(f"get_all_24h_quote_volumes hata: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════
# INDIKATOR HESAPLAMALARI (Pine Script ile birebir ayni yontem)
# ══════════════════════════════════════════════════════════════════
def wilder_rsi(close: pd.Series, length: int) -> pd.Series:
    """
    Pine'in ta.rsi() fonksiyonuyla ayni sonucu veren Wilder RSI hesabi
    (RMA / Wilder's smoothing kullanir, basit EMA degil).
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)

    # Wilder's RMA: ilk deger SMA, sonrasi alpha=1/length ile ussel yumusatma
    avg_gain = gain.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(avg_loss != 0, 100.0)  # kayip sifirsa RSI 100
    return rsi


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["vol_sma"] = df["volume"].rolling(VOLUME_SMA_PERIOD).mean()
    df["vol_ratio"] = df["volume"] / df["vol_sma"]
    df["rsi"] = wilder_rsi(df["close"], RSI_LEN)
    return df


# ══════════════════════════════════════════════════════════════════
# SINYAL: HACIM + MOMENTUM (Pine: show_vol_mom_up)
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    price: float
    volume: float
    vol_sma: float
    vol_ratio: float
    rsi_prev: float
    rsi_now: float
    body_pct: float
    bar_time: str


def analyze_symbol(symbol, quote_volumes=None):
    if USE_LIQUIDITY_FILTER:
        qv = (quote_volumes or {}).get(symbol, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return None

    df = get_klines_closed(symbol, TIMEFRAME, limit=KLINES_LIMIT)
    if df is None or len(df) < max(RSI_LEN, VOLUME_SMA_PERIOD) + 5:
        return None

    df = compute_indicators(df)
    if df[["vol_ratio", "rsi"]].iloc[-2:].isna().any().any():
        return None  # isinma donemi bitmemis (yeterli veri yok)

    candidate = df.iloc[-1]
    prev = df.iloc[-2]

    vol_ratio = float(candidate["vol_ratio"])
    rsi_now = float(candidate["rsi"])
    rsi_prev = float(prev["rsi"])

    # --- Hacim sarti: volRatio >= MIN_VOLUME_RATIO ---
    if vol_ratio < MIN_VOLUME_RATIO:
        return None

    # --- Momentum sarti: RSI, 50 seviyesini YUKARI KESMIS olmali (crossover) ---
    rsi_crossed_up = (rsi_prev <= RSI_LEVEL) and (rsi_now > RSI_LEVEL)
    if not rsi_crossed_up:
        return None

    # --- Mum boyu sarti: mum yesil olmali ve govdesi en az MIN_CANDLE_BODY_PCT olmali ---
    open_now = float(candidate["open"])
    close_now = float(candidate["close"])
    body_pct = (close_now - open_now) / open_now * 100

    if body_pct < MIN_CANDLE_BODY_PCT:
        return None  # yesil degil veya govde cok kucuk

    return Signal(
        symbol=symbol,
        price=float(candidate["close"]),
        volume=float(candidate["volume"]),
        vol_sma=float(candidate["vol_sma"]),
        vol_ratio=vol_ratio,
        rsi_prev=rsi_prev,
        rsi_now=rsi_now,
        body_pct=body_pct,
        bar_time=str(candidate["open_time"]),
    )


# ══════════════════════════════════════════════════════════════════
# COOLDOWN
# ══════════════════════════════════════════════════════════════════
def should_send(symbol):
    now = time.time()
    if now - last_signal.get(symbol, 0) < SIGNAL_COOLDOWN:
        return False
    last_signal[symbol] = now
    return True


# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID eksik, konsola yaziliyor:\n" + text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram hata: {e}")
        return False


def format_signal_message(signal: Signal):
    coin = signal.symbol.replace("USDT", "/USDT")
    sep = "=" * 24
    vol_increase_pct = (signal.vol_ratio - 1.0) * 100
    lines = [
        "🟢 <b>HACIM + MOMENTUM YÜKSELİŞİ (AL)</b>",
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"📊 <b>Hacim / SMA{VOLUME_SMA_PERIOD} Oranı:</b> {signal.vol_ratio:.2f}x (%{vol_increase_pct:.2f} üzeri)",
        f"📈 <b>RSI{RSI_LEN}:</b> {signal.rsi_prev:.1f} → {signal.rsi_now:.1f} (50 yukarı kesişim ✅)",
        f"🕯️ <b>Mum Gövdesi:</b> %{signal.body_pct:.2f}",
        sep,
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# TARAMA DONGUSU (paralel, tum coinler)
# ══════════════════════════════════════════════════════════════════
def check_signal(symbol, quote_volumes=None):
    try:
        signal = analyze_symbol(symbol, quote_volumes=quote_volumes)
        if signal is None:
            return None, {"symbol": symbol, "status": "no_signal"}
        if not should_send(symbol):
            return None, {"symbol": symbol, "status": "cooldown"}
        return signal, {"symbol": symbol, "status": "signal"}
    except Exception as e:
        return None, {"symbol": symbol, "status": "error", "error": str(e)}


def run_scan_parallel():
    symbols = get_symbols()
    total = len(symbols)
    log.info(f"TARAMA BASLADI | Coin: {total} | Workers: {MAX_WORKERS} | TF: {TIMEFRAME} | "
              f"Min hacim orani: {MIN_VOLUME_RATIO:.2f}x (%{MIN_VOLUME_INCREASE_PCT}) | "
              f"RSI{RSI_LEN} 50 yukari kesisim")

    quote_volumes = get_all_24h_quote_volumes() if USE_LIQUIDITY_FILTER else {}

    stats = {"total": total, "signal": 0, "no_signal": 0, "cooldown": 0, "error": 0}
    found = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_signal, s, quote_volumes): s for s in symbols}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                signal, info = future.result()
                status = info["status"]
                stats[status] = stats.get(status, 0) + 1
                if status == "signal":
                    found.append(signal)
            except Exception as e:
                log.error(f"Future hata: {e}")
                stats["error"] += 1

            if completed % 100 == 0 or completed == total:
                log.info(f"[{completed}/{total}] Sinyal:{stats['signal']} NoSignal:{stats['no_signal']} Hata:{stats['error']}")

    # --- En guclu sinyalleri one al, MAX_SIGNALS_PER_SCAN ile sinirla ---
    found.sort(key=lambda s: s.vol_ratio, reverse=True)
    if MAX_SIGNALS_PER_SCAN > 0 and len(found) > MAX_SIGNALS_PER_SCAN:
        log.info(f"{len(found)} sinyal bulundu, en guclu {MAX_SIGNALS_PER_SCAN} tanesi gonderiliyor (digerleri elendi)")
        found = found[:MAX_SIGNALS_PER_SCAN]

    for signal in found:
        try:
            msg = format_signal_message(signal)
            if send_telegram(msg):
                log.info(f"SINYAL GONDERILDI: {signal.symbol} vol_ratio={signal.vol_ratio:.2f}x rsi={signal.rsi_now:.1f}")
            else:
                log.error(f"Telegram gonderilemedi: {signal.symbol}")
        except Exception as e:
            log.error(f"Gonderim hatasi {signal.symbol}: {e}")

    log.info(f"Tarama tamamlandi | {stats['signal']} sinyal | {stats}")
    return stats["signal"]


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info("5DK HACIM + MOMENTUM SCANNER (Pine uyumlu) baslatildi")
    log.info(f"Max coin        : {MAX_COINS}")
    log.info(f"Workers         : {MAX_WORKERS}")
    log.info(f"TF              : {TIMEFRAME}")
    log.info(f"Hacim SMA       : {VOLUME_SMA_PERIOD} mum")
    log.info(f"Min hacim orani : {MIN_VOLUME_RATIO:.2f}x (%{MIN_VOLUME_INCREASE_PCT} uzeri)")
    log.info(f"RSI             : {RSI_LEN} periyot, seviye {RSI_LEVEL} (yukari kesisim)")
    log.info(f"Min mum govdesi : %{MIN_CANDLE_BODY_PCT}")
    log.info(f"Likidite filtre : {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"Max sinyal/tarama: {MAX_SIGNALS_PER_SCAN if MAX_SIGNALS_PER_SCAN > 0 else 'sinirsiz'}")
    log.info(f"Tarama araligi  : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown        : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 5DK HACIM + MOMENTUM SCANNER BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📊 Hacim SMA: {VOLUME_SMA_PERIOD} mum\n"
        f"🚀 Min hacim oranı: {MIN_VOLUME_RATIO:.2f}x (%{MIN_VOLUME_INCREASE_PCT} üzeri)\n"
        f"📈 Momentum: RSI{RSI_LEN} 50 yukarı kesişim\n"
        f"🕯️ Min mum gövdesi: %{MIN_CANDLE_BODY_PCT}\n"
        f"💧 Min likidite: {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT\n"
        f"⏰ Cooldown: {SIGNAL_COOLDOWN}sn\n"
        f"⚡ Workers: {MAX_WORKERS}"
    )

    while True:
        try:
            run_scan_parallel()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")

        log.info(f"{SCAN_INTERVAL}sn bekleniyor...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
