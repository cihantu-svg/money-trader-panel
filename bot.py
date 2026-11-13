import sys
import subprocess

# aiohttp yüklü değilse otomatik yükle (Shell'le uğraşmazsın)
try:
    import aiohttp
except ImportError:
    print("aiohttp kütüphanesi eksik, otomatik olarak yükleniyor...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp"])
    import aiohttp

import os
import time
import asyncio
import requests

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

MUM_YUZDE = 7         # 15dk mum boyu (yüksek-düşük) en az %
SMA_LEN = 100         # 4H SMA periyodu
OI_ESIK = 2.0         # GÜÇLÜ mesaj için mum süresince OI artışı en az %
TARAMA_ARALIK = 180   # tarama sıklığı (sn)
COOLDOWN = 1 * 3600   # aynı coinden tekrar mesaj için bekleme (1 saat)

son_mesaj = {}        # normal mesaj: "SEMBOL" -> son mesaj zamanı
son_guclu = {}        # güçlü mesaj:  "SEMBOL" -> son mesaj zamanı
ilk_tarama = True


async def fetch(session, semaphore, path, **p):
    """Binance API'den asenkron veri çeker, rate limit korumalıdır"""
    url = BASE + path
    for _ in range(2):
        async with semaphore:
            try:
                async with session.get(url, params=p, timeout=15) as r:
                    if r.status in (418, 429):
                        retry = int(r.headers.get("Retry-After", 120))
                        await asyncio.sleep(retry)
                        continue
                    if r.status == 200:
                        return await r.json()
            except Exception:
                pass
        await asyncio.sleep(1)
    return None


async def telegram(session, msg):
    """Telegram mesajını asenkron gönderir"""
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        async with session.post(url, data={"chat_id": CHAT, "text": msg}, timeout=15) as r:
            pass
    except Exception as e:
        print("telegram hata:", e)


async def oi_degisim(session, semaphore, s, mum_acilis_ms):
    """Mum açılışından bu yana Open Interest değişimi (%)"""
    h = await fetch(session, semaphore, "/futures/data/openInterestHist", symbol=s, period="5m", limit=12)
    if not h or not isinstance(h, list):
        return None
    noktalar = [(int(x["timestamp"]), float(x["sumOpenInterest"])) for x in h]
    if len(noktalar) < 2:
        return None
    onceki = [n for n in noktalar if n[0] <= mum_acilis_ms]
    if not onceki:
        return None
    bas = onceki[-1][1]
    son = noktalar[-1][1]
    if bas <= 0:
        return None
    return (son - bas) / bas * 100


async def process_symbol(session, semaphore, s):
    """Tek bir coini asenkron olarak kontrol eder ve sinyal üretirse bildirir"""
    global ilk_tarama
    simdi = time.time()
    normal_bekle = simdi - son_mesaj.get(s, 0) < COOLDOWN
    guclu_bekle = simdi - son_guclu.get(s, 0) < COOLDOWN
    if normal_bekle and guclu_bekle:
        return

    # 15dk mum verisi
    k = await fetch(session, semaphore, "/fapi/v1/klines", symbol=s, interval="15m", limit=3)
    if not k or len(k) < 2:
        return
    k = k[:-1]  # Son kapanmamış mumu atla
    if not k:
        return
    
    m = k[-1]
    acilis_ms = int(m[0])
    acilis, yuksek, dusuk, kapanis = float(m[1]), float(m[2]), float(m[3]), float(m[4])
    boy = (yuksek - dusuk) / dusuk * 100
    if boy < MUM_YUZDE:
        return

    # 4h SMA verisi
    h4 = await fetch(session, semaphore, "/fapi/v1/klines", symbol=s, interval="4h", limit=SMA_LEN)
    if not h4 or len(h4) < SMA_LEN:
        return
    sma = sum(float(x[4]) for x in h4) / SMA_LEN
    if not (dusuk <= sma <= yuksek):
        return

    yesil = kapanis >= acilis
    yon = "🟢 yeşil" if yesil else "🔴 kırmızı"

    # ── 1) NORMAL MESAJ ──
    if not normal_bekle:
        son_mesaj[s] = time.time()
        if not ilk_tarama:
            await telegram(session, f"📍 4H SMA100 TEMASI {s}\n"
                                   f"15dk mum: %{boy:.1f} ({yon})\n"
                                   f"Mum: {dusuk} - {yuksek}\n"
                                   f"4H SMA100: {sma:.6g}")

    # ── 2) GÜÇLÜ MESAJ (OI ONAYLI) ──
    if not guclu_bekle:
        oi = await oi_degisim(session, semaphore, s, acilis_ms)
        if oi is not None and oi >= OI_ESIK:
            son_guclu[s] = time.time()
            if not ilk_tarama:
                baslik = "🔥🟢 GÜÇLÜ LONG" if yesil else "🔥🔴 GÜÇLÜ SHORT"
                await telegram(session, f"{baslik} (OI ONAYLI) {s}\n"
                                       f"4H SMA100 teması + Open Interest artışı\n"
                                       f"15dk mum: %{boy:.1f} ({yon})\n"
                                       f"OI değişimi: +%{oi:.1f}\n"
                                       f"Mum: {dusuk} - {yuksek}\n"
                                       f"4H SMA100: {sma:.6g}")


async def tara():
    global ilk_tarama
    async with aiohttp.ClientSession() as session:
        # Binance ban yememek için aynı anda maksimum 20 eşzamanlı istek
        semaphore = asyncio.Semaphore(20)

        tickers = await fetch(session, semaphore, "/fapi/v1/ticker/24hr")
        if not tickers:
            print("Ticker listesi alınamadı!")
            return

        semboller = [t["symbol"] for t in tickers if t["symbol"].endswith("USDT") and "_" not in t["symbol"]]
        print(f"Toplam {len(semboller)} coin asenkron olarak taranıyor...")
        
        baslangic = time.time()
        
        tasks = [process_symbol(session, semaphore, s) for s in semboller]
        await asyncio.gather(*tasks)
        
        print(f"Tarama bitti. Geçen süre: {time.time() - baslangic:.2f} saniye")

    ilk_tarama = False


async def main():
    while True:
        try:
            await tara()
        except Exception as e:
            print("hata:", e)
        await asyncio.sleep(TARAMA_ARALIK)


if __name__ == "__main__":
    asyncio.run(main())
