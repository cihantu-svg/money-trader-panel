# -*- coding: utf-8 -*-
"""
15dk %7+ MUM + 1dk PULLBACK ONAYI BACKTEST (REPAINT'SIZ)

ADIM 1 - OLAY TESPITI (15dk):
  Govdesi (open->close) >= %7 olan yesil mum -> LONG olayi
  Govdesi <= -%7 olan kirmizi mum -> SHORT olayi

ADIM 2 - PULLBACK ONAYI (1dk, sadece olay sonrasi dar bir pencerede):
  Gonderilen Pine indikatorunun (@BarsStallone S/R) OZUNDEN alinan tetikleyici
  kosul, REPAINT KAYNAGI (request.security + lookahead_on, coklu-TF projeksiyonu)
  TAMAMEN CIKARILARAK, sadece 1dk'nin kendi KAPANMIS barlarindan hesaplanir:

    RSI_new = RSI(close, 9)          [Wilder/RMA - orijinaldeki gibi]
    HMA5_open_shifted = HMA(open,5) bir bar geriden (orijindeki [1] kaymasi)
    HMA12_close = HMA(close,12)
    momm1 = degisim(HMA5_open_shifted), momm2 = degisim(HMA12_close)
    CMO_new = 100*(f1-f2)/(f1+f2)   [orijindeki f1/f2 mantigi, length1=1]

    Pivot "var mi" bayragi: klasik pivot-high/pivot-low (sol=sag=2 bar),
    SADECE ONAYLANDIKTAN SONRA (2 bar gecikmeyle) True olur - repaint yok.
    Bir kez olustuktan sonra "var" bayragi kalici kalir (orijindeki fixnan
    davranisiyla ayni).

    LONG onayi (sup): RSI_new<25 VE CMO_new>50 VE en az bir dusuk pivot
                       daha once olusmus (guvenlik sarti)
    SHORT onayi (res): RSI_new>75 VE CMO_new<-50 VE en az bir yuksek pivot
                        daha once olusmus

  15dk LONG olayindan sonra ilk "sup" onayi -> giris (o 1dk barinin close'u)
  15dk SHORT olayindan sonra ilk "res" onayi -> giris

  PULLBACK_SEARCH_MINUTES icinde onay gelmezse o olay ELENIR (sinyal yok).

ADIM 3 - SONUC OLCUMU (verimlilik icin tekrar 15dk veriye donulur):
  1dk giris fiyatindan sonra FORWARD_HOURS icinde (15dk mumlarla) en iyi
  lehte hareket olculur, %5/%10/%20 hit-rate hesaplanir.

UYARI: Bu, repaint'li orijinal indikatorden FARKLI (daha muhafazakar, gecikmeli
ama durust) bir sinyal uretir. Amac gercekci/test edilebilir bir versiyon.
"""
import time
import logging
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, field

import numpy as np
import requests
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BINANCE_BASE = "https://fapi.binance.com"
session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=30, pool_maxsize=30)
session.mount("https://", _adapter)

# ══════════════════════════════════════════════════════════════════
# AYARLAR
# ══════════════════════════════════════════════════════════════════
EVENT_TF = "15m"
TOP_N_SYMBOLS = 400
MIN_QUOTE_VOLUME_24H = 3_000_000
LOOKBACK_DAYS = 20

BODY_PCT_THRESHOLD = 7.0

PULLBACK_SEARCH_CANDLES = 30    # olay sonrasi ONAY aranacak 1dk mum sayisi (30 mumu asarsa iptal)
WARMUP_MINUTES = 90             # indikator warmup icin olay ONCESI 1dk verisi

RSI_PERIOD = 9
CMO_HMA_FAST = 5
CMO_HMA_SLOW = 12
PIVOT_LEFT_RIGHT = 2            # orijindeki len5=2

FORWARD_HOURS = 48
TARGET_PCTS = [5]               # tek hedef: %5

MAX_EVENTS_PER_RUN = int(os.getenv("MAX_EVENTS_PER_RUN", "800"))  # asiri API yukunu sinirla

MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 0.5
REQUEST_TIMEOUT = 10

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


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
# VERI CEKME
# ══════════════════════════════════════════════════════════════════
def get_top_symbols(top_n, min_qv):
    r = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/ticker/24hr", timeout=15)
    data = r.json()
    info = _request_with_retry(f"{BINANCE_BASE}/fapi/v1/exchangeInfo", timeout=15).json()
    trading = {s["symbol"] for s in info["symbols"] if s["status"] == "TRADING" and s["symbol"].endswith("USDT")}
    rows = [(d["symbol"], float(d.get("quoteVolume", 0))) for d in data
            if d["symbol"] in trading and float(d.get("quoteVolume", 0)) >= min_qv]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:top_n]]


