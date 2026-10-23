import os, time, requests
import numpy as np
import pandas as pd

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")

BASE = "https://fapi.binance.com"
TF = "15m"
DAYS = 30
TOP_N = 150          # hacme göre ilk 150 coin
MIN_VOL = 1_000_000  # 24s hacim (USDT)
SL, TP = 3.0, 8.0    # yüzde
FEE = 0.1            # gidiş-dönüş komisyon (%)
MAX_HOLD = 96        # en fazla 96 mum (24 saat) tut
WINDOW = 3           # birlikte sinyal için pencere (mum)
GAP = 5              # aynı coinde sinyaller arası min mum


def get(path, params=None):
    for _ in range(5):
        r = requests.get(BASE + path, params=params, timeout=15)
        if r.status_code in (418, 429):
            time.sleep(30)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate limit")


def symbols():
    t = get("/fapi/v1/ticker/24hr")
    t = [x for x in t if x["symbol"].endswith("USDT") and float(x["quoteVolume"]) >= MIN_VOL]
    t.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [x["symbol"] for x in t[:TOP_N]]


def candles(sym):
    need = DAYS * 96 + 100
    rows, end = [], None
    while len(rows) < need:
        p = {"symbol": sym, "interval": TF, "limit": 1500}
        if end:
            p["endTime"] = end
        k = get("/fapi/v1/klines", p)
        if not k:
            break
        rows = k + rows
        end = k[0][0] - 1
        time.sleep(0.12)
    df = pd.DataFrame(rows).iloc[:, :10]
    df.columns = ["t", "open", "high", "low", "close", "vol", "ct", "qv", "n", "tb"]
    df = df.astype({c: float for c in ["open", "high", "low", "close", "vol", "tb"]})
    df["t"] = pd.to_datetime(df["t"], unit="ms")
    df["delta"] = 2 * df["tb"] - df["vol"]   # gerçek taker delta
    return df.iloc[:-1].tail(need).reset_index(drop=True)  # son (kapanmamış) mumu at


def rma(s, n):
    return s.ewm(alpha=1 / n, adjust=False).mean()


def supertrend(df, n=10, m=3.0):
    h, l, c = df.high.values, df.low.values, df.close.values
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = rma(pd.Series(tr), n).values
    src = (h + l) / 2
    ub, lb = src + m * atr, src - m * atr
    up = np.ones(len(c), bool)
    for i in range(1, len(c)):
        if lb[i] <= lb[i - 1] and c[i - 1] >= lb[i - 1]:
            lb[i] = lb[i - 1]
        if ub[i] >= ub[i - 1] and c[i - 1] <= ub[i - 1]:
            ub[i] = ub[i - 1]
        up[i] = (c[i] >=
