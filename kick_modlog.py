"""
Kick ModLog Eklentisi
---------------------
Kick chat'taki ban, timeout, unban ve mesaj silme olaylarını yakalar
ve Discord sunucusunda belirlenen kanala log olarak gönderir.

Kullanım:
    from kick_modlog import KickModLog

    modlog = KickModLog(
        channel_slug="kick-kanal-slug",
        discord_channel_id=1234567890,
        discord_client=client,
    )
    await modlog.start()
"""

import asyncio
import json
import logging

import websockets

logger = logging.getLogger("kick_modlog")

PUSHER_WS_URL = (
    "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679"
    "?protocol=7&client=js&version=8.4.0-rc2&flash=false"
)


class KickModLog:
    """Kick chat modlog eklentisi."""

    # ---------- renk kodları ----------
    COLOR_BAN = 0xED4245       # kırmızı
    COLOR_TIMEOUT = 0xFEE75C   # sarı
    COLOR_UNBAN = 0x57F287     # yeşil
    COLOR_DELETE = 0x99AAB5    # gri
    COLOR_AUTO_DELETE = 0xFF6B6B

    def __init__(
        self,
        *,
        channel_slug: str,
        discord_channel_id: int,
        discord_client,
        debug: bool = False,
        auto_reconnect: bool = True,
        reconnect_delay: int = 5,
    ):
        self.channel_slug = channel_slug
        self.discord_channel_id = discord_channel_id
        self.client = discord_client
        self.debug = debug
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay

        self.chatroom_id: int | None = None
        self.channel_id: int | None = None
        self._ws = None
        self._connected = False
        self._ping_task: asyncio.Task | None = None
        self._listen_task: asyncio.Task | None = None

        # Callback'ler
        self.on_ban = None
        self.on_unban = None
        self.on_message_delete = None

    # ──────────────────────────────────────────────────────────
    # public API
    # ──────────────────────────────────────────────────────────

    async def start(self):
        """Channel bilgilerini çek ve WebSocket dinlemeyi başlat."""
        if self.debug:
            logger.info(f"[KickModLog] {self.channel_slug} için başlatılıyor...")

        success = await self._fetch_channel_info()
        if not success:
            logger.error("[KickModLog] Kanal bilgisi alınamadı.")
            return False

        self._listen_task = asyncio.create_task(self._connect_loop())
        return True

    async def stop(self):
        """Dinlemeyi durdur ve bağlantıyı kapat."""
        self.auto_reconnect = False
        if self._ping_task:
            self._ping_task.cancel()
        if self._listen_task:
            self._listen_task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected = False
        logger.info("[KickModLog] Durduruldu.")

    def on_ban_event(self, callback):
        """Ban/timeout olayları için callback kaydet."""
        self.on_ban = callback

    def on_unban_event(self, callback):
        """Unban/untimeout olayları için callback kaydet."""
        self.on_unban = callback

    def on_delete_event(self, callback):
        """Mesaj silme olayları için callback kaydet."""
        self.on_message_delete = callback

    @property
    def status(self) -> dict:
        return {
            "channel": self.channel_slug,
            "chatroom_id": self.chatroom_id,
            "connected": self._connected,
            "auto_reconnect": self.auto_reconnect,
        }

    # ──────────────────────────────────────────────────────────
    # dahili
    # ──────────────────────────────────────────────────────────

    async def _fetch_channel_info(self) -> bool:
        """Kick API'den chatroom ve channel ID'lerini çeker."""
        import aiohttp

        url = f"https://kick.com/api/v2/channels/{self.channel_slug}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.json()
                    self.chatroom_id = data.get("chatroom", {}).get("id")
                    self.channel_id = data.get("id")
                    if not self.chatroom_id:
                        return False
                    if self.debug:
                        logger.info(
                            f"[KickModLog] Channel ID: {self.channel_id}, "
                            f"Chatroom ID: {self.chatroom_id}"
                        )
                    return True
        except Exception as e:
            logger.error(f"[KickModLog] API hatası: {e}")
            return False

    async def _connect_loop(self):
        """Bağlantı koptuğunda otomatik yeniden bağlanan döngü."""
        while self.auto_reconnect:
            try:
                await self._connect_and_listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[KickModLog] Bağlantı hatası: {e}")

            if self.auto_reconnect:
                logger.info(
                    f"[KickModLog] {self.reconnect_delay}s sonra yeniden bağlanıyor..."
                )
                await asyncio.sleep(self.reconnect_delay)

    async def _connect_and_listen(self):
        """Pusher WebSocket'e bağlanır ve mesajları dinler."""
        import aiohttp

        # aiohttp.ClientSession üzerinden WebSocket
        async with aiohttp.ClientSession() as session:
            self._ws = await session.ws_connect(
                PUSHER_WS_URL,
                timeout=aiohttp.ClientTimeout(total=None),
            )

            # connection_ack bekle
            init_msg = await self._ws.receive_json()
            if self.debug:
                logger.info(f"[KickModLog] Bağlandı: {init_msg}")

            # Abonelikler
            subscribe_msgs = [
                {
                    "event": "pusher:subscribe",
                    "data": {"auth": "", "channel": f"chatrooms.{self.chatroom_id}.v2"},
                },
            ]
            # Ayrıca channel.{id} kanalına da abone ol
            if self.channel_id:
                subscribe_msgs.append({
                    "event": "pusher:subscribe",
                    "data": {"auth": "", "channel": f"channel.{self.channel_id}"},
                })

            for msg in subscribe_msgs:
                await self._ws.send_json(msg)

            self._connected = True

            # Ping döngüsünü başlat
            if self._ping_task:
                self._ping_task.cancel()
            self._ping_task = asyncio.create_task(self._ping_loop())

            if self.debug:
                logger.info(f"[KickModLog] Dinleme başladı ({self.channel_slug})")

            # Mesaj dinleme döngüsü
            try:
                while self._connected:
                    msg = await self._ws.receive_json()
                    self._handle_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[KickModLog] Mesaj dinleme hatası: {e}")
            finally:
                self._connected = False
                if self._ping_task:
                    self._ping_task.cancel()
                await self._ws.close()
                self._ws = None

    async def _ping_loop(self):
        """Pusher ping mesajı gönderir (120s arayla)."""
        try:
            while self._connected and self._ws:
                await asyncio.sleep(120)
                if self._ws and self._connected:
                    await self._ws.send_json({"event": "pusher:ping", "data": {}})
                    if self.debug:
                        logger.info("[KickModLog] Ping gönderildi")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.debug:
                logger.error(f"[KickModLog] Ping hatası: {e}")

    def _handle_message(self, raw_msg: dict):
        """Gelen WebSocket mesajını işler."""
        try:
            event_name = raw_msg.get("event", "")

            # Pusher iç event'leri atla
            if event_name.startswith("pusher:"):
                if self.debug:
                    logger.debug(f"[KickModLog] Pusher iç: {event_name}")
                return

            # pusher_internal atla
            if event_name.startswith("pusher_internal:"):
                if self.debug:
                    logger.debug(f"[KickModLog] Internal: {event_name}")
                return

            # Event adından tip çıkar (App\Events\UserBannedEvent → UserBannedEvent)
            event_type = event_name.split("\\")[-1] if "\\" in event_name else event_name

            data_str = raw_msg.get("data", "{}")
            if isinstance(data_str, str):
                try:
                    parsed_data = json.loads(data_str)
                except (json.JSONDecodeError, TypeError):
                    parsed_data = {}
            else:
                parsed_data = data_str

            if self.debug:
                logger.debug(f"[KickModLog] Event: {event_type}")
                logger.debug(f"[KickModLog] Data: {json.dumps(parsed_data, ensure_ascii=False)}")

            if event_type == "UserBannedEvent":
                asyncio.create_task(self._handle_ban(parsed_data))
            elif event_type == "UserUnbannedEvent":
                asyncio.create_task(self._handle_unban(parsed_data))
            elif event_type == "MessageDeletedEvent":
                asyncio.create_task(self._handle_delete(parsed_data))
            else:
                if self.debug:
                    logger.debug(f"[KickModLog] Bilinmeyen event: {event_type}")

        except Exception as e:
            logger.error(f"[KickModLog] Mesaj işleme hatası: {e}")

    # ──────────────────────────────────────────────────────────
    # event handler'ları
    # ──────────────────────────────────────────────────────────

    async def _handle_ban(self, data: dict):
        """Ban / Timeout event'ini işler ve Discord'a gönderir."""
        try:
            user = data.get("user", {})
            banned_by = data.get("banned_by", {})
            is_permanent = data.get("permanent", True)
            duration = data.get("duration")
            expires_at = data.get("expires_at")
            event_id = data.get("id", "")

            channel = self.client.get_channel(self.discord_channel_id)
            if not channel:
                logger.error(f"[KickModLog] Discord kanal bulunamadı: {self.discord_channel_id}")
                return

            if is_permanent:
                embed = discord.Embed(
                    title="🔨 Kullanıcı Banlandı",
                    color=self.COLOR_BAN,
                    timestamp=discord.utils.utcnow(),
                    url=f"https://kick.com/{self.channel_slug}",
                )
                embed.add_field(name="Banlanan Kullanıcı", value=f"**{user.get('username', 'Bilinmiyor')}**\n(ID: {user.get('id', '?')})", inline=True)
                embed.add_field(name="Banlayan", value=f"**{banned_by.get('username', 'Bilinmiyor')}**\n(ID: {banned_by.get('id', '?')})", inline=True)
                embed.add_field(name="Ban Tipi", value="**Kalıcı Ban**", inline=True)
                embed.set_footer(text=f"Kick ModLog | Ban ID: {event_id}")
            else:
                duration_str = self._format_duration(duration)
                embed = discord.Embed(
                    title="⏱️ Kullanıcı Timeout Aldı",
                    color=self.COLOR_TIMEOUT,
                    timestamp=discord.utils.utcnow(),
                    url=f"https://kick.com/{self.channel_slug}",
                )
                embed.add_field(name="Timeout Alan", value=f"**{user.get('username', 'Bilinmiyor')}**\n(ID: {user.get('id', '?')})", inline=True)
                embed.add_field(name="Timeout Atan", value=f"**{banned_by.get('username', 'Bilinmiyor')}**\n(ID: {banned_by.get('id', '?')})", inline=True)
                embed.add_field(name="Süre", value=f"**{duration_str}** ({duration} dakika)", inline=True)
                if expires_at:
                    try:
                        ts = int(datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp())
                        embed.add_field(name="Sona Erme", value=f"<t:{ts}:R>", inline=True)
                    except Exception:
                        pass
                embed.set_footer(text=f"Kick ModLog | Event ID: {event_id}")

            await channel.send(embed=embed)

            if self.on_ban:
                self.on_ban({
                    "type": "BAN" if is_permanent else "TIMEOUT",
                    "user": user,
                    "banned_by": banned_by,
                    "is_permanent": is_permanent,
                    "duration": duration,
                    "expires_at": expires_at,
                    "id": event_id,
                })

        except Exception as e:
            logger.error(f"[KickModLog] Ban işleme hatası: {e}")

    async def _handle_unban(self, data: dict):
        """Unban / Unmute event'ini işler ve Discord'a gönderir."""
        try:
            user = data.get("user", {})
            unbanned_by = data.get("unbanned_by", {})
            is_permanent = data.get("permanent", True)
            event_id = data.get("id", "")

            channel = self.client.get_channel(self.discord_channel_id)
            if not channel:
                logger.error(f"[KickModLog] Discord kanal bulunamadı: {self.discord_channel_id}")
                return

            if is_permanent:
                embed = discord.Embed(
                    title="✅ Kullanıcı Banı Açıldı",
                    color=self.COLOR_UNBAN,
                    timestamp=discord.utils.utcnow(),
                    url=f"https://kick.com/{self.channel_slug}",
                )
                embed.add_field(name="Banı Açılan", value=f"**{user.get('username', 'Bilinmiyor')}**\n(ID: {user.get('id', '?')})", inline=True)
                embed.add_field(name="Banı Açan", value=f"**{unbanned_by.get('username', 'Bilinmiyor')}**\n(ID: {unbanned_by.get('id', '?')})", inline=True)
                embed.add_field(name="İşlem Tipi", value="**Kalıcı Ban Kaldırıldı**", inline=True)
                embed.set_footer(text=f"Kick ModLog | Event ID: {event_id}")
            else:
                embed = discord.Embed(
                    title="🔓 Kullanıcı Timeout'u Kaldırıldı",
                    color=self.COLOR_UNBAN,
                    timestamp=discord.utils.utcnow(),
                    url=f"https://kick.com/{self.channel_slug}",
                )
                embed.add_field(name="Timeout Kaldırılan", value=f"**{user.get('username', 'Bilinmiyor')}**\n(ID: {user.get('id', '?')})", inline=True)
                embed.add_field(name="Timeout Kaldıran", value=f"**{unbanned_by.get('username', 'Bilinmiyor')}**\n(ID: {unbanned_by.get('id', '?')})", inline=True)
                embed.add_field(name="İşlem Tipi", value="**Timeout Kaldırıldı**", inline=True)
                embed.set_footer(text=f"Kick ModLog | Event ID: {event_id}")

            await channel.send(embed=embed)

            if self.on_unban:
                self.on_unban({
                    "type": "UNBAN" if is_permanent else "UNMUTE",
                    "user": user,
                    "unbanned_by": unbanned_by,
                    "is_permanent": is_permanent,
                    "id": event_id,
                })

        except Exception as e:
            logger.error(f"[KickModLog] Unban işleme hatası: {e}")

    async def _handle_delete(self, data: dict):
        """Mesaj silme event'ini işler ve Discord'a gönderir."""
        try:
            msg_id = data.get("message", {}).get("id") or data.get("id", "")
            ai_moderated = data.get("aiModerated", False)

            channel = self.client.get_channel(self.discord_channel_id)
            if not channel:
                logger.error(f"[KickModLog] Discord kanal bulunamadı: {self.discord_channel_id}")
                return

            embed = discord.Embed(
                title="🤖 Mesaj Otomatik Silindi" if ai_moderated else "🗑️ Mesaj Silindi",
                color=self.COLOR_AUTO_DELETE if ai_moderated else self.COLOR_DELETE,
                timestamp=discord.utils.utcnow(),
                url=f"https://kick.com/{self.channel_slug}",
            )
            embed.add_field(name="Mesaj ID", value=f"`{msg_id}`", inline=True)
            embed.add_field(name="Kanal", value=self.channel_slug, inline=True)
            embed.add_field(name="Sebebi", value="Otomatik Moderasyon" if ai_moderated else "Manuel Silme", inline=True)
            embed.set_footer(text="Kick ModLog")

            await channel.send(embed=embed)

            if self.on_message_delete:
                self.on_message_delete({
                    "message_id": msg_id,
                    "ai_moderated": ai_moderated,
                    "channel_slug": self.channel_slug,
                })

        except Exception as e:
            logger.error(f"[KickModLog] Mesaj silme işleme hatası: {e}")

    # ──────────────────────────────────────────────────────────
    # yardımcı fonksiyonlar
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _format_duration(minutes) -> str:
        if not minutes:
            return "Bilinmiyor"
        if minutes >= 43200:
            return f"{int(minutes // 43200)} gün"
        if minutes >= 1440:
            return f"{int(minutes // 1440)} gün"
        if minutes >= 60:
            h = int(minutes // 60)
            m = int(minutes % 60)
            return f"{h} saat {m} dakika"
        return f"{int(minutes)} dakika"


# discord import (handler'larda kullanılır)
import discord  # noqa: E402
from datetime import datetime  # noqa: E402
