"""
MONEY TRADER - FIBO TRADE Backtest
Strateji: 200 barlik Tepe/Dip + Dokunma + Onay Mumu (DIPTEN AL / TEPEDEN SAT)
Giris: SADECE onay mumu (dipAL / tepeSAT) olustugunda, o mumun close'unda
SL/TP: Tetik mumunun low/high referans alinarak -%1 / +%4
Zaman dilimi: 15 dakika
Coin evreni: Binance Futures USDT-M perpetual, 24s hacim >= 3M USDT
Donem: Son 30 gun
Cikti: CSV + Telegram ozet
"""

import requests
import pandas as pd
import time
from datetime import datetime, timedelta, timezone

# ─────────────── AYARLAR ───────────────
LOOKBACK = 200
MIN_BAR_ARASI = 30
TOLERANS_PCT = 1.0
SL_PCT = 0.01     # tetik mumunun low/high'inin %1 asagisi/yukarisi
TP_PCT = 0.04     # tetik mumunun low/high'inin %4 yukarisi/asagisi
MIN_VOLUME_USDT = 3_000_000
TIMEFRAME = "15m"
DAYS_BACK = 30

BINANCE_FAPI = "https://fapi.binance.com"

TELEGRAM_TOKEN = "BURAYA_TOKEN"
TELEGRAM_CHAT_ID = "BURAYA_CHAT_ID"


def get_usdt_perpetual_symbols():
    url = f"{BINANCE_FAPI}/fapi/v1/exchangeInfo"
    r = requests.get(url, timeout=15)
    data = r.json()
    symbols = []
    for s in data["symbols"]:
        if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING":
            symbols.append(s["symbol"])
    return symbols


def get_24h_volumes():
    url = f"{BINANCE_FAPI}/fapi/v1/ticker/24hr"
    r = requests.get(url, timeout=15)
    data = r.json()
    vol_map = {}
    for d in data:
        try:
            vol_map[d["symbol"]] = float(d["quoteVolume"])
        except (KeyError, ValueError):
            continue
    return vol_map


