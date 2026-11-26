import asyncio
import base64
import html
import json
import logging
import time
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InputMediaPhoto, Message)

from . import asaas, binance_pay as bp, fornecedores as forn, store
from .config import ADMIN_IDS, BOT_TOKEN, CHANNEL_ID, PANEL_DOMAIN, POLL_SECONDS, SANDBOX
from .db import Repo, connect
from .store import cfg, num, on
from .ui import LINE, b, back, brl, header, home_kb, kb, stock_badge, usd

logging.basicConfig(level=logging.INFO)
router = Router()
repo: Repo
bot: Bot
BANNERS = Path(__file__).resolve().parent.parent / "assets" / "banners"
PAGE = 6
PIX_MIN = 5.0  # mínimo de cobrança do Asaas
TOPUP_VALUES = [20, 50, 100, 200]


class S(StatesGroup):
    cpf = State()
    coupon = State()
    ticket = State()
    ticket_reply = State()


def is_admin(uid): return uid in ADMIN_IDS


def esc(v):
    """Escapa texto vindo do cliente/fornecedor antes de pôr numa mensagem HTML do Telegram."""
    return html.escape(str(v if v is not None else ""), quote=False)


def ints(data, n):
    """Lê n inteiros de um callback 'x:1:2'. callback_data pode ser forjado: inválido → None."""
    parts = data.split(":")[1:]
    if len(parts) < n:
        return None
    try:
        return [int(x) for x in parts[:n]]
    except ValueError:
        return None


async def sellable(pid):
    """Produto existe, está ativo e a categoria existe."""
    return await repo.one("SELECT p.* FROM products p JOIN categories c ON c.id=p.category_id WHERE p.id=? AND p.active=1", pid)


# ───────────────────────── histórico de atividade (para o suporte) ─────────────────────────
CB_FIXED = {"home": "Início", "cat": "Abriu o catálogo", "cupom": "Clicou em Tenho cupom", "me": "Abriu Minha conta",
            "topup": "Abriu Recarregar saldo", "orders": "Abriu Meus pedidos", "ref": "Abriu Indique e ganhe",
            "sup": "Abriu Suporte", "del": "Fechou aviso", "noop": None}
PAY = {"pix": "Pix", "usdt": "USDT", "bal": "saldo", "saldo": "saldo"}


async def describe_cb(data):
    if data in CB_FIXED:
        return CB_FIXED[data]
    k, _, rest = data.partition(":")
    a = rest.split(":")
    if k in ("c", "p", "buy") and a[0].isdigit():
        row = await repo.one(f"SELECT name FROM {'categories' if k == 'c' else 'products'} WHERE id=?", int(a[0]))
        name = row["name"] if row else f"#{a[0]}"
        qty = a[1] if len(a) > 1 else 1
        return {"c": f"Categoria {name}", "p": f"Viu produto {name} (qtd {qty})", "buy": f"Clicou Comprar: {name} ×{qty}"}[k]
    return {"pay": f"Escolheu pagar com {PAY.get(rest, rest)}", "chk": f"Clicou Já paguei (pedido #{rest})",
            "cx": f"Cancelou o pedido #{rest}", "tp": f"Escolheu recarga de R$ {rest}",
            "od": f"Reabriu o pedido {rest}", "tk": f"Acionou garantia do pedido {rest}",
            "tr": f"Foi responder o chamado #{rest}"}.get(k, f"Clicou {data}")


