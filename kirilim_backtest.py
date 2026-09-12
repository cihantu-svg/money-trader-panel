"""
KIRILIM + RETEST BACKTEST  (tek dosya, bağımsız çalışır)
-----------------------------------------------------------
Çalıştırma:  python3 kirilim_backtest.py
Gerekli env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

Mantık:
  1) "MONEY TRADER - FIBO TRADE" indikatörünün kırılım bölümüyle BİREBİR AYNI
     şekilde KIRILIM AL / KIRILIM SAT sinyallerini üret (breakout + hacim + RSI/MACD).
  2) Sinyal barındaki kırılan seviyeyi (resistance_level / support_level) not al.
  3) Sinyalden SONRAKİ bar'lardan itibaren, EN FAZLA RETEST_MAX_BARS (varsayılan 10)
     bar içinde fiyat bu seviyeye GERİ DÖNÜP TEST EDİYOR MU (retest) diye bak.
     - Retest gelmezse: işlem AÇILMADI, sadece "no_retest" olarak kaydedilir.
     - Retest gelirse: işlem TAM O SEVİYEDEN (retest fiyatı) açılır.
  4) Açılan işlem: TP_PCT (%5) kâr / SL_PCT (%5) zarar ile takip edilir, hangisi
     önce gelirse o sonuç yazılır (ikisi aynı bar'da tetiklenirse muhafazakâr
     davranılıp SL öncelikli sayılır).
  5) Sonuç CSV'ye yazılır, özet + CSV dosyası Telegram'a gönderilir.
"""

import os
import time
import logging

import numpy as np
import pandas as pd
import requests

# ─────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────
BINANCE_FAPI = "https://fapi.binance.com"
INTERVAL = os.environ.get("INTERVAL", "15m")

BO_LEN = int(os.environ.get("BO_LEN", "20"))
BO_BUFFER_PCT = float(os.environ.get("BO_BUFFER_PCT", "0.5"))
VOL_MULT = float(os.environ.get("VOL_MULT", "2.0"))
MIN_BARS_BETWEEN = int(os.environ.get("MIN_BARS_BETWEEN", "5"))
RSI_LEN = int(os.environ.get("RSI_LEN", "14"))
RSI_BULL = float(os.environ.get("RSI_BULL", "60"))
RSI_BEAR = float(os.environ.get("RSI_BEAR", "40"))

RETEST_MAX_BARS = int(os.environ.get("RETEST_MAX_BARS", "10"))       # sinyalden sonra en fazla kaç bar retest beklenir
RETEST_TOLERANCE_PCT = float(os.environ.get("RETEST_TOLERANCE_PCT", "0.3"))  # seviyeye "dokundu" sayılması için tolerans
TP_PCT = float(os.environ.get("TP_PCT", "5.0"))
SL_PCT = float(os.environ.get("SL_PCT", "5.0"))
MAX_HOLD_BARS = int(os.environ.get("MAX_HOLD_BARS", "200"))          # TP/SL hiç gelmezse en fazla kaç bar beklenir

MIN_VOLUME_USDT = 3_000_000
LOOKBACK_BARS = int(os.environ.get("LOOKBACK_BARS", "1500"))         # Binance futures klines max limit
REQUEST_TIMEOUT = 15
SLEEP_BETWEEN_SYMBOLS = float(os.environ.get("SLEEP_BETWEEN_SYMBOLS", "0.45"))
WEIGHT_SOFT_LIMIT = int(os.environ.get("WEIGHT_SOFT_LIMIT", "1800"))
MAX_RETRIES = 5

OUTPUT_CSV = os.environ.get("OUTPUT_CSV", "kirilim_retest_backtest.csv")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kirilim_retest_backtest")


# ─────────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────────
def send_telegram_text(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram env eksik, sadece log:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log.error("Telegram mesaj hatası: %s - %s", r.status_code, r.text)
    except requests.RequestException as e:
        log.error("Telegram mesaj isteği başarısız: %s", e)


def send_telegram_document(path: str, caption: str = "") -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram env eksik, CSV gönderilemedi: %s", path)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, "rb") as f:
            r = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": (os.path.basename(path), f)},
                timeout=60,
            )
        if r.status_code != 200:
            log.error("Telegram dosya gönderim hatası: %s - %s", r.status_code, r.text)
    except requests.RequestException as e:
        log.error("Telegram dosya isteği başarısız: %s", e)


