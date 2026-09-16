import pandas as pd
import numpy as np
import requests
import os

# ==========================================
# CONFIGURATION & TELEGRAM SETTINGS
# ==========================================
TELEGRAM_TOKEN = "YOUR_TELEGRAM_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"

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
    df = df.copy()
    
    # Technical Indicators
    df['sma100'] = df['close'].rolling(window=sma_len).mean()
    df['vol_sma'] = df['volume'].rolling(window=vol_len).mean()
    df['rsi'] = calculate_rsi(df['close'], period=rsi_len)
    df['macd'], df['macd_signal'], df['macd_hist'] = calculate_macd(df['close'])
    
    # 24h/20-period Rolling Volume USD Liquidity Estimation
    # Filters out low liquidity pairs (min $1M turnover check)
    df['volume_usd'] = df['close'] * df['volume']
    df['volume_24h_usd'] = df['volume_usd'].rolling(window=24).sum()
    
    # Breakout Levels (shifted by 1 to prevent lookahead bias / repaint)
    df['resistance_level'] = df['high'].shift(1).rolling(window=bo_len).max()
    df['support_level'] = df['low'].shift(1).rolling(window=bo_len).min()
    
    return df

# ==========================================
# BACKTEST ENGINE
# ==========================================
def run_backtest(df, tf_label, bo_buffer_pct=0.5, vol_mult=2.0, rsi_bull=60, rsi_bear=40, rr_ratio=2.0, min_liquidity_usd=1000000):
    trades = []
    in_position = False
    current_trade = {}
    
    # Iterate through dataframe
    for i in range(100, len(df)):
        row = df.iloc[i]
        prev_row = df.iloc[i-1]
        
        # Check liquidity filter ($1M minimum turnover requirement)
        if pd.isna(row['volume_24h_usd']) or row['volume_24h_usd'] < min_liquidity_usd:
            continue

        # Position Exit / Management Check
        if in_position:
            # LONG Management
            if current_trade['type'] == 'LONG':
                # Check Stop Loss first
                if row['low'] <= current_trade['sl']:
                    current_trade['exit_time'] = row.name
                    current_trade['exit_price'] = current_trade['sl']
                    current_trade['pnl_pct'] = ((current_trade['sl'] - current_trade['entry']) / current_trade['entry']) * 100
                    current_trade['result'] = 'SL'
                    trades.append(current_trade)
                    in_position = False
                # Check Take Profit
                elif row['high'] >= current_trade['tp']:
                    current_trade['exit_time'] = row.name
                    current_trade['exit_price'] = current_trade['tp']
                    current_trade['pnl_pct'] = ((current_trade['tp'] - current_trade['entry']) / current_trade['entry']) * 100
                    current_trade['result'] = 'TP'
                    trades.append(current_trade)
                    in_position = False
            
            # SHORT Management
            elif current_trade['type'] == 'SHORT':
                # Check Stop Loss
                if row['high'] >= current_trade['sl']:
                    current_trade['exit_time'] = row.name
                    current_trade['exit_price'] = current_trade['sl']
                    current_trade['pnl_pct'] = ((current_trade['entry'] - current_trade['sl']) / current_trade['entry']) * 100
                    current_trade['result'] = 'SL'
                    trades.append(current_trade)
                    in_position = False
                # Check Take Profit
                elif row['low'] <= current_trade['tp']:
                    current_trade['exit_time'] = row.name
                    current_trade['exit_price'] = current_trade['tp']
                    current_trade['pnl_pct'] = ((current_trade['entry'] - current_trade['tp']) / current_trade['entry']) * 100
                    current_trade['result'] = 'TP'
                    trades.append(current_trade)
                    in_position = False

        # Entry Signals Check (if not in position)
        if not in_position:
            # Conditions
            vol_bullish = row['volume'] > (row['vol_sma'] * vol_mult)
            vol_bearish = row['volume'] > (row['vol_sma'] * vol_mult)
            mom_bullish = (row['rsi'] > rsi_bull) and (row['macd_hist'] > 0) and (row['macd'] > row['macd_signal'])
            mom_bearish = (row['rsi'] < rsi_bear) and (row['macd_hist'] < 0) and (row['macd'] < row['macd_signal'])
            
            breakout_up = (row['close'] > row['resistance_level'] * (1 + bo_buffer_pct / 100)) and (prev_row['close'] <= row['resistance_level'])
            breakout_down = (row['close'] < row['support_level'] * (1 - bo_buffer_pct / 100)) and (prev_row['close'] >= row['support_level'])
            
            # LONG Entry Condition: Breakout Up + Volume + Momentum + Price ABOVE SMA 100
            if breakout_up and vol_bullish and mom_bullish and (row['close'] > row['sma100']):
                entry_price = row['close']
                sl_price = row['low']  # Breakout candle low as SL
                risk = entry_price - sl_price
                if risk > 0:
                    tp_price = entry_price + (risk * rr_ratio) # 1:2 R/R Ratio
                    in_position = True
                    current_trade = {
                        'timeframe': tf_label,
                        'type': 'LONG',
                        'entry_time': row.name,
                        'entry': entry_price,
                        'sl': sl_price,
                        'tp': tp_price,
                        'risk': risk
                    }
            
            # SHORT Entry Condition: Breakout Down + Volume + Momentum + Price BELOW SMA 100
            elif breakout_down and vol_bearish and mom_bearish and (row['close'] < row['sma100']):
                entry_price = row['close']
                sl_price = row['high']  # Breakout candle high as SL
                risk = sl_price - entry_price
                if risk > 0:
                    tp_price = entry_price - (risk * rr_ratio) # 1:2 R/R Ratio
                    in_position = True
                    current_trade = {
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
# MAIN EXECUTION & EXAMPLE GENERATION
# ==========================================
if __name__ == "__main__":
    print("Backtest başlatılıyor...")
    
    # Synthetic Data Generation for Testing (Replace with your actual CSV/Binance API data)
    dates_15m = pd.date_range(start="2024-01-01", periods=1000, freq="15min")
    dates_1h = pd.date_range(start="2024-01-01", periods=1000, freq="1h")
    
    np.random.seed(42)
    price_15m = 50000 + np.cumsum(np.random.randn(1000) * 100)
    vol_15m = np.random.randint(50, 500, size=1000) * 1000
    
    df_15m = pd.DataFrame({
        'open': price_15m,
        'high': price_15m + np.abs(np.random.randn(1000) * 50),
        'low': price_15m - np.abs(np.random.randn(1000) * 50),
        'close': price_15m + np.random.randn(1000) * 20,
        'volume': vol_15m
    }, index=dates_15m)
    
    price_1h = 50000 + np.cumsum(np.random.randn(1000) * 250)
    vol_1h = np.random.randint(200, 2000, size=1000) * 1000
    
    df_1h = pd.DataFrame({
        'open': price_1h,
        'high': price_1h + np.abs(np.random.randn(1000) * 100),
        'low': price_1h - np.abs(np.random.randn(1000) * 100),
        'close': price_1h + np.random.randn(1000) * 40,
        'volume': vol_1h
    }, index=dates_1h)

    # Prepare data & calculate indicators
    df_15m_prep = prepare_data(df_15m)
    df_1h_prep = prepare_data(df_1h)

    # Run backtests
    trades_15m = run_backtest(df_15m_prep, tf_label="15m")
    trades_1h = run_backtest(df_1h_prep, tf_label="1H")

    # Combine results
    all_trades = pd.concat([trades_15m, trades_1h], ignore_index=True)
    
    # Export to CSV
    output_filename = "kirilim_backtest_sonuclari.csv"
    all_trades.to_csv(output_filename, index=False)
    print(f"İşlem sonuçları {output_filename} dosyasına kaydedildi.")

    # Telegram'a Gönder
    if TELEGRAM_TOKEN != "YOUR_TELEGRAM_TOKEN" and TELEGRAM_CHAT_ID != "YOUR_TELEGRAM_CHAT_ID":
        send_telegram_csv(output_filename, caption="📊 Kırılım Backtest Sonuçları (15m & 1H)")
    else:
        print("Lütfen TELEGRAM_TOKEN ve TELEGRAM_CHAT_ID değişkenlerini kendi bilgilerinizle güncelleyin.")
