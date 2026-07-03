# MONEY TRADER — MEXC Kripto Tarama Paneli (Web Sürümü)

Bu, orijinal tkinter masaüstü uygulamasının **Streamlit tabanlı web sürümüdür**. Sinyal mantığı
(RSI/Fibo/Momentum/OBV/Quantum hesaplamaları, repaint önleme kuralları) birebir korunmuştur —
tek fark arayüzün artık tarayıcıda çalışmasıdır.

## En Hızlı Yol: Streamlit Community Cloud (Ücretsiz)

1. **GitHub hesabınız yoksa** github.com üzerinden ücretsiz bir hesap açın.
2. Yeni bir **repo** oluşturun (örn. `money-trader-panel`), **Public** veya **Private** olabilir.
3. Bu 2 dosyayı repoya yükleyin:
   - `app.py`
   - `requirements.txt`
4. https://share.streamlit.io adresine gidin, GitHub hesabınızla giriş yapın.
5. **"New app"** → repo'nuzu seçin → Main file path olarak `app.py` yazın → **Deploy**.
6. 1-2 dakika içinde `https://sizin-uygulamaniz.streamlit.app` şeklinde bir link alırsınız.
   Bu linki telefonunuzdan, herhangi bir tarayıcıdan açabilirsiniz.

Tamamen ücretsizdir, sunucu yönetimi gerektirmez.

## Alternatif: Kendi Sunucunuzda / VPS'te Çalıştırma

```bash
pip install -r requirements.txt
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

Sonra `http://sunucu-ip-adresiniz:8501` üzerinden erişilebilir. Kalıcı çalışması için
`tmux`, `screen` veya `systemd` servis olarak arka planda çalıştırmanız önerilir.

## Önemli Notlar

- **Tarama süresi:** MEXC'de yüzlerce USDT paritesi var. Kenar çubuğundaki "Maksimum Coin Sayısı"
  ile taramayı sınırlayarak hız kazanabilirsiniz. Streamlit Cloud'un ücretsiz katmanında uzun
  süren işlemler zaman aşımına uğrayabilir; 100-300 coin ile başlamanız önerilir.
- **Aktif işlemler dosyası** (`active_trades_mexc.json`) uygulamanın çalıştığı sunucuda saklanır.
  Streamlit Community Cloud'da uygulama "uyku" moduna girip yeniden başlatılırsa bu dosya
  sıfırlanabilir. Kalıcı takip için kendi VPS'inizde çalıştırmanız daha güvenlidir.
- Bu araç **yatırım tavsiyesi değildir**, sinyaller bilgi amaçlıdır.

## Yerel Bilgisayarınızda Test Etmek İsterseniz

```bash
pip install -r requirements.txt
streamlit run app.py
```

Tarayıcınızda otomatik olarak `http://localhost:8501` açılacaktır.
