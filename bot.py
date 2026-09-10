"""
Bar Stallone Support/Resistance Scanner
-----------------------------------------------
Pine kaynağı: "Support/Resistance" (BarStallone / @christofferka güncellemesi)

Mantık (indikatörle birebir, ama repaint riski taşıyan request.security(lookahead_on)
KULLANILMADAN, tek zaman diliminde ve sadece kapanmış mumlar üzerinden hesaplanır):

  RSI(9) < 25  AND  CMO_custom > 50   AND  yakın pivot-low mevcut  -> DESTEK sinyali (sup)
  RSI(9) > 75  AND  CMO_custom < -50  AND  yakın pivot-high mevcut -> DİRENÇ sinyali (res)

  xup / xdown  : sup/res tetiklendiğinde güncellenen "yapışkan" (sticky) seviyeler
                 (Pine'daki yeşil/turuncu çizgilerin karşılığı)

  Zone oluşumu (xup/xdown değişimi): SADECE LOG'a yazılır, Telegram'a gitmez.
  Telegram alarmı SADECE şu iki durumda gider:
    - Zone'dan +REACTION_PCT (destek) / -REACTION_PCT (direnç) hareket -> ✅ TEYİT ALDI
    - Zone'un tersi yönünde INVALIDATE_PCT kırılırsa                    -> ❌ GEÇERSİZ

Kapsam: Binance Futures USDT-M perpetual, 24s hacim >= MIN_VOLUME_USDT
Zaman dilimi (mum): 15 dakika (kullanıcı seçimi)
Tarama sıklığı (döngü): 5 dakika (SCAN_INTERVAL_SECONDS, diğer botlarla aynı desen)
Pivot uzunluğu (len5): 2 (kullanıcı seçimi - orijinal, sık sinyal)
"""

import os
import time
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

# ─────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────
BINANCE_FAPI = "https://fapi.binance.com"
INTERVAL = "15m"                 # kullanıcı seçimi
PIVOT_LEN = 2                    # kullanıcı seçimi (len5)
RSI_LEN = 9
MIN_VOLUME_USDT = 3_000_000      # önceki botlarla tutarlı hacim filtresi
KLINES_LIMIT = 100               # RSI/HMA/pivot ısınma payı için yeterli (200 gereksizdi, ağırlığı gereksiz artırıyordu)
SCAN_INTERVAL_SECONDS = int(os.environ.get("SCAN_INTERVAL_SECONDS", 60 * 5))  # her 5 dakikada bir tara (diğer botlarla aynı desen), mum periyodu ayrı (INTERVAL=15m)
REQUEST_TIMEOUT = 10
SLEEP_BETWEEN_SYMBOLS = float(os.environ.get("SLEEP_BETWEEN_SYMBOLS", "0.25"))  # Binance rate-limit'e nazik davran
WEIGHT_SOFT_LIMIT = int(os.environ.get("WEIGHT_SOFT_LIMIT", "2000"))  # 1 dakikalık ağırlık limiti ~2400, buna yaklaşınca dur
MAX_RETRIES = 5

REACTION_PCT = float(os.environ.get("REACTION_PCT", "0.05"))      # zone'dan teyit için gereken hareket (varsayılan %5)
INVALIDATE_PCT = float(os.environ.get("INVALIDATE_PCT", "0.02"))  # ters yönde geçersizlik eşiği (varsayılan %2)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bar_stallone_scanner")

