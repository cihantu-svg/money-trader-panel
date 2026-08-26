#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
%7 KIRILIM + 1dk ONAY BACKTEST

Senaryo:
  1. 15dk mumda govde (close-open)/open >= %7  -> LONG olay
     15dk mumda govde <= -%7                     -> SHORT olay
  2. Olaydan sonraki 1dk veride (30 mum / 30dk icinde):
     LONG:  olay close'unun %2 USTUNE cikti mi?
     SHORT: olay close'unun %2 ALTINA dustu mu?
  3. Onay gelirse -> sinyal (entry = onay mumunun close'u)
     Onay gelmezse -> elenir

Cikti: CSV + konsol ozeti
"""

import time
import os
import sys
import logging
import csv
from datetime import datetime, timezone
import requests
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ==================== AYARLAR ====================
CFG = {
    'API_BASE': 'https://fapi.binance.com',
    'MAX_COINS': 300,
    'BODY_PCT': 7.0,          # %7 esik
    'CONFIRM_PCT': 2.0,       # onay icin %2
    'CONFIRM_CANDLES': 30,    # 30 mum (30dk) icinde onay ara
    'MIN_VOLUME_24H': 3_000_000,
    'DAYS_BACK': 20,
    'CSV_OUTPUT': 'breakout_confirm_backtest.csv',
    'API_DELAY': 0.25,
    'RETRY_MAX': 3,
    'RETRY_BASE': 1.0,
}

session = requests.Session()

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
        log.error(f"API hata: {e}")
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
    """15dk'da %7+ veya %7- olaylari bul"""
    if df15 is None or len(df15) < 5:
        return []
    df = df15.copy()
    df['body_pct'] = (df['close'] - df['open']) / df['open'] * 100
    events = []
    for i in range(len(df)):
        bp = df['body_pct'].iloc[i]
        if bp >= CFG['BODY_PCT']:
            events.append({'idx': i, 'direction': 'LONG', 'close': float(df['close'].iloc[i]),
                           'open_time': int(df['open_time'].iloc[i]),
                           'close_time': int(df['close_time'].iloc[i]), 'pct': float(bp)})
        elif bp <= -CFG['BODY_PCT']:
            events.append({'idx': i, 'direction': 'SHORT', 'close': float(df['close'].iloc[i]),
                           'open_time': int(df['open_time'].iloc[i]),
                           'close_time': int(df['close_time'].iloc[i]), 'pct': float(bp)})
    return events

def check_confirm(df1m, event):
    """Olaydan sonraki 30 mumda %2 onay var mi?"""
    if df1m is None or len(df1m) < 2:
        return None
    after = df1m[df1m['open_time'] > event['close_time']].copy().reset_index(drop=True)
    if len(after) == 0:
        return None

    limit = min(CFG['CONFIRM_CANDLES'], len(after))
    window = after.iloc[:limit]

    event_close = event['close']

    if event['direction'] == 'LONG':
        # %2 ustune cikti mi?
        target = event_close * (1 + CFG['CONFIRM_PCT'] / 100)
        for i in range(len(window)):
            if window['high'].iloc[i] >= target:
                return {
                    'entry_price': float(window['close'].iloc[i]),
                    'entry_time': int(window['close_time'].iloc[i]),
                    'confirm_high': float(window['high'].iloc[i]),
                    'mum_index': i + 1,
                }
    else:
        # %2 altina dustu mu?
        target = event_close * (1 - CFG['CONFIRM_PCT'] / 100)
        for i in range(len(window)):
            if window['low'].iloc[i] <= target:
                return {
                    'entry_price': float(window['close'].iloc[i]),
                    'entry_time': int(window['close_time'].iloc[i]),
                    'confirm_low': float(window['low'].iloc[i]),
                    'mum_index': i + 1,
                }
    return None

# ==================== BACKTEST ====================
def run_backtest():
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - CFG['DAYS_BACK'] * 24 * 60 * 60 * 1000

    log.info(f"Backtest: son {CFG['DAYS_BACK']} gun | %7 esik | %2 onay | 30 mum pencere")

    symbols = get_symbols()
    log.info(f"{len(symbols)} sembol bulundu")

    results = []
    stats = {'long_events': 0, 'short_events': 0, 'long_confirmed': 0, 'short_confirmed': 0, 'err': 0}

    for idx, sym in enumerate(symbols, 1):
        try:
            # 15dk verisi
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

            # Olay olan coinlere 1dk verisi cek (olay zamani +- 1 saat yeterli)
            if events:
                first_event = min(e['close_time'] for e in events)
                last_event = max(e['close_time'] for e in events)
                fetch_start = first_event - 60 * 60 * 1000  # 1 saat once
                fetch_end = last_event + 60 * 60 * 1000     # 1 saat sonra
                df1m = fetch_klines(sym, '1m', fetch_start, fetch_end)

                if df1m is None:
                    continue

                for ev in events:
                    confirm = check_confirm(df1m, ev)
                    if confirm:
                        if ev['direction'] == 'LONG':
                            stats['long_confirmed'] += 1
                        else:
                            stats['short_confirmed'] += 1

                        results.append({
                            'symbol': sym,
                            'direction': ev['direction'],
                            'event_time': datetime.fromtimestamp(ev['close_time'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
                            'event_pct': round(ev['pct'], 2),
                            'event_close': round(ev['close'], 6),
                            'entry_time': datetime.fromtimestamp(confirm['entry_time'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
                            'entry_price': round(confirm['entry_price'], 6),
                            'confirm_mum': confirm['mum_index'],
                        })

        except Exception as e:
            stats['err'] += 1
            log.error(f"Hata {sym}: {e}")

        if idx % 25 == 0:
            log.info(f"[{idx}/{len(symbols)}] tamamlandi | Onaylanan: {len(results)}")

    # CSV kaydet
    if results:
        df = pd.DataFrame(results)
        df.to_csv(CFG['CSV_OUTPUT'], index=False, encoding='utf-8-sig')
        log.info(f"CSV kaydedildi: {CFG['CSV_OUTPUT']} | {len(results)} sinyal")
    else:
        log.warning("Sonuc bulunamadi")

    # Ozet
    print("\n" + "=" * 60)
    print("BACKTEST SONUC")
    print("=" * 60)
    print(f"Taranan coin: {idx}")
    print(f"LONG olay: {stats['long_events']} | Onay: {stats['long_confirmed']}")
    print(f"SHORT olay: {stats['short_events']} | Onay: {stats['short_confirmed']}")
    total_events = stats['long_events'] + stats['short_events']
    total_confirmed = stats['long_confirmed'] + stats['short_confirmed']
    if total_events > 0:
        print(f"Onay orani: {total_confirmed}/{total_events} = %{total_confirmed/total_events*100:.1f}")
    print(f"Hata: {stats['err']}")
    print("=" * 60)

    return results

# ==================== MAIN ====================
if __name__ == '__main__':
    print("=" * 60)
    print("%7 KIRILIM + 1dk %2 ONAY BACKTEST")
    print("=" * 60)
    run_backtest()
