#!/usr/bin/env python3
"""
Kick Canli Yayin Bildirim & ModLog Botu
----------------------------------------
1. /yayinciekle ile eklenen Kick yayincilarindan biri canli yayina
   gectiginde otomatik mesaj atan bot.
2. Kick chat'taki ban, timeout, unban ve mesaj silme olaylarini
   yakalayıp Discord'a log olarak gonderen ModLog eklentisi.

Gerekli ortam degiskenleri (.env dosyasi ya da hosting panelinden):
    DISCORD_BOT_TOKEN
    KICK_CLIENT_ID
    KICK_CLIENT_SECRET
    MODLOG_CHANNEL_SLUG       # Loglanacak Kick kanali slug'i
    MODLOG_DISCORD_CHANNEL_ID # Log kanalinin Discord ID'si
    CHECK_INTERVAL_SECONDS    # Varsayılan 20

Calistirmak icin:
    pip install -r requirements.txt
    python bot.py
"""

import json
import os
import time
from datetime import datetime, timezone

import discord
import requests
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID")
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET")
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "20"))

# ModLog ayarlari
MODLOG_CHANNEL_SLUG = os.getenv("MODLOG_CHANNEL_SLUG", "")
MODLOG_DISCORD_CHANNEL_ID = int(os.getenv("MODLOG_DISCORD_CHANNEL_ID", "0"))

KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_CHANNELS_URL = "https://api.kick.com/public/v1/channels"

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guilds.json")

# ------------------------------------------------------------------
# Sunucu bazli veri
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
        data[guild_id] = {"channel_id": None, "streamers": {}}
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
        result[slug] = {
            "is_live": bool(stream.get("is_live")),
            "title": stream.get("stream_title") or channel.get("stream_title") or "",
            "thumbnail": stream.get("thumbnail") or "",
            "viewers": stream.get("viewer_count"),
            "url": f"https://kick.com/{slug}",
        }
    return result


# ------------------------------------------------------------------
# ModLog import (try/except ile guvenli)
# ------------------------------------------------------------------
KickModLog = None
try:
    from kick_modlog import KickModLog as _KickModLog
    KickModLog = _KickModLog
    print("[BILGI] kick_modlog modulu basariyla yuklendi.")
except Exception as e:
    print(f"[BILGI] kick_modlog modulu yuklenemedi: {e}")


# ------------------------------------------------------------------
# Discord bot
# ------------------------------------------------------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ModLog instance (global)
modlog = None


@client.event
async def on_ready():
    global modlog
    await tree.sync()
    print(f"[BILGI] Giris yapildi: {client.user}")

    # ModLog baslat
    if KickModLog and MODLOG_CHANNEL_SLUG and MODLOG_DISCORD_CHANNEL_ID:
        try:
            modlog = KickModLog(
                channel_slug=MODLOG_CHANNEL_SLUG,
                discord_channel_id=MODLOG_DISCORD_CHANNEL_ID,
                discord_client=client,
                debug=False,
            )
            result = await modlog.start()
            if result:
                print(f"[BILGI] Kick ModLog baslatildi: {MODLOG_CHANNEL_SLUG}")
            else:
                print(f"[BILGI] Kick ModLog baslatilamadi (kanal bilgisi alinamadi)")
        except Exception as e:
            print(f"[BILGI] Kick ModLog hatasi: {e}")
    else:
        print("[BILGI] ModLog ayarlari bulunamadi, atlandi.")

    if not check_streams.is_running():
        check_streams.start()


@tree.command(name="kanalayarla", description="Kick canli yayin bildirimlerinin gonderilecegi kanali secer")
@app_commands.describe(kanal="Bildirimlerin gonderilecegi metin kanali")
@app_commands.checks.has_permissions(manage_guild=True)
async def kanalayarla(interaction: discord.Interaction, kanal: discord.TextChannel):
    data = load_data()
    entry = get_guild_entry(data, str(interaction.guild_id))
    entry["channel_id"] = kanal.id
    save_data(data)
    await interaction.response.send_message(f"Bildirim kanali {kanal.mention} olarak ayarlandi.", ephemeral=True)


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

    entry["streamers"][slug] = {"is_live": False}
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
        lines.append(f"- **{slug}** — {durum}")

    channel_id = entry.get("channel_id")
    channel_text = f"<#{channel_id}>" if channel_id else "ayarlanmadi (`/kanalayarla` kullan)"

    await interaction.response.send_message(
        f"Bildirim kanali: {channel_text}\n\n" + "\n".join(lines),
        ephemeral=True,
    )


@tree.command(name="modlog-durum", description="Kick ModLog baglanti durumunu gosterir")
async def modlog_durum(interaction: discord.Interaction):
    if modlog is None:
        await interaction.response.send_message("ModLog calismiyor. Ortam degiskenlerini kontrol et.", ephemeral=True)
        return

    status = modlog.status
    embed = discord.Embed(
        title="Kick ModLog Durumu",
        color=0x53FC18,
    )
    embed.add_field(name="Kanal", value=status["channel"], inline=True)
    embed.add_field(name="Chatroom ID", value=str(status["chatroom_id"] or "Bilinmiyor"), inline=True)
    embed.add_field(
        name="Baglanti",
        value="🟢 Bagli" if status["connected"] else "🔴 Bagli Degil",
        inline=True,
    )
    embed.add_field(
        name="Otomatik Yeniden Baglanma",
        value="Aktif" if status["auto_reconnect"] else "Pasif",
        inline=True,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------------------------------------------------------
# Arka plan kontrol dongusu
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
        channel_id = entry.get("channel_id")
        streamers = entry.get("streamers", {})

        for slug, state in streamers.items():
            info = statuses.get(slug)
            if info is None:
                continue

            was_live = state.get("is_live", False)
            is_live = info["is_live"]

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
                    if info.get("thumbnail"):
                        embed.set_image(url=info["thumbnail"])
                    try:
                        await channel.send(content=f"**{slug}** yayina girdi -> {info['url']}", embed=embed)
                    except discord.DiscordException as e:
                        print(f"[HATA] Mesaj gonderilemedi: {e}")

            if is_live != was_live:
                state["is_live"] = is_live
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
