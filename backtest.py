import sys
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "bt_results/tum_islemler.csv"
tf_filter = sys.argv[2] if len(sys.argv) > 2 else None

df = pd.read_csv(path)
df = df[df["reason"] != "OPEN"].copy()
if tf_filter:
    df = df[df["timeframe"] == tf_filter]
if df.empty:
    print("Veri yok.")
    sys.exit()

df["win"] = (df["reason"] == "TP").astype(int)
df["hour"] = pd.to_datetime(df["signal_time"]).dt.hour
df["body_bin"] = pd.cut(df["m1_body_pct"], [0, 3, 4, 6, 10, 1000],
                        labels=["2-3%", "3-4%", "4-6%", "6-10%", "10%+"])
df["vol_bin"] = pd.cut(df["vol_multi"], [0, 4, 5, 8, 15, 10000],
                       labels=["3-4x", "4-5x", "5-8x", "8-15x", "15x+"])


def table(col, title):
    g = df.groupby(col, observed=True).agg(
        islem=("win", "size"),
        tp_orani=("win", lambda x: round(x.mean() * 100, 1)),
        ort_getiri=("pnl_pct", lambda x: round(x.mean(), 3)),
        toplam=("pnl_pct", lambda x: round(x.sum(), 1)),
    )
    g["not"] = g["islem"].apply(lambda n: "az veri" if n < 20 else "")
    print(f"\n--- {title} ---")
    print(g.to_string())


print(f"Toplam işlem: {len(df)} | Zaman dilimi: {tf_filter or 'hepsi'}")
for tf, sub in df.groupby("timeframe"):
    print(f"  {tf}: {len(sub)} işlem")

table("timeframe", "Zaman dilimine göre")
table("direction", "Yöne göre")
table("body_bin", "1. mum gövde büyüklüğüne göre")
table("vol_bin", "Hacim çarpanına göre")
table("hour", "Saate göre (UTC)")
table("symbol", "Sembole göre (en çok işlem)") if False else None

top = df.groupby("symbol")["pnl_pct"].agg(["size", "sum"]).sort_values("size", ascending=False).head(10)
print("\n--- En çok sinyal veren 10 sembol ---")
print(top.round(1).to_string())
