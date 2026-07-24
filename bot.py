#!/usr/bin/env python3
"""
Kick Canli Yayin Bildirim Botu + Sunucu Loglama Paketi (Firebase surumu)
---------------------------------------------------------------------------
Ozellikler:
  - /yayinciekle veya /izlemebaslat: Kick yayincisi takip listesine ekler
  - /yayincisil veya /izlemedurdur: listeden cikarir
  - Canli yayina gecince, yayin bitince ve kategori degisince otomatik bildirim
  - /kanit: yayincinin su anki resmi Kick onizleme goreselini gonderir
  - /loglamakanali: ban/unban/timeout/kick/mesaj sil-duzenle/katilma-ayrilma/
    ses kanali loglarinin gonderilecegi kanali secer
  - /hosgeldinkanali + /otorolayarla: yeni uye karsilama + otomatik rol
  - /sunucubilgi: sunucu ve bot ayarlari ozeti
  - /kesiftest: (deneysel) Kick chat'inde ban/timeout sinyali arar

Veri Firebase Firestore'da saklanir (Railway'in gecici dosya sisteminde
kaybolmaz).

Gerekli ortam degiskenleri:
    DISCORD_BOT_TOKEN
    KICK_CLIENT_ID
    KICK_CLIENT_SECRET
    FIREBASE_SERVICE_ACCOUNT_JSON   (Firebase servis hesabi anahtarinin
                                      TAM JSON icerigi, tek satir/deger olarak)
    CHECK_INTERVAL_SECONDS          (opsiyonel, varsayilan 20)

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
import firebase_admin
import requests
import websockets
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from firebase_admin import credentials, firestore

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID")
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "20"))
FIREBASE_SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

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
# Firebase / Firestore baglantisi
# ------------------------------------------------------------------
db = None


def init_firebase():
    global db
    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        raise SystemExit(
            "HATA: FIREBASE_SERVICE_ACCOUNT_JSON ortam degiskeni ayarlanmamis. "
            "Firebase Console -> Project Settings -> Service Accounts -> "
            "Generate new private key ile aldigin JSON dosyasinin TAM "
            "icerigini bu degiskene yapistirmalisin."
        )
    try:
        cred_dict = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError as e:
        raise SystemExit(f"HATA: FIREBASE_SERVICE_ACCOUNT_JSON gecerli bir JSON degil: {e}")

    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("[BILGI] Firebase Firestore baglantisi kuruldu.")


DEFAULT_GUILD_ENTRY = {
    "channel_id": None,
    "log_channel_id": None,
    "welcome_channel_id": None,
    "auto_role_id": None,
    "streamers": {},
}


def _get_guild_entry_sync(guild_id: str):
    doc = db.collection("guilds").document(guild_id).get()
    data = doc.to_dict() if doc.exists else {}
    merged = {**DEFAULT_GUILD_ENTRY, **data}
    merged["streamers"] = data.get("streamers") or {}
    return merged


def _save_guild_entry_sync(guild_id: str, entry: dict):
    db.collection("guilds").document(guild_id).set(entry)


def _get_all_guilds_sync():
    result = {}
    for doc in db.collection("guilds").stream():
        data = doc.to_dict() or {}
        merged = {**DEFAULT_GUILD_ENTRY, **data}
        merged["streamers"] = data.get("streamers") or {}
        result[doc.id] = merged
    return result


async def get_guild_entry(guild_id: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_guild_entry_sync, guild_id)


async def save_guild_entry(guild_id: str, entry: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _save_guild_entry_sync, guild_id, entry)


async def get_all_guilds():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _get_all_guilds_sync)


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


def get_log_channel(entry):
    log_channel_id = entry.get("log_channel_id")
    if not log_channel_id:
        return None
    return client.get_channel(log_channel_id)


async def send_log_embed(entry, embed: discord.Embed):
    channel = get_log_channel(entry)
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
    entry = await get_guild_entry(str(interaction.guild_id))

    if slug in entry["streamers"]:
        await interaction.response.send_message(f"**{slug}** zaten listede.", ephemeral=True)
        return

    entry["streamers"][slug] = {"is_live": False, "category": None}
    await save_guild_entry(str(interaction.guild_id), entry)
    await interaction.response.send_message(f"**{slug}** takip listesine eklendi.", ephemeral=True)


async def _remove_streamer(interaction: discord.Interaction, kullanici_adi: str):
    slug = kullanici_adi.strip().lower()
    entry = await get_guild_entry(str(interaction.guild_id))

    if slug not in entry["streamers"]:
        await interaction.response.send_message(f"**{slug}** listede bulunamadi.", ephemeral=True)
        return

    del entry["streamers"][slug]
    await save_guild_entry(str(interaction.guild_id), entry)
    await interaction.response.send_message(f"**{slug}** listeden cikarildi.", ephemeral=True)


@tree.command(name="kanalayarla", description="Kick canli yayin bildirimlerinin gonderilecegi kanali secer")
@app_commands.describe(kanal="Bildirimlerin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def kanalayarla(interaction: discord.Interaction, kanal: discord.TextChannel):
    entry = await get_guild_entry(str(interaction.guild_id))
    entry["channel_id"] = kanal.id
    await save_guild_entry(str(interaction.guild_id), entry)
    await interaction.response.send_message(f"Bildirim kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


@tree.command(name="loglamakanali", description="Sunucu loglarinin (ban/timeout/kick/mesaj/katilma/ses) gonderilecegi kanali secer")
@app_commands.describe(kanal="Loglarin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def loglamakanali(interaction: discord.Interaction, kanal: discord.TextChannel):
    entry = await get_guild_entry(str(interaction.guild_id))
    entry["log_channel_id"] = kanal.id
    await save_guild_entry(str(interaction.guild_id), entry)
    await interaction.response.send_message(f"Log kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


@tree.command(name="hosgeldinkanali", description="Yeni uye katilinca hos geldin mesajinin gonderilecegi kanali secer")
@app_commands.describe(kanal="Hos geldin mesajlarinin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def hosgeldinkanali(interaction: discord.Interaction, kanal: discord.TextChannel):
    entry = await get_guild_entry(str(interaction.guild_id))
    entry["welcome_channel_id"] = kanal.id
    await save_guild_entry(str(interaction.guild_id), entry)
    await interaction.response.send_message(f"Hos geldin kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


@tree.command(name="otorolayarla", description="Yeni uyelere otomatik verilecek rolu secer")
@app_commands.describe(rol="Yeni katilan uyelere otomatik verilecek rol")
@app_commands.checks.has_permissions(manage_guild=True)
async def otorolayarla(interaction: discord.Interaction, rol: discord.Role):
    entry = await get_guild_entry(str(interaction.guild_id))
    entry["auto_role_id"] = rol.id
    await save_guild_entry(str(interaction.guild_id), entry)
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
    entry = await get_guild_entry(str(interaction.guild_id))
    streamers = entry["streamers"]

    if not streamers:
        await interaction.response.send_message("Henuz takip edilen yayinci yok. `/yayinciekle` ile ekleyebilirsin.", ephemeral=True)
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

    await interaction.response.send_message(
        f"Bildirim kanali: {ch_text(entry.get('channel_id'), '/kanalayarla')}\n"
        f"Log kanali: {ch_text(entry.get('log_channel_id'), '/loglamakanali')}\n\n"
        + "\n".join(lines),
        ephemeral=True,
    )


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
    entry = await get_guild_entry(str(guild.id))

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
    entry = await get_guild_entry(str(guild.id))

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
    entry = await get_guild_entry(str(guild.id))

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

    entry = await get_guild_entry(str(after.guild.id))
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
    entry = await get_guild_entry(str(member.guild.id))

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
    entry = await get_guild_entry(str(member.guild.id))

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

    entry = await get_guild_entry(str(message.guild.id))
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

    entry = await get_guild_entry(str(before.guild.id))
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

    entry = await get_guild_entry(str(member.guild.id))

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
    try:
        all_guilds = await get_all_guilds()
    except Exception as e:
        print(f"[HATA] Firestore'dan veri okunamadi: {e}")
        return

    if not all_guilds:
        return

    all_slugs = set()
    for entry in all_guilds.values():
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

    for guild_id, entry in all_guilds.items():
        channel_id = entry.get("channel_id")
        streamers = entry.get("streamers", {})
        changed = False

        for slug, state in streamers.items():
            info = statuses.get(slug)
            if info is None:
                continue

            was_live = state.get("is_live", False)
            is_live = info["is_live"]
            prev_category = state.get("category")
            new_category = info.get("category")

            if is_live and not was_live and channel_id:
                channel = client.get_channel(channel_id)
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
                        await channel.send(content=f"**{slug}** yayina girdi -> {info['url']}", embed=embed)
                    except discord.DiscordException as e:
                        print(f"[HATA] Mesaj gonderilemedi: {e}")

            elif not is_live and was_live and channel_id:
                channel = client.get_channel(channel_id)
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

            elif (
                is_live
                and was_live
                and channel_id
                and new_category
                and prev_category
                and new_category != prev_category
            ):
                channel = client.get_channel(channel_id)
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

            if is_live != was_live or new_category != prev_category:
                state["is_live"] = is_live
                state["category"] = new_category
                changed = True

        if changed:
            entry["streamers"] = streamers
            try:
                await save_guild_entry(guild_id, entry)
            except Exception as e:
                print(f"[HATA] Firestore'a yazilamadi ({guild_id}): {e}")


@check_streams.before_loop
async def before_check_streams():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("HATA: DISCORD_BOT_TOKEN ortam degiskeni ayarlanmamis.")
    if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
        raise SystemExit("HATA: KICK_CLIENT_ID / KICK_CLIENT_SECRET ortam degiskenleri ayarlanmamis.")

    init_firebase()
    client.run(DISCORD_BOT_TOKEN)
