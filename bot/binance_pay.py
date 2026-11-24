"""USDT via Binance Pay (Merchant API) + cotação USDT/BRL."""
import hashlib
import hmac
import json
import secrets
import time
import aiohttp
from .config import BINANCE_API_KEY, BINANCE_SECRET, SANDBOX, ORDER_TIMEOUT_MIN, USDT_BRL_FALLBACK, USDT_SPREAD

BASE = "https://bpay.binanceapi.com"
_rate = {"v": USDT_BRL_FALLBACK, "t": 0}


async def usdt_brl() -> float:
    """Cotação USDT/BRL (Binance spot), cache 5 min, + spread."""
    if time.time() - _rate["t"] > 300:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get("https://api.binance.com/api/v3/ticker/price?symbol=USDTBRL", timeout=8) as r:
                    _rate["v"] = float((await r.json())["price"])
                    _rate["t"] = time.time()
        except Exception:
            pass
    return _rate["v"] * (1 + USDT_SPREAD)


async def brl_to_usdt(brl: float) -> float:
    return round(brl / await usdt_brl(), 2)


def _headers(body: str):
    ts = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    sig = hmac.new(BINANCE_SECRET.encode(), f"{ts}\n{nonce}\n{body}\n".encode(), hashlib.sha512).hexdigest().upper()
    return {"Content-Type": "application/json", "BinancePay-Timestamp": ts, "BinancePay-Nonce": nonce,
            "BinancePay-Certificate-SN": BINANCE_API_KEY, "BinancePay-Signature": sig}


async def _post(path, data):
    body = json.dumps(data, separators=(",", ":"))
    async with aiohttp.ClientSession() as s:
        async with s.post(BASE + path, data=body, headers=_headers(body), timeout=20) as r:
            res = await r.json()
    if res.get("status") != "SUCCESS":
        raise RuntimeError(f"Binance Pay: {res.get('code')} {res.get('errorMessage')}")
    return res["data"]


async def create_order(trade_no: str, usdt: float, title: str) -> dict:
    if SANDBOX:
        return {"prepayId": "SBX" + trade_no, "checkoutUrl": "https://pay.binance.com/"}
    return await _post("/binancepay/openapi/v3/order", {
        "env": {"terminalType": "APP"}, "merchantTradeNo": trade_no, "orderAmount": usdt, "currency": "USDT",
        "description": title[:256],
        "goodsDetails": [{"goodsType": "02", "goodsCategory": "Z000", "referenceGoodsId": trade_no, "goodsName": title[:256]}],
        "orderExpireTime": int(time.time() * 1000) + ORDER_TIMEOUT_MIN * 60_000})


async def status(trade_no: str) -> str:
    if SANDBOX:
        return "PAID"
    s = (await _post("/binancepay/openapi/v2/order/query", {"merchantTradeNo": trade_no}))["status"]
    return "PAID" if s == "PAID" else "EXPIRED" if s in ("CANCELED", "EXPIRED", "ERROR") else "PENDING"
