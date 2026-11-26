"""Camada de fornecedores: entrega por API genérica ou webhook assinado.

Config (JSON salvo pelo painel em suppliers.config):
  api:     {"url", "method", "headers": {..}, "body": "...", "path": "data.codes", "timeout": 20}
           placeholders {sku} {qtd} {pedido} na URL e no corpo; {secret:NOME} lê a variável de ambiente
           SUP_NOME (só variáveis com prefixo SUP_, cadastradas no ENV_FILE).
  webhook: {"url", "secret", "timeout": 15}  → POST JSON assinado com HMAC-SHA256 no header X-Vexa-Signature.
"""
import asyncio
import hashlib
import ipaddress
import socket
from urllib.parse import urlparse
import hmac
import json
import os
import re

import aiohttp

RETRY_WAIT = [2, 5, 10]  # espera antes da 2ª, 3ª tentativa (testes zeram)
UA = "vexa-store/1.0"


class SupplierError(Exception):
    pass


ALLOW_PRIVATE = os.environ.get("SUP_ALLOW_PRIVATE") == "1"  # só para testes locais


def public_url(url):
    """Bloqueia SSRF: só http(s) para host que resolve em IP público."""
    try:
        u = urlparse(url or "")
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        if ALLOW_PRIVATE:
            return True
        for info in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return False
        return True
    except Exception:
        return False


def conf(sup):
    try:
        return json.loads(sup["config"] or "{}")
    except ValueError:
        return {}


def _secret(name):
    return os.environ.get("SUP_" + name.upper().removeprefix("SUP_"), "")


def _secret_values(c):
    vals = [_secret(n) for n in re.findall(r"\{secret:([A-Za-z0-9_]+)\}", json.dumps(c))]
    if c.get("secret"):
        vals.append(c["secret"])
    return [v for v in vals if v]


def redact(text, c):
    text = str(text)
    for v in _secret_values(c):
        text = text.replace(v, "***")
    return text[:2000]


def fill(tpl, ctx, for_json=False):
    def rep(m):
        k = m.group(1)
        if k.startswith("secret:"):
            v = _secret(k[7:])
        else:
            v = str(ctx.get(k, m.group(0)))
        return json.dumps(v)[1:-1] if for_json else v
    return re.sub(r"\{([a-z]+(?::[A-Za-z0-9_]+)?)\}", rep, tpl or "")


def dig(data, path):
    """Caminho JSON tipo 'data.items.0.code' ou 'codes[*].key'."""
    cur = [data]
    for part in [p for p in re.split(r"\.", path or "") if p]:
        nxt = []
        star = part.endswith("[*]")
        key = part[:-3] if star else part
        for c in cur:
            if key:
                if isinstance(c, dict):
                    c = c.get(key)
                elif isinstance(c, list) and key.isdigit() and int(key) < len(c):
                    c = c[int(key)]
                else:
                    c = None
            if c is None:
                continue
            if star and isinstance(c, list):
                nxt.extend(c)
            else:
                nxt.append(c)
        cur = nxt
    out = []
    for c in cur:
        if isinstance(c, list):
            out.extend(str(x) for x in c if x not in (None, ""))
        elif isinstance(c, (dict,)):
            out.append(json.dumps(c, ensure_ascii=False))
        elif c not in (None, ""):
            out.append(str(c))
    return out


async def call_api(sup, sku, qty, code, log):
    """Pede ao fornecedor. Retenta 3x; idempotente pelo código do pedido (header Idempotency-Key)."""
    c = conf(sup)
    ctx = {"sku": sku, "qtd": qty, "pedido": code}
    url = fill(c.get("url"), ctx)
    if not public_url(url):
        raise SupplierError("URL do fornecedor inválida ou interna")
    method = (c.get("method") or "POST").upper()
    headers = {k: fill(str(v), ctx) for k, v in (c.get("headers") or {}).items()}
    headers.setdefault("Idempotency-Key", code)
    headers.setdefault("User-Agent", UA)
    body = c.get("body")
    data = fill(body, ctx, for_json=True).encode() if body else None
    if data and "Content-Type" not in headers:
        headers["Content-Type"] = "application/json"
    timeout = aiohttp.ClientTimeout(total=float(c.get("timeout") or 20))
    last = ""
    for attempt in range(1, 4):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.request(method, url, data=data, headers=headers, allow_redirects=False) as r:
                    text = await r.text()
                    log(f"tentativa {attempt}: HTTP {r.status} · {redact(text, c)[:500]}")
                    if r.status < 400:
                        try:
                            payload = json.loads(text)
                        except ValueError:
                            payload = text
                        items = dig(payload, c.get("path")) if c.get("path") else (
                            [payload] if isinstance(payload, str) else dig(payload, ""))
                        items = [i.strip() for i in items if i and i.strip()]
                        if len(items) >= qty:
                            return items[:qty]
                        last = f"resposta sem os {qty} código(s) esperados"
                        log(last)
                    else:
                        last = f"HTTP {r.status}"
                        if r.status in (400, 401, 403, 404, 422):
                            break  # erro definitivo, não adianta retentar
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = type(e).__name__
            log(f"tentativa {attempt}: {last}")
        if attempt < 3:
            await asyncio.sleep(RETRY_WAIT[attempt - 1])
    raise SupplierError(last or "falha no fornecedor")


