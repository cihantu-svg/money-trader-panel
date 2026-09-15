import os
import time
import csv
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone

# ============================== AYARLAR ==============================
BASE_URL = "https://fapi.binance.com"
INTERVAL = "15m"
LOOKBACK_DAYS = 30                 # Geriye dönük taranacak gün sayısı
FIBO_LOOKBACK = 200                # Fibo alanı için bar sayısı
MIN_CANDLE_BODY_PCT = 3.0          # Kırılım mumunun gövdesi en az %3
STOP_PCT_FROM_LOW = 3.0            # Kırılım mumunun dibinden %3 aşağısı Stop Loss
TP_PCT_FROM_ENTRY = 7.0            # Giriş fiyatından %7 yukarısı Take Profit
MIN_24H_VOLUME_USDT = 1_000_000    # Likidite filtresi (USDT)
MAX_HOLD_BARS = 300                # Zaman aşımı (~75 saat)
REQUEST_SLEEP = 0.25               # API rate-limit beklemesi (saniye)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

OUTPUT_CSV = "fibo_kirilim_backtest_sonuclari.csv"

# ============================== API & YARDIMCI FONKSİYONLAR ==============================

def get_usdt_perpetual_symbols():
    url = f"{BASE_URL}/fapi/v1/exchangeInfo"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    symbols = []
    for s in data["symbols"]:
        if (
            s.get("quoteAsset") == "USDT"
            and s.get("contractType") == "PERPETUAL"
            and s.get("status") == "TRADING"
        ):
            symbols.append(s["symbol"])
    return symbols


def filter_by_liquidity(symbols):
    url = f"{BASE_URL}/fapi/v1/ticker/24hr"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    vol_map = {d["symbol"]: float(d.get("quoteVolume", 0)) for d in data}

    liquid = [s for s in symbols if vol_map.get(s, 0) >= MIN_24H_VOLUME_USDT]
    print(f"[i] Toplam {len(symbols)} sembolden likidite filtresini geçen: {len(liquid)}")
    return liquid


def get_klines(symbol, interval, start_ms, end_ms):
    all_rows = []
    cursor = start_ms
    url = f"{BASE_URL}/fapi/v1/klines"

    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1500,
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            rows = r.json()
        except Exception as e:
            print(f"  [HATA] {symbol} klines çekilemedi: {e}")
            break

        if not rows:
            break

        all_rows.extend(rows)
        cursor = rows[-1][0] + 1
        time.sleep(REQUEST_SLEEP)

        if len(rows) < 1500:
            break

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.reset_index(drop=True)


# ============================== STRATEJİ MANTIĞI ==============================

def find_fibo_breakout_signals(df):
    """
    Son 200 mumun en yükseğini kırıp gövdesi >= %3 olan sinyalleri bulur.
    """
    df["grid_high"] = df["high"].rolling(FIBO_LOOKBACK).max().shift(1)
    df["body_pct"] = (df["close"] - df["open"]).abs() / df["open"] * 100

    signals = []
    for i in range(FIBO_LOOKBACK + 1, len(df)):
        grid_high = df.loc[i, "grid_high"]
        if pd.isna(grid_high):
            continue

        close_i = df.loc[i, "close"]
        prev_close = df.loc[i - 1, "close"]
        body_pct = df.loc[i, "body_pct"]

        if body_pct < MIN_CANDLE_BODY_PCT:
            continue

        breakout_up = close_i > grid_high and prev_close <= grid_high

        if breakout_up:
            signals.append((i, "LONG"))

    return signals


def simulate_trade(df, entry_idx, direction):
    """
    SL = Kırılım mumunun low'unun %3 altı
    TP = Giriş fiyatının %7 üstü
    """
    entry_price = df.loc[entry_idx, "close"]
    candle_low = df.loc[entry_idx, "low"]

    # Stop Loss ve Take Profit Seviyeleri
    sl_price = candle_low * (1 - (STOP_PCT_FROM_LOW / 100.0))
    tp_price = entry_price * (1 + (TP_PCT_FROM_ENTRY / 100.0))

    end_idx = min(entry_idx + MAX_HOLD_BARS, len(df) - 1)

    for j in range(entry_idx + 1, end_idx + 1):
        low_j = df.loc[j, "low"]
        high_j = df.loc[j, "high"]

        hit_sl = low_j <= sl_price
        hit_tp = high_j >= tp_price

        if hit_sl:
            pnl_pct = ((sl_price - entry_price) / entry_price) * 100
            return {
                "exit_time": df.loc[j, "open_time"],
                "exit_price": sl_price,
                "exit_reason": "SL",
                "pnl_pct": pnl_pct,
                "bars_held": j - entry_idx,
            }
        if hit_tp:
            pnl_pct = ((tp_price - entry_price) / entry_price) * 100
            return {
                "exit_time": df.loc[j, "open_time"],
                "exit_price": tp_price,
                "exit_reason": "TP",
                "pnl_pct": pnl_pct,
                "bars_held": j - entry_idx,
            }

    # Zaman Aşımı
    last_close = df.loc[end_idx, "close"]
    pnl_pct = ((last_close - entry_price) / entry_price) * 100
    return {
        "exit_time": df.loc[end_idx, "open_time"],
        "exit_price": last_close,
        "exit_reason": "SURESI_DOLDU",
        "pnl_pct": pnl_pct,
        "bars_held": end_idx - entry_idx,
    }


