# Kick Canlı Yayın Bildirim Botu

Bu bot, Discord sunucunda **takip ettiğin Kick yayıncılarından biri canlı
yayına geçtiğinde otomatik mesaj atar**. Bot, Railway adlı ücretsiz bir
bulut hizmetinde 7/24 çalışır — yani telefonun kapalı olsa bile bildirimler
gelmeye devam eder.

Kick'i her **20 saniyede bir** kontrol eder.

---

## Ne yapman gerekiyor? (Genel bakış)

1. Discord'da bir "bot hesabı" oluşturacaksın (kod yazmadan, sadece
   tıklayarak)
2. Kick'ten bir "uygulama kimliği" alacaksın (yine sadece tıklayarak)
3. Bu dosyaları GitHub'a yükleyeceksin
4. Railway'e bağlayıp botu çalıştıracaksın
5. Discord'da birkaç komutla yayıncı ekleyeceksin

Hepsi tarayıcıdan yapılabiliyor, bilgisayar gerekmiyor.

---

## ADIM 1 — Discord Bot Oluştur

1. Tarayıcıdan **discord.com/developers/applications** adresine git
   (Discord hesabınla giriş yapmış olmalısın)
2. Sağ üstten **New Application** butonuna bas, bir isim yaz (örnek:
   "Yayın Bildirici"), **Create** de
3. Sol menüden **Bot** sekmesine gir
4. **Reset Token** butonuna bas, çıkan uzun kodu kopyala ve bir yere kaydet
   (not defteri gibi). Buna **DISCORD_BOT_TOKEN** diyeceğiz. Bu kodu kimseyle
   paylaşma — bota tam yetki verir.
5. Aynı sayfada aşağıda **Privileged Gateway Intents** başlığı var,
   hiçbirine dokunmana gerek yok
6. Sol menüden **OAuth2** → **URL Generator** sekmesine gir:
   - **SCOPES** altında `bot` ve `applications.commands` kutucuklarını işaretle
   - Altta çıkan **BOT PERMISSIONS** altında `Send Messages` ve
     `Embed Links` kutucuklarını işaretle
   - En altta oluşan uzun linki kopyala, tarayıcıda aç, botu eklemek
     istediğin Discord sunucusunu seç, **Authorize** de

