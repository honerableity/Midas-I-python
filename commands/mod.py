"""/mod command -- moderation actions.

Ported from commands/mod.js.
"""
import datetime
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.moderation import (
    INVALID,
    add_warn,
    add_warn_threshold,
    clear_expiring_actions,
    format_duration,
    get_honeypot_channel,
    get_warn_thresholds,
    is_protected_target,
    parse_duration,
    schedule_expiring_action,
    send_mod_dm,
    set_honeypot_channel,
)

COMMAND_NAME = 'mod'

LOG_SCHEMA = {
    'subcommands': {
        'ban': {'label': 'Mod — Ban', 'fields': ['discordUser', 'duration', 'reason']},
        'kick': {'label': 'Mod — Kick', 'fields': ['discordUser', 'reason']},
        'unban': {'label': 'Mod — Unban', 'fields': ['discordUser']},
        'mute': {'label': 'Mod — Mute', 'fields': ['discordUser', 'duration', 'reason']},
        'vcmute': {'label': 'Mod — VC Mute', 'fields': ['discordUser', 'duration', 'reason']},
        'unmute': {'label': 'Mod — Unmute', 'fields': ['discordUser', 'reason']},
        'unvcmute': {'label': 'Mod — VC Unmute', 'fields': ['discordUser', 'reason']},
        'warn': {'label': 'Mod — Warn', 'fields': ['discordUser', 'reason', 'warnCount']},
        'setwarn': {'label': 'Mod — Set Warn Rule', 'fields': ['warnCount', 'action', 'duration', 'role']},
        'membercount': {'label': 'Mod — Member Count', 'fields': ['memberCount']},
        'honeypot': {'label': 'Mod — Honeypot Set', 'fields': ['channel']},
        'honeypotTrigger': {'label': 'Mod — Honeypot Triggered', 'fields': ['discordUser', 'channel']},
        'purge': {'label': 'Mod — Purge', 'fields': ['channel', 'amount', 'discordUser', 'contains', 'deletedCount']},
    },
}

ACTION_CHOICES = [
    app_commands.Choice(name='ban', value='ban'),
    app_commands.Choice(name='kick', value='kick'),
    app_commands.Choice(name='mute', value='mute'),
    app_commands.Choice(name='role', value='role'),
]


async def _apply_threshold_action(interaction: discord.Interaction, user: discord.User, threshold: dict) -> str:
    guild = interaction.guild
    member = guild.get_member(user.id)
    if member is None:
        try:
            member = await guild.fetch_member(user.id)
        except discord.HTTPException:
            member = None

    guard = is_protected_target(guild, member, user)
    if guard['blocked']:
        return f"Reached {threshold['count']} warns but target is protected ({guard['reason']}) — auto-action skipped."

    action = threshold['action']

    if action == 'ban':
        await send_mod_dm(user, guild_name=guild.name, action='Banned', reason=f"Reached {threshold['count']} warns", duration='permanent')
        try:
            await guild.ban(discord.Object(id=user.id), reason=f"Auto-ban at {threshold['count']} warns")
        except discord.HTTPException:
            return f"Reached {threshold['count']} warns but I can't ban (permissions)."
        return f"Auto-banned for reaching {threshold['count']} warns."

    if action == 'kick':
        if not member:
            return f"Reached {threshold['count']} warns but user already left."
        await send_mod_dm(user, guild_name=guild.name, action='Kicked', reason=f"Reached {threshold['count']} warns")
        try:
            await member.kick(reason=f"Auto-kick at {threshold['count']} warns")
        except discord.HTTPException:
            return f"Reached {threshold['count']} warns but I can't kick (permissions)."
        return f"Auto-kicked for reaching {threshold['count']} warns."

    if action == 'mute':
        if not member:
            return f"Reached {threshold['count']} warns but user is not in server."
        duration_ms = parse_duration(threshold.get('duration')) or 10 * 60 * 1000
        if duration_ms is INVALID:
            duration_ms = 10 * 60 * 1000
        try:
            await member.timeout(discord.utils.utcnow() + datetime.timedelta(milliseconds=duration_ms), reason=f"Auto-mute at {threshold['count']} warns")
        except discord.HTTPException:
            return f"Reached {threshold['count']} warns but I can't mute (permissions)."
        await send_mod_dm(user, guild_name=guild.name, action='Muted', reason=f"Reached {threshold['count']} warns", duration=format_duration(duration_ms))
        return f"Auto-muted for {format_duration(duration_ms)} for reaching {threshold['count']} warns."

    if action == 'role':
        if not member:
            return f"Reached {threshold['count']} warns but user is not in server."
        if not threshold.get('roleId'):
            return f"Reached {threshold['count']} warns but no role configured."
        role = guild.get_role(int(threshold['roleId']))
        if role is None:
            return f"Reached {threshold['count']} warns but configured role no longer exists."
        await member.add_roles(role, reason=f"Auto-role at {threshold['count']} warns")
        return f"Auto-assigned role for reaching {threshold['count']} warns."

    return ''


class ModCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    mod_group = app_commands.Group(name='mod', description='Moderation commands')

    def _has_perm(self, interaction: discord.Interaction) -> bool:
        return bool(interaction.user.guild_permissions.moderate_members)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        if not self._has_perm(interaction):
            await interaction.response.send_message('You need Moderate Members permission to do that.', ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True)
        return True

    async def _guard_purge(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message('You need Manage Messages permission to do that.', ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True)
        return True

    # ---------------- ban ----------------
    @mod_group.command(name='ban', description='Ban a user')
    @app_commands.describe(user='User to ban', duration='e.g. 10m, 2h, 3d, 1w. Leave empty for permanent', reason='Reason')
    async def ban(self, interaction: discord.Interaction, user: discord.User, duration: str | None = None, reason: str | None = None):
        if not await self._guard(interaction):
            return
        try:
            reason = reason or 'No reason provided'
            duration_ms = parse_duration(duration)
            if duration_ms is INVALID:
                return await interaction.followup.send(f'Invalid duration "{duration}". Use formats like 10m, 2h, 3d, 1w, or leave empty for permanent.')

            guild = interaction.guild
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.HTTPException:
                    member = None

            guard = is_protected_target(guild, member, user)
            if guard['blocked']:
                return await interaction.followup.send(f"Can't ban {user} — protected ({guard['reason']}).")

            await send_mod_dm(user, guild_name=guild.name, action='Banned', reason=reason,
                               duration='permanent' if duration_ms is None else format_duration(duration_ms))

            try:
                await guild.ban(discord.Object(id=user.id), reason=f'{reason} (by {interaction.user})')
            except discord.HTTPException:
                return await interaction.followup.send(f"I can't ban {user} — check role hierarchy / my permissions.")

            await clear_expiring_actions(str(interaction.guild_id), str(user.id), 'ban')
            if duration_ms is not None:
                await schedule_expiring_action(str(interaction.guild_id), str(user.id), 'ban', int(time.time() * 1000) + duration_ms, str(interaction.user.id))

            await interaction.followup.send(f"Banned {user} — {'permanent' if duration_ms is None else format_duration(duration_ms)}. Reason: {reason}")

            await log_command_activity(
                interaction, subcommand='ban', success=True,
                fields={'discordUser': user, 'duration': 'permanent' if duration_ms is None else format_duration(duration_ms), 'reason': reason},
            )
        except Exception as err:  # noqa: BLE001
            print(f'[mod] ban failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- kick ----------------
    @mod_group.command(name='kick', description='Kick a user')
    @app_commands.describe(user='User to kick', reason='Reason')
    async def kick(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None):
        if not await self._guard(interaction):
            return
        try:
            reason = reason or 'No reason provided'
            guild = interaction.guild
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.HTTPException:
                    member = None
            if not member:
                return await interaction.followup.send(f'{user} is not in this server.')

            guard = is_protected_target(guild, member, user)
            if guard['blocked']:
                return await interaction.followup.send(f"Can't kick {user} — protected ({guard['reason']}).")

            await send_mod_dm(user, guild_name=guild.name, action='Kicked', reason=reason)

            try:
                await member.kick(reason=f'{reason} (by {interaction.user})')
            except discord.HTTPException:
                return await interaction.followup.send(f"I can't kick {user} — check role hierarchy / my permissions.")

            await interaction.followup.send(f'Kicked {user}. Reason: {reason}')
            await log_command_activity(interaction, subcommand='kick', success=True, fields={'discordUser': user, 'reason': reason})
        except Exception as err:  # noqa: BLE001
            print(f'[mod] kick failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- unban ----------------
    @mod_group.command(name='unban', description='Unban a user')
    @app_commands.describe(user='User ID to unban')
    async def unban(self, interaction: discord.Interaction, user: str):
        if not await self._guard(interaction):
            return
        try:
            user_id = user.strip()
            try:
                ban_entry = await interaction.guild.fetch_ban(discord.Object(id=int(user_id)))
            except (discord.HTTPException, ValueError):
                return await interaction.followup.send('That user ID is not banned.')

            await interaction.guild.unban(ban_entry.user, reason=f'Unbanned by {interaction.user}')
            await clear_expiring_actions(str(interaction.guild_id), user_id, 'ban')

            await send_mod_dm(ban_entry.user, guild_name=interaction.guild.name, action='Unbanned', reversal=True)

            await interaction.followup.send(f'Unbanned {ban_entry.user}.')
            await log_command_activity(interaction, subcommand='unban', success=True, fields={'discordUser': ban_entry.user})
        except Exception as err:  # noqa: BLE001
            print(f'[mod] unban failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- mute ----------------
    @mod_group.command(name='mute', description='Timeout (mute) a user')
    @app_commands.describe(user='User to mute', duration='e.g. 10m, 2h, 3d (max 28d)', reason='Reason')
    async def mute(self, interaction: discord.Interaction, user: discord.User, duration: str, reason: str | None = None):
        if not await self._guard(interaction):
            return
        try:
            reason = reason or 'No reason provided'
            duration_ms = parse_duration(duration)
            if not duration_ms or duration_ms is INVALID:
                return await interaction.followup.send(f'Invalid duration "{duration}". Use formats like 10m, 2h, 3d (max 28d). Mute cannot be permanent.')

            max_timeout_ms = 28 * 24 * 60 * 60 * 1000
            if duration_ms > max_timeout_ms:
                return await interaction.followup.send('Discord timeouts cap at 28 days. Use a shorter duration.')

            guild = interaction.guild
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.HTTPException:
                    member = None
            if not member:
                return await interaction.followup.send(f'{user} is not in this server.')

            guard = is_protected_target(guild, member, user)
            if guard['blocked']:
                return await interaction.followup.send(f"Can't mute {user} — protected ({guard['reason']}).")

            try:
                await member.timeout(discord.utils.utcnow() + datetime.timedelta(milliseconds=duration_ms), reason=f'{reason} (by {interaction.user})')
            except discord.HTTPException:
                return await interaction.followup.send(f"I can't timeout {user} — check role hierarchy / my permissions.")

            await send_mod_dm(user, guild_name=guild.name, action='Muted', reason=reason, duration=format_duration(duration_ms))

            await interaction.followup.send(f'Muted {user} for {format_duration(duration_ms)}. Reason: {reason}')
            await log_command_activity(interaction, subcommand='mute', success=True, fields={'discordUser': user, 'duration': format_duration(duration_ms), 'reason': reason})
        except Exception as err:  # noqa: BLE001
            print(f'[mod] mute failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- vcmute ----------------
    @mod_group.command(name='vcmute', description='Voice server-mute a user')
    @app_commands.describe(user='User to voice-mute', duration='e.g. 10m, 2h, 3d. Leave empty for indefinite', reason='Reason')
    async def vcmute(self, interaction: discord.Interaction, user: discord.User, duration: str | None = None, reason: str | None = None):
        if not await self._guard(interaction):
            return
        try:
            reason = reason or 'No reason provided'
            duration_ms = parse_duration(duration)
            if duration_ms is INVALID:
                return await interaction.followup.send(f'Invalid duration "{duration}". Use formats like 10m, 2h, 3d, or leave empty for indefinite.')

            guild = interaction.guild
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.HTTPException:
                    member = None
            if not member:
                return await interaction.followup.send(f'{user} is not in this server.')
            if not member.voice or not member.voice.channel:
                return await interaction.followup.send(f'{user} is not currently in a voice channel.')

            guard = is_protected_target(guild, member, user)
            if guard['blocked']:
                return await interaction.followup.send(f"Can't voice-mute {user} — protected ({guard['reason']}).")

            try:
                await member.edit(mute=True, reason=f'{reason} (by {interaction.user})')
            except discord.HTTPException:
                return await interaction.followup.send(f"I can't voice-mute {user} — check role hierarchy / my permissions.")

            await clear_expiring_actions(str(interaction.guild_id), str(user.id), 'vcmute')
            if duration_ms is not None:
                await schedule_expiring_action(str(interaction.guild_id), str(user.id), 'vcmute', int(time.time() * 1000) + duration_ms, str(interaction.user.id))

            await send_mod_dm(user, guild_name=guild.name, action='Voice-muted', reason=reason,
                               duration='indefinite' if duration_ms is None else format_duration(duration_ms))

            await interaction.followup.send(
                f"Voice-muted {user} — {'indefinite' if duration_ms is None else format_duration(duration_ms)}. Reason: {reason}"
            )
            await log_command_activity(
                interaction, subcommand='vcmute', success=True,
                fields={'discordUser': user, 'duration': 'indefinite' if duration_ms is None else format_duration(duration_ms), 'reason': reason},
            )
        except Exception as err:  # noqa: BLE001
            print(f'[mod] vcmute failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- unmute ----------------
    @mod_group.command(name='unmute', description='Remove an active timeout from a user')
    @app_commands.describe(user='User to unmute', reason='Reason')
    async def unmute(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None):
        if not await self._guard(interaction):
            return
        try:
            reason = reason or 'No reason provided'
            guild = interaction.guild
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.HTTPException:
                    member = None
            if not member:
                return await interaction.followup.send(f'{user} is not in this server.')

            if not member.timed_out_until or member.timed_out_until < discord.utils.utcnow():
                return await interaction.followup.send(f'{user} is not currently muted.')

            try:
                await member.timeout(None, reason=f'{reason} (by {interaction.user})')
            except discord.HTTPException:
                return await interaction.followup.send(f"I can't unmute {user} — check role hierarchy / my permissions.")

            await send_mod_dm(user, guild_name=guild.name, action='Unmuted', reason=reason, reversal=True)

            await interaction.followup.send(f'Unmuted {user}. Reason: {reason}')
            await log_command_activity(interaction, subcommand='unmute', success=True, fields={'discordUser': user, 'reason': reason})
        except Exception as err:  # noqa: BLE001
            print(f'[mod] unmute failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- unvcmute ----------------
    @mod_group.command(name='unvcmute', description='Remove voice server-mute from a user')
    @app_commands.describe(user='User to unmute', reason='Reason')
    async def unvcmute(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None):
        if not await self._guard(interaction):
            return
        try:
            reason = reason or 'No reason provided'
            guild = interaction.guild
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.HTTPException:
                    member = None
            if not member:
                return await interaction.followup.send(f'{user} is not in this server.')
            if not member.voice or not member.voice.mute:
                return await interaction.followup.send(f'{user} is not currently voice-muted.')

            try:
                await member.edit(mute=False, reason=f'{reason} (by {interaction.user})')
            except discord.HTTPException:
                return await interaction.followup.send(f"I can't unmute {user} — check role hierarchy / my permissions.")

            await clear_expiring_actions(str(interaction.guild_id), str(user.id), 'vcmute')

            await send_mod_dm(user, guild_name=guild.name, action='Voice-unmuted', reason=reason, reversal=True)

            await interaction.followup.send(f'Voice-unmuted {user}. Reason: {reason}')
            await log_command_activity(interaction, subcommand='unvcmute', success=True, fields={'discordUser': user, 'reason': reason})
        except Exception as err:  # noqa: BLE001
            print(f'[mod] unvcmute failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- warn ----------------
    @mod_group.command(name='warn', description='Warn a user (DMs them)')
    @app_commands.describe(user='User to warn', reason='Reason')
    async def warn(self, interaction: discord.Interaction, user: discord.User, reason: str | None = None):
        if not await self._guard(interaction):
            return
        try:
            reason = reason or 'No reason provided'
            guild = interaction.guild
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.HTTPException:
                    member = None

            guard = is_protected_target(guild, member, user)
            if guard['blocked']:
                return await interaction.followup.send(f"Can't warn {user} — protected ({guard['reason']}).")

            data = await add_warn(str(interaction.guild_id), str(user.id), str(interaction.user.id), reason)
            count = data['count']

            dm_embed = discord.Embed(
                title='You received a warning',
                description=f'Server: **{guild.name}**\nReason: {reason}\nTotal warns: {count}',
                color=0xFFAA00,
            )
            try:
                await user.send(embed=dm_embed)
            except discord.HTTPException:
                pass

            thresholds = await get_warn_thresholds(str(interaction.guild_id))
            matched = next((t for t in thresholds if t['count'] == count), None)
            action_note = ''

            if matched:
                try:
                    action_note = await _apply_threshold_action(interaction, user, matched)
                except Exception as err:  # noqa: BLE001
                    print(f'[mod] threshold action failed: {err}')
                    action_note = 'Threshold action failed — check bot permissions.'

            await interaction.followup.send(f'Warned {user}. Total warns: {count}.{chr(10) + action_note if action_note else ""}')

            await log_command_activity(
                interaction, subcommand='warn', success=True,
                fields={'discordUser': user, 'reason': reason, 'warnCount': count},
            )
        except Exception as err:  # noqa: BLE001
            print(f'[mod] warn failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- setwarn ----------------
    @mod_group.command(name='setwarn', description='Set an action that fires at a warn count threshold')
    @app_commands.describe(warncount='Warn count that triggers this', action='Action to take',
                            duration='For mute action: e.g. 10m, 2h, 3d', role='For role action: role to give')
    @app_commands.choices(action=ACTION_CHOICES)
    async def setwarn(self, interaction: discord.Interaction, warncount: app_commands.Range[int, 1, None],
                       action: app_commands.Choice[str], duration: str | None = None, role: discord.Role | None = None):
        if not await self._guard(interaction):
            return
        try:
            action_value = action.value

            if action_value == 'mute':
                duration_ms = parse_duration(duration)
                if not duration_ms or duration_ms is INVALID:
                    return await interaction.followup.send('Action "mute" needs a valid duration, e.g. 10m, 2h, 3d.')

            if action_value == 'role' and not role:
                return await interaction.followup.send('Action "role" needs a role option.')

            threshold = {
                'count': warncount,
                'action': action_value,
                'duration': duration if action_value == 'mute' else None,
                'roleId': str(role.id) if action_value == 'role' else None,
            }

            await add_warn_threshold(str(interaction.guild_id), threshold)

            desc = (
                f'mute for {duration}' if action_value == 'mute'
                else f'give role {role.name}' if action_value == 'role'
                else action_value
            )

            await interaction.followup.send(f'Set: at {warncount} warns -> {desc}.')

            await log_command_activity(
                interaction, subcommand='setwarn', success=True,
                fields={'warnCount': warncount, 'action': action_value, 'duration': duration, 'role': role.name if role else None},
            )
        except Exception as err:  # noqa: BLE001
            print(f'[mod] setwarn failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- membercount ----------------
    @mod_group.command(name='membercount', description='Show the server member count')
    async def membercount(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        try:
            guild = interaction.guild
            total = guild.member_count
            humans = sum(1 for m in guild.members if not m.bot)
            bots = sum(1 for m in guild.members if m.bot)

            await interaction.followup.send(f'**{guild.name}** has **{total}** members ({humans} humans, {bots} bots).')
            await log_command_activity(interaction, subcommand='membercount', success=True, fields={'memberCount': total})
        except Exception as err:  # noqa: BLE001
            print(f'[mod] membercount failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- honeypot ----------------
    @mod_group.command(name='honeypot', description='Set a channel that instant-bans anyone who types in it')
    @app_commands.describe(channel='Trap channel')
    async def honeypot(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await self._guard(interaction):
            return
        try:
            await set_honeypot_channel(str(interaction.guild_id), str(channel.id))

            await interaction.followup.send(
                f'Honeypot set to {channel.mention}. Anyone who sends a message there gets banned (7 days) '
                f'and their messages purged server-wide.'
            )
            await log_command_activity(interaction, subcommand='honeypot', success=True, fields={'channel': channel.name})
        except Exception as err:  # noqa: BLE001
            print(f'[mod] honeypot failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')

    # ---------------- purge ----------------
    @mod_group.command(name='purge', description='Bulk-delete recent messages in this channel')
    @app_commands.describe(
        amount='How many messages to delete (1-100)',
        user='Only delete messages from this user',
        contains='Only delete messages containing this text',
    )
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100],
                     user: discord.User | None = None, contains: str | None = None):
        if not await self._guard_purge(interaction):
            return
        try:
            channel = interaction.channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return await interaction.followup.send("Purge only works in text channels/threads.")

            perms = channel.permissions_for(interaction.guild.me)
            if not perms.manage_messages or not perms.read_message_history:
                return await interaction.followup.send("I need Manage Messages and Read Message History here.")

            cutoff = discord.utils.utcnow() - datetime.timedelta(days=14)
            contains_lower = contains.lower() if contains else None

            def _check(m: discord.Message) -> bool:
                if m.created_at < cutoff:
                    return False
                if user is not None and m.author.id != user.id:
                    return False
                if contains_lower is not None and contains_lower not in m.content.lower():
                    return False
                return True

            deleted = await channel.purge(limit=amount, check=_check, bulk=True)
            deleted_count = len(deleted)

            desc_bits = []
            if user is not None:
                desc_bits.append(f'from {user}')
            if contains is not None:
                desc_bits.append(f'containing "{contains}"')
            filter_desc = f" ({', '.join(desc_bits)})" if desc_bits else ''

            await interaction.followup.send(f'Purged {deleted_count} message(s){filter_desc}.')

            await log_command_activity(
                interaction, subcommand='purge', success=True,
                fields={
                    'channel': channel.name,
                    'amount': amount,
                    'discordUser': user,
                    'contains': contains,
                    'deletedCount': deleted_count,
                },
            )
        except discord.HTTPException as err:
            print(f'[mod] purge failed: {err}')
            await interaction.followup.send("Couldn't purge — messages may be older than 14 days, or I lack permissions.")
        except Exception as err:  # noqa: BLE001
            print(f'[mod] purge failed: {err}')
            await interaction.followup.send('Bot error occurred while running that command.')


# ---------------------------------------------------------------------------
# honeypot trigger -- called from bot's on_message listener (see bot.py)
# ---------------------------------------------------------------------------
class _FakeInteraction:
    """Minimal stand-in so log_command_activity can log a honeypot trigger,
    which isn't attached to a real slash-command interaction.
    """
    def __init__(self, guild: discord.Guild):
        self.guild_id = guild.id
        self.guild = guild
        self.client = guild._state._get_client() if hasattr(guild._state, '_get_client') else None
        self.command = None
        self.command_name = 'mod'
        self.user = guild.me


async def handle_honeypot_message(message: discord.Message):
    if message.guild is None or message.author.bot:
        return

    try:
        honeypot_id = await get_honeypot_channel(str(message.guild.id))
    except Exception:  # noqa: BLE001
        honeypot_id = None
    if not honeypot_id or str(message.channel.id) != str(honeypot_id):
        return

    guild = message.guild
    user_id = message.author.id

    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            member = None

    guard = is_protected_target(guild, member, message.author)
    if guard['blocked']:
        print(f"[mod] honeypot triggered by {message.author} but target is protected ({guard['reason']}) — ignoring entirely.")
        return

    try:
        await message.delete()
    except discord.HTTPException:
        pass

    # Purge all their messages server-wide: scan text channels for recent
    # messages from this user and bulk-delete.
    for channel in guild.text_channels:
        perms = channel.permissions_for(guild.me)
        if not perms.manage_messages or not perms.read_message_history:
            continue
        try:
            theirs = [m async for m in channel.history(limit=100) if m.author.id == user_id]
            if len(theirs) == 1:
                await theirs[0].delete()
            elif len(theirs) > 1:
                await channel.delete_messages(theirs)
        except discord.HTTPException as err:
            print(f'[mod] honeypot purge failed in #{channel.name}: {err}')

    if member and not (guild.me.guild_permissions.ban_members):
        print(f'[mod] honeypot triggered by {message.author} but bot cannot ban.')
        return

    await send_mod_dm(message.author, guild_name=guild.name, action='Banned', reason='Honeypot channel triggered',
                       duration=format_duration(7 * 24 * 60 * 60 * 1000))

    try:
        await guild.ban(message.author, reason='Honeypot channel triggered', delete_message_seconds=7 * 24 * 60 * 60)
    except discord.HTTPException as err:
        print(f'[mod] honeypot ban failed: {err}')
        return

    await schedule_expiring_action(str(guild.id), str(user_id), 'ban', int(time.time() * 1000) + 7 * 24 * 60 * 60 * 1000, str(guild.me.id))

    try:
        fake_interaction = _FakeInteraction(guild)
        await log_command_activity(
            fake_interaction, subcommand='honeypotTrigger', success=True,
            fields={'discordUser': message.author, 'channel': message.channel.name},
        )
    except Exception:  # noqa: BLE001
        pass


async def setup(bot: commands.Bot):
    await bot.add_cog(ModCog(bot))
