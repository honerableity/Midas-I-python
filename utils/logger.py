"""Bot activity logging.

Ported from utils/logger.js.
"""
import importlib
import os
import pkgutil
import time

import discord

from utils.firebase import db

_COMMANDS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'commands')


def _now_ms():
    return int(time.time() * 1000)


def load_command_descriptors():
    """Reads every commands/*.py module fresh each time (not cached at import
    time) so a bot restart after adding a new command file always picks it up
    without extra wiring. Modules that don't export COMMAND_NAME are skipped,
    same as index.py skips them when building the command registry.
    """
    descriptors = []
    for _, mod_name, _ in pkgutil.iter_modules([_COMMANDS_DIR]):
        try:
            mod = importlib.import_module(f'commands.{mod_name}')
        except Exception as err:  # noqa: BLE001
            print(f'[logger] Could not load {mod_name} for log scan: {err}')
            continue

        name = getattr(mod, 'COMMAND_NAME', None)
        if not name:
            continue

        descriptors.append({
            'file': mod_name,
            'name': name,
            'channelName': f'{name}-logs',
            'logSchema': getattr(mod, 'LOG_SCHEMA', None),  # None = command hasn't opted into structured logging yet
        })

    return descriptors


async def get_log_config(guild_id: str):
    snap = db.collection('guildConfig').document(guild_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    return {
        'logCategoryId': data.get('logCategoryId'),
        'logChannels': data.get('logChannels') or {},
    }


async def save_log_category(guild_id: str, category_id: str):
    db.collection('guildConfig').document(guild_id).set({'logCategoryId': category_id}, merge=True)


async def save_log_channel(guild_id: str, command_name: str, channel_id: str):
    db.collection('guildConfig').document(guild_id).set(
        {'logChannels': {command_name: channel_id}}, merge=True
    )


async def resolve_log_category(guild: discord.Guild, category_option: discord.CategoryChannel | None):
    """Creates the log category if `category_option` is None: owner-only
    visibility (@everyone denied ViewChannel; server owners bypass channel
    overwrites entirely, but the bot's own permission overwrite is added
    explicitly so it can still post/manage channels inside). If provided,
    used as-is.
    """
    if category_option:
        return category_option

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, manage_channels=True
        ),
    }

    category = await guild.create_category('Bot Logs', overwrites=overwrites)
    return category


def _format_field_value(value):
    if value is None:
        return 'N/A'
    # Discord.py User/Member/Role/Channel objects all support mention
    if hasattr(value, 'mention'):
        return value.mention
    return str(value)


async def sync_log_channels(guild: discord.Guild, guild_id: str, category_id: int):
    """Scans commands/, creates a `{command}-logs` channel under the log
    category for any command that doesn't already have one on record.
    Existing channels are left untouched.
    """
    descriptors = load_command_descriptors()
    config = await get_log_config(guild_id)
    existing_channels = (config or {}).get('logChannels', {})

    created = []
    skipped_no_schema = []
    already_exists = []

    for desc in descriptors:
        existing_id = existing_channels.get(desc['name'])
        still_valid = existing_id and guild.get_channel(int(existing_id)) is not None

        if still_valid:
            already_exists.append(desc['channelName'])
            continue

        if not desc['logSchema']:
            print(f'[logger] Skipping channel creation for "{desc["name"]}" -- no LOG_SCHEMA exported.')
            skipped_no_schema.append(desc['name'])
            continue

        category = guild.get_channel(category_id)
        channel = await guild.create_text_channel(
            desc['channelName'],
            category=category,
            topic=f'Activity log for /{desc["name"]}',
        )

        await save_log_channel(guild_id, desc['name'], str(channel.id))
        created.append(desc['channelName'])

    return {'created': created, 'alreadyExists': already_exists, 'skippedNoSchema': skipped_no_schema}


async def log_command_activity(interaction, *, subcommand, success, fields=None, note=None):
    """Sends one log entry to the command's log channel, if logging is
    configured for this guild and this specific command has a channel on
    record. Silently no-ops (not raises) when logging isn't set up.
    """
    fields = fields or {}
    try:
        if interaction.guild_id is None:
            return

        config = await get_log_config(str(interaction.guild_id))
        channel_id = (config or {}).get('logChannels', {}).get(interaction.command.name if hasattr(interaction, 'command') and interaction.command else getattr(interaction, 'command_name', None))
        if not channel_id:
            return

        channel = interaction.client.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(int(channel_id))
            except discord.HTTPException:
                return
        if channel is None:
            return

        command_name = interaction.command.name if hasattr(interaction, 'command') and interaction.command else getattr(interaction, 'command_name', None)
        cmd_module = None
        try:
            cmd_module = importlib.import_module(f'commands.{command_name}')
        except Exception:  # noqa: BLE001
            pass

        schema_entry = None
        if cmd_module is not None:
            schema = getattr(cmd_module, 'LOG_SCHEMA', None)
            if schema:
                schema_entry = schema.get('subcommands', {}).get(subcommand)

        label = (schema_entry or {}).get('label', subcommand or command_name)

        embed = discord.Embed(
            title=label,
            color=0x57F287 if success else 0xED4245,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name='Status', value='✅ Success' if success else '❌ Failed', inline=True)
        embed.add_field(name='Run By', value=str(interaction.user.mention), inline=True)

        for key, value in fields.items():
            # discordUser is redundant with "Run By" added above.
            if key == 'discordUser':
                continue
            embed.add_field(name=key, value=_format_field_value(value), inline=True)

        if note:
            embed.add_field(name='Note', value=str(note)[:1024], inline=False)

        await channel.send(embed=embed)
    except Exception as err:  # noqa: BLE001
        # Logging must never break the command it's logging.
        print(f'[logger] log_command_activity failed: {err}')
