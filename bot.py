# -*- coding: utf-8 -*-
"""
15dk %7+ MUM + 1dk PULLBACK ONAYI SCANNER (REPAINT'SIZ, CANLI)

Backtest sonucu (60 gunluk veri, 76 sinyal): %5 hit-rate ~%84.2,
ort. lehte hareket ~%25.4, gunde ortalama ~4.2 sinyal.

MANTIK (iki fazli tarama):

FAZ A - OLAY TESPITI (15dk, TUM coinlerde her taramada):
  Son KAPANMIS 15dk mumun govdesi (open->close) >= %7 (LONG) veya
  <= -%7 (SHORT) ise, bu YENI bir "olay" olarak kaydedilir (ayni mum
  tekrar tekrar olay olarak islenmez - sembol bazinda son islenen mum
  zamani takip edilir). Sembolde zaten BEKLEYEN bir olay varsa, o
  cozulene kadar (onaylanana ya da suresi dolana kadar) yeni olay
  kaydedilmez.

FAZ B - PULLBACK ONAYI ARAMA (1dk, SADECE bekleyen olayi olan coinlerde):
  Olay sonrasi PULLBACK_SEARCH_CANDLES (45) mum icinde, gonderilen Pine
  indikatorunun (@BarsStallone S/R) REPAINT KAYNAGI (request.security +
  lookahead_on) CIKARILMIS, sadece kapanmis 1dk barlarindan hesaplanan
  RSI(9)+CMO(HMA5/12) tetikleyicisi aranir:
    LONG onayi (sup) : RSI(9)<25 VE CMO>50 VE en az bir dusuk pivot var
    SHORT onayi (res): RSI(9)>75 VE CMO<-50 VE en az bir yuksek pivot var
  45 mum icinde onay gelmezse olay IPTAL edilir (sinyal yok).
  Onay gelirse Telegram'a bildirim gonderilir.

SADECE KAPANMIS mumlar kullanilir (repaint yok).
"""
import os
import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))       # onay zamanlamasi hassas, sik taranmali
MAX_COINS = int(os.getenv("MAX_COINS", "600"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "15"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))

EVENT_TF = os.getenv("EVENT_TF", "15m")
CONFIRM_TF = os.getenv("CONFIRM_TF", "1m")

BODY_PCT_THRESHOLD = float(os.getenv("BODY_PCT_THRESHOLD", "7.0"))
PULLBACK_SEARCH_CANDLES = int(os.getenv("PULLBACK_SEARCH_CANDLES", "45"))
WARMUP_MINUTES = int(os.getenv("WARMUP_MINUTES", "90"))

RSI_PERIOD = int(os.getenv("RSI_PERIOD", "9"))
CMO_HMA_FAST = int(os.getenv("CMO_HMA_FAST", "5"))
CMO_HMA_SLOW = int(os.getenv("CMO_HMA_SLOW", "12"))
PIVOT_LEFT_RIGHT = int(os.getenv("PIVOT_LEFT_RIGHT", "2"))

USE_LIQUIDITY_FILTER = os.getenv("USE_LIQUIDITY_FILTER", "true").lower() == "true"
MIN_QUOTE_VOLUME_24H = float(os.getenv("MIN_QUOTE_VOLUME_24H", "3000000"))

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("RETRY_BACKOFF_BASE", "0.5"))

BINANCE_BASE = "https://fapi.binance.com"

# ══════════════════════════════════════════════════════════════════
# DURUM (loop boyunca hafizada tutulur)
# ══════════════════════════════════════════════════════════════════
last_processed_event_time = {}   # symbol -> son islenen 15dk mumun open_time'i (tekrar islememek icin)
pending_events = {}              # symbol -> {"direction","event_close_time_ms","event_open_time_ms"}

# ══════════════════════════════════════════════════════════════════
# GLOBAL SESSION + RETRY
# ══════════════════════════════════════════════════════════════════
session = requests.Session()
session.headers.update({"Connection": "keep-alive"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=MAX_WORKERS + 10, pool_maxsize=MAX_WORKERS + 20)
session.mount("https://", _adapter)
session.mount("http://", _adapter)


def _request_with_retry(url, params=None, timeout=REQUEST_TIMEOUT):
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 418) or r.status_code >= 500:
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                retry_after = r.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                log.warning(f"HTTP {r.status_code}, {wait:.1f}sn sonra tekrar (deneme {attempt+1})")
                time.sleep(wait)
                last_exc = Exception(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            last_exc = e
            wait = RETRY_BACKOFF_BASE * (2 ** attempt)
            time.sleep(wait)
    if last_exc:
        raise last_exc
    raise Exception("Bilinmeyen istek hatasi")


# ══════════════════════════════════════════════════════════════════
# BINANCE VERI CEKME
# ══════════════════════════════════════════════════════════════════
def get_symbols():
    try:
        r = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=10)
        data = r.json()
        syms = [s["symbol"] for s in data["symbols"]
                if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]
        return syms[:MAX_COINS]
    except Exception as e:
        log.error(f"get_symbols hata: {e}")
        return []


