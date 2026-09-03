"""Moderation helpers: durations, warns, honeypot config, expiring actions.

Ported from utils/moderation.js.
"""
import asyncio
import re
import time

import discord
from google.cloud.firestore_v1 import ArrayUnion

from utils.firebase import db

_DURATION_RE = re.compile(
    r'^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$'
)

# Sentinel used the same way JS uses `undefined` here: "invalid input, caller
# must distinguish from permanent (None)".
INVALID = object()


def _now_ms():
    return int(time.time() * 1000)


def parse_duration(raw):
    """Accepts "10m", "2h", "3d", "1w", or "permanent"/"none"/empty -> None
    (permanent). Returns milliseconds, or None for permanent, or INVALID.
    """
    if not raw:
        return None
    s = str(raw).strip().lower()
    if s in ('permanent', 'perm', 'none', ''):
        return None

    match = _DURATION_RE.match(s)
    if not match:
        return INVALID

    amount = int(match.group(1))
    unit = match.group(2)

    if unit.startswith('m'):
        return amount * 60 * 1000
    if unit.startswith('h'):
        return amount * 60 * 60 * 1000
    if unit.startswith('d'):
        return amount * 24 * 60 * 60 * 1000
    if unit.startswith('w'):
        return amount * 7 * 24 * 60 * 60 * 1000
    return INVALID


def format_duration(ms):
    if ms is None:
        return 'permanent'
    mins = round(ms / 60000)
    if mins < 60:
        return f'{mins}m'
    hours = round(mins / 60)
    if hours < 24:
        return f'{hours}h'
    days = round(hours / 24)
    if days < 7:
        return f'{days}d'
    weeks = round(days / 7)
    return f'{weeks}w'


def is_protected_target(guild: discord.Guild, member: discord.Member | None, user: discord.User):
    """Blocks mod actions against: server owner, any bot account, anyone with
    Manage Server permission.
    """
    target_user = member._user if member and hasattr(member, '_user') else (member or user)
    target_id = member.id if member else user.id
    is_bot = (member.bot if member else user.bot)

    if target_id == guild.owner_id:
        return {'blocked': True, 'reason': 'server owner'}
    if is_bot:
        return {'blocked': True, 'reason': 'bot account'}
    if member is not None and member.guild_permissions.manage_guild:
        return {'blocked': True, 'reason': 'has Manage Server permission'}
    return {'blocked': False}


async def send_mod_dm(user: discord.User, *, guild_name, action, reason=None, duration=None, reversal=False):
    """Best-effort DM to the target. Never raises. No moderator name/tag is
    included by design (privacy).
    """
    embed = discord.Embed(
        title=f'Action reversed: {action}' if reversal else f'Moderation action: {action}',
        color=0x57F287 if reversal else 0xED4245,
    )
    embed.add_field(name='Server', value=guild_name, inline=False)
    if reason:
        embed.add_field(name='Reason', value=reason, inline=False)
    if duration is not None:
        embed.add_field(name='Duration', value=duration, inline=False)

    try:
        await user.send(embed=embed)
    except discord.HTTPException:
        pass


async def get_warn_thresholds(guild_id: str):
    snap = db.collection('guildConfig').document(guild_id).get()
    return (snap.to_dict().get('warnThresholds') or []) if snap.exists else []


async def add_warn_threshold(guild_id: str, threshold: dict):
    ref = db.collection('guildConfig').document(guild_id)
    snap = ref.get()
    existing = (snap.to_dict().get('warnThresholds') or []) if snap.exists else []

    filtered = [t for t in existing if t['count'] != threshold['count']]
    filtered.append(threshold)
    filtered.sort(key=lambda t: t['count'])

    ref.set({'warnThresholds': filtered}, merge=True)
    return filtered


def _warn_doc_id(guild_id: str, user_id: str) -> str:
    return f'{guild_id}_{user_id}'


async def add_warn(guild_id: str, user_id: str, moderator_id: str, reason: str | None):
    ref = db.collection('warns').document(_warn_doc_id(guild_id, user_id))
    entry = {'moderatorId': moderator_id, 'reason': reason or 'No reason provided', 'timestamp': _now_ms()}

    snap = ref.get()
    current_count = (snap.to_dict().get('count') or 0) if snap.exists else 0

    ref.set(
        {
            'guildId': guild_id,
            'userId': user_id,
            'count': current_count + 1,
            'history': ArrayUnion([entry]),
        },
        merge=True,
    )

    snap = ref.get()
    return snap.to_dict()