def sign(secret, body: bytes):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def check_sig(secret, body: bytes, header):
    return bool(secret) and hmac.compare_digest(sign(secret, body), header or "")


async def call_webhook(sup, payload, log):
    """Envia o pedido para a URL do fornecedor (assinado). A entrega volta em /webhook/{id} do painel."""
    c = conf(sup)
    url = c.get("url") or ""
    if not public_url(url) or not c.get("secret"):
        raise SupplierError("webhook sem URL ou segredo")
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "X-Vexa-Signature": sign(c["secret"], body),
               "Idempotency-Key": payload["pedido"], "User-Agent": UA}
    timeout = aiohttp.ClientTimeout(total=float(c.get("timeout") or 15))
    last = ""
    for attempt in range(1, 4):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(url, data=body, headers=headers, allow_redirects=False) as r:
                    text = await r.text()
                    log(f"webhook tentativa {attempt}: HTTP {r.status} · {redact(text, c)[:300]}")
                    if r.status < 300:
                        return
                    last = f"HTTP {r.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = type(e).__name__
            log(f"webhook tentativa {attempt}: {last}")
        if attempt < 3:
            await asyncio.sleep(RETRY_WAIT[attempt - 1])
    raise SupplierError(last or "webhook falhou")


async def test(sup):
    """Botão 'Testar conexão': alcança a URL? (não cria pedido; GET/HEAD simples)."""
    c = conf(sup)
    if sup["kind"] == "manual":
        return True, "Fornecedor manual: nada a testar."
    if sup["kind"] == "buyer":
        try:
            b = await buyer_balance(sup)
            return True, f"Chave válida. Saldo no fornecedor: {b.get('balanceText') or b.get('balance')}."
        except SupplierError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Sem resposta: {type(e).__name__}."
    url = fill(c.get("url"), {"sku": "TESTE", "qtd": 1, "pedido": "TESTE"})
    if not public_url(url):
        return False, "URL inválida ou interna (bloqueada)."
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.request("HEAD" if sup["kind"] == "webhook" else "GET", url,
                                 headers={"User-Agent": UA}, allow_redirects=False) as r:
                return r.status < 500, f"Respondeu HTTP {r.status}."
    except Exception as e:
        return False, f"Sem resposta: {type(e).__name__}."


# ───────────────────────── API de revenda (padrão "Buyer API") ─────────────────────────
# Formato comum entre fornecedores: GET {base}{prefixo}/products|balance?key=..., POST {prefixo}/purchase
# com {"key","product_id","quantity"} + Idempotency-Key, entrega em "delivery". Config no painel:
# {"url": base, "prefix": "/api/v2/telegram-buyer", "key_secret": "FORN1", "min_balance": 0, "timeout": 25}
BUYER_PREFIX = "/api/v2/telegram-buyer"
LABELS = {"user": "login", "username": "login", "login": "login", "email": "e-mail", "password": "senha",
          "pass": "senha", "verifyEmail": "e-mail de recuperação", "code": "código", "key": "código",
          "license": "licença", "link": "link", "pin": "PIN", "note": "obs."}


class SupplierPending(Exception):
    """Fornecedor aceitou e cobrou, mas a entrega é manual do lado dele (vai para a fila manual)."""


def _buyer(sup):
    c = conf(sup)
    base = (c.get("url") or "").rstrip("/") + (c.get("prefix") or BUYER_PREFIX)
    return c, base, _secret(c.get("key_secret") or "")


def _fmt_item(it):
    if isinstance(it, dict):
        parts = [f"{LABELS.get(k, k)}: {v}" for k, v in it.items() if v not in (None, "", [], {})]
        return " | ".join(parts)
    return str(it).strip()


