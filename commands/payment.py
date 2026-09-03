"""/payment command -- Tokoshopp payment utilities.

Tokoshopp's documented API exposes payment creation and payment-status
endpoints, but no account-balance endpoint. The existing balance command
is kept as a compatibility command and reports that limitation instead
of calling a non-existent legacy endpoint.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.payments import pakasir_configured

COMMAND_NAME = 'payment'

LOG_SCHEMA = {
    'subcommands': {
        'balance': {'label': 'Payment — Balance Checked', 'fields': ['discordUser']},
    },
}


def _require_server_owner(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.guild.owner_id == interaction.user.id


def _format_idr_local(n) -> str:
    return f'Rp{int(n):,}'.replace(',', '.')


class PaymentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    payment_group = app_commands.Group(name='payment', description='Tokoshopp payment utilities')

    async def _guild_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        return True

    @payment_group.command(name='balance', description='[Owner only] Check Tokoshopp account balance availability')
    async def balance(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return
        if not _require_server_owner(interaction):
            return await interaction.response.send_message(
                'Command ini cuma bisa dipakai oleh pemilik server.', ephemeral=True
            )

        if not pakasir_configured():
            return await interaction.response.send_message(
                'TOKOSHOPP_API_KEY belum di-set di .env.', ephemeral=True
            )

        await log_command_activity(
            interaction, subcommand='balance', success=True,
            fields={'discordUser': interaction.user},
            note='Tokoshopp API docs do not expose an account-balance endpoint.',
        )
        await interaction.response.send_message(
            'Tokoshopp API tidak menyediakan endpoint saldo akun di dokumentasi saat ini. '
            'Payment QRIS tetap memakai `/api/payment/create` dan `/api/payment/status`.',
            ephemeral=True,
        )



async def setup(bot: commands.Bot):
    await bot.add_cog(PaymentCog(bot))
