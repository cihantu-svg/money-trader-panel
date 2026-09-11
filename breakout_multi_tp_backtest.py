"""
ICT Liquidity Sweep & Structure - BACKTEST
-----------------------------------------------
Tek seferlik çalıştırılır (shell'den), canlı bot DEĞİLDİR.

Yapılan iş:
  1) Binance Futures USDT-M perpetual (24s hacim >= MIN_VOLUME_USDT) sembollerini tara.
  2) Her sembol için son ~LOOKBACK_BARS mumu çek (varsayılan 1500 bar @ 15m ~ 15.6 gün).
  3) scanner.py'deki AYNI compute_signals() fonksiyonuyla BUY/SELL sinyallerini üret
     (canlı botla birebir aynı mantık, tutarsızlık olmasın diye kod tekrarlanmadı).
  4) Her sinyalin ardından HORIZON_BARS bar içinde fiyatın nereye gittiğini ölç
     (kapanıştaki % değişim + en iyi/en kötü % hareket).
  5) Sonuçları CSV'ye yaz, özet istatistiği ve CSV dosyasını Telegram'a gönder.

Kullanım:
  python backtest.py
  (env ile ayarlanabilir: LOOKBACK_BARS, HORIZON_BARS, MIN_VOLUME_USDT, PIV_LEN, ...)
"""

import os
import time
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from scanner import (
    binance_get,
    get_usdt_perpetual_symbols,
    get_24h_volume_map,
    compute_signals,
    PIV_LEN,
    INTERVAL,
    MIN_VOLUME_USDT,
    SLEEP_BETWEEN_SYMBOLS,
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ict_backtest")

LOOKBACK_BARS = int(os.environ.get("LOOKBACK_BARS", "1500"))   # Binance futures klines max limit
HORIZON_BARS = int(os.environ.get("HORIZON_BARS", "8"))        # sinyal sonrası kaç bar izlenecek (8*15dk=2sa)
OUTPUT_CSV = os.environ.get("OUTPUT_CSV", "ict_backtest_results.csv")
REQUEST_TIMEOUT = 15


def get_klines_full(symbol: str, interval: str, limit: int) -> pd.DataFrame:
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


def evaluate_signal(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray, idx: int, direction: str, horizon: int):
    """idx: sinyal barının indexi. direction: 'buy' ya da 'sell'."""
    n = len(closes)
    end = min(idx + horizon, n - 1)
    if end <= idx:
        return None  # yeterli ileri veri yok
    entry = closes[idx]
    fwd_close = closes[end]
    window_high = highs[idx + 1:end + 1].max()
    window_low = lows[idx + 1:end + 1].min()

    if direction == "buy":
        close_ret = (fwd_close / entry - 1) * 100
        best_ret = (window_high / entry - 1) * 100
        worst_ret = (window_low / entry - 1) * 100
    else:
        close_ret = (1 - fwd_close / entry) * 100
        best_ret = (1 - window_low / entry) * 100
        worst_ret = (1 - window_high / entry) * 100

    return round(close_ret, 3), round(best_ret, 3), round(worst_ret, 3)


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


def main() -> None:
    log.info("Backtest başlıyor. TF=%s PIV_LEN=%d LOOKBACK_BARS=%d HORIZON_BARS=%d", INTERVAL, PIV_LEN, LOOKBACK_BARS, HORIZON_BARS)

    symbols = get_usdt_perpetual_symbols()
    volumes = get_24h_volume_map()
    filtered = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    log.info("Taranacak sembol sayısı: %d", len(filtered))

    rows = []
    for n_done, symbol in enumerate(filtered, 1):
        try:
            df = get_klines_full(symbol, INTERVAL, LOOKBACK_BARS)
            if len(df) < (2 * PIV_LEN + 30):
                continue
            out = compute_signals(df, PIV_LEN)
            closes = out["close"].to_numpy()
            highs = out["high"].to_numpy()
            lows = out["low"].to_numpy()

            sig_idx = out.index[out["buy"] | out["sell"]].tolist()
            for idx in sig_idx:
                direction = "buy" if out.loc[idx, "buy"] else "sell"
                tag = "CHoCH" if (out.loc[idx, "choch_up"] or out.loc[idx, "choch_dn"]) else "BOS"
                ev = evaluate_signal(closes, highs, lows, idx, direction, HORIZON_BARS)
                rows.append({
                    "symbol": symbol,
                    "direction": direction,
                    "structure": tag,
                    "signal_time": out.loc[idx, "close_time"],
                    "signal_close": out.loc[idx, "close"],
                    "fwd_close_ret_pct": ev[0] if ev else None,
                    "best_ret_pct": ev[1] if ev else None,
                    "worst_ret_pct": ev[2] if ev else None,
                    "has_forward_data": ev is not None,
                })
        except Exception as e:
            log.error("Backtest hata (%s): %s", symbol, e)
        finally:
            time.sleep(SLEEP_BETWEEN_SYMBOLS)

        if n_done % 25 == 0:
            log.info("İlerleme: %d/%d sembol tarandı, şimdiye kadar %d sinyal.", n_done, len(filtered), len(rows))

    if not rows:
        log.warning("Hiç sinyal bulunamadı.")
        send_telegram_text("📊 ICT Liquidity Sweep Backtest tamamlandı.\nHiç sinyal bulunamadı (LOOKBACK/PIV_LEN ayarlarını gözden geçir).")
        return

    result_df = pd.DataFrame(rows).sort_values("signal_time")
    result_df.to_csv(OUTPUT_CSV, index=False)
    log.info("CSV yazıldı: %s (%d satır)", OUTPUT_CSV, len(result_df))

    evaluated = result_df[result_df["has_forward_data"]]
    total = len(result_df)
    n_buy = (result_df["direction"] == "buy").sum()
    n_sell = (result_df["direction"] == "sell").sum()

    def hit_rate(df, thresh):
        if len(df) == 0:
            return float("nan")
        return (df["best_ret_pct"] >= thresh).mean() * 100

    summary = (
        f"📊 <b>ICT Liquidity Sweep Backtest Sonucu</b>\n"
        f"TF: {INTERVAL} | PIV_LEN: {PIV_LEN} | Horizon: {HORIZON_BARS} bar\n"
        f"Sembol sayısı: {len(filtered)}\n"
        f"Toplam sinyal: {total}  (BUY: {n_buy} / SELL: {n_sell})\n"
        f"Ortalama kapanış getirisi: %{evaluated['fwd_close_ret_pct'].mean():.2f}\n"
        f"En iyi hareket ort.: %{evaluated['best_ret_pct'].mean():.2f}\n"
        f"En kötü hareket ort.: %{evaluated['worst_ret_pct'].mean():.2f}\n"
        f"+%2 hedefe ulaşma oranı: %{hit_rate(evaluated, 2):.1f}\n"
        f"+%5 hedefe ulaşma oranı: %{hit_rate(evaluated, 5):.1f}\n"
    )
    log.info(summary.replace("\n", " | "))
    send_telegram_text(summary)
    send_telegram_document(OUTPUT_CSV, caption="ICT Liquidity Sweep - ham backtest sonuçları")


if __name__ == "__main__":
    main()
