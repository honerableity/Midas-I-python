"""Manual slash command deploy script.

Ported from deploy-commands.js. Usually bot.py auto-syncs on boot, but this
lets you force a redeploy without starting the full bot (e.g. CI, or a
quick fix after editing a command's options).
"""
import asyncio
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

REQUIRED = ['DISCORD_TOKEN', 'CLIENT_ID', 'GUILD_ID']
missing = [k for k in REQUIRED if not os.getenv(k)]
if missing:
    print(f'Missing env var(s): {", ".join(missing)}. Copy .env.example to .env and fill them in.', file=sys.stderr)
    sys.exit(1)

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID'))


def _discover_command_extensions() -> list[str]:
    """Mirrors bot.py's auto-discovery so this script never drifts out of
    sync with what commands/ actually contains. Skips __init__.py and any
    other underscore-prefixed file.
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

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    print(f'Deploying commands as {bot.user}...')
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        names = ', '.join(c.name for c in synced)
        print(f'Deploying {len(synced)} command(s): {names}')
        print(f'Slash commands registered successfully. Discord confirmed {len(synced)} command(s) live.')
    except Exception as err:  # noqa: BLE001
        print(f'Slash command deploy FAILED: {err}')
        await bot.close()
        sys.exit(1)
    await bot.close()


async def main():
    for ext in COMMAND_EXTENSIONS:
        await bot.load_extension(f'commands.{ext}')
    await bot.start(TOKEN)


if __name__ == '__main__':
    asyncio.run(main())
