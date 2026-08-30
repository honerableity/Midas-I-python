"""/payment command -- ARTAN SHOP account balance and withdrawals.

Split out of commands/product.py so payment/withdrawal operations (which
touch the shop's real ARTAN SHOP account, not a specific product) have
their own namespace: /payment withdraw, /payment balance.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.payments import (
    PaymentGatewayError,
    WITHDRAW_FEES,
    get_account_balance,
    withdraw_balance,
    withdraw_configured,
)

COMMAND_NAME = 'payment'

LOG_SCHEMA = {
    'subcommands': {
        'withdraw': {'label': 'Payment — Withdraw', 'fields': ['discordUser', 'amount']},
        'balance': {'label': 'Payment — Balance Checked', 'fields': ['discordUser']},
    },
}

# Default withdraw destination for /payment withdraw. Kept as a named
# constant instead of repeated inline so it's a one-line change if the
# destination ever needs to move.
WITHDRAW_DESTINATION = {'jenis': 'ewallet', 'bank': 'GOPAY', 'nomor': '082384636491', 'nama': 'HUH'}


def _require_server_owner(interaction: discord.Interaction) -> bool:
    return interaction.guild is not None and interaction.guild.owner_id == interaction.user.id


def _format_idr_local(n) -> str:
    return f'Rp{int(n):,}'.replace(',', '.')


class WithdrawConfirmView(discord.ui.View):
    """One-time confirm/cancel gate in front of an actual withdraw call --
    withdraw_balance() deducts real money the instant it succeeds with no
    undo, so this makes sure /payment withdraw amount always needs an
    explicit second click before anything is sent to ARTAN SHOP.
    """

    def __init__(self, requester_id: int, amount: int):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.amount = amount
        self._used = False

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message('Cuma yang menjalankan command ini yang bisa konfirmasi.', ephemeral=True)
            return False
        if self._used:
            await interaction.response.send_message('Konfirmasi ini sudah dipakai.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Konfirmasi Withdraw', style=discord.ButtonStyle.danger, emoji='✅')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self._used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            await withdraw_balance(
                jenis=WITHDRAW_DESTINATION['jenis'],
                bank=WITHDRAW_DESTINATION['bank'],
                nomor=WITHDRAW_DESTINATION['nomor'],
                nama=WITHDRAW_DESTINATION['nama'],
                jumlah=self.amount,
            )
        except PaymentGatewayError as err:
            print(f'[payment withdraw] withdraw_balance failed: {err}')
            await interaction.followup.send(f'❌ Withdraw gagal: {err}', ephemeral=True)
            return

        await log_command_activity(
            interaction, subcommand='withdraw', success=True,
            fields={'discordUser': interaction.user, 'amount': self.amount},
            note=f"Withdrawn to {WITHDRAW_DESTINATION['bank']} {WITHDRAW_DESTINATION['nomor']}.",
        )
        await interaction.followup.send(
            f"✅ Withdraw {_format_idr_local(self.amount)} ke {WITHDRAW_DESTINATION['bank']} "
            f"{WITHDRAW_DESTINATION['nomor']} berhasil diajukan.",
            ephemeral=True,
        )

    @discord.ui.button(label='Batal', style=discord.ButtonStyle.secondary, emoji='✖️')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self._used = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content='Withdraw dibatalkan.', embed=None, view=self)


class PaymentCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    payment_group = app_commands.Group(name='payment', description='ARTAN SHOP balance and withdrawals')

    async def _guild_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
            return False
        return True

    @payment_group.command(name='balance', description='[Owner only] Check the current ARTAN SHOP account balance')
    async def balance(self, interaction: discord.Interaction):
        if not await self._guild_check(interaction):
            return
        if not _require_server_owner(interaction):
            return await interaction.response.send_message(
                'Command ini cuma bisa dipakai oleh pemilik server.', ephemeral=True
            )

        if not withdraw_configured():
            return await interaction.response.send_message(
                'TOKOSHOPP_USERNAME/TOKOSHOPP_PASSWORD belum di-set di .env.', ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            balance = await get_account_balance()
        except PaymentGatewayError as err:
            print(f'[payment balance] get_account_balance failed: {err}')
            await log_command_activity(
                interaction, subcommand='balance', success=False,
                fields={'discordUser': interaction.user}, note='ARTAN SHOP login/balance check failed.',
            )
            return await interaction.followup.send('Gagal mengecek saldo ARTAN SHOP. Coba lagi sebentar lagi.', ephemeral=True)

        await log_command_activity(
            interaction, subcommand='balance', success=True,
            fields={'discordUser': interaction.user}, note=f'Balance: {balance}.',
        )
        await interaction.followup.send(f'Saldo ARTAN SHOP saat ini: **{_format_idr_local(balance)}**', ephemeral=True)

    @payment_group.command(name='withdraw', description='[Owner only] Withdraw ARTAN SHOP balance to GoPay')
    @app_commands.describe(amount='Nominal yang ditarik dalam Rupiah (minimal Rp3.000)')
    async def withdraw(self, interaction: discord.Interaction, amount: app_commands.Range[int, 3000, None]):
        if not await self._guild_check(interaction):
            return
        if not _require_server_owner(interaction):
            return await interaction.response.send_message(
                'Command ini cuma bisa dipakai oleh pemilik server.', ephemeral=True
            )

        if not withdraw_configured():
            return await interaction.response.send_message(
                'TOKOSHOPP_USERNAME/TOKOSHOPP_PASSWORD belum di-set di .env.', ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            balance = await get_account_balance()
        except PaymentGatewayError as err:
            print(f'[payment withdraw] get_account_balance failed: {err}')
            return await interaction.followup.send('Gagal mengecek saldo ARTAN SHOP. Coba lagi sebentar lagi.', ephemeral=True)

        fee = WITHDRAW_FEES.get(WITHDRAW_DESTINATION['bank'], 500)
        total_deducted = amount + fee

        if total_deducted > balance:
            return await interaction.followup.send(
                f"Saldo tidak cukup. Saldo saat ini: {_format_idr_local(balance)}, "
                f"butuh {_format_idr_local(total_deducted)} (nominal {_format_idr_local(amount)} + fee {_format_idr_local(fee)}).",
                ephemeral=True,
            )

        embed = discord.Embed(title='Konfirmasi Withdraw', color=0xFFA500)
        embed.add_field(name='Nominal', value=_format_idr_local(amount), inline=True)
        embed.add_field(name='Fee', value=_format_idr_local(fee), inline=True)
        embed.add_field(name='Total terpotong', value=_format_idr_local(total_deducted), inline=True)
        embed.add_field(name='Tujuan', value=f"{WITHDRAW_DESTINATION['bank']} — {WITHDRAW_DESTINATION['nomor']}", inline=False)
        embed.add_field(name='Saldo saat ini', value=_format_idr_local(balance), inline=False)
        embed.set_footer(text='Aksi ini tidak bisa dibatalkan setelah dikonfirmasi.')

        view = WithdrawConfirmView(interaction.user.id, amount)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PaymentCog(bot))
