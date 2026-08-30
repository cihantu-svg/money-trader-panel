#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
%7 KIRILIM (ACIK MUMDA) + 1dk ONAY + "TEST EDILMIS SEVIYE" FILTRESI + TP/SL BACKTEST

Temel senaryo onceki scriptle ayni (acik mumda %7 govde + 1dk'da %2 onay),
buna EK OLARAK iki yeni parca var:

A) TEST EDILMIS SEVIYE SKORU
   Gozlem: "hedefe giden sinyallerin coğu, kirilan bolge daha once bir cok
   kez test edilmis (fiyat orada birden fazla kez durup geri donmus)."
   Bunu olculebilir hale getirmek icin:
     - Olaydan ONCEKI LEVEL_LOOKBACK_CANDLES kadar 15dk mumda (agirlik
       olcumu icin 1dk'dan agregatlanir) pivot tepe/dip noktalari bulunur
       (sol/sag PIVOT_LEFT_RIGHT bar).
     - Pivot fiyatlari LEVEL_TOLERANCE_PCT toleransla kumelenir; en kalabalik
       kume "test edilen seviye" kabul edilir, o kumenin dokunus sayisi
       "touch_count" olarak kaydedilir.
     - touch_count >= LEVEL_MIN_TOUCHES ise sinyal "TESTED" (yuksek kalite
       aday), degilse "UNTESTED" olarak etiketlenir.
   NOT: bu bir hipotez testi araci - varsayilan parametreler (100 mum,
   %1.0 tolerans, 3 dokunus) makul bir baslangic noktasi ama kesin dogru
   degil; sonuclara bakip ayarlanabilir.

B) TP/SL SIMULASYONU
   Onay geldikten sonra (entry = onay mumunun close'u), STOP_LOSS_PCT (%5)
   ve TAKE_PROFIT_PCT (%10) ile 1dk veride ileri dogru TP/SL takibi yapilir
   (ayni mumda ikisi de tetiklenirse kotumser varsayimla SL kabul edilir).
   Sonuclar TESTED / UNTESTED kirilimina gore AYRI raporlanir - boylece
   "test edilmis seviye" hipotezinin gercekten TP oranini yukseltip
   yukseltmedigi somut rakamla gorulur.

Aym coinde bir islem acikken (entry'den TRADE_MAX_HOURS'a kadar TP/SL
beklenirken) yeni sinyal aranmaz.

Cikti: CSV (her sinyalin TESTED/UNTESTED etiketi + TP/SL sonucu ile) + konsol ozeti
"""

import time
import os
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ==================== AYARLAR ====================
CFG = {
    'API_BASE': 'https://fapi.binance.com',
    'BODY_PCT': 7.0,               # %7 esik (acik mumda kontrol edilir)
    'CONFIRM_PCT': 2.0,            # onay icin %2
    'CONFIRM_CANDLES': 30,         # 30 mum (30dk) icinde onay ara
    'MIN_VOLUME_24H': 1_000_000,   # trailing 24h (1440x1dk) hacim esigi
    'DAYS_BACK': 20,
    'CSV_OUTPUT': 'breakout_level_test_backtest.csv',
    'API_DELAY': 0.12,
    'RETRY_MAX': 5,
    'RETRY_BASE': 1.0,
    'MAX_WORKERS': 5,

    # --- YENI: test edilmis seviye ---
    'LEVEL_LOOKBACK_CANDLES': 100,   # 15dk mum sayisi (~25 saat)
    'LEVEL_TOLERANCE_PCT': 3.0,      # ayni bolge sayilma toleransi
    'LEVEL_MIN_TOUCHES': 3,          # min. dokunus sayisi -> "TESTED"
    'PIVOT_LEFT_RIGHT': 2,           # pivot tespiti icin sol/sag bar
    'MIN_TOUCH_SEPARATION_CANDLES': 5,  # ayni dokunus sayilmamasi icin min. mum araligi

    # --- YENI: TP/SL ---
    'STOP_LOSS_PCT': 5.0,
    'TAKE_PROFIT_PCT': 10.0,
    'TAKER_FEE_PCT_PER_SIDE': 0.04,
    'TRADE_MAX_HOURS': 72,           # bu sureye kadar TP/SL gelmezse "OPEN"
}

WINDOW_MS = 15 * 60 * 1000
DAY_MS = 24 * 60 * 60 * 1000

session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(pool_connections=CFG['MAX_WORKERS'] + 5,
                                          pool_maxsize=CFG['MAX_WORKERS'] + 10)
session.mount("https://", _adapter)


# ==================== API ====================
def api_get(endpoint, params=None):
    """Tum gecici hatalarda (429/418/5xx/timeout/baglanti kopmasi)
    exponential backoff ile RETRY_MAX kez tekrar dener."""
    last_err = None
    for attempt in range(CFG['RETRY_MAX'] + 1):
        try:
            r = session.get(f"{CFG['API_BASE']}{endpoint}", params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 418) or r.status_code >= 500:
                wait = CFG['RETRY_BASE'] * (2 ** attempt)
                retry_after = r.headers.get('Retry-After')
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        pass
                sym = params.get('symbol', '') if params else ''
                log.warning(f"HTTP {r.status_code} ({endpoint} {sym}), "
                            f"{wait:.1f}sn sonra tekrar (deneme {attempt + 1}/{CFG['RETRY_MAX']})")
                time.sleep(wait)
                last_err = f"HTTP {r.status_code}"
                continue
            log.error(f"API hata (retry edilmez) {endpoint}: HTTP {r.status_code} - {r.text[:200]}")
            return None
        except requests.exceptions.RequestException as e:
            last_err = str(e)
            wait = CFG['RETRY_BASE'] * (2 ** attempt)
            sym = params.get('symbol', '') if params else ''
            log.warning(f"Ag hatasi ({endpoint} {sym}): {e}, "
                        f"{wait:.1f}sn sonra tekrar (deneme {attempt + 1}/{CFG['RETRY_MAX']})")
            time.sleep(wait)

    log.error(f"KALICI HATA - {CFG['RETRY_MAX']} denemeden sonra basarisiz: "
              f"{endpoint} params={params} son_hata={last_err}")
    return None


def get_all_symbols():
    info = api_get('/fapi/v1/exchangeInfo')
    if not info:
        log.error("exchangeInfo alinamadi - sembol listesi BOS donuyor")
        return []
    symbols = [s['symbol'] for s in info['symbols']
               if s['symbol'].endswith("USDT") and s.get('contractType') == 'PERPETUAL'
               and s.get('status') == 'TRADING']
    if not symbols:
        log.error("exchangeInfo bos sembol listesi dondurdu")
    return symbols


def fetch_klines_1m(symbol, start_ms, end_ms):
    all_rows = []
    cursor = start_ms
    while cursor < end_ms:
        time.sleep(CFG['API_DELAY'])
        d = api_get('/fapi/v1/klines', {
            'symbol': symbol, 'interval': '1m',
            'startTime': cursor, 'endTime': end_ms, 'limit': 1500
        })
        if d is None:
            gap_start = datetime.fromtimestamp(cursor / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
            log.error(f"[EKSIK VERI] {symbol} 1m: {gap_start} UTC sonrasi alinamadi "
                      f"(retry'ler tukendi) - bu aralik eksik kalabilir")
            return all_rows, True
        if not isinstance(d, list) or not d:
            break
        all_rows.extend(d)
        last_open = d[-1][0]
        if len(d) < 1500:
            break
        cursor = last_open + 1

    return all_rows, False


def rows_to_df(rows):
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time',
        'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'
    ])
    for c in ['open', 'high', 'low', 'close', 'volume', 'quote_volume']:
        df[c] = df[c].astype(float)
    df['open_time'] = df['open_time'].astype(np.int64)
    df['close_time'] = df['close_time'].astype(np.int64)
    return df.drop_duplicates(subset='open_time').sort_values('open_time').reset_index(drop=True)


# ==================== SINYAL MOTORU (ACIK MUM) ====================
def find_events_open_candle(df1m, test_start_ms):
    if df1m is None or len(df1m) < 1440:
        return []

    df = df1m.copy()
    df['window_start'] = (df['open_time'] // WINDOW_MS) * WINDOW_MS
    df['rolling_24h_vol'] = df['quote_volume'].rolling(1440, min_periods=1440).sum()

    events = []
    for window_start, grp in df.groupby('window_start', sort=True):
        if window_start < test_start_ms:
            continue
        grp = grp.reset_index(drop=True)
        window_open = float(grp['open'].iloc[0])

        for i in range(len(grp)):
            row = grp.iloc[i]
            body_pct = (float(row['close']) - window_open) / window_open * 100

            if abs(body_pct) < CFG['BODY_PCT']:
                continue

            vol24 = row['rolling_24h_vol']
            if pd.isna(vol24) or vol24 < CFG['MIN_VOLUME_24H']:
                continue

            direction = 'LONG' if body_pct > 0 else 'SHORT'
            events.append({
                'direction': direction,
                'window_start': int(window_start),
                'event_close': float(row['close']),
                'event_time': int(row['close_time']),
                'pct': float(body_pct),
                'vol24h': float(vol24),
                'minute_in_window': i + 1,
            })
            break

    return events


def check_confirm(df1m, event):
    after = df1m[df1m['open_time'] > event['event_time']]
    if len(after) == 0:
        return None

    limit = min(CFG['CONFIRM_CANDLES'], len(after))
    window = after.iloc[:limit].reset_index(drop=True)
    event_close = event['event_close']

    if event['direction'] == 'LONG':
        target = event_close * (1 + CFG['CONFIRM_PCT'] / 100)
        hits = window.index[window['high'] >= target]
    else:
        target = event_close * (1 - CFG['CONFIRM_PCT'] / 100)
        hits = window.index[window['low'] <= target]

    if len(hits) == 0:
        return None
    i = hits[0]
    return {
        'entry_price': float(window['close'].iloc[i]),
        'entry_time': int(window['close_time'].iloc[i]),
        'mum_index': int(i) + 1,
    }


# ==================== YENI: TEST EDILMIS SEVIYE ====================
def build_15m_ohlc(df1m):
    """1dk veriden pivot analizi icin 15dk OHLC agregasyonu turetir."""
    df = df1m.copy()
    df['window_start'] = (df['open_time'] // WINDOW_MS) * WINDOW_MS
    agg = df.groupby('window_start').agg(
        open=('open', 'first'), high=('high', 'max'),
        low=('low', 'min'), close=('close', 'last'),
    ).reset_index().rename(columns={'window_start': 'open_time'})
    return agg.sort_values('open_time').reset_index(drop=True)


def find_pivot_indices(series, left, right, kind):
    """Basit pivot tepe/dip tespiti: merkez bar, sol+sag pencerede
    tek basina en yuksek/en dusuksa pivot sayilir."""
    n = len(series)
    idxs = []
    for i in range(left, n - right):
        window = series.iloc[i - left:i + right + 1]
        center = series.iloc[i]
        if kind == 'high':
            if center == window.max() and (window == center).sum() == 1:
                idxs.append(i)
        else:
            if center == window.min() and (window == center).sum() == 1:
                idxs.append(i)
    return idxs


def compute_level_test(df15, event_window_start_ms, direction):
    """Olaydan ONCEKI LEVEL_LOOKBACK_CANDLES kadar 15dk mumda, KIRILAN
    SEVIYEYI (pencerenin en yuksegi/LONG icin, en dusugu/SHORT icin)
    bulur ve o spesifik seviyeye kac kez dokunulmus, sayar.
    (touch_count, is_tested)

    NOT: ilk versiyon "pencerede en kalabalik fiyat kumesini" ariyordu -
    bu, dar/sakin bir fiyat araliginda RASTGELE gurultude bile yanlis
    pozitif verebiliyordu (test ettim, oyle cikti). Simdi sadece asil
    kirilan uc seviyeye (direnc/destek) yakin pivotlar sayiliyor - bu
    hem daha dogru hem de grafikte gordugun senaryoyla (ayni direnc
    cizgisine tekrar tekrar dokunma) birebir eslesiyor."""
    before_idx = df15.index[df15['open_time'] < event_window_start_ms]
    if len(before_idx) < 20:
        return 0, False
    end_idx = before_idx[-1]
    start_idx = max(0, end_idx - CFG['LEVEL_LOOKBACK_CANDLES'] + 1)
    window = df15.iloc[start_idx:end_idx + 1].reset_index(drop=True)
    if len(window) < 20:
        return 0, False

    lr = CFG['PIVOT_LEFT_RIGHT']
    if direction == 'LONG':
        pivots = find_pivot_indices(window['high'], lr, lr, 'high')
        pivot_prices = [(p, float(window['high'].iloc[p])) for p in pivots]
        if not pivot_prices:
            return 0, False
        level = max(pr for _, pr in pivot_prices)   # kirilan direnc
    else:
        pivots = find_pivot_indices(window['low'], lr, lr, 'low')
        pivot_prices = [(p, float(window['low'].iloc[p])) for p in pivots]
        if not pivot_prices:
            return 0, False
        level = min(pr for _, pr in pivot_prices)   # kirilan destek

    tol = CFG['LEVEL_TOLERANCE_PCT']
    min_sep = CFG['MIN_TOUCH_SEPARATION_CANDLES']
    near_level_idxs = sorted(idx for idx, pr in pivot_prices if abs(pr - level) / level * 100 <= tol)

    # zaman olarak birbirine cok yakin pivotlar (ayni yuvarlak tepenin
    # komsu bar gurultusu) TEK dokunus sayilir - gercek "tekrar tekrar
    # gelip test etme" zaman icinde ayrisik olmali
    touch_count = 0
    last_counted_idx = -10**9
    for idx in near_level_idxs:
        if idx - last_counted_idx >= min_sep:
            touch_count += 1
            last_counted_idx = idx

    return touch_count, touch_count >= CFG['LEVEL_MIN_TOUCHES']


# ==================== YENI: TP/SL SIMULASYONU ====================
def simulate_trade_exit(df1m, direction, entry_price, entry_time_ms):
    """Onay entry'sinden sonra 1dk veride TP/SL takibi. Ayni mumda
    ikisi de tetiklenirse kotumser varsayimla SL kabul edilir."""
    cutoff_ms = entry_time_ms + CFG['TRADE_MAX_HOURS'] * 3600 * 1000
    after = df1m[(df1m['open_time'] > entry_time_ms) & (df1m['open_time'] <= cutoff_ms)]

    if direction == 'LONG':
        tp_price = entry_price * (1 + CFG['TAKE_PROFIT_PCT'] / 100)
        sl_price = entry_price * (1 - CFG['STOP_LOSS_PCT'] / 100)
    else:
        tp_price = entry_price * (1 - CFG['TAKE_PROFIT_PCT'] / 100)
        sl_price = entry_price * (1 + CFG['STOP_LOSS_PCT'] / 100)

    for _, row in after.iterrows():
        hi, lo = float(row['high']), float(row['low'])
        if direction == 'LONG':
            hit_tp, hit_sl = hi >= tp_price, lo <= sl_price
        else:
            hit_tp, hit_sl = lo <= tp_price, hi >= sl_price

        if hit_sl:
            result = 'SL'
        elif hit_tp:
            result = 'TP'
        else:
            continue

        raw = CFG['TAKE_PROFIT_PCT'] if result == 'TP' else -CFG['STOP_LOSS_PCT']
        pnl = raw - 2 * CFG['TAKER_FEE_PCT_PER_SIDE']
        return result, pnl, int(row['close_time'])

    if len(after) == 0:
        return 'OPEN', 0.0, entry_time_ms
    last = after.iloc[-1]
    last_close = float(last['close'])
    if direction == 'LONG':
        raw = (last_close - entry_price) / entry_price * 100
    else:
        raw = (entry_price - last_close) / entry_price * 100
    pnl = raw - 2 * CFG['TAKER_FEE_PCT_PER_SIDE']
    return 'OPEN', pnl, int(last['close_time'])


# ==================== TEK COIN ====================
def process_symbol(sym, fetch_start_ms, test_start_ms, now_ms):
    rows, incomplete = fetch_klines_1m(sym, fetch_start_ms, now_ms)
    df1m = rows_to_df(rows)
    empty_stats = {'events': 0, 'confirmed': 0}
    if df1m is None or len(df1m) < 1440:
        return [], empty_stats, incomplete

    events = find_events_open_candle(df1m, test_start_ms)
    if not events:
        return [], empty_stats, incomplete

    df15 = build_15m_ohlc(df1m)
    stats = {'events': len(events), 'confirmed': 0}
    results = []
    busy_until_ms = 0

    for ev in events:
        if ev['event_time'] < busy_until_ms:
            continue  # onceki islem hala "acik" - yeni sinyal atlanir

        confirm = check_confirm(df1m, ev)
        if not confirm:
            continue

        stats['confirmed'] += 1
        touch_count, is_tested = compute_level_test(df15, ev['window_start'], ev['direction'])
        result, pnl_pct, exit_time_ms = simulate_trade_exit(
            df1m, ev['direction'], confirm['entry_price'], confirm['entry_time'])
        busy_until_ms = exit_time_ms

        results.append({
            'symbol': sym,
            'direction': ev['direction'],
            'window_start': datetime.fromtimestamp(ev['window_start'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
            'event_pct': round(ev['pct'], 2),
            'entry_time': datetime.fromtimestamp(confirm['entry_time'] / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M'),
            'entry_price': round(confirm['entry_price'], 6),
            'touch_count': touch_count,
            'is_tested': is_tested,
            'result': result,
            'pnl_pct': round(pnl_pct, 2),
        })

    return results, stats, incomplete


# ==================== OZET ====================
def summarize(results, label, subset_filter):
    rows = [r for r in results if subset_filter(r)]
    closed = [r for r in rows if r['result'] in ('TP', 'SL')]
    wins = [r for r in closed if r['result'] == 'TP']
    n_closed = len(closed)
    win_rate = (len(wins) / n_closed * 100) if n_closed else 0.0
    total_pnl = sum(r['pnl_pct'] for r in closed)
    avg_pnl = (total_pnl / n_closed) if n_closed else 0.0

    print(f"\n--- {label} ---")
    print(f"Toplam sinyal: {len(rows)} | Kapanan: {n_closed} | Kazanan(TP): {len(wins)}")
    print(f"Kazanma orani: %{win_rate:.1f} | Toplam getiri: %{total_pnl:.1f} | Islem basi ort.: %{avg_pnl:.2f}")
    return {'label': label, 'total': len(rows), 'closed': n_closed, 'wins': len(wins),
            'win_rate': win_rate, 'total_pnl': total_pnl, 'avg_pnl': avg_pnl}


# ==================== BACKTEST ====================
def run_backtest():
    now_ms = int(time.time() * 1000)
    test_start_ms = now_ms - CFG['DAYS_BACK'] * DAY_MS
    # rolling 24h hacim + LEVEL_LOOKBACK icin yeterli warmup (buyuk olani al)
    warmup_ms = max(DAY_MS, CFG['LEVEL_LOOKBACK_CANDLES'] * WINDOW_MS + DAY_MS)
    fetch_start_ms = test_start_ms - warmup_ms

    log.info(f"Backtest: son {CFG['DAYS_BACK']} gun | ACIK MUMDA %{CFG['BODY_PCT']} esik | "
              f"%{CFG['CONFIRM_PCT']} onay | min hacim ${CFG['MIN_VOLUME_24H']:,.0f}")
    log.info(f"Test edilmis seviye: son {CFG['LEVEL_LOOKBACK_CANDLES']} mum | "
              f"tolerans %{CFG['LEVEL_TOLERANCE_PCT']} | min {CFG['LEVEL_MIN_TOUCHES']} dokunus")
    log.info(f"TP/SL: SL %{CFG['STOP_LOSS_PCT']} | TP %{CFG['TAKE_PROFIT_PCT']}")

    symbols = get_all_symbols()
    log.info(f"{len(symbols)} sembol bulundu - bu islem uzun surebilir, sabirla bekleyin.")

    all_results = []
    total_events = 0
    total_confirmed = 0
    incomplete_symbols = []
    done = 0

    with ThreadPoolExecutor(max_workers=CFG['MAX_WORKERS']) as executor:
        futures = {
            executor.submit(process_symbol, sym, fetch_start_ms, test_start_ms, now_ms): sym
            for sym in symbols
        }
        for future in as_completed(futures):
            sym = futures[future]
            try:
                results, stats, incomplete = future.result()
                all_results.extend(results)
                total_events += stats['events']
                total_confirmed += stats['confirmed']
                if incomplete:
                    incomplete_symbols.append(sym)
            except Exception as e:
                log.error(f"Hata {sym}: {e}")

            done += 1
            if done % 25 == 0:
                log.info(f"[{done}/{len(symbols)}] tamamlandi | Onaylanan: {len(all_results)}")

    if all_results:
        df = pd.DataFrame(all_results)
        df.to_csv(CFG['CSV_OUTPUT'], index=False, encoding='utf-8-sig')
        log.info(f"CSV kaydedildi: {CFG['CSV_OUTPUT']} | {len(all_results)} sinyal")
    else:
        log.warning("Sonuc bulunamadi")

    print("\n" + "=" * 60)
    print("BACKTEST SONUC")
    print("=" * 60)
    print(f"Taranan coin: {len(symbols)}")
    print(f"Toplam olay: {total_events} | Onaylanan: {total_confirmed}")

    summarize(all_results, "TUM ONAYLANAN SINYALLER", lambda r: True)
    summarize(all_results, f"TESTED (>={CFG['LEVEL_MIN_TOUCHES']} dokunus)", lambda r: r['is_tested'])
    summarize(all_results, f"UNTESTED (<{CFG['LEVEL_MIN_TOUCHES']} dokunus)", lambda r: not r['is_tested'])

    if incomplete_symbols:
        print(f"\n1dk verisi EKSIK KALAN coinler ({len(incomplete_symbols)}): {incomplete_symbols}")
    print("=" * 60)

    return all_results


if __name__ == '__main__':
    print("=" * 60)
    print("%7 KIRILIM + ONAY + TEST EDILMIS SEVIYE + TP/SL BACKTEST")
    print("=" * 60)
    run_backtest()
