import asyncio
import logging
import secrets
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton as IB, InlineKeyboardMarkup as IK,
                           KeyboardButton as KB, Message, ReplyKeyboardMarkup as RK)

from . import binance_pay as bp
from .config import (ADMIN_IDS, BOT_TOKEN, CHANNEL_ID, ORDER_TIMEOUT_MIN, POLL_SECONDS,
                     STORE_NAME, SUPPORT_USERNAME)
from .db import Repo, connect

logging.basicConfig(level=logging.INFO)
router = Router()
repo: Repo
bot: Bot

MENU = RK(resize_keyboard=True, keyboard=[
    [KB(text="🛍 Produtos"), KB(text="💬 Suporte")],
    [KB(text="👛 Carteira"), KB(text="📜 Meus pedidos")],
    [KB(text="🛡 Garantia")],
], input_field_placeholder="Escolha uma opção abaixo")


def money(v): return f"${v:.2f}"


def stock_txt(c): return "∞" if c is None else str(c)


# ---------------- cliente ----------------
@router.message(CommandStart())
async def start(m: Message):
    await repo.ensure_user(m.from_user.id, m.from_user.username)
    await m.answer(f"⚡ <b>Bem-vindo à {STORE_NAME}</b>\n\nPagamento via <b>Binance Pay</b>, "
                   "entrega automática em segundos, 24/7.", reply_markup=MENU)
    await show_catalog(m)


@router.message(F.text == "🛍 Produtos")
async def show_catalog(m: Message):
    prods = await repo.q("SELECT p.*, c.emoji FROM products p LEFT JOIN categories c ON c.id=p.category_id "
                         "WHERE active=1 ORDER BY c.name, p.name")
    if not prods:
        return await m.answer("Catálogo vazio no momento.")
    rows = []
    for p in prods:
        c = await repo.stock_count(p["id"])
        mark = "❌ " if c == 0 else f"{p['emoji'] or '📦'} "
        rows.append([IB(text=f"{mark}{p['name']} | {money(p['price'])} | 📦 {stock_txt(c)}",
                        callback_data=f"p:{p['id']}")])
    await m.answer("🛍 <b>Produtos</b>", reply_markup=IK(inline_keyboard=rows))


@router.callback_query(F.data.startswith("p:"))
async def product(cb: CallbackQuery):
    pid = int(cb.data[2:])
    p = await repo.one("SELECT * FROM products WHERE id=?", pid)
    c = await repo.stock_count(pid)
    u = await repo.one("SELECT balance FROM users WHERE id=?", cb.from_user.id)
    txt = (f"<b>{p['name']}</b>\n\n{p['description']}\n\n💵 Preço: <b>{money(p['price'])}</b>\n"
           f"📦 Estoque: {stock_txt(c)}\n🛡 Garantia: {p['warranty_days']} dias\n👛 Seu saldo: {money(u['balance'])}")
    kb = []
    if c != 0:
        kb.append([IB(text="💳 Pagar com Binance Pay", callback_data=f"buy:{pid}")])
        if u["balance"] >= p["price"]:
            kb.append([IB(text="👛 Pagar com saldo", callback_data=f"bal:{pid}")])
    kb.append([IB(text="⬅️ Voltar", callback_data="cat")])
    await cb.message.edit_text(txt, reply_markup=IK(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data == "cat")
async def back(cb: CallbackQuery):
    await cb.message.delete()
    await show_catalog(cb.message)
    await cb.answer()


async def new_order(uid, kind, amount, pid=None):
    trade = f"NX{datetime.utcnow():%y%m%d}{secrets.token_hex(5).upper()}"
    oid = await repo.x("INSERT INTO orders(user_id,kind,product_id,amount,merchant_trade_no) VALUES(?,?,?,?,?)",
                       uid, kind, pid, amount, trade)
    return oid, trade


async def send_checkout(msg: Message, oid, trade, amount, title):
    try:
        d = await bp.create_order(trade, amount, title)
    except Exception as e:
        logging.exception("create_order")
        await repo.x("UPDATE orders SET status='failed' WHERE id=?", oid)
        return await msg.answer(f"⚠️ Não foi possível gerar o pagamento agora. Tente de novo.\n<code>{e}</code>")
    await repo.x("UPDATE orders SET prepay_id=?, checkout_url=? WHERE id=?", d["prepayId"], d["checkoutUrl"], oid)
    kb = IK(inline_keyboard=[[IB(text="💳 Abrir Binance Pay", url=d["checkoutUrl"])],
                             [IB(text="🔄 Já paguei", callback_data=f"chk:{oid}")]])
    await msg.answer(f"🧾 Pedido <b>#{oid}</b> — {title}\nValor: <b>{money(amount)}</b>\n\n"
                     f"Pague em até {ORDER_TIMEOUT_MIN} min. A confirmação e entrega são automáticas.",
                     reply_markup=kb)


@router.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery):
    p = await repo.one("SELECT * FROM products WHERE id=?", int(cb.data[4:]))
    if await repo.stock_count(p["id"]) == 0:
        return await cb.answer("Sem estoque.", show_alert=True)
    oid, trade = await new_order(cb.from_user.id, "buy", p["price"], p["id"])
    await send_checkout(cb.message, oid, trade, p["price"], p["name"])
    await cb.answer()


