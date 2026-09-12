"""
ICT Liquidity Sweep & Structure Scanner
-----------------------------------------------
Pine kaynağı: "ICT Liquidity Sweep & Structure" (@version=6, overlay indikatör)

Mantık (indikatörle birebir port edildi, tek zaman diliminde, security() kullanılmadığı
için repaint riski yok):

  1) SWING PIVOT: pivLen bar sağında/solunda teyitli swing high/low (lastPH / lastPL).
  2) LIQUIDITY SWEEP: fiyat bir swing'i FİTİLLE aşıp KAPANIŞLA geri içeri dönerse
     "sweep" (likidite avı) sayılır -> armedLong / armedShort.
  3) MARKET STRUCTURE: kapanış son swing'i geçerse BOS (trend yönünde) ya da
     CHoCH (ilk ters kırılım).
  4) SİNYAL: sweep + (BOS ya da güçlü yönlü kapanış) ikisi birden gerçekleşirse
     BUY / SELL üretilir. Bar bazlı debounce (minGap) ile sık sinyal engellenir.
     -> Bu tasarım zaten "sweep + yapısal teyit" istediği için Bar Stallone'daki
        gibi ayrı bir %5 reaction katmanına ihtiyaç yok; sinyal zaten teyitli üretiliyor.

Kapsam: Binance Futures USDT-M perpetual, 24s hacim >= MIN_VOLUME_USDT
Zaman dilimi: 15 dakika (kullanıcı seçimi)
Pivot gücü (pivLen): 5 (kullanıcı seçimi - "biraz daha sık")
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
INTERVAL = "15m"                          # kullanıcı seçimi
PIV_LEN = int(os.environ.get("PIV_LEN", "5"))      # kullanıcı seçimi (pivLen)
SWEEP_WICK_ATR = float(os.environ.get("SWEEP_WICK_ATR", "0.0"))  # Pine varsayılanı: 0.0 (herhangi bir piercing yeterli)
ARM_EXPIRE_BARS = int(os.environ.get("ARM_EXPIRE_BARS", "6"))    # sweep sonrası onay beklenen max bar sayısı
MIN_SIGNAL_GAP_BARS = int(os.environ.get("MIN_SIGNAL_GAP_BARS", "8"))  # aynı sembolde ardışık sinyal debounce
ATR_LEN = 14

MIN_VOLUME_USDT = 3_000_000
KLINES_LIMIT = 150
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", 60 * 5))  # 5 dakikada bir tara
REQUEST_TIMEOUT = 10
SLEEP_BETWEEN_SYMBOLS = float(os.environ.get("SLEEP_BETWEEN_SYMBOLS", "0.45"))
WEIGHT_SOFT_LIMIT = int(os.environ.get("WEIGHT_SOFT_LIMIT", "1800"))
MAX_RETRIES = 5

CONVICTION_MIN = int(os.environ.get("CONVICTION_MIN", "60"))  # bunun altındaki sinyaller sadece log'a yazılır, Telegram'a gitmez
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ict_liquidity_scanner")

# symbol -> son alarm verilen bar'ın close_time'ı (aynı bar için tekrar mesaj atmamak için)
LAST_ALERTED_BAR: dict[str, str] = {}


# ─────────────────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────────────────
def send_telegram(message: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram env değişkenleri eksik, mesaj sadece loglanıyor:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log.error("Telegram gönderim hatası: %s - %s", r.status_code, r.text)
    except requests.RequestException as e:
        log.error("Telegram isteği başarısız: %s", e)


# ─────────────────────────────────────────────────────────────────────────
#  BINANCE FUTURES HELPERS  (rate-limit korumalı: retry/backoff + weight izleme)
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
    if raw and raw[-1][6] > now_ms:  # son mum hâlâ açık -> at
        df = df.iloc[:-1].reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────────
#  İNDİKATÖR HESAPLARI (Pine koduna sadık port)
# ─────────────────────────────────────────────────────────────────────────
def atr(df: pd.DataFrame, length: int = ATR_LEN) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def find_pivots(df: pd.DataFrame, piv_len: int) -> tuple[pd.Series, pd.Series]:
    """Standart pivot high/low: piv_len bar solunda ve sağında en yüksek/düşük.
    Sadece i+piv_len <= son index olan barlarda teyitlidir (non-repainting)."""
    high, low = df["high"], df["low"]
    n = len(df)
    ph = pd.Series(np.nan, index=df.index)
    pl = pd.Series(np.nan, index=df.index)
    for i in range(piv_len, n - piv_len):
        window_h = high.iloc[i - piv_len:i + piv_len + 1]
        if high.iloc[i] == window_h.max():
            ph.iloc[i] = high.iloc[i]
        window_l = low.iloc[i - piv_len:i + piv_len + 1]
        if low.iloc[i] == window_l.min():
            pl.iloc[i] = low.iloc[i]
    return ph, pl


def compute_signals(df: pd.DataFrame, piv_len: int = PIV_LEN) -> pd.DataFrame:
    out = df.copy().reset_index(drop=True)
    out["atr"] = atr(out, ATR_LEN)
    ph, pl = find_pivots(out, piv_len)

    n = len(out)
    last_ph = np.nan
    last_pl = np.nan
    ph_swept = False
    pl_swept = False
    bias = 0
    bos_hi = np.nan
    bos_lo = np.nan

    armed_long = False
    armed_short = False
    arm_low_ref = np.nan
    arm_high_ref = np.nan
    arm_long_bar = None
    arm_short_bar = None
    last_sig_bar = None

    sweep_low_arr = np.zeros(n, dtype=bool)
    sweep_high_arr = np.zeros(n, dtype=bool)
    bos_up_arr = np.zeros(n, dtype=bool)
    bos_dn_arr = np.zeros(n, dtype=bool)
    choch_up_arr = np.zeros(n, dtype=bool)
    choch_dn_arr = np.zeros(n, dtype=bool)
    buy_arr = np.zeros(n, dtype=bool)
    sell_arr = np.zeros(n, dtype=bool)
    conv_arr = np.zeros(n, dtype=float)

    high = out["high"].to_numpy()
    low = out["low"].to_numpy()
    close = out["close"].to_numpy()
    open_ = out["open"].to_numpy()
    atr_v = out["atr"].to_numpy()
    # Pine'da ta.pivothigh/pivotlow bir pivotu piv_len bar SONRA (sağ taraf teyit
    # olunca) "biliniyor" sayar ve lastPH/lastPL ancak o barda güncellenir.
    # find_pivots() değeri pivot barının KENDİSİNE yazıyor, bu yüzden burada
    # piv_len bar ileri kaydırıp Pine'ın gerçek "biliniyor" anına hizalıyoruz.
    ph_v = ph.shift(piv_len).to_numpy()
    pl_v = pl.shift(piv_len).to_numpy()

    for i in range(n):
        # --- yeni pivot teyidi -> sticky lastPH/lastPL güncelle, sweep flag sıfırla ---
        if not np.isnan(ph_v[i]):
            last_ph = ph_v[i]
            ph_swept = False
        if not np.isnan(pl_v[i]):
            last_pl = pl_v[i]
            pl_swept = False

        if not np.isnan(last_ph):
            bos_hi = last_ph
        if not np.isnan(last_pl):
            bos_lo = last_pl

        # --- market structure: BOS / CHoCH ---
        bos_up = (not np.isnan(bos_hi)) and i > 0 and close[i - 1] <= bos_hi < close[i]
        bos_dn = (not np.isnan(bos_lo)) and i > 0 and close[i - 1] >= bos_lo > close[i]
        choch_up = bos_up and bias <= 0
        choch_dn = bos_dn and bias >= 0
        if bos_up:
            bias = 1
        if bos_dn:
            bias = -1

        # --- liquidity sweep ---
        pad = SWEEP_WICK_ATR * (atr_v[i] if not np.isnan(atr_v[i]) else 0.0)
        sweep_low = (not np.isnan(last_pl)) and (not pl_swept) and low[i] < (last_pl - pad) and close[i] > last_pl
        sweep_high = (not np.isnan(last_ph)) and (not ph_swept) and high[i] > (last_ph + pad) and close[i] < last_ph

        if sweep_low:
            pl_swept = True
            armed_long = True
            arm_low_ref = low[i]
            arm_long_bar = i
        if sweep_high:
            ph_swept = True
            armed_short = True
            arm_high_ref = high[i]
            arm_short_bar = i

        # --- confirm ---
        bull_confirm = armed_long and (bos_up or (close[i] > open_[i] and i > 0 and close[i] > high[i - 1]))
        bear_confirm = armed_short and (bos_dn or (close[i] < open_[i] and i > 0 and close[i] < low[i - 1]))

        sell = bear_confirm
        buy = bull_confirm and not sell

        ok_gap = (last_sig_bar is None) or (i - last_sig_bar >= MIN_SIGNAL_GAP_BARS)
        buy = buy and ok_gap
        sell = sell and ok_gap
        sig = buy or sell
        if sig:
            last_sig_bar = i

        # --- CONVICTION (Pine dashboard'daki 0-100 skorun aynısı; FVG bileşeni
        # bizde olmadığı için nötr/muhafazakar (0.4) sabit kabul edilir) ---
        conv = 0.0
        if sig:
            struct_comp = 1.0 if ((buy and choch_up) or (sell and choch_dn)) else 0.6
            eps = 1e-9
            if buy and not np.isnan(arm_low_ref):
                sweep_depth = min(1.0, max(0.0, (last_pl - arm_low_ref) / (atr_v[i] + eps)))
            elif sell and not np.isnan(arm_high_ref):
                sweep_depth = min(1.0, max(0.0, (arm_high_ref - last_ph) / (atr_v[i] + eps)))
            else:
                sweep_depth = 0.0
            fvg_comp = 0.4  # FVG confluence portu yok, nötr/muhafazakar sabit
            conv = round(100 * (0.4 * struct_comp + 0.35 * sweep_depth + 0.25 * fvg_comp))

        if buy:
            armed_long = False
        if sell:
            armed_short = False

        # --- staleness: onaysız kalan sweep'i belirli bar sonra düşür ---
        if arm_long_bar is not None and (i - arm_long_bar) > ARM_EXPIRE_BARS:
            armed_long = False
        if arm_short_bar is not None and (i - arm_short_bar) > ARM_EXPIRE_BARS:
            armed_short = False

        sweep_low_arr[i] = sweep_low
        sweep_high_arr[i] = sweep_high
        bos_up_arr[i] = bos_up
        bos_dn_arr[i] = bos_dn
        choch_up_arr[i] = choch_up
        choch_dn_arr[i] = choch_dn
        buy_arr[i] = buy
        sell_arr[i] = sell
        conv_arr[i] = conv

    out["sweep_low"] = sweep_low_arr
    out["sweep_high"] = sweep_high_arr
    out["bos_up"] = bos_up_arr
    out["bos_dn"] = bos_dn_arr
    out["choch_up"] = choch_up_arr
    out["choch_dn"] = choch_dn_arr
    out["buy"] = buy_arr
    out["sell"] = sell_arr
    out["conv"] = conv_arr
    return out


# ─────────────────────────────────────────────────────────────────────────
#  SEMBOL TARAMA
# ─────────────────────────────────────────────────────────────────────────
def scan_symbol(symbol: str) -> None:
    try:
        df = get_klines(symbol, INTERVAL, KLINES_LIMIT)
        if len(df) < (2 * PIV_LEN + 30):
            return
        out = compute_signals(df, PIV_LEN)
        last = out.iloc[-1]
        bar_key = str(last["close_time"])

        if LAST_ALERTED_BAR.get(symbol) == bar_key:
            return  # bu bar için zaten mesaj atıldı

        if last["buy"]:
            tag = "CHoCH" if last["choch_up"] else "BOS"
            conv = last["conv"]
            msg = (
                f"🟢 <b>BUY — ICT Liquidity Sweep</b>\n"
                f"Sembol: <b>{symbol}</b> | TF: {INTERVAL}\n"
                f"Yapı: {tag} (sweep + onay)\n"
                f"Conviction: {conv:.0f}/100\n"
                f"Kapanış: {last['close']:.6g}\n"
                f"Mum kapanış: {last['close_time']}"
            )
            if conv >= CONVICTION_MIN:
                log.info(msg.replace("\n", " | "))
                send_telegram(msg)
            else:
                log.info("%s | BUY sinyali ama conviction düşük (%.0f < %d), Telegram'a atlanmadı.", symbol, conv, CONVICTION_MIN)
            LAST_ALERTED_BAR[symbol] = bar_key

        elif last["sell"]:
            tag = "CHoCH" if last["choch_dn"] else "BOS"
            conv = last["conv"]
            msg = (
                f"🔴 <b>SELL — ICT Liquidity Sweep</b>\n"
                f"Sembol: <b>{symbol}</b> | TF: {INTERVAL}\n"
                f"Yapı: {tag} (sweep + onay)\n"
                f"Conviction: {conv:.0f}/100\n"
                f"Kapanış: {last['close']:.6g}\n"
                f"Mum kapanış: {last['close_time']}"
            )
            if conv >= CONVICTION_MIN:
                log.info(msg.replace("\n", " | "))
                send_telegram(msg)
            else:
                log.info("%s | SELL sinyali ama conviction düşük (%.0f < %d), Telegram'a atlanmadı.", symbol, conv, CONVICTION_MIN)
            LAST_ALERTED_BAR[symbol] = bar_key

        elif last["sweep_low"] or last["sweep_high"]:
            # sadece log — henüz onay yok, henüz sinyal değil
            direction = "SSL (destek) sweep" if last["sweep_low"] else "BSL (direnç) sweep"
            log.info("%s | %s tespit edildi, onay bekleniyor.", symbol, direction)

    except Exception as e:
        log.error("Sembol taramasında hata (%s): %s", symbol, e)


def run_scan_cycle() -> None:
    log.info("Tarama döngüsü başlıyor...")
    try:
        symbols = get_usdt_perpetual_symbols()
        volumes = get_24h_volume_map()
    except Exception as e:
        log.error("Sembol/hacim listesi alınamadı: %s", e)
        return

    filtered = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    log.info("Taranacak sembol sayısı: %d (hacim filtresi >= %s USDT)", len(filtered), f"{MIN_VOLUME_USDT:,}")

    for sym in filtered:
        scan_symbol(sym)
        time.sleep(SLEEP_BETWEEN_SYMBOLS)

    log.info("Tarama döngüsü tamamlandı.")


def main() -> None:
    log.info("ICT Liquidity Sweep Scanner başlatıldı. TF=%s, PIV_LEN=%d", INTERVAL, PIV_LEN)
    while True:
        start = time.time()
        run_scan_cycle()
        elapsed = time.time() - start
        sleep_for = max(5.0, SCAN_INTERVAL_SECONDS - elapsed)
        log.info("Sonraki tarama %.0f saniye sonra.", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
