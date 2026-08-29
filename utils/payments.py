"""Payment gateway integration for /product buy.

Wraps tokoshopp.web.id (ARTAN SHOP), https://tokoshopp.web.id/api-docs.html.

This provider is new and unverified compared to more established options --
start with small real transactions and watch a handful of them complete
before relying on it for volume. confirm_payment() below never trusts a
webhook body by itself; it always re-queries the provider's own Check
Status endpoint first.

Env vars required:
    TOKOSHOPP_API_KEY -- API key from the ARTAN SHOP dashboard's API Key menu

Unlike some gateways, ARTAN SHOP's Check Status endpoint is keyed by
*their* transaction_id, not the order_id we chose -- so we store their
transaction_id on our own order record the moment Create Payment returns
it, and use that for every later lookup.
"""
import os
import time

import aiohttp

from utils.firebase import db

TOKOSHOPP_BASE = 'https://tokoshopp.web.id'
TOKOSHOPP_API_KEY = os.getenv('TOKOSHOPP_API_KEY')

ORDER_EXPIRY_MS = 30 * 60 * 1000


class PaymentGatewayError(Exception):
    """Raised on any non-2xx / malformed response from a gateway."""


def _now_ms():
    return int(time.time() * 1000)


def pakasir_configured() -> bool:
    """Name kept for compatibility with commands/product.py and
    utils/webhook_server.py -- despite the name, this now reports whether
    the active gateway (tokoshopp/ARTAN SHOP) is configured.
    """
    return bool(TOKOSHOPP_API_KEY)


def _headers() -> dict:
    return {'Content-Type': 'application/json', 'x-api-key': TOKOSHOPP_API_KEY or ''}


async def create_qris_payment(order_id: str, amount: int) -> dict:
    """Creates a QRIS transaction on ARTAN SHOP. Returns the raw response
    dict: {success, transaction_id, status, qr_image (base64 PNG data URI)}.
    """
    if not pakasir_configured():
        raise PaymentGatewayError('TOKOSHOPP_API_KEY not configured.')

    url = f'{TOKOSHOPP_BASE}/api/payment/create'
    body = {'amount': amount, 'order_id': order_id}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=_headers()) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get('success'):
                raise PaymentGatewayError(f'ARTAN SHOP payment/create failed ({resp.status}): {data}')
            return data


async def get_transaction_status(transaction_id: str) -> dict | None:
    """Polls ARTAN SHOP directly for a transaction's true status -- used
    both as a fallback if the webhook never arrives and to double-check
    every webhook payload before trusting it. Returns the response dict,
    or None if not found yet.
    """
    if not pakasir_configured():
        raise PaymentGatewayError('TOKOSHOPP_API_KEY not configured.')

    url = f'{TOKOSHOPP_BASE}/api/payment/status'
    body = {'transaction_id': transaction_id}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers=_headers()) as resp:
            if resp.status == 404:
                return None
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get('success'):
                if resp.status == 404:
                    return None
                raise PaymentGatewayError(f'ARTAN SHOP payment/status failed ({resp.status}): {data}')
            return data


async def cancel_transaction(order_id: str, amount: int) -> None:
    """ARTAN SHOP's docs don't expose a cancel endpoint -- transactions
    just expire on their own. Kept as a no-op so callers (and any future
    admin tooling) don't need a gateway-specific branch.
    """
    return None


async def create_payment_order(*, guild_id: str, channel_id: str, buyer_id: str,
                                product_id: str, product_name: str, amount: int) -> dict:
    order_id = f'{channel_id}-{_now_ms()}'
    payment = await create_qris_payment(order_id, amount)

    record = {
        'orderId': order_id,
        'gatewayTransactionId': payment.get('transaction_id'),
        'guildId': guild_id,
        'channelId': channel_id,
        'buyerId': buyer_id,
        'productId': product_id,
        'productName': product_name,
        'amount': amount,
        'gateway': 'tokoshopp',
        'status': 'pending',
        'qrImageDataUri': payment.get('qr_image'),
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


async def find_order_by_gateway_transaction_id(transaction_id: str) -> dict | None:
    """Used by the webhook handler, whose payload only carries ARTAN
    SHOP's transaction_id, not our own order_id.
    """
    query = (
        db.collection('paymentOrders')
        .where('gatewayTransactionId', '==', transaction_id)
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


async def mark_order_completed(order_id: str):
    db.collection('paymentOrders').document(order_id).set(
        {'status': 'completed', 'completedAt': _now_ms()}, merge=True
    )


async def mark_order_status(order_id: str, status: str):
    db.collection('paymentOrders').document(order_id).set({'status': status}, merge=True)


async def confirm_payment(order_id: str) -> dict | None:
    """Double-checks a webhook (or a manual /product buy status poll)
    directly against ARTAN SHOP before treating a payment as real. Returns
    the updated order record if it just got confirmed, else None.
    """
    order = await get_payment_order(order_id)
    if not order or order['status'] != 'pending':
        return None

    transaction_id = order.get('gatewayTransactionId')
    if not transaction_id:
        return None

    txn = await get_transaction_status(transaction_id)
    if not txn or txn.get('status') != 'paid':
        return None

    if int(txn.get('amount', -1)) != int(order['amount']):
        raise PaymentGatewayError(
            f"Amount mismatch on {order_id}: expected {order['amount']}, got {txn.get('amount')}"
        )

    await mark_order_completed(order_id)
    order['status'] = 'completed'
    return order