def _parse_klines(raw):
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "qav", "trades", "taker_buy_base", "taker_buy_quote", "ignore"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = df["open_time"].astype(np.int64)
    df["close_time"] = df["close_time"].astype(np.int64)
    return df.drop_duplicates(subset="open_time").reset_index(drop=True)


def get_klines_range(symbol, interval, start_ms, end_ms, interval_ms):
    all_rows = []
    cursor = start_ms
    while cursor < end_ms:
        r = _request_with_retry(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "startTime": cursor,
                    "endTime": end_ms, "limit": 1500},
        )
        raw = r.json()
        if not raw:
            break
        all_rows.extend(raw)
        last_open = raw[-1][0]
        next_cursor = last_open + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(raw) < 1500:
            break
        time.sleep(0.08)
    if not all_rows:
        return None
    return _parse_klines(all_rows)


def get_klines_window(symbol, interval, start_ms, end_ms):
    """Tek istekte (limit=1500) dar bir pencere ceker - olay bazli 1dk sorgular icin."""
    try:
        r = _request_with_retry(
            f"{BINANCE_BASE}/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "startTime": start_ms,
                    "endTime": end_ms, "limit": 1500},
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
    """Klasik pivot-high/pivot-low (sol=sag), SADECE right bar sonra onaylanir.
    fixnan davranisi: bir kez pivot olustu mu, 'var' bayragi kalicidir."""
    window = left + right + 1
    roll_max = high.rolling(window, center=True).max()
    roll_min = low.rolling(window, center=True).min()
    is_high_pivot_center = (high == roll_max)
    is_low_pivot_center = (low == roll_min)
    # onay 'right' bar sonra gelir -> ileri kaydir
    confirmed_high = is_high_pivot_center.shift(right).fillna(False)
    confirmed_low = is_low_pivot_center.shift(right).fillna(False)
    hpivot_exists = (confirmed_high.cumsum() > 0)
    lpivot_exists = (confirmed_low.cumsum() > 0)
    return hpivot_exists, lpivot_exists


def compute_sup_res(df_1m):
    """df_1m: warmup + arama penceresini birlikte iceren 1dk dataframe.
    Doner: sup (bool Series), res (bool Series) - REPAINT'SIZ, sadece kapanmis barlar."""
    close = df_1m["close"].astype(float)
    open_ = df_1m["open"].astype(float)
    high = df_1m["high"].astype(float)
    low = df_1m["low"].astype(float)

    rsi_new = compute_rsi(close, RSI_PERIOD)

    hma5_open = hma(open_, CMO_HMA_FAST).shift(1)   # orijindeki [1] kaymasi
    hma12_close = hma(close, CMO_HMA_SLOW)
    momm1 = hma5_open.diff()
    momm2 = hma12_close.diff()

    m1 = np.where(momm1 >= momm2, momm1, 0.0)
    m2 = np.where(momm1 >= momm2, 0.0, -momm1)
    sm1, sm2 = m1, m2  # length1=1 -> sum(1 bar) = kendisi
    denom = sm1 + sm2
    with np.errstate(divide="ignore", invalid="ignore"):
        cmo_new = np.where(denom != 0, 100 * (sm1 - sm2) / denom, np.nan)
    cmo_new = pd.Series(cmo_new, index=df_1m.index)

    hpivot_exists, lpivot_exists = causal_pivot_exists(high, low, PIVOT_LEFT_RIGHT, PIVOT_LEFT_RIGHT)

    sup = (rsi_new < 25) & (cmo_new > 50) & lpivot_exists
    res = (rsi_new > 75) & (cmo_new < -50) & hpivot_exists
    return sup.fillna(False), res.fillna(False)


# ══════════════════════════════════════════════════════════════════
# OLAY TESPITI (15dk)
# ══════════════════════════════════════════════════════════════════
@dataclass
class Event:
    symbol: str
    direction: str
    event_close_time_ms: int
    event_time_str: str
    idx15: int


def find_events(symbol, df15):
    events = []
    n = len(df15)
    if n < 5:
        return events
    close = df15["close"].astype(float)
    open_ = df15["open"].astype(float)
    body_pct = (close - open_) / open_ * 100

    for i in range(n):
        bp = body_pct.iloc[i]
        if pd.isna(bp):
            continue
        direction = None
        if bp >= BODY_PCT_THRESHOLD:
            direction = "LONG"
        elif bp <= -BODY_PCT_THRESHOLD:
            direction = "SHORT"
        if direction is None:
            continue
        events.append(Event(
            symbol=symbol, direction=direction,
            event_close_time_ms=int(df15["close_time"].iloc[i]),
            event_time_str=datetime.fromtimestamp(int(df15["open_time"].iloc[i]) / 1000).strftime("%Y-%m-%d %H:%M"),
            idx15=i,
        ))
    return events


