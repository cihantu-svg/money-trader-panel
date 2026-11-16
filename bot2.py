import os, time, asyncio, aiohttp

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
BASE = "https://fapi.binance.com"

TARAMA_ARALIK = 3600    # 1 saatte bir tara
ZAMAN_DILIMI = "4h"
MUM_YUZDE = 10          # açık 4H mum: AL için open->high, SAT için open->low %
BO_LEN = 20             # kırılım referansı: önceki 20 kapanmış mum
MIN_HACIM = 1_000_000   # 24s USDT hacim (0 yaparsan filtre kalkar)
ESZAMANLI = 10          # aynı anda kaç istek

gonderildi = set()      # aynı 4H mumda aynı coin/yön tekrar gitmesin


async def get(oturum, path, **p):
    for _ in range(3):
        async with oturum.get(BASE + path, params=p) as r:
            if r.status in (418, 429):
                bekle = int(r.headers.get("Retry-After", 60))
                print(f"Rate limit, {bekle} sn bekleniyor")
                await asyncio.sleep(bekle)
                continue
            r.raise_for_status()
            return await r.json()
    raise RuntimeError("Rate limit devam ediyor")


async def telegram(oturum, msg):
    try:
        async with oturum.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                               data={"chat_id": CHAT, "text": msg}) as r:
            await r.read()
    except Exception as e:
        print("telegram hata:", e)


async def kontrol(oturum, sem, t):
    s = t["symbol"]
    async with sem:
        try:
            k = await get(oturum, "/fapi/v1/klines", symbol=s,
                          interval=ZAMAN_DILIMI, limit=BO_LEN + 1)
        except Exception as e:
            print(s, e)
            return
    if len(k) < BO_LEN + 1:
        return

    acik = k[-1]                      # henüz kapanmamış 4H mum
    onceki = k[-BO_LEN - 1:-1]        # önceki 20 kapanmış mum
    mum_zaman = acik[0]
    o, h, l, fiyat = float(acik[1]), float(acik[2]), float(acik[3]), float(acik[4])
    direnc = max(float(x[2]) for x in onceki)
    destek = min(float(x[3]) for x in onceki)
    kalan_dk = max(0, (acik[6] / 1000 - time.time()) / 60)

    al_yuzde = (h - o) / o * 100
    sat_yuzde = (o - l) / o * 100

    sinyaller = []
    if h > direnc and al_yuzde >= MUM_YUZDE:
        geri = (h - fiyat) / h * 100
        sinyaller.append(("AL", "🚀 4H KIRILIM AL", f"+%{al_yuzde:.1f}", f"Tepeden geri çekilme: %{geri:.1f}"))
    if l < destek and sat_yuzde >= MUM_YUZDE:
        geri = (fiyat - l) / l * 100
        sinyaller.append(("SAT", "🔻 4H KIRILIM SAT", f"-%{sat_yuzde:.1f}", f"Dipten geri dönüş: %{geri:.1f}"))

    for yon, baslik, yuzde, geri_metin in sinyaller:
        anahtar = f"{s}|{yon}|{mum_zaman}"
        if anahtar in gonderildi:
            continue
        gonderildi.add(anahtar)
        await telegram(oturum,
            f"{baslik} {s}\n"
            f"4H mum (açık): {yuzde}\n"
            f"Fiyat: {fiyat} | {geri_metin}\n"
            f"Mum kapanışına: {kalan_dk:.0f} dk\n"
            f"24s hacim: {float(t['quoteVolume'])/1e6:.1f}M | 24s değişim: %{t['priceChangePercent']}")


async def tara(oturum):
    tickerlar = [t for t in await get(oturum, "/fapi/v1/ticker/24hr")
                 if t["symbol"].endswith("USDT") and "_" not in t["symbol"]
                 and float(t["quoteVolume"]) >= MIN_HACIM]
    print(f"{len(tickerlar)} coin taranıyor")
    sem = asyncio.Semaphore(ESZAMANLI)
    await asyncio.gather(*(kontrol(oturum, sem, t) for t in tickerlar))


async def main():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as oturum:
        while True:
            try:
                await tara(oturum)
            except Exception as e:
                print("hata:", e)
            await asyncio.sleep(TARAMA_ARALIK)


asyncio.run(main())
