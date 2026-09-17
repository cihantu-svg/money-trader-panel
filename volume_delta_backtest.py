"""
Volume Delta - 3'lu Ardisik Artan Momentum Backtest
Sinyal: Ayni renkte (pozitif ya da negatif delta) ard arda 3 mum, her birinin
        |delta| degeri bir oncekinden BUYUK olmali (belirgin artis).
Giris:  3. mumun close'unda
SL:     AL icin 3'lu serinin ILK mumunun low'u, SAT icin ILK mumunun high'i
TP:     Giristen %5
Delta yaklasimi: 15dk mumun kendi ici taker buy/sell hacim farki
                 delta = 2*taker_buy_volume - volume  (pozitif=yesil, negatif=kirmizi)
Not: Gercek tick/LTF bazli Volume Delta indikatorunun tam esdegeri degildir,
     gecmis veriden hesaplanabilecek en yakin yaklasimdir.
Coin evreni: Binance Futures USDT-M perpetual, 24s hacim >= 1M USDT
Zaman dilimi: 15 dakika, son 30 gun
Cikti: CSV + Telegram ozet
"""

import requests
import pandas as pd
import time
from datetime import datetime, timedelta, timezone

# ─────────────── AYARLAR ───────────────
SERI_UZUNLUK = 3
TP_PCT = 0.05
MIN_VOLUME_USDT = 1_000_000
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
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume", "taker_buy_base"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["delta"] = 2 * df["taker_buy_base"] - df["volume"]
    return df.reset_index(drop=True)


def find_signals(df):
    n = len(df)
    signals = []
    delta = df["delta"].values

    i = SERI_UZUNLUK - 1
    while i < n:
        d0, d1, d2 = delta[i - 2], delta[i - 1], delta[i]

        same_sign = (d0 > 0 and d1 > 0 and d2 > 0) or (d0 < 0 and d1 < 0 and d2 < 0)
        increasing = abs(d0) < abs(d1) < abs(d2)

        if same_sign and increasing:
            sig_type = "AL" if d2 > 0 else "SAT"
            signals.append({
                "index": i,
                "first_index": i - 2,
                "type": sig_type,
            })
            i += 1  # bir sonraki bardan devam, ama trade simulate_trade icinde acik pozisyon kontrolu yapilacak
        else:
            i += 1

    return signals


def simulate_trade(df, sig_index, first_index, sig_type):
    if sig_index + 1 >= len(df):
        return None

    entry_price = df["close"].iloc[sig_index]
    first_low = df["low"].iloc[first_index]
    first_high = df["high"].iloc[first_index]

    if sig_type == "AL":
        sl = first_low
        tp = entry_price * (1 + TP_PCT)
        direction = 1
    else:
        sl = first_high
        tp = entry_price * (1 - TP_PCT)
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

        if hit_sl:
            return {"result": "SL", "exit_price": sl, "exit_index": j}
        elif hit_tp:
            return {"result": "TP", "exit_price": tp, "exit_index": j}

    return {"result": "OPEN", "exit_price": df["close"].iloc[-1], "exit_index": len(df) - 1}


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
        if df is None or len(df) < SERI_UZUNLUK + 5:
            continue

        signals = find_signals(df)

        acik_pozisyon_kadar_bar = -1  # ayni sembolde ust uste trade birikmesin

        for sig in signals:
            if sig["index"] <= acik_pozisyon_kadar_bar:
                continue

            trade = simulate_trade(df, sig["index"], sig["first_index"], sig["type"])
            if trade is None:
                continue

            all_trades.append({
                "symbol": symbol,
                "type": sig["type"],
                "signal_time": df["open_time"].iloc[sig["index"]],
                "entry_price": df["close"].iloc[sig["index"]],
                "result": trade["result"],
                "exit_price": trade["exit_price"],
                "bars_held": trade["exit_index"] - sig["index"],
            })

            acik_pozisyon_kadar_bar = trade["exit_index"]

        time.sleep(0.1)

    if not all_trades:
        print("Hicbir sinyal bulunamadi.")
        return

    result_df = pd.DataFrame(all_trades)
    csv_path = "volume_delta_backtest_sonuclari.csv"
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
        f"Volume Delta 3'lu Momentum Backtest ({DAYS_BACK} gun, {TIMEFRAME})\n\n"
        f"Toplam sinyal: {total}\n"
        f"Kapanan: {closed} | Acik: {open_count}\n"
        f"TP: {tp_count} | SL: {sl_count}\n"
        f"Genel basari orani: %{win_rate:.1f}\n\n"
        f"AL -> {al_tp}/{al_closed} basarili (%{al_wr:.1f})\n"
        f"SAT -> {sat_tp}/{sat_closed} basarili (%{sat_wr:.1f})\n\n"
        f"SL: 3'lu serinin ilk mumunun low/high'i\n"
        f"TP: giristen %{TP_PCT * 100:.0f}"
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
