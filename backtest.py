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
        up[i] = (c[i] >= lb[i]) if up[i - 1] else (c[i] > ub[i])
    return up


def signals(df):
    c, o, h, l, v, d = df.close, df.open, df.high, df.low, df.vol, df.delta
    hh = h.rolling(20).max().shift(1)
    ll = l.rolling(20).min().shift(1)
    vs = v.rolling(20).mean()

    # 1) Kırılım AL/SAT (FIBO TRADE mantığı)
    dc = c.diff()
    rsi = 100 - 100 / (1 + rma(dc.clip(lower=0), 14) / rma((-dc).clip(lower=0), 14))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = macd - macd.ewm(span=9, adjust=False).mean()
    aL = (c > hh * 1.005) & (v > 2 * vs) & (rsi > 60) & (hist > 0) & (macd > 0)
    aS = (c < ll * 0.995) & (v > 2 * vs) & (rsi < 40) & (hist < 0) & (macd < 0)

    # 2) Delta + Supertrend
    up = pd.Series(supertrend(df), index=df.index)
    prev = up.shift(1, fill_value=bool(up.iloc[0]))
    flipUp, flipDn = up & ~prev, ~up & prev
    ad = d.abs().rolling(20).mean()
    roll = d.rolling(5).sum()
    vok = v > 1.5 * vs
    bL = up & ((c > hh) | flipUp) & (c > o) & vok & (d > 1.5 * ad) & (roll > 0)
    bS = ~up & ((c < ll) | flipDn) & (c < o) & vok & (d < -1.5 * ad) & (roll < 0)

    # 3) İkisi WINDOW mum içinde birlikte
    near = lambda s: s.astype(int).rolling(WINDOW).max() > 0
    cL = near(aL) & near(bL) & (aL | bL)
    cS = near(aS) & near(bS) & (aS | bS)

    return {"1-Kirilim": (aL, aS), "2-Delta+ST": (bL, bS), "3-Birlikte": (cL, cS)}


def run(df, name, L, S, sym):
    out = []
    Lv, Sv = L.values, S.values
    op, hi, lo, cl = df.open.values, df.high.values, df.low.values, df.close.values
    n = len(df)
    busy, last = -1, -999
    for i in range(30, n - 2):
        if i <= busy or i - last < GAP:
            continue
        side = 1 if Lv[i] else (-1 if Sv[i] else 0)
        if not side:
            continue
        last = i
        entry = op[i + 1]
        sl = entry * (1 - side * SL / 100)
        tp = entry * (1 + side * TP / 100)
        end = min(i + 1 + MAX_HOLD, n - 1)
        res, ex, j = "TIME", cl[end], end
        for k in range(i + 1, end + 1):
            hit_sl = lo[k] <= sl if side == 1 else hi[k] >= sl
            hit_tp = hi[k] >= tp if side == 1 else lo[k] <= tp
            if hit_sl:
                res, ex, j = "SL", sl, k
                break
            if hit_tp:
                res, ex, j = "TP", tp, k
                break
        busy = j
        pnl = side * (ex - entry) / entry * 100 - FEE
        out.append({"strategy": name, "symbol": sym, "time": str(df.t.iloc[i]),
                    "side": "LONG" if side == 1 else "SHORT", "entry": entry,
                    "result": res, "pnl": round(pnl, 2), "bars": j - i})
    return out


def summarize(tr):
    lines = [f"BACKTEST {DAYS}g | {TF} | SL %{SL} TP %{TP} | komisyon %{FEE}\n"]
    for name in ["1-Kirilim", "2-Delta+ST", "3-Birlikte"]:
        g = tr[tr.strategy == name]
        if g.empty:
            lines.append(f"{name}\n  Sinyal yok\n")
            continue
        n, w = len(g), (g.pnl > 0).sum()
        gp, gl = g.pnl[g.pnl > 0].sum(), -g.pnl[g.pnl <= 0].sum()
        pf = gp / gl if gl else float("inf")
        lines.append(
            f"{name}\n  Sinyal: {n} (long {(g.side == 'LONG').sum()} / short {(g.side == 'SHORT').sum()})\n"
            f"  TP {(g.result == 'TP').sum()} | SL {(g.result == 'SL').sum()} | Sure {(g.result == 'TIME').sum()}\n"
            f"  Basari: %{w / n * 100:.1f} | Ort: %{g.pnl.mean():.2f} | Toplam: %{g.pnl.sum():.1f} | PF: {pf:.2f}\n")
    return "\n".join(lines)


def tg_msg(t):
    if TOKEN and CHAT:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT, "text": t}, timeout=30)


def tg_file(path):
    if TOKEN and CHAT:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendDocument",
                          data={"chat_id": CHAT}, files={"document": f}, timeout=60)


def main():
    syms = symbols()
    print(f"{len(syms)} coin taranacak")
    trades = []
    for n, sym in enumerate(syms, 1):
        try:
            df = candles(sym)
            if len(df) < 300:
                continue
            for name, (L, S) in signals(df).items():
                trades += run(df, name, L, S, sym)
        except Exception as e:
            print("hata", sym, e)
        if n % 20 == 0:
            print(f"{n}/{len(syms)} bitti")
    tr = pd.DataFrame(trades)
    if tr.empty:
        tg_msg("Backtest: hic sinyal cikmadi")
        return
    tr.to_csv("backtest_sonuc.csv", index=False)
    txt = summarize(tr)
    print(txt)
    tg_msg(txt)
    tg_file("backtest_sonuc.csv")


if __name__ == "__main__":
    main()
