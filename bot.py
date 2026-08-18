# -*- coding: utf-8 -*-
"""
5DK SPIKE + TAKER BUY/SELL ONAY SCANNER (LONG/SHORT)

MANTIK:
1) Coin'de son birkac kapanmis 5dk mumda en az MIN_SPIKE_BODY_PCT (%5) govdeli
   YESIL bir "spike" mumu aranir.
2) Spike'tan sonraki en fazla MAX_CONFIRM_BARS (3) mumda, taker buy/sell hacim
   oranina bakilarak yon belirlenir:
     - Taker BUY baskin kalirsa (buy_ratio >= LONG_TAKER_BUY_RATIO)
       -> LONG sinyali (spike gercek, alim devam ediyor)
     - Taker SELL baskin olursa (sell_ratio >= SHORT_TAKER_SELL_RATIO)
       -> SHORT sinyali (spike tuzak, tersine donuyor / satis yiyor)
3) Onay penceresi (MAX_CONFIRM_BARS) icinde net bir yon olusmazsa sinyal
   uretilmez, bir sonraki taramada ayni spike icin tekrar denenir (pencere
   dolana kadar).

Taker buy hacmi, Binance kline API'sinin dogal alani (taker_buy_base_asset_volume)
oldugu icin ayri bir istege gerek yok.

SADECE KAPANMIS mumlar kullanilir (repaint yok).
"""
import os
import time
import logging
from datetime import datetime
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd

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

# --- SPIKE (KIRILIM MUMU) AYARLARI ---
MIN_SPIKE_BODY_PCT = float(os.getenv("MIN_SPIKE_BODY_PCT", "5.0"))   # spike mumunun govdesi en az % kac olmali
MAX_CONFIRM_BARS = int(os.getenv("MAX_CONFIRM_BARS", "3"))           # spike'tan sonra en fazla kac mum icinde onay aranir

# --- TAKER BUY/SELL ONAY ESIKLERI ---
LONG_TAKER_BUY_RATIO = float(os.getenv("LONG_TAKER_BUY_RATIO", "0.55"))    # onay penceresinde taker buy orani >= bu ise LONG
SHORT_TAKER_SELL_RATIO = float(os.getenv("SHORT_TAKER_SELL_RATIO", "0.55"))  # onay penceresinde taker sell orani >= bu ise SHORT

# --- LIKIDITE FILTRESI ---
USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))  # 5 milyon USDT

# --- SINYAL SAYISI SINIRI ---
# Her taramada sadece en net (en guclu taker imbalance) MAX_SIGNALS_PER_SCAN
# kadar sinyal gonderilir. 0 = sinir yok.
MAX_SIGNALS_PER_SCAN = int(os.getenv("MAX_SIGNALS_PER_SCAN", "5"))

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}

# Spike aramasi + onay penceresi icin yeterli mum + pay
KLINES_LIMIT = MAX_CONFIRM_BARS + 10


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
        # Binance kline alanlari: [open_time, open, high, low, close, volume, close_time,
        #   quote_asset_volume, trades, taker_buy_base_asset_volume,
        #   taker_buy_quote_asset_volume, ignore]
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume", "close_time",
            "qav", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
        for c in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
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
# SINYAL: SPIKE + TAKER BUY/SELL ONAYI
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    direction: str          # "LONG" veya "SHORT"
    price: float
    spike_body_pct: float
    bars_since_spike: int
    taker_buy_ratio: float  # onay penceresindeki taker buy orani (0-1)
    bar_time: str


