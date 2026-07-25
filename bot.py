#!/usr/bin/env python3
"""
Kick Canli Yayin Bildirim Botu + Sunucu Loglama Paketi (JSON dosya surumu)
---------------------------------------------------------------------------
Ozellikler:
  - /yayinciekle veya /izlemebaslat: Kick yayincisi takip listesine ekler
  - /topluekle: Birden fazla Kick yayincisini TEK KOMUTLA ekler
  - /yayincisil veya /izlemedurdur: listeden cikarir
  - Canli yayina gecince, yayin bitince ve kategori degisince otomatik bildirim
  - /kanit: yayincinin su anki resmi Kick onizleme goreselini gonderir
  - /loglamakanali: ban/unban/timeout/kick/mesaj sil-duzenle/katilma-ayrilma/
    ses kanali loglarinin gonderilecegi kanali secer
  - /hosgeldinkanali + /otorolayarla: yeni uye karsilama + otomatik rol
  - /sunucubilgi: sunucu ve bot ayarlari ozeti
  - /kesiftest: (deneysel) Kick chat'inde ban/timeout sinyali arar

Veri basit bir JSON dosyasinda (guilds.json) saklanir. Railway'de veri
kaybini onlemek icin bir "Volume" (kalici disk) baglayip DATA_DIR ortam
degiskenini o volume'un mount yoluna ayarlayabilirsin (bkz. README).

Gerekli ortam degiskenleri:
    DISCORD_BOT_TOKEN
    KICK_CLIENT_ID
    KICK_CLIENT_SECRET
    CHECK_INTERVAL_SECONDS   (opsiyonel, varsayilan 20)
    DATA_DIR                 (opsiyonel - Railway Volume mount yolu, ör: /data)

Calistirmak icin:
    pip install -r requirements.txt
    python bot.py

NOT: Bu botun calismasi icin Discord Developer Portal'da su iki
"Privileged Gateway Intent" AYARININ ACIK olmasi gerekiyor:
    - SERVER MEMBERS INTENT
    - MESSAGE CONTENT INTENT
(Bot sekmesi -> Privileged Gateway Intents)
"""

import asyncio
import io
import json
import os
import time
from datetime import datetime, timezone

import discord
import requests
import websockets
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID")
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "20"))

# Railway'de bir Volume baglarsan, onu mount ettigin yolu buraya
# DATA_DIR olarak ver (ör: /data). Vermezsen dosya botun kendi
# klasorunde tutulur (redeploy'da sifirlanabilir).
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(DATA_DIR, "guilds.json")

KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_CHANNELS_URL = "https://api.kick.com/public/v1/channels"
KICK_UNOFFICIAL_CHANNEL_URL = "https://kick.com/api/v2/channels/{slug}"

KICK_PUSHER_URL = (
    "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679"
    "?protocol=7&client=js&version=8.4.0-rc2&flash=false"
)

KICK_KNOWN_CHAT_EVENTS = {
    "App\\Events\\ChatMessageEvent",
    "App\\Events\\ChatMessageSentEvent",
    "App\\Events\\FollowEvent",
    "App\\Events\\SubscriptionEvent",
    "App\\Events\\GiftedSubscriptionsEvent",
    "pusher:connection_established",
    "pusher_internal:subscription_succeeded",
    "pusher:pong",
    "pusher:ping",
}

# ------------------------------------------------------------------
# Basit JSON dosya deposu
# ------------------------------------------------------------------
DEFAULT_GUILD_ENTRY = {
    "channel_id": None,
    "log_channel_id": None,
    "welcome_channel_id": None,
    "auto_role_id": None,
    "streamers": {},
    # Bildirim turune gore ayri kanal yonlendirmesi. Bos birakilanlar
    # otomatik olarak "channel_id"yi kullanir (geriye donuk uyumluluk).
    "notify_channels": {
        "canli": None,
        "kategori": None,
        "klip": None,
        "sabitmesaj": None,
    },
}


def get_notify_channel_id(entry, tur):
    """Bildirim turune ozel kanal ayarlanmissa onu, yoksa genel
    kanal_id'yi doner."""
    notify_channels = entry.get("notify_channels") or {}
    return notify_channels.get(tur) or entry.get("channel_id")


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_guild_entry(data, guild_id: str):
    if guild_id not in data:
        data[guild_id] = json.loads(json.dumps(DEFAULT_GUILD_ENTRY))  # derin kopya
        data[guild_id]["streamers"] = {}
    else:
        for key, default_value in DEFAULT_GUILD_ENTRY.items():
            if key not in data[guild_id]:
                data[guild_id][key] = json.loads(json.dumps(default_value))
        # notify_channels alt alanlarini da tamamla (eski kayitlarda olmayabilir)
        for sub_key in DEFAULT_GUILD_ENTRY["notify_channels"]:
            data[guild_id]["notify_channels"].setdefault(sub_key, None)
    return data[guild_id]


# ------------------------------------------------------------------
# Kick API
# ------------------------------------------------------------------
_token_cache = {"access_token": None, "obtained_at": 0}


