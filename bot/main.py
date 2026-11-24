import asyncio
import base64
import logging
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InputMediaPhoto, Message)

from . import asaas, binance_pay as bp
from .config import (ADMIN_IDS, BOT_TOKEN, CHANNEL_ID, ORDER_TIMEOUT_MIN, POLL_SECONDS, REF_BONUS_PCT,
                     STORE_NAME, SUPPORT_USERNAME)
from .db import Repo, connect
from .ui import LINE, b, back, bar, brl, header, home_kb, kb, level, stock_badge, usd

logging.basicConfig(level=logging.INFO)
router = Router()
repo: Repo
bot: Bot
BANNERS = Path(__file__).resolve().parent.parent / "assets" / "banners"
PAGE = 6


class S(StatesGroup):
    cpf = State()
    coupon = State()
    ticket = State()


def is_admin(uid): return uid in ADMIN_IDS


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
    (_, lvl, ico), _ = level(u["spent"])
    first = (u["name"] or "").split(" ")[0] or "bem-vindo"
    return (f"<b>Olá, {first}.</b> 👋\n"
            f"Produtos digitais com entrega automática, 24 horas por dia.\n{LINE}\n"
            f"👛 Saldo  <b>{brl(u['balance'])}</b>\n"
            f"{ico} Nível  <b>{lvl}</b>\n{LINE}\n"
            f"⚡ Pix  ·  🟡 USDT  ·  👛 Saldo\n<i>comprou, chegou.</i>")


@router.message(CommandStart())
async def start(m: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    new = not await repo.one("SELECT 1 FROM users WHERE id=?", m.from_user.id)
    u = await user(m.from_user)
    if new and command.args and command.args.startswith("r") and command.args[1:].isdigit():
        ref = int(command.args[1:])
        if ref != m.from_user.id:
            await repo.x("UPDATE users SET ref_by=? WHERE id=?", ref, m.from_user.id)
    await screen(m, "welcome", await home_caption(u), home_kb(is_admin(m.from_user.id)))


@router.callback_query(F.data == "home")
async def home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await user(cb.from_user)
    await screen(cb, "welcome", await home_caption(u), home_kb(is_admin(cb.from_user.id)))


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
        mark = "⛔" if st == 0 else "🔥" if st is not None and st <= 5 else "◆"
        rows.append([b(f"{mark}  {p['name']}  —  {brl(p['price'])}", f"p:{p['id']}:1")])
    return rows


@router.callback_query(F.data.startswith("c:"))
async def category(cb: CallbackQuery):
    _, cid, pg = cb.data.split(":")
    cid, pg = int(cid), int(pg)
    c = await repo.one("SELECT * FROM categories WHERE id=?", cid)
    total = (await repo.one("SELECT COUNT(*) n FROM products WHERE category_id=? AND active=1", cid))["n"]
    prods = await repo.q("SELECT * FROM products WHERE category_id=? AND active=1 ORDER BY sold DESC, name "
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


@router.callback_query(F.data == "top")
async def top(cb: CallbackQuery):
    prods = await repo.q("SELECT * FROM products WHERE active=1 ORDER BY sold DESC LIMIT 8")
    await screen(cb, "catalog", header("🔥 Mais vendidos", "O que a galera mais leva"),
                 kb(*(await product_rows(prods)), back()))


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery): await cb.answer()


@router.callback_query(F.data.startswith("p:"))
async def product(cb: CallbackQuery):
    _, pid, qty = cb.data.split(":")
    p = await repo.one("SELECT * FROM products WHERE id=?", int(pid))
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
    p = await repo.one("SELECT * FROM products WHERE id=?", d["pid"])
    u = await repo.one("SELECT * FROM users WHERE id=?", d["uid"])
    sub = p["price"] * d["qty"]
    disc = round(sub * d.get("pct", 0) / 100, 2)
    total = round(sub - disc, 2)
    usdt = await bp.brl_to_usdt(total)
    await state.update_data(total=total, disc=disc, usdt=usdt)
    cap = (f"{header('Resumo do pedido')}\n"
           f"◆ {p['name']}  ×{d['qty']}\n"
           f"Subtotal  {brl(sub)}\n" + (f"🎟 Cupom {d['coupon']}  −{brl(disc)}\n" if disc else "") +
           f"{LINE}\n<b>Total  {brl(total)}</b>\n<i>≈ {usd(usdt)}</i>\n\nEscolha a forma de pagamento:")
    rows = [[b(f"⚡ Pix  ·  {brl(total)}", "pay:pix")], [b(f"🟡 USDT  ·  {usd(usdt)}", "pay:usdt")]]
    if u["balance"] >= total:
        rows.append([b(f"👛 Saldo  ·  {brl(u['balance'])} disponível", "pay:bal")])
    rows.append([b("🎟 Tenho cupom", "cupom"), b("✖ Cancelar", f"p:{p['id']}:{d['qty']}")])
    m = await screen(target, "checkout", cap, kb(*rows))
    await state.update_data(scr=m.message_id)
    return m


@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery, state: FSMContext):
    _, pid, qty = cb.data.split(":")
    if await repo.stock(int(pid)) == 0:
        return await cb.answer("Esgotou agora há pouco 😕", show_alert=True)
    await state.set_data({"kind": "buy", "pid": int(pid), "qty": int(qty), "uid": cb.from_user.id})
    await checkout_view(cb, state)