def get_klines(symbol, interval, start_ms, end_ms):
    all_klines = []
    url = f"{BINANCE_FAPI}/fapi/v1/klines"
    limit = 1500
    cur = start_ms
    while cur < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cur,
            "endTime": end_ms,
            "limit": limit,
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
        except Exception:
            break
        if not isinstance(data, list) or len(data) == 0:
            break
        all_klines.extend(data)
        cur = data[-1][0] + 1
        time.sleep(0.05)
        if len(data) < limit:
            break
    if not all_klines:
        return None
    df = pd.DataFrame(all_klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def find_signals(df):
    """Pine script'teki dipAL / tepeSAT mantiginin birebir Python karsiligi."""
    n = len(df)
    grid_yuksek = df["high"].rolling(LOOKBACK, min_periods=LOOKBACK).max()
    grid_dusuk = df["low"].rolling(LOOKBACK, min_periods=LOOKBACK).min()
    aralik = grid_yuksek - grid_dusuk
    tolerans = aralik * (TOLERANS_PCT / 100.0)

    dip_touch = df["low"] <= (grid_dusuk + tolerans)
    tepe_touch = df["high"] >= (grid_yuksek - tolerans)

    son_al_bar = -999
    son_sat_bar = -999
    al_bekliyor = False
    sat_bekliyor = False

    signals = []

    for i in range(n):
        if pd.isna(aralik.iloc[i]) or aralik.iloc[i] <= 0:
            continue

        if dip_touch.iloc[i] and (i - son_al_bar > MIN_BAR_ARASI):
            al_bekliyor = True
        if tepe_touch.iloc[i] and (i - son_sat_bar > MIN_BAR_ARASI):
            sat_bekliyor = True

        close = df["close"].iloc[i]
        open_ = df["open"].iloc[i]

        dip_al = al_bekliyor and (close > open_) and (i - son_al_bar > MIN_BAR_ARASI)
        tepe_sat = sat_bekliyor and (close < open_) and (i - son_sat_bar > MIN_BAR_ARASI)

        if dip_al:
            al_bekliyor = False
            son_al_bar = i
            signals.append({"index": i, "type": "AL"})
        if tepe_sat:
            sat_bekliyor = False
            son_sat_bar = i
            signals.append({"index": i, "type": "SAT"})

    return signals


def simulate_trade(df, sig_index, sig_type):
    """Sinyal mumunun close'unda giris. SL/TP tetik mumunun low/high'i referans alinarak."""
    if sig_index + 1 >= len(df):
        return None

    trigger_low = df["low"].iloc[sig_index]
    trigger_high = df["high"].iloc[sig_index]

    if sig_type == "AL":
        sl = trigger_low * (1 - SL_PCT)
        tp = trigger_low * (1 + TP_PCT)
        direction = 1
    else:
        sl = trigger_high * (1 + SL_PCT)
        tp = trigger_high * (1 - TP_PCT)
        direction = -1

    for j in range(sig_index + 1, len(df)):
        low = df["low"].iloc[j]
        high = df["high"].iloc[j]

        if direction == 1:
            hit_sl = low <= sl
            hit_tp = high >= tp
        else:
            hit_sl = high >= sl
            hit_tp = low <= tp

        # Ayni mumda ikisi de tetiklenirse muhafazakar davranilir: SL kabul edilir
        if hit_sl:
            return {"result": "SL", "exit_price": sl, "bars_held": j - sig_index}
        elif hit_tp:
            return {"result": "TP", "exit_price": tp, "bars_held": j - sig_index}

    return {"result": "OPEN", "exit_price": df["close"].iloc[-1], "bars_held": len(df) - 1 - sig_index}


def main():
    print("Semboller ve hacimler cekiliyor...")
    symbols = get_usdt_perpetual_symbols()
    volumes = get_24h_volumes()

    filtered_symbols = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    print(f"{len(filtered_symbols)} coin hacim filtresini gecti.")

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).timestamp() * 1000)

    all_trades = []

    for idx, symbol in enumerate(filtered_symbols):
        print(f"[{idx + 1}/{len(filtered_symbols)}] {symbol} isleniyor...")
        df = get_klines(symbol, TIMEFRAME, start_ms, end_ms)
        if df is None or len(df) < LOOKBACK + MIN_BAR_ARASI + 5:
            continue

        signals = find_signals(df)

        for sig in signals:
            trade = simulate_trade(df, sig["index"], sig["type"])
            if trade is None:
                continue
            all_trades.append({
                "symbol": symbol,
                "type": sig["type"],
                "signal_time": df["open_time"].iloc[sig["index"]],
                "entry_price": df["close"].iloc[sig["index"]],
                "result": trade["result"],
                "exit_price": trade["exit_price"],
                "bars_held": trade["bars_held"],
            })

        time.sleep(0.1)

    if not all_trades:
        print("Hicbir sinyal bulunamadi.")
        return

    result_df = pd.DataFrame(all_trades)
    csv_path = "fibo_backtest_sonuclari.csv"
    result_df.to_csv(csv_path, index=False)

    total = len(result_df)
    tp_count = (result_df["result"] == "TP").sum()
    sl_count = (result_df["result"] == "SL").sum()
    open_count = (result_df["result"] == "OPEN").sum()
    closed = tp_count + sl_count
    win_rate = (tp_count / closed * 100) if closed > 0 else 0

    al_df = result_df[result_df["type"] == "AL"]
    sat_df = result_df[result_df["type"] == "SAT"]

    def winrate(sub_df):
        c = sub_df[sub_df["result"].isin(["TP", "SL"])]
        if len(c) == 0:
            return 0, 0, 0
        tp = (c["result"] == "TP").sum()
        return tp, len(c), (tp / len(c) * 100)

    al_tp, al_closed, al_wr = winrate(al_df)
    sat_tp, sat_closed, sat_wr = winrate(sat_df)

    summary = (
        f"FIBO TRADE Backtest Sonucu ({DAYS_BACK} gun, {TIMEFRAME})\n\n"
        f"Toplam sinyal: {total}\n"
        f"Kapanan: {closed} | Acik: {open_count}\n"
        f"TP: {tp_count} | SL: {sl_count}\n"
        f"Genel basari orani: %{win_rate:.1f}\n\n"
        f"DIPTEN AL -> {al_tp}/{al_closed} basarili (%{al_wr:.1f})\n"
        f"TEPEDEN SAT -> {sat_tp}/{sat_closed} basarili (%{sat_wr:.1f})\n\n"
        f"SL: tetik mumu low/high -%{SL_PCT * 100:.0f}\n"
        f"TP: tetik mumu low/high +%{TP_PCT * 100:.0f}\n"
        f"(Risk/odul orani bu ayarla yaklasik 1:{TP_PCT / SL_PCT:.0f})"
    )

    print(summary)

    if TELEGRAM_TOKEN != "BURAYA_TOKEN":
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": summary},
                timeout=10,
            )
        except Exception as e:
            print(f"Telegram gonderim hatasi: {e}")


if __name__ == "__main__":
    main()