# symbol -> {
#   "tf1": float or None, "tf2": float or None,             (son bilinen sticky seviyeler)
#   "sup_zone": {"level": float, "status": "pending"/"confirmed"/"invalidated"} or None,
#   "res_zone": {...} or None,
# }
LAST_STATE: dict[str, dict] = {}


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
    """
    Tüm Binance Futures GET isteklerinin geçtiği tek nokta.
    - 429 (Too Many Requests) veya 418 (IP ban) geldiğinde Retry-After'a göre
      bekleyip tekrar dener (exponential backoff ile).
    - Yanıttaki X-MBX-USED-WEIGHT-1M header'ını izler; limite yaklaşılırsa
      bir sonraki dakikaya kadar bekler.
    """
    url = f"{BINANCE_FAPI}{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        r = _session.get(url, params=params, timeout=REQUEST_TIMEOUT)

        if r.status_code == 200:
            used_weight = r.headers.get("X-MBX-USED-WEIGHT-1M")
            if used_weight is not None and int(used_weight) >= WEIGHT_SOFT_LIMIT:
                log.warning(
                    "Kullanılan ağırlık %s/%s soft limite yaklaştı, 60sn bekleniyor.",
                    used_weight, WEIGHT_SOFT_LIMIT,
                )
                time.sleep(60)
            return r

        if r.status_code in (429, 418):
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else min(60, 2 ** attempt)
            log.warning(
                "%s: %s alındı (deneme %d/%d), %.0f sn bekleniyor.",
                path, r.status_code, attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)
            continue

        r.raise_for_status()

    raise RuntimeError(f"{path} için {MAX_RETRIES} denemeden sonra rate-limit aşılamadı.")


def get_usdt_perpetual_symbols() -> list[str]:
    """USDT-M perpetual, TRADING durumundaki tüm semboller."""
    r = binance_get("/fapi/v1/exchangeInfo")
    data = r.json()
    symbols = []
    for s in data.get("symbols", []):
        if (
            s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
        ):
            symbols.append(s["symbol"])
    return symbols


def get_24h_volume_map() -> dict[str, float]:
    """symbol -> 24s quoteVolume (USDT)"""
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
    # Binance'in son satırı henüz KAPANMAMIŞ (oluşmakta olan) mum olabilir -> at.
    now_ms = int(time.time() * 1000)
    if raw and raw[-1][6] > now_ms:  # close_time > şimdi -> mum hâlâ açık
        df = df.iloc[:-1].reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────────
#  İNDİKATÖR HESAPLARI (Pine koduna birebir sadık)
# ─────────────────────────────────────────────────────────────────────────
def wma(series: pd.Series, length: int) -> pd.Series:
    weights = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def hma(series: pd.Series, length: int) -> pd.Series:
    half = max(1, int(length / 2))
    sqrt_len = max(1, int(round(np.sqrt(length))))
    diff = 2 * wma(series, half) - wma(series, length)
    return wma(diff, sqrt_len)


def rma(series: pd.Series, length: int) -> pd.Series:
    alpha = 1.0 / length
    return series.ewm(alpha=alpha, adjust=False).mean()


def wilder_rsi(close: pd.Series, length: int = 9) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = rma(up, length)
    roll_down = rma(down, length)
    rs = roll_up / roll_down
    rsi = np.where(roll_down == 0, 100.0, np.where(roll_up == 0, 0.0, 100 - 100 / (1 + rs)))
    return pd.Series(rsi, index=close.index)