@router.callback_query(F.data == "cupom")
async def ask_coupon(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.coupon)
    await cb.answer("Digite o código do cupom no chat ✍️", show_alert=True)


@router.message(S.coupon)
async def drop_screen(m: Message, state: FSMContext):
    d = await state.get_data()
    if d.get("scr"):
        try:
            await bot.delete_message(m.chat.id, d["scr"])
        except Exception:
            pass


async def got_coupon(m: Message, state: FSMContext):
    code = (m.text or "").strip().upper()
    c = await repo.one("SELECT * FROM coupons WHERE code=? AND active=1 AND uses_left!=0", code)
    await m.delete()
    await state.set_state(None)
    if not c:
        return await m.answer("❌ Cupom inválido ou esgotado.", reply_markup=kb([b("OK", "del")]))
    await state.update_data(coupon=code, pct=c["pct"])
    await drop_screen(m, state)
    await checkout_view(m, state)


@router.callback_query(F.data == "del")
async def delete_msg(cb: CallbackQuery):
    await cb.message.delete()


def new_code(): return f"VX-{datetime.utcnow():%y%m%d}-{secrets.token_hex(2).upper()}"


async def create_order(d, method):
    code = new_code()
    oid = await repo.x("INSERT INTO orders(code,user_id,kind,product_id,qty,amount,discount,coupon,method,usdt) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)", code, d["uid"], d["kind"], d.get("pid"), d.get("qty", 1),
                       d["total"], d.get("disc", 0), d.get("coupon"), method, d.get("usdt"))
    return oid, code


@router.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d:
        return await home(cb, state)
    method = cb.data[4:]
    if method == "bal":
        u = await repo.one("SELECT balance FROM users WHERE id=?", cb.from_user.id)
        if u["balance"] < d["total"]:
            return await cb.answer("Saldo insuficiente.", show_alert=True)
        oid, _ = await create_order(d, "saldo")
        await repo.x("UPDATE users SET balance=balance-? WHERE id=?", d["total"], cb.from_user.id)
        await repo.x("UPDATE orders SET status='paid', paid_at=CURRENT_TIMESTAMP, msg_id=? WHERE id=?",
                     cb.message.message_id, oid)
        await state.clear()
        await cb.answer("Pago com saldo ✅")
        return await deliver(oid)
    if method == "pix":
        u = await repo.one("SELECT cpf FROM users WHERE id=?", cb.from_user.id)
        if not u["cpf"]:
            await state.set_state(S.cpf)
            await state.update_data(scr=cb.message.message_id)
            return await screen(cb, "pix", header("Pix", "Só na primeira compra") +
                                "\nPara gerar o Pix, envie seu <b>CPF</b> (somente números).\n"
                                "<i>Exigência do banco emissor. Usamos só para a cobrança.</i>",
                                kb(back("home", "✖ Cancelar")))
        return await pix_checkout(cb, state)
    if method == "usdt":
        return await usdt_checkout(cb, state)


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
    title = await order_title(oid)
    try:
        cid = await asaas.ensure_customer(uid, u["name"], u["cpf"])
        px = await asaas.create_pix(cid, d["total"], f"{STORE_NAME} {code} - {title}", code)
    except Exception as e:
        logging.exception("pix")
        await repo.x("UPDATE orders SET status='failed' WHERE id=?", oid)
        return await screen(target, "pix", header("Pix indisponível") + f"\nTente USDT ou mais tarde.\n<code>{e}</code>",
                            kb(back()))
    await repo.x("UPDATE orders SET ext_id=? WHERE id=?", px["id"], oid)
    qr = BufferedInputFile(base64.b64decode(px["qr_b64"]), filename="pix.png")
    cap = (f"{header('Pague com Pix', code)}\n◆ {title}\n<b>Total  {brl(d['total'])}</b>\n\n"
           f"<b>Copia e cola</b> (toque para copiar):\n<code>{px['payload']}</code>\n\n"
           f"⏳ Expira em {ORDER_TIMEOUT_MIN} min · confirmação automática")
    m = await screen(target, "pix", cap, kb([b("🔄 Já paguei", f"chk:{oid}")], [b("✖ Cancelar", f"cx:{oid}")]),
                     photo=qr)
    await repo.x("UPDATE orders SET msg_id=? WHERE id=?", m.message_id, oid)
    await state.clear()


