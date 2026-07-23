# -*- coding: utf-8 -*-
"""
MAJOR KIRILIM BOT
Strateji: SMA100 (Major/turuncu cizgi) kesisimi + kesen mumun govde
buyuklugu (BODY_PCT_MIN yuzde ve uzeri) - yesil mum yukari kesiste AL,
kirmizi mum asagi kesiste SAT sinyali uretir.
EK: Mumun dibi (low) SMA100'un uzerinde veya body range'inin max %25'i kadar altinda olmali (AL)
    Mumun tepesi (high) SMA100'un altinda veya body range'inin max %25'i kadar ustunde olmali (SAT)
Zaman dilimi: 1 saat
Tarama araligi: 5 dakika (ayarlanabilir)
Borsa: Binance Futures (USDT-M Perpetual)

Tarama artik PARALEL (ThreadPoolExecutor) + Binance IP limitine (2400 weight/dk)
karsi thread sayisindan bagimsiz calisan bir rate limiter ile korunuyor.
"""
import os
import time
import logging
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import numpy as np
import urllib3
import warnings

urllib3.disable_warnings()
warnings.filterwarnings('ignore')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)

# ============================================================
# AYARLAR (Render Environment Variables)
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL_SEC", "300"))  # 5 dakika
TIMEFRAME = os.environ.get("SCAN_TIMEFRAME", "1h")  # 1 saat
MAX_COINS = int(os.environ.get("MAX_COINS", "600"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME_USDT", "1000000"))

# Strateji parametreleri
MAJOR_LEN = int(os.environ.get("MAJOR_LEN", "100"))   # SMA100
SPANB_LEN = int(os.environ.get("SPANB_LEN", "52"))    # Span B periyodu
BREAK_PCT = float(os.environ.get("BREAK_PCT", "7.0")) # (artik kullanilmiyor - referans)
VOL_MA_LEN = int(os.environ.get("VOL_MA_LEN", "20"))  # (artik kullanilmiyor - referans)
VOL_MULT = float(os.environ.get("VOL_MULT", "4.0"))   # (artik kullanilmiyor - referans)
BODY_PCT_MIN = float(os.environ.get("BODY_PCT_MIN", "4.0"))   # Kesisim mumunun min govde yuzdesi
MAX_LINE_GAP_PCT = float(os.environ.get("MAX_LINE_GAP_PCT", "1.0"))  # SMA100 ile Span B arasi max mesafe %
SIGNAL_COOLDOWN = int(os.environ.get("SIGNAL_COOLDOWN", "3600"))  # 1 saat bekleme

# YENI: Dip onayi parametreleri
LOW_PCT_MAX = float(os.environ.get("LOW_PCT_MAX", "25.0"))   # AL: Mum low'unun body range'ine gore max %25'i SMA100 altinda kalabilir
HIGH_PCT_MAX = float(os.environ.get("HIGH_PCT_MAX", "25.0")) # SAT: Mum high'unun body range'ine gore max %25'i SMA100 ustunde kalabilir

# Paralel tarama + Binance IP limit korumasi
SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "10"))          # ayni anda kac coin paralel taransin
REQUESTS_PER_SEC = float(os.environ.get("REQUESTS_PER_SEC", "10"))  # Binance'e saniyede max istek (thread sayisindan bagimsiz)
TELEGRAM_MSGS_PER_SEC = float(os.environ.get("TELEGRAM_MSGS_PER_SEC", "1.0"))  # Telegram flood korumasi

BINANCE_BASE = "https://fapi.binance.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "MajorKirilimBot/1.0"})
_adapter = requests.adapters.HTTPAdapter(pool_connections=SCAN_WORKERS, pool_maxsize=SCAN_WORKERS * 2)
SESSION.mount("https://", _adapter)

# Gonderilen sinyalleri takip et
sent_signals: dict = {}

# ---- Binance rate limit korumasi: thread'ler arasi paylasilan token-bucket ----
_rate_lock = threading.Lock()
_next_slot = [0.0]


def _rate_limit_wait():
    """Toplam Binance istek hizini REQUESTS_PER_SEC ile sinirlar (thread sayisindan bagimsiz)."""
    with _rate_lock:
        now = time.time()
        wait = _next_slot[0]
