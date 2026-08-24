#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUMP PULLBACK RADAR v2.0 - Temiz Versiyon
Strateji:
  1. 1h mumda %10+ pump (3M USD hacim)
  2. 5m'de S/R desteğine pullback
  3. Backtest: %3 SL / %8 TP
  4. CSV + Telegram bildirim

Tarama (verimli):
  - Once tum coinler icin 1h verisi cek
  - Pump olanlari filtrele
  - SADECE pump olan coinlere 5m cek
  - Pullback ara
"""

import time
import os
import sys
import logging
from datetime import datetime, timedelta, timezone
import requests
import numpy as np
import pandas as pd

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# ==================== ZAMAN ====================
LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

def now():
    return datetime.now(LONDON)

def fmt(dt):
    if dt is None:
        return 'N/A'
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LONDON).strftime('%Y-%m-%d %H:%M:%S %Z')

# ==================== LOGGING ====================
class Fmt(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, LONDON).strftime('%Y-%m-%d %H:%M:%S %Z')

h = logging.StreamHandler(sys.stdout)
h.setFormatter(Fmt('%(asctime)s | %(levelname)s | %(message)s'))
fh = logging.FileHandler('pump_pullback.log', encoding='utf-8')
fh.setFormatter(Fmt('%(asctime)s | %(levelname)s | %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[h, fh])
log = logging.getLogger('PUMP_PB')

# ==================== AYARLAR ====================
# ASAGIDAKI DEGERLERI KENDINE GORE DEGISTIR
CFG = {
    'API_BASE': 'https://fapi.binance.com',
    'MAX_COINS': 300,
    'MIN_VOLUME_USD': 3_000_000,
    'PUMP_PCT': 10.0,
    'PULLBACK_WINDOW': 50,
    'SL_PCT': 3.0,
    'TP_PCT': 8.0,
    'TELEGRAM_BOT_TOKEN': os.getenv('TELEGRAM_BOT_TOKEN', ''),  # BOS BIRAKMA
    'TELEGRAM_CHAT_ID': os.getenv('TELEGRAM_CHAT_ID', ''),      # BOS BIRAKMA
    'BACKTEST_MODE': True,
    'BACKTEST_START': '2024-01-01',
    'BACKTEST_END': '2024-01-22',
    'CSV_OUTPUT': 'pump_pullback_results.csv',
    'API_DELAY': 0.3,        # 0.3sn = guvenli (3 req/s)
    'RETRY_MAX': 5,
    'RETRY_BASE': 3,
}

# ==================== API ====================
class API:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({'User-Agent': 'PUMP-PB/2.0'})
        self.base = CFG['API_BASE']
        self.last = 0
        self.delay = CFG['API_DELAY']
        self.count = 0

    def _rl(self):
        n = time.time()
        wait = self.delay - (n - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.count += 1

    def get(self, endpoint, params=None, retries=0):
        self._rl()
        try:
            r = self.s.get(f"{self.base}{endpoint}", params=params, timeout=20)

            if r.status_code == 429:
                wait = int(r.headers.get('Retry-After', CFG['RETRY_BASE'] * (2 ** retries)))
                if retries < CFG['RETRY_MAX']:
                    log.warning(f"429 -> {wait}sn bekleniyor ({retries+1}/{CFG['RETRY_MAX']})")
                    time.sleep(wait)
                    return self.get(endpoint, params, retries + 1)
                log.error("429 - Max retry")
                return None

            if r.status_code == 418:
                log.error("IP BAN (418) - 2dk bekleniyor...")
                time.sleep(120)
                if retries < CFG['RETRY_MAX']:
                    return self.get(endpoint, params, retries + 1)
                return None

            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error(f"API hata: {e}")
            return None

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=1500):
        p = {'symbol': symbol, 'interval': interval, 'limit': limit}
        if start_ms:
            p['startTime'] = int(start_ms)
        if end_ms:
            p['endTime'] = int(end_ms)
        d = self.get('/fapi/v1/klines', p)
        if not d or not isinstance(d, list):
            return None
        df = pd.DataFrame(d, columns=[
            'open_time','open','high','low','close','volume',
            'close_time','quote_volume','trades','taker_buy_base',
            'taker_buy_quote','ignore'
        ])
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms', utc=True).dt.tz_convert(LONDON)
        df['close_time'] = pd.to_datetime(df['close_time'], unit='ms', utc=True).dt.tz_convert(LONDON)
        for c in ['open','high','low','close','volume','quote_volume']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        return df.dropna()

api = API()

# ==================== SEMBOLLER ====================
_sym_cache = None
_sym_cache_t = 0

def get_symbols():
    global _sym_cache, _sym_cache_t
    if _sym_cache and (time.time() - _sym_cache_t) < 3600:
        return _sym_cache

    info = api.get('/fapi/v1/exchangeInfo')
    if not info:
        return []

    syms = [s['symbol'] for s in info.get('symbols', [])
            if s.get('status') == 'TRADING' and s.get('contractType') == 'PERPETUAL' and s['symbol'].endswith('USDT')]
    syms = sorted(syms)[:CFG['MAX_COINS']]
    _sym_cache = syms
    _sym_cache_t = time.time()
    log.info(f"{len(syms)} sembol yuklendi")
    return syms

# ==================== VERI CEKME ====================
def fetch_range(symbol, interval, start_dt, end_dt):
    all_rows = []
    interval_ms = {'1h': 3600000, '5m': 300000, '1m': 60000}[interval]
    start_ms = int(start_dt.astimezone(UTC).timestamp() * 1000)
    end_ms = int(end_dt.astimezone(UTC).timestamp() * 1000)
    cursor = start_ms

    while cursor < end_ms:
        df = api.klines(symbol, interval, start_ms=cursor, end_ms=end_ms, limit=1500)
        if df is None or len(df) == 0:
            break

        all_rows.append(df)
        last_open = int(df['open_time'].iloc[-1].timestamp() * 1000)
        next_cursor = last_open + interval_ms

        if next_cursor <= cursor or len(df) < 1500:
            break
        cursor = next_cursor
        time.sleep(0.05)

    if not all_rows:
        return None

    combined = pd.concat(all_rows, ignore_index=True)
    combined = combined.drop_duplicates(subset=['open_time']).sort_values('open_time').reset_index(drop=True)
    combined = combined[(combined['open_time'] >= start_dt) & (combined['open_time'] <= end_dt)]
    return combined.reset_index(drop=True)

# ==================== S/R ====================
def compute_sr(df, left=5, right=5):
    h = df['high'].values
    l = df['low'].values
    n = len(df)
    ph = np.zeros(n, dtype=bool)
    pl = np.zeros(n, dtype=bool)

    for i in range(left, n - right):
        if h[i] >= np.max(h[i-left:i]) and h[i] >= np.max(h[i+1:i+right+1]):
            ph[i] = True
        if l[i] <= np.min(l[i-left:i]) and l[i] <= np.min(l[i+1:i+right+1]):
            pl[i] = True

    res = df.loc[ph, 'high'].tail(3).values
    sup = df.loc[pl, 'low'].tail(3).values
    return (float(sup[-1]) if len(sup) else None,
            float(res[-1]) if len(res) else None)

# ==================== PUMP TESPITI ====================
def find_pumps(df_1h):
    if df_1h is None or len(df_1h) < 2:
        return []
    df = df_1h.copy()
    df['pct'] = (df['close'] - df['open']) / df['open'] * 100
    pumps = df[(df['pct'] >= CFG['PUMP_PCT']) & (df['quote_volume'] >= CFG['MIN_VOLUME_USD'])]
    return pumps.index.tolist()

# ==================== PULLBACK ====================
def find_pullback(df_5m, window=50):
    if df_5m is None or len(df_5m) < window:
        return None
    w = df_5m.iloc[-window:].copy().reset_index(drop=True)
    sup, _ = compute_sr(w)
    if sup is None:
        return None
    for i in range(len(w)):
        if w['low'].iloc[i] <= sup * 1.005 and w['close'].iloc[i] > sup:
            return i, sup, w['close'].iloc[i], w['close_time'].iloc[i]
    return None

# ==================== POZISYON ====================
def simulate(entry_price, df_after, entry_idx):
    if entry_idx >= len(df_after):
        return False, False, entry_price, None, 0, 0

    sl = entry_price * (1 - CFG['SL_PCT'] / 100)
    tp = entry_price * (1 + CFG['TP_PCT'] / 100)
    sl_hit = tp_hit = False
    exit_p = entry_price
    exit_t = None
    max_dd = max_prof = 0

    for i in range(entry_idx, len(df_after)):
        low = df_after['low'].iloc[i]
        high = df_after['high'].iloc[i]
        ts = df_after['close_time'].iloc[i]

        dd = (low - entry_price) / entry_price * 100
        prof = (high - entry_price) / entry_price * 100
        max_dd = min(max_dd, dd)
        max_prof = max(max_prof, prof)

        if low <= sl:
            sl_hit = True
            exit_p = sl
            exit_t = ts
            break
        if high >= tp:
            tp_hit = True
            exit_p = tp
            exit_t = ts
            break

    if not sl_hit and not tp_hit:
        exit_p = df_after['close'].iloc[-1]
        exit_t = df_after['close_time'].iloc[-1]

    return sl_hit, tp_hit, exit_p, exit_t, max_dd, max_prof

# ==================== TELEGRAM ====================
def send_tg(text, csv_path=None):
    tok = CFG['TELEGRAM_BOT_TOKEN']
    cid = CFG['TELEGRAM_CHAT_ID']

    if not tok or not cid:
        log.warning("Telegram token/chat_id bos - mesaj atilmadi")
        log.warning("Ayarlamak icin:")
        log.warning("  export TELEGRAM_BOT_TOKEN='123456:ABC...'")
        log.warning("  export TELEGRAM_CHAT_ID='123456789'")
        return False

    try:
        # Mesaj gonder
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={'chat_id': cid, 'text': text, 'parse_mode': 'HTML'},
            timeout=15
        )
        if r.status_code != 200:
            log.error(f"TG mesaj hatasi: {r.text}")
            return False
        log.info("Telegram mesaji gonderildi")

        # CSV gonder
        if csv_path and os.path.exists(csv_path):
            with open(csv_path, 'rb') as f:
                r2 = requests.post(
                    f"https://api.telegram.org/bot{tok}/sendDocument",
                    files={'document': f},
                    data={'chat_id': cid},
                    timeout=60
                )
                if r2.status_code == 200:
                    log.info("CSV Telegram'a gonderildi")
                else:
                    log.error(f"TG CSV hatasi: {r2.text}")
        return True
    except Exception as e:
        log.error(f"TG hata: {e}")
        return False

# ==================== BACKTEST ====================
class Backtest:
    def __init__(self):
        self.results = []
        self.csv = CFG['CSV_OUTPUT']
        self.stats = {'scanned': 0, 'pump': 0, 'signal': 0, 'sl': 0, 'tp': 0, 'err': 0}

    def run(self):
        log.info("=" * 60)
        log.info("PUMP PULLBACK BACKTEST")
        log.info(f"Zaman: {fmt(now())}")
        log.info(f"Periyot: {CFG['BACKTEST_START']} - {CFG['BACKTEST_END']}")
        log.info(f"Coin: {CFG['MAX_COINS']} | Pump: %{CFG['PUMP_PCT']}+ | SL/TP: %{CFG['SL_PCT']}/%{CFG['TP_PCT']}")
        log.info("=" * 60)

        syms = get_symbols()
        if not syms:
            log.error("Sembol yok")
            return

        start = datetime.strptime(CFG['BACKTEST_START'], '%Y-%m-%d').replace(tzinfo=LONDON)
        end = datetime.strptime(CFG['BACKTEST_END'], '%Y-%m-%d').replace(tzinfo=LONDON)

        # ADIM 1: Tum coinler icin 1h verisi
        log.info(f"ADIM 1: {len(syms)} coin icin 1h verisi cekiliyor...")
        df1h_by_sym = {}
        pump_events = []

        for idx, sym in enumerate(syms, 1):
            try:
                df1h = fetch_range(sym, '1h', start, end)
                if df1h is not None and len(df1h) > 0:
                    df1h_by_sym[sym] = df1h
                    self.stats['scanned'] += len(df1h)
                    pidxs = find_pumps(df1h)
                    for pidx in pidxs:
                        pump_events.append({
                            'symbol': sym,
                            'pidx': pidx,
                            'time': df1h['close_time'].iloc[pidx],
                            'close': float(df1h['close'].iloc[pidx]),
                            'pct': float((df1h['close'].iloc[pidx] - df1h['open'].iloc[pidx]) / df1h['open'].iloc[pidx] * 100),
                            'vol': float(df1h['quote_volume'].iloc[pidx])
                        })
            except Exception as e:
                self.stats['err'] += 1
                log.error(f"1h hata {sym}: {e}")

            if idx % 20 == 0:
                log.info(f"[{idx}/{len(syms)}] 1h tamamlandi, {len(pump_events)} pump bulundu")

        self.stats['pump'] = len(pump_events)
        log.info(f"1h tamamlandi: {len(pump_events)} pump olayi ({len(df1h_by_sym)} coin)")

        if not pump_events:
            log.warning("Pump bulunamadi")
            send_tg("<b>PUMP PULLBACK</b>
Sonuc bulunamadi.")
            return

        # ADIM 2: Sadece pump olan coinlere 5m cek
        log.info("=" * 60)
        pump_symbols = list(set(e['symbol'] for e in pump_events))
        log.info(f"ADIM 2: {len(pump_symbols)} coin icin 5m verisi cekiliyor...")

        df5m_by_sym = {}
        for idx, sym in enumerate(pump_symbols, 1):
            try:
                df5m = fetch_range(sym, '5m', start, end)
                if df5m is not None and len(df5m) > 0:
                    df5m_by_sym[sym] = df5m
            except Exception as e:
                log.error(f"5m hata {sym}: {e}")
            if idx % 10 == 0:
                log.info(f"[{idx}/{len(pump_symbols)}] 5m tamamlandi")

        # ADIM 3: Pullback ara
        log.info("=" * 60)
        log.info(f"ADIM 3: {len(pump_events)} pump icin pullback araniyor...")

        for idx, ev in enumerate(pump_events, 1):
            sym = ev['symbol']
            df5m = df5m_by_sym.get(sym)
            if df5m is None or len(df5m) < 50:
                continue

            try:
                after = df5m[df5m['open_time'] > ev['time']].copy().reset_index(drop=True)
                if len(after) < 10:
                    continue

                pb = find_pullback(after, CFG['PULLBACK_WINDOW'])
                if pb is None:
                    continue

                _, sup_level, entry_p, entry_t = pb
                sl_hit, tp_hit, exit_p, exit_t, max_dd, max_prof = simulate(entry_p, after, pb[0] + 1)
                result_pct = (exit_p - entry_p) / entry_p * 100

                self.stats['signal'] += 1
                if sl_hit:
                    self.stats['sl'] += 1
                if tp_hit:
                    self.stats['tp'] += 1

                self.results.append({
                    'symbol': sym,
                    'pump_time': fmt(ev['time']),
                    'pump_pct': round(ev['pct'], 2),
                    'pump_volume_usd': round(ev['vol'], 2),
                    'entry_time': fmt(entry_t),
                    'entry_price': round(entry_p, 6),
                    'support': round(sup_level, 6),
                    'sl_hit': sl_hit,
                    'tp_hit': tp_hit,
                    'exit_price': round(exit_p, 6),
                    'exit_time': fmt(exit_t),
                    'result_pct': round(result_pct, 2),
                    'max_dd_pct': round(max_dd, 2),
                    'max_profit_pct': round(max_prof, 2),
                })

            except Exception as e:
                self.stats['err'] += 1
                log.error(f"Pullback hata {sym}: {e}")

            if idx % 50 == 0:
                log.info(f"[{idx}/{len(pump_events)}] pump islendi, {self.stats['signal']} sinyal")

        self._save()
        self._report()

        # Telegram
        msg = self._summary()
        send_tg(msg, self.csv)

        log.info("BACKTEST BITTI")

    def _save(self):
        if not self.results:
            log.warning("Sonuc yok, CSV kaydedilmedi")
            return
        df = pd.DataFrame(self.results)
        df.to_csv(self.csv, index=False, encoding='utf-8-sig')
        log.info(f"CSV kaydedildi: {self.csv} | {len(self.results)} sinyal")

    def _report(self):
        log.info("=" * 60)
        log.info("SONUCLAR")
        log.info(f"Taranan 1h mum: {self.stats['scanned']}")
        log.info(f"Pump: {self.stats['pump']} | Sinyal: {self.stats['signal']}")
        log.info(f"SL: {self.stats['sl']} | TP: {self.stats['tp']}")
        log.info(f"Hata: {self.stats['err']} | API cagrisi: {api.count}")
        if self.stats['signal'] > 0:
            log.info(f"TP Orani: {self.stats['tp']/self.stats['signal']*100:.1f}%")
        log.info("=" * 60)

    def _summary(self):
        if not self.results:
            return "<b>PUMP PULLBACK</b>
Sonuc bulunamadi."
        df = pd.DataFrame(self.results)
        win = self.stats['tp']
        loss = self.stats['sl']
        total = self.stats['signal']
        tp_rate = win / total * 100 if total else 0
        avg_res = df['result_pct'].mean()
        return f"""<b>PUMP PULLBACK Backtest</b>

Sinyal: {total}
TP: {win} | SL: {loss}
TP Orani: {tp_rate:.1f}%
Ort. Getiri: {avg_res:.2f}%
API: {api.count}

CSV ekte."""

# ==================== MAIN ====================
if __name__ == '__main__':
    print(f"""
    ============================================
    PUMP PULLBACK RADAR v2.0
    {fmt(now())}
    ============================================
    """)

    # Telegram kontrol
    if not CFG['TELEGRAM_BOT_TOKEN'] or not CFG['TELEGRAM_CHAT_ID']:
        print("
[!] UYARI: Telegram token/chat_id ayarlanmamis!")
        print("    Mesaj gitmeyecek. Ayarlamak icin:")
        print("    export TELEGRAM_BOT_TOKEN='123456:ABC...'")
        print("    export TELEGRAM_CHAT_ID='123456789'")
        print()

    Backtest().run()
