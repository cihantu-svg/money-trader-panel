import time
import json
import csv
import os
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# ---- AYARLAR ----
MIN_HACIM_USD = 1_000_000   # 24 saatlik minimum hacim
MIN_YUKSELIS = 10.0         # açık 15dk mumda açılıştan tepeye minimum % artış
TARAMA_SANIYE = 7 * 60      # 7 dakikada bir tara
DEGERLENDIRME_MS = 24 * 3600 * 1000
GUN_SONU_SAAT = 23          # her gün bu saatten sonra rapor + CSV gönderir (sunucu saati)

# Render'da Environment kısmına yaz, ya da buraya direkt yapıştır
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "BURAYA_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "BURAYA_CHAT_ID")

JSON_DOSYA = "sinyaller.json"
CSV_DOSYA = "sonuclar.csv"
# -----------------------

BASE = "https://api.binance.com"

ALANLAR = [
    "zaman", "sembol", "giris", "acilis", "tepe", "yukselis", "anlik", "geri_cekilme",
    "mum_dk", "mum_hacim_usd", "alim_orani", "islem_sayisi", "hacim24_usd",
    "hacim_payi", "degisim24", "btc24",
    "son_fiyat24", "degisim_24s", "max_yuk", "max_dus", "sonuc",
]

sinyaller = []
if os.path.exists(JSON_DOSYA):
    with open(JSON_DOSYA, "r", encoding="utf-8") as f:
        sinyaller = json.load(f)
bildirilen = {(s["sembol"], s["mum_acilis_ms"]) for s in sinyaller}
durum = {"son_gun_sonu": None}


# ---------- TELEGRAM ----------
def telegram_mesaj(metin):
    if "BURAYA" in TELEGRAM_TOKEN:
        return
    for i in range(0, len(metin), 4000):
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id":
