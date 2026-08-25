"""/verify command -- Roblox account verification.

Ported from commands/verify.js.
"""
import asyncio
import time

import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.products import get_products_by_ids
from utils.verification import (
    EXPIRY_MS,
    clear_session,
    create_session,
    description_contains_code,
    fetch_roblox_description,
    fetch_roblox_profile_details,
    fetch_user_group_ids,
    get_guild_config,
    get_session,
    get_verified_user,
    link_group,
    remove_verified_user,
    save_verified_user,
    set_guild_role,
    unlink_group,
)

COMMAND_NAME = 'verify'

LOG_SCHEMA = {
    'subcommands': {
        'start': {'label': 'Verify — Start', 'fields': ['discordUser']},
        'setrole': {'label': 'Verify — Set Role', 'fields': ['discordUser', 'role']},
        'unverify': {'label': 'Verify — Unverify', 'fields': ['discordUser', 'robloxUsername']},
        'profile': {'label': 'Verify — Profile Lookup', 'fields': ['discordUser', 'targetUser']},
        'verifyComplete': {'label': 'Verify — Completed', 'fields': ['discordUser', 'robloxUsername']},
        'linkgroup': {'label': 'Verify — Group Linked', 'fields': ['discordUser', 'groupId']},
        'unlinkgroup': {'label': 'Verify — Group Unlinked', 'fields': ['discordUser', 'groupId']},
        'sendpanel': {'label': 'Verify — Panel Sent', 'fields': ['discordUser', 'channel']},
    },
}