✅ Bot artık sunucunda görünüyor olmalı (çevrimdışı görünse de sorun değil,
Railway'e bağlayınca aktif olacak).

---

## ADIM 2 — Kick Bilgilerini Al

1. Tarayıcıdan **kick.com/settings/developer** adresine git
2. Yeni bir uygulama oluştur (isim önemli değil, örnek: "Bildirim Botu")
3. Çıkan **Client ID** ve **Client Secret** değerlerini kopyalayıp kaydet

---

## ADIM 3 — Dosyaları GitHub'a Yükle

1. **github.com** adresine git, hesabın yoksa ücretsiz üye ol
2. Sağ üstten **+** işaretine bas → **New repository**
3. Bir isim ver (örnek: `kick-discord-bot`), **Public** ya da **Private**
   fark etmez, **Create repository** de
4. Açılan sayfada **uploading an existing file** yazan linke tıkla
   (ya da **Add file → Upload files**)
5. Bu klasördeki **tüm dosyaları** (bot.py, requirements.txt, Procfile,
   .env.example, README.md) sürükleyip bırak ya da seç, aşağıdan
   **Commit changes** de

---

## ADIM 4 — Railway'e Deploy Et

1. **railway.app** adresine git, **Login** → GitHub hesabınla giriş yap
2. **New Project** → **Deploy from GitHub repo** → az önce oluşturduğun
   `kick-discord-bot` reposunu seç
3. Railway otomatik olarak projeyi algılayıp kurmaya başlayacak
4. Sol menüden (ya da proje kutusuna tıklayınca çıkan) **Variables**
   sekmesine gir, **New Variable** ile şunları tek tek ekle:

   | İsim | Değer |
   |---|---|
   | `DISCORD_BOT_TOKEN` | Adım 1'de kaydettiğin token |
   | `KICK_CLIENT_ID` | Adım 2'de kaydettiğin Client ID |
   | `KICK_CLIENT_SECRET` | Adım 2'de kaydettiğin Client Secret |
   | `CHECK_INTERVAL_SECONDS` | `20` |

5. Değişkenleri kaydedince Railway botu otomatik olarak yeniden başlatır
6. **Deployments** sekmesinden logları izleyebilirsin — en altta
   `[BILGI] Giris yapildi: ...` yazısını görürsen bot çalışıyor demektir ✅

---

## ADIM 5 — Discord'da Kullan

Discord sunucunda, bildirimlerin gelmesini istediğin kanalda şunları yaz:

```
/kanalayarla kanal:#yayin-bildirimleri
```
→ Bildirimlerin hangi kanala düşeceğini belirler

```
/yayinciekle kullanici_adi:xqc
```
→ Takip listesine bir Kick yayıncısı ekler (kick.com/**xqc** adresindeki
kullanıcı adını yaz)

```
/liste
```
→ Takip edilen yayıncıları ve şu anki durumlarını gösterir

```
/yayincisil kullanici_adi:xqc
```
→ Listeden çıkarır

Bu kadar! Artık eklediğin yayıncılardan biri canlıya geçtiğinde, bot 20
saniye içinde fark edip o kanala otomatik mesaj atacak.

---

## Sık Sorulan Sorular

**"Deployments" sekmesinde kırmızı hata görüyorum, ne yapmalıyım?**
Muhtemelen `Variables` sekmesindeki bilgilerden biri eksik ya da yanlış
kopyalanmış. Loglardaki hata mesajını oku — genelde hangi bilgi eksikse onu
söyler (`DISCORD_BOT_TOKEN ayarlanmamis` gibi).

**Komutlar Discord'da görünmüyor?**
Botu ekledikten ve Railway'de ilk kez başarıyla çalıştıktan sonra
komutların Discord'da görünmesi birkaç dakika sürebilir. Sunucudan çıkıp
tekrar girmek genelde hızlandırır.

**20 saniye çok mu sık, sorun olur mu?**
Bot, kaç yayıncı eklersen ekle her kontrolde Kick'e tek bir istek atıyor
(hepsini birden soruyor), o yüzden 20 saniye normal kullanımda sorun
yaratmaz. Eğer ileride "rate limit" (çok istek) hatası görürsen bu değeri
Railway'deki `CHECK_INTERVAL_SECONDS` değişkenini 30-60 gibi bir değere
çıkarabilirsin.

**Bot bilgilerimi (token, secret) güvende mi?**
Bu bilgileri sadece Railway'in "Variables" kısmına giriyorsun, koda hiçbir
zaman yazmıyorsun. `.env` dosyanı ya da bu bilgileri GitHub'a **açık** olarak
(kod içine yazarak) asla yükleme.

---

## Komutlar Özeti

| Komut | Ne işe yarar | Kim kullanabilir |
|---|---|---|
| `/kanalayarla` | Bildirim kanalını seçer | Yöneticiler |
| `/yayinciekle` | Takip listesine yayıncı ekler | Yöneticiler |
| `/yayincisil` | Takip listesinden çıkarır | Yöneticiler |
| `/liste` | Takip edilenleri ve durumlarını gösterir | Herkes |

---

## Dosyalar Ne İşe Yarıyor?

- **bot.py** — Botun tüm kodu (komutlar + 20 saniyede bir Kick kontrolü)
- **requirements.txt** — Botun ihtiyaç duyduğu Python kütüphaneleri
- **Procfile** — Railway'e "bunu çalıştır" diyen dosya
- **.env.example** — Hangi bilgilerin gerektiğini gösteren örnek (Railway'de
  bunun yerine "Variables" sekmesini kullanıyorsun)
- **guilds.json** — Bot ilk çalıştığında kendisi oluşturur, hangi sunucuda
  hangi kanal/yayıncıların ayarlı olduğunu burada saklar