def get_app_access_token():
    now = time.time()
    if _token_cache["access_token"] and (now - _token_cache["obtained_at"]) < 3500:
        return _token_cache["access_token"]

    resp = requests.post(
        KICK_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": KICK_CLIENT_ID,
            "client_secret": KICK_CLIENT_SECRET,
        },
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    _token_cache["access_token"] = token
    _token_cache["obtained_at"] = now
    return token


def get_channels_status(slugs):
    if not slugs:
        return {}
    token = get_app_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    params = [("slug", s) for s in slugs]
    resp = requests.get(KICK_CHANNELS_URL, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json().get("data", [])

    result = {}
    for channel in data:
        slug = channel.get("slug")
        stream = channel.get("stream") or {}
        category = channel.get("category") or {}
        result[slug] = {
            "is_live": bool(stream.get("is_live")),
            "title": stream.get("stream_title") or channel.get("stream_title") or "",
            "thumbnail": stream.get("thumbnail") or "",
            "viewers": stream.get("viewer_count"),
            "category": category.get("name"),
            "url": f"https://kick.com/{slug}",
        }
    return result


# ------------------------------------------------------------------
# /kesiftest icin yardimci fonksiyonlar (DENEYSEL - resmi API degil)
# ------------------------------------------------------------------
async def resolve_chatroom_id(slug: str):
    def _fetch():
        resp = requests.get(
            KICK_UNOFFICIAL_CHANNEL_URL.format(slug=slug),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                "Accept": "application/json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch)
    chatroom = data.get("chatroom") or {}
    return chatroom.get("id")


# ------------------------------------------------------------------
# Klip ve sabitlenmis mesaj kontrolu (DENEYSEL - resmi/dokumante
# edilmemis endpoint'ler kullaniliyor, calismama ihtimali var)
# ------------------------------------------------------------------
KICK_UNOFFICIAL_CLIPS_URL = "https://api.kick.com/private/v1/channels/{slug}/clips"
KICK_UNOFFICIAL_PINNED_MESSAGE_URL = "https://kick.com/api/internal/v1/channels/{slug}/chatroom/pinned-message"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


async def get_latest_clip(slug: str):
    """En son klibi doner (bulamazsa/erisemezse None). Deneysel."""

    def _fetch():
        resp = requests.get(
            KICK_UNOFFICIAL_CLIPS_URL.format(slug=slug),
            headers=_BROWSER_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch)

    clips = data.get("clips") if isinstance(data, dict) else data
    if not clips:
        return None

    clip = clips[0]
    return {
        "id": clip.get("id"),
        "title": clip.get("title") or "(basliksiz klip)",
        "url": clip.get("clip_url") or clip.get("video_url") or f"https://kick.com/{slug}/clips",
        "thumbnail": clip.get("thumbnail_url") or clip.get("thumbnail"),
        "creator": (clip.get("creator") or {}).get("username") if isinstance(clip.get("creator"), dict) else None,
    }


async def get_pinned_message(slug: str):
    """Su anki sabitlenmis mesaji doner (yoksa/erisemezse None). Deneysel."""

    def _fetch():
        resp = requests.get(
            KICK_UNOFFICIAL_PINNED_MESSAGE_URL.format(slug=slug),
            headers=_BROWSER_HEADERS,
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch)

    if not data:
        return None

    message_block = data.get("message") if isinstance(data, dict) else None
    if not message_block:
        return None

    return {
        "id": message_block.get("id"),
        "content": message_block.get("content") or "(bos mesaj)",
        "sender": (message_block.get("sender") or {}).get("username"),
    }



async def listen_for_unknown_events(chatroom_id: int, duration_seconds: int, max_events: int = 15):
    channel_name = f"chatrooms.{chatroom_id}.v2"
    found = []

    try:
        async with websockets.connect(KICK_PUSHER_URL, open_timeout=15) as ws:
            await ws.send(json.dumps({
                "event": "pusher:subscribe",
                "data": {"channel": channel_name},
            }))

            end_time = time.time() + duration_seconds
            while time.time() < end_time and len(found) < max_events:
                remaining = end_time - time.time()
                if remaining <= 0:
                    break
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break

                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    continue

                event_name = payload.get("event", "")
                if event_name not in KICK_KNOWN_CHAT_EVENTS:
                    found.append(payload)

    except Exception as e:
        return found, str(e)

    return found, None


# ------------------------------------------------------------------
# Discord bot kurulumu
# ------------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True          # katilma/ayrilma/timeout olaylari icin
intents.message_content = True  # silinen/duzenlenen mesaj icerigini gorebilmek icin

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    print(f"[BILGI] Giris yapildi: {client.user}")
    print(f"[BILGI] Veri dosyasi: {DATA_FILE}")

    if not check_streams.is_running():
        check_streams.start()

    try:
        synced = await tree.sync()
        print(f"[BILGI] {len(synced)} komut Discord'a senkronize edildi: "
              f"{', '.join(sorted(c.name for c in synced))}")
    except Exception as e:
        print(f"[HATA] Komutlar senkronize edilemedi: {e}")


# ------------------------------------------------------------------
# Ortak yardimcilar
# ------------------------------------------------------------------
async def find_audit_log_entry(guild: discord.Guild, action, target_id: int):
    try:
        async for entry in guild.audit_logs(action=action, limit=5):
            if entry.target and entry.target.id == target_id:
                return entry
    except discord.Forbidden:
        print("[UYARI] Audit log okuma yetkisi yok. Bota 'Denetim Kaydini Goruntule' yetkisi ver.")
    except discord.HTTPException as e:
        print(f"[HATA] Audit log okunamadi: {e}")
    return None


async def send_log_embed(entry, embed: discord.Embed):
    log_channel_id = entry.get("log_channel_id")
    if not log_channel_id:
        return
    channel = client.get_channel(log_channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.DiscordException as e:
        print(f"[HATA] Log mesaji gonderilemedi: {e}")


# ------------------------------------------------------------------
# Kick takip komutlari
# ------------------------------------------------------------------
async def _add_streamer(interaction: discord.Interaction, kullanici_adi: str):
    slug = kullanici_adi.strip().lower()
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))

    if slug in entry["streamers"]:
        await interaction.response.send_message(f"**{slug}** zaten listede.", ephemeral=True)
        return

    entry["streamers"][slug] = {"is_live": False, "category": None}
    save_data(data)
    await interaction.response.send_message(f"**{slug}** takip listesine eklendi.", ephemeral=True)


async def _remove_streamer(interaction: discord.Interaction, kullanici_adi: str):
    slug = kullanici_adi.strip().lower()
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))

    if slug not in entry["streamers"]:
        await interaction.response.send_message(f"**{slug}** listede bulunamadi.", ephemeral=True)
        return

    del entry["streamers"][slug]
    save_data(data)
    await interaction.response.send_message(f"**{slug}** listeden cikarildi.", ephemeral=True)


