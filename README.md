# Kick Canlı Yayın Bildirim Botu + Sunucu Loglama Paketi

Bu bot iki ana iş yapıyor:

1. **Kick takibi:** Takip listesine eklediğin yayıncılardan biri canlıya
   geçince, yayını bitirince, kategori değiştirince, (deneysel olarak) yeni
   klip paylaşınca ya da mesaj sabitleyince Discord'a otomatik bildirim
   atar. Canlı bildirimindeki önizleme görseli yayın sürdükçe kendini
   günceller. `/kanit` ile o an için Kick'in ürettiği resmi önizleme
   görselini istediğin an çekebilirsin.
2. **Sunucu loglama:** Ban/unban, zaman aşımı (timeout), sunucudan atma
   (kick), mesaj silme/düzenleme, üye katılma/ayrılma ve ses kanalı
   giriş/çıkışlarını tek bir log kanalına otomatik yazar. Ayrıca yeni
   üyelere hoş geldin mesajı ve otomatik rol atayabilir.

Veri basit bir JSON dosyasında tutulur. Railway'de bir **Volume** (kalıcı
disk) bağlarsan, bu dosya redeploy'larda silinmez (aşağıda ADIM 2).

---

## ÖNEMLİ: Discord Developer Portal'da 2 ayarı açman lazım

Bazı özelliklerin (timeout logu, katılma/ayrılma logu, mesaj logu) çalışması
için Discord'un normalde kapalı olan iki "Privileged Intent" ayarını açman
gerekiyor, yoksa bot hiç başlamaz.

1. **discord.com/developers/applications** → uygulamanı seç → **Bot** sekmesi
2. Aşağı in, **Privileged Gateway Intents** başlığını bul
3. **SERVER MEMBERS INTENT**'i aç
4. **MESSAGE CONTENT INTENT**'i aç
5. Sayfadan çıkmadan önce kaydedildiğinden emin ol

Bu olmadan bot Railway'de sürekli çöker.

---

## ADIM 1 — GitHub'daki Dosyaları Güncelle

Şu dosyaları güncel haliyle değiştir:
- `bot.py`
- `requirements.txt`
- `.env.example` (bilgi amaçlı, opsiyonel)

GitHub'da her dosyayı aç → ✏️ (Edit) → içeriğini yeni haliyle değiştir →
Commit. Railway otomatik yeniden deploy edecek.

---

## ADIM 2 — (Önerilir) Railway'de Volume Ekle — Liste Kaybolmasın

Volume eklemezsen bot yine çalışır ama her redeploy'da (kod güncellemesi,
Variables değişikliği vb.) takip listesi ve ayarlar sıfırlanır. Bunu
önlemek için:

1. Railway'de **Kick-bot** servisine tıkla
2. Üstteki **Settings** sekmesine gir
3. Aşağı in, **Volumes** bölümünü bul → **+ New Volume**
4. **Mount path** olarak `/data` yaz, kaydet
5. **Variables** sekmesine geç, yeni bir değişken ekle:

| İsim | Değer |
|---|---|
| `DATA_DIR` | `/data` |

Bu kadar. Bot artık `guilds.json` dosyasını `/data` klasöründe (kalıcı
diskte) tutacak, redeploy'lar onu silmeyecek.

**Volume eklemek istemiyorsan** bu adımı atla, bot yine çalışır — sadece
her redeploy'da listeyi tekrar oluşturman gerekir (`/topluekle` ile hızlıca
geri ekleyebilirsin).

---

## Yeni ve Eski Tüm Komutlar

### Kick takibi
| Komut | Ne yapar |
|---|---|
| `/kanalayarla` | Yayın bildirimlerinin gideceği VARSAYILAN kanalı seçer |
| `/bildirimkanaliayarla` | Canlı/kategori/klip/sabit mesaj bildirimlerini FARKLI kanallara yönlendirir (opsiyonel) |
| `/yayinciekle` / `/izlemebaslat` | Takip listesine yayıncı ekler (ikisi de aynı işi yapar) |
| `/topluekle` | Birden fazla yayıncıyı TEK SEFERDE ekler (virgülle ya da alt alta) |
| `/yayincisil` / `/izlemedurdur` | Listeden çıkarır |
| `/liste` | Takip edilenleri, kanalları ve durumlarını gösterir |
| `/kanit` | Yayıncının şu anki resmi Kick önizleme görselini gönderir |

**Otomatik bildirimler (ekstra komut gerekmez):**
- Canlıya geçince (thumbnail dahil, yayın sürdükçe **otomatik güncellenir**)
- Yayın bitince
- Kategori değişince
- 🧪 Yeni klip paylaşılınca *(deneysel, resmi API değil)*
- 🧪 Yeni bir mesaj sabitlenince *(deneysel, resmi API değil)*

