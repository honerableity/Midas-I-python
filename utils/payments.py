"""Payment gateway integration for /product buy.

Currently wraps Pakasir (https://pakasir.com/p/docs) behind a small
gateway-agnostic interface so a second provider (e.g. an Indonesian
gateway like tokoshopp.web.id) can be dropped in later without touching
commands/product.py or commands/ticket.py.

Env vars required:
    PAKASIR_PROJECT   -- project slug from the Pakasir dashboard
    PAKASIR_API_KEY   -- API key from the Pakasir project's detail page

Order id convention: we always use the Discord order document id
("orders/{orderId}" in Firestore) as Pakasir's order_id, and the order's
locked total (in whole Rupiah, no decimals) as amount. Pakasir keys a
transaction by the (project, order_id, amount) triple, so both must be
sent back unchanged on every lookup/cancel call.
"""
import os
import time

import aiohttp

from utils.firebase import db

PAKASIR_BASE = 'https://app.pakasir.com'
PAKASIR_PROJECT = os.getenv('PAKASIR_PROJECT')
PAKASIR_API_KEY = os.getenv('PAKASIR_API_KEY')

ORDER_EXPIRY_MS = 30 * 60 * 1000


class PaymentGatewayError(Exception):
    """Raised on any non-2xx / malformed response from a gateway."""


def _now_ms():
    return int(time.time() * 1000)


def pakasir_configured() -> bool:
    return bool(PAKASIR_PROJECT and PAKASIR_API_KEY)



async def create_qris_payment(order_id: str, amount: int) -> dict:
    """Creates a QRIS transaction on Pakasir. Returns the raw `payment` dict:
    {project, order_id, amount, fee, total_payment, payment_method,
     payment_number (the QRIS string), expired_at}.
    """
    if not pakasir_configured():
        raise PaymentGatewayError('PAKASIR_PROJECT / PAKASIR_API_KEY not configured.')

    url = f'{PAKASIR_BASE}/api/transactioncreate/qris'
    body = {
        'project': PAKASIR_PROJECT,
        'order_id': order_id,
        'amount': amount,
        'api_key': PAKASIR_API_KEY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or 'payment' not in data:
                raise PaymentGatewayError(f'Pakasir transactioncreate failed ({resp.status}): {data}')
            return data['payment']


async def get_transaction_status(order_id: str, amount: int) -> dict | None:
    """Polls Pakasir directly for a transaction's true status -- used both
    as a fallback if the webhook never arrives and to double-check every
    webhook payload before trusting it (Pakasir's own docs recommend this;
    webhook bodies alone aren't signed, so treat them as a hint, not proof).
    Returns the `transaction` dict, or None if not found yet.
    """
    if not pakasir_configured():
        raise PaymentGatewayError('PAKASIR_PROJECT / PAKASIR_API_KEY not configured.')

    url = (
        f'{PAKASIR_BASE}/api/transactiondetail'
        f'?project={PAKASIR_PROJECT}&amount={amount}&order_id={order_id}&api_key={PAKASIR_API_KEY}'
    )
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 404:
                return None
            data = await resp.json(content_type=None)
            if resp.status != 200 or 'transaction' not in data:
                raise PaymentGatewayError(f'Pakasir transactiondetail failed ({resp.status}): {data}')
            return data['transaction']


async def cancel_transaction(order_id: str, amount: int) -> None:
    if not pakasir_configured():
        return
    url = f'{PAKASIR_BASE}/api/transactioncancel'
    body = {
        'project': PAKASIR_PROJECT,
        'order_id': order_id,
        'amount': amount,
        'api_key': PAKASIR_API_KEY,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body):
                pass
    except Exception as err:
        print(f'[payments] transactioncancel failed for {order_id}: {err}')



async def create_payment_order(*, guild_id: str, channel_id: str, buyer_id: str,
                                product_id: str, product_name: str, amount: int) -> dict:
    order_id = f'{channel_id}-{_now_ms()}'
    payment = await create_qris_payment(order_id, amount)

    record = {
        'orderId': order_id,
        'guildId': guild_id,
        'channelId': channel_id,
        'buyerId': buyer_id,
        'productId': product_id,
        'productName': product_name,
        'amount': amount,
        'gateway': 'pakasir',
        'status': 'pending',
        'qrisString': payment.get('payment_number'),
        'expiredAt': payment.get('expired_at'),
        'createdAt': _now_ms(),
    }
    db.collection('paymentOrders').document(order_id).set(record)
    return record


async def get_payment_order(order_id: str) -> dict | None:
    doc = db.collection('paymentOrders').document(order_id).get()
    if not doc.exists:
        return None
    return {'id': doc.id, **doc.to_dict()}


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


async def mark_order_completed(order_id: str):
    db.collection('paymentOrders').document(order_id).set(
        {'status': 'completed', 'completedAt': _now_ms()}, merge=True
    )


async def mark_order_status(order_id: str, status: str):
    db.collection('paymentOrders').document(order_id).set({'status': status}, merge=True)


async def confirm_payment(order_id: str) -> dict | None:
    """Double-checks a webhook (or a manual /product buy status poll)
    directly against Pakasir before treating a payment as real. Returns
    the updated order record if it just got confirmed, else None.
    """
    order = await get_payment_order(order_id)
    if not order or order['status'] != 'pending':
        return None

    txn = await get_transaction_status(order_id, order['amount'])
    if not txn or txn.get('status') != 'completed':
        return None

    if int(txn.get('amount', -1)) != int(order['amount']):
        raise PaymentGatewayError(
            f"Amount mismatch on {order_id}: expected {order['amount']}, got {txn.get('amount')}"
        )

    await mark_order_completed(order_id)
    order['status'] = 'completed'
    return order
