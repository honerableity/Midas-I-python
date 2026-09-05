"""Product catalog helpers: types, forums, ownership, delivery DM."""
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


async def list_products_by_guild(guild_id: str):
    """All products in a guild, across every type. Used for admin-facing
    autocomplete (edit/delete/give/revoke/sendpost) where any product could
    be the target, not just ones the invoking user owns.
    """
    query = db.collection('products').where('guildId', '==', guild_id)
    return [{'id': doc.id, **doc.to_dict()} for doc in query.stream()]


async def save_product(product_id: str, data: dict):
    db.collection('products').document(product_id).set(data)


async def delete_product(product_id: str):
    db.collection('products').document(product_id).delete()


async def bump_product_version(product_id: str, changelog: str, updated_by: str) -> int:
    """Increments a product's version counter and appends a changelog entry.
    Does NOT touch stock/fileLink -- that's what /product setstock is for.
    Returns the new version number.
    """
    product = await get_product(product_id)
    if not product:
        raise ValueError('Product not found')

    new_version = product_version(product) + 1
    entry = {
        'version': new_version,
        'changelog': changelog,
        'updatedBy': updated_by,
        'updatedAt': _now_ms(),
    }
    history = list(product.get('versionHistory') or [])
    history.append(entry)

    db.collection('products').document(product_id).set(
        {'version': new_version, 'versionHistory': history}, merge=True
    )
    return new_version


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



def _stock_pool(product: dict) -> list[dict]:
    pool = product.get('stockPool')
    if pool:
        return pool
    legacy_link = product.get('fileLink')
    if legacy_link:
        return [{'fileLink': legacy_link, 'remaining': -1}]
    return []


def product_version(product: dict) -> int:
    return int(product.get('version') or 1)


def total_stock(product: dict) -> int | None:
    """Sum of remaining units across all batches, or None if any batch is
    infinite (there's then no meaningful finite total).
    """
    pool = _stock_pool(product)
    if not pool:
        return 0
    total = 0
    for batch in pool:
        remaining = batch.get('remaining', 0)
        if remaining == -1:
            return None
        total += max(0, remaining)
    return total


def stock_summary_text(product: dict) -> str:
    """Human-readable stock line for embeds, e.g. '23 tersedia (2 varian)'
    or 'Stok tidak terbatas'.
    """
    pool = _stock_pool(product)
    if not pool:
        return 'Habis'
    if any(b.get('remaining') == -1 for b in pool):
        return 'Stok tidak terbatas'
    total = total_stock(product) or 0
    if total <= 0:
        return 'Habis'
    variant_note = f' ({len(pool)} varian)' if len(pool) > 1 else ''
    return f'{total} tersedia{variant_note}'


def is_in_stock(product: dict) -> bool:
    pool = _stock_pool(product)
    if not pool:
        return False
    if any(b.get('remaining') == -1 for b in pool):
        return True
    return (total_stock(product) or 0) > 0


async def add_stock_batch(product_id: str, file_link: str, quantity: int | None):
    """Appends a new batch to the product's stock pool. quantity=None means
    infinite (remaining=-1). Reads-modifies-writes rather than using
    ArrayUnion so total_stock() sees a consistent pool immediately after.
    """
    product = await get_product(product_id)
    if not product:
        raise ValueError('Product not found')

    pool = list(_stock_pool(product))
    pool.append({
        'fileLink': file_link,
        'remaining': -1 if quantity is None else max(0, quantity),
        'addedAt': _now_ms(),
    })
    db.collection('products').document(product_id).set(
        {'stockPool': pool, 'fileLink': file_link}, merge=True
    )
    return pool


def _pick_batch_index(pool: list[dict]) -> int | None:
    """Picks a random in-stock batch index, weighted so every unit of stock
    (not every batch) has an equal chance -- an infinite batch always wins
    since it never runs out, matching "unlimited stock" semantics.
    """
    import random

    infinite_indices = [i for i, b in enumerate(pool) if b.get('remaining') == -1]
    if infinite_indices:
        return random.choice(infinite_indices)

    weighted = [(i, b.get('remaining', 0)) for i, b in enumerate(pool) if b.get('remaining', 0) > 0]
    if not weighted:
        return None
    total = sum(w for _, w in weighted)
    r = random.randint(1, total)
    upto = 0
    for i, w in weighted:
        upto += w
        if r <= upto:
            return i
    return weighted[-1][0]


async def draw_stock_unit(product_id: str) -> str | None:
    """Atomically claims one unit of stock and returns its fileLink, or
    None if nothing is left. Runs as a Firestore transaction so two buyers
    can't both be handed the last unit of a finite batch.
    """
    from google.cloud.firestore_v1.transaction import Transaction, transactional

    ref = db.collection('products').document(product_id)

    @transactional
    def _txn(transaction: Transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        product = {'id': snap.id, **snap.to_dict()}
        pool = list(_stock_pool(product))
        idx = _pick_batch_index(pool)
        if idx is None:
            return None

        chosen_link = pool[idx]['fileLink']
        if pool[idx].get('remaining', -1) != -1:
            pool[idx] = {**pool[idx], 'remaining': pool[idx]['remaining'] - 1}

        transaction.set(ref, {'stockPool': pool}, merge=True)
        return chosen_link

    transaction = db.transaction()
    return _txn(transaction)


_URL_RE = re.compile(r'^https?://', re.IGNORECASE)


def build_product_delivery_dm(product: dict, file_link: str | None = None):
    """Builds the DM payload used to deliver a purchased/owned product to a
    user. Returns kwargs suitable for `discord.abc.Messageable.send(**kwargs)`.

    file_link overrides which link is sent -- used when the product has a
    stock pool with several batches (see draw_stock_unit above) so the
    buyer gets the specific unit that was drawn for them, not just
    whatever the top-level fileLink field happens to hold.
    """
    embed = discord.Embed(
        title=product['name'],
        color=0x00B0F4,
        description='Here is your product, click the button below to download the file.',
    )
    if product.get('version'):
        embed.set_footer(text=f"v{product['version']}")

    file_link = file_link if file_link is not None else (product.get('fileLink', '') or '')
    button_ok = bool(_URL_RE.match(file_link))

    if product.get('tutorialLink'):
        embed.add_field(name='Tutorial', value=product['tutorialLink'], inline=False)

    view = None
    if button_ok:
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='Download', style=discord.ButtonStyle.link, url=file_link))
    else:
        embed.add_field(name='Link File', value=file_link, inline=False)

    return {'embed': embed, 'view': view}


def build_rating_embed(product: dict, rating: int, reason: str, reviewer_name: str):
    """Embed posted into the product's own forum thread (or the guild's
    reviews channel) whenever someone submits a /product rating. Separate
    from the main sendpost listing embed. Always names the product being
    reviewed so the embed makes sense on its own, wherever it lands.
    """
    from utils.reviews import stars_bar

    color = 0x57F287 if rating >= 7 else (0xFEE75C if rating >= 4 else 0xED4245)
    embed = discord.Embed(
        title=f'⭐ New Rating: {rating}/10',
        description=reason,
        color=color,
    )
    embed.add_field(name='Produk', value=product['name'], inline=False)
    embed.add_field(name='Score', value=f'{stars_bar(rating)}  **{rating}/10**', inline=False)
    embed.set_footer(text=f'Reviewed by {reviewer_name}')
    return embed
