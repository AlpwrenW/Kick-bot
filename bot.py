#!/usr/bin/env python3
"""
Kick Canli Yayin Bildirim Botu + Ban Log
-------------------------------------------
Discord sunucunda /yayinciekle ile eklenen Kick yayincilarindan biri
canli yayina gectiginde ya da yayin sirasinda kategori (oyun) degistirdiginde
otomatik mesaj atan, ayrica sunucuda biri banlandiginda/banı kaldirildiginda
/loglamakanali ile ayarlanan kanala otomatik log atan bot.

Gerekli ortam degiskenleri (.env dosyasi ya da hosting panelinden):
    DISCORD_BOT_TOKEN
    KICK_CLIENT_ID
    KICK_CLIENT_SECRET

Calistirmak icin:
    pip install -r requirements.txt
    python bot.py
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

KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_CHANNELS_URL = "https://api.kick.com/public/v1/channels"
KICK_UNOFFICIAL_CHANNEL_URL = "https://kick.com/api/v2/channels/{slug}"

# Kick'in herkese acik chat websocket'i (Pusher altyapisi, kimlik dogrulama
# istemiyor). Sadece /kesiftest komutu icin kullaniliyor.
KICK_PUSHER_URL = (
    "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679"
    "?protocol=7&client=js&version=8.4.0-rc2&flash=false"
)

# /kesiftest sirasinda "normal/bilinen" sayilan eventler - bunlarin
# DISINDA gelen her sey potansiyel ban/timeout sinyali olarak yakalanir.
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

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guilds.json")

# ------------------------------------------------------------------
# Sunucu bazli veri (hangi kanala bildirim atilacak, hangi yayincilar
# izleniyor, kim su an canli). Basit JSON dosyasinda tutuluyor.
# ------------------------------------------------------------------
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_guild_entry(data, guild_id: str):
    if guild_id not in data:
        data[guild_id] = {"channel_id": None, "streamers": {}, "log_channel_id": None}
    else:
        data[guild_id].setdefault("log_channel_id", None)
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
    """Kick'in resmi olmayan (Cloudflare korumali) endpoint'inden
    chatroom ID'sini cekmeyi dener. Basarisiz olursa None doner - bu
    durumda kullanici chatroom_id'yi elle girmeli."""

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
    """Belirtilen chatroom'u duration_seconds boyunca dinler, bilinmeyen
    (KICK_KNOWN_CHAT_EVENTS listesinde olmayan) eventleri toplar."""
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
# Discord bot
# ------------------------------------------------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    print(f"[BILGI] Giris yapildi: {client.user}")

    # Kontrol dongusunu ONCE baslat - komut senkronizasyonu hata verse bile
    # canli yayin / kategori kontrolu calismaya devam etsin.
    if not check_streams.is_running():
        check_streams.start()

    try:
        synced = await tree.sync()
        print(f"[BILGI] {len(synced)} komut Discord'a senkronize edildi: "
              f"{', '.join(sorted(c.name for c in synced))}")
    except Exception as e:
        print(f"[HATA] Komutlar senkronize edilemedi: {e}")


@tree.command(name="kanalayarla", description="Kick canli yayin bildirimlerinin gonderilecegi kanali secer")
@app_commands.describe(kanal="Bildirimlerin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def kanalayarla(interaction: discord.Interaction, kanal: discord.TextChannel):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    entry["channel_id"] = kanal.id
    save_data(data)
    await interaction.response.send_message(f"Bildirim kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


@tree.command(name="loglamakanali", description="Banlanan/ban kaldirilan uyelerin loglanacagi kanali secer")
@app_commands.describe(kanal="Ban loglarinin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def loglamakanali(interaction: discord.Interaction, kanal: discord.TextChannel):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    entry["log_channel_id"] = kanal.id
    save_data(data)
    await interaction.response.send_message(f"Ban log kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


@tree.command(name="yayinciekle", description="Takip listesine bir Kick yayincisi ekler")
@app_commands.describe(kullanici_adi="Kick kullanici adi (kick.com/KULLANICIADI)")
@app_commands.checks.has_permissions(manage_guild=True)
async def yayinciekle(interaction: discord.Interaction, kullanici_adi: str):
    slug = kullanici_adi.strip().lower()
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))

    if slug in entry["streamers"]:
        await interaction.response.send_message(f"**{slug}** zaten listede.", ephemeral=True)
        return

    entry["streamers"][slug] = {"is_live": False, "category": None}
    save_data(data)
    await interaction.response.send_message(f"**{slug}** takip listesine eklendi.", ephemeral=True)


@tree.command(name="yayincisil", description="Takip listesinden bir Kick yayincisini cikarir")
@app_commands.describe(kullanici_adi="Kick kullanici adi")
@app_commands.checks.has_permissions(manage_guild=True)
async def yayincisil(interaction: discord.Interaction, kullanici_adi: str):
    slug = kullanici_adi.strip().lower()
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))

    if slug not in entry["streamers"]:
        await interaction.response.send_message(f"**{slug}** listede bulunamadi.", ephemeral=True)
        return

    del entry["streamers"][slug]
    save_data(data)
    await interaction.response.send_message(f"**{slug}** listeden cikarildi.", ephemeral=True)


