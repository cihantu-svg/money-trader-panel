cat << 'EOF' > kirilim_backtest.py
import os
import time
import requests
import pandas as pd
import numpy as np

# ==========================================
# CONFIGURATION & BINANCE FAPI SETTINGS
# ==========================================
BINANCE_FAPI = "https://fapi.binance.com"
MIN_VOLUME_USDT = 3_000_000
REQUEST_TIMEOUT = 10
SLEEP_BETWEEN_SYMBOLS = float(os.environ.get("SLEEP_BETWEEN_SYMBOLS", "0.45"))
WEIGHT_SOFT_LIMIT = int(os.environ.get("WEIGHT_SOFT_LIMIT", "1800"))
MAX_RETRIES = 5

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ==========================================
# BINANCE FUTURES API HELPERS
# ==========================================
_session = requests.Session()

def binance_get(path: str, params: dict | None = None) -> requests.Response:
    url = f"{BINANCE_FAPI}{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        r = _session.get(url, params=params, timeout=REQUEST_TIMEOUT)

        if r.status_code == 200:
            used_weight = r.headers.get("X-MBX-USED-WEIGHT-1M")
            if used_weight is not None and int(used_weight) >= WEIGHT_SOFT_LIMIT:
                print(f"[UYARI] Kullanılan ağırlık {used_weight}/{WEIGHT_SOFT_LIMIT} soft limite yaklaştı, 60sn bekleniyor...")
                time.sleep(60)
            return r

        if r.status_code in (429, 418):
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else min(60, 2 ** attempt)
            print(f"[UYARI] {path}: {r.status_code} alındı (deneme {attempt}/{MAX_RETRIES}), {wait:.0f} sn bekleniyor...")
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