def get_all_24h_quote_volumes():
    try:
        r = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
        data = r.json()
        return {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}
    except Exception as e:
        log.error(f"get_all_24h_quote_volumes hata: {e}")
        return {}


def _parse_klines(raw):
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype(np.int64)
    df["close_time"] = df["close_time"].astype(np.int64)
    return df.drop_duplicates(subset="open_time").reset_index(drop=True)


def get_klines(symbol, interval, limit=200):
    try:
        r = _request_with_retry(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=REQUEST_TIMEOUT,
        )
        return _parse_klines(r.json())
    except Exception as e:
        log.debug(f"get_klines hata ({symbol}): {e}")
        return None


def get_klines_closed(symbol, interval, limit=200):
    """Sadece KAPANMIS mumlari doner - repaint/titresim onlenir."""
    df = get_klines(symbol, interval, limit=limit + 1)
    if df is None or len(df) < 2:
        return None
    return df.iloc[:-1].reset_index(drop=True)


def get_klines_window(symbol, interval, start_ms, end_ms):
    try:
        r = _request_with_retry(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "startTime": start_ms,
                    "endTime": end_ms, "limit": 1500},
            timeout=REQUEST_TIMEOUT,
        )
        raw = r.json()
        if not raw:
            return None
        return _parse_klines(raw)
    except Exception as e:
        log.debug(f"1dk pencere hatasi ({symbol}): {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# INDIKATOR HESAPLARI (repaint'siz)
# ══════════════════════════════════════════════════════════════════
def compute_rsi(close, period):
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def wma(series, length):
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def hma(series, length):
    half = max(1, int(length / 2))
    sqrt_len = max(1, int(round(np.sqrt(length))))
    wma_half = wma(series, half)
    wma_full = wma(series, length)
    diff = 2 * wma_half - wma_full
    return wma(diff, sqrt_len)


def causal_pivot_exists(high, low, left, right):
    """Orijinal Pine mantigi BIREBIR: klasik pivot-high/pivot-low (sol=sag),
    SADECE right bar sonra onaylanir (repaint yok). fixnan davranisi BIREBIR
    korundu: bir kez pivot olustu mu, 'var' bayragi KALICIDIR."""
    window = left + right + 1
    roll_max = high.rolling(window, center=True).max()
    roll_min = low.rolling(window, center=True).min()
    is_high_pivot_center = (high == roll_max)
    is_low_pivot_center = (low == roll_min)
    confirmed_high = is_high_pivot_center.shift(right).fillna(False)
    confirmed_low = is_low_pivot_center.shift(right).fillna(False)
    hpivot_exists = (confirmed_high.cumsum() > 0)
    lpivot_exists = (confirmed_low.cumsum() > 0)
    return hpivot_exists, lpivot_exists


def compute_sup_res(df_1m):
    close = df_1m["close"].astype(float)
    open_ = df_1m["open"].astype(float)
    high = df_1m["high"].astype(float)
    low = df_1m["low"].astype(float)

    rsi_new = compute_rsi(close, RSI_PERIOD)

    hma5_open = hma(open_, CMO_HMA_FAST).shift(1)
    hma12_close = hma(close, CMO_HMA_SLOW)
    momm1 = hma5_open.diff()
    momm2 = hma12_close.diff()

    m1 = np.where(momm1 >= momm2, momm1, 0.0)
    m2 = np.where(momm1 >= momm2, 0.0, -momm1)
    sm1, sm2 = m1, m2
    denom = sm1 + sm2
    with np.errstate(divide="ignore", invalid="ignore"):
        cmo_new = np.where(denom != 0, 100 * (sm1 - sm2) / denom, np.nan)
    cmo_new = pd.Series(cmo_new, index=df_1m.index)

    hpivot_exists, lpivot_exists = causal_pivot_exists(high, low, PIVOT_LEFT_RIGHT, PIVOT_LEFT_RIGHT)

    sup = (rsi_new < 25) & (cmo_new > 50) & lpivot_exists
    res = (rsi_new > 75) & (cmo_new < -50) & hpivot_exists
    return sup.fillna(False), res.fillna(False)


# ══════════════════════════════════════════════════════════════════
# FAZ A: OLAY TESPITI (15dk)
# ══════════════════════════════════════════════════════════════════
def check_new_event(symbol):
    df15 = get_klines_closed(symbol, EVENT_TF, limit=3)
    if df15 is None or len(df15) < 1:
        return None

    last = df15.iloc[-1]
    open_time = int(last["open_time"])

    if last_processed_event_time.get(symbol) == open_time:
        return None  # bu mum zaten islendi
    last_processed_event_time[symbol] = open_time

    if symbol in pending_events:
        return None  # zaten bekleyen bir olay var, cozulmeden yenisi kaydedilmiyor

    body_pct = (float(last["close"]) - float(last["open"])) / float(last["open"]) * 100
    direction = None
    if body_pct >= BODY_PCT_THRESHOLD:
        direction = "LONG"
    elif body_pct <= -BODY_PCT_THRESHOLD:
        direction = "SHORT"
    if direction is None:
        return None

    event = {
        "direction": direction,
        "event_open_time_ms": open_time,
        "event_close_time_ms": int(last["close_time"]),
        "body_pct": body_pct,
    }
    pending_events[symbol] = event
    log.info(f"YENI OLAY: {symbol} {direction} govde=%{body_pct:.2f} - 1dk onayi aranmaya baslandi")
    return event


# ══════════════════════════════════════════════════════════════════
# FAZ B: PULLBACK ONAYI ARAMA (1dk, sadece bekleyenler)
# ══════════════════════════════════════════════════════════════════
@dataclass
class Signal:
    symbol: str
    direction: str
    price: float
    event_body_pct: float
    minutes_to_confirm: int
    bar_time: str


def check_pending_confirmation(symbol):
    event = pending_events.get(symbol)
    if event is None:
        return None

    now_ms = int(time.time() * 1000)
    expire_ms = event["event_close_time_ms"] + PULLBACK_SEARCH_CANDLES * 60 * 1000

    if now_ms > expire_ms:
        log.info(f"OLAY IPTAL: {symbol} {event['direction']} - {PULLBACK_SEARCH_CANDLES} mum icinde onay gelmedi")
        del pending_events[symbol]
        return None

    start_ms = event["event_close_time_ms"] - WARMUP_MINUTES * 60 * 1000
    df1m = get_klines_window(symbol, CONFIRM_TF, start_ms, now_ms)
    if df1m is None or len(df1m) < WARMUP_MINUTES // 2:
        return None

    # KRITIK: henuz KAPANMAMIS (olusmakta olan) son 1dk mumunu at - repaint kaynagi.
    # Binance API, sorgu araligina "now" dahil oldugunda o an olusmakta olan mumu
    # da doner; bu mum kapanana kadar RSI/CMO degerleri her taramada degisir.
    df1m = df1m[df1m["close_time"] < now_ms].reset_index(drop=True)
    if len(df1m) < WARMUP_MINUTES // 2:
        return None

    sup, res = compute_sup_res(df1m)
    search_mask = df1m["open_time"] >= event["event_close_time_ms"]

    if event["direction"] == "LONG":
        cond = sup & search_mask
    else:
        cond = res & search_mask

    hits = df1m.index[cond]
    if len(hits) == 0:
        return None

    # 45 mum sinirini burada da dogrula (guvenlik icin)
    search_indices = list(df1m.index[search_mask])
    first_hit_idx = hits[0]
    if first_hit_idx not in search_indices:
        return None
    candle_position = search_indices.index(first_hit_idx)
    if candle_position >= PULLBACK_SEARCH_CANDLES:
        return None

    entry_price = float(df1m["close"].iloc[first_hit_idx])
    entry_time_ms = int(df1m["open_time"].iloc[first_hit_idx])
    minutes_to_confirm = int((entry_time_ms - event["event_close_time_ms"]) / 60000)

    signal = Signal(
        symbol=symbol, direction=event["direction"], price=entry_price,
        event_body_pct=event["body_pct"], minutes_to_confirm=minutes_to_confirm,
        bar_time=datetime.fromtimestamp(entry_time_ms / 1000).strftime("%d/%m/%Y %H:%M:%S"),
    )
    del pending_events[symbol]  # olay cozuldu, tekrar kontrol edilmeyecek
    return signal


# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID eksik, konsola yaziliyor:\n" + text)
        return False
    try:
        r = session.post(
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
        header = "🟢 <b>15dk %7+ KIRILIM + 1dk PULLBACK ONAYI (LONG)</b>"
    else:
        header = "🔴 <b>15dk %7+ KIRILIM + 1dk PULLBACK ONAYI (SHORT)</b>"

    lines = [
        header,
        sep,
        f"💱 <b>Coin:</b> {coin}",
        f"💰 <b>Giris Fiyati:</b> {signal.price:.6f}",
        f"🕯️ <b>15dk Olay Govdesi:</b> %{signal.event_body_pct:.2f}",
        f"⏱️ <b>Onay Suresi:</b> {signal.minutes_to_confirm} dakika",
        sep,
        f"🕐 <b>Onay Zamani:</b> {signal.bar_time}",
        f"⏰ {datetime.now().strftime('%H:%M:%S %d/%m/%Y')}",
    ]
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════
# TARAMA DONGUSU
# ══════════════════════════════════════════════════════════════════
def run_scan():
    symbols = get_symbols()
    total = len(symbols)
    quote_volumes = get_all_24h_quote_volumes() if USE_LIQUIDITY_FILTER else {}

    if USE_LIQUIDITY_FILTER:
        symbols = [s for s in symbols if quote_volumes.get(s, 0.0) >= MIN_QUOTE_VOLUME_24H]

    log.info(f"FAZ A: {len(symbols)}/{total} coin (likidite filtresi sonrasi) 15dk olay taramasi...")

    new_events = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(check_new_event, s): s for s in symbols}
        for future in as_completed(futures):
            try:
                event = future.result()
                if event is not None:
                    new_events += 1
            except Exception as e:
                log.debug(f"FAZ A hata: {e}")

    log.info(f"FAZ A tamamlandi: {new_events} yeni olay. Bekleyen olay sayisi: {len(pending_events)}")

    if not pending_events:
        return 0

    pending_symbols = list(pending_events.keys())
    log.info(f"FAZ B: {len(pending_symbols)} coin icin 1dk pullback onayi kontrol ediliyor...")

    confirmed_signals = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(pending_symbols))) as executor:
        futures = {executor.submit(check_pending_confirmation, s): s for s in pending_symbols}
        for future in as_completed(futures):
            try:
                signal = future.result()
                if signal is not None:
                    confirmed_signals.append(signal)
            except Exception as e:
                log.warning(f"FAZ B hata: {e}")

    for signal in confirmed_signals:
        try:
            msg = format_signal_message(signal)
            if send_telegram(msg):
                log.info(f"SINYAL GONDERILDI: {signal.symbol} {signal.direction} "
                          f"onay={signal.minutes_to_confirm}dk")
            else:
                log.error(f"Telegram gonderilemedi: {signal.symbol}")
        except Exception as e:
            log.error(f"Gonderim hatasi {signal.symbol}: {e}")

    log.info(f"Tarama tamamlandi | {len(confirmed_signals)} onaylanan sinyal")
    return len(confirmed_signals)


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info("=" * 60)
    log.info("15dk %7+ KIRILIM + 1dk PULLBACK ONAYI SCANNER baslatildi (repaint'siz)")
    log.info(f"Max coin              : {MAX_COINS}")
    log.info(f"Workers               : {MAX_WORKERS}")
    log.info(f"Olay TF / Onay TF     : {EVENT_TF} / {CONFIRM_TF}")
    log.info(f"Govde esigi           : %{BODY_PCT_THRESHOLD}")
    log.info(f"Onay arama penceresi  : {PULLBACK_SEARCH_CANDLES} mum")
    log.info(f"RSI({RSI_PERIOD}) / CMO(HMA{CMO_HMA_FAST}/{CMO_HMA_SLOW}) / Pivot({PIVOT_LEFT_RIGHT})")
    log.info(f"Likidite filtre       : {USE_LIQUIDITY_FILTER} (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)")
    log.info(f"Tarama araligi        : {SCAN_INTERVAL} sn")
    log.info("=" * 60)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID eksik!")
        return

    send_telegram(
        "🚀 15dk %7+ KIRILIM + 1dk PULLBACK ONAYI SCANNER BASLADI\n"
        + "=" * 30 + "\n"
        f"💱 Olay TF: {EVENT_TF} | Onay TF: {CONFIRM_TF}\n"
        f"🕯️ Govde esigi: %{BODY_PCT_THRESHOLD}\n"
        f"⏱️ Onay penceresi: {PULLBACK_SEARCH_CANDLES} mum\n"
        f"💧 Min likidite: {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT\n"
        f"⚡ Workers: {MAX_WORKERS}"
    )

    while True:
        try:
            run_scan()
        except Exception as e:
            log.error(f"run_scan genel hata: {e}")

        log.info(f"{SCAN_INTERVAL}sn bekleniyor...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
