"""Firestore listener that reacts the instant a /verify OAuth callback
completes -- replaces the old manual "Continue" button.

Flow before this file existed:
  1. Bot DMs a link + a "Continue" button.
  2. User authorizes in the browser (Vercel /api/callback sets
     status="oauth_done" in Firestore).
  3. User has to come BACK to Discord and click "Continue" themselves,
     which re-reads the session to check it's actually done.

Flow with this file:
  1. Bot DMs a link only (no "Continue" button needed anymore).
  2. User authorizes in the browser -- Vercel sets status="oauth_done".
  3. This listener notices that write *immediately* (Firestore push,
     not polling) and DMs the user the rules-agreement embed itself --
     no action required from the user in Discord at all until they hit
     "I agree".

Threading note: firebase_admin's Firestore client runs on_snapshot()
callbacks on a background gRPC thread, completely outside discord.py's
asyncio event loop. Every callback here immediately hands off to the
bot's event loop via asyncio.run_coroutine_threadsafe -- nothing
discord.py-related is ever touched directly from the gRPC thread.

Usage (see bottom of file / README snippet for the exact wiring):
    from utils.verify_listener import start_verify_listener

    @bot.event
    async def on_ready():
        start_verify_listener(bot)
"""
import asyncio

import discord

from utils.firebase import db
from utils.verification import get_guild_config, mark_rules_agreed as _mark_rules_agreed  # noqa: F401  (kept for parity/reference)

# Local import to avoid a circular import at module load time -- verify.py
# and this module both live under commands/utils and may load in either
# order depending on cog discovery.
def _get_rules_agree_view_cls():
    from commands.verify import RulesAgreeView
    return RulesAgreeView


DEFAULT_RULES_TEXT = (
    "By verifying you agree to follow this server's rules, be respectful to other "
    "members, and follow Discord's Terms of Service and Community Guidelines."
)

_listener_registration = None  # keep a reference so it isn't garbage collected


def start_verify_listener(bot: discord.Client):
    """Attach a Firestore snapshot listener for `verifications` docs whose
    status is "oauth_done". Call this once, after the bot is ready (so
    `bot.loop` and guild/member caches are populated).

    Safe to call more than once by accident -- re-registering just adds
    a second listener, so guard against that at the call site (e.g. a
    module-level flag), not here.
    """
    global _listener_registration

    loop = asyncio.get_event_loop()

    query = db.collection('verifications').where('status', '==', 'oauth_done')

    def on_snapshot(col_snapshot, changes, read_time):
        for change in changes:
            # We only care about docs newly matching the query (ADDED) or
            # updated while still matching (MODIFIED going pending -> oauth_done
            # is the only realistic transition into this filter). REMOVED
            # fires when a doc stops matching (e.g. moves to rules_agreed or
            # gets deleted) -- nothing to do then.
            if change.type.name not in ('ADDED', 'MODIFIED'):
                continue

            doc = change.document
            discord_id = doc.id
            data = doc.to_dict() or {}
            guild_id = data.get('guildId')

            if not guild_id:
                print(f'[verify_listener] oauth_done doc {discord_id} has no guildId, skipping')
                continue

            asyncio.run_coroutine_threadsafe(
                _handle_oauth_done(bot, discord_id, guild_id),
                loop,
            )

    # on_snapshot keeps firing for the lifetime of this Watch -- store it so
    # it isn't garbage collected (which would silently kill the listener).
    _listener_registration = query.on_snapshot(on_snapshot)
    print('[verify_listener] Firestore listener for verifications (oauth_done) started')
    return _listener_registration


async def _handle_oauth_done(bot: discord.Client, discord_id: str, guild_id: str):
    """Runs on the bot's own event loop (hopped over from the gRPC thread).
    DMs the user the rules-agreement step directly -- this is exactly what
    VerifyDMView._on_continue used to do after checking session status,
    minus the check (we only get here when status is already oauth_done).
    """
    try:
        user = bot.get_user(int(discord_id)) or await bot.fetch_user(int(discord_id))
    except discord.NotFound:
        print(f'[verify_listener] Discord user {discord_id} not found, skipping DM')
        return
    except Exception as err:  # noqa: BLE001
        print(f'[verify_listener] fetch_user failed for {discord_id}: {err}')
        return

    config = await get_guild_config(str(guild_id))
    rules_text = (config or {}).get('rulesText') or DEFAULT_RULES_TEXT

    embed = discord.Embed(title='Server Rules', description=rules_text, color=0x00B0F4)

    RulesAgreeView = _get_rules_agree_view_cls()
    view = RulesAgreeView(
        discord_user_id=int(discord_id),
        guild_id=int(guild_id),
        source_interaction=None,  # no interaction triggered this DM; see note in verify.py
    )

    try:
        await user.send(embed=embed, view=view)
    except discord.Forbidden:
        print(f'[verify_listener] Could not DM {discord_id} (DMs closed) after oauth_done')
    except Exception as err:  # noqa: BLE001
        print(f'[verify_listener] Unexpected error DMing {discord_id}: {err}')