def get_klines(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
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
    
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    
    now_ms = int(time.time() * 1000)
    if raw and raw[-1][6] > now_ms:
        df = df.iloc[:-1].reset_index(drop=True)
        
    df.set_index("open_time", inplace=True)
    return df

# ==========================================
# INDICATOR CALCULATIONS
# ==========================================
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def prepare_data(df, bo_len=20, rsi_len=14, sma_len=100, vol_len=20):
    if df.empty:
        return df
    df = df.copy()
    
    df['sma100'] = df['close'].rolling(window=sma_len).mean()
    df['vol_sma'] = df['volume'].rolling(window=vol_len).mean()
    df['rsi'] = calculate_rsi(df['close'], period=rsi_len)
    df['macd'], df['macd_signal'], df['macd_hist'] = calculate_macd(df['close'])
    
    df['volume_usd'] = df['close'] * df['volume']
    df['volume_24h_usd'] = df['volume_usd'].rolling(window=24).sum()
    
    df['resistance_level'] = df['high'].shift(1).rolling(window=bo_len).max()
    df['support_level'] = df['low'].shift(1).rolling(window=bo_len).min()
    
    return df

# ==========================================
# BACKTEST ENGINE
# ==========================================
def run_backtest(df, symbol, tf_label, bo_buffer_pct=0.5, vol_mult=2.0, rsi_bull=60, rsi_bear=40, rr_ratio=2.0, min_liquidity_usd=MIN_VOLUME_USDT):
    trades = []
    in_position = False
    current_trade = {}
    
    for i in range(100, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i-1]
        
        if pd.isna(row['volume_24h_usd']) or row['volume_24h_usd'] < min_liquidity_usd:
            continue

        if in_position:
            if current_trade['type'] == 'LONG':
                if row['low'] <= current_trade['sl']:
                    current_trade['exit_time'] = row.name
                    current_trade['exit_price'] = current_trade['sl']
                    current_trade['pnl_pct'] = ((current_trade['sl'] - current_trade['entry']) / current_trade['entry']) * 100
                    current_trade['result'] = 'SL'
                    trades.append(current_trade)
                    in_position = False
                elif row['high'] >= current_trade['tp']:
                    current_trade['exit_time'] = row.name
                    current_trade['exit_price'] = current_trade['tp']
                    current_trade['pnl_pct'] = ((current_trade['tp'] - current_trade['entry']) / current_trade['entry']) * 100
                    current_trade['result'] = 'TP'
                    trades.append(current_trade)
                    in_position = False
            
            elif current_trade['type'] == 'SHORT':
                if row['high'] >= current_trade['sl']:
                    current_trade['exit_time'] = row.name
                    current_trade['exit_price'] = current_trade['sl']
                    current_trade['pnl_pct'] = ((current_trade['entry'] - current_trade['sl']) / current_trade['entry']) * 100
                    current_trade['result'] = 'SL'
                    trades.append(current_trade)
                    in_position = False
                elif row['low'] <= current_trade['tp']:
                    current_trade['exit_time'] = row.name
                    current_trade['exit_price'] = current_trade['tp']
                    current_trade['pnl_pct'] = ((current_trade['entry'] - current_trade['tp']) / current_trade['entry']) * 100
                    current_trade['result'] = 'TP'
                    trades.append(current_trade)
                    in_position = False

        if not in_position:
            vol_bullish = row['volume'] > (row['vol_sma'] * vol_mult)
            vol_bearish = row['volume'] > (row['vol_sma'] * vol_mult)
            mom_bullish = (row['rsi'] > rsi_bull) and (row['macd_hist'] > 0) and (row['macd'] > row['macd_signal'])
            mom_bearish = (row['rsi'] < rsi_bear) and (row['macd_hist'] < 0) and (row['macd'] < row['macd_signal'])
            
            breakout_up = (row['close'] > row['resistance_level'] * (1 + bo_buffer_pct / 100)) and (prev_row['close'] <= row['resistance_level'])
            breakout_down = (row['close'] < row['support_level'] * (1 - bo_buffer_pct / 100)) and (prev_row['close'] >= row['support_level'])
            
            if breakout_up and vol_bullish and mom_bullish and (row['close'] > row['sma100']):
                entry_price = row['close']
                sl_price = row['low']
                risk = entry_price - sl_price
                if risk > 0:
                    tp_price = entry_price + (risk * rr_ratio)
                    in_position = True
                    current_trade = {
                        'symbol': symbol,
                        'timeframe': tf_label,
                        'type': 'LONG',
                        'entry_time': row.name,
                        'entry': entry_price,
                        'sl': sl_price,
                        'tp': tp_price,
                        'risk': risk
                    }
            
            elif breakout_down and vol_bearish and mom_bearish and (row['close'] < row['sma100']):
                entry_price = row['close']
                sl_price = row['high']
                risk = sl_price - entry_price
                if risk > 0:
                    tp_price = entry_price - (risk * rr_ratio)
                    in_position = True
                    current_trade = {
                        'symbol': symbol,
                        'timeframe': tf_label,
                        'type': 'SHORT',
                        'entry_time': row.name,
                        'entry': entry_price,
                        'sl': sl_price,
                        'tp': tp_price,
                        'risk': risk
                    }

    return pd.DataFrame(trades)

# ==========================================
# TELEGRAM SENDER FUNCTION
# ==========================================
def send_telegram_csv(file_path, caption="Backtest Sonuçları CSV"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("HATA: TELEGRAM_TOKEN veya TELEGRAM_CHAT_ID ortam değişkenlerinde (ENV) bulunamadı!")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(file_path, "rb") as file:
            payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
            files = {"document": file}
            response = requests.post(url, data=payload, files=files)
            if response.status_code == 200:
                print("CSV dosyası Telegram'a başarıyla gönderildi!")
            else:
                print(f"Telegram gönderme hatası: {response.text}")
    except Exception as e:
        print(f"Hata oluştu: {e}")

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    print("Binance Futures Borsa Taraması & Backtest Başlatılıyor...")
    
    try:
        symbols = get_usdt_perpetual_symbols()
        volumes = get_24h_volume_map()
    except Exception as e:
        print(f"Sembol/hacim listesi alınamadı: {e}")
        exit(1)

    filtered_symbols = [s for s in symbols if volumes.get(s, 0) >= MIN_VOLUME_USDT]
    print(f"Taranacak Filtrelenmiş Sembol Sayısı: {len(filtered_symbols)} (24s Hacim >= ${MIN_VOLUME_USDT:,} USDT)")

    all_trades_list = []

    for idx, sym in enumerate(filtered_symbols, 1):
        print(f"[{idx}/{len(filtered_symbols)}] {sym} çekiliyor ve test ediliyor...")
        
        df_15m = get_klines(sym, interval="15m", limit=1000)
        if not df_15m.empty:
            df_15m_prep = prepare_data(df_15m)
            trades_15m = run_backtest(df_15m_prep, symbol=sym, tf_label="15m")
            if not trades_15m.empty:
                all_trades_list.append(trades_15m)

        df_1h = get_klines(sym, interval="1h", limit=1000)
        if not df_1h.empty:
            df_1h_prep = prepare_data(df_1h)
            trades_1h = run_backtest(df_1h_prep, symbol=sym, tf_label="1H")
            if not trades_1h.empty:
                all_trades_list.append(trades_1h)

        time.sleep(SLEEP_BETWEEN_SYMBOLS)

    if all_trades_list:
        final_trades = pd.concat(all_trades_list, ignore_index=True)
        output_filename = "kirilim_backtest_sonuclari.csv"
        final_trades.to_csv(output_filename, index=False)
        print(f"\n[BAŞARILI] Toplam {len(final_trades)} adet işlem bulundu ve {output_filename} dosyasına kaydedildi.")

        send_telegram_csv(output_filename, caption=f"📊 Binance Futures Tüm Borsa Kırılım Backtest Sonuçları (15m & 1H)\nToplam İşlem: {len(final_trades)}")
    else:
        print("\n[BİLGİ] Kriterlere uyan hiçbir işlem bulunamadı.")
EOF
