"""/verify command -- Discord OAuth2 + rules-agreement verification.

Flow:
  1. /verify start (or panel button) -> DM with a link button to Discord's
     own OAuth2 consent screen (scope: identify).
  2. Discord redirects to our Vercel /api/callback, which exchanges the
     code, confirms the authorizing user really is this discordId, and
     flips the session to "oauth_done".
  3. utils/verify_listener.py is watching Firestore for that flip and
     immediately DMs the user the rules -> they click "I agree" ->
     session flips to "rules_agreed" and the role is assigned right
     away. There is no "Continue" button anymore -- step 3 fires on its
     own the instant oauth_done is written, no action needed in Discord
     between authorizing and seeing the rules.

Roblox-specific commands (/verify profile, /verify linkgroup, etc.) are
gone -- there's no external identity left to look up.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.logger import log_command_activity
from utils.verification import (
    build_authorize_url,
    clear_session,
    create_session,
    get_guild_config,
    get_session,
    get_verified_user,
    mark_rules_agreed,
    remove_verified_user,
    save_verified_user,
    set_guild_role,
    set_guild_rules,
    snowflake_created_at_ms,
)

COMMAND_NAME = 'verify'

LOG_SCHEMA = {
    'subcommands': {
        'start': {'label': 'Verify — Start', 'fields': ['discordUser']},
        'setrole': {'label': 'Verify — Set Role', 'fields': ['discordUser', 'role']},
        'setrules': {'label': 'Verify — Set Rules', 'fields': ['discordUser']},
        'unverify': {'label': 'Verify — Unverify', 'fields': ['discordUser']},
        'verifyComplete': {'label': 'Verify — Completed', 'fields': ['discordUser']},
        'sendpanel': {'label': 'Verify — Panel Sent', 'fields': ['discordUser', 'channel']},
    },
}

DEFAULT_RULES_TEXT = (
    "By verifying you agree to follow this server's rules, be respectful to other "
    "members, and follow Discord's Terms of Service and Community Guidelines."
)

EXPIRY_MS_TIMEOUT = 15 * 60  # seconds, matches the session's own expiry


# ---------------------------------------------------------------------------
# /verify unverify -- simple confirm button (no username to type anymore,
# there's nothing external to double check against)
# ---------------------------------------------------------------------------
class UnverifyConfirmView(discord.ui.View):
    def __init__(self, *, owner_id: int, source_interaction: discord.Interaction):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.source_interaction = source_interaction

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('Only the person who ran this command can use this button.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Yes, unverify me', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        record = await get_verified_user(str(interaction.user.id))
        if not record:
            return await interaction.followup.send('You are already not verified!')

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
            self.source_interaction, subcommand='unverify', success=True,
            fields={'discordUser': interaction.user},
        )

        self.stop()
        return await interaction.followup.send('You have been unverified. Your role and verification data have been removed.')

    @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content='Cancelled.', view=None)


# ---------------------------------------------------------------------------
# Rules step -- sent automatically by utils/verify_listener.py the moment
# Firestore flips a session to "oauth_done". Can also, in principle, be
# constructed from a live interaction, so source_interaction is optional:
# it's only used for activity logging and is simply skipped when absent.
# ---------------------------------------------------------------------------
class RulesAgreeView(discord.ui.View):
    def __init__(self, *, discord_user_id: int, guild_id: int, source_interaction: discord.Interaction | None = None):
        super().__init__(timeout=EXPIRY_MS_TIMEOUT)
        self.discord_user_id = discord_user_id
        self.guild_id = guild_id
        # None when this view was sent by the Firestore listener rather
        # than from a slash-command interaction -- there's no interaction
        # to log activity against in that path (see _finish_verification).
        self.source_interaction = source_interaction

    @discord.ui.button(label='I agree', style=discord.ButtonStyle.success)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await _finish_verification(interaction, self.discord_user_id, self.guild_id, self.source_interaction)
        self.stop()


async def _finish_verification(interaction: discord.Interaction, discord_user_id: int, guild_id: int, source_interaction: discord.Interaction | None):
    session = await get_session(str(discord_user_id))

    if not session:
        return await interaction.followup.send(
            'Your verification session expired. Run `/verify start` again to get a new link.',
            ephemeral=True,
        )

    if session['status'] == 'pending':
        return await interaction.followup.send(
            "You haven't finished authorizing with Discord yet. Run `/verify start` again.",
            ephemeral=True,
        )

    await mark_rules_agreed(str(discord_user_id))

    client = interaction.client
    guild = client.get_guild(guild_id) or await client.fetch_guild(guild_id)
    member = guild.get_member(discord_user_id) or await guild.fetch_member(discord_user_id)
    cfg = await get_guild_config(str(guild_id))

    if not cfg or not cfg.get('verifiedRoleId'):
        return await interaction.followup.send('Bot error: verified role is no longer configured for that server.', ephemeral=True)

    role = guild.get_role(int(cfg['verifiedRoleId']))
    if role is None:
        return await interaction.followup.send('Bot error: verified role is no longer configured for that server.', ephemeral=True)

    try:
        await member.add_roles(role)
    except discord.HTTPException as role_err:
        print(f'Role assign failed: {role_err}')
        return await interaction.followup.send('Bot error: could not assign role. Check my role position/permissions.', ephemeral=True)

    try:
        await save_verified_user(
            str(discord_user_id),
            guild_id=str(guild_id),
            account_created_at=snowflake_created_at_ms(str(discord_user_id)),
        )
    except Exception as save_err:  # noqa: BLE001
        print(f'save_verified_user failed (role already assigned): {save_err}')

    await clear_session(str(discord_user_id))

    if source_interaction is not None:
        await log_command_activity(
            source_interaction, subcommand='verifyComplete', success=True,
            fields={'discordUser': interaction.user},
        )
    else:
        # Triggered via the Firestore listener's auto-DM flow -- there's no
        # originating slash-command interaction to attach a log entry to,
        # so just print instead; it still shows up in bot process logs.
        print(f'[verify] {interaction.user} ({interaction.user.id}) completed verification via listener-driven flow')

    return await interaction.followup.send('Verified! Role assigned. Welcome!', ephemeral=True)


async def _run_verify_start(interaction: discord.Interaction):
    """Shared body for /verify start and the button on the verify panel.
    Every response here is ephemeral=True -- deferring with
    ephemeral=True only makes the initial "thinking" state ephemeral,
    subsequent followups need it passed explicitly, or they post
    publicly in the panel's channel.

    The DM sent here only ever contains the OAuth link button. There's no
    "Continue" button anymore -- once the user approves the OAuth consent
    screen and lands back on the Vercel success page, utils/verify_listener.py
    notices the Firestore write and DMs the rules-agreement step on its own.
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
            f"You already have an active verification link. Check your DMs, or wait "
            f"<t:{int(existing['expiresAt'] / 1000)}:R> for it to expire before starting over.",
            ephemeral=True,
        )

    await interaction.followup.send('Check your DMs! 📬', ephemeral=True)

    session = await create_session(str(interaction.user.id), str(interaction.guild_id))
    authorize_url = build_authorize_url(session['state'])

    embed = discord.Embed(
        title='Server Verification',
        description=(
            "Click **Authorize with Discord** below and approve the request. This just confirms "
            "you're a real Discord account, nothing else is shared.\n\n"
            "Once you approve it, I'll automatically DM you the server rules to accept -- "
            "your role is assigned right after.\n\n"
            f"This link expires <t:{int(session['expiresAt'] / 1000)}:R>."
        ),
        color=0x00B0F4,
    )

    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label='Authorize with Discord',
        style=discord.ButtonStyle.link,
        url=authorize_url,
        emoji='🔗',
    ))

    try:
        await interaction.user.send(embed=embed, view=view)
    except discord.HTTPException:
        return await interaction.followup.send(
            'Could not DM you. Please enable DMs from server members and run `/verify start` again.',
            ephemeral=True,
        )

    await log_command_activity(interaction, subcommand='start', success=True, fields={'discordUser': interaction.user})


# ---------------------------------------------------------------------------
# /verify sendpanel -- persistent panel embed with a "Verify!" button that
# runs the same logic as /verify start.
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

    verify_group = app_commands.Group(name='verify', description='Verify your Discord account to get server access')

    @verify_group.command(name='start', description='Start verification (DMs you a link)')
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

    @verify_group.command(name='setrules', description='Set the rules text shown during verification')
    @app_commands.describe(rules='The rules text members must agree to')
    async def setrules(self, interaction: discord.Interaction, rules: str):
        if interaction.guild is None:
            return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.followup.send('You need Manage Server permission to do that.')

        await set_guild_rules(str(interaction.guild_id), rules)
        await log_command_activity(interaction, subcommand='setrules', success=True, fields={'discordUser': interaction.user})
        return await interaction.followup.send('Rules text updated.')

    @verify_group.command(name='unverify', description='Remove your verification')
    async def unverify(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await interaction.response.send_message('This command only works inside a server.', ephemeral=True)

        view = UnverifyConfirmView(owner_id=interaction.user.id, source_interaction=interaction)
        await interaction.response.send_message('Are you sure you want to unverify? This removes your role.', view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VerifyCog(bot))
    bot.add_view(VerifyPanelView())