class Activity(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            if isinstance(event, CallbackQuery):
                txt = await describe_cb(event.data or "")
                if txt:
                    await repo.event(event.from_user.id, "clique", txt)
            elif isinstance(event, Message) and event.from_user:
                st = await data["state"].get_state() if data.get("state") else None
                t = event.text or event.caption or ("(foto)" if event.photo else "(mídia)")
                if st == S.cpf.state:
                    t = "Enviou o CPF (oculto)"
                elif st == S.coupon.state:
                    t = f"Digitou o cupom {t.strip().upper()[:30]}"
                elif st in (S.ticket.state, S.ticket_reply.state):
                    t = f"Mensagem no chamado: {t}"
                elif not t.startswith("/"):
                    t = f"Mensagem: {t}"
                await repo.event(event.from_user.id, "mensagem", t)
        except Exception:
            logging.exception("atividade")
        return await handler(event, data)


router.message.outer_middleware(Activity())
router.callback_query.outer_middleware(Activity())


# ───────────────────────── tela única (edita a mesma mensagem) ─────────────────────────
async def media(key: str):
    """Banner da marca: envia o arquivo uma vez e reaproveita o file_id."""
    r = await repo.one("SELECT file_id FROM media WHERE key=?", key)
    return r["file_id"] if r else FSInputFile(BANNERS / f"{key}.png")


async def remember(key, msg: Message):
    if msg and msg.photo and not (await repo.one("SELECT 1 FROM media WHERE key=?", key)):
        await repo.x("INSERT OR REPLACE INTO media(key,file_id) VALUES(?,?)", key, msg.photo[-1].file_id)


async def screen(target, key, caption, markup, photo=None):
    """Renderiza uma tela. Callback → edita no lugar; Message → envia nova."""
    pic = photo or await media(key)
    if isinstance(target, CallbackQuery):
        try:
            m = await target.message.edit_media(InputMediaPhoto(media=pic, caption=caption), reply_markup=markup)
            await target.answer()
            if not photo:
                await remember(key, m if isinstance(m, Message) else None)
            return target.message
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                await target.answer()
                return target.message
            target = target.message  # mensagem antiga sem foto → envia nova
    m = await target.answer_photo(pic, caption=caption, reply_markup=markup)
    if not photo:
        await remember(key, m)
    return m


async def user(u):
    await repo.x("INSERT INTO users(id,username,name) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                 "username=excluded.username, name=excluded.name", u.id, u.username, u.full_name)
    return await repo.one("SELECT * FROM users WHERE id=?", u.id)


# ───────────────────────── início ─────────────────────────
async def home_caption(u):
    first = esc((u["name"] or "").split(" ")[0] or "cliente")
    refs = (await repo.one("SELECT COUNT(*) n FROM users WHERE ref_by=?", u["id"]))["n"]
    extra = f"\nIndicações  <b>{refs}</b>  ·  ganhos <b>{brl(u['ref_earned'])}</b>" if refs else ""
    return (f"<b>Olá, {first}</b>\n\n"
            f"Saldo  <b>{brl(u['balance'])}</b>{extra}\n\n"
            f"<i>{cfg('txt_home')}</i>")


@router.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    new = not await repo.one("SELECT 1 FROM users WHERE id=?", m.from_user.id)
    u = await user(m.from_user)
    if new and command.args and command.args.startswith("r") and command.args[1:].isdigit():
        ref = int(command.args[1:])
        if ref != m.from_user.id:
            await repo.x("UPDATE users SET ref_by=? WHERE id=?", ref, m.from_user.id)
    await screen(m, "welcome", await home_caption(u), home_kb())


@router.callback_query(F.data == "home")
async def home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await user(cb.from_user)
    await screen(cb, "welcome", await home_caption(u), home_kb())


# ───────────────────────── catálogo ─────────────────────────
@router.callback_query(F.data == "cat")
async def catalog(cb: CallbackQuery):
    cats = await repo.q("SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id AND p.active=1) n "
                        "FROM categories c ORDER BY sort, name")
    rows = [[b(f"{c['emoji']}  {c['name']}  ·  {c['n']}", f"c:{c['id']}:0")] for c in cats if c["n"]]
    cap = header("Catálogo", "Escolha uma categoria") + ("\nNenhum produto disponível agora." if not rows else "")
    await screen(cb, "catalog", cap, kb(*rows, back()))


async def product_rows(prods):
    rows = []
    for p in prods:
        st = await repo.stock(p["id"])
        mark = "⛔" if st == 0 else "🔥" if st is not None and st <= 5 else "•"
        rows.append([b(f"{mark}  {p['name']}  —  {brl(p['price'])}", f"p:{p['id']}:1")])
    return rows


@router.callback_query(F.data.startswith("c:"))
async def category(cb: CallbackQuery):
    v = ints(cb.data, 2)
    c = await repo.one("SELECT * FROM categories WHERE id=?", v[0]) if v else None
    if not c:
        return await cb.answer()
    cid, pg = v[0], max(0, v[1])
    total = (await repo.one("SELECT COUNT(*) n FROM products WHERE category_id=? AND active=1", cid))["n"]
    prods = await repo.q("SELECT * FROM products WHERE category_id=? AND active=1 ORDER BY sort, sold DESC, name "
                         "LIMIT ? OFFSET ?", cid, PAGE, pg * PAGE)
    rows = await product_rows(prods)
    pages = max(1, -(-total // PAGE))
    nav = []
    if pg > 0:
        nav.append(b("‹", f"c:{cid}:{pg - 1}"))
    if pages > 1:
        nav.append(b(f"{pg + 1}/{pages}", "noop"))
    if pg < pages - 1:
        nav.append(b("›", f"c:{cid}:{pg + 1}"))
    await screen(cb, "catalog", header(f"{c['emoji']} {c['name']}", f"{total} produto(s)"),
                 kb(*rows, nav, back("cat")))


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery): await cb.answer()


@router.callback_query(F.data.startswith("p:"))
async def product(cb: CallbackQuery):
    v = ints(cb.data, 2)
    p = await sellable(v[0]) if v else None
    if not p:
        return await cb.answer("Produto indisponível.", show_alert=True)
    pid, qty = p["id"], v[1]
    st = await repo.stock(p["id"])
    cap_qty = p["max_qty"] if st is None else min(p["max_qty"], st)
    qty = max(1, min(int(qty), max(cap_qty, 1)))
    price = f"<b>{brl(p['price'])}</b>"
    if p["old_price"] and p["old_price"] > p["price"]:
        off = round(100 - p["price"] / p["old_price"] * 100)
        price = f"<s>{brl(p['old_price'])}</s>  {price}  <code>-{off}%</code>"
    cap = (f"{header(p['name'])}\n{p['description']}\n\n"
           f"💰 {price}\n📦 {stock_badge(st)}\n🛡 Garantia {p['warranty_days']} dias\n"
           f"⚡ Entrega automática  ·  {p['sold']} vendidos")
    rows = []
    if st != 0:
        if cap_qty > 1:
            rows.append([b("−", f"p:{pid}:{qty - 1}"), b(f"{qty} un.", "noop"), b("+", f"p:{pid}:{qty + 1}")])
        rows.append([b(f"⚡ Comprar  ·  {brl(p['price'] * qty)}", f"buy:{pid}:{qty}")])
    else:
        rows.append([b("🔔 Esgotado — volte em breve", "noop")])
    rows.append(back(f"c:{p['category_id']}:0"))
    await screen(cb, "catalog", cap, kb(*rows), photo=p["image"])


# ───────────────────────── checkout ─────────────────────────
async def checkout_view(target, state: FSMContext):
    d = await state.get_data()
    p = await sellable(d["pid"])
    u = await repo.one("SELECT * FROM users WHERE id=?", d["uid"])
    if not p or not 1 <= d["qty"] <= p["max_qty"]:
        await state.clear()
        return await screen(target, "checkout", header("Produto indisponível"), kb(back("cat")))
    sub = p["price"] * d["qty"]
    disc = round(sub * d.get("pct", 0) / 100, 2)
    total = round(sub - disc, 2)
    usdt = await bp.brl_to_usdt(total)
    await state.update_data(total=total, disc=disc, usdt=usdt)
    cap = (f"{header('Resumo do pedido')}\n"
           f"{p['name']}  ×{d['qty']}\n"
           f"Subtotal  {brl(sub)}\n" + (f"🎟 Cupom {d['coupon']}  −{brl(disc)}\n" if disc else "") +
           f"{LINE}\n<b>Total  {brl(total)}</b>\n<i>≈ {usd(usdt)}</i>\n\nEscolha a forma de pagamento:")
    rows = [[b(f"⚡ Pix  ·  {brl(total)}", "pay:pix")]] if total >= PIX_MIN and on("pix_on") else []
    if on("usdt_on"):
        rows.append([b(f"🟡 USDT  ·  {usd(usdt)}", "pay:usdt")])
    if u["balance"] >= total:
        rows.append([b(f"👛 Saldo  ·  {brl(u['balance'])} disponível", "pay:bal")])
    rows.append([b("🎟 Tenho cupom", "cupom"), b("✖ Cancelar", f"p:{p['id']}:{d['qty']}")])
    m = await screen(target, "checkout", cap, kb(*rows))
    await state.update_data(scr=m.message_id)
    return m


@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery, state: FSMContext):
    v = ints(cb.data, 2)
    p = await sellable(v[0]) if v else None
    if not p:
        return await cb.answer("Produto indisponível.", show_alert=True)
    st = await repo.stock(p["id"])
    if st == 0:
        return await cb.answer("Esgotou agora há pouco 😕", show_alert=True)
    cap = p["max_qty"] if st is None else min(p["max_qty"], st)
    qty = v[1]
    if not 1 <= qty <= max(cap, 1):  # quantidade forjada (negativa, zero ou acima do máximo)
        return await cb.answer("Quantidade inválida.", show_alert=True)
    await state.set_data({"kind": "buy", "pid": p["id"], "qty": qty, "uid": cb.from_user.id})
    await checkout_view(cb, state)


@router.callback_query(F.data == "cupom")
async def ask_coupon(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.coupon)
    await cb.answer("Digite o código do cupom no chat ✍️", show_alert=True)


async def drop_screen(m: Message, state: FSMContext):
    d = await state.get_data()
    if d.get("scr"):
        try:
            await bot.delete_message(m.chat.id, d["scr"])
        except Exception:
            pass


@router.message(S.coupon)
async def got_coupon(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    c = await repo.one("SELECT * FROM coupons WHERE code=? AND active=1 AND uses_left!=0 AND (expires_at IS NULL "
                       "OR expires_at='' OR expires_at>=date('now','-3 hours'))", code)
    await m.delete()
    await state.set_state(None)
    if not c:
        await repo.event(m.from_user.id, "erro", f"Cupom recusado: {code[:30]}")
        return await m.answer("❌ Cupom inválido ou esgotado.", reply_markup=kb([b("OK", "del")]))
    await state.update_data(coupon=code, pct=c["pct"])
    await drop_screen(m, state)
    await checkout_view(m, state)


@router.callback_query(F.data == "del")
async def delete_msg(cb: CallbackQuery):
    await cb.message.delete()


def new_code(): return f"VX-{datetime.utcnow():%y%m%d}-{secrets.token_hex(2).upper()}"


async def reserve_coupon(code):
    """Reserva 1 uso do cupom (atômico). Ilimitado (-1) sempre passa."""
    c = await repo.one("SELECT uses_left FROM coupons WHERE code=? AND active=1", code)
    if not c:
        return False
    if c["uses_left"] < 0:
        return True
    return bool(await repo.run("UPDATE coupons SET uses_left=uses_left-1 WHERE code=? AND active=1 AND uses_left>0", code))


async def release_coupon(o):
    """Pedido não pago (expirou, cancelou, falhou): devolve o uso reservado."""
    if o and o["coupon"]:
        await repo.x("UPDATE coupons SET uses_left=uses_left+1 WHERE code=? AND uses_left>=0", o["coupon"])


async def order_dead(oid, status):
    """Marca pedido não pago como expirado/cancelado/falho e devolve o cupom (só uma vez)."""
    if await repo.run(f"UPDATE orders SET status=? WHERE id=? AND status='pending'", status, oid):
        await release_coupon(await repo.one("SELECT * FROM orders WHERE id=?", oid))
        return True
    return False


async def create_order(d, method):
    if d.get("coupon") and not await reserve_coupon(d["coupon"]):
        return None, None  # cupom esgotou entre aplicar e pagar
    code = new_code()
    oid = await repo.x("INSERT INTO orders(code,user_id,kind,product_id,qty,amount,discount,coupon,method,usdt) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", code, d["uid"], d["kind"], d.get("pid"), d.get("qty", 1),
                       d["total"], d.get("disc", 0), d.get("coupon"), method, d.get("usdt"))
    await repo.event(d["uid"], "pedido", f"Pedido {code} criado · {await order_title(oid)} · {brl(d['total'])} · "
                                         f"{PAY.get(method, method)}" + (f" · cupom {d['coupon']}" if d.get("coupon") else ""), code)
    return oid, code


@router.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d:
        return await home(cb, state)
    method = cb.data[4:]
    if method == "bal":
        if d.get("kind") != "buy" or "total" not in d:
            return await home(cb, state)
        await state.clear()
        oid, code = await create_order(d, "saldo")
        if not oid:
            return await cb.answer("Esse cupom acabou de esgotar.", show_alert=True)
        if not await repo.debit(cb.from_user.id, d["total"], "compra"):
            await order_dead(oid, "canceled")
            await repo.event(cb.from_user.id, "erro", f"Saldo insuficiente para {brl(d['total'])}")
            return await cb.answer("Saldo insuficiente.", show_alert=True)
        await repo.x("UPDATE ledger SET ref=? WHERE id=(SELECT MAX(id) FROM ledger WHERE user_id=? AND reason='compra')",
                     code, cb.from_user.id)
        await repo.x("UPDATE orders SET status='paid', paid_at=CURRENT_TIMESTAMP, msg_id=? WHERE id=?",
                     cb.message.message_id, oid)
        await cb.answer("Pago com saldo ✅")
        return await deliver(oid)
    if method == "pix":
        if not on("pix_on"):
            return await cb.answer("Pix indisponível no momento.", show_alert=True)
        if d.get("total", 0) < PIX_MIN:
            return await cb.answer(f"Pix a partir de {brl(PIX_MIN)}.", show_alert=True)
        u = await repo.one("SELECT cpf FROM users WHERE id=?", cb.from_user.id)
        if not u["cpf"]:
            await state.set_state(S.cpf)
            await state.update_data(scr=cb.message.message_id)
            return await screen(cb, "pix", header("Pix", "Só na primeira compra") +
                                "\nPara gerar o Pix, envie seu <b>CPF</b> (somente números).\n"
                                "<i>Exigência do banco emissor. Usamos só para a cobrança.</i>",
                                kb(back("home", "✖ Cancelar")))
        return await pix_checkout(cb, state)
    if method == "usdt" and on("usdt_on"):
        return await usdt_checkout(cb, state)
    await cb.answer("Forma de pagamento indisponível.", show_alert=True)


def valid_cpf(c):
    if len(c) != 11 or c == c[0] * 11:
        return False
    for i in (9, 10):
        s = sum(int(c[j]) * ((i + 1) - j) for j in range(i))
        if (s * 10 % 11) % 10 != int(c[i]):
            return False
    return True


@router.message(S.cpf)
async def got_cpf(m: Message, state: FSMContext):
    cpf = re.sub(r"\D", "", m.text or "")
    await m.delete()
    if not valid_cpf(cpf):
        return await m.answer("CPF inválido. Envie só os 11 números.", reply_markup=kb([b("OK", "del")]))
    await repo.x("UPDATE users SET cpf=? WHERE id=?", cpf, m.from_user.id)
    await state.set_state(None)
    await drop_screen(m, state)
    await pix_checkout(m, state)


async def pix_checkout(target, state: FSMContext):
    d = await state.get_data()
    uid = d["uid"]
    u = await repo.one("SELECT * FROM users WHERE id=?", uid)
    oid, code = await create_order(d, "pix")
    if not oid:
        return await screen(target, "pix", header("Cupom esgotado") + "\nEsse cupom acabou de esgotar. Refaça o pedido.", kb(back()))
    title = await order_title(oid)
    try:
        cid = await asaas.ensure_customer(uid, u["name"], u["cpf"])
        px = await asaas.create_pix(cid, d["total"], f"{cfg('store_name')} {code} - {title}", code)
    except Exception as e:
        logging.exception("pix")
        await repo.event(uid, "erro", f"Pix não gerado: {e}"[:300], code)
        await order_dead(oid, "failed")
        return await screen(target, "pix", header("Pix indisponível") + f"\nTente USDT ou mais tarde.\n<code>{esc(e)}</code>",
                            kb(back()))
    await repo.x("UPDATE orders SET ext_id=? WHERE id=?", px["id"], oid)
    qr = BufferedInputFile(base64.b64decode(px["qr_b64"]), filename="pix.png")
    cap = (f"{header('Pague com Pix', code)}\n{title}\n<b>Total  {brl(d['total'])}</b>\n\n"
           f"<b>Copia e cola</b> (toque para copiar):\n<code>{px['payload']}</code>\n\n"
           f"⏳ Expira em {cfg('order_timeout_min')} min · confirmação automática")
    m = await screen(target, "pix", cap, kb([b("🔄 Já paguei", f"chk:{oid}")], [b("✖ Cancelar", f"cx:{oid}")]),
                     photo=qr)
    await repo.x("UPDATE orders SET msg_id=? WHERE id=?", m.message_id, oid)
    await state.clear()


async def usdt_checkout(cb, state):
    d = await state.get_data()
    oid, code = await create_order(d, "usdt")
    if not oid:
        return await screen(cb, "usdt", header("Cupom esgotado") + "\nEsse cupom acabou de esgotar. Refaça o pedido.", kb(back()))
    title = await order_title(oid)
    try:
        o = await bp.create_order(code.replace("-", ""), d["usdt"], title)
    except Exception as e:
        logging.exception("usdt")
        await repo.event(d["uid"], "erro", f"USDT não gerado: {e}"[:300], code)
        await order_dead(oid, "failed")
        return await screen(cb, "usdt", header("Binance Pay indisponível") + f"\nTente Pix.\n<code>{esc(e)}</code>",
                            kb(back()))
    await repo.x("UPDATE orders SET ext_id=? WHERE id=?", code.replace("-", ""), oid)
    cap = (f"{header('Pague em USDT', code)}\n{title}\n<b>Total  {usd(d['usdt'])}</b>\n"
           f"<i>≈ {brl(d['total'])}</i>\n\n1. Toque em <b>Abrir Binance Pay</b>\n2. Confirme no app\n"
           f"3. Pronto — entregamos aqui automaticamente\n\n⏳ Expira em {cfg('order_timeout_min')} min")
    m = await screen(cb, "usdt", cap, kb([b("🟡 Abrir Binance Pay", url=o["checkoutUrl"])],
                                         [b("🔄 Já paguei", f"chk:{oid}"), b("✖ Cancelar", f"cx:{oid}")]))
    await repo.x("UPDATE orders SET msg_id=? WHERE id=?", m.message_id, oid)
    await state.clear()


async def order_title(oid):
    o = await repo.one("SELECT o.*, p.name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE o.id=?", oid)
    return f"{o['name']} ×{o['qty']}" if o["kind"] == "buy" else "Recarga de saldo"


@router.callback_query(F.data.startswith("chk:"))
async def check(cb: CallbackQuery):
    o = await repo.one("SELECT * FROM orders WHERE id=? AND user_id=?", int(cb.data[4:]), cb.from_user.id)
    if not o:
        return await cb.answer()
    await process(o)
    st = (await repo.one("SELECT status FROM orders WHERE id=?", o["id"]))["status"]
    if st == "pending":
        await cb.answer("Ainda não identificamos o pagamento. Pode levar alguns segundos ⏳", show_alert=True)
    elif st == "expired":
        await cb.answer("Este pedido expirou.", show_alert=True)
    else:
        await cb.answer("Pagamento confirmado ✅")


@router.callback_query(F.data.startswith("cx:"))
async def cancel(cb: CallbackQuery, state: FSMContext):
    o = await repo.one("SELECT * FROM orders WHERE id=? AND user_id=? AND status='pending'", int(cb.data[3:]),
                       cb.from_user.id)
    if o:
        await process(o)  # pagou antes de cancelar? entrega em vez de cancelar
        if await order_dead(o["id"], "canceled"):
            if o["method"] == "pix" and o["ext_id"]:
                await asaas.cancel(o["ext_id"])
        else:
            return await cb.answer("Pagamento já confirmado ✅", show_alert=True)
    await cb.answer("Pedido cancelado")
    await home(cb, state)


# ───────────────────────── confirmação e entrega ─────────────────────────
async def process(o):
    if o["status"] != "pending" or not o["ext_id"]:
        return
    if datetime.utcnow() - datetime.fromisoformat(o["created_at"]) > timedelta(minutes=num("order_timeout_min") + 5):
        try:
            late = await (asaas.status(o["ext_id"]) if o["method"] == "pix" else bp.status(o["ext_id"]))
        except Exception:
            return
        if late != "PAID":
            if await order_dead(o["id"], "expired"):
                await repo.event(o["user_id"], "pagamento", "Cobrança expirou sem pagamento", o["code"])
                if o["method"] == "pix":
                    await asaas.cancel(o["ext_id"])
            return
    try:
        st = await (asaas.status(o["ext_id"]) if o["method"] == "pix" else bp.status(o["ext_id"]))
    except Exception:
        logging.exception("status")
        return
    if st == "PAID":
        if await repo.run("UPDATE orders SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", o["id"]):
            await repo.event(o["user_id"], "pagamento", f"Pagamento confirmado ({o['method']}) · {brl(o['amount'])}", o["code"])
            await deliver(o["id"])
    elif st == "EXPIRED":
        if await order_dead(o["id"], "expired"):
            await repo.event(o["user_id"], "pagamento", "Cobrança expirou sem pagamento", o["code"])


async def show_done(o, cap, markup, new=False):
    pic = await media("done")
    try:
        if o["msg_id"] and not new:
            m = await bot.edit_message_media(chat_id=o["user_id"], message_id=o["msg_id"],
                                             media=InputMediaPhoto(media=pic, caption=cap), reply_markup=markup)
            return await remember("done", m if isinstance(m, Message) else None)
    except TelegramBadRequest:
        pass
    await remember("done", await bot.send_photo(o["user_id"], pic, caption=cap, reply_markup=markup))


def done_kb(o):
    return kb([b("🛍 Continuar comprando", "cat")], [b("🛡 Garantia", f"tk:{o['code']}"), b("‹ Início", "home")])


async def notify_admins(text):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


async def olog(oid, line):
    """Linha do tempo do fornecedor no pedido (nunca contém segredos: forn.redact)."""
    await repo.x("UPDATE orders SET supplier_log=COALESCE(supplier_log,'')||? WHERE id=?",
                 f"[{datetime.utcnow():%d/%m %H:%M:%S}] {line}\n", oid)


async def refund_fail(o, p, reason):
    """Fornecedor falhou: estorno automático em saldo + avisos."""
    if not await repo.run("UPDATE orders SET status='refunded' WHERE id=? AND status IN('delivering','awaiting','manual')",
                          o["id"]):
        return
    await repo.credit(o["user_id"], o["amount"], "estorno", o["code"])
    await olog(o["id"], f"estornado em saldo: {reason}")
    await repo.event(o["user_id"], "erro", f"Entrega falhou ({reason}); {brl(o['amount'])} estornados em saldo", o["code"])
    await show_done(o, f"{header('Não foi possível entregar', o['code'])}\nO fornecedor não confirmou o seu pedido.\n"
                       f"<b>{brl(o['amount'])}</b> voltaram para a sua carteira. Use em qualquer compra.", done_kb(o))
    await notify_admins(f"⚠️ Fornecedor falhou · {o['code']} · {p['name']} ×{o['qty']}\n{esc(reason)}\n"
                        f"Estornado {brl(o['amount'])} em saldo.")


async def preparing(o, p):
    await show_done(o, f"✅ <b>{cfg('txt_manual')}</b>\nPedido <code>{o['code']}</code>\n\n"
                       f"<b>{p['name']}</b>  ·  {o['qty']} un.\n⏱ Prazo: {cfg('manual_eta')}\n"
                       f"Você recebe aqui assim que ficar pronto.", done_kb(o))


async def save_items(o, p, items):
    await repo.db.executemany("INSERT INTO stock(product_id,content,sold,order_id) VALUES(?,?,1,?)",
                              [(p["id"], i, o["id"]) for i in items])
    await repo.db.commit()


async def deliver(oid):
    o = await repo.one("SELECT * FROM orders WHERE id=?", oid)
    if o["status"] != "paid":
        return
    if not await repo.run("UPDATE orders SET status='delivering' WHERE id=? AND status='paid'", oid):
        return  # outra tarefa já está entregando
    if o["kind"] == "topup":
        if not await repo.one("SELECT 1 FROM ledger WHERE ref=? AND reason='recarga'", o["code"]):
            await repo.credit(o["user_id"], o["amount"], "recarga", o["code"])
        await repo.x("UPDATE orders SET status='delivered', delivered_at=CURRENT_TIMESTAMP WHERE id=?", oid)
        await repo.event(o["user_id"], "entrega", f"Recarga de {brl(o['amount'])} creditada", o["code"])
        u = await repo.one("SELECT balance FROM users WHERE id=?", o["user_id"])
        return await show_done(o, f"{header('Recarga confirmada', o['code'])}\n+{brl(o['amount'])} na carteira\n"
                                  f"👛 Saldo atual  <b>{brl(u['balance'])}</b>", done_kb(o))
    p = await repo.one("SELECT * FROM products WHERE id=?", o["product_id"])
    mode = p["delivery_mode"] or "estoque"
    sup = await repo.one("SELECT * FROM suppliers WHERE id=?", p["supplier_id"]) if p["supplier_id"] else None
    await repo.x("UPDATE orders SET supplier_id=?, cost=? WHERE id=?", p["supplier_id"], round((p["cost"] or 0) * o["qty"], 2), oid)
    if mode == "estoque":
        items = await repo.take_stock(p["id"], o["qty"], oid)
        if items is None:
            await repo.credit(o["user_id"], o["amount"], "estorno", o["code"])
            await repo.x("UPDATE orders SET status='refunded' WHERE id=?", oid)
            await repo.event(o["user_id"], "erro", f"Estoque esgotou no pagamento; {brl(o['amount'])} estornados em saldo", o["code"])
            return await show_done(o, f"{header('Estoque esgotado', o['code'])}\nO produto acabou durante o pagamento.\n"
                                      f"<b>{brl(o['amount'])}</b> foram creditados na sua carteira.", done_kb(o))
        return await finish(oid, items)
    if mode == "manual":
        await repo.x("UPDATE orders SET status='manual', sent_at=CURRENT_TIMESTAMP WHERE id=?", oid)
        await olog(oid, "na fila manual")
        await repo.event(o["user_id"], "entrega", "Pedido foi para a fila manual (aguardando preparo)", o["code"])
        await preparing(o, p)
        link = f"\n🔗 {p['ref_link']}" if p["ref_link"] else ""
        return await notify_admins(f"🧾 Fila manual · {o['code']} · {p['name']} ×{o['qty']} · {brl(o['amount'])}{link}")
    if not sup or not sup["active"]:
        return await refund_fail(o, p, "produto sem fornecedor ativo")
    log_lines = []
    await repo.x("UPDATE orders SET sent_at=CURRENT_TIMESTAMP WHERE id=?", oid)
    await olog(oid, f"enviado ao fornecedor {sup['name']} ({mode})")
    try:
        if mode == "api":
            items = await forn.call_api(sup, p["sku"] or "", o["qty"], o["code"], log_lines.append)
        else:
            await forn.call_webhook(sup, {"pedido": o["code"], "sku": p["sku"] or "", "qtd": o["qty"],
                                          "produto": p["name"], "valor": o["amount"]}, log_lines.append)
            items = None
    except Exception as e:
        for ln in log_lines:
            await olog(oid, ln)
        return await refund_fail(o, p, f"{sup['name']}: {e}")
    for ln in log_lines:
        await olog(oid, ln)
    if items is None:  # webhook: aguarda o retorno no painel
        await repo.x("UPDATE orders SET status='awaiting' WHERE id=? AND status='delivering'", oid)
        await repo.event(o["user_id"], "entrega", f"Pedido enviado ao fornecedor {sup['name']}, aguardando retorno", o["code"])
        return await preparing(o, p)
    await save_items(o, p, items)
    await finish(oid, items)


async def finish(oid, items=None):
    """Entrega final (estoque, API, ou pedido 'ready' vindo do painel/webhook)."""
    o = await repo.one("SELECT * FROM orders WHERE id=?", oid)
    p = await repo.one("SELECT * FROM products WHERE id=?", o["product_id"])
    if items is None:
        items = [r["content"] for r in await repo.q("SELECT content FROM stock WHERE order_id=? ORDER BY id", oid)]
    ms = None
    if o["paid_at"]:
        ms = int((datetime.utcnow() - datetime.fromisoformat(o["paid_at"])).total_seconds() * 1000)
    await repo.x("UPDATE orders SET status='delivered', delivered_at=CURRENT_TIMESTAMP, delivery_ms=? WHERE id=?", ms, oid)
    await olog(oid, "entregue ao cliente")
    await repo.event(o["user_id"], "entrega", f"Entregue: {p['name']} ×{o['qty']}"
                     + (f" em {ms / 1000:.1f}s após o pagamento" if ms is not None else ""), o["code"])
    await repo.x("UPDATE products SET sold=sold+? WHERE id=?", o["qty"], p["id"])
    await repo.x("UPDATE users SET spent=spent+? WHERE id=?", o["amount"], o["user_id"])
    u = await repo.one("SELECT ref_by FROM users WHERE id=?", o["user_id"])
    if u["ref_by"]:
        bonus = round(o["amount"] * num("ref_pct") / 100, 2)
        if bonus > 0:
            await repo.credit(u["ref_by"], bonus, "comissao", o["code"])
            await repo.x("UPDATE users SET ref_earned=ref_earned+? WHERE id=?", bonus, u["ref_by"])
            try:
                await bot.send_message(u["ref_by"], f"<b>Comissão recebida</b>\n+{brl(bonus)} de saldo por uma compra do seu indicado.",
                                       reply_markup=kb([b("Ver minha conta", "me")]))
            except Exception:
                pass
    await send_delivery(o, p, items)
    if CHANNEL_ID:
        try:
            await bot.send_message(CHANNEL_ID, f"🟢 Acabou de sair: <b>{p['name']}</b>")
        except Exception:
            pass
    await notify_admins(f"💸 Venda {o['code']} · {p['name']} ×{o['qty']} · {brl(o['amount'])} · {o['method']}")


async def send_delivery(o, p, items, new=False):
    lines = [f"<code>{esc(i)}</code>" for i in items]
    body = "\n".join(lines)
    until = (datetime.now() + timedelta(days=p["warranty_days"] or 0)).strftime("%d/%m/%Y")
    head = f"✅ <b>Pagamento aprovado</b>\nPedido <code>{o['code']}</code>\n\n<b>{esc(p['name'])}</b>  ·  {o['qty']} un.\n{LINE}\n"
    foot = f"{LINE}\n🛡 Garantia até <b>{until}</b>\n📦 Reabra quando quiser em <b>Meus pedidos</b>"
    cap = f"{head}🔑 <b>Seu acesso</b>  <i>(toque para copiar)</i>\n{body}\n{foot}"
    big = len(cap) > 1024
    try:
        await show_done(o, f"{head}🔑 <b>Seu acesso</b> segue nas mensagens abaixo 👇\n{foot}" if big else cap, done_kb(o), new=new)
        if big:
            for chunk in chunks(lines):
                await bot.send_message(o["user_id"], chunk)
    except Exception as e:
        logging.exception("entrega")
        await repo.event(o["user_id"], "erro", f"Falha ao enviar a entrega no Telegram: {type(e).__name__}", o["code"])
        await notify_admins(f"⚠️ Não consegui enviar a entrega do pedido {o['code']} ao cliente "
                            f"(<code>{o['user_id']}</code>). Reenvie pelo painel.")


def chunks(lines, limit=3900):
    """Agrupa linhas inteiras em mensagens de até ~4096 caracteres (nunca corta uma tag)."""
    out, cur = [], ""
    for ln in lines:
        if cur and len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{ln}" if cur else ln
    if cur:
        out.append(cur)
    return out


async def tick():
    """Um ciclo do poller (também usado pelos testes)."""
    await store.load(repo)
    for o in await repo.q("SELECT * FROM orders WHERE status='pending' AND ext_id IS NOT NULL"):
        await process(o)
    for o in await repo.q("SELECT id FROM orders WHERE status='paid'"):
        await deliver(o["id"])
    # pedidos prontos pelo painel (fila manual / retorno de webhook)
    for o in await repo.q("SELECT id FROM orders WHERE status='ready'"):
        if await repo.run("UPDATE orders SET status='delivering' WHERE id=? AND status='ready'", o["id"]):
            await finish(o["id"])
    # reenviar entrega pedida no painel
    for o in await repo.q("SELECT * FROM orders WHERE resend=1"):
        await repo.x("UPDATE orders SET resend=0 WHERE id=?", o["id"])
        await repo.event(o["user_id"], "suporte", "Entrega reenviada pelo suporte", o["code"])
        p = await repo.one("SELECT * FROM products WHERE id=?", o["product_id"])
        items = [r["content"] for r in await repo.q("SELECT content FROM stock WHERE order_id=? ORDER BY id", o["id"])]
        items = items or ([p["unlimited"]] * o["qty"] if p and p["unlimited"] else [])
        if p and items:
            await send_delivery(o, p, items, new=True)
    # webhook sem resposta no prazo → estorno
    for o in await repo.q("SELECT * FROM orders WHERE status='awaiting' AND sent_at<=datetime('now', ?)",
                          f"-{int(num('webhook_timeout_min'))} minutes"):
        p = await repo.one("SELECT * FROM products WHERE id=?", o["product_id"])
        await refund_fail(o, p, "webhook sem resposta no prazo")


async def poller():
    while True:
        try:
            await tick()
        except Exception:
            logging.exception("poller")
        await asyncio.sleep(POLL_SECONDS)


# ───────────────────────── conta, pedidos, indicação ─────────────────────────
@router.callback_query(F.data == "me")
async def me(cb: CallbackQuery):
    u = await user(cb.from_user)
    n = (await repo.one("SELECT COUNT(*) n FROM orders WHERE user_id=? AND status='delivered' AND kind='buy'", u["id"]))["n"]
    cap = (f"{header('Minha conta')}\nSaldo  <b>{brl(u['balance'])}</b>\n{LINE}\n"
           f"Compras  {n}\nTotal gasto  {brl(u['spent'])}\nComissões  {brl(u['ref_earned'])}\nID  <code>{u['id']}</code>")
    await screen(cb, "profile", cap, kb([b("Adicionar saldo", "topup"), b("Meus pedidos", "orders")], back()))


@router.callback_query(F.data == "topup")
async def topup(cb: CallbackQuery):
    vals = TOPUP_VALUES
    await screen(cb, "profile", header("Recarregar saldo", "Use depois em qualquer compra, na hora"),
                 kb([b(brl(v), f"tp:{v}") for v in vals[:2]], [b(brl(v), f"tp:{v}") for v in vals[2:]], back("me")))


@router.callback_query(F.data.startswith("tp:"))
async def topup_go(cb: CallbackQuery, state: FSMContext):
    v = ints(cb.data, 1)
    if not v or v[0] not in TOPUP_VALUES:  # valor forjado
        return await cb.answer("Valor inválido.", show_alert=True)
    v = float(v[0])
    await state.set_data({"kind": "topup", "uid": cb.from_user.id, "total": v, "usdt": await bp.brl_to_usdt(v)})
    d = await state.get_data()
    await screen(cb, "checkout", f"{header('Recarga')}\n<b>{brl(v)}</b>  ≈ {usd(d['usdt'])}\n\nComo quer pagar?",
                 kb(([b("⚡ Pix", "pay:pix")] if on("pix_on") else []) + ([b("🟡 USDT", "pay:usdt")] if on("usdt_on") else []),
                    back("topup")))


@router.callback_query(F.data == "orders")
async def orders(cb: CallbackQuery):
    rows = await repo.q("SELECT o.*, p.name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE user_id=? "
                        "AND status IN('delivered','refunded','manual','awaiting') ORDER BY o.id DESC LIMIT 8", cb.from_user.id)
    btns = [[b(f"{'' if r['kind'] == 'buy' else 'Recarga · '}{r['code']} · {r['name'] or 'Saldo'} · {brl(r['amount'])}",
               f"od:{r['code']}")] for r in rows]
    await screen(cb, "profile", header("Meus pedidos", "Toque para reabrir a entrega") +
                 ("" if rows else "\nVocê ainda não comprou nada. Bora? 🛍"), kb(*btns, back()))


async def order_items(code, uid):
    return await repo.q("SELECT s.content FROM stock s JOIN orders o ON o.id=s.order_id WHERE o.code=? AND o.user_id=?",
                        code, uid)


@router.callback_query(F.data.startswith("od:"))
async def order_detail(cb: CallbackQuery):
    code = cb.data[3:]
    o = await repo.one("SELECT o.*, p.name, p.unlimited FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                       "WHERE code=? AND user_id=?", code, cb.from_user.id)
    if not o:
        return await cb.answer("Pedido não encontrado.", show_alert=True)
    items = [i["content"] for i in await order_items(code, cb.from_user.id)] or ([o["unlimited"]] if o["unlimited"] else [])
    lines = [f"<code>{esc(i)}</code>" for i in items]
    head = f"{header(esc(o['name'] or 'Recarga'), code)}\n{brl(o['amount'])} · {o['method']} · {o['created_at'][:16]}\n\n"
    cap = head + "\n".join(lines)
    big = len(cap) > 1024
    await screen(cb, "done", head + "Seu acesso segue abaixo 👇" if big else cap,
                 kb([b("🛡 Acionar garantia", f"tk:{code}")], back("orders")))
    if big:
        for chunk in chunks(lines):
            await bot.send_message(cb.from_user.id, chunk)


@router.message(Command("pedido"))
async def resend(m: Message, command: CommandObject):
    items = await order_items((command.args or "").strip().upper(), m.from_user.id)
    if not items:
        return await m.answer("Pedido não encontrado.")
    for chunk in chunks([f"<code>{esc(i['content'])}</code>" for i in items]):
        await m.answer(chunk)


@router.callback_query(F.data == "ref")
async def ref(cb: CallbackQuery):
    me_ = await bot.me()
    n = (await repo.one("SELECT COUNT(*) n FROM users WHERE ref_by=?", cb.from_user.id))["n"]
    link = f"https://t.me/{me_.username}?start=r{cb.from_user.id}"
    tot = await repo.one("SELECT COUNT(DISTINCT o.user_id) b, COALESCE(SUM(o.amount),0) v FROM orders o JOIN users x ON x.id=o.user_id "
                         "WHERE x.ref_by=? AND o.status='delivered' AND o.kind='buy'", cb.from_user.id)
    u = await repo.one("SELECT ref_earned FROM users WHERE id=?", cb.from_user.id)
    cap = (f"{header('Indique e ganhe')}\nGanhe <b>{num('ref_pct'):g}%</b> de comissão em saldo sobre toda compra de quem "
           f"entrar pelo seu link. Sem limite, para sempre.\n{LINE}\n"
           f"Indicados  <b>{n}</b>  ·  compradores <b>{tot['b']}</b>\nVendido pelos indicados  {brl(tot['v'])}\n"
           f"Comissão recebida  <b>{brl(u['ref_earned'])}</b>\n{LINE}\nSeu link\n<code>{link}</code>")
    await screen(cb, "profile", cap, kb([b("Compartilhar link", url=f"https://t.me/share/url?url={link}&text=VEXA%20%E2%80%94%20comprou%2C%20chegou.")], back()))


@router.callback_query(F.data == "sup")
async def support(cb: CallbackQuery):
    rows = [[b("🛡 Acionar garantia de um pedido", "orders")]]
    if cfg("support_username"):
        rows.append([b("💬 Falar com atendente", url=f"https://t.me/{cfg('support_username').lstrip('@')}")])
    await screen(cb, "support", f"{header('Suporte')}\n{cfg('txt_support')}", kb(*rows, back()))


@router.callback_query(F.data.startswith("tk:"))
async def ticket_start(cb: CallbackQuery, state: FSMContext):
    if not await repo.one("SELECT 1 FROM orders WHERE code=? AND user_id=?", cb.data[3:], cb.from_user.id):
        return await cb.answer("Pedido não encontrado.", show_alert=True)
    await state.set_state(S.ticket)
    await state.update_data(code=cb.data[3:])
    await cb.answer("Descreva o problema numa mensagem ✍️", show_alert=True)


@router.message(S.ticket)
async def ticket_text(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    text = m.text or m.caption or "(mídia)"
    await repo.event(m.from_user.id, "chamado", f"Abriu chamado do pedido {d['code']}", d["code"])
    tid = await repo.x("INSERT INTO tickets(user_id,order_code,text,status,updated_at) VALUES(?,?,?,'aberto',CURRENT_TIMESTAMP)",
                       m.from_user.id, d["code"], text)
    await repo.x("INSERT INTO ticket_messages(ticket_id,sender,text) VALUES(?,?,?)", tid, "cliente", text)
    await m.answer(f"✅ Chamado <b>#{tid}</b> aberto para o pedido {esc(d['code'])}. Respondemos por aqui.",
                   reply_markup=kb([b("‹ Início", "home")]))
    await notify_admins(f"🎫 Chamado novo #{tid} · {esc(d['code'])} · {esc(m.from_user.full_name)} (<code>{m.from_user.id}</code>)\n"
                        f"{esc(text)}\n\nResponda pelo painel.")


@router.callback_query(F.data.startswith("tr:"))
async def ticket_reply_start(cb: CallbackQuery, state: FSMContext):
    t = await repo.one("SELECT id FROM tickets WHERE id=? AND user_id=?", int(cb.data[3:]), cb.from_user.id)
    if not t:
        return await cb.answer()
    await state.set_state(S.ticket_reply)
    await state.update_data(tid=t["id"])
    await cb.answer("Escreva sua resposta numa mensagem ✍️", show_alert=True)


@router.message(S.ticket_reply)
async def ticket_reply_text(m: Message, state: FSMContext):
    tid = (await state.get_data())["tid"]
    await state.clear()
    text = m.text or m.caption or "(mídia)"
    await repo.x("INSERT INTO ticket_messages(ticket_id,sender,text) VALUES(?,?,?)", tid, "cliente", text)
    await repo.x("UPDATE tickets SET status='aberto', updated_at=CURRENT_TIMESTAMP WHERE id=?", tid)
    await m.answer(f"✅ Resposta adicionada ao chamado <b>#{tid}</b>.", reply_markup=kb([b("‹ Início", "home")]))
    await notify_admins(f"🎫 Resposta no chamado #{tid} · {esc(m.from_user.full_name)}\n{esc(text)}")


# ───────────────────────── admin ─────────────────────────
ADMIN_HELP = f"""<b>Painel admin</b>
{LINE}
<b>Catálogo</b>
/cat Nome|emoji — nova categoria
/add cat_id|Nome|preço|garantia|descrição|preço_antigo
/infinito id|conteúdo — entrega ilimitada (link/arquivo)
/estoque id + linhas abaixo (1 unidade por linha)
/img id — responda a uma foto para usar no produto
/preco id|valor · /off id · /on id · /lista

<b>Vendas</b>
/vendas — resumo
/cupom CODIGO|%|usos (-1 = ilimitado)
/credito user_id|valor · /responder user_id texto
/aviso texto — mensagem para todos os clientes

<b>Painel web</b>
/painel — link de acesso ao painel (uso único)"""


def admin_msg(m: Message): return is_admin(m.from_user.id)


def args(m, n):
    p = [x.strip() for x in (m.text.split(maxsplit=1) + [""])[1].split("|")]
    if len(p) < n or not p[0]:
        raise ValueError
    return p


@router.message(Command("admin"), admin_msg)
async def a_help(m: Message): await m.answer(ADMIN_HELP)


@router.message(Command("painel"), admin_msg)
async def a_panel(m: Message):
    """Abre a porta do painel neste aparelho: link de uso único, 10 min (só em botão, sem prévia que o consuma)."""
    token = await repo.panel_link()
    await m.answer("🔐 <b>Painel VEXA</b>\nToque no botão para abrir o painel neste aparelho.\n"
                   "<i>Link de uso único, vale 10 minutos. Depois entre com usuário e senha.</i>",
                   reply_markup=kb([b("Abrir painel", url=f"https://{PANEL_DOMAIN}/entrar/{token}")]))


@router.message(Command("cat"), admin_msg)
async def a_cat(m: Message):
    p = args(m, 1)
    cid = await repo.x("INSERT INTO categories(name,emoji) VALUES(?,?)", p[0], p[1] if len(p) > 1 and p[1] else "•")
    await m.answer(f"Categoria #{cid} criada.")


@router.message(Command("add"), admin_msg)
async def a_add(m: Message):
    p = args(m, 4) + ["", ""]
    pid = await repo.x("INSERT INTO products(category_id,name,price,warranty_days,description,old_price) VALUES(?,?,?,?,?,?)",
                       int(p[0]), p[1], float(p[2].replace(",", ".")), int(p[3]), p[4],
                       float(p[5].replace(",", ".")) if p[5] else None)
    await m.answer(f"Produto #{pid} criado. Estoque: /estoque {pid} · Foto: /img {pid}")


@router.message(Command("infinito"), admin_msg)
async def a_inf(m: Message):
    pid, content = args(m, 2)[:2]
    await repo.x("UPDATE products SET unlimited=? WHERE id=?", content, int(pid))
    await m.answer("OK — entrega ilimitada.")


@router.message(Command("estoque"), admin_msg)
async def a_stock(m: Message):
    lines = m.text.split("\n")
    pid = int(lines[0].split()[1])
    items = [x.strip() for x in lines[1:] if x.strip()]
    await repo.db.executemany("INSERT INTO stock(product_id,content) VALUES(?,?)", [(pid, i) for i in items])
    await repo.db.commit()
    await m.answer(f"+{len(items)} un. · estoque {await repo.stock(pid)}")


@router.message(Command("img"), admin_msg)
async def a_img(m: Message, command: CommandObject):
    src = m.reply_to_message
    if not (src and src.photo and command.args):
        return await m.answer("Responda a uma foto com /img ID")
    await repo.x("UPDATE products SET image=? WHERE id=?", src.photo[-1].file_id, int(command.args))
    await m.answer("Foto aplicada ✅")


@router.message(Command("off", "on"), admin_msg)
async def a_toggle(m: Message, command: CommandObject):
    await repo.x("UPDATE products SET active=? WHERE id=?", int(m.text.startswith("/on")), int(command.args))
    await m.answer("OK")


@router.message(Command("preco"), admin_msg)
async def a_price(m: Message):
    pid, v = args(m, 2)[:2]
    await repo.x("UPDATE products SET price=? WHERE id=?", float(v.replace(",", ".")), int(pid))
    await m.answer("OK")


@router.message(Command("lista"), admin_msg)
async def a_list(m: Message):
    out = [f"#{p['id']} {'🟢' if p['active'] else '⚪'} {p['name']} · {brl(p['price'])} · {stock_badge(await repo.stock(p['id']))}"
           for p in await repo.q("SELECT * FROM products")]
    await m.answer("\n".join(out) or "vazio")


@router.message(Command("cupom"), admin_msg)
async def a_coupon(m: Message):
    p = args(m, 2) + ["-1"]
    if not 0 < float(p[1]) < 100:
        raise ValueError
    await repo.x("INSERT OR REPLACE INTO coupons(code,pct,uses_left) VALUES(?,?,?)", p[0].upper(), float(p[1]), int(p[2]))
    await m.answer(f"🎟 Cupom {p[0].upper()} · {p[1]}% criado.")


@router.message(Command("vendas"), admin_msg)
async def a_sales(m: Message):
    q = "SELECT COUNT(*) n, COALESCE(SUM(amount),0) t FROM orders WHERE status='delivered' AND kind='buy'"
    d, w, t = [await repo.one(q + extra) for extra in
               (" AND date(paid_at)=date('now')", " AND paid_at>=datetime('now','-7 days')", "")]
    u = (await repo.one("SELECT COUNT(*) n FROM users"))["n"]
    await m.answer(f"<b>Vendas</b>\n{LINE}\nHoje  {d['n']} · {brl(d['t'])}\n7 dias  {w['n']} · {brl(w['t'])}\n"
                   f"Total  {t['n']} · {brl(t['t'])}\nClientes  {u}")


@router.message(Command("credito"), admin_msg)
async def a_credit(m: Message):
    uid, v = args(m, 2)[:2]
    await repo.credit(int(uid), float(v.replace(",", ".")), "ajuste: /credito no Telegram")
    await m.answer("OK")


@router.message(Command("responder"), admin_msg)
async def a_reply(m: Message):
    _, uid, txt = m.text.split(maxsplit=2)
    t = await repo.one("SELECT id FROM tickets WHERE user_id=? AND status!='resolvido' ORDER BY id DESC LIMIT 1", int(uid))
    await bot.send_message(int(uid), f"💬 <b>Suporte VEXA</b>\n{txt}",
                           reply_markup=kb([b("Responder", f"tr:{t['id']}")]) if t else None)
    if t:
        await repo.x("INSERT INTO ticket_messages(ticket_id,sender,staff,text) VALUES(?,?,?,?)", t["id"], "suporte", "telegram", txt)
        await repo.x("UPDATE tickets SET status='respondido', updated_at=CURRENT_TIMESTAMP WHERE id=?", t["id"])
    await m.answer("Enviado.")


@router.message(Command("aviso"), admin_msg)
async def a_broadcast(m: Message, command: CommandObject):
    if not command.args:
        raise ValueError
    n = 0
    for u in await repo.q("SELECT id FROM users"):
        try:
            await bot.send_message(u["id"], command.args)
            n += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"Enviado para {n}.")


@router.errors()
async def on_error(ev):
    msg = getattr(ev.update, "message", None)
    if msg and msg.text and msg.text.startswith("/") and isinstance(ev.exception, (ValueError, IndexError, TypeError)):
        await msg.answer("Formato inválido. Veja /admin.")
        return True
    logging.error("erro no update", exc_info=ev.exception)
    cb = getattr(ev.update, "callback_query", None)
    src = cb or msg
    await repo.event(src.from_user.id if src and src.from_user else None, "erro",
                     f"Erro interno ({type(ev.exception).__name__}: {ev.exception})"[:300])
    if cb:
        try:
            await cb.answer("Algo deu errado. Tente de novo.", show_alert=True)
        except Exception:
            pass
    return True


async def main():
    global repo, bot
    repo = Repo(await connect())
    await store.load(repo)
    # pedidos interrompidos por queda no meio da entrega voltam para a fila (entrega é idempotente)
    await repo.x("UPDATE orders SET status='paid' WHERE status='delivering'")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    if SANDBOX:
        logging.warning("SANDBOX=1: pagamentos simulados")
        for a in ADMIN_IDS:
            try:
                await bot.send_message(a, "⚠️ <b>Bot em modo SANDBOX</b>: pagamentos são simulados e qualquer pedido é "
                                          "aprovado sem pagar. Para vender de verdade, SANDBOX=0 no ENV_FILE.")
            except Exception:
                pass
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(poller())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
