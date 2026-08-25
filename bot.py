"""Main bot entrypoint.

Ported from index.js. Loads env, boots discord.py Client w/ command tree,
loads every commands/*.py as an extension, wires honeypot message listener,
starts expiry scanner on ready.
"""
import asyncio
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = os.getenv('GUILD_ID')

if not TOKEN:
    print('Missing DISCORD_TOKEN in .env. Copy .env.example to .env and fill it in.', file=sys.stderr)
    sys.exit(1)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True


def _discover_command_extensions() -> list[str]:
    """Every commands/*.py file becomes an extension name automatically --
    no more manually keeping a COMMAND_EXTENSIONS list in sync with what's
    actually on disk. Skips __init__.py (package marker, not a cog) and any
    other underscore-prefixed file (e.g. _shared.py helpers meant to be
    imported by other command files, not loaded as a cog themselves).
    """
    commands_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'commands')
    extensions = []
    for filename in sorted(os.listdir(commands_dir)):
        if not filename.endswith('.py'):
            continue
        if filename.startswith('_'):
            continue
        extensions.append(filename[:-len('.py')])
    return extensions


COMMAND_EXTENSIONS = _discover_command_extensions()

# Slash commands that work without being verified yet. Anything else is
# blocked with a friendly message via VerificationGatedTree below.
# "verify setrole" is exempt too, since an admin has to be able to set the
# verified role *before* anyone can verify -- otherwise no one could ever
# bootstrap a fresh server.
UNVERIFIED_ALLOWED = {
    ('verify', 'start'),
    ('verify', 'setrole'),
    ('verify', 'sendpanel'),
}


def _command_path(interaction: discord.Interaction) -> tuple[str, str | None]:
    """Returns (root_command_name, subcommand_name_or_None) for the command
    tied to this interaction, e.g. ('verify', 'start') or ('mod', 'ban').
    """
    command = interaction.command
    if command is None:
        return ('', None)
    if command.parent is not None:
        return (command.parent.name, command.name)
    return (command.name, None)


class VerificationGatedTree(app_commands.CommandTree):
    """CommandTree subclass that blocks every slash command except the ones
    in UNVERIFIED_ALLOWED until the caller has verified their Roblox account.
    interaction_check() is the documented override point for a tree-wide
    gate like this -- it isn't decoratable on a plain CommandTree instance.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.type != discord.InteractionType.application_command:
            return True

        root, sub = _command_path(interaction)
        if (root, sub) in UNVERIFIED_ALLOWED:
            return True

        from utils.verification import get_verified_user
        record = await get_verified_user(str(interaction.user.id))
        if record:
            return True

        await interaction.response.send_message(
            "You need to verify your Roblox account before using this. Run `/verify start` first.",
            ephemeral=True,
        )
        return False


bot = commands.Bot(command_prefix='!', intents=intents, tree_cls=VerificationGatedTree)


@bot.event
async def setup_hook():
    for ext in COMMAND_EXTENSIONS:
        await bot.load_extension(f'commands.{ext}')

    # Always re-sync slash commands on boot -- mirrors index.js's
    # deployCommands() always-redeploy comment: a stale hash file from a
    # prior boot can't be manually cleared on some hosts, so redeploying
    # every boot avoids "unchanged" false positives even after real edits.
    print('Deploying slash commands...')
    try:
        if GUILD_ID:
            guild_obj = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild_obj)
            synced = await bot.tree.sync(guild=guild_obj)
        else:
            synced = await bot.tree.sync()
        print(f'Slash commands registered successfully. Discord confirmed {len(synced)} command(s) live.')
    except Exception as err:  # noqa: BLE001
        print(f'Auto-deploy failed: {err}')
        print('Bot will still start, but slash commands may be out of date. Run `python deploy_commands.py` manually.')


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

    # Reverses temp-bans and temp-vcmutes past their expiry. Runs once
    # immediately (catches anything missed while offline) then every 60s.
    from utils.moderation import start_expiry_scanner
    start_expiry_scanner(bot)


# Honeypot channel watch -- separate from slash-command handling since this
# fires on every message, not on interactions.
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    try:
        from commands.mod import handle_honeypot_message
        await handle_honeypot_message(message)
    except Exception as err:  # noqa: BLE001
        print(f'[mod] honeypot handler failed: {err}')

    await bot.process_commands(message)


@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    command_name = interaction.command.name if interaction.command else 'unknown'
    print(f'Error in /{command_name}: {error}')
    try:
        if interaction.response.is_done():
            await interaction.followup.send('Bot error occurred.', ephemeral=True)
        else:
            await interaction.response.send_message('Bot error occurred.', ephemeral=True)
    except discord.HTTPException as reply_err:
        # Interaction likely expired (>3s) or was already acknowledged elsewhere.
        print(f'Could not send error reply (interaction likely expired): {reply_err}')


def main():
    try:
        bot.run(TOKEN)
    except Exception as err:  # noqa: BLE001
        print(f'Unhandled error: {err}')


if __name__ == '__main__':
    main()
