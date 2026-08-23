"""Product rating/review helpers.

New feature, not ported from JS. Stores reviews as subdoc-per-user under
`products/{productId}/reviews/{discordId}` (one review per user per product,
re-rating overwrites), and keeps a rolling aggregate (avg + count) cached
directly on the product doc so `/product view` / forum posts don't need to
fan out a subcollection read.

`testimonyCount` is a SEPARATE manually-editable counter on the product doc
(admin can set it by hand, e.g. to backfill legacy testimonials that predate
this feature) -- it is NOT the same number as `reviewCount` (which is the
live count of `/product rating` submissions). Both are shown side by side.
"""
import time

from utils.firebase import db

RATING_MIN = 1
RATING_MAX = 10


def _now_ms():
    return int(time.time() * 1000)


def _reviews_col(product_id: str):
    return db.collection('products').document(product_id).collection('reviews')


async def get_user_review(product_id: str, discord_id: str):
    doc = _reviews_col(product_id).document(discord_id).get()
    if not doc.exists:
        return None
    return {'id': doc.id, **doc.to_dict()}


async def list_reviews(product_id: str, limit: int = 25):
    """Most recent reviews first."""
    query = _reviews_col(product_id).order_by('createdAt', direction='DESCENDING').limit(limit)
    return [{'id': doc.id, **doc.to_dict()} for doc in query.stream()]


async def _recompute_aggregate(product_id: str):
    """Recomputes avg rating + live review count from the reviews
    subcollection and caches it onto the parent product doc. Called after
    every write/delete so cached fields never drift.
    """
    docs = list(_reviews_col(product_id).stream())
    count = len(docs)
    avg = round(sum(d.to_dict().get('rating', 0) for d in docs) / count, 2) if count else 0

    db.collection('products').document(product_id).set(
        {'reviewCount': count, 'reviewAvg': avg}, merge=True
    )
    return {'reviewCount': count, 'reviewAvg': avg}


async def submit_review(product_id: str, discord_id: str, discord_name: str, rating: int, reason: str, ticket_channel_id: str | None = None):
    """Creates or overwrites a user's review for a product (one per user).
    Returns the updated aggregate.
    """
    rating = max(RATING_MIN, min(RATING_MAX, int(rating)))

    existing = await get_user_review(product_id, discord_id)
    payload = {
        'discordId': discord_id,
        'discordName': discord_name,
        'rating': rating,
        'reason': reason.strip(),
        'ticketChannelId': ticket_channel_id,
        'updatedAt': _now_ms(),
        'createdAt': (existing or {}).get('createdAt') or _now_ms(),
    }
    _reviews_col(product_id).document(discord_id).set(payload)

    aggregate = await _recompute_aggregate(product_id)
    return {'review': payload, 'wasUpdate': bool(existing), **aggregate}


async def delete_review(product_id: str, discord_id: str):
    _reviews_col(product_id).document(discord_id).delete()
    return await _recompute_aggregate(product_id)


async def set_testimony_count(guild_id: str, count: int):
    """Manually override the display-only testimony count for a guild.
    Independent from reviewCount (the live /product rating tally per
    product) -- lets an admin backfill or correct the number shown
    alongside real reviews (e.g. legacy testimonials from before this
    feature existed). Stored on guildConfig, not per-product.
    """
    count = max(0, int(count))
    db.collection('guildConfig').document(guild_id).set({'testimonyCount': count}, merge=True)
    return count


async def get_testimony_count(guild_id: str) -> int:
    doc = db.collection('guildConfig').document(guild_id).get()
    if not doc.exists:
        return 0
    return (doc.to_dict() or {}).get('testimonyCount') or 0


def stars_bar(rating_out_of_10: float, slots: int = 10) -> str:
    """Renders a filled/empty block bar, e.g. 7/10 -> '███████░░░'."""
    filled = round(max(0, min(slots, rating_out_of_10)))
    return '█' * filled + '░' * (slots - filled)
