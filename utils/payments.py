"""Payment gateway integration for /product buy -- RamaShop only.

Base URL: https://ramashop.my.id/api/public
Auth: X-API-Key header (RAMASHOP_API_KEY env var), no login step needed.

Endpoints used:
  GET  /balance              -> {success, data: {balance, username, email}}
  POST /deposit/create       -> {success, data: {depositId, amount,
                                 uniqueCode, totalAmount, fee, getBalance,
                                 qrImage, qrString, status, expiredAt},
                                 message}
  GET  /deposit/status/{id}  -> {success, data: {depositId, amount,
                                 totalAmount, status, createdAt}}

RamaShop matches a deposit by the exact transferred AMOUNT, not by any
order/transaction id we pass -- that's why /deposit/create adds a random
"unique code" on top of the product price (Rp1.000 -> Rp1.121) and why
every embed/message shown to the buyer must display totalAmount, never
the bare product price.

/deposit/status only ever returns a bare `status` field ('pending' or
'success') plus the same amount/totalAmount -- no paidAmount/paidAt, so
there's nothing to reconcile beyond that one field: pending -> tell the
buyer it's not paid yet, success -> run _deliver.

Env var:
    RAMASHOP_API_KEY -- from the RamaShop dashboard's API Key menu.
"""
import json
import os
import time

import aiohttp

from utils.firebase import db

BASE_URL = 'https://ramashop.my.id/api/public'
RAMASHOP_API_KEY = os.getenv('RAMASHOP_API_KEY')

ORDER_EXPIRY_MS = 30 * 60 * 1000

# A generic browser-like User-Agent -- doesn't affect correctness (Postman's
# plain PostmanRuntime UA works fine per real testing, confirmed 200 OK on
# all 3 endpoints), kept just so requests don't look like a bare script.
_USER_AGENT = 'Mozilla/5.0 (compatible; MidasBot/1.0; +https://github.com/honerableity/midas-i-python)'


class PaymentGatewayError(Exception):
    """Raised on any non-2xx / malformed response from RamaShop."""


def _now_ms():
    return int(time.time() * 1000)


def pakasir_configured() -> bool:
    """Name kept for backwards compatibility with commands/product.py and
    commands/payment.py -- reports whether RAMASHOP_API_KEY is set.
    """
    return bool(RAMASHOP_API_KEY)


def _headers() -> dict:
    return {'X-API-Key': RAMASHOP_API_KEY or '', 'Content-Type': 'application/json', 'User-Agent': _USER_AGENT}


async def _read_json(resp: aiohttp.ClientResponse, *, context: str) -> dict:
    """Reads the response as raw text first instead of resp.json() directly
    -- RamaShop (or an intermediary like Cloudflare) can return a
    non-JSON body (empty, an HTML error page, a plain-text rate-limit
    message) on error responses, and parsing that straight as JSON
    crashes with an opaque "Expecting value: line 1 column 1" that hides
    what the server actually sent. Surfacing the real status+body here
    makes that failure diagnosable instead of a bare traceback.
    """
    raw = await resp.text()
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        raise PaymentGatewayError(f'{context} returned a non-JSON response ({resp.status}): {raw[:300]!r}')


async def get_account_balance() -> int:
    """GET /balance. Used by /payment balance."""
    if not pakasir_configured():
        raise PaymentGatewayError('RAMASHOP_API_KEY not configured.')

    url = f'{BASE_URL}/balance'
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_headers()) as resp:
            data = await _read_json(resp, context='RamaShop balance check')
            if resp.status != 200 or not data.get('success'):
                raise PaymentGatewayError(f'RamaShop balance check failed ({resp.status}): {data}')
            return int(data.get('data', {}).get('balance', 0))


async def create_qris_deposit(amount: int) -> dict:
    """POST /deposit/create. Returns the raw `data` dict: {depositId,
    amount, uniqueCode, totalAmount, fee, getBalance, qrImage, qrString,
    status, expiredAt}. totalAmount (amount + uniqueCode) is what the
    buyer must actually transfer -- callers must display totalAmount, not
    amount.
    """
    if not pakasir_configured():
        raise PaymentGatewayError('RAMASHOP_API_KEY not configured.')

    url = f'{BASE_URL}/deposit/create'
    body = {'amount': amount}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=_headers()) as resp:
            data = await _read_json(resp, context='RamaShop deposit/create')
            if resp.status != 200 or not data.get('success'):
                raise PaymentGatewayError(f'RamaShop deposit/create failed ({resp.status}): {data}')
            return data.get('data', {})


async def get_deposit_status(deposit_id: str) -> dict | None:
    """GET /deposit/status/{deposit_id}. Returns the raw `data` dict
    ({depositId, amount, totalAmount, status, createdAt}), or None if the
    deposit doesn't exist. `status` is only ever 'pending' or 'success' --
    there's no paidAmount/paidAt to reconcile against, so callers just
    branch on that single field.
    """
    if not pakasir_configured():
        raise PaymentGatewayError('RAMASHOP_API_KEY not configured.')

    url = f'{BASE_URL}/deposit/status/{deposit_id}'
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_headers()) as resp:
            if resp.status == 404:
                return None
            data = await _read_json(resp, context='RamaShop deposit/status')
            if resp.status != 200 or not data.get('success'):
                raise PaymentGatewayError(f'RamaShop deposit/status failed ({resp.status}): {data}')
            return data.get('data', {})


async def cancel_transaction(order_id: str, amount: int) -> None:
    """RamaShop's docs don't expose a cancel endpoint -- deposits just
    expire on their own. Kept as a no-op so any caller/admin tooling
    doesn't need a special case.
    """
    return None


