"""Pix via Asaas. Docs: https://docs.asaas.com"""
from datetime import date
import aiohttp
from .config import ASAAS_API_KEY, ASAAS_ENV, SANDBOX

BASE = "https://api-sandbox.asaas.com/v3" if ASAAS_ENV == "sandbox" else "https://api.asaas.com/v3"
# QR de demonstração para modo SANDBOX (1x1 PNG roxo)
_DEMO_QR = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


async def _req(method, path, json=None):
    async with aiohttp.ClientSession(headers={"access_token": ASAAS_API_KEY, "User-Agent": "vexa-bot"}) as s:
        async with s.request(method, BASE + path, json=json, timeout=20) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(f"Asaas {r.status}: {data.get('errors')}")
            return data


async def ensure_customer(tg_id: int, name: str, cpf: str) -> str:
    if SANDBOX:
        return f"cus_demo_{tg_id}"
    found = await _req("GET", f"/customers?externalReference=tg{tg_id}")
    if found.get("data"):
        return found["data"][0]["id"]
    c = await _req("POST", "/customers", {"name": name or f"Cliente {tg_id}", "cpfCnpj": cpf,
                                           "externalReference": f"tg{tg_id}", "notificationDisabled": True})
    return c["id"]


async def create_pix(customer_id: str, value: float, description: str, ref: str) -> dict:
    """Retorna {id, payload (copia e cola), qr_b64}."""
    if SANDBOX:
        return {"id": f"pay_demo_{ref}", "payload": "00020126VEXADEMO-PIX-COPIA-E-COLA5204000053039865802BR",
                "qr_b64": _DEMO_QR}
    p = await _req("POST", "/payments", {"customer": customer_id, "billingType": "PIX", "value": round(value, 2),
                                         "dueDate": date.today().isoformat(), "description": description[:500],
                                         "externalReference": ref})
    qr = await _req("GET", f"/payments/{p['id']}/pixQrCode")
    return {"id": p["id"], "payload": qr["payload"], "qr_b64": qr["encodedImage"]}


async def status(payment_id: str) -> str:
    """PENDING | RECEIVED | CONFIRMED | OVERDUE | REFUNDED ... -> normaliza para PAID/PENDING/EXPIRED"""
    if SANDBOX:
        return "PAID"
    p = await _req("GET", f"/payments/{payment_id}")
    s = p["status"]
    if p.get("deleted"):
        return "EXPIRED"
    if s in ("RECEIVED", "CONFIRMED", "RECEIVED_IN_CASH"):
        return "PAID"
    if s in ("OVERDUE", "REFUNDED", "DELETED"):
        return "EXPIRED"
    return "PENDING"


async def cancel(payment_id: str):
    if not SANDBOX:
        try:
            await _req("DELETE", f"/payments/{payment_id}")
        except Exception:
            pass