def analyze_symbol(symbol, quote_volumes=None):
    if USE_LIQUIDITY_FILTER:
        qv = (quote_volumes or {}).get(symbol, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return None

    df = get_klines_closed(symbol, TIMEFRAME, limit=KLINES_LIMIT)
    if df is None or len(df) < MAX_CONFIRM_BARS + 3:
        return None

    df["body_pct"] = (df["close"] - df["open"]) / df["open"] * 100
    df["is_spike"] = (df["body_pct"] >= MIN_SPIKE_BODY_PCT) & (df["close"] > df["open"])

    n = len(df)
    last_idx = n - 1

    # --- Onay penceresi icindeki en son spike'i bul (en yakin/en gecerli) ---
    spike_idx = None
    for j in range(last_idx - 1, max(last_idx - 1 - MAX_CONFIRM_BARS, -1), -1):
        if bool(df["is_spike"].iloc[j]):
            spike_idx = j
            break

    if spike_idx is None:
        return None  # onay penceresinde spike yok

    bars_since_spike = last_idx - spike_idx
    if bars_since_spike < 1 or bars_since_spike > MAX_CONFIRM_BARS:
        return None  # spike ya cok yeni (henuz onay mumu yok) ya da pencere disi

    # --- Onay penceresi: spike'tan sonraki mumlar (spike_idx+1 .. last_idx) ---
    confirm = df.iloc[spike_idx + 1: last_idx + 1]
    total_volume = float(confirm["volume"].sum())
    if total_volume <= 0:
        return None

    total_taker_buy = float(confirm["taker_buy_base"].sum())
    taker_buy_ratio = total_taker_buy / total_volume
    taker_sell_ratio = 1.0 - taker_buy_ratio

    current = df.iloc[last_idx]
    spike = df.iloc[spike_idx]

    direction = None
    if taker_buy_ratio >= LONG_TAKER_BUY_RATIO:
        direction = "LONG"
    elif taker_sell_ratio >= SHORT_TAKER_SELL_RATIO:
        direction = "SHORT"

    if direction is None:
        return None  # henuz net bir yon olusmadi, sonraki taramada tekrar denenir

    return Signal(
        symbol=symbol,
        direction=direction,
        price=float(current["close"]),
        spike_body_pct=float(spike["body_pct"]),
        bars_since_spike=bars_since_spike,
        taker_buy_ratio=taker_buy_ratio,
        bar_time=str(current["open_time"]),
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
    if signal.direction == "LONG":
        header = "🟢 <b>SPIKE ONAYLANDI (LONG)</b>"
        ratio_line = f"📈 <b>Taker Buy Oranı:</b> %{signal.taker_buy_ratio*100:.1f} (alım baskın, spike devam ediyor)"
    else:
        header = "🔴 <b>SPIKE TERSİNE DÖNDÜ (SHORT)</b>"
        ratio_line = f"📉 <b>Taker Sell Oranı:</b> %{(1-signal.taker_buy_ratio)*100:.1f} (satış baskın, spike tuzak)"

    lines = [
        header,
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"🕯️ <b>Spike Mum Gövdesi:</b> %{signal.spike_body_pct:.2f}",
        f"⏳ <b>Spike'tan Sonra:</b> {signal.bars_since_spike} mum",
        ratio_line,
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
              f"Min spike govde: %{MIN_SPIKE_BODY_PCT} | Onay penceresi: {MAX_CONFIRM_BARS} mum | "
              f"LONG esik: %{LONG_TAKER_BUY_RATIO*100:.0f} taker buy | SHORT esik: %{SHORT_TAKER_SELL_RATIO*100:.0f} taker sell")

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

    # --- En net (en guclu taker imbalance) sinyalleri one al, MAX_SIGNALS_PER_SCAN ile sinirla ---
    found.sort(key=lambda s: abs(s.taker_buy_ratio - 0.5), reverse=True)
    if MAX_SIGNALS_PER_SCAN > 0 and len(found) > MAX_SIGNALS_PER_SCAN:
        log.info(f"{len(found)} sinyal bulundu, en net {MAX_SIGNALS_PER_SCAN} tanesi gonderiliyor (digerleri elendi)")
        found = found[:MAX_SIGNALS_PER_SCAN]

    for signal in found:
        try:
            msg = format_signal_message(signal)
            if send_telegram(msg):
                log.info(f"SINYAL GONDERILDI: {signal.symbol} {signal.direction} "
                          f"taker_buy_ratio={signal.taker_buy_ratio:.2f} bars={signal.bars_since_spike}")
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
    log.info("5DK SPIKE + TAKER BUY/SELL ONAY SCANNER baslatildi")
    log.info(f"Max coin         : {MAX_COINS}")
    log.info(f"Workers          : {MAX_WORKERS}")
    log.info(f"TF               : {TIMEFRAME}")
    log.info(f"Min spike govde  : %{MIN_SPIKE_BODY_PCT}")
    log.info(f"Onay penceresi   : {MAX_CONFIRM_BARS} mum")
    log.info(f"LONG esik        : taker buy orani >= %{LONG_TAKER_BUY_RATIO*100:.0f}")
    log.info(f"SHORT esik       : taker sell orani >= %{SHORT_TAKER_SELL_RATIO*100:.0f}")
    log.info(f"Likidite filtre  : {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"Max sinyal/tarama: {MAX_SIGNALS_PER_SCAN if MAX_SIGNALS_PER_SCAN > 0 else 'sinirsiz'}")
    log.info(f"Tarama araligi   : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown         : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 5DK SPIKE + TAKER BUY/SELL ONAY SCANNER BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"🕯️ Min spike gövde: %{MIN_SPIKE_BODY_PCT}\n"
        f"⏳ Onay penceresi: {MAX_CONFIRM_BARS} mum\n"
        f"🟢 LONG eşik: taker buy >= %{LONG_TAKER_BUY_RATIO*100:.0f}\n"
        f"🔴 SHORT eşik: taker sell >= %{SHORT_TAKER_SELL_RATIO*100:.0f}\n"
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
