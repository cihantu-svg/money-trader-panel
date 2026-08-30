#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FIB 0.50 KIRILIM BACKTEST v2.1

Senaryo:
  1. 15dk KAPANMIS mumda (high-low)/open >= %7
  2. fib 0.50 = (high + low) / 2
  3. Sonraki 30 mumda (7.5 saat) kiralim ara
  4. Kirilinca entry = kirilma mumunun CLOSE'u
  5. Sonraki mumlarda SL%3 / TP%6 / TP%10 / TP%15 takip et
  6. Ilk hangisi gelirse onu kaydet
"""

import time
import os
import logging
from datetime import datetime, timezone
import requests
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

CFG = {
    'API_BASE': 'https://fapi.binance.com',
    'MAX_COINS': 300,
    'BODY_PCT': 7.0,
    'FIB_WINDOW': 30,
    'MIN_VOLUME_24H': 3_000_000,
    'DAYS_BACK': 15,
    'CSV_OUTPUT': 'fib50_backtest.csv',
    'API_DELAY': 0.25,
    'RETRY_MAX': 3,
    'RETRY_BASE': 1.0,
}

session = requests.Session()

# ==================== TELEGRAM ====================
def send_telegram(msg, csv_path=None):
    tok = os.getenv('TELEGRAM_BOT_TOKEN', '')
    cid = os.getenv('TELEGRAM_CHAT_ID', '')
    if not tok or not cid:
        log.warning('Telegram token/chat_id bos')
        return False
    try:
        r = session.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                         data={"chat_id": cid, "text": msg}, timeout=10)
        if csv_path and os.path.exists(csv_path):
            with open(csv_path, 'rb') as f:
                session.post(f"https://api.telegram.org/bot{tok}/sendDocument",
                             files={"document": f}, data={"chat_id": cid}, timeout=60)
        return True
    except Exception as e:
        log.error(f"TG: {e}")
        return False

# ==================== API ====================
def api_get(endpoint, params=None, retries=0):
    try:
        r = session.get(f"{CFG['API_BASE']}{endpoint}", params=params, timeout=15)
        if r.status_code == 429 and retries < CFG['RETRY_MAX']:
            time.sleep(CFG['RETRY_BASE'] * (2 ** retries))
            return api_get(endpoint, params, retries + 1)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"API: {e}")
        return None

def get_symbols():
    info = api_get('/fapi/v1/exchangeInfo')
    tickers = api_get('/fapi/v1/ticker/24hr')
    if not info or not tickers:
        return []
    trading = {s['symbol'] for s in info['symbols']
               if s['symbol'].endswith('USDT') and s.get('contractType') == 'PERPETUAL'
               and s.get('status') == 'TRADING'}
    rows = [(t['symbol'], float(t.get('quoteVolume', 0))) for t in tickers if t['symbol'] in trading]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:CFG['MAX_COINS']]]

def fetch_klines(symbol, interval, start_ms, end_ms):
    all_rows = []
    cursor = start_ms
    while cursor < end_ms:
        time.sleep(CFG['API_DELAY'])
        d = api_get('/fapi/v1/klines', {
            'symbol': symbol, 'interval': interval,
            'startTime': cursor, 'endTime': end_ms, 'limit': 1500
        })
        if not d or not isinstance(d, list) or not d:
            break
        all_rows.extend(d)
        last_open = d[-1][0]
        if len(d) < 1500:
            break
        cursor = last_open + 1
    if not all_rows:
        return None
    df = pd.DataFrame(all_rows, columns=[
        'open_time','open','high','low','close','volume','close_time',
        'quote_volume','trades','taker_buy_base','taker_buy_quote','ignore'
    ])
    for c in ['open','high','low','close','volume','quote_volume']:
        df[c] = df[c].astype(float)
    df['open_time'] = df['open_time'].astype(np.int64)
    df['close_time'] = df['close_time'].astype(np.int64)
    return df.drop_duplicates(subset='open_time').sort_values('open_time').reset_index(drop=True)

# ==================== SINYAL MOTORU ====================
def find_events(df15):
    if df15 is None or len(df15) < 5:
        return []
    df = df15.copy()
    df['mum_pct'] = (df['high'] - df['low']) / df['open'] * 100
    events = []
    for i in range(len(df) - 1):
        mp = df['mum_pct'].iloc[i]
        if mp >= CFG['BODY_PCT']:
            direction = 'LONG' if df['close'].iloc[i] >= df['open'].iloc[i] else 'SHORT'
            events.append({
                'idx': i,
                'direction': direction,
                'open': float(df['open'].iloc[i]),
                'high': float(df['high'].iloc[i]),
                'low': float(df['low'].iloc[i]),
                'close': float(df['close'].iloc[i]),
                'open_time': int(df['open_time'].iloc[i]),
                'close_time': int(df['close_time'].iloc[i]),
                'pct': float(mp),
            })
    return events

def check_fib50(df15, event):
    if df15 is None or len(df15) < event['idx'] + 2:
        return None

    fib50 = (event['high'] + event['low']) / 2
    after = df15.iloc[event['idx'] + 1:].copy().reset_index(drop=True)
    limit = min(CFG['FIB_WINDOW'], len(after))

    result = {
        'fib50': fib50,
        'break_price': None,
        'break_time': None,
        'break_mum': None,
        'kirildi': False,
        'sl3': False,
        'tp6': False,
        'tp10': False,
        'tp15': False,
        'first_target': None,
    }

    entry = None

    for i in range(limit):
        high = float(after['high'].iloc[i])
        low = float(after['low'].iloc[i])
        close = float(after['close'].iloc[i])
        close_time = int(after['close_time'].iloc[i])

        # Kirilma arama
        if not result['kirildi']:
            if event['direction'] == 'LONG' and low <= fib50:
                result['kirildi'] = True
                result['break_price'] = close  # ENTRY = CLOSE
                result['break_time'] = close_time
                result['break_mum'] = i + 1
                entry = close
            elif event['direction'] == 'SHORT' and high >= fib50:
                result['kirildi'] = True
                result['break_price'] = close  # ENTRY = CLOSE
                result['break_time'] = close_time
                result['break_mum'] = i + 1
                entry = close

        # TP/SL takip (KIRILMA SONRAKI MUMLARDA)
        if result['kirildi'] and entry is not None:
            if event['direction'] == 'LONG':
                if not result['sl3'] and low <= entry * 0.97:
                    result['sl3'] = True
                    if not result['first_target']:
                        result['first_target'] = 'SL3'
                if not result['tp6'] and high >= entry * 1.06:
                    result['tp6'] = True
                    if not result['first_target']:
                        result['first_target'] = 'TP6'
                if not result['tp10'] and high >= entry * 1.10:
                    result['tp10'] = True
                    if not result['first_target']:
                        result['first_target'] = 'TP10'
                if not result['tp15'] and high >= entry * 1.15:
                    result['tp15'] = True
                    if not result['first_target']:
                        result['first_target'] = 'TP15'
            else:
                if not result['sl3'] and high >= entry * 1.03:
                    result['sl3'] = True
                    if not result['first_target']:
                        result['first_target'] = 'SL3'
                if not result['tp6'] and low <= entry * 0.94:
                    result['tp6'] = True
                    if not result['first_target']:
                        result['first_target'] = 'TP6'
                if not result['tp10'] and low <= entry * 0.90:
                    result['tp10'] = True
                    if not result['first_target']:
                        result['first_target'] = 'TP10'
                if not result['tp15'] and low <= entry * 0.85:
                    result['tp15'] = True
                    if not result['first_target']:
                        result['first_target'] = 'TP15'

    return result

# ==================== BACKTEST ====================
def run_backtest():
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - CFG['DAYS_BACK'] * 24 * 60 * 60 * 1000

    log.info(f"Backtest: son {CFG['DAYS_BACK']} gun | fib 0.50 | {CFG['FIB_WINDOW']} mum")

    symbols = get_symbols()
    log.info(f"{len(symbols)} sembol bulundu")

    results = []
    stats = {'long_events': 0, 'short_events': 0, 'long_kirildi': 0, 'short_kirildi': 0, 'err': 0}

    for idx, sym in enumerate(symbols, 1):
        try:
            df15 = fetch_klines(sym, '15m', start_ms, now_ms)
            if df15 is None or len(df15) < 10:
                continue

            events = find_events(df15)
            if not events:
                continue

            for ev in events:
                if ev['direction'] == 'LONG':
                    stats['long_events'] += 1
                else:
                    stats['short_events'] += 1

                fib = check_fib50(df15, ev)
                if fib is None:
                    continue

                if fib['kirildi']:
                    if ev['direction'] == 'LONG':
                        stats['long_kirildi'] += 1
                    else:
                        stats['short_kirildi'] += 1

                results.append({
                    'symbol': sym,
                    'direction': ev['direction'],
                    'event_time': datetime.fromtimestamp(ev['close_time'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
                    'event_pct': round(ev['pct'], 2),
                    'event_high': round(ev['high'], 6),
                    'event_low': round(ev['low'], 6),
                    'fib50': round(fib['fib50'], 6),
                    'kirildi': fib['kirildi'],
                    'break_mum': fib['break_mum'],
                    'break_price': round(fib['break_price'], 6) if fib['break_price'] else None,
                    'sl3': fib['sl3'],
                    'tp6': fib['tp6'],
                    'tp10': fib['tp10'],
                    'tp15': fib['tp15'],
                    'first_target': fib['first_target'],
                })

        except Exception as e:
            stats['err'] += 1
            log.error(f"Hata {sym}: {e}")

        if idx % 25 == 0:
            log.info(f"[{idx}/{len(symbols)}] tamamlandi | Olay: {len(results)}")

    if results:
        df = pd.DataFrame(results)
        df.to_csv(CFG['CSV_OUTPUT'], index=False, encoding='utf-8-sig')
        log.info(f"CSV: {CFG['CSV_OUTPUT']} | {len(results)} olay")
    else:
        log.warning("Sonuc yok")

    # RAPOR
    total_events = stats['long_events'] + stats['short_events']
    total_kirildi = stats['long_kirildi'] + stats['short_kirildi']

    kirilan = [r for r in results if r['kirildi']]
    sl3_c = sum(1 for r in kirilan if r['sl3'])
    tp6_c = sum(1 for r in kirilan if r['tp6'])
    tp10_c = sum(1 for r in kirilan if r['tp10'])
    tp15_c = sum(1 for r in kirilan if r['tp15'])

    print("\n" + "=" * 60)
    print("FIB 0.50 KIRILIM BACKTEST SONUC")
    print("=" * 60)
    print(f"Taranan coin: {idx}")
    print(f"LONG olay: {stats['long_events']} | Kirildi: {stats['long_kirildi']}")
    print(f"SHORT olay: {stats['short_events']} | Kirildi: {stats['short_kirildi']}")
    if total_events > 0:
        print(f"Toplam kirilma: {total_kirildi}/{total_events} = %{total_kirildi/total_events*100:.1f}")
    print("-" * 60)
    print(f"KIRILMA SONRASI HEDEFLER ({total_kirildi} kirilan olay):")
    if total_kirildi > 0:
        print(f"  SL %3  : {sl3_c}/{total_kirildi} = %{sl3_c/total_kirildi*100:.1f}")
        print(f"  TP %6  : {tp6_c}/{total_kirildi} = %{tp6_c/total_kirildi*100:.1f}")
        print(f"  TP %10 : {tp10_c}/{total_kirildi} = %{tp10_c/total_kirildi*100:.1f}")
        print(f"  TP %15 : {tp15_c}/{total_kirildi} = %{tp15_c/total_kirildi*100:.1f}")
    print("-" * 60)
    print(f"Hata: {stats['err']}")
    print("=" * 60)

    # Telegram
    kirilma = f"%{total_kirildi/total_events*100:.1f}" if total_events > 0 else "N/A"
    msg = (
        f"FIB 0.50 Backtest\n"
        f"Kirilma: {total_kirildi}/{total_events} = {kirilma}\n"
        f"SL %3: {sl3_c}/{total_kirildi} = %{sl3_c/total_kirildi*100:.1f}\n"
        f"TP %6: {tp6_c}/{total_kirildi} = %{tp6_c/total_kirildi*100:.1f}\n"
        f"TP %10: {tp10_c}/{total_kirildi} = %{tp10_c/total_kirildi*100:.1f}\n"
        f"TP %15: {tp15_c}/{total_kirildi} = %{tp15_c/total_kirildi*100:.1f}"
    )
    send_telegram(msg, CFG['CSV_OUTPUT'])

    return results

if __name__ == '__main__':
    print("=" * 60)
    print("FIB 0.50 KIRILIM BACKTEST v2.1")
    print("=" * 60)
    run_backtest()
