"""Not used.

This file previously handled a webhook callback from a fallback gateway
(ARTAN/tokoshopp.web.id) that's no longer part of this bot -- payment is
RamaShop-only now. RamaShop's docs don't document any webhook/callback at
all: confirmation is polling-only, via GET /deposit/status/{id} (see
utils/payments.get_deposit_status, called from confirm_payment and from
QRISPaymentView's poll loop in commands/product.py).

Nothing in bot.py imports or starts this module, so it was already dead
code before this cleanup -- kept as an empty stub, rather than deleted
outright, in case something still imports it.
"""
