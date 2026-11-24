"""Cliente Binance Pay (Merchant API). Docs: https://developers.binance.com/docs/binance-pay"""
import hashlib
import hmac
import json
import secrets
import time
import aiohttp
from .config import BINANCE_API_KEY, BINANCE_SECRET, BINANCE_CURRENCY, SANDBOX, ORDER_TIMEOUT_MIN

BASE = "https://bpay.binanceapi.com"


def _headers(body: str):
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    payload = f"{ts}\n{nonce}\n{body}\n"
    sig = hmac.new(BINANCE_SECRET.encode(), payload.encode(), hashlib.sha512).hexdigest().upper()
    return {
        "Content-Type": "application/json",
        "BinancePay-Timestamp": ts,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": BINANCE_API_KEY,
        "BinancePay-Signature": sig,
    }


async def _post(path, data):
    body = json.dumps(data, separators=(",", ":"))
    async with aiohttp.ClientSession() as s:
        async with s.post(BASE + path, data=body, headers=_headers(body), timeout=20) as r:
            res = await r.json()
    if res.get("status") != "SUCCESS":
        raise RuntimeError(f"Binance Pay: {res.get('code')} {res.get('errorMessage')}")
    return res["data"]


async def create_order(trade_no: str, amount: float, title: str) -> dict:
    """Retorna {prepayId, checkoutUrl, qrcodeLink, universalUrl}."""
    if SANDBOX:
        return {"prepayId": "SBX" + trade_no, "checkoutUrl": f"https://example.com/pay/{trade_no}"}
    return await _post("/binancepay/openapi/v3/order", {
        "env": {"terminalType": "APP"},
        "merchantTradeNo": trade_no,
        "orderAmount": round(amount, 2),
        "currency": BINANCE_CURRENCY,
        "description": title[:256],
        "goodsDetails": [{"goodsType": "02", "goodsCategory": "Z000",
                          "referenceGoodsId": trade_no, "goodsName": title[:256]}],
        "orderExpireTime": int(time.time() * 1000) + ORDER_TIMEOUT_MIN * 60_000,
    })


async def query_status(trade_no: str) -> str:
    """INITIAL | PENDING | PAID | CANCELED | ERROR | REFUNDING | REFUNDED | EXPIRED"""
    if SANDBOX:
        return "PAID"
    data = await _post("/binancepay/openapi/v2/order/query", {"merchantTradeNo": trade_no})
    return data["status"]