@router.callback_query(F.data.startswith("bal:"))
async def buy_balance(cb: CallbackQuery):
    p = await repo.one("SELECT * FROM products WHERE id=?", int(cb.data[4:]))
    u = await repo.one("SELECT balance FROM users WHERE id=?", cb.from_user.id)
    if u["balance"] < p["price"]:
        return await cb.answer("Saldo insuficiente.", show_alert=True)
    oid, _ = await new_order(cb.from_user.id, "buy", p["price"], p["id"])
    await repo.x("UPDATE users SET balance=balance-? WHERE id=?", p["price"], cb.from_user.id)
    await repo.x("UPDATE orders SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE id=?", oid)
    await deliver(oid)
    await cb.answer()


@router.callback_query(F.data.startswith("chk:"))
async def check(cb: CallbackQuery):
    o = await repo.one("SELECT * FROM orders WHERE id=? AND user_id=?", int(cb.data[4:]), cb.from_user.id)
    if not o:
        return await cb.answer()
    await process(o)
    o = await repo.one("SELECT status FROM orders WHERE id=?", o["id"])
    await cb.answer({"pending": "Pagamento ainda não identificado.", "delivered": "Entregue ✅",
                     "expired": "Pedido expirado."}.get(o["status"], o["status"]), show_alert=True)


@router.message(F.text == "👛 Carteira")
async def wallet(m: Message):
    await repo.ensure_user(m.from_user.id, m.from_user.username)
    u = await repo.one("SELECT balance FROM users WHERE id=?", m.from_user.id)
    kb = IK(inline_keyboard=[[IB(text=f"+ {money(v)}", callback_data=f"top:{v}") for v in (5, 10, 25, 50)]])
    await m.answer(f"👛 Saldo: <b>{money(u['balance'])}</b>\nRecarregue via Binance Pay:", reply_markup=kb)


@router.callback_query(F.data.startswith("top:"))
async def topup(cb: CallbackQuery):
    v = float(cb.data[4:])
    oid, trade = await new_order(cb.from_user.id, "topup", v)
    await send_checkout(cb.message, oid, trade, v, f"Recarga {STORE_NAME}")
    await cb.answer()


@router.message(F.text == "📜 Meus pedidos")
async def my_orders(m: Message):
    rows = await repo.q("SELECT o.*, p.name FROM orders o LEFT JOIN products p ON p.id=o.product_id "
                        "WHERE user_id=? AND status IN('delivered','paid') ORDER BY o.id DESC LIMIT 15", m.from_user.id)
    if not rows:
        return await m.answer("Nenhum pedido ainda.")
    await m.answer("\n".join(f"#{r['id']} · {r['name'] or 'Recarga'} · {money(r['amount'])} · {r['created_at'][:10]}"
                             for r in rows) + "\n\nReenviar entrega: /pedido ID")


@router.message(Command("pedido"))
async def resend(m: Message):
    try:
        oid = int(m.text.split()[1])
    except (IndexError, ValueError):
        return await m.answer("Uso: /pedido ID")
    items = await repo.q("SELECT s.content FROM stock s JOIN orders o ON o.id=s.order_id "
                         "WHERE o.id=? AND o.user_id=?", oid, m.from_user.id)
    if not items:
        return await m.answer("Pedido não encontrado.")
    await m.answer("\n".join(f"<code>{i['content']}</code>" for i in items))


@router.message(F.text == "💬 Suporte")
async def support(m: Message):
    await m.answer(f"💬 Fale com o suporte: @{SUPPORT_USERNAME}\nOu envie: /ticket ID_DO_PEDIDO descrição")