async def usdt_checkout(cb, state):
    d = await state.get_data()
    oid, code = await create_order(d, "usdt")
    title = await order_title(oid)
    try:
        o = await bp.create_order(code.replace("-", ""), d["usdt"], title)
    except Exception as e:
        logging.exception("usdt")
        await repo.x("UPDATE orders SET status='failed' WHERE id=?", oid)
        return await screen(cb, "usdt", header("Binance Pay indisponível") + f"\nTente Pix.\n<code>{e}</code>",
                            kb(back()))
    await repo.x("UPDATE orders SET ext_id=? WHERE id=?", code.replace("-", ""), oid)
    cap = (f"{header('Pague em USDT', code)}\n◆ {title}\n<b>Total  {usd(d['usdt'])}</b>\n"
           f"<i>≈ {brl(d['total'])}</i>\n\n1. Toque em <b>Abrir Binance Pay</b>\n2. Confirme no app\n"
           f"3. Pronto — entregamos aqui automaticamente\n\n⏳ Expira em {ORDER_TIMEOUT_MIN} min")
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
        await repo.x("UPDATE orders SET status='canceled' WHERE id=?", o["id"])
        if o["method"] == "pix" and o["ext_id"]:
            await asaas.cancel(o["ext_id"])
    await cb.answer("Pedido cancelado")
    await home(cb, state)


# ───────────────────────── confirmação e entrega ─────────────────────────
async def process(o):
    if o["status"] != "pending" or not o["ext_id"]:
        return
    if datetime.utcnow() - datetime.fromisoformat(o["created_at"]) > timedelta(minutes=ORDER_TIMEOUT_MIN + 5):
        await repo.x("UPDATE orders SET status='expired' WHERE id=?", o["id"])
        return
    try:
        st = await (asaas.status(o["ext_id"]) if o["method"] == "pix" else bp.status(o["ext_id"]))
    except Exception:
        logging.exception("status")
        return
    if st == "PAID":
        await repo.x("UPDATE orders SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", o["id"])
        await deliver(o["id"])
    elif st == "EXPIRED":
        await repo.x("UPDATE orders SET status='expired' WHERE id=?", o["id"])


async def show_done(o, cap, markup):
    pic = await media("done")
    try:
        if o["msg_id"]:
            m = await bot.edit_message_media(chat_id=o["user_id"], message_id=o["msg_id"],
                                             media=InputMediaPhoto(media=pic, caption=cap), reply_markup=markup)
            return await remember("done", m if isinstance(m, Message) else None)
    except TelegramBadRequest:
        pass
    await remember("done", await bot.send_photo(o["user_id"], pic, caption=cap, reply_markup=markup))