def rolling_dev(series: pd.Series, length: int) -> pd.Series:
    """Pine ta.dev: ortalama mutlak sapma (mean absolute deviation)."""
    return series.rolling(length).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    # --- RSI(9) ---
    out["rsi"] = wilder_rsi(out["close"], RSI_LEN)

    # --- Özel HMA tabanlı CMO ---
    src1 = hma(out["open"], 5).shift(1)   # Pine: ta.hma(open,5)[1]  (built-in lag düzeltmesi)
    src2 = hma(out["close"], 12)
    momm1 = src1.diff()
    momm2 = src2.diff()
    m1 = np.where(momm1 >= momm2, momm1, 0.0)
    m2 = np.where(momm1 >= momm2, 0.0, -momm1)
    # length1 = 1 -> sum(x,1) = x, ek işlem gerekmiyor
    sm1 = pd.Series(m1, index=out.index)
    sm2 = pd.Series(m2, index=out.index)
    out["cmo"] = 100 * (sm1 - sm2) / (sm1 + sm2)

    # --- Pivot (Pine'daki backward-only highest/lowest + dev tekniği) ---
    h = out["high"].rolling(PIVOT_LEN).max()
    h_dev = rolling_dev(h, PIVOT_LEN)
    h1 = np.where(h_dev != 0, np.nan, h)
    out["hpivot"] = pd.Series(h1, index=out.index).ffill()

    l = out["low"].rolling(PIVOT_LEN).min()
    l_dev = rolling_dev(l, PIVOT_LEN)
    l1 = np.where(l_dev != 0, np.nan, l)
    out["lpivot"] = pd.Series(l1, index=out.index).ffill()

    # --- sup / res ---
    out["sup"] = (out["rsi"] < 25) & (out["cmo"] > 50) & out["lpivot"].notna()
    out["res"] = (out["rsi"] > 75) & (out["cmo"] < -50) & out["hpivot"].notna()

    # --- xup / xdown (sticky çizgi seviyeleri) ---
    xup = np.zeros(len(out))
    xdown = np.zeros(len(out))
    prev_up = 0.0
    prev_down = 0.0
    sup_vals = out["sup"].to_numpy()
    res_vals = out["res"].to_numpy()
    low_vals = out["low"].to_numpy()
    high_vals = out["high"].to_numpy()
    for i in range(len(out)):
        if sup_vals[i]:
            prev_up = low_vals[i]
        xup[i] = prev_up
        if res_vals[i]:
            prev_down = high_vals[i]
        xdown[i] = prev_down
    out["tf1"] = xup   # destek (yeşil) seviyesi
    out["tf2"] = xdown  # direnç (turuncu) seviyesi

    return out


