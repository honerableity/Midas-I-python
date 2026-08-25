"""Reviews channel config: which channel product ratings get posted to, and
who's exempt from the "reviews only" message cleanup in that channel.

New file -- utils/reviews_channel.py
"""
import discord

from utils.firebase import db


async def get_guild_reviews_config(guild_id: str):
    snap = db.collection('guildConfig').document(guild_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    return {
        'reviewsChannelId': data.get('reviewsChannelId'),
        'reviewsModRoleId': data.get('reviewsModRoleId'),
    }


async def save_reviews_channel(guild_id: str, channel_id: str):
    db.collection('guildConfig').document(guild_id).set({'reviewsChannelId': channel_id}, merge=True)


async def save_reviews_mod_role(guild_id: str, role_id: str | None):
    db.collection('guildConfig').document(guild_id).set({'reviewsModRoleId': role_id}, merge=True)


async def get_reviews_channel(guild: discord.Guild, guild_id: str):
    """Resolves the configured reviews channel object, or None if unset /
    no longer exists.
    """
    config = await get_guild_reviews_config(guild_id)
    if not config or not config.get('reviewsChannelId'):
        return None

    channel_id = int(config['reviewsChannelId'])
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
    return channel


def is_exempt_from_reviews_cleanup(member: discord.Member, mod_role_id: str | None) -> bool:
    """True if this member's messages should be left alone in the reviews
    channel -- Administrators, plus an optional configured mod role.
    """
    if member.guild_permissions.administrator:
        return True
    if member.bot:
        return True
    if mod_role_id:
        return any(str(role.id) == str(mod_role_id) for role in member.roles)
    return False