async def deliver(oid):
    o = await repo.one("SELECT * FROM orders WHERE id=?", oid)
    if o["status"] != "paid":
        return
    await repo.x("UPDATE orders SET status='delivering' WHERE id=? AND status='paid'", oid)
    done_kb = kb([b("🛍 Continuar comprando", "cat")], [b("🛡 Garantia", f"tk:{o['code']}"), b("‹ Início", "home")])
    if o["kind"] == "topup":
        await repo.x("UPDATE users SET balance=balance+? WHERE id=?", o["amount"], o["user_id"])
        await repo.x("UPDATE orders SET status='delivered' WHERE id=?", oid)
        u = await repo.one("SELECT balance FROM users WHERE id=?", o["user_id"])
        return await show_done(o, f"{header('Recarga confirmada', o['code'])}\n+{brl(o['amount'])} na carteira\n"
                                  f"👛 Saldo atual  <b>{brl(u['balance'])}</b>", done_kb)
    p = await repo.one("SELECT * FROM products WHERE id=?", o["product_id"])
    items = await repo.take_stock(p["id"], o["qty"], oid)
    if items is None:
        await repo.x("UPDATE users SET balance=balance+? WHERE id=?", o["amount"], o["user_id"])
        await repo.x("UPDATE orders SET status='refunded' WHERE id=?", oid)
        return await show_done(o, f"{header('Estoque esgotado', o['code'])}\nO produto acabou durante o pagamento.\n"
                                  f"<b>{brl(o['amount'])}</b> foram creditados na sua carteira.", done_kb)
    await repo.x("UPDATE orders SET status='delivered' WHERE id=?", oid)
    await repo.x("UPDATE products SET sold=sold+? WHERE id=?", o["qty"], p["id"])
    await repo.x("UPDATE users SET spent=spent+? WHERE id=?", o["amount"], o["user_id"])
    if o["coupon"]:
        await repo.x("UPDATE coupons SET uses_left=uses_left-1 WHERE code=? AND uses_left>0", o["coupon"])
    u = await repo.one("SELECT ref_by FROM users WHERE id=?", o["user_id"])
    if u["ref_by"]:
        bonus = round(o["amount"] * REF_BONUS_PCT / 100, 2)
        await repo.x("UPDATE users SET balance=balance+? WHERE id=?", bonus, u["ref_by"])
        try:
            await bot.send_message(u["ref_by"], f"🤝 Indicação: você ganhou <b>{brl(bonus)}</b> de saldo!")
        except Exception:
            pass
    body = "\n".join(f"<code>{i}</code>" for i in items)
    cap = (f"{header('Pedido entregue', o['code'])}\n◆ {p['name']}  ×{o['qty']}\n\n{body}\n\n"
           f"🛡 Garantia {p['warranty_days']} dias · reenviar: /pedido {o['code']}")
    await show_done(o, cap[:1024], done_kb)
    if len(cap) > 1024:
        await bot.send_message(o["user_id"], body)
    if CHANNEL_ID:
        try:
            await bot.send_message(CHANNEL_ID, f"🟢 Acabou de sair: <b>{p['name']}</b>")
        except Exception:
            pass
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, f"💸 Venda {o['code']} · {p['name']} ×{o['qty']} · {brl(o['amount'])} · {o['method']}")
        except Exception:
            pass


async def poller():
    while True:
        try:
            for o in await repo.q("SELECT * FROM orders WHERE status='pending' AND ext_id IS NOT NULL"):
                await process(o)
            for o in await repo.q("SELECT id FROM orders WHERE status='paid'"):
                await deliver(o["id"])
        except Exception:
            logging.exception("poller")
        await asyncio.sleep(POLL_SECONDS)


# ───────────────────────── conta, pedidos, indicação ─────────────────────────
@router.callback_query(F.data == "me")
async def me(cb: CallbackQuery):
    u = await user(cb.from_user)
    (mn, lvl, ico), nxt = level(u["spent"])
    n = (await repo.one("SELECT COUNT(*) n FROM orders WHERE user_id=? AND status='delivered' AND kind='buy'", u["id"]))["n"]
    prog = f"\n{bar((u['spent'] - mn) / (nxt[0] - mn))}  faltam {brl(nxt[0] - u['spent'])} p/ {nxt[2]} {nxt[1]}" if nxt else "\nNível máximo 💎"
    cap = (f"{header('Minha conta')}\n👛 Saldo  <b>{brl(u['balance'])}</b>\n{ico} Nível  <b>{lvl}</b>{prog}\n\n"
           f"🧾 Compras  {n}\n💳 Total gasto  {brl(u['spent'])}\n🆔 <code>{u['id']}</code>")
    await screen(cb, "profile", cap, kb([b("➕ Recarregar saldo", "topup")], [b("📦 Meus pedidos", "orders")], back()))


