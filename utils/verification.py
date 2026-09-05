"""Roblox <-> Discord verification helpers -- OAuth2 edition.

Replaces the old "paste a code into your profile description" flow with
Roblox's real OAuth 2.0 login (Sign in with Roblox). The bot never sees a
Roblox password or session cookie; it only ever sees:
  - the signed `state` it minted itself
  - whatever the Vercel callback writes back into Firestore once Roblox
    has confirmed the user approved the requested scopes

Flow:
  1. bot: create_session()          -> writes a pending doc + returns state
  2. bot: build_authorize_url(state) -> DM'd as a link button
  3. user approves on Roblox's site
  4. Roblox -> Vercel /api/callback -> exchanges code, calls userinfo,
     calls mark_session_authorized() (imported + used on the Vercel side,
     see api/callback.py) which flips the same Firestore doc to
     {"status": "authorized", "robloxId": ..., "robloxUsername": ...}
  5. bot: user clicks "I've authorized" button -> get_session() sees
     status == "authorized" -> finish_verification() assigns the role
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

ROBLOX_AUTHORIZE_URL = 'https://apis.roblox.com/oauth/v1/authorize'
ROBLOX_CLIENT_ID = os.environ['ROBLOX_OAUTH_CLIENT_ID']
# The redirect_uri must be registered EXACTLY (scheme+host+path) in the
# Roblox Creator Dashboard OAuth app config, and must match what the
# Vercel callback (api/callback.py) is deployed at.
ROBLOX_REDIRECT_URI = os.environ['ROBLOX_OAUTH_REDIRECT_URI']

# Only what we actually use. group:read is needed for the guild-side
# "must be a member of group X" gating some servers configure; if a guild
# has no such requirement it's harmless to have asked for it. If you want
# to shrink the consent screen for guilds that never use group gating you
# could make this conditional, but a single fixed scope set is simpler to
# reason about and matches what the Vercel callback expects.
OAUTH_SCOPES = 'openid profile group:read'

STATE_SECRET = os.environ['VERIFY_STATE_SECRET'].encode()


def _now_ms():
    return int(time.time() * 1000)


def _sign_state(payload: dict) -> str:
    """Signed, url-safe state blob: base64(payload) + '.' + hmac.

    Signing (not just Firestore lookup) means Vercel can validate the
    state's authenticity/expiry without needing a Firestore read on that
    hot path if it ever wants to skip straight to token exchange -- and it
    means a forged/replayed state can't be used to graft one Discord
    user's verification onto another's session doc.
    """
    raw = json.dumps(payload, separators=(',', ':')).encode()
    b64 = base64.urlsafe_b64encode(raw).rstrip(b'=')
    sig = hmac.new(STATE_SECRET, b64, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b'=')
    return f'{b64.decode()}.{sig_b64.decode()}'


def verify_state(state: str) -> dict | None:
    """Used by the Vercel callback to validate + decode the state param.
    Mirrors this function -- kept here as the source of truth / for the
    bot's own polling to decode session ids without a second Firestore
    round trip. See api/_state.py for the Vercel-side copy.
    """
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
        'client_id': ROBLOX_CLIENT_ID,
        'redirect_uri': ROBLOX_REDIRECT_URI,
        'scope': OAUTH_SCOPES,
        'response_type': 'code',
        'state': state,
    }
    return f'{ROBLOX_AUTHORIZE_URL}?{urlencode(params)}'


async def get_session(discord_id: str):
    """Returns the raw session doc, or None if missing/expired.
    Lazily deletes stale docs so 'Check status' clicks don't loop forever
    on a session that will never be authorized.
    """
    snap = db.collection('verifications').document(discord_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    if _now_ms() > data['expiresAt']:
        await clear_session(discord_id)
        return None
    return data


async def clear_session(discord_id: str):
    db.collection('verifications').document(discord_id).delete()


async def get_guild_config(guild_id: str):
    snap = db.collection('guildConfig').document(guild_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


async def set_guild_role(guild_id: str, role_id: str):
    db.collection('guildConfig').document(guild_id).set({'verifiedRoleId': role_id}, merge=True)


async def save_verified_user(discord_id: str, *, roblox_id, roblox_username, guild_id):
    db.collection('verifiedUsers').document(discord_id).set({
        'robloxId': roblox_id,
        'robloxUsername': roblox_username,
        'verifiedAt': _now_ms(),
        'guildId': guild_id,
    }, merge=True)


async def get_verified_user(discord_id: str):
    snap = db.collection('verifiedUsers').document(discord_id).get()
    if not snap.exists:
        return None
    return snap.to_dict()


async def remove_verified_user(discord_id: str):
    db.collection('verifiedUsers').document(discord_id).delete()


async def fetch_roblox_profile_details(roblox_id):
    """Full profile pull for /verify profile: account info + groups.

    Note: badges.roblox.com is intentionally not called here -- Roblox
    removed unauthenticated access to that endpoint (4 May '26 API
    change), so it 401s for every request without a logged-in
    .ROBLOSECURITY cookie or an OAuth token carrying a badges scope, and
    we didn't ask for one.
    """
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://users.roblox.com/v1/users/{roblox_id}') as user_res:
            if user_res.status != 200:
                raise RuntimeError(f'Roblox user fetch failed: {user_res.status}')
            user = await user_res.json()

        async with session.get(f'https://groups.roblox.com/v1/users/{roblox_id}/groups/roles') as groups_res:
            if groups_res.status != 200:
                raise RuntimeError(f'Roblox groups fetch failed: {groups_res.status}')
            groups_data = await groups_res.json()

    groups = [{'name': g['group']['name'], 'role': g['role']['name']} for g in (groups_data.get('data') or [])]

    return {
        'username': user['name'],
        'displayName': user.get('displayName'),
        'created': user.get('created'),
        'hasVerifiedBadge': user.get('hasVerifiedBadge'),
        'groups': groups,
    }
