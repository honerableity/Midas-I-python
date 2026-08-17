"""Product catalog helpers: types, forums, ownership, delivery DM.

Ported from utils/products.js.
"""
import re
import time

import discord
from google.cloud.firestore_v1 import ArrayRemove, ArrayUnion

from utils.firebase import db

_SLUG_RE = re.compile(r'[^a-z0-9]+')
_SLUG_TRIM_RE = re.compile(r'^-+|-+$')


def _now_ms():
    return int(time.time() * 1000)


def slugify_channel_name(name: str) -> str:
    """Slugify a type name into a Discord-safe channel name (lowercase,
    dashes, alnum only).
    """
    slug = name.lower().strip()
    slug = _SLUG_RE.sub('-', slug)
    slug = _SLUG_TRIM_RE.sub('', slug)
    slug = slug[:90]
    return slug or 'produk'


async def get_guild_product_config(guild_id: str):
    snap = db.collection('guildConfig').document(guild_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    return {'productCategoryId': data.get('productCategoryId')}


async def save_product_category(guild_id: str, category_id: str):
    db.collection('guildConfig').document(guild_id).set({'productCategoryId': category_id}, merge=True)


async def resolve_product_category(guild: discord.Guild, guild_id: str):
    """Creates the shared "Bot Products" category the first time any product
    type forum is created in a guild. Subsequent createtype calls reuse it.
    """
    config = await get_guild_product_config(guild_id)

    if config and config.get('productCategoryId'):
        existing = guild.get_channel(int(config['productCategoryId']))
        if existing is None:
            try:
                existing = await guild.fetch_channel(int(config['productCategoryId']))
            except discord.HTTPException:
                existing = None
        if existing:
            return existing
        # Stored id is stale (category deleted manually) -- fall through and create a new one.

    category = await guild.create_category('Bot Products')
    await save_product_category(guild_id, str(category.id))
    return category


async def list_product_types(guild_id: str):
    query = db.collection('productTypes').where('guildId', '==', guild_id)
    return [{'id': doc.id, **doc.to_dict()} for doc in query.stream()]


async def get_product_type_by_name(guild_id: str, name: str):
    query = (
        db.collection('productTypes')
        .where('guildId', '==', guild_id)
        .where('name', '==', name)
        .limit(1)
    )
    docs = list(query.stream())
    if not docs:
        return None
    doc = docs[0]
    return {'id': doc.id, **doc.to_dict()}


async def get_product_type_by_id(type_id: str):
    doc = db.collection('productTypes').document(type_id).get()
    if not doc.exists:
        return None
    return {'id': doc.id, **doc.to_dict()}


def _build_forum_permission_overwrites(guild: discord.Guild):
    return {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            read_message_history=True,
            send_messages=False,
            send_messages_in_threads=False,
            create_public_threads=False,
            create_private_threads=False,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
            create_public_threads=True,
            manage_channels=True,
        ),
    }


async def create_or_sync_product_type_forum(guild: discord.Guild, guild_id: str, type_name: str):
    """Creates (or, if the type name already exists, re-syncs) a forum
    channel dedicated to one product type. Idempotent by design.
    """
    existing_type = await get_product_type_by_name(guild_id, type_name)
    category = await resolve_product_category(guild, guild_id)
    overwrites = _build_forum_permission_overwrites(guild)

    if existing_type and existing_type.get('forumChannelId'):
        existing_channel = guild.get_channel(int(existing_type['forumChannelId']))
        if existing_channel is None:
            try:
                existing_channel = await guild.fetch_channel(int(existing_type['forumChannelId']))
            except discord.HTTPException:
                existing_channel = None
        if existing_channel:
            await existing_channel.edit(overwrites=overwrites)
            return {'type': existing_type, 'forumChannel': existing_channel, 'created': False}
        # Stored channel id is stale -- fall through and create a fresh one.

    forum_channel = await guild.create_forum(
        slugify_channel_name(type_name),
        category=category,
        topic=f'Produk kategori: {type_name}',
        overwrites=overwrites,
    )

    if existing_type:
        db.collection('productTypes').document(existing_type['id']).set(
            {'forumChannelId': str(forum_channel.id)}, merge=True
        )
        type_doc = {**existing_type, 'forumChannelId': str(forum_channel.id)}
    else:
        _, ref = db.collection('productTypes').add({
            'name': type_name,
            'guildId': guild_id,
            'forumChannelId': str(forum_channel.id),
            'createdAt': _now_ms(),
        })
        type_doc = {'id': ref.id, 'name': type_name, 'guildId': guild_id, 'forumChannelId': str(forum_channel.id)}

    return {'type': type_doc, 'forumChannel': forum_channel, 'created': True}


