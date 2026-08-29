"""Tiny aiohttp server that receives ARTAN SHOP's (tokoshopp.web.id)
payment webhook callback.

This is a convenience/speed-up layer only: /product buy already polls the
gateway's own payment/status endpoint every few seconds and never trusts a
webhook body by itself (see utils/payments.confirm_payment, which always
re-queries the gateway before marking an order paid). This server just lets
a completed payment get delivered within ~1s instead of waiting for the
next poll tick.

Runs on PORT (default 8080) at POST /webhook/pakasir (path kept as-is for
continuity with prior config -- rename the route below plus your dashboard
callback URL if you'd rather it read /callback/payment). If you don't want
this exposed publicly, that's fine -- leave WEBHOOK_ENABLED=0 and rely on
polling alone; nothing else here changes.

ARTAN SHOP's callback payload only carries their own transaction_id (not
our order_id), so this handler looks the order up by that id -- see
utils.payments.find_order_by_gateway_transaction_id.
"""
import os

from aiohttp import web

WEBHOOK_PORT = int(os.getenv('WEBHOOK_PORT', '8080'))

_bot = None


def bind_bot(bot):
    global _bot
    _bot = bot


async def _handle_payment_webhook(request: web.Request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({'ok': False, 'error': 'invalid json'}, status=400)

    transaction_id = payload.get('transaction_id')
    status = payload.get('status')
    if not transaction_id or status != 'paid':
        return web.json_response({'ok': True})

    from utils.payments import confirm_payment, find_order_by_gateway_transaction_id, PaymentGatewayError

    order_lookup = await find_order_by_gateway_transaction_id(transaction_id)
    if not order_lookup:
        return web.json_response({'ok': True})

    order_id = order_lookup['id']

    try:
        order = await confirm_payment(order_id)
    except PaymentGatewayError as err:
        print(f'[webhook] confirm_payment failed for {order_id}: {err}')
        return web.json_response({'ok': False}, status=502)

    if order and _bot is not None:
        from commands.product import notify_order_paid
        try:
            await notify_order_paid(_bot, order_id, order)
        except Exception as err:
            print(f'[webhook] notify_order_paid failed for {order_id}: {err}')

    return web.json_response({'ok': True})


async def start(bot, port: int | None = None):
    bind_bot(bot)

    app = web.Application()
    app.router.add_post('/webhook/pakasir', _handle_payment_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port or WEBHOOK_PORT)
    await site.start()
    print(f'[webhook] Listening on 0.0.0.0:{port or WEBHOOK_PORT} (POST /webhook/pakasir)')
    return runner