**`/bildirimkanaliayarla` nasıl çalışır:** Varsayılan olarak her şey
`/kanalayarla` ile ayarladığın tek kanala düşer. İstersen örneğin klip
bildirimlerini ayrı bir `#klipler` kanalına, kategori değişikliklerini
`#kategori-log` kanalına yönlendirebilirsin:
```
/bildirimkanaliayarla tur:Yeni klip kanal:#klipler
/bildirimkanaliayarla tur:Kategori degisikligi kanal:#kategori-log
```
Ayarlamadığın türler otomatik olarak varsayılan kanala düşmeye devam eder.

**`/topluekle` örneği:**
```
/topluekle kullanici_adlari:xqc, trainwreckstv, ninja
```
ya da alt alta:
```
xqc
trainwreckstv
ninja
```

### Sunucu loglama ve yönetim
| Komut | Ne yapar |
|---|---|
| `/loglamakanali` | Ban/timeout/kick/mesaj/katılma-ayrılma/ses loglarının gideceği kanalı seçer |
| `/hosgeldinkanali` | Yeni üye katılınca hoş geldin mesajının gideceği kanalı seçer |
| `/otorolayarla` | Yeni üyelere otomatik verilecek rolü seçer |
| `/sunucubilgi` | Sunucu ve bot ayarlarının özetini gösterir |

**Otomatik loglar (tek `/loglamakanali` yeterli, hepsi aynı kanala düşer):**
- Ban / ban kaldırma (kim, neden)
- Zaman aşımı (timeout) verilme/kaldırılma (kim, ne zamana kadar)
- Sunucudan atılma - kick (kim attı, neden) — gönüllü ayrılmadan ayrı gösterilir
- Mesaj silme (kim, ne yazmış, hangi kanalda)
- Mesaj düzenleme (eski hali / yeni hali)
- Üyenin sunucuya katılması / ayrılması
- Ses kanalına giriş / çıkış / kanal değiştirme

### Deneysel
| Komut | Ne yapar |
|---|---|
| `/kesiftest` | Moderatörü olmadığın bir Kick kanalında ban/timeout sinyali var mı diye test eder (şu ana kadarki testlerde sonuç hep olumsuz çıktı — bu resmi olarak desteklenmiyor) |

**Klip ve sabit mesaj bildirimleri hakkında dürüst bir not:** Bunlar
Kick'in **resmi olarak belgelemediği** uçlara dayanıyor. Çalışabilir de,
hiç çalışmayabilir de, ya da bir gün Kick tarafında sessizce kapatılabilir.
Loglarda sürekli `[UYARI] Klip bilgisi alinamadi` ya da `[UYARI] Sabit
mesaj bilgisi alinamadi` görüyorsan, bu özellik senin durumunda çalışmıyor
demektir — botun geri kalanını etkilemez, sadece o iki bildirim gelmez.

---

## Sık Sorulan Sorular

**Bot açılmıyor / "PrivilegedIntentsRequired" hatası veriyor:**
Yukarıdaki "Discord Developer Portal'da 2 ayarı açman lazım" adımını
atlamışsındır. Bot sekmesine gidip Server Members Intent ve Message Content
Intent'i aç.

**Liste hâlâ her redeploy'da kayboluyor:**
Railway'de Volume eklemeyi (ADIM 2) atladın demektir, ya da `DATA_DIR`
değişkenini eklemeyi unuttun. İkisini de kontrol et.

**Mesaj silme/düzenleme logu bazı mesajları göstermiyor:**
Bot çalışmaya başlamadan önce gönderilmiş mesajlar Discord'un cache'inde
olmadığı için loglanamaz — bu Discord'un kendi kısıtlaması.

**Thumbnail güncellenmiyor:**
Kick'in önizleme görseli kendisi de birkaç dakikada bir değişiyor; bot her
kontrol döngüsünde (20 sn) en güncel halini çekmeye çalışıyor ama Kick
tarafında görsel değişmediyse gösterilecek yeni bir şey yok demektir.

---

## Dosyalar

```
bot.py               -> Botun tüm kodu
requirements.txt     -> Python bağımlılıkları
Procfile              -> Railway'e "bunu çalıştır" diyen dosya
.env.example           -> Hangi ortam değişkenlerinin gerektiğini gösteren örnek
guilds.json            -> Bot ilk çalıştığında kendisi oluşturur (Volume varsa /data içinde)
```