def send_to_telegram(csv_path, summary_text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[i] Telegram bilgileri tanımlı değil, gönderim atlandı.")
        return
    try:
        msg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(msg_url, data={"chat_id": TELEGRAM_CHAT_ID, "text": summary_text}, timeout=15)

        doc_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        with open(csv_path, "rb") as f:
            requests.post(doc_url, data={"chat_id": TELEGRAM_CHAT_ID}, files={"document": f}, timeout=30)
        print("[i] Telegram'a gönderildi.")
    except Exception as e:
        print(f"[HATA] Telegram gönderimi başarısız: {e}")


# ============================== ANA BACKTEST MANTIĞI ==============================

def run_backtest():
    print("=" * 60)
    print("FIBO ALANI KIRILIM BACKTEST (LONG - %3 SL / %7 TP)")
    print(f"Zaman dilimi: {INTERVAL} | Geriye dönük: {LOOKBACK_DAYS} gün | Fibo lookback: {FIBO_LOOKBACK} bar")
    print(f"Body filtresi: >= %{MIN_CANDLE_BODY_PCT} | Stop Loss: Mum Low -%{STOP_PCT_FROM_LOW} | TP: Entry +%{TP_PCT_FROM_ENTRY}")
    print(f"Likidite filtresi: >= {MIN_24H_VOLUME_USDT:,.0f} USDT (24s hacim)")
    print("=" * 60)

    symbols = get_usdt_perpetual_symbols()
    symbols = filter_by_liquidity(symbols)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_trades = []

    for idx, symbol in enumerate(symbols, 1):
        print(f"[{idx}/{len(symbols)}] {symbol} taranıyor...")
        df = get_klines(symbol, INTERVAL, start_ms, end_ms)
        if df is None or len(df) < FIBO_LOOKBACK + 5:
            print(f"  [!] {symbol} için yeterli veri yok, atlanıyor.")
            continue

        signals = find_fibo_breakout_signals(df)
        if not signals:
            continue

        print(f"  -> {len(signals)} fibo kırılım sinyali bulundu.")

        for sig_idx, direction in signals:
            trade = simulate_trade(df, sig_idx, direction)
            if trade is None:
                continue
            trade["symbol"] = symbol
            trade["direction"] = direction
            trade["entry_time"] = df.loc[sig_idx, "open_time"]
            trade["entry_price"] = df.loc[sig_idx, "close"]
            trade["body_pct"] = df.loc[sig_idx, "body_pct"]
            all_trades.append(trade)

    if not all_trades:
        print("[!] Hiç sinyal/işlem bulunamadı.")
        return

    fieldnames = [
        "symbol", "direction", "entry_time", "entry_price", "body_pct",
        "exit_time", "exit_price", "exit_reason", "pnl_pct", "bars_held",
    ]
    with open(OUTPUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for t in all_trades:
            writer.writerow({k: t[k] for k in fieldnames})

    total = len(all_trades)
    wins = [t for t in all_trades if t["exit_reason"] == "TP"]
    losses = [t for t in all_trades if t["exit_reason"] == "SL"]
    timeouts = [t for t in all_trades if t["exit_reason"] == "SURESI_DOLDU"]

    win_rate = len(wins) / total * 100 if total else 0
    avg_pnl = sum(t["pnl_pct"] for t in all_trades) / total if total else 0
    total_pnl = sum(t["pnl_pct"] for t in all_trades)

    summary_lines = [
        "FIBO ALANI KIRILIM BACKTEST SONUÇLARI",
        f"Toplam işlem: {total} (LONG)",
        f"TP (%7 Kar): {len(wins)} | SL (%3 Zarar): {len(losses)} | Süresi dolan: {len(timeouts)}",
        f"Kazanma oranı: %{win_rate:.1f}",
        f"Ortalama PnL/işlem: %{avg_pnl:.2f}",
        f"Toplam PnL: %{total_pnl:.2f}",
    ]
    summary_text = "\n".join(summary_lines)
    print("\n" + "=" * 60)
    print(summary_text)
    print("=" * 60)
    print(f"\nSonuçlar kaydedildi: {OUTPUT_CSV}")

    send_to_telegram(OUTPUT_CSV, summary_text)


if __name__ == "__main__":
    run_backtest()