# ---------------------------------------------------------------------
# Gateway-agnostic-looking order records (Firestore) -- what
# commands/product.py actually talks to.
# ---------------------------------------------------------------------

async def create_payment_order(*, guild_id: str, channel_id: str, buyer_id: str,
                                product_id: str, product_name: str, amount: int) -> dict:
    order_id = f'{channel_id}-{_now_ms()}'

    deposit = await create_qris_deposit(amount)

    record = {
        'orderId': order_id,
        'gatewayDepositId': deposit.get('depositId'),
        'guildId': guild_id,
        'channelId': channel_id,
        'buyerId': buyer_id,
        'productId': product_id,
        'productName': product_name,
        # amount is the bare product price; totalAmount (amount + unique
        # code) is what the buyer must actually transfer. Every
        # embed/message shown to the buyer must display totalAmount,
        # never the bare amount.
        'amount': amount,
        'uniqueCode': deposit.get('uniqueCode'),
        'totalAmount': deposit.get('totalAmount', amount),
        'status': 'pending',
        'qrImageUrl': deposit.get('qrImage'),
        'qrString': deposit.get('qrString'),
        'createdAt': _now_ms(),
        'expiredAt': _now_ms() + ORDER_EXPIRY_MS,
    }
    db.collection('paymentOrders').document(order_id).set(record)
    return record


async def get_payment_order(order_id: str) -> dict | None:
    doc = db.collection('paymentOrders').document(order_id).get()
    if not doc.exists:
        return None
    return {'id': doc.id, **doc.to_dict()}


async def find_order_by_gateway_deposit_id(deposit_id: str) -> dict | None:
    query = (
        db.collection('paymentOrders')
        .where('gatewayDepositId', '==', deposit_id)
        .limit(1)
    )
    docs = list(query.stream())
    if not docs:
        return None
    return {'id': docs[0].id, **docs[0].to_dict()}


async def find_pending_order_for_channel(channel_id: str) -> dict | None:
    query = (
        db.collection('paymentOrders')
        .where('channelId', '==', channel_id)
        .where('status', '==', 'pending')
        .limit(1)
    )
    docs = list(query.stream())
    if not docs:
        return None
    return {'id': docs[0].id, **docs[0].to_dict()}


async def find_latest_order_for_channel(channel_id: str) -> dict | None:
    """Like find_pending_order_for_channel but also matches orders already
    marked 'completed'. Used by /product forcecheck so it can still find
    (and re-sync the embed for) an order that got confirmed by another
    path -- e.g. the poller or the button -- moments before forcecheck ran,
    instead of reporting 'no pending payment' just because the status
    field had already moved on.

    Runs two single-field queries instead of one 'in' + order_by query, to
    avoid depending on a composite Firestore index that may not exist.
    """
    candidates = []
    for status in ('pending', 'completed'):
        query = (
            db.collection('paymentOrders')
            .where('channelId', '==', channel_id)
            .where('status', '==', status)
            .limit(5)
        )
        candidates.extend({'id': doc.id, **doc.to_dict()} for doc in query.stream())

    if not candidates:
        return None
    return max(candidates, key=lambda o: o.get('createdAt', 0))


async def find_all_pending_orders() -> list[dict]:
    """Used on bot startup to resume in-memory polling for orders that
    were still 'pending' when the process died/restarted. Firestore is
    the source of truth, so a restart mid-payment never loses the order --
    only the in-memory QRISPaymentView + poll loop need recreating.
    Already-expired orders (past expiredAt) are skipped; the caller should
    mark those 'expired' instead of resuming them.
    """
    query = db.collection('paymentOrders').where('status', '==', 'pending')
    docs = list(query.stream())
    return [{'id': doc.id, **doc.to_dict()} for doc in docs]


async def mark_order_completed(order_id: str):
    db.collection('paymentOrders').document(order_id).set(
        {'status': 'completed', 'completedAt': _now_ms()}, merge=True
    )


async def mark_order_status(order_id: str, status: str):
    db.collection('paymentOrders').document(order_id).set({'status': status}, merge=True)


async def set_order_message(order_id: str, message_id: str):
    """Stores the Discord message ID for the QR-code embed so a stale
    embed can be found and updated later even without the in-memory
    QRISPaymentView -- e.g. after a bot restart, or when payment is
    confirmed via /product forcecheck instead of the button/poller.
    """
    db.collection('paymentOrders').document(order_id).set({'messageId': message_id}, merge=True)


async def confirm_payment(order_id: str) -> dict | None:
    """Double-checks a manual /product buy status poll directly against
    RamaShop's own /deposit/status before treating a payment as real.
    Returns the order record if it's paid (whether just confirmed now, or
    already marked 'completed' earlier -- e.g. a previous check delivered
    the product but the bot restarted/lost its in-memory view before it
    could update the embed). Returns None only for orders that are still
    genuinely pending or don't exist.
    """
    order = await get_payment_order(order_id)
    if not order:
        return None

    if order['status'] == 'completed':
        # Already confirmed earlier. Caller (_deliver) is idempotent --
        # give_product_to_user/auto_whitelist_product_for_user use
        # ArrayUnion and are safe to re-run, so this just lets a stale
        # embed/button catch up to what Firestore already knows.
        return order

    if order['status'] != 'pending':
        return None

    deposit_id = order.get('gatewayDepositId')
    if not deposit_id:
        return None

    status_data = await get_deposit_status(deposit_id)
    if not status_data:
        return None

    status = str(status_data.get('status', '')).strip().lower()
    if status != 'success':
        print(f'[confirm_payment] {order_id}: RamaShop status is {status_data.get("status")!r}, not yet paid.')
        return None

    await mark_order_completed(order_id)
    order['status'] = 'completed'
    return order