async def link_existing_forum_to_type(guild: discord.Guild, guild_id: str, type_name: str, forum_channel: discord.ForumChannel):
    """Links a product type to an ALREADY-EXISTING forum channel instead of
    creating a new one.
    """
    existing_type = await get_product_type_by_name(guild_id, type_name)
    overwrites = _build_forum_permission_overwrites(guild)

    await forum_channel.edit(overwrites=overwrites)

    if existing_type:
        db.collection('productTypes').document(existing_type['id']).set(
            {'forumChannelId': str(forum_channel.id)}, merge=True
        )
        type_doc = {**existing_type, 'forumChannelId': str(forum_channel.id)}
    else:
        _, ref = db.collection('productTypes').add({
            'name': type_name,
            'guildId': guild_id,
            'forumChannelId': str(forum_channel.id),
            'createdAt': _now_ms(),
        })
        type_doc = {'id': ref.id, 'name': type_name, 'guildId': guild_id, 'forumChannelId': str(forum_channel.id)}

    return {'type': type_doc, 'forumChannel': forum_channel, 'wasExistingType': bool(existing_type)}


async def get_product(product_id: str):
    doc = db.collection('products').document(product_id).get()
    if not doc.exists:
        return None
    return {'id': doc.id, **doc.to_dict()}


async def list_products_by_type(guild_id: str, type_id: str):
    query = (
        db.collection('products')
        .where('guildId', '==', guild_id)
        .where('typeId', '==', type_id)
    )
    return [{'id': doc.id, **doc.to_dict()} for doc in query.stream()]


async def save_product(product_id: str, data: dict):
    db.collection('products').document(product_id).set(data)


async def delete_product(product_id: str):
    db.collection('products').document(product_id).delete()


def user_owns_product(product: dict, discord_id: str) -> bool:
    owners = product.get('owners')
    return isinstance(owners, list) and discord_id in owners


async def give_product_to_user(product_id: str, discord_id: str):
    batch = db.batch()
    batch.set(
        db.collection('products').document(product_id),
        {'owners': ArrayUnion([discord_id])},
        merge=True,
    )
    batch.set(
        db.collection('verifiedUsers').document(discord_id),
        {'ownedProducts': ArrayUnion([product_id])},
        merge=True,
    )
    batch.commit()


async def revoke_product_from_user(product_id: str, discord_id: str):
    batch = db.batch()
    batch.set(
        db.collection('products').document(product_id),
        {'owners': ArrayRemove([discord_id])},
        merge=True,
    )
    batch.set(
        db.collection('verifiedUsers').document(discord_id),
        {'ownedProducts': ArrayRemove([product_id])},
        merge=True,
    )
    batch.commit()


async def get_products_by_ids(product_ids: list[str]):
    """Fetches multiple products by id in one round trip. Missing/deleted
    product ids are silently skipped.
    """
    if not product_ids:
        return []
    refs = [db.collection('products').document(pid) for pid in product_ids]
    docs = db.get_all(refs)
    return [{'id': doc.id, **doc.to_dict()} for doc in docs if doc.exists]


_URL_RE = re.compile(r'^https?://', re.IGNORECASE)


def build_product_delivery_dm(product: dict):
    """Builds the DM payload used to deliver a purchased/owned product to a
    user. Returns kwargs suitable for `discord.abc.Messageable.send(**kwargs)`.
    """
    embed = discord.Embed(
        title=product['name'],
        color=0x00B0F4,
        description='Here is your product, click the button below to download the file.',
    )

    file_link = product.get('fileLink', '') or ''
    button_ok = bool(_URL_RE.match(file_link))

    if product.get('tutorialLink'):
        embed.add_field(name='Tutorial', value=product['tutorialLink'], inline=False)

    view = None
    if button_ok:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='Download', style=discord.ButtonStyle.link, url=file_link))
    else:
        # fileLink wasn't a valid URL for a Link button -- show it as text.
        embed.add_field(name='Link File', value=file_link, inline=False)

    return {'embed': embed, 'view': view}