async def get_warn_count(guild_id: str, user_id: str):
    snap = db.collection('warns').document(_warn_doc_id(guild_id, user_id)).get()
    return (snap.to_dict().get('count') or 0) if snap.exists else 0


async def reset_warns(guild_id: str, user_id: str):
    db.collection('warns').document(_warn_doc_id(guild_id, user_id)).set(
        {'guildId': guild_id, 'userId': user_id, 'count': 0, 'history': []}, merge=True
    )


async def set_honeypot_channel(guild_id: str, channel_id: str):
    db.collection('guildConfig').document(guild_id).set({'honeypotChannelId': channel_id}, merge=True)


async def get_honeypot_channel(guild_id: str):
    snap = db.collection('guildConfig').document(guild_id).get()
    return snap.to_dict().get('honeypotChannelId') if snap.exists else None


async def schedule_expiring_action(guild_id: str, user_id: str, action_type: str, expires_at_ms: int | None, moderator_id: str):
    if expires_at_ms is None:
        return  # permanent, nothing to schedule
    db.collection('expiringActions').add({
        'guildId': guild_id,
        'userId': user_id,
        'type': action_type,
        'expiresAt': expires_at_ms,
        'moderatorId': moderator_id,
        'createdAt': _now_ms(),
    })


async def clear_expiring_actions(guild_id: str, user_id: str, action_type: str):
    """Remove any pending expiring actions of a type for a user."""
    query = (
        db.collection('expiringActions')
        .where('guildId', '==', guild_id)
        .where('userId', '==', user_id)
        .where('type', '==', action_type)
    )
    docs = list(query.stream())
    if not docs:
        return
    batch = db.batch()
    for doc in docs:
        batch.delete(doc.reference)
    batch.commit()


async def get_due_expiring_actions(now_ms: int):
    query = db.collection('expiringActions').where('expiresAt', '<=', now_ms)
    return [{'id': doc.id, **doc.to_dict()} for doc in query.stream()]


async def delete_expiring_action(doc_id: str):
    try:
        db.collection('expiringActions').document(doc_id).delete()
    except Exception:  # noqa: BLE001
        pass


async def run_expiry_scan(client: discord.Client):
    """Scans due expiring actions and reverses them (unban / voice-unmute).
    Called on boot and on an interval.
    """
    try:
        due = await get_due_expiring_actions(_now_ms())
    except Exception as err:  # noqa: BLE001
        print(f'[moderation] run_expiry_scan fetch failed: {err}')
        return

    for action in due:
        try:
            guild = client.get_guild(int(action['guildId']))
            if guild is None:
                try:
                    guild = await client.fetch_guild(int(action['guildId']))
                except discord.HTTPException:
                    guild = None

            if guild is None:
                await delete_expiring_action(action['id'])
                continue

            if action['type'] == 'ban':
                try:
                    await guild.unban(discord.Object(id=int(action['userId'])), reason='Temp-ban duration expired')
                except discord.HTTPException:
                    pass
            elif action['type'] == 'vcmute':
                member = guild.get_member(int(action['userId']))
                if member is None:
                    try:
                        member = await guild.fetch_member(int(action['userId']))
                    except discord.HTTPException:
                        member = None
                if member and member.voice and member.voice.channel:
                    try:
                        await member.edit(mute=False, reason='Temp voice-mute duration expired')
                    except discord.HTTPException:
                        pass

            await delete_expiring_action(action['id'])
        except Exception as err:  # noqa: BLE001
            print(f'[moderation] failed to reverse expiring action {action["id"]}: {err}')
            # Leave the doc in place so it retries on the next scan pass.


def start_expiry_scanner(client: discord.Client, interval_seconds: int = 60):
    async def _loop():
        await run_expiry_scan(client)  # run once immediately on boot
        while True:
            await asyncio.sleep(interval_seconds)
            await run_expiry_scan(client)

    client.loop.create_task(_loop())
