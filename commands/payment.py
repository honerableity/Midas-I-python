"""/payment command -- RamaShop account balance.

Split out of commands/product.py so payment-related operations (which
touch the shop's RamaShop account, not a specific product) have their
own namespace. RamaShop has no withdraw endpoint at all -- withdrawals
for this account are handled manually through the RamaShop dashboard --
so /payment only exposes balance.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.payments import (
    PaymentGatewayError,
    get_account_balance,
    pakasir_configured,
)

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

    payment_group = app_commands.Group(name='payment', description='RamaShop account balance')

    async def _guild_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        return True

    @payment_group.command(name='balance', description='[Owner only] Check the current RamaShop account balance')
    async def balance(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return
        if not _require_server_owner(interaction):
            return await interaction.response.send_message(
                'Command ini cuma bisa dipakai oleh pemilik server.', ephemeral=True
            )

        if not pakasir_configured():
            return await interaction.response.send_message(
                'RAMASHOP_API_KEY belum di-set di .env.', ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            balance = await get_account_balance()
        except PaymentGatewayError as err:
            print(f'[payment balance] get_account_balance failed: {err}')
            await log_command_activity(
                interaction, subcommand='balance', success=False,
                fields={'discordUser': interaction.user}, note='RamaShop balance check failed.',
            )
            return await interaction.followup.send('Gagal mengecek saldo RamaShop. Coba lagi sebentar lagi.', ephemeral=True)

        await log_command_activity(
            interaction, subcommand='balance', success=True,
            fields={'discordUser': interaction.user}, note=f'Balance: {balance}.',
        )
        await interaction.followup.send(f'Saldo RamaShop saat ini: **{_format_idr_local(balance)}**', ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PaymentCog(bot))
