"""Painel admin da VEXA SHOPPING (FastAPI + Jinja2 + HTMX/Alpine), mesmo SQLite do bot.

Rodar: uvicorn panel.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=*
"""
import asyncio
import csv
import re
from html import escape as html_esc
import hmac
import io
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bot import asaas, fornecedores as forn, store
from bot.config import ADMIN_IDS, BOT_TOKEN, SANDBOX
from bot.db import Repo, connect
from bot.ui import brl
from . import auth

log = logging.getLogger("painel")
BASE = Path(__file__).resolve().parent
ctx = {"repo": None, "bot": None}  # os testes injetam repo e bot falsos
TZ = timedelta(hours=-3)  # America/Sao_Paulo


# ───────────────────────── infraestrutura ─────────────────────────
@asynccontextmanager
async def lifespan(app):
    if ctx["repo"] is None:
        ctx["repo"] = Repo(await connect())
    if ctx["bot"] is None:
        ctx["bot"] = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    await bootstrap()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
T = Jinja2Templates(directory=BASE / "templates")

STATUS = {"pending": ("Aguardando pagamento", "gray"), "paid": ("Pago", "blue"), "delivering": ("Entregando", "blue"),
          "awaiting": ("No fornecedor", "amber"), "manual": ("Fila manual", "amber"), "ready": ("Enviando", "blue"),
          "delivered": ("Entregue", "green"), "refunded": ("Estornado", "red"), "expired": ("Expirado", "gray"),
          "canceled": ("Cancelado", "gray"), "failed": ("Falhou", "red")}
MODES = {"estoque": "Estoque", "api": "API do fornecedor", "webhook": "Webhook", "manual": "Manual"}
EVENT = {"clique": ("Clique", "gray"), "mensagem": ("Mensagem", "blue"), "pedido": ("Pedido", "blue"),
         "pagamento": ("Pagamento", "green"), "entrega": ("Entrega", "green"), "erro": ("Erro", "red"),
         "chamado": ("Chamado", "amber"), "suporte": ("Suporte", "amber")}
TICKET = {"aberto": ("Aberto", "amber"), "respondido": ("Respondido", "blue"), "resolvido": ("Resolvido", "green")}


def local(ts, fmt="%d/%m/%Y %H:%M"):
    if not ts:
        return "—"
    try:
        return (datetime.fromisoformat(str(ts)) + TZ).strftime(fmt)
    except ValueError:
        return str(ts)


T.env.filters.update(brl=lambda v: brl(v or 0), dt=local, pct=lambda v: f"{(v or 0):.1f}%".replace(".", ","))
T.env.globals.update(SANDBOX=SANDBOX, STATUS=STATUS, MODES=MODES, TICKET=TICKET, ROLES=auth.ROLES, EVENT=EVENT)


def R():
    return ctx["repo"]


def tg():
    return ctx["bot"]


class Redirect(Exception):
    def __init__(self, url):
        self.url = url


class Forbidden(Exception):
    pass


@app.exception_handler(Redirect)
async def _redir(request, e):
    if request.headers.get("HX-Request"):
        return Response(status_code=204, headers={"HX-Redirect": e.url})
    return RedirectResponse(e.url, status_code=303)


@app.exception_handler(Forbidden)
async def _forb(request, e):
    return PlainTextResponse("Acesso negado.", status_code=403)


# scripts servidos daqui mesmo (static/vendor): nada de CDN executando código no painel
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")


# O painel é só do dono: sem a "porta" (cookie criado pelo link de uso único que o bot manda em /painel),
# tudo responde 404 como se o site não existisse. Ficam abertos só o retorno de fornecedores e o health.
PUBLIC = ("/webhook/", "/entrar/")
DOOR = "vx_door"
DOOR_DAYS = 30


async def door_ok(request):
    tok = request.cookies.get(DOOR)
    if not tok or len(tok) > 100:
        return False
    return bool(await R().one("SELECT 1 FROM panel_doors WHERE token_hash=? AND expires_at>datetime('now')", auth._h(tok)))


@app.middleware("http")
async def headers(request, call_next):
    path = request.url.path
    if path != "/health" and not path.startswith(PUBLIC) and not await door_ok(request):
        r = JSONResponse({"detail": "Not Found"}, status_code=404)
    else:
        r = await call_next(request)
    r.headers.setdefault("X-Frame-Options", "DENY")
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    r.headers.setdefault("Referrer-Policy", "same-origin")
    r.headers.setdefault("Cache-Control", "no-store")
    r.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    r.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    r.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    r.headers.setdefault("Content-Security-Policy", CSP)
    return r


