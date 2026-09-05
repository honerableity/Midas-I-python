"""Alt-account flag review -- watches Firestore `altFlags` docs (written
by api/callback.js whenever a new verification's IP/fingerprint matches
an already-verified account) and posts a review embed with action
buttons to the mod-log channel.

This intentionally does NOT auto-kick anything. IP address alone is a
weak signal -- shared wifi, mobile carrier CGNAT, and family members on
the same network routinely collide on IP without being the same
person. A human always makes the final call via the buttons.

Threading note: same pattern as utils/verify_listener.py -- Firestore's
on_snapshot() callback fires on a background gRPC thread, so every
callback here hops back onto the bot's asyncio event loop via
asyncio.run_coroutine_threadsafe before touching anything discord.py.

Wiring (add to main.py's on_ready, alongside start_verify_listener):
    from utils.altcheck_listener import start_altcheck_listener
    start_altcheck_listener(bot)
"""
import asyncio

import discord

from utils.firebase import db

MOD_LOG_CHANNEL_ID = 1535296537049964715

_listener_registration = None


def start_altcheck_listener(bot: discord.Client):
    """Attach a Firestore snapshot listener for `altFlags` docs with
    status == "pending". Call once from on_ready, after the bot's
    channel cache is populated.
    """
    global _listener_registration

    loop = asyncio.get_event_loop()
    query = db.collection('altFlags').where('status', '==', 'pending')

    def on_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name != 'ADDED':
                # Only act the moment a flag first appears -- MODIFIED
                # fires again when the bot itself later writes status:
                # "resolved"/"ignored", which would otherwise loop.
                continue

            doc = change.document
            data = doc.to_dict() or {}
            asyncio.run_coroutine_threadsafe(
                _post_alt_flag(bot, doc.id, data),
                loop,
            )

    _listener_registration = query.on_snapshot(on_snapshot)
    print('[altcheck_listener] Firestore listener for altFlags started')
    return _listener_registration


class AltFlagReviewView(discord.ui.View):
    """Buttons attached to the review embed. Both actions write back to
    the altFlags doc (status: resolved/ignored) so re-running the bot
    doesn't re-post already-handled flags, and record who actioned it.
    """

    def __init__(self, *, flag_id: str, guild_id: int, new_discord_id: str, matched_ids: list[str]):
        super().__init__(timeout=None)  # mod-log buttons should stay usable indefinitely
        self.flag_id = flag_id
        self.guild_id = guild_id
        self.new_discord_id = new_discord_id
        self.matched_ids = matched_ids

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message('You need Kick Members permission to act on this.', ephemeral=True)
            return False
        return True

    @discord.ui.button(label='Kick Both', style=discord.ButtonStyle.danger, custom_id='altflag_kick_both')
    async def kick_both(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.client.get_guild(self.guild_id) or await interaction.client.fetch_guild(self.guild_id)
        kicked = []
        failed = []

        all_ids = [self.new_discord_id, *self.matched_ids]
        for uid in all_ids:
            try:
                member = guild.get_member(int(uid)) or await guild.fetch_member(int(uid))
                await member.kick(reason=f'Alt account flagged by {interaction.user} via /verify alt-detection')
                kicked.append(uid)
            except discord.NotFound:
                pass  # already left the server
            except Exception as err:  # noqa: BLE001
                print(f'[altcheck] kick failed for {uid}: {err}')
                failed.append(uid)

        db.collection('altFlags').document(self.flag_id).set({
            'status': 'resolved',
            'resolvedBy': str(interaction.user.id),
            'kickedIds': kicked,
        }, merge=True)

        self.stop()
        summary = f"Kicked: {', '.join(kicked) if kicked else 'none'}"
        if failed:
            summary += f"\nFailed: {', '.join(failed)}"
        await interaction.followup.send(summary, ephemeral=True)
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass

    @discord.ui.button(label='Ignore (False Positive)', style=discord.ButtonStyle.secondary, custom_id='altflag_ignore')
    async def ignore(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        db.collection('altFlags').document(self.flag_id).set({
            'status': 'ignored',
            'resolvedBy': str(interaction.user.id),
        }, merge=True)

        self.stop()
        await interaction.followup.send('Marked as false positive. No action taken.', ephemeral=True)
        try:
            await interaction.message.edit(view=None)
        except discord.HTTPException:
            pass


async def _post_alt_flag(bot: discord.Client, flag_id: str, data: dict):
    guild_id = data.get('guildId')
    new_discord_id = data.get('newDiscordId')
    matched = data.get('matchedAccounts') or []
    ip_partial = data.get('ipPartial')
    is_proxy = data.get('isProxy')

    if not guild_id or not new_discord_id or not matched:
        print(f'[altcheck] altFlags/{flag_id} missing required fields, skipping')
        return

    channel = bot.get_channel(MOD_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(MOD_LOG_CHANNEL_ID)
        except discord.HTTPException as err:
            print(f'[altcheck] could not fetch mod-log channel {MOD_LOG_CHANNEL_ID}: {err}')
            return

    matched_ids = [m['discordId'] for m in matched]
    matched_lines = []
    for m in matched:
        matched_on = ', '.join(m.get('matchedOn', []))
        matched_lines.append(f"<@{m['discordId']}> (`{m['discordId']}`) -- matched on: {matched_on}")

    embed = discord.Embed(
        title='⚠️ Possible alt account detected',
        description=(
            f"New verification: <@{new_discord_id}> (`{new_discord_id}`)\n\n"
            f"**Matches an already-verified account:**\n" + '\n'.join(matched_lines)
        ),
        color=0xFFA500,
    )
    if ip_partial:
        embed.add_field(name='IP (masked)', value=f'`{ip_partial}`', inline=True)
    if is_proxy is not None:
        embed.add_field(name='Proxy/VPN detected', value='Yes' if is_proxy else 'No', inline=True)
    embed.set_footer(text='IP/fingerprint matches can happen on shared networks (wifi, mobile carriers) without being the same person -- verify before kicking.')

    view = AltFlagReviewView(
        flag_id=flag_id,
        guild_id=int(guild_id),
        new_discord_id=new_discord_id,
        matched_ids=matched_ids,
    )

    try:
        await channel.send(embed=embed, view=view)
    except discord.HTTPException as err:
        print(f'[altcheck] failed to post alt flag to mod-log channel: {err}')