def _paginate_list_field(embed: discord.Embed, label: str, items: list[str], page: int, page_size: int):
    total_pages = max(1, -(-len(items) // page_size))  # ceil div
    clamped_page = min(page, total_pages - 1)
    start = clamped_page * page_size
    chunk = items[start:start + page_size]
    value = '\n'.join(chunk) if chunk else 'None'
    embed.add_field(name=f'{label} ({len(items)}) — Page {clamped_page + 1}/{total_pages}', value=value, inline=False)
    return embed


# ---------------------------------------------------------------------------
# /verify unverify -- confirm-via-modal flow
# ---------------------------------------------------------------------------
class UnverifyModal(discord.ui.Modal, title='Confirm Unverify'):
    roblox_username = discord.ui.TextInput(
        label='Type your Roblox username to confirm',
        placeholder='Your exact Roblox username',
        style=discord.TextStyle.short,
        required=True,
    )

    def __init__(self, source_interaction: discord.Interaction):
        super().__init__()
        self.source_interaction = source_interaction

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        record = await get_verified_user(str(interaction.user.id))
        if not record:
            return await interaction.followup.send('You are already not verified!')

        typed = self.roblox_username.value.strip()
        if typed != record['robloxUsername']:
            return await interaction.followup.send(
                f"Username didn't match. You typed `{typed}`, expected `{record['robloxUsername']}`. "
                f"Run `/verify unverify` again to retry."
            )

        config = await get_guild_config(str(interaction.guild_id))

        if config and config.get('verifiedRoleId'):
            try:
                guild = interaction.guild
                member = guild.get_member(interaction.user.id) or await guild.fetch_member(interaction.user.id)
                role = guild.get_role(int(config['verifiedRoleId']))
                if role:
                    await member.remove_roles(role)
            except Exception as err:  # noqa: BLE001
                print(f'Role removal failed during unverify: {err}')

        await remove_verified_user(str(interaction.user.id))

        await log_command_activity(
            self.source_interaction,
            subcommand='unverify',
            success=True,
            fields={'discordUser': interaction.user, 'robloxUsername': record['robloxUsername']},
        )

        return await interaction.followup.send('You have been unverified. Your role and verification data have been removed.')


# ---------------------------------------------------------------------------
# /verify profile -- tabbed, paginated profile card
# ---------------------------------------------------------------------------
class ProfileView(discord.ui.View):
    PAGE_SIZE = 10

    def __init__(self, *, owner_id: int, target: discord.User, record: dict, details: dict, owned_products: list[dict]):
        super().__init__(timeout=5 * 60)
        self.owner_id = owner_id
        self.target = target
        self.record = record
        self.details = details
        self.owned_products = owned_products
        self.tab = 'overview'
        self.page = 0
        self.message: discord.Message | None = None
        self._build_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                'Only the person who ran this command can use these buttons.', ephemeral=True
            )
            return False
        return True

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title=f'Roblox Profile — {self.details["username"]}', color=0x00B0F4)
        embed.set_footer(text=f'Discord: {self.target.name}')

        if self.tab == 'overview':
            created_raw = self.details.get('created')
            account_age_days = None
            if created_raw:
                try:
                    import datetime
                    created_dt = datetime.datetime.fromisoformat(created_raw.replace('Z', '+00:00'))
                    now = datetime.datetime.now(datetime.timezone.utc)
                    account_age_days = (now - created_dt).days
                except Exception:  # noqa: BLE001
                    account_age_days = None

            embed.add_field(name='Discord User', value=self.target.mention, inline=True)
            embed.add_field(name='Roblox Username', value=self.details['username'], inline=True)
            embed.add_field(name='Display Name', value=self.details.get('displayName') or self.details['username'], inline=True)
            embed.add_field(
                name='Account Age',
                value='Unknown' if account_age_days is None else f'{account_age_days} days',
                inline=True,
            )
            embed.add_field(name='Verified Badge', value='Yes' if self.details.get('hasVerifiedBadge') else 'No', inline=True)
            embed.add_field(name='Groups', value=str(len(self.details['groups'])), inline=True)
            return embed

        if self.tab == 'groups':
            items = [f'{g["name"]} — {g["role"]}' for g in self.details['groups']]
            return _paginate_list_field(embed, 'Groups', items, self.page, self.PAGE_SIZE)

        if self.tab == 'products':
            items = [f'{p["name"]} — {p["price"]}' for p in self.owned_products]
            return _paginate_list_field(embed, 'Owned Products', items, self.page, self.PAGE_SIZE)

        # account tab
        verified_at = self.record.get('verifiedAt')
        verified_at_str = f'<t:{int(verified_at / 1000)}:F>' if verified_at else 'Unknown'
        embed.add_field(name='Roblox ID', value=str(self.record.get('robloxId')), inline=True)
        embed.add_field(name='Verified At', value=verified_at_str, inline=True)
        embed.add_field(name='Verified Badge', value='Yes' if self.details.get('hasVerifiedBadge') else 'No', inline=True)
        return embed

    def _build_components(self, disabled: bool = False):
        self.clear_items()

        for tab_key, tab_label in (
            ('overview', 'Overview'),
            ('groups', 'Groups'),
            ('products', 'Products'),
            ('account', 'Account'),
        ):
            style = discord.ButtonStyle.primary if self.tab == tab_key else discord.ButtonStyle.secondary
            btn = discord.ui.Button(label=tab_label, style=style, disabled=disabled, custom_id=f'profile_tab_{tab_key}', row=0)

            async def _tab_cb(interaction: discord.Interaction, key=tab_key):
                self.tab = key
                self.page = 0  # reset page on tab switch
                self._build_components()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

            btn.callback = _tab_cb
            self.add_item(btn)

        if self.tab in ('groups', 'products'):
            items = self.details['groups'] if self.tab == 'groups' else self.owned_products
            total_pages = max(1, -(-len(items) // self.PAGE_SIZE))
            if total_pages > 1:
                prev_btn = discord.ui.Button(
                    label='◀ Prev', style=discord.ButtonStyle.secondary,
                    disabled=disabled or self.page == 0, row=1,
                )
                next_btn = discord.ui.Button(
                    label='Next ▶', style=discord.ButtonStyle.secondary,
                    disabled=disabled or self.page >= total_pages - 1, row=1,
                )

                async def _prev_cb(interaction: discord.Interaction):
                    self.page = max(0, self.page - 1)
                    self._build_components()
                    await interaction.response.edit_message(embed=self.build_embed(), view=self)

                async def _next_cb(interaction: discord.Interaction):
                    self.page += 1
                    self._build_components()
                    await interaction.response.edit_message(embed=self.build_embed(), view=self)

                prev_btn.callback = _prev_cb
                next_btn.callback = _next_cb
                self.add_item(prev_btn)
                self.add_item(next_btn)

    async def on_timeout(self):
        self._build_components(disabled=True)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# /verify start -- DM verify button -> modal -> Roblox description check
# ---------------------------------------------------------------------------
class VerifyDMButtonView(discord.ui.View):
    def __init__(self, *, discord_user_id: int, guild_id: int, source_interaction: discord.Interaction):
        super().__init__(timeout=EXPIRY_MS / 1000)
        self.discord_user_id = discord_user_id
        self.guild_id = guild_id
        self.source_interaction = source_interaction
        self.verified = False

    @discord.ui.button(label='Verify!', style=discord.ButtonStyle.success, custom_id='verify_button')
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = VerifyUsernameModal(
            discord_user_id=self.discord_user_id,
            guild_id=self.guild_id,
            source_interaction=self.source_interaction,
            dm_view=self,
        )
        await interaction.response.send_modal(modal)

    async def on_timeout(self):
        if self.verified:
            return  # already handled
        await clear_session(str(self.discord_user_id))
        try:
            user = self.source_interaction.client.get_user(self.discord_user_id)
            if user:
                await user.send('Your verification code expired without completing verification. Run `/verify start` again to get a new code.')
        except discord.HTTPException:
            pass


class VerifyUsernameModal(discord.ui.Modal, title='Roblox Verification'):
    roblox_username = discord.ui.TextInput(label='Your Roblox Username', style=discord.TextStyle.short, required=True)

    def __init__(self, *, discord_user_id: int, guild_id: int, source_interaction: discord.Interaction, dm_view: VerifyDMButtonView):
        super().__init__()
        self.discord_user_id = discord_user_id
        self.guild_id = guild_id
        self.source_interaction = source_interaction
        self.dm_view = dm_view

    async def on_submit(self, interaction: discord.Interaction):
        username = self.roblox_username.value.strip()
        await interaction.response.defer(ephemeral=True)

        session = await get_session(str(self.discord_user_id))
        if not session or time.time() * 1000 > session['expiresAt']:
            return await interaction.followup.send("Your verification code expired. Run `/verify start` again.")

        try:
            profile = await fetch_roblox_description(username)
        except Exception:  # noqa: BLE001
            return await interaction.followup.send('Bot error while contacting Roblox. Try again later.')

        if profile.get('notFound'):
            return await interaction.followup.send('That Roblox username was not found. Check spelling and try again.')

        if not description_contains_code(profile['description'], session['code']):
            return await interaction.followup.send(
                f"Code not found in your Roblox profile description yet. Make sure `{session['code']}` "
                f"is pasted in your About section, then click Verify! again."
            )

        # success
        client = interaction.client
        guild = client.get_guild(self.guild_id) or await client.fetch_guild(self.guild_id)
        member = guild.get_member(self.discord_user_id) or await guild.fetch_member(self.discord_user_id)
        cfg = await get_guild_config(str(self.guild_id))

        if not cfg or not cfg.get('verifiedRoleId'):
            return await interaction.followup.send('Bot error: verified role is no longer configured.')

        role = guild.get_role(int(cfg['verifiedRoleId']))
        if role is None:
            return await interaction.followup.send('Bot error: verified role is no longer configured.')

        try:
            await member.add_roles(role)
        except discord.HTTPException as role_err:
            print(f'Role assign failed: {role_err}')
            return await interaction.followup.send('Bot error: could not assign role. Check my role position/permissions.')

        try:
            await save_verified_user(
                str(self.discord_user_id),
                roblox_id=profile['robloxId'],
                roblox_username=profile['robloxUsername'],
                guild_id=str(self.guild_id),
            )
        except Exception as save_err:  # noqa: BLE001
            print(f'save_verified_user failed (role already assigned): {save_err}')

        await clear_session(str(self.discord_user_id))
        self.dm_view.verified = True
        self.dm_view.stop()

        await log_command_activity(
            self.source_interaction,
            subcommand='verifyComplete',
            success=True,
            fields={'discordUser': interaction.user, 'robloxUsername': profile['robloxUsername']},
        )

        return await interaction.followup.send(f"Verified! You're linked as **{profile['robloxUsername']}**. Role assigned.")


async def _run_verify_start(interaction: discord.Interaction):
    """Shared body for /verify start and the green button on the verify
    panel (/verify sendpanel). Panel button interactions don't go through
    the slash-command tree, so this must be self-contained and not assume
    it's a slash command context.

    Every response here is explicitly ephemeral=True. Deferring with
    ephemeral=True only makes the initial "thinking" state ephemeral --
    subsequent interaction.followup.send() calls do NOT inherit that and
    need ephemeral=True passed on each one, or they end up posting publicly
    in the panel's channel. That matters a lot here specifically, since the
    whole point of the panel is to keep its channel clean.
    """
    if interaction.guild is None:
        return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    config = await get_guild_config(str(interaction.guild_id))
    if not config or not config.get('verifiedRoleId'):
        return await interaction.followup.send(
            "Verify role isn't set up yet. Ask an admin to run `/verify setrole` first.",
            ephemeral=True,
        )

    existing_record = await get_verified_user(str(interaction.user.id))
    if existing_record:
        return await interaction.followup.send('You are already verified!', ephemeral=True)

    existing = await get_session(str(interaction.user.id))
    if existing:
        return await interaction.followup.send(
            f"You already have an active verification code. Check your DMs, or wait "
            f"<t:{int(existing['expiresAt'] / 1000)}:R> for it to expire before starting over.",
            ephemeral=True,
        )

    await interaction.followup.send('Check your DMs! 📬', ephemeral=True)

    session = await create_session(str(interaction.user.id))
    code, expires_at = session['code'], session['expiresAt']

    embed = discord.Embed(
        title='Roblox Verification',
        description=(
            f'Copy this code and paste it anywhere in your Roblox profile **About/Description**:\n\n'
            f'```{code}```\n'
            f'This code expires <t:{int(expires_at / 1000)}:R>.\n\n'
            f"Once it's on your profile, click **Verify!** below."
        ),
        color=0x00B0F4,
    )

    view = VerifyDMButtonView(discord_user_id=interaction.user.id, guild_id=interaction.guild_id, source_interaction=interaction)

    try:
        dm = await interaction.user.send(embed=embed, view=view)
        view.message = dm
    except discord.HTTPException:
        return await interaction.followup.send(
            'Could not DM you. Please enable DMs from server members and run `/verify start` again.',
            ephemeral=True,
        )

    await log_command_activity(interaction, subcommand='start', success=True, fields={'discordUser': interaction.user})


# ---------------------------------------------------------------------------
# /verify sendpanel -- persistent panel embed with a green "Verify!" button
# that runs the same logic as /verify start. Modelled on /ticket send.
# ---------------------------------------------------------------------------
CID_VERIFY_PANEL_BTN = 'verify_panel_start'


class VerifyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Verify!', style=discord.ButtonStyle.success, custom_id=CID_VERIFY_PANEL_BTN)
    async def on_verify_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _run_verify_start(interaction)


class SendVerifyPanelModal(discord.ui.Modal, title='Verify Panel'):
    panel_title = discord.ui.TextInput(label='Title', style=discord.TextStyle.short, required=True)
    panel_description = discord.ui.TextInput(label='Description', style=discord.TextStyle.paragraph, required=True)
    panel_color = discord.ui.TextInput(label='Color (hex, e.g. #57F287)', placeholder='#57F287', style=discord.TextStyle.short, required=False)

    def __init__(self, source_interaction: discord.Interaction, target_channel: discord.TextChannel):
        super().__init__()
        self.source_interaction = source_interaction
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        color = 0x57F287
        color_raw = self.panel_color.value.strip()
        if color_raw:
            try:
                color = int(color_raw.replace('#', ''), 16)
            except ValueError:
                pass

        embed = discord.Embed(title=self.panel_title.value.strip(), description=self.panel_description.value.strip(), color=color)

        try:
            await self.target_channel.send(embed=embed, view=VerifyPanelView())
        except discord.HTTPException as err:
            print(f'Failed to send verify panel: {err}')
            return await interaction.followup.send(f"Couldn't send the panel to {self.target_channel.mention}.", ephemeral=True)

        await log_command_activity(
            self.source_interaction, subcommand='sendpanel', success=True,
            fields={'discordUser': interaction.user, 'channel': self.target_channel},
        )

        await interaction.followup.send(f'Verify panel sent to {self.target_channel.mention}.', ephemeral=True)


class VerifyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    verify_group = app_commands.Group(name='verify', description='Verify your Roblox account')

    @verify_group.command(name='start', description='Start Roblox verification (DMs you a code)')
    async def start(self, interaction: discord.Interaction):
        await _run_verify_start(interaction)

    @verify_group.command(name='sendpanel', description='Send a verify panel embed with a button that runs /verify start')
    @app_commands.describe(channel='Where to send the panel')
    async def sendpanel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message('You need **Administrator** permission to do that.', ephemeral=True)

        perms = channel.permissions_for(interaction.guild.me)
        if not perms.send_messages or not perms.view_channel:
            return await interaction.response.send_message(f"I can't send messages in {channel.mention}. Check my permissions there.", ephemeral=True)

        await interaction.response.send_modal(SendVerifyPanelModal(interaction, channel))
    @verify_group.command(name='setrole', description='Set the role given after verification')
    @app_commands.describe(role='Role to assign on verify')
    async def setrole(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        if not interaction.user.guild_permissions.manage_roles:
            return await interaction.followup.send('You need Manage Roles permission to do that.')

        await set_guild_role(str(interaction.guild_id), str(role.id))
        await log_command_activity(interaction, subcommand='setrole', success=True, fields={'discordUser': interaction.user, 'role': role})
        return await interaction.followup.send(f'Verified role set to {role.mention}.')

    @verify_group.command(name='unverify', description='Remove your Roblox verification')
    async def unverify(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)

        # showModal() must be the very first thing that happens on this
        # interaction -- no Firestore read before it.
        await interaction.response.send_modal(UnverifyModal(source_interaction=interaction))

    @verify_group.command(name='linkgroup', description='Link a Roblox group you belong to, so its places can use your whitelisted products')
    @app_commands.describe(group_id='Roblox group ID (numeric)')
    async def linkgroup(self, interaction: discord.Interaction, group_id: str):
        await interaction.response.defer(ephemeral=True)

        group_id = group_id.strip()
        if not group_id.isdigit():
            return await interaction.followup.send('Group ID harus berupa angka. Contoh: `12345678`.')

        record = await get_verified_user(str(interaction.user.id))
        if not record:
            await log_command_activity(
                interaction, subcommand='linkgroup', success=False,
                fields={'discordUser': interaction.user, 'groupId': group_id}, note='User is not verified.',
            )
            return await interaction.followup.send('Kamu harus verifikasi akun Roblox dulu lewat `/verify start`.')

        try:
            member_group_ids = await fetch_user_group_ids(record['robloxId'])
        except Exception as err:  # noqa: BLE001
            print(f'fetch_user_group_ids failed: {err}')
            await log_command_activity(
                interaction, subcommand='linkgroup', success=False,
                fields={'discordUser': interaction.user, 'groupId': group_id}, note='Roblox groups API call failed.',
            )
            return await interaction.followup.send('Gagal mengambil daftar group Roblox kamu. Coba lagi nanti.')

        if group_id not in member_group_ids:
            await log_command_activity(
                interaction, subcommand='linkgroup', success=False,
                fields={'discordUser': interaction.user, 'groupId': group_id}, note='User is not a member of that group.',
            )
            return await interaction.followup.send(
                f'Akun Roblox kamu (**{record["robloxUsername"]}**) bukan anggota group `{group_id}`. '
                f'Pastikan kamu sudah join group itu, baru jalankan lagi.'
            )

        await link_group(str(interaction.user.id), group_id)
        await log_command_activity(
            interaction, subcommand='linkgroup', success=True,
            fields={'discordUser': interaction.user, 'groupId': group_id},
        )
        await interaction.followup.send(
            f'Group `{group_id}` berhasil ditautkan ke akunmu. Produk yang di-whitelist ke group ini sekarang bisa '
            f'kamu pakai di place manapun yang dimiliki group tersebut -- selama kamu masih jadi anggotanya. '
            f'Kalau kamu keluar dari group, akses ini otomatis hilang.'
        )

    @verify_group.command(name='unlinkgroup', description='Unlink a previously linked Roblox group')
    @app_commands.describe(group_id='Roblox group ID (numeric)')
    async def unlinkgroup(self, interaction: discord.Interaction, group_id: str):
        await interaction.response.defer(ephemeral=True)

        group_id = group_id.strip()
        await unlink_group(str(interaction.user.id), group_id)
        await log_command_activity(
            interaction, subcommand='unlinkgroup', success=True,
            fields={'discordUser': interaction.user, 'groupId': group_id},
        )
        await interaction.followup.send(f'Group `{group_id}` sudah dilepas dari akunmu.')

    @verify_group.command(name='profile', description="Look up a member's linked Roblox profile")
    @app_commands.describe(user='Discord user to look up')
    async def profile(self, interaction: discord.Interaction, user: discord.User):
        if interaction.guild is None:
            return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)

        # public (not ephemeral) on purpose -- mods need to see it to catch misuse
        await interaction.response.defer()

        record = await get_verified_user(str(user.id))
        if not record:
            await log_command_activity(
                interaction, subcommand='profile', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user},
                note='Target user is not verified.',
            )
            return await interaction.followup.send('The user is not verified!')

        try:
            details = await fetch_roblox_profile_details(record['robloxId'])
        except Exception:  # noqa: BLE001
            await log_command_activity(
                interaction, subcommand='profile', success=False,
                fields={'discordUser': interaction.user, 'targetUser': user},
                note='Roblox API error while fetching profile details.',
            )
            return await interaction.followup.send('Bot error while contacting Roblox. Try again later.')

        await log_command_activity(interaction, subcommand='profile', success=True, fields={'discordUser': interaction.user, 'targetUser': user})

        owned_products = await get_products_by_ids(record.get('ownedProducts') or [])

        view = ProfileView(owner_id=interaction.user.id, target=user, record=record, details=details, owned_products=owned_products)
        message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)
        view.message = message


async def setup(bot: commands.Bot):
    await bot.add_cog(VerifyCog(bot))
    # Register the persistent panel view so its button keeps working after a
    # bot restart, same pattern as TicketPanelView in commands/ticket.py.
    bot.add_view(VerifyPanelView())
