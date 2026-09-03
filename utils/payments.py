"""Payment gateway integration for /product buy -- Tokoshopp API.

Base URL: https://tokoshopp.web.id
Auth: `x-api-key` header using the `TOKOSHOPP_API_KEY` environment variable.

Payment endpoints:
  POST /api/payment/create
      {amount, order_id} -> {success, transaction_id, status, qr_image}
  POST /api/payment/status
      {transaction_id} -> {success, transaction_id, status, amount, paid_at}

Tokoshopp returns the QR as a data URI (for example
`data:image/png;base64,...`). The Discord checkout code converts that
payload to an attachment so it can be displayed in an embed.

Environment variable:
    TOKOSHOPP_API_KEY -- API key from the Tokoshopp/ARTAN SHOP dashboard.
"""
import json
import os
import time

import aiohttp

from utils.firebase import db

BASE_URL = 'https://tokoshopp.web.id'
TOKOSHOPP_API_KEY = os.getenv('TOKOSHOPP_API_KEY')

ORDER_EXPIRY_MS = 30 * 60 * 1000

# A generic browser-like User-Agent so requests do not look like a bare script.
_USER_AGENT = 'Mozilla/5.0 (compatible; MidasBot/1.0; +https://github.com/honerableity/midas-i-python)'


class PaymentGatewayError(Exception):
    """Raised on any non-2xx / malformed response from Tokoshopp."""


def _now_ms():
    return int(time.time() * 1000)


def tokoshopp_configured() -> bool:
    """Whether the Tokoshopp API key is configured."""
    return bool(TOKOSHOPP_API_KEY)


# Kept for backwards compatibility with existing command imports.
def pakasir_configured() -> bool:
    return tokoshopp_configured()


def _headers() -> dict:
    return {
        'x-api-key': TOKOSHOPP_API_KEY or '',
        'Content-Type': 'application/json',
        'User-Agent': _USER_AGENT,
    }


async def _read_json(resp: aiohttp.ClientResponse, *, context: str) -> dict:
    """Read raw text first so non-JSON gateway errors remain diagnosable."""
    raw = await resp.text()
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        raise PaymentGatewayError(
            f'{context} returned a non-JSON response ({resp.status}): {raw[:300]!r}'
        )


async def create_qris_deposit(amount: int, order_id: str) -> dict:
    """POST /api/payment/create and return the raw response payload."""
    if not tokoshopp_configured():
        raise PaymentGatewayError('TOKOSHOPP_API_KEY not configured.')

    url = f'{BASE_URL}/api/payment/create'
    body = {'amount': int(amount), 'order_id': str(order_id)}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=_headers()) as resp:
            data = await _read_json(resp, context='Tokoshopp payment/create')
            if resp.status != 200 or not data.get('success'):
                raise PaymentGatewayError(
                    f'Tokoshopp payment/create failed ({resp.status}): {data}'
                )
            return data


async def get_deposit_status(transaction_id: str) -> dict | None:
    """POST /api/payment/status using Tokoshopp's transaction_id."""
    if not tokoshopp_configured():
        raise PaymentGatewayError('TOKOSHOPP_API_KEY not configured.')

    url = f'{BASE_URL}/api/payment/status'
    body = {'transaction_id': str(transaction_id)}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=_headers()) as resp:
            if resp.status == 404:
                return None
            data = await _read_json(resp, context='Tokoshopp payment/status')
            if resp.status != 200 or not data.get('success'):
                raise PaymentGatewayError(
                    f'Tokoshopp payment/status failed ({resp.status}): {data}'
                )
            return data


async def cancel_transaction(order_id: str, amount: int) -> None:
    """Tokoshopp's payment docs do not expose a cancellation endpoint."""
    return None



# ---------------------------------------------------------------------
# Gateway-agnostic-looking order records (Firestore) -- what
# commands/product.py actually talks to.
# ---------------------------------------------------------------------

async def create_payment_order(*, guild_id: str, channel_id: str, buyer_id: str,
                                product_id: str, product_name: str, amount: int) -> dict:
    order_id = f'{channel_id}-{_now_ms()}'

    payment = await create_qris_deposit(amount, order_id)
    transaction_id = payment.get('transaction_id')
    qr_image = payment.get('qr_image')
    if not transaction_id:
        raise PaymentGatewayError(f'Tokoshopp payment/create returned no transaction_id: {payment}')
    if not qr_image:
        raise PaymentGatewayError(f'Tokoshopp payment/create returned no qr_image: {payment}')

    record = {
        'orderId': order_id,
        'gatewayDepositId': transaction_id,  # legacy field name used throughout the bot
        'gatewayTransactionId': transaction_id,
        'guildId': guild_id,
        'channelId': channel_id,
        'buyerId': buyer_id,
        'productId': product_id,
        'productName': product_name,
        'amount': amount,
        'totalAmount': amount,
        'status': 'pending',
        'qrImageUrl': qr_image,  # data URI from Tokoshopp; rendered as an attachment by commands/product.py
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
    Tokoshopp's own /api/payment/status before treating a payment as real.
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
    if status not in ('paid', 'success'):
        print(f'[confirm_payment] {order_id}: Tokoshopp status is {status_data.get("status")!r}, not yet paid.')
        return None

    await mark_order_completed(order_id)
    order['status'] = 'completed'
    return order