@router.callback_query(F.data == "topup")
async def topup(cb: CallbackQuery):
    vals = [20, 50, 100, 200]
    await screen(cb, "profile", header("Recarregar saldo", "Use depois em qualquer compra, na hora"),
                 kb([b(brl(v), f"tp:{v}") for v in vals[:2]], [b(brl(v), f"tp:{v}") for v in vals[2:]], back("me")))


@router.callback_query(F.data.startswith("tp:"))
async def topup_go(cb: CallbackQuery, state: FSMContext):
    v = float(cb.data[3:])
    await state.set_data({"kind": "topup", "uid": cb.from_user.id, "total": v, "usdt": await bp.brl_to_usdt(v)})
    d = await state.get_data()
    await screen(cb, "checkout", f"{header('Recarga')}\n<b>{brl(v)}</b>  ≈ {usd(d['usdt'])}\n\nComo quer pagar?",
                 kb([b("⚡ Pix", "pay:pix"), b("🟡 USDT", "pay:usdt")], back("topup")))


@router.callback_query(F.data == "orders")
async def orders(cb: CallbackQuery):
    rows = await repo.q("SELECT o.*, p.name FROM orders o LEFT JOIN products p ON p.id=o.product_id WHERE user_id=? "
                        "AND status IN('delivered','refunded') ORDER BY o.id DESC LIMIT 8", cb.from_user.id)
    btns = [[b(f"{'◆' if r['kind'] == 'buy' else '➕'} {r['code']} · {r['name'] or 'Recarga'} · {brl(r['amount'])}",
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
    items = [i["content"] for i in await order_items(code, cb.from_user.id)] or ([o["unlimited"]] if o["unlimited"] else [])
    cap = (f"{header(o['name'] or 'Recarga', code)}\n{brl(o['amount'])} · {o['method']} · {o['created_at'][:16]}\n\n" +
           "\n".join(f"<code>{i}</code>" for i in items))
    await screen(cb, "done", cap[:1024], kb([b("🛡 Acionar garantia", f"tk:{code}")], back("orders")))


@router.message(Command("pedido"))
async def resend(m: Message, command: CommandObject):
    items = await order_items((command.args or "").strip().upper(), m.from_user.id)
    await m.answer("\n".join(f"<code>{i['content']}</code>" for i in items) or "Pedido não encontrado.")


@router.callback_query(F.data == "ref")
async def ref(cb: CallbackQuery):
    me_ = await bot.me()
    n = (await repo.one("SELECT COUNT(*) n FROM users WHERE ref_by=?", cb.from_user.id))["n"]
    link = f"https://t.me/{me_.username}?start=r{cb.from_user.id}"
    cap = (f"{header('Indique e ganhe')}\nCada compra de quem entrar pelo seu link vira "
           f"<b>{REF_BONUS_PCT:g}%</b> de saldo para você. Para sempre.\n\n🔗 Seu link:\n<code>{link}</code>\n\n"
           f"👥 Indicados  {n}")
    await screen(cb, "profile", cap, kb([b("📤 Compartilhar", url=f"https://t.me/share/url?url={link}&text=Loja%20VEXA%20%E2%80%94%20comprou%2C%20chegou.")], back()))


# ───────────────────────── suporte e garantia ─────────────────────────
@router.callback_query(F.data == "sup")
async def support(cb: CallbackQuery):
    rows = [[b("🛡 Acionar garantia de um pedido", "orders")]]
    if SUPPORT_USERNAME:
        rows.append([b("💬 Falar com atendente", url=f"https://t.me/{SUPPORT_USERNAME}")])
    await screen(cb, "support", f"{header('Suporte')}\nProblema com um produto? Abra a garantia pelo pedido "
                                "— repomos ou devolvemos em saldo.\n\n🕘 Atendimento humano todos os dias.",
                 kb(*rows, back()))


@router.callback_query(F.data.startswith("tk:"))
async def ticket_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.ticket)
    await state.update_data(code=cb.data[3:])
    await cb.answer("Descreva o problema numa mensagem ✍️", show_alert=True)


@router.message(S.ticket)
async def ticket_text(m: Message, state: FSMContext):
    d = await state.get_data()
    await state.clear()
    tid = await repo.x("INSERT INTO tickets(user_id,order_code,text) VALUES(?,?,?)", m.from_user.id, d["code"], m.text)
    await m.answer(f"✅ Chamado <b>#{tid}</b> aberto para o pedido {d['code']}. Respondemos por aqui.",
                   reply_markup=kb([b("‹ Início", "home")]))
    for a in ADMIN_IDS:
        await bot.send_message(a, f"🎫 #{tid} · {d['code']} · {m.from_user.full_name} (<code>{m.from_user.id}</code>)\n"
                                  f"{m.text}\n\nResponder: /responder {m.from_user.id} texto")


# ───────────────────────── admin ─────────────────────────
ADMIN_HELP = """<b>◆ Painel admin</b>
━━━━━━━━━━━━━━━━━━
<b>Catálogo</b>
/cat Nome|emoji
/add cat_id|Nome|preço|garantia|descrição|preço_antigo
/infinito id|conteúdo  — entrega ilimitada (link/arquivo)
/estoque id  + linhas abaixo (1 unidade por linha)
/img id  — responda a uma foto para usar no produto
/preco id|valor · /off id · /on id · /lista
<b>Vendas</b>
/vendas · /cupom CODIGO|%|usos(-1=∞)
/credito user_id|valor · /responder user_id texto
/aviso texto — mensagem para todos"""


@router.callback_query(F.data == "adm")
async def adm(cb: CallbackQuery):
    if is_admin(cb.from_user.id):
        await cb.message.answer(ADMIN_HELP)
    await cb.answer()


def admin_msg(m: Message): return is_admin(m.from_user.id)


def args(m, n):
    p = [x.strip() for x in (m.text.split(maxsplit=1) + [""])[1].split("|")]
    if len(p) < n or not p[0]:
        raise ValueError
    return p


@router.message(Command("admin"), admin_msg)
async def a_help(m: Message): await m.answer(ADMIN_HELP)


@router.message(Command("cat"), admin_msg)
async def a_cat(m: Message):
    p = args(m, 1)
    cid = await repo.x("INSERT INTO categories(name,emoji) VALUES(?,?)", p[0], p[1] if len(p) > 1 and p[1] else "◆")
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
    await repo.x("INSERT OR REPLACE INTO coupons(code,pct,uses_left) VALUES(?,?,?)", p[0].upper(), float(p[1]), int(p[2]))
    await m.answer(f"🎟 Cupom {p[0].upper()} · {p[1]}% criado.")


@router.message(Command("vendas"), admin_msg)
async def a_sales(m: Message):
    q = "SELECT COUNT(*) n, COALESCE(SUM(amount),0) t FROM orders WHERE status='delivered' AND kind='buy'"
    d, w, t = [await repo.one(q + extra) for extra in
               (" AND date(paid_at)=date('now')", " AND paid_at>=datetime('now','-7 days')", "")]
    u = (await repo.one("SELECT COUNT(*) n FROM users"))["n"]
    await m.answer(f"<b>◆ Vendas</b>\nHoje  {d['n']} · {brl(d['t'])}\n7 dias  {w['n']} · {brl(w['t'])}\n"
                   f"Total  {t['n']} · {brl(t['t'])}\nClientes  {u}")


@router.message(Command("credito"), admin_msg)
async def a_credit(m: Message):
    uid, v = args(m, 2)[:2]
    await repo.x("UPDATE users SET balance=balance+? WHERE id=?", float(v.replace(",", ".")), int(uid))
    await m.answer("OK")


@router.message(Command("responder"), admin_msg)
async def a_reply(m: Message):
    _, uid, txt = m.text.split(maxsplit=2)
    await bot.send_message(int(uid), f"💬 <b>Suporte VEXA</b>\n{txt}")
    await m.answer("Enviado.")


@router.message(Command("aviso"), admin_msg)
async def a_broadcast(m: Message, command: CommandObject):
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
    logging.exception(ev.exception)
    msg = getattr(ev.update, "message", None)
    if msg and isinstance(ev.exception, (ValueError, IndexError, TypeError)):
        await msg.answer("Formato inválido. Veja /admin.")
    return True


async def main():
    global repo, bot
    repo = Repo(await connect())
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(poller())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
