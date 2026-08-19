# -*- coding: utf-8 -*-
"""
5DK TAKER BUY/SELL BASKINLIGI + GUCLU MUM SCANNER (LONG/SHORT)

MANTIK:
Son KAPANMIS 5dk mumda iki sart ayni anda aranir:
  1) Taker buy veya sell baskinligi >= IMBALANCE_RATIO (%55)
  2) Mum govdesi ayni yonde en az MIN_BODY_PCT (%4)

  - Taker BUY >= %55 VE yesil govde >= +%4  -> LONG sinyali
  - Taker SELL >= %55 VE kirmizi govde <= -%4 -> SHORT sinyali

Uyumsuz durumlar (orn. %60 buy ama kirmizi mum) sinyal uretmez.

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
SIGNAL_COOLDOWN = int(os.getenv("SIGNAL_COOLDOWN", "1800"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "20"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "8"))

TIMEFRAME = os.getenv("TIMEFRAME", "5m")

# --- TAKER BASKINLIGI + MUM GOVDESI AYARLARI ---
IMBALANCE_RATIO = float(os.getenv("IMBALANCE_RATIO", "0.55"))   # taker buy veya sell orani >= bu olmali
MIN_BODY_PCT = float(os.getenv("MIN_BODY_PCT", "4.0"))          # mum govdesi min % bu olmali (yon ile uyumlu)

# --- LIKIDITE FILTRESI ---
USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "5000000"))  # 5 milyon USDT

# --- SINYAL SAYISI SINIRI ---
# Her taramada sadece en net (en guclu taker imbalance) MAX_SIGNALS_PER_SCAN
# kadar sinyal gonderilir. 0 = sinir yok.
MAX_SIGNALS_PER_SCAN = int(os.getenv("MAX_SIGNALS_PER_SCAN", "5"))

BINANCE_BASE = "https://fapi.binance.com"
last_signal = {}

# Tek muma bakildigi icin birkac mum yeterli
KLINES_LIMIT = 5


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
# SINYAL: TAKER BASKINLIGI + GUCLU MUM
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    direction: str          # "LONG" veya "SHORT"
    price: float
    body_pct: float         # mum govdesi % (isaretli: + yesil, - kirmizi)
    taker_buy_ratio: float  # mumdaki taker buy orani (0-1)
    bar_time: str


def analyze_symbol(symbol, quote_volumes=None):
    if USE_LIQUIDITY_FILTER:
        qv = (quote_volumes or {}).get(symbol, 0.0)
        if qv < MIN_QUOTE_VOLUME_24H:
            return None

    df = get_klines_closed(symbol, TIMEFRAME, limit=KLINES_LIMIT)
    if df is None or len(df) < 2:
        return None

    last = df.iloc[-1]                      # son KAPANMIS mum
    vol = float(last["volume"])
    if vol <= 0:
        return None

    body_pct = (float(last["close"]) - float(last["open"])) / float(last["open"]) * 100
    buy_ratio = float(last["taker_buy_base"]) / vol
    sell_ratio = 1.0 - buy_ratio

    # LONG: taker buy >= %55 VE yesil govde >= +%4
    if buy_ratio >= IMBALANCE_RATIO and body_pct >= MIN_BODY_PCT:
        direction = "LONG"
    # SHORT: taker sell >= %55 VE kirmizi govde <= -%4
    elif sell_ratio >= IMBALANCE_RATIO and body_pct <= -MIN_BODY_PCT:
        direction = "SHORT"
    else:
        return None

    return Signal(
        symbol=symbol,
        direction=direction,
        price=float(last["close"]),
        body_pct=body_pct,
        taker_buy_ratio=buy_ratio,
        bar_time=str(last["open_time"]),
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
        header = "🟢 <b>ALIM BASKINI + GUCLU MUM (LONG)</b>"
        ratio_line = f"📈 <b>Taker Buy Oranı:</b> %{signal.taker_buy_ratio*100:.1f} (alım baskın)"
    else:
        header = "🔴 <b>SATIS BASKINI + GUCLU MUM (SHORT)</b>"
        ratio_line = f"📉 <b>Taker Sell Oranı:</b> %{(1-signal.taker_buy_ratio)*100:.1f} (satış baskın)"

    lines = [
        header,
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Fiyat:</b> {signal.price:.6f}",
        f"🕯️ <b>Mum Gövdesi:</b> %{signal.body_pct:.2f}",
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
              f"Imbalance esik: %{IMBALANCE_RATIO*100:.0f} | Min govde: %{MIN_BODY_PCT}")

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
                          f"taker_buy_ratio={signal.taker_buy_ratio:.2f} body={signal.body_pct:.2f}%")
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
    log.info("5DK TAKER BUY/SELL BASKINLIGI + GUCLU MUM SCANNER baslatildi")
    log.info(f"Max coin         : {MAX_COINS}")
    log.info(f"Workers          : {MAX_WORKERS}")
    log.info(f"TF               : {TIMEFRAME}")
    log.info(f"Imbalance esik   : taker buy/sell orani >= %{IMBALANCE_RATIO*100:.0f}")
    log.info(f"Min mum govdesi  : %{MIN_BODY_PCT} (yon ile uyumlu)")
    log.info(f"Likidite filtre  : {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"Max sinyal/tarama: {MAX_SIGNALS_PER_SCAN if MAX_SIGNALS_PER_SCAN > 0 else 'sinirsiz'}")
    log.info(f"Tarama araligi   : {SCAN_INTERVAL} sn")
    log.info(f"Cooldown         : {SIGNAL_COOLDOWN} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 5DK TAKER BASKINLIGI + GUCLU MUM SCANNER BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 TF: {TIMEFRAME}\n"
        f"📊 Imbalance eşik: taker buy/sell >= %{IMBALANCE_RATIO*100:.0f}\n"
        f"🕯️ Min mum gövdesi: %{MIN_BODY_PCT}\n"
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
