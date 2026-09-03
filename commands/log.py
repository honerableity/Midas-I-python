"""/log command -- configure bot activity logging.

Ported from commands/log.js.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import get_log_config, resolve_log_category, save_log_category, sync_log_channels

COMMAND_NAME = 'log'

# Read by utils/logger.py -- describes each subcommand for log-channel embed
# labeling. Purely metadata; does not trigger logging by itself.
LOG_SCHEMA = {
    'subcommands': {
        # This command's own activity isn't logged -- see note in execute().
    }
}


def _build_summary_message(category: discord.CategoryChannel, created, already_exists, skipped_no_schema):
    lines = [f'Log category: {category.mention}']
    lines.append(f'✅ Created: {", ".join(created)}' if created else '✅ Created: none')
    lines.append(f'↩️ Already existed: {", ".join(already_exists)}' if already_exists else '↩️ Already existed: none')
    if skipped_no_schema:
        lines.append(f'⚠️ Skipped (no LOG_SCHEMA defined yet): {", ".join(skipped_no_schema)}')
    return '\n'.join(lines)


class LogCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    log_group = app_commands.Group(name='log', description='Configure bot activity logging')

    @log_group.command(name='setcategory', description='Set (or create) the category logs go into, and generate log channels')
    @app_commands.describe(category='Existing category to use. Leave empty to create an owner-only category.')
    async def setcategory(self, interaction: discord.Interaction, category: discord.CategoryChannel | None = None):
        if interaction.guild is None:
            return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)

        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message('You need Manage Server permission to do that.', ephemeral=True)

        # Defer immediately, before any Firestore reads or Discord channel-creation
        # calls -- same "Unknown interaction" guard used in verify.py.
        await interaction.response.defer(ephemeral=True)

        try:
            resolved_category = await resolve_log_category(interaction.guild, category)
        except Exception as err:  # noqa: BLE001
            print(f'resolve_log_category failed: {err}')
            return await interaction.followup.send('Bot error while creating the log category. Check my Manage Channels permission.')

        await save_log_category(str(interaction.guild_id), str(resolved_category.id))

        try:
            summary = await sync_log_channels(interaction.guild, str(interaction.guild_id), resolved_category.id)
        except Exception as err:  # noqa: BLE001
            print(f'sync_log_channels failed: {err}')
            return await interaction.followup.send(
                f'Log category set to {resolved_category.mention}, but channel creation failed partway through. Run `/log update` to retry.'
            )

        return await interaction.followup.send(
            _build_summary_message(resolved_category, summary['created'], summary['alreadyExists'], summary['skippedNoSchema'])
        )

    @log_group.command(name='update', description='Scan commands/ and create log channels for any commands missing one')
    async def update(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)

        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message('You need Manage Server permission to do that.', ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        config = await get_log_config(str(interaction.guild_id))
        if not config or not config.get('logCategoryId'):
            return await interaction.followup.send('No log category set yet. Run `/log setcategory` first.')

        category = interaction.guild.get_channel(int(config['logCategoryId']))
        if category is None:
            try:
                category = await interaction.guild.fetch_channel(int(config['logCategoryId']))
            except discord.HTTPException:
                category = None

        if category is None:
            return await interaction.followup.send(
                'The configured log category no longer exists (deleted?). Run `/log setcategory` again to set a new one.'
            )

        try:
            summary = await sync_log_channels(interaction.guild, str(interaction.guild_id), category.id)
        except Exception as err:  # noqa: BLE001
            print(f'sync_log_channels failed: {err}')
            return await interaction.followup.send('Bot error while creating log channels. Check my Manage Channels permission.')

        return await interaction.followup.send(
            _build_summary_message(category, summary['created'], summary['alreadyExists'], summary['skippedNoSchema'])
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LogCog(bot))
