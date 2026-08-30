"""Payment gateway integration for /product buy.

Wraps tokoshopp.web.id (ARTAN SHOP), https://tokoshopp.web.id/api-docs.html.

This provider is new and unverified compared to more established options --
start with small real transactions and watch a handful of them complete
before relying on it for volume. confirm_payment() below never trusts a
webhook body by itself; it always re-queries the provider's own Check
Status endpoint first.

Env vars required:
    TOKOSHOPP_API_KEY   -- API key from the ARTAN SHOP dashboard's API Key menu,
                           used for Create Payment / Check Status (x-api-key header)
    TOKOSHOPP_USERNAME  -- ARTAN SHOP dashboard login, only needed for /withdraw
    TOKOSHOPP_PASSWORD  -- ARTAN SHOP dashboard login, only needed for /withdraw

Unlike some gateways, ARTAN SHOP's Check Status endpoint is keyed by
*their* transaction_id, not the order_id we chose -- so we store their
transaction_id on our own order record the moment Create Payment returns
it, and use that for every later lookup.

Withdraw (/withdraw command) uses a SEPARATE auth scheme from the rest of
this file: it acts on behalf of the ARTAN SHOP account itself, via a login
token from POST /login (username+password), not the x-api-key. That token
is short-lived-ish and account-wide, so it's cached in memory and never
logged or stored in Firestore.
"""
import os
import time

import aiohttp

from utils.firebase import db

TOKOSHOPP_BASE = 'https://tokoshopp.web.id'
TOKOSHOPP_API_KEY = os.getenv('TOKOSHOPP_API_KEY')
TOKOSHOPP_USERNAME = os.getenv('TOKOSHOPP_USERNAME')
TOKOSHOPP_PASSWORD = os.getenv('TOKOSHOPP_PASSWORD')

ORDER_EXPIRY_MS = 30 * 60 * 1000
WITHDRAW_MIN_AMOUNT = 3000
WITHDRAW_FEES = {'DANA': 500, 'OVO': 500, 'GOPAY': 500}

_login_token_cache: dict = {'token': None, 'username': None}


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


def withdraw_configured() -> bool:
    return bool(TOKOSHOPP_USERNAME and TOKOSHOPP_PASSWORD)


async def _login_for_token() -> str:
    """Logs into the ARTAN SHOP dashboard account and returns a fresh
    token. Cached in memory (_login_token_cache) so /withdraw doesn't log
    in on every single call -- only re-logs in if a withdraw attempt comes
    back unauthorized.
    """
    if not withdraw_configured():
        raise PaymentGatewayError('TOKOSHOPP_USERNAME/TOKOSHOPP_PASSWORD not configured.')

    url = f'{TOKOSHOPP_BASE}/login'
    body = {'username': TOKOSHOPP_USERNAME, 'password': TOKOSHOPP_PASSWORD}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers={'Content-Type': 'application/json'}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get('success') or not data.get('token'):
                raise PaymentGatewayError(f'ARTAN SHOP login failed ({resp.status}): {data}')
            _login_token_cache['token'] = data['token']
            _login_token_cache['username'] = TOKOSHOPP_USERNAME
            return data['token']


async def get_account_balance() -> int:
    """Login also returns the account's current saldo -- there's no
    separate balance-check endpoint in the docs, so this just re-logs in
    and reads it off the response. Used by /withdraw to show/confirm the
    amount before pulling it, and never cached (balance changes constantly).
    """
    if not withdraw_configured():
        raise PaymentGatewayError('TOKOSHOPP_USERNAME/TOKOSHOPP_PASSWORD not configured.')

    url = f'{TOKOSHOPP_BASE}/login'
    body = {'username': TOKOSHOPP_USERNAME, 'password': TOKOSHOPP_PASSWORD}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=body, headers={'Content-Type': 'application/json'}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get('success'):
                raise PaymentGatewayError(f'ARTAN SHOP login failed ({resp.status}): {data}')
            _login_token_cache['token'] = data.get('token')
            _login_token_cache['username'] = TOKOSHOPP_USERNAME
            return int(data.get('saldo', 0))


async def withdraw_balance(*, jenis: str, bank: str, nomor: str, nama: str, jumlah: int) -> dict:
    """Calls POST /withdraw against the ARTAN SHOP account. This moves real
    money out of the account balance the moment it succeeds (jumlah + fee
    is deducted immediately) -- there is no undo. Retries once with a
    fresh login token if the cached one is stale/unauthorized.
    """
    if not withdraw_configured():
        raise PaymentGatewayError('TOKOSHOPP_USERNAME/TOKOSHOPP_PASSWORD not configured.')
    if jumlah < WITHDRAW_MIN_AMOUNT:
        raise PaymentGatewayError(f'Minimum withdraw is Rp{WITHDRAW_MIN_AMOUNT:,}.'.replace(',', '.'))

    token = _login_token_cache.get('token') or await _login_for_token()
    body = {'jenis': jenis, 'bank': bank, 'nomor': nomor, 'nama': nama, 'jumlah': jumlah}
    url = f'{TOKOSHOPP_BASE}/withdraw'

    async def _attempt(tok: str):
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers={'Content-Type': 'application/json', 'token': tok}) as resp:
                data = await resp.json(content_type=None)
                return resp.status, data

    status, data = await _attempt(token)
    if status == 401:
        token = await _login_for_token()
        status, data = await _attempt(token)

    if status != 200 or not data.get('success'):
        raise PaymentGatewayError(f'ARTAN SHOP withdraw failed ({status}): {data}')
    return data


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
    """Double-checks a webhook (or a manual /product buy status poll)
    directly against ARTAN SHOP before treating a payment as real. Returns
    the order record if it's paid (whether we just confirmed it now, or it
    was already marked 'completed' earlier -- e.g. a previous check
    delivered the product but the bot restarted/lost its in-memory view
    before it could update the embed). Returns None only for orders that
    are still genuinely pending or don't exist.
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