def buyer_items(delivery):
    """Normaliza a entrega (accounts, codes, keys, items… ou lista simples) em linhas de texto."""
    if not delivery:
        return []
    if isinstance(delivery, (list, tuple)):
        return [x for x in (_fmt_item(i) for i in delivery) if x]
    if isinstance(delivery, dict):
        out = []
        for v in delivery.values():
            if isinstance(v, (list, tuple)):
                out += [x for x in (_fmt_item(i) for i in v) if x]
            elif isinstance(v, str) and v.strip():
                out.append(v.strip())
        return out
    return [str(delivery)]


async def _buyer_get(sup, path):
    c, base, key = _buyer(sup)
    if not key:
        raise SupplierError(f"chave não cadastrada (SUP_{(c.get('key_secret') or 'NOME').upper()} no ENV_FILE)")
    if not public_url(base):
        raise SupplierError("URL do fornecedor inválida ou interna")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=float(c.get("timeout") or 25))) as s:
        async with s.get(f"{base}/{path}", params={"key": key}, headers={"User-Agent": UA, "x-buyer-key": key},
                         allow_redirects=False) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {}
            if r.status >= 400 or not data.get("success", False):
                raise SupplierError(f"HTTP {r.status}: {redact((data or {}).get('message', ''), c)[:200]}")
            return data


async def buyer_products(sup):
    return (await _buyer_get(sup, "products")).get("products", [])


async def buyer_balance(sup):
    return await _buyer_get(sup, "balance")


async def buyer_purchase(sup, sku, qty, code, log):
    """Compra no fornecedor. Devolve (itens, info). Respeita Retry-After; idempotente pelo código do pedido."""
    c, base, key = _buyer(sup)
    if not key:
        raise SupplierError(f"chave não cadastrada (SUP_{(c.get('key_secret') or 'NOME').upper()} no ENV_FILE)")
    if not public_url(base):
        raise SupplierError("URL do fornecedor inválida ou interna")
    body = {"key": key, "product_id": sku, "quantity": qty}
    headers = {"Idempotency-Key": f"vexa-{code}", "x-buyer-key": key, "User-Agent": UA}
    timeout = aiohttp.ClientTimeout(total=float(c.get("timeout") or 25))
    last = ""
    for attempt in range(1, 4):
        wait = RETRY_WAIT[attempt - 1] if attempt < 3 else 0
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(f"{base}/purchase", json=body, headers=headers, allow_redirects=False) as r:
                    text = await r.text()
                    try:
                        data = json.loads(text)
                    except ValueError:
                        data = {}
                    msg = redact(str(data.get("message", "")), c)[:200]
                    # nunca grava credenciais entregues no log: só status e mensagem
                    log(f"tentativa {attempt}: HTTP {r.status}" + (f" · {msg}" if msg else ""))
                    if r.status == 200 and data.get("success"):
                        order = data.get("order") or {}
                        pay = data.get("payment") or {}
                        info = {"supplier_order": order.get("orderCode"), "status": order.get("status"),
                                "amount": pay.get("amountText") or pay.get("amount"),
                                "balance": pay.get("balance"), "balance_text": pay.get("balanceText")}
                        log(f"pedido no fornecedor {info['supplier_order']} · {info['status']} · pago {info['amount']}"
                            f" · saldo restante {info['balance_text'] or info['balance']}")
                        items = buyer_items(data.get("delivery"))
                        if len(items) >= qty:
                            return items, info
                        if str(order.get("status", "")).lower() in ("paid", "processing", "pending", "in_progress"):
                            raise SupplierPending(info["supplier_order"] or "")
                        raise SupplierError("fornecedor confirmou mas não entregou os itens")
                    if r.status == 429:
                        ra = int(r.headers.get("Retry-After", "0") or 0)
                        last = f"limite de requisições do fornecedor (aguardar {ra}s)"
                        if ra > 15:
                            break
                        wait = max(wait, ra)
                    elif r.status == 401:
                        last = "chave do fornecedor inválida"
                        break
                    elif r.status == 400:
                        last = "saldo insuficiente no fornecedor" if "balance" in msg.lower() or "saldo" in msg.lower() else (msg or "pedido inválido")
                        break
                    elif r.status == 404:
                        last = "produto não encontrado no fornecedor (confira o SKU)"
                        break
                    elif r.status == 409:
                        last = msg or "conflito no fornecedor"
                        if "progress" not in msg.lower():
                            break  # sem estoque no fornecedor: não adianta repetir
                        wait = max(wait, 3)
                    else:
                        last = f"HTTP {r.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = type(e).__name__
            log(f"tentativa {attempt}: {last}")
        if attempt < 3:
            await asyncio.sleep(wait)
    raise SupplierError(last or "falha no fornecedor")