def ip_of(request):
    """IP real do visitante. O painel só é alcançável pelo Caddy (porta presa no 127.0.0.1), e o Caddy ACRESCENTA
    o IP de quem conectou no fim do X-Forwarded-For. Pegar o último impede forjar IP mandando esse cabeçalho."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()[:64]
    return request.client.host if request.client else ""


async def guard(request, *roles):
    """Usuário logado com um dos papéis listados (admin sempre pode; nada listado = só admin)."""
    u = await auth.session_user(R(), request.cookies.get(auth.COOKIE))
    if not u:
        raise Redirect("/login")
    if u["must_change"] and request.url.path != "/senha":
        raise Redirect("/senha")
    if u["role"] != "admin" and u["role"] not in roles:  # sem papel listado = só admin
        raise Forbidden()
    return u


async def form(request, u):
    f = await request.form()
    token = f.get("csrf") or request.headers.get("X-CSRF-Token") or ""
    if not hmac.compare_digest(str(token), u["csrf"]):
        raise Forbidden()
    return f


async def client_context(uid, events=80):
    """Tudo sobre o cliente para dar suporte: dados, pedidos com entregas, histórico no bot e carteira."""
    c = await R().one("SELECT * FROM users WHERE id=?", uid)
    orders = [dict(o) for o in await R().q(
        "SELECT o.*, p.name pname, p.unlimited, p.warranty_days FROM orders o LEFT JOIN products p ON p.id=o.product_id "
        "WHERE o.user_id=? ORDER BY o.id DESC LIMIT 50", uid)]
    for o in orders:
        o["entregas"] = [r["content"] for r in await R().q("SELECT content FROM stock WHERE order_id=? ORDER BY id", o["id"])] or (
            [o["unlimited"]] * o["qty"] if o["unlimited"] and o["status"] == "delivered" else [])
        o["warranty_until"] = None
        if o["delivered_at"] and o["warranty_days"]:
            o["warranty_until"] = datetime.fromisoformat(o["delivered_at"]) + timedelta(days=o["warranty_days"])
    ev = await R().q("SELECT * FROM events WHERE user_id=? ORDER BY id DESC LIMIT ?", uid, events)
    ledger = await R().q("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 30", uid)
    tickets = await R().q("SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC", uid)
    n = sum(1 for o in orders if o["status"] == "delivered" and o["kind"] == "buy")
    return {"c": c, "orders": orders, "events": ev, "ledger": ledger, "tickets": tickets, "compras": n,
            "now": datetime.utcnow()}


async def audit(request, u, action, entity="", eid="", detail=None):
    await auth.audit(R(), u["username"], action, entity, eid, detail, ip_of(request))


def page(request, name, u, **kw):
    counts = kw.pop("_counts", None)
    return T.TemplateResponse(request, name, {"u": u, "msg": request.query_params.get("msg"),
                                              "err": request.query_params.get("err"), "nav": counts, **kw})


async def nav_counts():
    q = R().one
    return {"fila": (await q("SELECT COUNT(*) n FROM orders WHERE status IN('manual','awaiting')"))["n"],
            "chamados": (await q("SELECT COUNT(*) n FROM tickets WHERE status='aberto'"))["n"]}


async def render(request, name, u, **kw):
    return page(request, name, u, _counts=await nav_counts(), **kw)


def go(url, msg=None, err=None):
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    if err:
        url += ("&" if "?" in url else "?") + "err=" + quote(err)
    raise Redirect(url)


def num(v, default=0.0):
    try:
        return float(str(v).replace(",", ".")) if str(v).strip() != "" else default
    except ValueError:
        return default


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in r] for r in rows])


async def notify_admins(text):
    sent = 0
    for a in ADMIN_IDS:
        try:
            await tg().send_message(a, text)
            sent += 1
        except Exception:
            log.warning("não consegui avisar o admin no Telegram")
    return sent


async def bootstrap():
    """1º deploy: cria 'thiago' com senha temporária enviada só no Telegram do admin."""
    await store.load(R())
    if await R().one("SELECT 1 FROM panel_users LIMIT 1"):
        return
    pw = auth.strong_password()
    ok = await notify_admins(
        "🔐 <b>Painel VEXA criado</b>\nPara abrir, mande /painel aqui no bot.\n\n"
        f"Usuário: <code>thiago</code>\nSenha temporária: <code>{pw}</code>\n\n"
        "Você vai trocar a senha no primeiro acesso. Apague esta mensagem depois.")
    if not ok:
        log.error("senha temporária NÃO enviada (Telegram indisponível); usuário não criado, tento no próximo início")
        return
    await R().x("INSERT INTO panel_users(username,pass_hash,role,must_change) VALUES('thiago',?,'admin',1)", auth.hash_pw(pw))
    await auth.audit(R(), "sistema", "usuário criado", "panel_users", "thiago", "admin inicial; senha enviada no Telegram")
    log.info("usuário 'thiago' criado; senha temporária enviada no Telegram")


@app.get("/entrar/{token}")
async def entrar(request: Request, token: str):
    """Consome o link de uso único e grava a porta neste navegador por 30 dias."""
    if len(token) > 100 or not await R().run(
            "UPDATE panel_links SET used=1 WHERE token_hash=? AND used=0 AND expires_at>datetime('now')", auth._h(token)):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    door = secrets.token_urlsafe(32)
    ip = ip_of(request)
    await R().x("INSERT INTO panel_doors(token_hash,expires_at,ip) VALUES(?, datetime('now', ?), ?)",
                auth._h(door), f"+{DOOR_DAYS} days", ip)
    await auth.audit(R(), "sistema", "porta do painel aberta", ip=ip)
    asyncio.create_task(notify_admins(f"🚪 Painel liberado num aparelho · IP <code>{html_esc(ip)}</code>\n"
                                      "Se não foi você, avise o suporte técnico."))
    r = RedirectResponse("/login", status_code=303)
    r.set_cookie(DOOR, door, max_age=DOOR_DAYS * 86400, httponly=True, secure=True, samesite="lax")
    return r


@app.get("/health")
async def health():
    return {"ok": True}


# ───────────────────────── login ─────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return page(request, "login.html", None)


@app.post("/login")
async def login(request: Request):
    f = await request.form()
    user = str(f.get("username", "")).strip().lower()[:40]
    pw = str(f.get("password", ""))
    ip = ip_of(request)
    known = bool(user) and await R().one("SELECT 1 FROM panel_known_ips WHERE username=? AND ip=?", user, ip)
    # o bloqueio só por nome de usuário não vale para um IP de onde esse usuário já entrou (atacante não tranca o dono)
    keys = (f"uip:{user}|{ip}", f"ip:{ip}") + (() if known else (f"u:{user}",))
    if await auth.locked(R(), *keys):
        return page(request, "login.html", None, error="Muitas tentativas. Aguarde 15 minutos.")
    allowed, newly = await auth.attempt(R(), *keys)
    if newly:
        asyncio.create_task(notify_admins(
            f"🚨 Painel: bloqueio por tentativas erradas\nUsuário tentado: <code>{html_esc(user or '?')}</code>\n"
            f"IP <code>{html_esc(ip)}</code> · bloqueado 15 min"))
    if not allowed:
        await auth.audit(R(), user or "?", "login recusado (bloqueado)", ip=ip)
        return page(request, "login.html", None, error="Muitas tentativas. Aguarde 15 minutos.")
    u = await R().one("SELECT * FROM panel_users WHERE username=? AND active=1", user)
    # bcrypt fora do loop (não trava o painel) e sempre executado (tempo não revela se o usuário existe)
    ok = await asyncio.to_thread(auth.check_pw, pw, u["pass_hash"] if u else auth.DUMMY_HASH) and u is not None
    if not ok:
        await auth.audit(R(), user or "?", "login falhou", ip=ip)
        locked_now = await auth.locked(R(), *keys)
        return page(request, "login.html", None,
                    error="Muitas tentativas. Aguarde 15 minutos." if locked_now else "Usuário ou senha incorretos.")
    await auth.success(R(), f"uip:{user}|{ip}", f"ip:{ip}")
    await R().x("INSERT OR IGNORE INTO panel_known_ips(username, ip) VALUES(?,?)", user, ip)
    token = await auth.new_session(R(), u["id"], ip)
    await R().x("UPDATE panel_users SET last_login=CURRENT_TIMESTAMP WHERE id=?", u["id"])
    await auth.audit(R(), u["username"], "login", ip=ip)
    asyncio.create_task(notify_admins(f"🔐 Login no painel · <b>{u['username']}</b>\n"
                                      f"{(datetime.utcnow() + TZ):%d/%m/%Y %H:%M} · IP <code>{ip}</code>"))
    r = RedirectResponse("/senha" if u["must_change"] else "/", status_code=303)
    r.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True, secure=True, samesite="lax")
    return r


@app.post("/logout")
async def logout(request: Request):
    u = await guard(request, "atendente")
    await form(request, u)
    await auth.end_session(R(), request.cookies.get(auth.COOKIE))
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(auth.COOKIE)
    return r


@app.get("/senha", response_class=HTMLResponse)
async def senha_page(request: Request):
    u = await guard(request, "atendente")
    return page(request, "senha.html", u)


@app.post("/senha")
async def senha(request: Request):
    u = await guard(request, "atendente")
    f = await form(request, u)
    cur, new, rep = str(f.get("current", "")), str(f.get("new", "")), str(f.get("repeat", ""))
    err = None
    if not await asyncio.to_thread(auth.check_pw, cur, u["pass_hash"]):
        err = "Senha atual incorreta."
    elif new != rep:
        err = "As senhas novas não conferem."
    elif new == cur:
        err = "A nova senha precisa ser diferente da atual."
    else:
        err = auth.weak_reason(new)
    if err:
        return page(request, "senha.html", u, error=err)
    await R().x("UPDATE panel_users SET pass_hash=?, must_change=0 WHERE id=?", await asyncio.to_thread(auth.hash_pw, new), u["id"])
    await R().x("DELETE FROM panel_sessions WHERE user_id=? AND token_hash!=?", u["id"],
                auth._h(request.cookies.get(auth.COOKIE, "")))  # derruba sessões abertas em outros aparelhos
    await audit(request, u, "trocou a própria senha", "panel_users", u["username"])
    go("/", "Senha alterada.")


# ───────────────────────── início ─────────────────────────
async def money(where, args=()):
    r = await R().one("SELECT COUNT(*) n, COALESCE(SUM(amount),0) fat, COALESCE(SUM(cost),0) custo FROM orders "
                      f"WHERE status='delivered' AND kind='buy' AND {where}", *args)
    return {"n": r["n"], "fat": r["fat"], "custo": r["custo"], "lucro": r["fat"] - r["custo"]}


LOCALDAY = "date(paid_at,'-3 hours')"


@app.get("/", response_class=HTMLResponse)
async def inicio(request: Request):
    u = await guard(request, "atendente")
    if u["role"] != "admin":
        go("/pedidos")
    today = (datetime.utcnow() + TZ).date()
    k = {"hoje": await money(f"{LOCALDAY}=?", (today.isoformat(),)),
         "7d": await money(f"{LOCALDAY}>=?", ((today - timedelta(days=6)).isoformat(),)),
         "mes": await money(f"{LOCALDAY}>=?", (today.replace(day=1).isoformat(),))}
    q = R().one
    extra = {"pendentes": (await q("SELECT COUNT(*) n FROM orders WHERE status='pending'"))["n"],
             "fila": (await q("SELECT COUNT(*) n FROM orders WHERE status IN('manual','awaiting')"))["n"],
             "chamados": (await q("SELECT COUNT(*) n FROM tickets WHERE status='aberto'"))["n"],
             "carteira": (await q("SELECT COALESCE(SUM(balance),0) v FROM users"))["v"],
             "clientes": (await q("SELECT COUNT(*) n FROM users"))["n"]}
    rows = await R().q(f"SELECT {LOCALDAY} d, COALESCE(SUM(amount),0) v, COALESCE(SUM(amount-cost),0) l FROM orders "
                       f"WHERE status='delivered' AND kind='buy' AND {LOCALDAY}>=? GROUP BY d",
                       (today - timedelta(days=29)).isoformat())
    by = {r["d"]: r for r in rows}
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    chart = {"labels": [d.strftime("%d/%m") for d in days],
             "fat": [round(by[d.isoformat()]["v"], 2) if d.isoformat() in by else 0 for d in days],
             "lucro": [round(by[d.isoformat()]["l"], 2) if d.isoformat() in by else 0 for d in days]}
    recent = await R().q("SELECT o.*, p.name, us.name uname FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                         "LEFT JOIN users us ON us.id=o.user_id ORDER BY o.id DESC LIMIT 8")
    return await render(request, "inicio.html", u, k=k, extra=extra, chart=json.dumps(chart), recent=recent)


# ───────────────────────── produtos e categorias ─────────────────────────
PRODUCT_FIELDS = ["name", "category_id", "price", "old_price", "cost", "description", "warranty_days", "max_qty",
                  "active", "delivery_mode", "supplier_id", "sku", "ref_link", "notes", "unlimited", "sort"]


@app.get("/produtos", response_class=HTMLResponse)
async def produtos(request: Request):
    u = await guard(request)
    cats = await R().q("SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id) n FROM categories c "
                       "ORDER BY sort, name")
    prods = await R().q("SELECT p.*, c.name cat, s.name sup, (SELECT COUNT(*) FROM stock st WHERE st.product_id=p.id "
                        "AND st.sold=0) disp FROM products p LEFT JOIN categories c ON c.id=p.category_id "
                        "LEFT JOIN suppliers s ON s.id=p.supplier_id ORDER BY c.sort, p.sort, p.name")
    return await render(request, "produtos.html", u, cats=cats, prods=prods)


@app.post("/categorias/salvar")
async def cat_save(request: Request):
    u = await guard(request)
    f = await form(request, u)
    name = str(f.get("name", "")).strip()
    if not name:
        go("/produtos", err="Informe o nome da categoria.")
    vals = (name, str(f.get("emoji") or "•").strip()[:4], int(num(f.get("sort"), 0)))
    cid = f.get("id")
    try:
        if cid:
            old = dict(await R().one("SELECT * FROM categories WHERE id=?", int(cid)))
            await R().x("UPDATE categories SET name=?, emoji=?, sort=? WHERE id=?", *vals, int(cid))
            await audit(request, u, "editou categoria", "categories", cid, {"antes": old, "depois": vals})
        else:
            cid = await R().x("INSERT INTO categories(name,emoji,sort) VALUES(?,?,?)", *vals)
            await audit(request, u, "criou categoria", "categories", cid, vals)
    except Exception:
        go("/produtos", err="Já existe uma categoria com esse nome.")
    go("/produtos", "Categoria salva.")


@app.post("/categorias/{cid}/excluir")
async def cat_del(request: Request, cid: int):
    u = await guard(request)
    await form(request, u)
    if (await R().one("SELECT COUNT(*) n FROM products WHERE category_id=?", cid))["n"]:
        go("/produtos", err="Mova ou exclua os produtos da categoria antes.")
    await R().x("DELETE FROM categories WHERE id=?", cid)
    await audit(request, u, "excluiu categoria", "categories", cid)
    go("/produtos", "Categoria excluída.")


@app.get("/produtos/{pid}", response_class=HTMLResponse)
async def produto(request: Request, pid: str):
    u = await guard(request)
    p = None
    if pid != "novo":
        p = await R().one("SELECT * FROM products WHERE id=?", int(pid))
        if not p:
            go("/produtos", err="Produto não encontrado.")
    cats = await R().q("SELECT * FROM categories ORDER BY sort, name")
    sups = await R().q("SELECT * FROM suppliers ORDER BY name")
    qp = request.query_params
    pre = {"name": qp.get("nome", "")[:120], "sku": qp.get("sku", "")[:200], "description": qp.get("desc", "")[:800],
           "supplier_id": int(qp["fornecedor"]) if qp.get("fornecedor", "").isdigit() else None,
           "notes": qp.get("obs", "")[:500]} if not p else {}
    st = None
    if p:
        st = await R().one("SELECT SUM(sold=0) disp, SUM(sold=1) vend FROM stock WHERE product_id=?", p["id"])
    return await render(request, "produto.html", u, p=p, cats=cats, sups=sups, st=st, pre=pre)


@app.post("/produtos/salvar")
async def produto_save(request: Request):
    u = await guard(request)
    f = await form(request, u)
    d = {k: str(f.get(k, "")).strip() for k in PRODUCT_FIELDS}
    if not d["name"] or not d["category_id"]:
        go(f"/produtos/{f.get('id') or 'novo'}", err="Nome e categoria são obrigatórios.")
    vals = {"name": d["name"][:120], "category_id": int(d["category_id"]), "price": round(num(d["price"]), 2),
            "old_price": round(num(d["old_price"]), 2) or None, "cost": round(num(d["cost"]), 2),
            "description": d["description"][:800], "warranty_days": int(num(d["warranty_days"])),
            "max_qty": max(1, int(num(d["max_qty"], 10))), "active": 1 if f.get("active") else 0,
            "delivery_mode": d["delivery_mode"] if d["delivery_mode"] in MODES else "estoque",
            "supplier_id": int(d["supplier_id"]) if d["supplier_id"] else None, "sku": d["sku"][:200],
            "ref_link": d["ref_link"][:500], "notes": d["notes"][:2000], "unlimited": d["unlimited"] or None,
            "sort": int(num(d["sort"]))}
    if vals["price"] <= 0:
        go(f"/produtos/{f.get('id') or 'novo'}", err="Preço precisa ser maior que zero.")
    if vals["delivery_mode"] in ("api", "webhook") and not vals["supplier_id"]:
        go(f"/produtos/{f.get('id') or 'novo'}", err="Escolha o fornecedor para entrega por API/webhook.")
    pid = f.get("id")
    cols = list(vals)
    if pid:
        old = dict(await R().one("SELECT * FROM products WHERE id=?", int(pid)))
        await R().x(f"UPDATE products SET {', '.join(c + '=?' for c in cols)} WHERE id=?", *vals.values(), int(pid))
        diff = {c: [old.get(c), vals[c]] for c in cols if str(old.get(c) or "") != str(vals[c] or "")}
        await audit(request, u, "editou produto", "products", pid, diff)
    else:
        pid = await R().x(f"INSERT INTO products({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", *vals.values())
        await audit(request, u, "criou produto", "products", pid, vals)
    go(f"/produtos/{pid}", "Produto salvo.")


@app.post("/produtos/{pid}/estoque")
async def produto_stock(request: Request, pid: int):
    u = await guard(request)
    f = await form(request, u)
    items = [x.strip() for x in str(f.get("items", "")).splitlines() if x.strip()]
    if items:
        await R().db.executemany("INSERT INTO stock(product_id,content) VALUES(?,?)", [(pid, i) for i in items])
        await R().db.commit()
        await audit(request, u, "repôs estoque", "products", pid, f"+{len(items)} unidade(s)")
    go(f"/produtos/{pid}", f"+{len(items)} unidade(s) no estoque.")


@app.post("/produtos/{pid}/estoque/limpar")
async def produto_stock_clear(request: Request, pid: int):
    u = await guard(request)
    await form(request, u)
    n = await R().run("DELETE FROM stock WHERE product_id=? AND sold=0", pid)
    await audit(request, u, "zerou estoque disponível", "products", pid, f"-{n} unidade(s)")
    go(f"/produtos/{pid}", f"{n} unidade(s) removida(s).")


@app.post("/produtos/{pid}/foto")
async def produto_foto(request: Request, pid: int):
    """Upload → envia ao Telegram uma vez para obter o file_id (o bot usa o file_id)."""
    u = await guard(request)
    f = await form(request, u)
    up = f.get("foto")
    if not up or not getattr(up, "filename", ""):
        go(f"/produtos/{pid}", err="Escolha uma imagem.")
    data = await up.read()
    if len(data) > 8 * 1024 * 1024:
        go(f"/produtos/{pid}", err="Imagem maior que 8 MB.")
    if not ADMIN_IDS:
        go(f"/produtos/{pid}", err="ADMIN_IDS não configurado.")
    try:
        m = await tg().send_photo(next(iter(ADMIN_IDS)), BufferedInputFile(data, filename=up.filename),
                                  caption="Foto de produto (pode apagar)", disable_notification=True)
        fid = m.photo[-1].file_id
        try:
            await tg().delete_message(m.chat.id, m.message_id)
        except Exception:
            pass
    except Exception as e:
        go(f"/produtos/{pid}", err=f"Telegram recusou a imagem: {type(e).__name__}")
    await R().x("UPDATE products SET image=? WHERE id=?", fid, pid)
    await audit(request, u, "trocou foto", "products", pid)
    go(f"/produtos/{pid}", "Foto aplicada.")


@app.get("/produtos/{pid}/foto")
async def produto_foto_ver(request: Request, pid: int):
    await guard(request)
    p = await R().one("SELECT image FROM products WHERE id=?", pid)
    if not p or not p["image"]:
        return Response(status_code=404)
    try:
        file = await tg().get_file(p["image"])
        buf = io.BytesIO()
        await tg().download_file(file.file_path, buf)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
    except Exception:
        return Response(status_code=404)


# ───────────────────────── fornecedores ─────────────────────────
@app.get("/fornecedores", response_class=HTMLResponse)
async def fornecedores(request: Request):
    u = await guard(request)
    sups = await R().q("SELECT s.*, (SELECT COUNT(*) FROM products p WHERE p.supplier_id=s.id) n FROM suppliers s ORDER BY name")
    return await render(request, "fornecedores.html", u, sups=sups)


@app.get("/fornecedores/{sid}", response_class=HTMLResponse)
async def fornecedor(request: Request, sid: str):
    u = await guard(request)
    s = None if sid == "novo" else await R().one("SELECT * FROM suppliers WHERE id=?", int(sid))
    c = forn.conf(s) if s else {}
    base = str(request.base_url).rstrip("/")
    return await render(request, "fornecedor.html", u, s=s, c=c, headers=json.dumps(c.get("headers") or {}, indent=2),
                        callback=f"{base}/webhook/{s['id']}" if s else "")


@app.post("/fornecedores/salvar")
async def fornecedor_save(request: Request):
    u = await guard(request)
    f = await form(request, u)
    sid = f.get("id")
    kind = str(f.get("kind")) if f.get("kind") in ("api", "buyer", "webhook", "manual") else "manual"
    old = await R().one("SELECT * FROM suppliers WHERE id=?", int(sid)) if sid else None
    oldc = forn.conf(old) if old else {}
    try:
        headers = json.loads(str(f.get("headers") or "{}") or "{}")
        assert isinstance(headers, dict)
    except Exception:
        go(f"/fornecedores/{sid or 'novo'}", err="Headers precisam ser um JSON de objeto, ex.: {\"Authorization\": \"Bearer {secret:FORN1}\"}")
    c = {"url": str(f.get("url", "")).strip(), "method": str(f.get("method") or "POST").upper(), "headers": headers,
         "body": str(f.get("body", "")), "path": str(f.get("path", "")).strip(), "timeout": num(f.get("timeout"), 20) or 20}
    if kind == "buyer":
        ks = re.sub(r"[^A-Z0-9_]", "", str(f.get("key_secret", "")).upper().removeprefix("SUP_"))[:40]
        if not ks:
            go(f"/fornecedores/{sid or 'novo'}", err="Informe o nome da chave (ex.: FORN1) e cadastre SUP_FORN1 no ENV_FILE.")
        c = {"url": c["url"].rstrip("/"), "prefix": "/" + str(f.get("prefix") or forn.BUYER_PREFIX).strip().strip("/"),
             "key_secret": ks, "min_balance": num(f.get("min_balance"), 0), "timeout": num(f.get("timeout"), 25) or 25}
    if kind in ("api", "buyer", "webhook") and not forn.public_url(c["url"]):
        go(f"/fornecedores/{sid or 'novo'}", err="URL precisa ser https/http pública (endereços internos são bloqueados).")
    if kind == "webhook":
        c["secret"] = oldc.get("secret") or secrets.token_hex(24)
        if f.get("new_secret"):
            c["secret"] = secrets.token_hex(24)
    if any(len(str(v)) > 30 and not str(v).count("{secret:") and k.lower() in ("authorization", "x-api-key", "api-key", "token")
           for k, v in headers.items()):
        go(f"/fornecedores/{sid or 'novo'}", err="Não cole chaves no header: use {secret:NOME} e cadastre SUP_NOME no ENV_FILE.")
    vals = (str(f.get("name", "")).strip()[:80] or "Fornecedor", kind, json.dumps(c, ensure_ascii=False),
            str(f.get("link", "")).strip()[:500], str(f.get("contact", "")).strip()[:200],
            str(f.get("notes", "")).strip()[:2000], 1 if f.get("active") else 0)
    if sid:
        await R().x("UPDATE suppliers SET name=?, kind=?, config=?, link=?, contact=?, notes=?, active=? WHERE id=?", *vals, int(sid))
        await audit(request, u, "editou fornecedor", "suppliers", sid, {"nome": vals[0], "tipo": kind, "url": c["url"]})
    else:
        sid = await R().x("INSERT INTO suppliers(name,kind,config,link,contact,notes,active) VALUES(?,?,?,?,?,?,?)", *vals)
        await audit(request, u, "criou fornecedor", "suppliers", sid, {"nome": vals[0], "tipo": kind})
    go(f"/fornecedores/{sid}", "Fornecedor salvo.")


@app.get("/fornecedores/{sid}/produtos", response_class=HTMLResponse)
async def fornecedor_produtos(request: Request, sid: int):
    """Catálogo do fornecedor (padrão Buyer API) para escolher o que revender."""
    u = await guard(request)
    s = await R().one("SELECT * FROM suppliers WHERE id=? AND kind='buyer'", sid)
    if not s:
        go("/fornecedores", err="Fornecedor não encontrado ou não é do tipo API de revenda.")
    prods, bal, erro = [], None, None
    try:
        prods = await forn.buyer_products(s)
        bal = await forn.buyer_balance(s)
    except Exception as e:
        erro = str(e) if isinstance(e, forn.SupplierError) else f"Sem resposta ({type(e).__name__})"
    mine = {r["sku"]: r for r in await R().q("SELECT id, sku, name, price, active FROM products WHERE supplier_id=?", sid)}
    return await render(request, "fornecedor_produtos.html", u, s=s, prods=prods, bal=bal, erro=erro, mine=mine)


@app.post("/fornecedores/{sid}/testar")
async def fornecedor_test(request: Request, sid: int):
    u = await guard(request)
    await form(request, u)
    s = await R().one("SELECT * FROM suppliers WHERE id=?", sid)
    ok, info = await forn.test(s)
    go(f"/fornecedores/{sid}", **({"msg": f"Conexão OK. {info}"} if ok else {"err": f"Falhou. {info}"}))


@app.post("/webhook/{sid}")
async def webhook_in(request: Request, sid: int):
    """Retorno do fornecedor webhook: {"pedido": "VX-...", "itens": ["..."]} ou {"pedido", "erro"}; assinado."""
    s = await R().one("SELECT * FROM suppliers WHERE id=? AND kind='webhook'", sid)
    body = await request.body()
    if not s or not forn.check_sig(forn.conf(s).get("secret"), body, request.headers.get("X-Vexa-Signature")):
        return JSONResponse({"ok": False, "erro": "assinatura inválida"}, status_code=401)
    try:
        d = json.loads(body)
    except ValueError:
        return JSONResponse({"ok": False, "erro": "json inválido"}, status_code=400)
    o = await R().one("SELECT * FROM orders WHERE code=? AND supplier_id=?", str(d.get("pedido", "")), sid)
    if not o:
        return JSONResponse({"ok": False, "erro": "pedido não encontrado"}, status_code=404)
    if o["status"] != "awaiting":
        return {"ok": True, "status": o["status"]}  # idempotente
    stamp = f"[{datetime.utcnow():%d/%m %H:%M:%S}] "
    if d.get("erro"):
        await R().x("UPDATE orders SET supplier_log=COALESCE(supplier_log,'')||? WHERE id=?",
                    stamp + f"webhook retornou erro: {str(d['erro'])[:300]}\n", o["id"])
        await R().x("UPDATE orders SET sent_at='2000-01-01 00:00:00' WHERE id=? AND status='awaiting'", o["id"])
        return {"ok": True, "status": "estorno"}  # o bot estorna no próximo ciclo (prazo vencido)
    itens = [str(i).strip() for i in (d.get("itens") or []) if str(i).strip()]
    if len(itens) < o["qty"]:
        return JSONResponse({"ok": False, "erro": f"esperado {o['qty']} item(ns)"}, status_code=422)
    if not await R().run("UPDATE orders SET status='ready' WHERE id=? AND status='awaiting'", o["id"]):
        return {"ok": True}
    await R().db.executemany("INSERT INTO stock(product_id,content,sold,order_id) VALUES(?,?,1,?)",
                             [(o["product_id"], i, o["id"]) for i in itens[:o["qty"]]])
    await R().x("UPDATE orders SET supplier_log=COALESCE(supplier_log,'')||? WHERE id=?",
                stamp + "webhook entregou\n", o["id"])
    return {"ok": True, "status": "ready"}


# ───────────────────────── pedidos e fila manual ─────────────────────────
@app.get("/pedidos", response_class=HTMLResponse)
async def pedidos(request: Request):
    u = await guard(request, "atendente")
    qp = request.query_params
    where, args = ["1=1"], []
    if qp.get("status"):
        where.append("o.status=?"); args.append(qp["status"])
    if qp.get("metodo"):
        where.append("o.method=?"); args.append(qp["metodo"])
    if qp.get("produto"):
        where.append("o.product_id=?"); args.append(int(qp["produto"]))
    if qp.get("de"):
        where.append("date(o.created_at,'-3 hours')>=?"); args.append(qp["de"])
    if qp.get("ate"):
        where.append("date(o.created_at,'-3 hours')<=?"); args.append(qp["ate"])
    if qp.get("cliente"):
        c = qp["cliente"].strip().lstrip("@")
        where.append("(CAST(o.user_id AS TEXT)=? OR us.username LIKE ? OR us.name LIKE ? OR o.code LIKE ?)")
        args += [c, f"%{c}%", f"%{c}%", f"%{c.upper()}%"]
    rows = await R().q("SELECT o.*, p.name pname, us.name uname, us.username FROM orders o "
                       "LEFT JOIN products p ON p.id=o.product_id LEFT JOIN users us ON us.id=o.user_id "
                       f"WHERE {' AND '.join(where)} ORDER BY o.id DESC LIMIT 300", *args)
    prods = await R().q("SELECT id, name FROM products ORDER BY name")
    return await render(request, "pedidos.html", u, rows=rows, prods=prods, qp=qp)


@app.get("/pedidos/{oid}", response_class=HTMLResponse)
async def pedido(request: Request, oid: int):
    u = await guard(request, "atendente")
    o = await R().one("SELECT o.*, p.name pname, p.ref_link, p.unlimited, us.name uname, us.username, s.name sname, s.link slink "
                      "FROM orders o LEFT JOIN products p ON p.id=o.product_id LEFT JOIN users us ON us.id=o.user_id "
                      "LEFT JOIN suppliers s ON s.id=o.supplier_id WHERE o.id=?", oid)
    if not o:
        go("/pedidos", err="Pedido não encontrado.")
    items = [r["content"] for r in await R().q("SELECT content FROM stock WHERE order_id=? ORDER BY id", oid)]
    timeline = [("Criado", o["created_at"]), ("Pago", o["paid_at"]), ("Enviado ao fornecedor", o["sent_at"]),
                ("Entregue", o["delivered_at"])]
    # o que o cliente fez desde 30 min antes do pedido até a entrega (+ tudo ligado a este código)
    events = await R().q("SELECT * FROM events WHERE user_id=? AND (ref=? OR created_at BETWEEN datetime(?, '-30 minutes') "
                         "AND datetime(COALESCE(?, ?), '+30 minutes')) ORDER BY id DESC LIMIT 80",
                         o["user_id"], o["code"], o["created_at"], o["delivered_at"], o["created_at"])
    return await render(request, "pedido.html", u, o=o, items=items, timeline=timeline, events=events)


async def get_order(oid):
    o = await R().one("SELECT * FROM orders WHERE id=?", oid)
    if not o:
        go("/pedidos", err="Pedido não encontrado.")
    return o


@app.post("/pedidos/{oid}/entregar")
async def pedido_entregar(request: Request, oid: int):
    """Fila manual / marcar como entregue: grava a entrega e o bot envia na tela de entrega."""
    u = await guard(request, "atendente")
    f = await form(request, u)
    o = await get_order(oid)
    back = str(f.get("back") or "")
    if not back.startswith("/") or back.startswith("//"):
        back = f"/pedidos/{oid}"  # evita redirecionamento para fora do painel
    items = [x.strip() for x in str(f.get("items", "")).splitlines() if x.strip()]
    if o["kind"] != "buy" or o["status"] not in ("manual", "awaiting", "paid", "delivering"):
        go(back, err="Esse pedido não está aguardando entrega.")
    if len(items) < o["qty"]:
        go(back, err=f"Cole {o['qty']} entrega(s), uma por linha.")
    if not await R().run("UPDATE orders SET status='ready' WHERE id=? AND status=?", oid, o["status"]):
        go(back, err="O pedido mudou de status; recarregue a página.")
    await R().db.executemany("INSERT INTO stock(product_id,content,sold,order_id) VALUES(?,?,1,?)",
                             [(o["product_id"], i, oid) for i in items[:o["qty"]]])
    await R().x("UPDATE orders SET supplier_log=COALESCE(supplier_log,'')||? WHERE id=?",
                f"[{datetime.utcnow():%d/%m %H:%M:%S}] entregue manualmente por {u['username']}\n", oid)
    await audit(request, u, "entregou pedido manualmente", "orders", o["code"], f"{len(items[:o['qty']])} item(ns)")
    await R().event(o["user_id"], "suporte", f"Entrega manual feita por {u['username']}", o["code"])
    go(back, f"Pedido {o['code']} enviado ao cliente.")


@app.post("/pedidos/{oid}/reenviar")
async def pedido_reenviar(request: Request, oid: int):
    u = await guard(request, "atendente")
    await form(request, u)
    o = await get_order(oid)
    if o["status"] != "delivered":
        go(f"/pedidos/{oid}", err="Só pedidos entregues podem ser reenviados.")
    await R().x("UPDATE orders SET resend=1 WHERE id=?", oid)
    await audit(request, u, "reenviou entrega", "orders", o["code"])
    go(f"/pedidos/{oid}", "Reenvio agendado (chega em segundos).")


@app.post("/pedidos/{oid}/estornar")
async def pedido_estornar(request: Request, oid: int):
    u = await guard(request)
    f = await form(request, u)
    o = await get_order(oid)
    motivo = str(f.get("motivo", "")).strip()
    if not motivo:
        go(f"/pedidos/{oid}", err="Informe o motivo do estorno.")
    if o["status"] not in ("delivered", "manual", "awaiting", "paid"):
        go(f"/pedidos/{oid}", err="Esse pedido não pode ser estornado.")
    if not await R().run("UPDATE orders SET status='refunded' WHERE id=? AND status=?", oid, o["status"]):
        go(f"/pedidos/{oid}", err="O pedido mudou de status; recarregue.")
    await R().credit(o["user_id"], o["amount"], "estorno", o["code"])
    com = await R().one("SELECT user_id, delta FROM ledger WHERE ref=? AND reason='comissao'", o["code"])
    if com and not await R().one("SELECT 1 FROM ledger WHERE ref=? AND reason='comissao estornada'", o["code"]):
        await R().credit(com["user_id"], -com["delta"], "comissao estornada", o["code"])  # pode ficar negativo
        await R().x("UPDATE users SET ref_earned=ref_earned-? WHERE id=?", com["delta"], com["user_id"])
    await audit(request, u, "estornou em saldo", "orders", o["code"],
                {"valor": o["amount"], "motivo": motivo, "comissao_desfeita": com["delta"] if com else 0})
    await R().event(o["user_id"], "suporte", f"Estorno de {brl(o['amount'])} por {u['username']}: {motivo}", o["code"])
    try:
        await tg().send_message(o["user_id"], f"↩️ <b>Estorno do pedido</b> <code>{o['code']}</code>\n"
                                              f"<b>{brl(o['amount'])}</b> foram creditados na sua carteira.\nMotivo: {html_esc(motivo)}",
                                reply_markup=kb([("Ver minha conta", "me")]))
    except Exception:
        pass
    go(f"/pedidos/{oid}", f"Estornado {brl(o['amount'])} em saldo.")


@app.get("/fila", response_class=HTMLResponse)
async def fila(request: Request):
    u = await guard(request, "atendente")
    rows = await R().q("SELECT o.*, p.name pname, p.ref_link, p.notes pnotes, s.name sname, s.link slink, us.name uname, "
                       "us.username FROM orders o LEFT JOIN products p ON p.id=o.product_id LEFT JOIN suppliers s "
                       "ON s.id=COALESCE(o.supplier_id,p.supplier_id) LEFT JOIN users us ON us.id=o.user_id "
                       "WHERE o.status IN('manual','awaiting') ORDER BY o.id")
    return await render(request, "fila.html", u, rows=rows)


# ───────────────────────── clientes ─────────────────────────
@app.get("/clientes", response_class=HTMLResponse)
async def clientes(request: Request):
    u = await guard(request)
    q = request.query_params.get("q", "").strip().lstrip("@")
    where, args = "", []
    if q:
        where = "WHERE CAST(us.id AS TEXT)=? OR us.username LIKE ? OR us.name LIKE ?"
        args = [q, f"%{q}%", f"%{q}%"]
    rows = await R().q("SELECT us.*, (SELECT COUNT(*) FROM orders o WHERE o.user_id=us.id AND o.status='delivered' "
                       f"AND o.kind='buy') compras FROM users us {where} ORDER BY us.spent DESC, us.created_at DESC LIMIT 300", *args)
    return await render(request, "clientes.html", u, rows=rows, q=q)


@app.get("/clientes/{uid}", response_class=HTMLResponse)
async def cliente(request: Request, uid: int):
    u = await guard(request)
    c = await R().one("SELECT * FROM users WHERE id=?", uid)
    if not c:
        go("/clientes", err="Cliente não encontrado.")
    ref = await R().one("SELECT id, name, username FROM users WHERE id=?", c["ref_by"]) if c["ref_by"] else None
    refs = await R().q("SELECT id, name, username, spent FROM users WHERE ref_by=? ORDER BY spent DESC", uid)
    orders = await R().q("SELECT o.*, p.name pname FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                         "WHERE o.user_id=? ORDER BY o.id DESC LIMIT 100", uid)
    ledger = await R().q("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 100", uid)
    events = await R().q("SELECT * FROM events WHERE user_id=? ORDER BY id DESC LIMIT 200", uid)
    return await render(request, "cliente.html", u, c=c, ref=ref, refs=refs, orders=orders, ledger=ledger, events=events)


@app.post("/clientes/{uid}/saldo")
async def cliente_saldo(request: Request, uid: int):
    u = await guard(request)
    f = await form(request, u)
    valor, motivo = round(num(f.get("valor")), 2), str(f.get("motivo", "")).strip()
    if not valor or not motivo:
        go(f"/clientes/{uid}", err="Valor (≠ 0) e motivo são obrigatórios.")
    c = await R().one("SELECT balance FROM users WHERE id=?", uid)
    if c["balance"] + valor < 0:
        go(f"/clientes/{uid}", err="O saldo não pode ficar negativo.")
    await R().credit(uid, valor, f"ajuste: {motivo[:200]}", u["username"])
    await audit(request, u, "ajustou saldo", "users", uid, {"valor": valor, "antes": c["balance"], "motivo": motivo})
    await R().event(uid, "suporte", f"Saldo ajustado em {brl(valor)} por {u['username']}: {motivo}")
    go(f"/clientes/{uid}", f"Saldo ajustado em {brl(valor)}.")


# ───────────────────────── financeiro ─────────────────────────
def period(qp):
    today = (datetime.utcnow() + TZ).date()
    de = qp.get("de") or today.replace(day=1).isoformat()
    ate = qp.get("ate") or today.isoformat()
    return de, ate


async def fin_data(de, ate):
    days = await R().q(f"SELECT {LOCALDAY} d, COUNT(*) n, SUM(amount) fat, SUM(cost) custo FROM orders "
                       f"WHERE status='delivered' AND kind='buy' AND {LOCALDAY} BETWEEN ? AND ? GROUP BY d ORDER BY d DESC", de, ate)
    months = await R().q(f"SELECT substr({LOCALDAY},1,7) m, COUNT(*) n, SUM(amount) fat, SUM(cost) custo FROM orders "
                         "WHERE status='delivered' AND kind='buy' GROUP BY m ORDER BY m DESC LIMIT 12")
    prods = await R().q(f"SELECT p.name, COUNT(*) n, SUM(o.qty) un, SUM(o.amount) fat, SUM(o.cost) custo FROM orders o "
                        f"JOIN products p ON p.id=o.product_id WHERE o.status='delivered' AND o.kind='buy' "
                        f"AND date(o.paid_at,'-3 hours') BETWEEN ? AND ? GROUP BY p.id ORDER BY fat DESC", de, ate)
    led = await R().q("SELECT CASE WHEN reason LIKE 'ajuste%' THEN 'ajuste' ELSE reason END r, COUNT(*) n, SUM(delta) v "
                      "FROM ledger WHERE date(created_at,'-3 hours') BETWEEN ? AND ? GROUP BY r", de, ate)
    methods = await R().q(f"SELECT method, kind, COUNT(*) n, SUM(amount) v FROM orders WHERE paid_at IS NOT NULL "
                          f"AND {LOCALDAY} BETWEEN ? AND ? GROUP BY method, kind", de, ate)
    return days, months, prods, {r["r"]: r for r in led}, methods


@app.get("/financeiro", response_class=HTMLResponse)
async def financeiro(request: Request):
    u = await guard(request)
    de, ate = period(request.query_params)
    days, months, prods, led, methods = await fin_data(de, ate)
    tot = await money(f"{LOCALDAY} BETWEEN ? AND ?", (de, ate))
    conf = None
    if request.query_params.get("conferir"):
        try:
            asa = await asaas.received(de, ate)
            ours = {r["ext_id"]: r for r in await R().q(
                "SELECT code, ext_id, amount, status FROM orders WHERE method='pix' AND ext_id IS NOT NULL")}
            ids = {p["id"] for p in asa}
            conf = {"asaas_n": len(asa), "asaas_v": sum(p["value"] for p in asa),
                    "sem_pedido": [p for p in asa if p["id"] not in ours],
                    "nao_pago": [{**p, "status": ours[p["id"]]["status"], "code": ours[p["id"]]["code"]} for p in asa
                                 if p["id"] in ours and ours[p["id"]]["status"] in ("pending", "expired", "canceled", "failed")],
                    "so_aqui": [dict(r) for r in await R().q(
                        f"SELECT code, ext_id, amount FROM orders WHERE method='pix' AND paid_at IS NOT NULL "
                        f"AND {LOCALDAY} BETWEEN ? AND ? AND ext_id NOT LIKE 'pay_demo%'", de, ate) if r["ext_id"] not in ids]}
        except Exception as e:
            conf = {"erro": f"Asaas indisponível: {type(e).__name__}"}
    return await render(request, "financeiro.html", u, de=de, ate=ate, days=days, months=months, prods=prods,
                        led=led, methods=methods, tot=tot, conf=conf)


@app.get("/financeiro/csv")
async def financeiro_csv(request: Request):
    u = await guard(request)
    de, ate = period(request.query_params)
    tipo = request.query_params.get("tipo", "pedidos")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    if tipo == "extrato":
        w.writerow(["data", "cliente_id", "valor", "motivo", "referencia"])
        for r in await R().q("SELECT * FROM ledger WHERE date(created_at,'-3 hours') BETWEEN ? AND ? ORDER BY id", de, ate):
            w.writerow([local(r["created_at"]), r["user_id"], f"{r['delta']:.2f}".replace(".", ","), r["reason"], r["ref"]])
    else:
        w.writerow(["pedido", "data_pagamento", "cliente_id", "tipo", "produto", "qtd", "valor", "custo", "lucro",
                    "desconto", "cupom", "forma", "status"])
        for r in await R().q(f"SELECT o.*, p.name pname FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                             f"WHERE o.paid_at IS NOT NULL AND date(o.paid_at,'-3 hours') BETWEEN ? AND ? ORDER BY o.id", de, ate):
            br = lambda v: f"{(v or 0):.2f}".replace(".", ",")
            w.writerow([r["code"], local(r["paid_at"]), r["user_id"], r["kind"], r["pname"] or "Recarga", r["qty"],
                        br(r["amount"]), br(r["cost"]), br((r["amount"] or 0) - (r["cost"] or 0)), br(r["discount"]),
                        r["coupon"] or "", r["method"], r["status"]])
    await audit(request, u, "exportou CSV", "financeiro", tipo, f"{de} a {ate}")
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="vexa-{tipo}-{de}-{ate}.csv"'})


# ───────────────────────── chamados ─────────────────────────
@app.get("/chamados", response_class=HTMLResponse)
async def chamados(request: Request):
    u = await guard(request, "atendente")
    st = request.query_params.get("status", "")
    rows = await R().q("SELECT t.*, us.name uname, us.username, (SELECT COUNT(*) FROM ticket_messages m WHERE m.ticket_id=t.id) n "
                       "FROM tickets t LEFT JOIN users us ON us.id=t.user_id " + ("WHERE t.status=? " if st else "") +
                       "ORDER BY CASE t.status WHEN 'aberto' THEN 0 WHEN 'respondido' THEN 1 ELSE 2 END, "
                       "COALESCE(t.updated_at,t.created_at) DESC LIMIT 300", *([st] if st else []))
    return await render(request, "chamados.html", u, rows=rows, st=st)


@app.get("/chamados/{tid}", response_class=HTMLResponse)
async def chamado(request: Request, tid: int):
    u = await guard(request, "atendente")
    t = await R().one("SELECT t.*, us.name uname, us.username FROM tickets t LEFT JOIN users us ON us.id=t.user_id "
                      "WHERE t.id=?", tid)
    if not t:
        go("/chamados", err="Chamado não encontrado.")
    msgs = await R().q("SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id", tid)
    if not msgs:  # chamado antigo, antes das conversas
        msgs = [{"sender": "cliente", "staff": "", "text": t["text"], "created_at": t["created_at"]}]
    o = await R().one("SELECT id, code, status FROM orders WHERE code=?", t["order_code"])
    return await render(request, "chamado.html", u, t=t, msgs=msgs, o=o, cx=await client_context(t["user_id"]))


@app.post("/chamados/{tid}/responder")
async def chamado_responder(request: Request, tid: int):
    u = await guard(request, "atendente")
    f = await form(request, u)
    text = str(f.get("text", "")).strip()
    t = await R().one("SELECT * FROM tickets WHERE id=?", tid)
    if not t or not text:
        go(f"/chamados/{tid}", err="Escreva a resposta.")
    try:
        await tg().send_message(t["user_id"], f"💬 <b>Suporte VEXA</b> · chamado #{tid}\n{html_esc(text)}",
                                reply_markup=kb([("Responder", f"tr:{tid}")]))
    except Exception as e:
        go(f"/chamados/{tid}", err=f"Não consegui enviar no Telegram ({type(e).__name__}).")
    await R().x("INSERT INTO ticket_messages(ticket_id,sender,staff,text) VALUES(?,?,?,?)", tid, "suporte", u["username"], text)
    novo = "resolvido" if f.get("resolver") else "respondido"
    await R().x("UPDATE tickets SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", novo, tid)
    await audit(request, u, "respondeu chamado", "tickets", tid, text[:200])
    await R().event(t["user_id"], "suporte", f"Resposta do suporte ({u['username']}) no chamado #{tid}: {text}", t["order_code"])
    go(f"/chamados/{tid}", "Resposta enviada ao cliente.")


@app.post("/chamados/{tid}/status")
async def chamado_status(request: Request, tid: int):
    u = await guard(request, "atendente")
    f = await form(request, u)
    st = str(f.get("status"))
    if st in TICKET:
        await R().x("UPDATE tickets SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", st, tid)
        await audit(request, u, f"chamado → {st}", "tickets", tid)
    go(f"/chamados/{tid}", "Status atualizado.")


# ───────────────────────── cupons ─────────────────────────
@app.get("/cupons", response_class=HTMLResponse)
async def cupons(request: Request):
    u = await guard(request)
    rows = await R().q("SELECT c.*, (SELECT COUNT(*) FROM orders o WHERE o.coupon=c.code AND o.status='delivered') usados "
                       "FROM coupons c ORDER BY active DESC, code")
    return await render(request, "cupons.html", u, rows=rows)


@app.post("/cupons/salvar")
async def cupom_save(request: Request):
    u = await guard(request)
    f = await form(request, u)
    code = "".join(ch for ch in str(f.get("code", "")).upper() if ch.isalnum() or ch in "-_")[:30]
    pct = num(f.get("pct"))
    if not code or not 0 < pct < 100:
        go("/cupons", err="Código e % entre 1 e 99 são obrigatórios.")
    uses = int(num(f.get("uses"), -1)) if str(f.get("uses", "")).strip() else -1
    exp = str(f.get("expires_at", "")).strip() or None
    old = await R().one("SELECT * FROM coupons WHERE code=?", code)
    await R().x("INSERT INTO coupons(code,pct,uses_left,active,expires_at) VALUES(?,?,?,1,?) ON CONFLICT(code) DO UPDATE SET "
                "pct=excluded.pct, uses_left=excluded.uses_left, expires_at=excluded.expires_at", code, pct, uses, exp)
    await audit(request, u, "editou cupom" if old else "criou cupom", "coupons", code,
                {"pct": pct, "usos": uses, "validade": exp, "antes": dict(old) if old else None})
    go("/cupons", f"Cupom {code} salvo.")


@app.post("/cupons/{code}/alternar")
async def cupom_toggle(request: Request, code: str):
    u = await guard(request)
    await form(request, u)
    await R().x("UPDATE coupons SET active=1-active WHERE code=?", code)
    c = await R().one("SELECT active FROM coupons WHERE code=?", code)
    await audit(request, u, "ativou cupom" if c and c["active"] else "desativou cupom", "coupons", code)
    go("/cupons", "Cupom atualizado.")


# ───────────────────────── avisos em massa ─────────────────────────
FILTERS = {"todos": "Todos os clientes", "compradores": "Quem já comprou", "nunca": "Quem nunca comprou",
           "saldo": "Quem tem saldo na carteira", "produto": "Quem comprou o produto…"}


async def audience(filtro, pid=None):
    if filtro == "compradores":
        sql, a = "SELECT DISTINCT user_id id FROM orders WHERE status='delivered' AND kind='buy'", []
    elif filtro == "nunca":
        sql, a = "SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM orders WHERE status='delivered' AND kind='buy')", []
    elif filtro == "saldo":
        sql, a = "SELECT id FROM users WHERE balance>0", []
    elif filtro == "produto" and pid:
        sql, a = "SELECT DISTINCT user_id id FROM orders WHERE status='delivered' AND product_id=?", [int(pid)]
    else:
        sql, a = "SELECT id FROM users", []
    return [r["id"] for r in await R().q(sql, *a)]


@app.get("/avisos", response_class=HTMLResponse)
async def avisos(request: Request):
    u = await guard(request)
    prods = await R().q("SELECT id, name FROM products ORDER BY name")
    hist = await R().q("SELECT * FROM audit WHERE action='enviou aviso' ORDER BY id DESC LIMIT 10")
    return await render(request, "avisos.html", u, prods=prods, filters=FILTERS, hist=hist, prev=None)


@app.post("/avisos/previa", response_class=HTMLResponse)
async def avisos_previa(request: Request):
    u = await guard(request)
    f = await form(request, u)
    text, filtro, pid = str(f.get("text", "")).strip(), str(f.get("filtro", "todos")), f.get("produto") or None
    if not text:
        go("/avisos", err="Escreva a mensagem.")
    ids = await audience(filtro, pid)
    prods = await R().q("SELECT id, name FROM products ORDER BY name")
    return await render(request, "avisos.html", u, prods=prods, filters=FILTERS, hist=[],
                        prev={"text": text, "filtro": filtro, "pid": pid, "n": len(ids)})


async def broadcast(ids, text):
    n = 0
    for uid in ids:
        try:
            await tg().send_message(uid, text)
            n += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # ~20 msg/s, dentro do limite do Telegram
    await notify_admins(f"📣 Aviso enviado para {n}/{len(ids)} cliente(s).")


@app.post("/avisos/enviar")
async def avisos_enviar(request: Request):
    u = await guard(request)
    f = await form(request, u)
    text, filtro, pid = str(f.get("text", "")).strip(), str(f.get("filtro", "todos")), f.get("produto") or None
    ids = await audience(filtro, pid)
    if not text or not ids:
        go("/avisos", err="Nada para enviar.")
    await audit(request, u, "enviou aviso", "broadcast", filtro, {"para": len(ids), "texto": text[:500]})
    task = asyncio.create_task(broadcast(ids, text))
    ctx.setdefault("tasks", set()).add(task)
    task.add_done_callback(ctx["tasks"].discard)
    go("/avisos", f"Enviando para {len(ids)} cliente(s). Você recebe a confirmação no Telegram.")


# ───────────────────────── configurações, usuários, auditoria ─────────────────────────
@app.get("/config", response_class=HTMLResponse)
async def config(request: Request):
    u = await guard(request)
    await store.load(R())
    return await render(request, "config.html", u, cfg=store.CFG, labels=store.LABELS)


@app.post("/config")
async def config_save(request: Request):
    u = await guard(request)
    f = await form(request, u)
    await store.load(R())
    changes = {}
    for k in store.DEFAULTS:
        v = ("1" if f.get(k) else "0") if k in ("pix_on", "usdt_on") else str(f.get(k, "")).strip()
        if k in ("ref_pct", "order_timeout_min", "webhook_timeout_min"):
            v = str(max(0.0, num(v, float(store.DEFAULTS[k])))).removesuffix(".0")
        if v != store.CFG.get(k):
            changes[k] = [store.CFG.get(k), v]
            await R().set_setting(k, v)
    if changes:
        await audit(request, u, "alterou configurações", "settings", "", changes)
    go("/config", "Configurações salvas. O bot aplica em até 10 segundos.")


@app.get("/usuarios", response_class=HTMLResponse)
async def usuarios(request: Request):
    u = await guard(request)
    rows = await R().q("SELECT * FROM panel_users ORDER BY username")
    return await render(request, "usuarios.html", u, rows=rows, newpw=request.query_params.get("pw"))


@app.post("/usuarios/salvar")
async def usuario_save(request: Request):
    u = await guard(request)
    f = await form(request, u)
    role = str(f.get("role")) if f.get("role") in auth.ROLES else "atendente"
    uid = f.get("id")
    if uid:
        target = await R().one("SELECT * FROM panel_users WHERE id=?", int(uid))
        active = 1 if f.get("active") else 0
        if target["id"] == u["id"] and (role != "admin" or not active):
            go("/usuarios", err="Você não pode rebaixar ou desativar a si mesmo.")
        await R().x("UPDATE panel_users SET role=?, active=? WHERE id=?", role, active, int(uid))
        if not active:
            await R().x("DELETE FROM panel_sessions WHERE user_id=?", int(uid))
        msg, pw = "Usuário atualizado.", None
        if f.get("reset"):
            pw = auth.strong_password()
            await R().x("UPDATE panel_users SET pass_hash=?, must_change=1 WHERE id=?", auth.hash_pw(pw), int(uid))
            await R().x("DELETE FROM panel_sessions WHERE user_id=?", int(uid))
        await audit(request, u, "editou usuário", "panel_users", target["username"],
                    {"papel": role, "ativo": active, "senha_resetada": bool(pw)})
    else:
        name = "".join(ch for ch in str(f.get("username", "")).lower() if ch.isalnum() or ch in "._-")[:40]
        if not name or await R().one("SELECT 1 FROM panel_users WHERE username=?", name):
            go("/usuarios", err="Usuário vazio ou já existente.")
        pw = auth.strong_password()
        await R().x("INSERT INTO panel_users(username,pass_hash,role,must_change) VALUES(?,?,?,1)", name, auth.hash_pw(pw), role)
        await audit(request, u, "criou usuário", "panel_users", name, {"papel": role})
        msg = f"Usuário {name} criado."
    if pw:
        # senha temporária exibida uma única vez para quem criou/resetou (troca obrigatória no 1º login)
        return await render(request, "usuarios.html", u, rows=await R().q("SELECT * FROM panel_users ORDER BY username"),
                            newpw=pw, flash=msg)
    go("/usuarios", msg)


@app.get("/logs", response_class=HTMLResponse)
async def logs(request: Request):
    """Histórico de todos os clientes: cliques, mensagens, pedidos, pagamentos, entregas, erros e suporte."""
    u = await guard(request)
    qp = request.query_params
    where, args = ["1=1"], []
    if qp.get("tipo"):
        where.append("e.kind=?"); args.append(qp["tipo"])
    if qp.get("q"):
        q = qp["q"].strip().lstrip("@")
        where.append("(CAST(e.user_id AS TEXT)=? OR e.ref LIKE ? OR e.detail LIKE ? OR us.username LIKE ? OR us.name LIKE ?)")
        args += [q, f"%{q.upper()}%", f"%{q}%", f"%{q}%", f"%{q}%"]
    rows = await R().q("SELECT e.*, us.name uname, us.username FROM events e LEFT JOIN users us ON us.id=e.user_id "
                       f"WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT 500", *args)
    erros24 = (await R().one("SELECT COUNT(*) n FROM events WHERE kind='erro' AND created_at>=datetime('now','-1 day')"))["n"]
    return await render(request, "logs.html", u, rows=rows, qp=qp, erros24=erros24)


@app.get("/auditoria", response_class=HTMLResponse)
async def auditoria(request: Request):
    u = await guard(request)
    q = request.query_params.get("q", "").strip()
    rows = await R().q("SELECT * FROM audit " + ("WHERE who LIKE ? OR action LIKE ? OR entity_id LIKE ? OR detail LIKE ? " if q else "")
                       + "ORDER BY id DESC LIMIT 500", *([f"%{q}%"] * 4 if q else []))
    return await render(request, "auditoria.html", u, rows=rows, q=q)
