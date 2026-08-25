"""/sticky command -- keep a message stuck to the bottom of a channel
(classic StickyBot-style), and enforce the "reviews only" cleanup in the
guild's configured product-reviews channel.

New file -- commands/sticky.py
"""
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils.firebase import db
from utils.logger import log_command_activity
from utils.reviews_channel import get_guild_reviews_config, is_exempt_from_reviews_cleanup

COMMAND_NAME = 'sticky'

LOG_SCHEMA = {
    'subcommands': {
        'stick': {'label': 'Sticky — Started', 'fields': ['discordUser', 'channel', 'messageId']},
        'unstick': {'label': 'Sticky — Stopped', 'fields': ['discordUser', 'channel']},
    },
}


def _require_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)


async def _admin_denied(interaction: discord.Interaction):
    await interaction.response.send_message('You need **Administrator** permission to do that.', ephemeral=True)


# ---------------------------------------------------------------------------
# Firestore-backed sticky config, one doc per channel.
# ---------------------------------------------------------------------------
def _sticky_doc_id(channel_id: str) -> str:
    return str(channel_id)


async def get_sticky(channel_id: str):
    doc = db.collection('stickyMessages').document(_sticky_doc_id(channel_id)).get()
    if not doc.exists:
        return None
    return doc.to_dict()


async def save_sticky(channel_id: str, *, guild_id: str, content: str | None, embed_dict: dict | None, last_message_id: str | None):
    db.collection('stickyMessages').document(_sticky_doc_id(channel_id)).set({
        'guildId': guild_id,
        'channelId': channel_id,
        'content': content,
        'embed': embed_dict,
        'lastMessageId': last_message_id,
    })


async def update_sticky_last_message(channel_id: str, last_message_id: str | None):
    db.collection('stickyMessages').document(_sticky_doc_id(channel_id)).set({'lastMessageId': last_message_id}, merge=True)


async def delete_sticky(channel_id: str):
    db.collection('stickyMessages').document(_sticky_doc_id(channel_id)).delete()


class StickyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-channel lock so rapid-fire messages don't race to repost the
        # sticky multiple times.
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    sticky_group = app_commands.Group(name='sticky', description='Keep a message stuck to the bottom of a channel')

    async def _guild_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        return True

    @sticky_group.command(name='stick', description='Stick a message to the bottom of this channel')
    @app_commands.describe(messageid='ID of the message to stick (must be in this channel)')
    async def stick(self, interaction: discord.Interaction, messageid: str):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        messageid = messageid.strip()
        if not messageid.isdigit():
            return await interaction.followup.send('Message ID harus berupa angka. Klik kanan pesan -> Copy Message ID (aktifkan Developer Mode dulu kalau belum).')

        channel = interaction.channel
        try:
            source_message = await channel.fetch_message(int(messageid))
        except discord.NotFound:
            return await interaction.followup.send('Pesan dengan ID itu tidak ditemukan di channel ini.')
        except discord.HTTPException as err:
            print(f'Failed to fetch message for /sticky stick: {err}')
            return await interaction.followup.send('Bot error saat mengambil pesan. Coba lagi.')

        content = source_message.content or None
        embed_dict = source_message.embeds[0].to_dict() if source_message.embeds else None

        if not content and not embed_dict:
            return await interaction.followup.send('Pesan itu tidak punya teks maupun embed yang bisa di-stick.')

        try:
            posted = await channel.send(content=content, embed=discord.Embed.from_dict(embed_dict) if embed_dict else None)
        except discord.HTTPException as err:
            print(f'Failed to post sticky message: {err}')
            return await interaction.followup.send('Bot error saat mengirim pesan sticky. Cek permission bot di channel ini.')

        try:
            await save_sticky(
                str(channel.id), guild_id=str(interaction.guild_id),
                content=content, embed_dict=embed_dict, last_message_id=str(posted.id),
            )
        except Exception as err:  # noqa: BLE001
            print(f'Failed to save sticky config to Firestore: {err}')
            await log_command_activity(
                interaction, subcommand='stick', success=False,
                fields={'discordUser': interaction.user, 'channel': channel, 'messageId': messageid}, note='Firestore write failed.',
            )
            return await interaction.followup.send('Pesan sudah diposting, tapi gagal menyimpan config sticky ke database. Jalankan `/sticky stick` lagi.')

        await log_command_activity(
            interaction, subcommand='stick', success=True,
            fields={'discordUser': interaction.user, 'channel': channel, 'messageId': messageid},
        )

        await interaction.followup.send(f'Pesan di-stick di {channel.mention}. Bot akan otomatis memindahkannya ke bawah setiap ada pesan baru.')

    @sticky_group.command(name='unstick', description='Stop stickying a message in this channel')
    async def unstick(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return
        if not _require_admin(interaction):
            return await _admin_denied(interaction)

        await interaction.response.defer(ephemeral=True)

        channel = interaction.channel
        existing = await get_sticky(str(channel.id))
        if not existing:
            return await interaction.followup.send('Channel ini tidak punya sticky message yang aktif.')

        last_message_id = existing.get('lastMessageId')
        if last_message_id:
            try:
                msg = await channel.fetch_message(int(last_message_id))
                await msg.delete()
            except discord.HTTPException as err:
                print(f'Failed to delete last sticky message (continuing anyway): {err}')

        try:
            await delete_sticky(str(channel.id))
        except Exception as err:  # noqa: BLE001
            print(f'Failed to delete sticky config from Firestore: {err}')
            await log_command_activity(
                interaction, subcommand='unstick', success=False,
                fields={'discordUser': interaction.user, 'channel': channel}, note='Firestore delete failed.',
            )
            return await interaction.followup.send('Gagal menghapus config sticky dari database. Coba lagi.')

        await log_command_activity(
            interaction, subcommand='unstick', success=True,
            fields={'discordUser': interaction.user, 'channel': channel},
        )

        await interaction.followup.send(f'Sticky message di {channel.mention} sudah dihentikan.')

    # -----------------------------------------------------------------
    # Listener: handles BOTH resticking and reviews-channel cleanup.
    # These are split into two independent checks -- a channel could be
    # sticky, reviews-only, both, or neither.
    # -----------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.id == self.bot.user.id:
            return

        await self._handle_reviews_cleanup(message)
        await self._handle_restick(message)

    async def _handle_reviews_cleanup(self, message: discord.Message):
        """Deletes any message in the configured reviews channel that isn't
        from the bot or an exempt admin/mod-role member, and DMs the author
        why. /product rating itself doesn't produce a message (it's a slash
        command + modal), so this only ever catches ordinary chat.
        """
        try:
            config = await get_guild_reviews_config(str(message.guild.id))
        except Exception as err:  # noqa: BLE001
            print(f'Failed to load reviews channel config: {err}')
            return

        if not config or not config.get('reviewsChannelId'):
            return
        if str(message.channel.id) != str(config['reviewsChannelId']):
            return

        member = message.author
        if not isinstance(member, discord.Member):
            return  # shouldn't happen for guild messages, but guard anyway

        if is_exempt_from_reviews_cleanup(member, config.get('reviewsModRoleId')):
            return

        try:
            await message.delete()
        except discord.HTTPException as err:
            print(f'Failed to delete non-review message in reviews channel: {err}')
            return

        try:
            await member.send(
                f"Pesan kamu di **#{message.channel.name}** dihapus otomatis -- channel itu khusus untuk review produk "
                f"(`/product rating`). Chat biasa tidak diperbolehkan di sana."
            )
        except discord.HTTPException:
            # DMs closed -- nothing more we can do.
            pass

    async def _handle_restick(self, message: discord.Message):
        try:
            sticky = await get_sticky(str(message.channel.id))
        except Exception as err:  # noqa: BLE001
            print(f'Failed to load sticky config: {err}')
            return

        if not sticky:
            return

        # If the new message IS the current sticky (e.g. some race), skip.
        if str(message.id) == str(sticky.get('lastMessageId')):
            return

        async with self._lock_for(message.channel.id):
            # Re-fetch inside the lock in case another task already resticked.
            try:
                sticky = await get_sticky(str(message.channel.id))
            except Exception as err:  # noqa: BLE001
                print(f'Failed to reload sticky config inside lock: {err}')
                return
            if not sticky:
                return

            # If the most recent message in the channel is already the
            # sticky, nothing to do (avoids reposting on every message when
            # multiple messages arrive between fetch and lock acquisition).
            last_message_id = sticky.get('lastMessageId')

            old_message = None
            if last_message_id:
                try:
                    old_message = await message.channel.fetch_message(int(last_message_id))
                except discord.HTTPException:
                    old_message = None

            # Nothing changed since the sticky was last posted right before
            # this message -- i.e. the sticky is still the second-to-last
            # message and this is the only new one. We always restick on any
            # new message per spec, so just delete-and-repost.
            embed_dict = sticky.get('embed')
            content = sticky.get('content')

            if old_message is not None:
                try:
                    await old_message.delete()
                except discord.HTTPException as err:
                    print(f'Failed to delete old sticky message (continuing anyway): {err}')

            try:
                reposted = await message.channel.send(
                    content=content,
                    embed=discord.Embed.from_dict(embed_dict) if embed_dict else None,
                )
            except discord.HTTPException as err:
                print(f'Failed to repost sticky message: {err}')
                return

            try:
                await update_sticky_last_message(str(message.channel.id), str(reposted.id))
            except Exception as err:  # noqa: BLE001
                print(f'Failed to update sticky lastMessageId (message reposted anyway): {err}')


async def setup(bot: commands.Bot):
    await bot.add_cog(StickyCog(bot))