# ─────────────────────────────────────────────────────────────────────────
#  SEMBOL TARAMA
# ─────────────────────────────────────────────────────────────────────────
def scan_symbol(symbol: str) -> None:
    try:
        df = get_klines(symbol, INTERVAL, KLINES_LIMIT)
        if len(df) < 60:
            return
        df = compute_signals(df)

        last = df.iloc[-1]
        last_close = float(last["close"])
        prev_state = LAST_STATE.get(
            symbol, {"tf1": None, "tf2": None, "sup_zone": None, "res_zone": None}
        )

        new_tf1 = last["tf1"]
        new_tf2 = last["tf2"]

        tf1_changed = prev_state["tf1"] is not None and new_tf1 != prev_state["tf1"]
        tf2_changed = prev_state["tf2"] is not None and new_tf2 != prev_state["tf2"]
        first_run = prev_state["tf1"] is None and prev_state["tf2"] is None

        sup_zone = prev_state["sup_zone"]
        res_zone = prev_state["res_zone"]

        # ── İlk çalıştırmada state'i sadece kaydet, spam alarm atma ──
        if not first_run:
            # --- Önce MEVCUT pending zone'ları kontrol et (yeni sinyal onları ezmeden önce) ---
            if sup_zone is not None and sup_zone["status"] == "pending":
                level = sup_zone["level"]
                if last_close >= level * (1 + REACTION_PCT):
                    move_pct = (last_close / level - 1) * 100
                    msg = (
                        f"✅ <b>DESTEK TEYİT ALDI</b>\n"
                        f"Sembol: <b>{symbol}</b> | TF: {INTERVAL}\n"
                        f"Zone: {level:.6g} -> Kapanış: {last_close:.6g} (+%{move_pct:.1f})\n"
                        f"Mum kapanış: {last['close_time']}"
                    )
                    log.info(msg.replace("\n", " | "))
                    send_telegram(msg)
                    sup_zone["status"] = "confirmed"
                elif last_close <= level * (1 - INVALIDATE_PCT):
                    msg = (
                        f"❌ <b>DESTEK GEÇERSİZ (kırıldı)</b>\n"
                        f"Sembol: <b>{symbol}</b> | TF: {INTERVAL}\n"
                        f"Zone: {level:.6g} -> Kapanış: {last_close:.6g}\n"
                        f"Mum kapanış: {last['close_time']}"
                    )
                    log.info(msg.replace("\n", " | "))
                    send_telegram(msg)
                    sup_zone["status"] = "invalidated"
                else:
                    move_pct = (last_close / level - 1) * 100
                    log.info(
                        "%s | DESTEK pending | zone=%.6g kapanış=%.6g hareket=%+.2f%% (hedef +%%%.0f)",
                        symbol, level, last_close, move_pct, REACTION_PCT * 100,
                    )

            if res_zone is not None and res_zone["status"] == "pending":
                level = res_zone["level"]
                if last_close <= level * (1 - REACTION_PCT):
                    move_pct = (1 - last_close / level) * 100
                    msg = (
                        f"✅ <b>DİRENÇ TEYİT ALDI</b>\n"
                        f"Sembol: <b>{symbol}</b> | TF: {INTERVAL}\n"
                        f"Zone: {level:.6g} -> Kapanış: {last_close:.6g} (-%{move_pct:.1f})\n"
                        f"Mum kapanış: {last['close_time']}"
                    )
                    log.info(msg.replace("\n", " | "))
                    send_telegram(msg)
                    res_zone["status"] = "confirmed"
                elif last_close >= level * (1 + INVALIDATE_PCT):
                    msg = (
                        f"❌ <b>DİRENÇ GEÇERSİZ (kırıldı)</b>\n"
                        f"Sembol: <b>{symbol}</b> | TF: {INTERVAL}\n"
                        f"Zone: {level:.6g} -> Kapanış: {last_close:.6g}\n"
                        f"Mum kapanış: {last['close_time']}"
                    )
                    log.info(msg.replace("\n", " | "))
                    send_telegram(msg)
                    res_zone["status"] = "invalidated"
                else:
                    move_pct = (1 - last_close / level) * 100
                    log.info(
                        "%s | DİRENÇ pending | zone=%.6g kapanış=%.6g hareket=%+.2f%% (hedef -%%%.0f)",
                        symbol, level, last_close, move_pct, REACTION_PCT * 100,
                    )

            # --- SONRA yeni destek/direnç çizgisi oluştu mu bak -> zone'u "pending" aç, SADECE LOG ---
            if tf1_changed and new_tf1 > 0:
                if sup_zone is not None and sup_zone["status"] == "pending":
                    log.info(
                        "%s | DESTEK zone yeni sinyalle değişti, önceki (%.6g) sonuçlanmadan kapandı.",
                        symbol, sup_zone["level"],
                    )
                log.info(
                    "%s | DESTEK sinyali oluştu (henüz teyit yok) | seviye=%.6g RSI=%.1f CMO=%.1f",
                    symbol, new_tf1, last["rsi"], last["cmo"],
                )
                sup_zone = {"level": new_tf1, "status": "pending"}

            if tf2_changed and new_tf2 > 0:
                if res_zone is not None and res_zone["status"] == "pending":
                    log.info(
                        "%s | DİRENÇ zone yeni sinyalle değişti, önceki (%.6g) sonuçlanmadan kapandı.",
                        symbol, res_zone["level"],
                    )
                log.info(
                    "%s | DİRENÇ sinyali oluştu (henüz teyit yok) | seviye=%.6g RSI=%.1f CMO=%.1f",
                    symbol, new_tf2, last["rsi"], last["cmo"],
                )
                res_zone = {"level": new_tf2, "status": "pending"}

        LAST_STATE[symbol] = {
            "tf1": new_tf1,
            "tf2": new_tf2,
            "sup_zone": sup_zone,
            "res_zone": res_zone,
        }

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
    log.info("Bar Stallone S/R Scanner başlatıldı. TF=%s, PIVOT_LEN=%d", INTERVAL, PIVOT_LEN)
    while True:
        start = time.time()
        run_scan_cycle()
        elapsed = time.time() - start
        sleep_for = max(5.0, SCAN_INTERVAL_SECONDS - elapsed)
        log.info("Sonraki tarama %.0f saniye sonra.", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