@router.message(F.text == "🛡 Garantia")
async def warranty(m: Message):
    await m.answer("🛡 Cada produto tem prazo de garantia indicado na página dele. Problema com a entrega? "
                   "Abra um chamado: /ticket ID_DO_PEDIDO descrição — reposição ou crédito em carteira.")


@router.message(Command("ticket"))
async def ticket(m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        return await m.answer("Uso: /ticket ID_DO_PEDIDO descrição do problema")
    tid = await repo.x("INSERT INTO tickets(user_id,order_id,text) VALUES(?,?,?)", m.from_user.id, int(parts[1]), parts[2])
    await m.answer(f"✅ Chamado #{tid} aberto. Resposta em breve.")
    for a in ADMIN_IDS:
        await bot.send_message(a, f"🎫 Ticket #{tid} de {m.from_user.id} (pedido {parts[1]}):\n{parts[2]}\n"
                                  f"Responder: /responder {m.from_user.id} texto")


# ---------------- pagamento/entrega ----------------
async def process(o):
    if o["status"] != "pending":
        return
    if datetime.utcnow() - datetime.fromisoformat(o["created_at"]) > timedelta(minutes=ORDER_TIMEOUT_MIN + 5):
        await repo.x("UPDATE orders SET status='expired' WHERE id=?", o["id"])
        return
    try:
        st = await bp.query_status(o["merchant_trade_no"])
    except Exception:
        logging.exception("query")
        return
    if st == "PAID":
        n = await repo.x("UPDATE orders SET status='paid', paid_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'", o["id"])
        await deliver(o["id"])
    elif st in ("CANCELED", "EXPIRED", "ERROR"):
        await repo.x("UPDATE orders SET status='expired' WHERE id=?", o["id"])


async def deliver(oid):
    o = await repo.one("SELECT * FROM orders WHERE id=?", oid)
    if o["status"] != "paid":
        return
    if o["kind"] == "topup":
        await repo.x("UPDATE users SET balance=balance+? WHERE id=?", o["amount"], o["user_id"])
        await repo.x("UPDATE orders SET status='delivered' WHERE id=?", oid)
        return await bot.send_message(o["user_id"], f"✅ Recarga de {money(o['amount'])} creditada.")
    p = await repo.one("SELECT * FROM products WHERE id=?", o["product_id"])
    items = await repo.take_stock(p["id"], o["qty"], oid)
    if items is None:  # vendeu tudo enquanto pagava → credita em carteira
        await repo.x("UPDATE users SET balance=balance+? WHERE id=?", o["amount"], o["user_id"])
        await repo.x("UPDATE orders SET status='delivered' WHERE id=?", oid)
        return await bot.send_message(o["user_id"], f"⚠️ Estoque esgotado durante o pagamento. "
                                                    f"{money(o['amount'])} creditados na sua carteira.")
    await repo.x("UPDATE orders SET status='delivered' WHERE id=?", oid)
    body = "\n".join(f"<code>{i}</code>" for i in items)
    await bot.send_message(o["user_id"], f"✅ <b>Pedido #{oid} entregue</b> — {p['name']}\n\n{body}\n\n"
                                         f"🛡 Garantia: {p['warranty_days']} dias. Guarde esta mensagem.")
    if CHANNEL_ID:
        try:
            await bot.send_message(CHANNEL_ID, f"🟢 Venda: {p['name']} · {money(o['amount'])}")
        except Exception:
            pass


async def poller():
    while True:
        try:
            for o in await repo.q("SELECT * FROM orders WHERE status='pending' AND merchant_trade_no IS NOT NULL"):
                await process(o)
            for o in await repo.q("SELECT id FROM orders WHERE status='paid'"):
                await deliver(o["id"])  # recuperação após queda
        except Exception:
            logging.exception("poller")
        await asyncio.sleep(POLL_SECONDS)


# ---------------- admin ----------------
def admin(m: Message): return m.from_user.id in ADMIN_IDS


ADMIN_HELP = """<b>Admin</b>
/cat Nome|emoji — criar categoria
/add categoria_id|Nome|preço|garantia_dias|descrição — criar produto
/infinito produto_id|conteúdo — produto sem estoque (ex.: link de download)
/estoque produto_id  (+ linhas abaixo, 1 unidade por linha) — repor
/off produto_id · /on produto_id — ocultar/mostrar
/preco produto_id|valor
/lista — produtos e estoque
/vendas — resumo
/credito user_id|valor — ajustar saldo
/responder user_id texto
/aviso texto — broadcast para todos os usuários"""


@router.message(Command("admin"), admin)
async def a_help(m: Message): await m.answer(ADMIN_HELP)


def args(m, n):
    p = m.text.split(maxsplit=1)[1].split("|") if len(m.text.split(maxsplit=1)) > 1 else []
    if len(p) < n:
        raise ValueError
    return [x.strip() for x in p]


@router.message(Command("cat"), admin)
async def a_cat(m: Message):
    name, *e = args(m, 1)
    cid = await repo.x("INSERT INTO categories(name,emoji) VALUES(?,?)", name, e[0] if e else "📦")
    await m.answer(f"Categoria #{cid} criada.")


@router.message(Command("add"), admin)
async def a_add(m: Message):
    cid, name, price, w, *d = args(m, 4)
    pid = await repo.x("INSERT INTO products(category_id,name,price,warranty_days,description) VALUES(?,?,?,?,?)",
                       int(cid), name, float(price), int(w), d[0] if d else "")
    await m.answer(f"Produto #{pid} criado. Reponha com /estoque {pid}")


@router.message(Command("infinito"), admin)
async def a_inf(m: Message):
    pid, content = args(m, 2)
    await repo.x("UPDATE products SET unlimited=? WHERE id=?", content, int(pid))
    await m.answer("OK — estoque ∞.")


@router.message(Command("estoque"), admin)
async def a_stock(m: Message):
    lines = m.text.split("\n")
    pid = int(lines[0].split()[1])
    items = [l.strip() for l in lines[1:] if l.strip()]
    await repo.db.executemany("INSERT INTO stock(product_id,content) VALUES(?,?)", [(pid, i) for i in items])
    await repo.db.commit()
    await m.answer(f"+{len(items)} unidades. Estoque: {await repo.stock_count(pid)}")


@router.message(Command("off", "on"), admin)
async def a_toggle(m: Message):
    pid = int(m.text.split()[1])
    await repo.x("UPDATE products SET active=? WHERE id=?", int(m.text.startswith("/on")), pid)
    await m.answer("OK")


@router.message(Command("preco"), admin)
async def a_price(m: Message):
    pid, v = args(m, 2)
    await repo.x("UPDATE products SET price=? WHERE id=?", float(v), int(pid))
    await m.answer("OK")


@router.message(Command("lista"), admin)
async def a_list(m: Message):
    out = [f"#{p['id']} {'🟢' if p['active'] else '⚪'} {p['name']} {money(p['price'])} 📦{stock_txt(await repo.stock_count(p['id']))}"
           for p in await repo.q("SELECT * FROM products")]
    await m.answer("\n".join(out) or "vazio")


@router.message(Command("vendas"), admin)
async def a_sales(m: Message):
    r = await repo.one("SELECT COUNT(*) n, COALESCE(SUM(amount),0) t FROM orders WHERE status='delivered' AND kind='buy'")
    d = await repo.one("SELECT COUNT(*) n, COALESCE(SUM(amount),0) t FROM orders WHERE status='delivered' "
                       "AND kind='buy' AND date(paid_at)=date('now')")
    u = await repo.one("SELECT COUNT(*) n FROM users")
    await m.answer(f"Hoje: {d['n']} vendas · {money(d['t'])}\nTotal: {r['n']} · {money(r['t'])}\nUsuários: {u['n']}")


@router.message(Command("credito"), admin)
async def a_credit(m: Message):
    uid, v = args(m, 2)
    await repo.x("UPDATE users SET balance=balance+? WHERE id=?", float(v), int(uid))
    await m.answer("OK")


@router.message(Command("responder"), admin)
async def a_reply(m: Message):
    _, uid, txt = m.text.split(maxsplit=2)
    await bot.send_message(int(uid), f"💬 Suporte:\n{txt}")
    await m.answer("Enviado.")


@router.message(Command("aviso"), admin)
async def a_broadcast(m: Message):
    txt = m.text.split(maxsplit=1)[1]
    n = 0
    for u in await repo.q("SELECT id FROM users"):
        try:
            await bot.send_message(u["id"], txt)
            n += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await m.answer(f"Enviado para {n}.")


@router.errors()
async def on_error(ev):
    logging.exception(ev.exception)
    msg = getattr(ev.update, "message", None)
    if msg and isinstance(ev.exception, (ValueError, IndexError)):
        await msg.answer("Formato inválido. /admin para ajuda.")
    return True


async def main():
    global repo, bot
    from aiogram.client.default import DefaultBotProperties
    repo = Repo(await connect())
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(poller())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
