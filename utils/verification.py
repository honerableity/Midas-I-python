"""Discord OAuth2 verification helpers.

No external platform anymore -- verification is now: user clicks
Verify -> approves a Discord OAuth2 consent (identify scope) -> agrees
to the rules -> gets the role.

Session doc shape in Firestore (`verifications/{discordId}`):
  {
    state: "<signed state>",
    guildId: "...",
    status: "pending" | "oauth_done" | "rules_agreed",
    expiresAt: <ms>,
    createdAt: <ms>,
  }
"""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

from utils.firebase import db

EXPIRY_MS = 15 * 60 * 1000  # 15 min

DISCORD_AUTHORIZE_URL = 'https://discord.com/api/oauth2/authorize'

DISCORD_CLIENT_ID = os.environ['DISCORD_OAUTH_CLIENT_ID']
DISCORD_CLIENT_SECRET = os.environ['DISCORD_OAUTH_CLIENT_SECRET']
# Must be registered exactly (scheme+host+path) in the Discord Developer
# Portal app's OAuth2 redirect list, and match the Vercel callback route.
DISCORD_REDIRECT_URI = os.environ['DISCORD_OAUTH_REDIRECT_URI']

# `identify` is enough -- proves the user actually approved via a real
# Discord session (not just clicking a button), and gives us id/username.
OAUTH_SCOPES = 'identify'

STATE_SECRET = os.environ['VERIFY_STATE_SECRET'].encode()

DISCORD_EPOCH_MS = 1420070400000  # 2015-01-01, start of Discord's snowflake epoch


def _now_ms():
    return int(time.time() * 1000)


def snowflake_created_at_ms(snowflake_id: str) -> int:
    return (int(snowflake_id) >> 22) + DISCORD_EPOCH_MS


def _sign_state(payload: dict) -> str:
    raw = json.dumps(payload, separators=(',', ':')).encode()
    b64 = base64.urlsafe_b64encode(raw).rstrip(b'=')
    sig = hmac.new(STATE_SECRET, b64, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b'=')
    return f'{b64.decode()}.{sig_b64.decode()}'


def verify_state(state: str) -> dict | None:
    """Mirrors api/_state.js on the Vercel side -- keep both in sync."""
    try:
        b64, sig_b64 = state.split('.', 1)
        expected_sig = hmac.new(STATE_SECRET, b64.encode(), hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b'=').decode()
        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            return None
        padded = b64 + '=' * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if _now_ms() > payload['expiresAt']:
            return None
        return payload
    except Exception:  # noqa: BLE001
        return None


async def create_session(discord_id: str, guild_id: str):
    expires_at = _now_ms() + EXPIRY_MS
    state = _sign_state({
        'discordId': discord_id,
        'guildId': guild_id,
        'expiresAt': expires_at,
    })

    db.collection('verifications').document(discord_id).set({
        'state': state,
        'guildId': guild_id,
        'status': 'pending',
        'expiresAt': expires_at,
        'createdAt': _now_ms(),
    })

    return {'state': state, 'expiresAt': expires_at}


def build_authorize_url(state: str) -> str:
    params = {
        'client_id': DISCORD_CLIENT_ID,
        'redirect_uri': DISCORD_REDIRECT_URI,
        'response_type': 'code',
        'scope': OAUTH_SCOPES,
        'state': state,
        'prompt': 'consent',
    }
    return f'{DISCORD_AUTHORIZE_URL}?{urlencode(params)}'


async def get_session(discord_id: str):
    snap = db.collection('verifications').document(discord_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    if _now_ms() > data['expiresAt']:
        await clear_session(discord_id)
        return None
    return data


async def mark_rules_agreed(discord_id: str):
    db.collection('verifications').document(discord_id).set({'status': 'rules_agreed'}, merge=True)


async def clear_session(discord_id: str):
    db.collection('verifications').document(discord_id).delete()


async def get_guild_config(guild_id: str):
    snap = db.collection('guildConfig').document(guild_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


async def set_guild_role(guild_id: str, role_id: str):
    db.collection('guildConfig').document(guild_id).set({'verifiedRoleId': role_id}, merge=True)


async def set_guild_rules(guild_id: str, rules_text: str):
    db.collection('guildConfig').document(guild_id).set({'rulesText': rules_text}, merge=True)


async def save_verified_user(discord_id: str, *, guild_id: str, account_created_at: int):
    db.collection('verifiedUsers').document(discord_id).set({
        'verifiedAt': _now_ms(),
        'guildId': guild_id,
        'accountCreatedAt': account_created_at,
    }, merge=True)


async def get_verified_user(discord_id: str):
    snap = db.collection('verifiedUsers').document(discord_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


async def remove_verified_user(discord_id: str):
    db.collection('verifiedUsers').document(discord_id).delete()