@tree.command(name="kanalayarla", description="Kick canli yayin bildirimlerinin gonderilecegi kanali secer")
@app_commands.describe(kanal="Bildirimlerin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def kanalayarla(interaction: discord.Interaction, kanal: discord.TextChannel):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    entry["channel_id"] = kanal.id
    save_data(data)
    await interaction.response.send_message(f"Bildirim kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


@tree.command(
    name="bildirimkanaliayarla",
    description="Canli/kategori/klip/sabit mesaj bildirimlerini FARKLI kanallara yonlendirir",
)
@app_commands.describe(
    tur="Hangi bildirim turu icin ayarliyorsun",
    kanal="Bu turun gonderilecegi metin kanali",
)
@app_commands.choices(
    tur=[
        app_commands.Choice(name="Canli yayina gecti", value="canli"),
        app_commands.Choice(name="Kategori degisikligi", value="kategori"),
        app_commands.Choice(name="Yeni klip", value="klip"),
        app_commands.Choice(name="Sabitlenen mesaj", value="sabitmesaj"),
    ]
)
@app_commands.checks.has_permissions(manage_guild=True)
async def bildirimkanaliayarla(interaction: discord.Interaction, tur: app_commands.Choice[str], kanal: discord.TextChannel):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    entry["notify_channels"][tur.value] = kanal.id
    save_data(data)
    await interaction.response.send_message(
        f"**{tur.name}** bildirimleri artik {kanal.mention} kanalina gidecek.",
        ephemeral=True,
    )


@tree.command(name="loglamakanali", description="Sunucu loglarinin (ban/timeout/kick/mesaj/katilma/ses) gonderilecegi kanali secer")
@app_commands.describe(kanal="Loglarin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def loglamakanali(interaction: discord.Interaction, kanal: discord.TextChannel):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    entry["log_channel_id"] = kanal.id
    save_data(data)
    await interaction.response.send_message(f"Log kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


@tree.command(name="hosgeldinkanali", description="Yeni uye katilinca hos geldin mesajinin gonderilecegi kanali secer")
@app_commands.describe(kanal="Hos geldin mesajlarinin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def hosgeldinkanali(interaction: discord.Interaction, kanal: discord.TextChannel):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    entry["welcome_channel_id"] = kanal.id
    save_data(data)
    await interaction.response.send_message(f"Hos geldin kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


@tree.command(name="otorolayarla", description="Yeni uyelere otomatik verilecek rolu secer")
@app_commands.describe(rol="Yeni katilan uyelere otomatik verilecek rol")
@app_commands.checks.has_permissions(manage_guild=True)
async def otorolayarla(interaction: discord.Interaction, rol: discord.Role):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    entry["auto_role_id"] = rol.id
    save_data(data)
    await interaction.response.send_message(f"Yeni uyelere otomatik olarak {rol.mention} rolu verilecek.", ephemeral=True)


@tree.command(name="yayinciekle", description="Takip listesine bir Kick yayincisi ekler")
@app_commands.describe(kullanici_adi="Kick kullanici adi (kick.com/KULLANICIADI)")
@app_commands.checks.has_permissions(manage_guild=True)
async def yayinciekle(interaction: discord.Interaction, kullanici_adi: str):
    await _add_streamer(interaction, kullanici_adi)


@tree.command(name="izlemebaslat", description="Bir Kick kanalini izlemeye baslar (yayinciekle ile ayni)")
@app_commands.describe(kullanici_adi="Kick kullanici adi (kick.com/KULLANICIADI)")
@app_commands.checks.has_permissions(manage_guild=True)
async def izlemebaslat(interaction: discord.Interaction, kullanici_adi: str):
    await _add_streamer(interaction, kullanici_adi)


@tree.command(name="topluekle", description="Birden fazla Kick yayincisini TEK SEFERDE takip listesine ekler")
@app_commands.describe(kullanici_adlari="Kullanici adlarini virgul VEYA alt alta (yeni satir) yaz, ornek: xqc, trainwreckstv, ninja")
@app_commands.checks.has_permissions(manage_guild=True)
async def topluekle(interaction: discord.Interaction, kullanici_adlari: str):
    raw_list = kullanici_adlari.replace(",", "\n").splitlines()
    slugs = [s.strip().lower() for s in raw_list if s.strip()]

    if not slugs:
        await interaction.response.send_message("Gecerli bir kullanici adi bulamadim.", ephemeral=True)
        return

    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))

    eklenen = []
    zaten_vardi = []

    for slug in slugs:
        if slug in entry["streamers"]:
            zaten_vardi.append(slug)
        else:
            entry["streamers"][slug] = {"is_live": False, "category": None}
            eklenen.append(slug)

    save_data(data)

    parts = []
    if eklenen:
        parts.append(f"✅ **{len(eklenen)} yayinci eklendi:** {', '.join(eklenen)}")
    if zaten_vardi:
        parts.append(f"⏭️ **{len(zaten_vardi)} tanesi zaten listedeydi:** {', '.join(zaten_vardi)}")

    await interaction.response.send_message("\n".join(parts), ephemeral=True)


@tree.command(name="yayincisil", description="Takip listesinden bir Kick yayincisini cikarir")
@app_commands.describe(kullanici_adi="Kick kullanici adi")
@app_commands.checks.has_permissions(manage_guild=True)
async def yayincisil(interaction: discord.Interaction, kullanici_adi: str):
    await _remove_streamer(interaction, kullanici_adi)


@tree.command(name="izlemedurdur", description="Bir Kick kanalini izlemeyi durdurur (yayincisil ile ayni)")
@app_commands.describe(kullanici_adi="Kick kullanici adi")
@app_commands.checks.has_permissions(manage_guild=True)
async def izlemedurdur(interaction: discord.Interaction, kullanici_adi: str):
    await _remove_streamer(interaction, kullanici_adi)


@tree.command(name="liste", description="Takip edilen Kick yayincilarini listeler")
async def liste(interaction: discord.Interaction):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    streamers = entry["streamers"]

    if not streamers:
        await interaction.response.send_message("Henuz takip edilen yayinci yok. `/yayinciekle` ya da `/topluekle` ile ekleyebilirsin.", ephemeral=True)
        return

    lines = []
    for slug, info in streamers.items():
        durum = "CANLI" if info.get("is_live") else "cevrimdisi"
        category = info.get("category")
        if info.get("is_live") and category:
            lines.append(f"- **{slug}** — {durum} ({category})")
        else:
            lines.append(f"- **{slug}** — {durum}")

    def ch_text(cid, cmd):
        return f"<#{cid}>" if cid else f"ayarlanmadi (`{cmd}` kullan)"

    nc = entry.get("notify_channels") or {}
    ozel_yonlendirmeler = []
    for tur, isim in [("kategori", "Kategori"), ("klip", "Klip"), ("sabitmesaj", "Sabit mesaj")]:
        if nc.get(tur):
            ozel_yonlendirmeler.append(f"{isim}: <#{nc[tur]}>")

    ozel_text = ("\n" + "\n".join(ozel_yonlendirmeler)) if ozel_yonlendirmeler else ""

    message = (
        f"Bildirim kanali (varsayilan): {ch_text(entry.get('channel_id'), '/kanalayarla')}\n"
        f"Log kanali: {ch_text(entry.get('log_channel_id'), '/loglamakanali')}"
        f"{ozel_text}\n\n"
        + "\n".join(lines)
    )

    # Discord mesaj limiti 2000 karakter - liste cok uzunsa dosya olarak gonder
    if len(message) > 1900:
        file_obj = discord.File(io.BytesIO(message.encode("utf-8")), filename="yayinci_listesi.txt")
        await interaction.response.send_message("Liste uzun oldugu icin dosya olarak gonderiyorum:", file=file_obj, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@tree.command(name="kanit", description="Kick yayincisinin su anki resmi Kick onizleme goreselini gonderir")
@app_commands.describe(kullanici_adi="Kick kullanici adi")
async def kanit(interaction: discord.Interaction, kullanici_adi: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    slug = kullanici_adi.strip().lower()

    try:
        statuses = get_channels_status([slug])
    except Exception as e:
        await interaction.followup.send(f"Kick bilgisi alinamadi: {e}", ephemeral=True)
        return

    info = statuses.get(slug)
    if not info:
        await interaction.followup.send("Kullanici bulunamadi. Kullanici adini kontrol et.", ephemeral=True)
        return
    if not info["is_live"]:
        await interaction.followup.send(f"**{slug}** su an canli degil.", ephemeral=True)
        return
    if not info.get("thumbnail"):
        await interaction.followup.send(
            f"**{slug}** canli ama Kick henuz bir onizleme goreseli uretmemis, birazdan tekrar dene.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title=f"{slug} - su anki onizleme goruntusu",
        url=info["url"],
        description=(
            "Bu, Kick'in periyodik olarak urettigi resmi onizleme goreseli "
            "(canli video akisindan saniyelik kare yakalama degildir)."
        ),
        color=0x53FC18,
        timestamp=datetime.now(timezone.utc),
    )
    if info.get("category"):
        embed.add_field(name="Kategori", value=info["category"], inline=True)
    if info.get("viewers") is not None:
        embed.add_field(name="Izleyici", value=str(info["viewers"]), inline=True)
    embed.set_image(url=info["thumbnail"])

    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="sunucubilgi", description="Bu sunucu hakkinda genel bilgi ve bot ayarlarinin ozetini gosterir")
async def sunucubilgi(interaction: discord.Interaction):
    guild = interaction.guild
    data = load_data()
    entry = get_guild_entry(data, str(guild.id))

    embed = discord.Embed(title=guild.name, color=0x5865F2, timestamp=datetime.now(timezone.utc))
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="Uye sayisi", value=str(guild.member_count), inline=True)
    embed.add_field(name="Olusturulma", value=guild.created_at.strftime("%d.%m.%Y"), inline=True)
    embed.add_field(name="Takip edilen yayinci", value=str(len(entry["streamers"])), inline=True)

    def ch_text(cid):
        return f"<#{cid}>" if cid else "ayarlanmadi"

    embed.add_field(name="Yayin bildirim kanali", value=ch_text(entry.get("channel_id")), inline=False)
    embed.add_field(name="Log kanali", value=ch_text(entry.get("log_channel_id")), inline=False)
    embed.add_field(name="Hos geldin kanali", value=ch_text(entry.get("welcome_channel_id")), inline=False)

    auto_role_id = entry.get("auto_role_id")
    embed.add_field(
        name="Otomatik rol",
        value=f"<@&{auto_role_id}>" if auto_role_id else "ayarlanmadi",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(
    name="kesiftest",
    description="(DENEYSEL) Bir Kick kanalinin chat'inde ban/timeout sinyali yayinlanip yayinlanmadigini test eder",
)
@app_commands.describe(
    kullanici_adi="Test edilecek Kick kullanici adi (moderatoru olman gerekmiyor)",
    sure_saniye="Kac saniye dinlensin (varsayilan 120, en fazla 240)",
    chatroom_id="Otomatik bulma basarisiz olursa elle girebilecegin chatroom ID (opsiyonel)",
)
async def kesiftest(
    interaction: discord.Interaction,
    kullanici_adi: str,
    sure_saniye: int = 120,
    chatroom_id: str = None,
):
    await interaction.response.defer(ephemeral=True, thinking=True)

    slug = kullanici_adi.strip().lower()
    duration = max(30, min(sure_saniye, 240))

    resolved_id = chatroom_id
    if not resolved_id:
        try:
            resolved_id = await resolve_chatroom_id(slug)
        except Exception as e:
            await interaction.followup.send(
                f"Kanal bilgisi otomatik alinamadi ({e}). Kick'in koruma sistemi "
                f"engellemis olabilir. Chatroom ID'yi tarayicidan bulup "
                f"`chatroom_id` parametresiyle tekrar dene:\n"
                f"1) `kick.com/api/v2/channels/{slug}` adresini tarayicidan ac\n"
                f'2) Icinde `"chatroom":{{"id": SAYI` seklinde bir alan ara\n'
                f"3) O sayiyi `/kesiftest kullanici_adi:{slug} chatroom_id:SAYI` "
                f"seklinde tekrar gonder",
                ephemeral=True,
            )
            return

    if not resolved_id:
        await interaction.followup.send(
            "Chatroom ID bulunamadi. Kullanici adini kontrol et ya da "
            "`chatroom_id` parametresiyle elle gir.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"**{slug}** kanalinin chat'i **{duration} saniye** boyunca dinleniyor "
        f"(chatroom_id: `{resolved_id}`). Sonuc birazdan burada.",
        ephemeral=True,
    )

    found_events, error = await listen_for_unknown_events(int(resolved_id), duration)

    if error:
        await interaction.followup.send(f"Baglanti hatasi olustu: {error}", ephemeral=True)
        return

    if not found_events:
        await interaction.followup.send(
            "**Test bitti.** Bilinmeyen (potansiyel ban/timeout) bir event yakalanmadi.",
            ephemeral=True,
        )
        return

    report = json.dumps(found_events, indent=2, ensure_ascii=False)
    file_obj = discord.File(io.BytesIO(report.encode("utf-8")), filename=f"{slug}_kesif_sonuclari.json")

    await interaction.followup.send(
        f"**{len(found_events)} bilinmeyen event yakalandi!** Detaylar ekli dosyada.",
        file=file_obj,
        ephemeral=True,
    )


# ------------------------------------------------------------------
# Ban / unban loglama
# ------------------------------------------------------------------
@client.event
async def on_member_ban(guild: discord.Guild, user):
    data = load_data()
    entry = get_guild_entry(data, str(guild.id))
    save_data(data)

    audit_entry = await find_audit_log_entry(guild, discord.AuditLogAction.ban, user.id)
    moderator = audit_entry.user if audit_entry else None
    reason = audit_entry.reason if audit_entry else None

    embed = discord.Embed(
        title="Uye banlandi",
        description=f"{user} (`{user.id}`)",
        color=0xFF5C5C,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Banlayan", value=str(moderator) if moderator else "Bilinmiyor", inline=True)
    embed.add_field(name="Sebep", value=reason or "Belirtilmemis", inline=False)

    await send_log_embed(entry, embed)


@client.event
async def on_member_unban(guild: discord.Guild, user):
    data = load_data()
    entry = get_guild_entry(data, str(guild.id))
    save_data(data)

    audit_entry = await find_audit_log_entry(guild, discord.AuditLogAction.unban, user.id)
    moderator = audit_entry.user if audit_entry else None

    embed = discord.Embed(
        title="Uyenin banı kaldirildi",
        description=f"{user} (`{user.id}`)",
        color=0x53FC18,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Kaldiran", value=str(moderator) if moderator else "Bilinmiyor", inline=True)

    await send_log_embed(entry, embed)


# ------------------------------------------------------------------
# Timeout loglama (Discord'un kendi zaman asimi ozelligi)
# ------------------------------------------------------------------
@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.timed_out_until == after.timed_out_until:
        return

    data = load_data()
    entry = get_guild_entry(data, str(after.guild.id))
    save_data(data)

    now = datetime.now(timezone.utc)
    is_new_timeout = after.timed_out_until is not None and after.timed_out_until > now

    audit_entry = await find_audit_log_entry(after.guild, discord.AuditLogAction.member_update, after.id)
    moderator = audit_entry.user if audit_entry else None

    if is_new_timeout:
        embed = discord.Embed(
            title="Uyeye zaman asimi (timeout) verildi",
            description=f"{after} (`{after.id}`)",
            color=0xFFB454,
            timestamp=now,
        )
        embed.set_thumbnail(url=after.display_avatar.url)
        embed.add_field(name="Bitis", value=discord.utils.format_dt(after.timed_out_until, style="R"), inline=True)
        embed.add_field(name="Veren", value=str(moderator) if moderator else "Bilinmiyor", inline=True)
    else:
        embed = discord.Embed(
            title="Uyenin zaman asimi kaldirildi",
            description=f"{after} (`{after.id}`)",
            color=0x53FC18,
            timestamp=now,
        )
        embed.set_thumbnail(url=after.display_avatar.url)
        embed.add_field(name="Kaldiran", value=str(moderator) if moderator else "Bilinmiyor / suresi doldu", inline=True)

    await send_log_embed(entry, embed)


# ------------------------------------------------------------------
# Katilma / ayrilma / kick loglama + hos geldin + otomatik rol
# ------------------------------------------------------------------
@client.event
async def on_member_join(member: discord.Member):
    data = load_data()
    entry = get_guild_entry(data, str(member.guild.id))
    save_data(data)

    welcome_channel_id = entry.get("welcome_channel_id")
    if welcome_channel_id:
        channel = client.get_channel(welcome_channel_id)
        if channel is not None:
            embed = discord.Embed(
                description=f"{member.mention} sunucuya katildi! Aramiza hos geldin 🎉",
                color=0x53FC18,
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"Uye sayisi: {member.guild.member_count}")
            try:
                await channel.send(embed=embed)
            except discord.DiscordException as e:
                print(f"[HATA] Hos geldin mesaji gonderilemedi: {e}")

    auto_role_id = entry.get("auto_role_id")
    if auto_role_id:
        role = member.guild.get_role(auto_role_id)
        if role is not None:
            try:
                await member.add_roles(role, reason="Otomatik rol atama")
            except discord.DiscordException as e:
                print(f"[HATA] Otomatik rol verilemedi: {e}")

    log_embed = discord.Embed(
        description=f"📥 {member} (`{member.id}`) sunucuya katildi.",
        color=0x53FC18,
        timestamp=datetime.now(timezone.utc),
    )
    log_embed.set_thumbnail(url=member.display_avatar.url)
    await send_log_embed(entry, log_embed)


@client.event
async def on_member_remove(member: discord.Member):
    data = load_data()
    entry = get_guild_entry(data, str(member.guild.id))
    save_data(data)

    audit_entry = await find_audit_log_entry(member.guild, discord.AuditLogAction.kick, member.id)
    now = datetime.now(timezone.utc)

    is_recent_kick = (
        audit_entry is not None
        and (now - audit_entry.created_at).total_seconds() < 10
    )

    if is_recent_kick:
        embed = discord.Embed(
            title="Uye sunucudan atildi (kick)",
            description=f"{member} (`{member.id}`)",
            color=0xFF9F43,
            timestamp=now,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Atan", value=str(audit_entry.user), inline=True)
        embed.add_field(name="Sebep", value=audit_entry.reason or "Belirtilmemis", inline=False)
    else:
        embed = discord.Embed(
            description=f"📤 {member} (`{member.id}`) sunucudan ayrildi.",
            color=0x6B726C,
            timestamp=now,
        )
        embed.set_thumbnail(url=member.display_avatar.url)

    await send_log_embed(entry, embed)


# ------------------------------------------------------------------
# Mesaj silme / duzenleme loglama
# ------------------------------------------------------------------
@client.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    data = load_data()
    entry = get_guild_entry(data, str(message.guild.id))
    save_data(data)

    log_channel_id = entry.get("log_channel_id")
    if not log_channel_id or log_channel_id == message.channel.id:
        return

    content = message.content or "*(metin yok - resim/dosya olabilir)*"
    if len(content) > 1000:
        content = content[:1000] + "..."

    embed = discord.Embed(
        title="Mesaj silindi",
        description=f"**Kanal:** {message.channel.mention}\n**Yazan:** {message.author}",
        color=0xFF5C5C,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Icerik", value=content, inline=False)

    await send_log_embed(entry, embed)


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return

    data = load_data()
    entry = get_guild_entry(data, str(before.guild.id))
    save_data(data)

    log_channel_id = entry.get("log_channel_id")
    if not log_channel_id or log_channel_id == before.channel.id:
        return

    old_content = (before.content or "*(bos)*")[:500]
    new_content = (after.content or "*(bos)*")[:500]

    embed = discord.Embed(
        title="Mesaj duzenlendi",
        description=f"**Kanal:** {before.channel.mention}\n**Yazan:** {before.author}",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Eski hali", value=old_content, inline=False)
    embed.add_field(name="Yeni hali", value=new_content, inline=False)

    await send_log_embed(entry, embed)


# ------------------------------------------------------------------
# Ses kanali giris/cikis loglama
# ------------------------------------------------------------------
@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if before.channel == after.channel:
        return

    data = load_data()
    entry = get_guild_entry(data, str(member.guild.id))
    save_data(data)

    if before.channel is None and after.channel is not None:
        desc = f"🔊 {member} **{after.channel.name}** ses kanalina katildi."
        color = 0x53FC18
    elif before.channel is not None and after.channel is None:
        desc = f"🔇 {member} **{before.channel.name}** ses kanalindan ayrildi."
        color = 0x6B726C
    else:
        desc = f"🔀 {member} **{before.channel.name}** kanalindan **{after.channel.name}** kanalina gecti."
        color = 0x5865F2

    embed = discord.Embed(description=desc, color=color, timestamp=datetime.now(timezone.utc))
    await send_log_embed(entry, embed)


# ------------------------------------------------------------------
# Arka plan kontrol dongusu - Kick canli/kategori/yayin sonu kontrolu
# ------------------------------------------------------------------
@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_streams():
    data = load_data()
    if not data:
        return

    all_slugs = set()
    for entry in data.values():
        all_slugs.update(entry.get("streamers", {}).keys())

    if not all_slugs:
        return

    try:
        statuses = get_channels_status(list(all_slugs))
    except requests.exceptions.RequestException as e:
        print(f"[HATA] Kick API istegi basarisiz: {e}")
        return
    except Exception as e:
        print(f"[HATA] Beklenmeyen hata: {e}")
        return

    changed = False

    for guild_id, entry in data.items():
        streamers = entry.get("streamers", {})

        for slug, state in streamers.items():
            info = statuses.get(slug)
            if info is None:
                continue

            was_live = state.get("is_live", False)
            is_live = info["is_live"]
            prev_category = state.get("category")
            new_category = info.get("category")

            live_channel_id = get_notify_channel_id(entry, "canli")
            kategori_channel_id = get_notify_channel_id(entry, "kategori")

            if is_live and not was_live and live_channel_id:
                channel = client.get_channel(live_channel_id)
                if channel is not None:
                    embed = discord.Embed(
                        title=f"{slug} canli yayina gecti",
                        url=info["url"],
                        description=info["title"] or "Yayin basladi.",
                        color=0x53FC18,
                        timestamp=datetime.now(timezone.utc),
                    )
                    if new_category:
                        embed.add_field(name="Kategori", value=new_category, inline=True)
                    if info.get("thumbnail"):
                        embed.set_image(url=info["thumbnail"])
                    try:
                        sent_message = await channel.send(
                            content=f"**{slug}** yayina girdi -> {info['url']}", embed=embed
                        )
                        # Thumbnail'i periyodik guncelleyebilmek icin mesaji hatirla
                        state["live_message_id"] = sent_message.id
                        state["live_channel_id"] = channel.id
                    except discord.DiscordException as e:
                        print(f"[HATA] Mesaj gonderilemedi: {e}")

            elif not is_live and was_live and live_channel_id:
                channel = client.get_channel(live_channel_id)
                if channel is not None:
                    embed = discord.Embed(
                        title=f"{slug} yayini sonlandirdi",
                        url=info["url"],
                        description="Yayin sona erdi.",
                        color=0x6B726C,
                        timestamp=datetime.now(timezone.utc),
                    )
                    try:
                        await channel.send(content=f"**{slug}** yayini bitirdi.", embed=embed)
                    except discord.DiscordException as e:
                        print(f"[HATA] Yayin sonu mesaji gonderilemedi: {e}")
                state["live_message_id"] = None
                state["live_channel_id"] = None

            elif (
                is_live
                and was_live
                and kategori_channel_id
                and new_category
                and prev_category
                and new_category != prev_category
            ):
                channel = client.get_channel(kategori_channel_id)
                if channel is not None:
                    embed = discord.Embed(
                        title=f"{slug} kategori degistirdi",
                        url=info["url"],
                        description=f"**{prev_category}** ➜ **{new_category}**",
                        color=0x5865F2,
                        timestamp=datetime.now(timezone.utc),
                    )
                    try:
                        await channel.send(
                            content=f"**{slug}** kategoriyi degistirdi: **{new_category}**",
                            embed=embed,
                        )
                    except discord.DiscordException as e:
                        print(f"[HATA] Kategori mesaji gonderilemedi: {e}")

            # Yayin devam ediyorsa, canli bildirim mesajinin thumbnail'ini
            # guncel tut (Kick'in ana ekran goruntusu periyodik degisir).
            if is_live and state.get("live_message_id") and state.get("live_channel_id") and info.get("thumbnail"):
                try:
                    live_channel = client.get_channel(state["live_channel_id"])
                    if live_channel is not None:
                        msg = await live_channel.fetch_message(state["live_message_id"])
                        if msg.embeds:
                            updated_embed = msg.embeds[0]
                            updated_embed.set_image(url=info["thumbnail"])
                            if info.get("title"):
                                updated_embed.description = info["title"]
                            await msg.edit(embed=updated_embed)
                except discord.NotFound:
                    state["live_message_id"] = None
                    state["live_channel_id"] = None
                except discord.DiscordException as e:
                    print(f"[UYARI] Thumbnail guncellenemedi ({slug}): {e}")

            # --- DENEYSEL: yeni klip kontrolu ---
            klip_channel_id = get_notify_channel_id(entry, "klip")
            if klip_channel_id:
                try:
                    latest_clip = await get_latest_clip(slug)
                except Exception as e:
                    latest_clip = None
                    print(f"[UYARI] Klip bilgisi alinamadi ({slug}) - bu ozellik resmi degil, calismayabilir: {e}")

                if latest_clip and latest_clip.get("id"):
                    last_seen_clip_id = state.get("last_clip_id")
                    if last_seen_clip_id != latest_clip["id"]:
                        if last_seen_clip_id is not None:  # ilk kontrolde spam yapma, sadece referans al
                            channel = client.get_channel(klip_channel_id)
                            if channel is not None:
                                embed = discord.Embed(
                                    title=f"{slug} icin yeni klip: {latest_clip['title']}",
                                    url=latest_clip["url"],
                                    color=0xFFB454,
                                    timestamp=datetime.now(timezone.utc),
                                )
                                if latest_clip.get("creator"):
                                    embed.add_field(name="Klip sahibi", value=latest_clip["creator"], inline=True)
                                if latest_clip.get("thumbnail"):
                                    embed.set_image(url=latest_clip["thumbnail"])
                                try:
                                    await channel.send(content=f"🎬 **{slug}** icin yeni klip!", embed=embed)
                                except discord.DiscordException as e:
                                    print(f"[HATA] Klip mesaji gonderilemedi: {e}")
                        state["last_clip_id"] = latest_clip["id"]
                        changed = True

            # --- DENEYSEL: sabitlenen mesaj kontrolu ---
            sabitmesaj_channel_id = get_notify_channel_id(entry, "sabitmesaj")
            if sabitmesaj_channel_id:
                try:
                    pinned = await get_pinned_message(slug)
                except Exception as e:
                    pinned = None
                    print(f"[UYARI] Sabit mesaj bilgisi alinamadi ({slug}) - bu ozellik resmi degil, calismayabilir: {e}")

                pinned_id = pinned.get("id") if pinned else None
                last_seen_pinned_id = state.get("last_pinned_id")
                if pinned_id != last_seen_pinned_id:
                    if pinned_id is not None and last_seen_pinned_id is not None:
                        # yeni bir mesaj sabitlendi (ilk tespitte spam yapma)
                        channel = client.get_channel(sabitmesaj_channel_id)
                        if channel is not None:
                            embed = discord.Embed(
                                title=f"{slug} yeni bir mesaj sabitledi",
                                description=pinned.get("content", ""),
                                color=0x53FC18,
                                timestamp=datetime.now(timezone.utc),
                            )
                            if pinned.get("sender"):
                                embed.add_field(name="Yazan", value=pinned["sender"], inline=True)
                            try:
                                await channel.send(content=f"📌 **{slug}** bir mesaj sabitledi!", embed=embed)
                            except discord.DiscordException as e:
                                print(f"[HATA] Sabit mesaj bildirimi gonderilemedi: {e}")
                    state["last_pinned_id"] = pinned_id
                    changed = True

            if is_live != was_live or new_category != prev_category:
                state["is_live"] = is_live
                state["category"] = new_category
                changed = True

    if changed:
        save_data(data)


@check_streams.before_loop
async def before_check_streams():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("HATA: DISCORD_BOT_TOKEN ortam degiskeni ayarlanmamis.")
    if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
        raise SystemExit("HATA: KICK_CLIENT_ID / KICK_CLIENT_SECRET ortam degiskenleri ayarlanmamis.")

    client.run(DISCORD_BOT_TOKEN)
