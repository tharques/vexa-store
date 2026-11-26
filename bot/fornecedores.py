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
