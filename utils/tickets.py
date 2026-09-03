"""Ticket system helpers: config, creation, locks, order selections.

Ported from utils/tickets.js.

Ticket doc shape (collection "tickets", doc id = channel id):
{
  guildId, channelId, category: 'order'|'service'|'customerservice',
  creatorId, status: 'open'|'done'|'deleted',
  products: [{ productId, name, price, qty }],  # order only
  total: number,                                 # order only
  serviceAnswer: string,                          # service only
  ticketNumber: number,
  createdAt, closedAt,
}
"""
import time

from google.cloud.firestore_v1.transaction import Transaction

from utils.firebase import db

CREATE_LOCK_MS = 15 * 1000
SELECTION_TTL_MS = 15 * 60 * 1000


def _now_ms():
    return int(time.time() * 1000)


async def get_guild_config(guild_id: str):
    snap = db.collection('guildConfig').document(guild_id).get()
    return snap.to_dict() if snap.exists else {}


async def set_testi_channel(guild_id: str, channel_id: str):
    db.collection('guildConfig').document(guild_id).set({'testiChannelId': channel_id}, merge=True)


async def get_testi_channel(guild_id: str):
    cfg = await get_guild_config(guild_id)
    return cfg.get('testiChannelId')


async def set_ticket_categories(guild_id: str, category_ids: dict):
    db.collection('guildConfig').document(guild_id).set({'ticketCategories': category_ids}, merge=True)


async def get_ticket_categories(guild_id: str):
    cfg = await get_guild_config(guild_id)
    return cfg.get('ticketCategories')


async def next_ticket_number(guild_id: str):
    """Atomic counter for testimonial numbering, per guild. Firestore
    transaction so two /ticket done calls racing each other can't grab the
    same number.
    """
    ref = db.collection('guildConfig').document(guild_id)

    @firestore_transactional
    def _txn(transaction: Transaction):
        doc = ref.get(transaction=transaction)
        current = (doc.to_dict().get('testiCounter') or 0) if doc.exists else 0
        nxt = current + 1
        transaction.set(ref, {'testiCounter': nxt}, merge=True)
        return nxt

    transaction = db.transaction()
    return _txn(transaction)


async def create_ticket(data: dict):
    channel_id = data['channelId']
    payload = {**data, 'status': 'open', 'createdAt': _now_ms()}
    db.collection('tickets').document(channel_id).set(payload)


async def get_ticket(channel_id: str):
    doc = db.collection('tickets').document(channel_id).get()
    if not doc.exists:
        return None
    return {'id': doc.id, **doc.to_dict()}


async def find_open_ticket(guild_id: str, creator_id: str, category: str):
    """One open ticket per category per user. Filtering category in Python
    instead of adding it to the Firestore query keeps this to a 2-field
    equality query (creatorId + status), which doesn't need a composite
    index.
    """
    query = (
        db.collection('tickets')
        .where('creatorId', '==', creator_id)
        .where('status', '==', 'open')
    )
    for doc in query.stream():
        data = doc.to_dict()
        if data.get('guildId') == guild_id and data.get('category') == category:
            return {'id': doc.id, **data}
    return None


async def close_ticket(channel_id: str, extra: dict | None = None):
    payload = {'status': 'done', 'closedAt': _now_ms(), **(extra or {})}
    db.collection('tickets').document(channel_id).set(payload, merge=True)


async def mark_ticket_deleted(channel_id: str):
    db.collection('tickets').document(channel_id).set(
        {'status': 'deleted', 'deletedAt': _now_ms()}, merge=True
    )


def firestore_transactional(func):
    """Thin wrapper around google.cloud.firestore's @transactional decorator,
    imported lazily to keep the top-level import list tidy.
    """
    from google.cloud.firestore_v1.transaction import transactional
    return transactional(func)


async def claim_ticket_create_lock(user_id: str, category: str) -> bool:
    """Double-click failsafe for ticket creation. Claims a short-lived
    per-user-per-category lock via Firestore transaction: whichever call
    transacts first wins and proceeds, the second sees the lock already held.
    """
    ref = db.collection('ticketCreateLocks').document(f'{user_id}_{category}')

    @firestore_transactional
    def _txn(transaction: Transaction):
        doc = ref.get(transaction=transaction)
        now = _now_ms()
        if doc.exists and (now - (doc.to_dict().get('lockedAt') or 0)) < CREATE_LOCK_MS:
            return False  # someone else (or an earlier click) holds the lock
        transaction.set(ref, {'lockedAt': now})
        return True

    transaction = db.transaction()
    return _txn(transaction)


async def release_ticket_create_lock(user_id: str, category: str):
    try:
        db.collection('ticketCreateLocks').document(f'{user_id}_{category}').delete()
    except Exception:  # noqa: BLE001
        pass


async def save_order_selection(token: str, user_id: str, product_ids: list[str]):
    db.collection('orderSelections').document(token).set({
        'userId': user_id,
        'productIds': product_ids,
        'createdAt': _now_ms(),
    })


async def get_order_selection(token: str):
    doc = db.collection('orderSelections').document(token).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    if _now_ms() - (data.get('createdAt') or 0) > SELECTION_TTL_MS:
        return None
    return data