# ══════════════════════════════════════════════════════════════════
# PULLBACK ARAMA (1dk, olay bazli dar pencere)
# ══════════════════════════════════════════════════════════════════
@dataclass
class ConfirmedSignal:
    symbol: str
    direction: str
    event_time: str
    entry_time: str
    entry_price: float
    minutes_to_confirm: int
    max_favorable_pct: float = None
    hit_targets: dict = field(default_factory=dict)


def find_pullback_entry(event):
    start_ms = event.event_close_time_ms - WARMUP_MINUTES * 60 * 1000
    # 30 mumluk arama penceresi + guvenlik payi (1dk mum = 1dk, coin bazinda ara kapanan mum olmayabilir diye pay birakiyoruz)
    end_ms = event.event_close_time_ms + (PULLBACK_SEARCH_CANDLES + 10) * 60 * 1000
    df1m = get_klines_window(event.symbol, "1m", start_ms, end_ms)
    if df1m is None or len(df1m) < WARMUP_MINUTES // 2:
        return None

    sup, res = compute_sup_res(df1m)
    search_mask = df1m["open_time"] >= event.event_close_time_ms

    if event.direction == "LONG":
        cond = sup & search_mask
    else:
        cond = res & search_mask

    hits = df1m.index[cond]
    if len(hits) == 0:
        return None

    # arama penceresindeki (olay sonrasi) mumlarin sirasini bul, ilk onay 30. mumu asiyorsa iptal
    search_indices = df1m.index[search_mask]
    first_hit_idx = hits[0]
    candle_position = list(search_indices).index(first_hit_idx) if first_hit_idx in search_indices else None
    if candle_position is None or candle_position >= PULLBACK_SEARCH_CANDLES:
        return None  # 30 mum icinde onay gelmedi - iptal

    entry_idx = first_hit_idx
    entry_price = float(df1m["close"].iloc[entry_idx])
    entry_time_ms = int(df1m["open_time"].iloc[entry_idx])
    minutes_to_confirm = int((entry_time_ms - event.event_close_time_ms) / 60000)

    return ConfirmedSignal(
        symbol=event.symbol, direction=event.direction,
        event_time=event.event_time_str,
        entry_time=datetime.fromtimestamp(entry_time_ms / 1000).strftime("%Y-%m-%d %H:%M"),
        entry_price=entry_price, minutes_to_confirm=minutes_to_confirm,
    )


# ══════════════════════════════════════════════════════════════════
# SONUC OLCUMU (15dk veriye geri donerek)
# ══════════════════════════════════════════════════════════════════
def measure_outcome(signal, df15):
    entry_time_ms = int(datetime.strptime(signal.entry_time, "%Y-%m-%d %H:%M").timestamp() * 1000)
    forward_candles = int(FORWARD_HOURS * 60 / 15)

    idx_after = df15.index[df15["open_time"] >= entry_time_ms]
    if len(idx_after) == 0:
        return None
    i_entry = idx_after[0]
    future = df15.iloc[i_entry: i_entry + forward_candles]
    if len(future) == 0:
        return None

    if signal.direction == "LONG":
        max_fav = (future["high"].max() - signal.entry_price) / signal.entry_price * 100
    else:
        max_fav = (signal.entry_price - future["low"].min()) / signal.entry_price * 100

    signal.max_favorable_pct = float(max_fav)
    signal.hit_targets = {t: bool(max_fav >= t) for t in TARGET_PCTS}
    return signal


# ══════════════════════════════════════════════════════════════════
# RAPOR
# ══════════════════════════════════════════════════════════════════
def summarize(confirmed, total_events):
    print("\n" + "=" * 95)
    print(f"15dk %{BODY_PCT_THRESHOLD}+ MUM + 1dk PULLBACK ONAYI BACKTEST (REPAINT'SIZ)")
    print(f"{TOP_N_SYMBOLS} coin, {LOOKBACK_DAYS} gun | Toplam olay: {total_events} | Onaylanan: {len(confirmed)}")
    print("=" * 95)

    if not confirmed:
        print("Hic onaylanmis pullback sinyali yok.")
        return

    n = len(confirmed)
    rates = {}
    for t in TARGET_PCTS:
        hits = sum(1 for s in confirmed if s.hit_targets.get(t))
        rates[t] = hits / n * 100
    avg_fav = sum(s.max_favorable_pct for s in confirmed) / n
    avg_confirm_min = sum(s.minutes_to_confirm for s in confirmed) / n

    print(f"\nOnay orani: {n}/{total_events} olay (%{n/total_events*100:.1f})")
    for t in TARGET_PCTS:
        print(f"  >= %{t} hedefe ulasma orani: {rates[t]:.1f}%")
    print(f"  Ortalama en iyi lehte hareket: {avg_fav:.2f}%")
    print(f"  Ortalama onay suresi (olay sonrasi): {avg_confirm_min:.1f} dakika")

    print("\n--- YON BAZINDA ---")
    for direction in ("LONG", "SHORT"):
        subset = [s for s in confirmed if s.direction == direction]
        if not subset:
            continue
        nd = len(subset)
        r10 = sum(1 for s in subset if s.hit_targets.get(10)) / nd * 100
        fav = sum(s.max_favorable_pct for s in subset) / nd
        print(f"  {direction:<6} n={nd:>4}  %10 hit-rate={r10:.1f}%  ort.fav={fav:.2f}%")