@tree.command(name="liste", description="Takip edilen Kick yayincilarini listeler")
async def liste(interaction: discord.Interaction):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
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

    channel_id = entry.get("channel_id")
    channel_text = f"<#{channel_id}>" if channel_id else "ayarlanmadi (`/kanalayarla` kullan)"

    log_channel_id = entry.get("log_channel_id")
    log_channel_text = f"<#{log_channel_id}>" if log_channel_id else "ayarlanmadi (`/loglamakanali` kullan)"

    await interaction.response.send_message(
        f"Bildirim kanali: {channel_text}\n"
        f"Ban log kanali: {log_channel_text}\n\n" + "\n".join(lines),
        ephemeral=True,
    )


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
        f"(chatroom_id: `{resolved_id}`). Bu sure icinde o kanalda bilerek bir "
        f"ban/timeout/unban islemi yaptirirsan sonuc daha net olur. "
        f"Sonuc birazdan burada.",
        ephemeral=True,
    )

    found_events, error = await listen_for_unknown_events(int(resolved_id), duration)

    if error:
        await interaction.followup.send(f"Baglanti hatasi olustu: {error}", ephemeral=True)
        return

    if not found_events:
        await interaction.followup.send(
            "**Test bitti.** Bu sure icinde bilinmeyen (potansiyel ban/timeout) bir "
            "event yakalanmadi. Ya bu sure icinde kanalda boyle bir islem olmadi, "
            "ya da Kick bu bilgiyi herkese acik yayinlamiyor (simdilik boyle "
            "gorunuyor). Tekrar denemek istersen o kanalda testin ortasinda "
            "birinin banlanmasini/timeout yemesini saglamayi dene.",
            ephemeral=True,
        )
        return

    report = json.dumps(found_events, indent=2, ensure_ascii=False)
    file_obj = discord.File(io.BytesIO(report.encode("utf-8")), filename=f"{slug}_kesif_sonuclari.json")

    await interaction.followup.send(
        f"**{len(found_events)} bilinmeyen event yakalandi!** Detaylar ekli dosyada. "
        f"Bunlardan ban/timeout ile ilgili olani bulursak, kalici bir loglama "
        f"ozelligi olarak bota ekleyebiliriz.",
        file=file_obj,
        ephemeral=True,
    )


# ------------------------------------------------------------------
# Ban / unban loglama
# ------------------------------------------------------------------
async def find_audit_log_entry(guild: discord.Guild, action, target_id: int):
    """Son ban/unban islemini yapan yetkiliyi ve sebebi audit log'dan bulur."""
    try:
        async for entry in guild.audit_logs(action=action, limit=5):
            if entry.target and entry.target.id == target_id:
                return entry
    except discord.Forbidden:
        print("[UYARI] Audit log okuma yetkisi yok. Bota 'Denetim Kaydini Goruntule' yetkisi ver.")
    except discord.HTTPException as e:
        print(f"[HATA] Audit log okunamadi: {e}")
    return None


@client.event
async def on_member_ban(guild: discord.Guild, user):
    data = load_data()
    entry = get_guild_entry(data, str(guild.id))
    save_data(data)

    log_channel_id = entry.get("log_channel_id")
    if not log_channel_id:
        return
    channel = client.get_channel(log_channel_id)
    if channel is None:
        return

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

    try:
        await channel.send(embed=embed)
    except discord.DiscordException as e:
        print(f"[HATA] Ban log mesaji gonderilemedi: {e}")


@client.event
async def on_member_unban(guild: discord.Guild, user):
    data = load_data()
    entry = get_guild_entry(data, str(guild.id))
    save_data(data)

    log_channel_id = entry.get("log_channel_id")
    if not log_channel_id:
        return
    channel = client.get_channel(log_channel_id)
    if channel is None:
        return

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

    try:
        await channel.send(embed=embed)
    except discord.DiscordException as e:
        print(f"[HATA] Unban log mesaji gonderilemedi: {e}")


# ------------------------------------------------------------------
# Arka plan kontrol dongusu
# ------------------------------------------------------------------
@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_streams():
    data = load_data()
    if not data:
        return

    # Tum sunuculardaki tum yayincilari topla, tek istekte sorgula
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
        channel_id = entry.get("channel_id")
        streamers = entry.get("streamers", {})

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

            elif (
                is_live
                and was_live
                and channel_id
                and new_category
                and prev_category
                and new_category != prev_category
            ):
                # Yayin zaten aciktı, kategori degisti
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