# ─────────────────────────────────────────────────────────────────────────
#  BINANCE FUTURES HELPERS  (rate-limit korumalı)
# ─────────────────────────────────────────────────────────────────────────
_session = requests.Session()


def binance_get(path: str, params: dict | None = None) -> requests.Response:
    url = f"{BINANCE_FAPI}{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        r = _session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            used_weight = r.headers.get("X-MBX-USED-WEIGHT-1M")
            if used_weight is not None and int(used_weight) >= WEIGHT_SOFT_LIMIT:
                log.warning("Kullanılan ağırlık %s/%s soft limite yaklaştı, 60sn bekleniyor.", used_weight, WEIGHT_SOFT_LIMIT)
                time.sleep(60)
            return r
        if r.status_code in (429, 418):
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else min(60, 2 ** attempt)
            log.warning("%s: %s alındı (deneme %d/%d), %.0f sn bekleniyor.", path, r.status_code, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
            continue
        r.raise_for_status()
    raise RuntimeError(f"{path} için {MAX_RETRIES} denemeden sonra rate-limit aşılamadı.")


def get_usdt_perpetual_symbols() -> list[str]:
    r = binance_get("/fapi/v1/exchangeInfo")
    data = r.json()
    return [
        s["symbol"] for s in data.get("symbols", [])
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
    ]


def get_24h_volume_map() -> dict[str, float]:
    r = binance_get("/fapi/v1/ticker/24hr")
    data = r.json()
    return {d["symbol"]: float(d["quoteVolume"]) for d in data}


def get_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    r = binance_get("/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit})
    raw = r.json()
    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    now_ms = int(time.time() * 1000)
    if raw and raw[-1][6] > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────────
#  İNDİKATÖR HESAPLARI (Pine'ın kırılım bölümüne sadık port)
# ─────────────────────────────────────────────────────────────────────────
def rma(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(alpha=1 / length, adjust=False).mean()


def wilder_rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = rma(up, length)
    roll_down = rma(down, length)
    rs = roll_up / roll_down
    rsi = np.where(roll_down == 0, 100.0, np.where(roll_up == 0, 0.0, 100 - 100 / (1 + rs)))
    return pd.Series(rsi, index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().reset_index(drop=True)

    out["rsi"] = wilder_rsi(out["close"], RSI_LEN)
    macd_line, signal_line, hist = macd(out["close"], 12, 26, 9)
    out["macd_line"] = macd_line
    out["signal_line"] = signal_line
    out["hist"] = hist
    out["vol_sma"] = out["volume"].rolling(20).mean()

    out["resistance_level"] = out["high"].rolling(BO_LEN).max().shift(1)
    out["support_level"] = out["low"].rolling(BO_LEN).min().shift(1)

    vol_bullish = out["volume"] > (out["vol_sma"] * VOL_MULT)
    vol_bearish = out["volume"] > (out["vol_sma"] * VOL_MULT)

    mom_bullish = (out["rsi"] > RSI_BULL) & (out["hist"] > 0) & (out["macd_line"] > out["signal_line"])
    mom_bearish = (out["rsi"] < RSI_BEAR) & (out["hist"] < 0) & (out["macd_line"] < out["signal_line"])

    prev_close = out["close"].shift(1)
    breakout_up = (out["close"] > out["resistance_level"] * (1 + BO_BUFFER_PCT / 100)) & (prev_close <= out["resistance_level"])
    breakout_down = (out["close"] < out["support_level"] * (1 - BO_BUFFER_PCT / 100)) & (prev_close >= out["support_level"])

    raw_buy = (breakout_up & vol_bullish & mom_bullish).fillna(False).to_numpy()
    raw_sell = (breakout_down & vol_bearish & mom_bearish).fillna(False).to_numpy()

    n = len(out)
    buy_arr = np.zeros(n, dtype=bool)
    sell_arr = np.zeros(n, dtype=bool)
    last_signal_bar = None
    for i in range(n):
        bars_since = 999 if last_signal_bar is None else (i - last_signal_bar)
        can_signal = bars_since >= MIN_BARS_BETWEEN
        buy_arr[i] = bool(raw_buy[i] and can_signal)
        sell_arr[i] = bool(raw_sell[i] and can_signal)
        if buy_arr[i] or sell_arr[i]:
            last_signal_bar = i

    out["buy"] = buy_arr
    out["sell"] = sell_arr
    return out


# ─────────────────────────────────────────────────────────────────────────
#  RETEST + TP/SL SİMÜLASYONU
# ─────────────────────────────────────────────────────────────────────────
def find_retest_and_simulate(out: pd.DataFrame, sig_idx: int, direction: str) -> dict:
    n = len(out)
    high = out["high"].to_numpy()
    low = out["low"].to_numpy()
    close = out["close"].to_numpy()
    close_time = out["close_time"]

    level = out.loc[sig_idx, "resistance_level"] if direction == "buy" else out.loc[sig_idx, "support_level"]
    tol = RETEST_TOLERANCE_PCT / 100

    retest_bar = None
    end_search = min(sig_idx + RETEST_MAX_BARS, n - 1)
    for j in range(sig_idx + 1, end_search + 1):
        if direction == "buy":
            if low[j] <= level * (1 + tol):
                retest_bar = j
                break
        else:
            if high[j] >= level * (1 - tol):
                retest_bar = j
                break

    base_row = {
        "signal_time": close_time[sig_idx],
        "direction": direction,
        "signal_close": out.loc[sig_idx, "close"],
        "level": level,
        "retest_bar_offset": None,
        "retest_time": None,
        "entry": None,
        "tp_price": None,
        "sl_price": None,
        "outcome": "no_retest",
        "bars_to_resolution": None,
        "resolution_time": None,
        "realized_ret_pct": None,
    }

    if retest_bar is None:
        return base_row

    entry = level  # retest tam seviyeden giriş
    if direction == "buy":
        tp_price = entry * (1 + TP_PCT / 100)
        sl_price = entry * (1 - SL_PCT / 100)
    else:
        tp_price = entry * (1 - TP_PCT / 100)
        sl_price = entry * (1 + SL_PCT / 100)

    base_row.update({
        "retest_bar_offset": retest_bar - sig_idx,
        "retest_time": close_time[retest_bar],
        "entry": entry,
        "tp_price": tp_price,
        "sl_price": sl_price,
    })

    end_hold = min(retest_bar + MAX_HOLD_BARS, n - 1)
    outcome = "open"
    resolution_bar = None
    for k in range(retest_bar, end_hold + 1):
        hit_tp = high[k] >= tp_price if direction == "buy" else low[k] <= tp_price
        hit_sl = low[k] <= sl_price if direction == "buy" else high[k] >= sl_price
        if hit_tp and hit_sl:
            outcome = "SL"  # aynı barda ikisi de tetiklenirse muhafazakâr davran
            resolution_bar = k
            break
        elif hit_sl:
            outcome = "SL"
            resolution_bar = k
            break
        elif hit_tp:
            outcome = "TP"
            resolution_bar = k
            break

    if resolution_bar is not None:
        realized = TP_PCT if outcome == "TP" else -SL_PCT
        base_row.update({
            "outcome": outcome,
            "bars_to_resolution": resolution_bar - retest_bar,
            "resolution_time": close_time[resolution_bar],
            "realized_ret_pct": realized,
        })
    else:
        final_close = close[end_hold]
        ret = (final_close / entry - 1) * 100 if direction == "buy" else (1 - final_close / entry) * 100
        base_row.update({
            "outcome": "open_no_resolution",
            "bars_to_resolution": end_hold - retest_bar,
            "resolution_time": close_time[end_hold],
            "realized_ret_pct": round(ret, 3),
        })

    return base_row


# ─────────────────────────────────────────────────────────────────────────
#  ANA AKIŞ
# ─────────────────────────────────────────────────────────────────────────
def main() -> None:
    log.info(
        "Kırılım+Retest backtest başlıyor. TF=%s BO_LEN=%d RETEST_MAX_BARS=%d TP=%%%.1f SL=%%%.1f",
        INTERVAL, BO_LEN, RETEST_MAX_BARS, TP_PCT, SL_PCT,
    )

    symbols = get_usdt_perpetual_symbols()
    volumes = get_24h_volume_map()
    filtered = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    log.info("Taranacak sembol sayısı: %d", len(filtered))

    rows = []
    for n_done, symbol in enumerate(filtered, 1):
        try:
            df = get_klines(symbol, INTERVAL, LOOKBACK_BARS)
            if len(df) < max(BO_LEN, 26) + 30:
                continue
            out = compute_signals(df)
            sig_idx_buy = out.index[out["buy"]].tolist()
            sig_idx_sell = out.index[out["sell"]].tolist()

            for idx in sig_idx_buy:
                row = find_retest_and_simulate(out, idx, "buy")
                row["symbol"] = symbol
                rows.append(row)
            for idx in sig_idx_sell:
                row = find_retest_and_simulate(out, idx, "sell")
                row["symbol"] = symbol
                rows.append(row)
        except Exception as e:
            log.error("Backtest hata (%s): %s", symbol, e)
        finally:
            time.sleep(SLEEP_BETWEEN_SYMBOLS)

        if n_done % 25 == 0:
            log.info("İlerleme: %d/%d sembol tarandı, şimdiye kadar %d sinyal.", n_done, len(filtered), len(rows))

    if not rows:
        log.warning("Hiç sinyal bulunamadı.")
        send_telegram_text("📊 Kırılım+Retest Backtest tamamlandı.\nHiç sinyal bulunamadı.")
        return

    result_df = pd.DataFrame(rows)
    cols_order = [
        "symbol", "direction", "signal_time", "signal_close", "level",
        "retest_bar_offset", "retest_time", "entry", "tp_price", "sl_price",
        "outcome", "bars_to_resolution", "resolution_time", "realized_ret_pct",
    ]
    result_df = result_df[cols_order].sort_values("signal_time")
    result_df.to_csv(OUTPUT_CSV, index=False)
    log.info("CSV yazıldı: %s (%d satır)", OUTPUT_CSV, len(result_df))

    total_signals = len(result_df)
    no_retest = (result_df["outcome"] == "no_retest").sum()
    traded = result_df[result_df["outcome"] != "no_retest"]
    n_tp = (traded["outcome"] == "TP").sum()
    n_sl = (traded["outcome"] == "SL").sum()
    n_open = (traded["outcome"] == "open_no_resolution").sum()
    resolved = n_tp + n_sl
    win_rate = (n_tp / resolved * 100) if resolved > 0 else float("nan")
    avg_retest_wait = traded["retest_bar_offset"].mean() if len(traded) else float("nan")
    avg_bars_to_res = traded.loc[traded["outcome"].isin(["TP", "SL"]), "bars_to_resolution"].mean()

    summary = (
        f"📊 <b>Kırılım + Retest Backtest Sonucu</b>\n"
        f"TF: {INTERVAL} | Retest penceresi: {RETEST_MAX_BARS} bar | TP/SL: %{TP_PCT:.0f} / %{SL_PCT:.0f}\n"
        f"Sembol sayısı: {len(filtered)}\n"
        f"Toplam ham sinyal: {total_signals}\n"
        f"Retest GELMEDİ (işlem açılmadı): {no_retest}\n"
        f"Retest GELDİ (işleme girildi): {len(traded)}\n"
        f"  ✅ TP: {n_tp}  |  ❌ SL: {n_sl}  |  ⏳ Açık/sonuçsuz: {n_open}\n"
        f"  Kazanma oranı (TP/(TP+SL)): %{win_rate:.1f}\n"
        f"  Ort. retest bekleme: {avg_retest_wait:.1f} bar\n"
        f"  Ort. sonuca ulaşma süresi: {avg_bars_to_res:.1f} bar\n"
    )
    log.info(summary.replace("\n", " | "))
    send_telegram_text(summary)
    send_telegram_document(OUTPUT_CSV, caption="Kırılım + Retest backtest - ham sonuçlar")


if __name__ == "__main__":
    main()
