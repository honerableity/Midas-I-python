"""Roblox <-> Discord verification helpers.

Ported from utils/verification.js.
"""
import random
import re
import time
import unicodedata

import aiohttp
from google.cloud.firestore_v1 import ArrayRemove, ArrayUnion

from data.words import WORDS
from utils.firebase import db

CODE_LENGTH = 5
EXPIRY_MS = 15 * 60 * 1000  # 15 min


def _now_ms():
    return int(time.time() * 1000)


def gen_code():
    pool = list(WORDS)
    picked = []
    for _ in range(CODE_LENGTH):
        idx = random.randrange(len(pool))
        picked.append(pool[idx])
        pool.pop(idx)  # no repeat word in same code
    return '-'.join(picked)


async def create_session(discord_id: str):
    code = gen_code()
    expires_at = _now_ms() + EXPIRY_MS
    db.collection('verifications').document(discord_id).set({
        'code': code,
        'expiresAt': expires_at,
        'createdAt': _now_ms(),
    })
    return {'code': code, 'expiresAt': expires_at}


async def get_session(discord_id: str):
    snap = db.collection('verifications').document(discord_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    if _now_ms() > data['expiresAt']:
        await clear_session(discord_id)  # lazy cleanup of stale docs
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
    # merge=True so re-verifying overwrites cleanly instead of erroring on existing doc.
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


async def fetch_roblox_description(username: str):
    """Fetches Roblox user id from username, then their profile description (blurb)."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            'https://users.roblox.com/v1/usernames/users',
            json={'usernames': [username], 'excludeBannedUsers': False},
        ) as user_res:
            if user_res.status != 200:
                raise RuntimeError(f'Roblox user lookup failed: {user_res.status}')
            user_data = await user_res.json()

        if not user_data.get('data'):
            return {'notFound': True}

        roblox_id = user_data['data'][0]['id']

        async with session.get(f'https://users.roblox.com/v1/users/{roblox_id}') as profile_res:
            if profile_res.status != 200:
                raise RuntimeError(f'Roblox profile fetch failed: {profile_res.status}')
            profile_data = await profile_res.json()

    return {
        'notFound': False,
        'robloxId': roblox_id,
        'robloxUsername': profile_data['name'],
        'description': profile_data.get('description', ''),
    }


async def fetch_roblox_profile_details(roblox_id):
    """Full profile pull for /verify profile: account info + groups.

    Note: badges.roblox.com is intentionally not called here -- Roblox removed
    unauthenticated access to that endpoint (4 May '26 API change), so it 401s
    for every request without a logged-in .ROBLOSECURITY cookie. Not worth
    standing up a Roblox account session just for a badge count.
    """
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
        'created': user.get('created'),  # ISO string
        'hasVerifiedBadge': user.get('hasVerifiedBadge'),
        'groups': groups,
    }


_ZERO_WIDTH_RE = re.compile('[\u200B-\u200D\uFEFF]')
_SPACE_DASH_RE = re.compile(r'[\s-]+')


def normalize(s: str) -> str:
    s = s.lower()
    s = _ZERO_WIDTH_RE.sub('', s)  # strip zero-width chars
    s = _SPACE_DASH_RE.sub('', s)  # collapse spaces/dashes so "pearl - opal" still matches "pearl-opal"
    return s


def description_contains_code(description: str, code: str) -> bool:
    return normalize(code) in normalize(description or '')


# ---------------------------------------------------------------------------
# Group linking (/verify linkgroup) -- lets a verified dev tie a Roblox
# group to their account so the Vercel /api/validate endpoint can grant
# access to any place owned by that group, for products the group itself
# has been whitelisted for (see utils/products.add_group_whitelist).
#
# Membership is checked live at link time here, AND re-checked live on every
# /api/validate call -- this file does not cache "is still a member".
# ---------------------------------------------------------------------------

async def fetch_user_group_ids(roblox_id) -> list[str]:
    """Live list of group ids robloxId currently belongs to."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f'https://groups.roblox.com/v1/users/{roblox_id}/groups/roles') as res:
            if res.status != 200:
                raise RuntimeError(f'Roblox groups fetch failed: {res.status}')
            data = await res.json()
    return [str(g['group']['id']) for g in (data.get('data') or [])]


async def link_group(discord_id: str, group_id: str):
    """Adds group_id to this discord user's linkedGroupIds. Caller must
    verify group membership BEFORE calling this (see /verify linkgroup).
    """
    db.collection('verifiedUsers').document(discord_id).set(
        {'linkedGroupIds': ArrayUnion([str(group_id)])},
        merge=True,
    )


async def unlink_group(discord_id: str, group_id: str):
    db.collection('verifiedUsers').document(discord_id).set(
        {'linkedGroupIds': ArrayRemove([str(group_id)])},
        merge=True,
    )