def save_csv(confirmed, path="fifteen_min_pullback_results.csv"):
    rows = []
    for s in confirmed:
        row = {
            "symbol": s.symbol, "direction": s.direction, "event_time": s.event_time,
            "entry_time": s.entry_time, "entry_price": s.entry_price,
            "minutes_to_confirm": s.minutes_to_confirm,
            "max_favorable_pct": round(s.max_favorable_pct, 2) if s.max_favorable_pct is not None else None,
        }
        for t in TARGET_PCTS:
            row[f"hit_{t}pct"] = s.hit_targets.get(t)
        rows.append(row)
    pd.DataFrame(rows).to_csv(path, index=False)
    log.info(f"Detayli sonuclar kaydedildi: {path}")


def send_telegram_document(path, caption=""):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        with open(path, "rb") as f:
            r = session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": f}, timeout=60,
            )
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram dosya gonderim hatasi: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    log.info(f"Top {TOP_N_SYMBOLS} likit coin cekiliyor (min {MIN_QUOTE_VOLUME_24H/1e6:.1f}M USDT)...")
    symbols = get_top_symbols(TOP_N_SYMBOLS, MIN_QUOTE_VOLUME_24H)
    log.info(f"{len(symbols)} coin bulundu. Adim 1: 15dk'da govde>=%{BODY_PCT_THRESHOLD} olaylari taraniyor...")

    end_ms = int(time.time() * 1000)
    start_ms = int((datetime.now() - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    all_events = []
    df15_by_symbol = {}
    for idx, symbol in enumerate(symbols, 1):
        try:
            df15 = get_klines_range(symbol, EVENT_TF, start_ms, end_ms, 15 * 60 * 1000)
            if df15 is None or len(df15) < 10:
                continue
            df15_by_symbol[symbol] = df15
            evs = find_events(symbol, df15)
            all_events.extend(evs)
            if idx % 20 == 0:
                log.info(f"[{idx}/{len(symbols)}] islendi, su ana kadar {len(all_events)} olay")
        except Exception as e:
            log.warning(f"{symbol} 15dk hata: {e}")
        time.sleep(0.05)

    total_events = len(all_events)
    log.info(f"Toplam {total_events} olay bulundu (LONG+SHORT, govde>=%{BODY_PCT_THRESHOLD}).")

    if total_events == 0:
        log.error("Hic olay bulunamadi.")
        return

    if total_events > MAX_EVENTS_PER_RUN:
        log.warning(f"Olay sayisi ({total_events}) MAX_EVENTS_PER_RUN ({MAX_EVENTS_PER_RUN}) asiyor, "
                    f"rastgele {MAX_EVENTS_PER_RUN} tanesi test edilecek.")
        rng = np.random.default_rng(42)
        idxs = rng.choice(total_events, size=MAX_EVENTS_PER_RUN, replace=False)
        events_to_test = [all_events[i] for i in sorted(idxs)]
    else:
        events_to_test = all_events

    log.info(f"Adim 2: {len(events_to_test)} olay icin 1dk pullback onayi araniyor "
              f"(en fazla {PULLBACK_SEARCH_CANDLES} mum icinde onay aranacak, gecerse iptal)...")

    confirmed = []
    for idx, event in enumerate(events_to_test, 1):
        try:
            sig = find_pullback_entry(event)
            if sig is not None:
                df15 = df15_by_symbol.get(event.symbol)
                if df15 is not None:
                    result = measure_outcome(sig, df15)
                    if result is not None:
                        confirmed.append(result)
        except Exception as e:
            log.warning(f"{event.symbol} pullback arama hatasi: {e}")
        if idx % 50 == 0:
            log.info(f"[{idx}/{len(events_to_test)}] olay islendi, {len(confirmed)} onaylanan sinyal")
        time.sleep(0.05)

    summarize(confirmed, total_events)
    if confirmed:
        save_csv(confirmed)
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            sent = send_telegram_document("fifteen_min_pullback_results.csv",
                                           caption="15dk %7 + 1dk pullback backtest sonuclari")
            if sent:
                log.info("CSV Telegram'a gonderildi.")


if __name__ == "__main__":
    main()
