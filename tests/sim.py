"""Simulação ponta a ponta: roda os handlers reais com um Telegram falso, 10 clientes + admin.
Uso: python -m tests.sim   (SANDBOX=1, banco temporário, sem rede do Telegram)"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime

os.environ.update(BOT_TOKEN="123456:TESTE_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", SANDBOX="1", ADMIN_IDS="1323196351", CHANNEL_ID="@canal",
                  DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"), SUPPORT_USERNAME="vexasuporte",
                  SUP_ALLOW_PRIVATE="1")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot import binance_pay, main as M
from bot.db import Repo, connect

ADMIN = 1323196351
ok_n = fail_n = 0
results = []


def check(name, cond, info=""):
    global ok_n, fail_n
    ok_n += bool(cond)
    fail_n += not cond
    results.append(("OK  " if cond else "FALHA") + f"  {name}" + (f"  — {info}" if info and not cond else ""))


class FakeTG(BaseSession):
    def __init__(self):
        super().__init__()
        self.mid = 1000
        self.chats = {}  # chat_id -> list[dict]
        self.alerts = {}  # user_id -> last callback answer text

    async def close(self): pass

    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="VEXA", username="vexashopping_bot")
        if name == "AnswerCallbackQuery":
            self.alerts[self.cbuser.get(method.callback_query_id)] = method.text
            return True
        if name in ("DeleteMessage",):
            for msgs in self.chats.values():
                for m in msgs:
                    if m["id"] == method.message_id:
                        m["deleted"] = True
            return True
        chat = int(method.chat_id) if str(method.chat_id).lstrip("-").isdigit() else method.chat_id
        if name == "EditMessageMedia":
            text, mid = method.media.caption, method.message_id
        else:
            self.mid += 1
            text, mid = getattr(method, "caption", None) or getattr(method, "text", None), self.mid
        rec = {"id": mid, "method": name, "text": text or "", "kb": method.reply_markup, "deleted": False}
        self.chats.setdefault(chat, []).append(rec)
        return Message.model_validate({
            "message_id": mid, "date": datetime.now(), "chat": {"id": chat if isinstance(chat, int) else -1, "type": "private"},
            "photo": [{"file_id": f"file{mid}", "file_unique_id": f"u{mid}", "width": 1, "height": 1}],
            "caption": text}, context={"bot": bot})

    cbuser = {}


tg = FakeTG()
bot = Bot("123:abc", session=tg, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
dp.include_router(M.router)
UPD = [0]


def screen_of(uid):
    """Última mensagem com teclado (a 'tela' atual do usuário)."""
    for m in reversed(tg.chats.get(uid, [])):
        if m["kb"] is not None and not m["deleted"]:
            return m
    return None


def last_text(uid):
    return tg.chats[uid][-1]["text"] if tg.chats.get(uid) else ""


def buttons(uid):
    s = screen_of(uid)
    return [x for row in s["kb"].inline_keyboard for x in row] if s else []


class U:
    def __init__(self, uid, name):
        self.id, self.name = uid, name
        self.user = User(id=uid, is_bot=False, first_name=name, username=f"u{uid}")

    async def send(self, text, reply_to_photo=False):
        UPD[0] += 1
        msg = {"message_id": UPD[0] + 500000, "date": datetime.now(), "chat": {"id": self.id, "type": "private"},
               "from": self.user.model_dump(), "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await dp.feed_update(bot, Update.model_validate({"update_id": UPD[0], "message": msg}, context={"bot": bot}))

    def has(self, label):
        return any(label in x.text for x in buttons(self.id))

    async def click(self, label, data=None):
        s = screen_of(self.id)
        btn = next((x for x in buttons(self.id) if label in x.text), None)
        if data is None:
            if not btn:
                raise AssertionError(f"{self.name}: botão '{label}' não existe. Tela: {[x.text for x in buttons(self.id)]}")
            if btn.url:
                return btn.url
            data = btn.callback_data
        UPD[0] += 1
        cid = f"cb{UPD[0]}"
        tg.cbuser[cid] = self.id
        msg = {"message_id": s["id"] if s else 1, "date": datetime.now(), "chat": {"id": self.id, "type": "private"},
               "photo": [{"file_id": "x", "file_unique_id": "x", "width": 1, "height": 1}], "caption": s["text"] if s else ""}
        cb = {"id": cid, "from": self.user.model_dump(), "chat_instance": "c", "data": data, "message": msg}
        await dp.feed_update(bot, Update.model_validate({"update_id": UPD[0], "callback_query": cb}, context={"bot": bot}))
        return data

    @property
    def alert(self):
        return tg.alerts.get(self.id)

    @property
    def text(self):
        s = screen_of(self.id)
        return s["text"] if s else ""


async def bal(uid):
    return (await M.repo.one("SELECT balance FROM users WHERE id=?", uid))["balance"]


async def run():
    M.repo = Repo(await connect())
    M.bot = bot
    binance_pay._rate.update(v=5.5, t=9e18)  # sem rede
    adm = U(ADMIN, "Thiago")
    errors = []
    import logging
    class H(logging.Handler):
        def emit(self, r):
            if r.levelno >= logging.ERROR:
                errors.append(r.getMessage() + (" " + str(r.exc_info[1]) if r.exc_info else ""))
    logging.getLogger().addHandler(H())
    logging.getLogger().setLevel(logging.WARNING)

    # ── Admin monta a loja ──
    await adm.send("/admin"); check("admin: /admin mostra painel", "Painel admin" in last_text(ADMIN))
    await adm.send("/cat Cursos|🎓"); check("admin: /cat cria categoria", "Categoria #1" in last_text(ADMIN))
    await adm.send("/cat Templates|🧩")
    await adm.send("/add 1|Curso Python|29,90|7|Curso completo|59,90"); check("admin: /add cria produto", "Produto #1" in last_text(ADMIN))
    await adm.send("/add 1|Ebook Marketing|9,90|0|PDF")
    await adm.send("/add 2|Pack Canva|15|30|200 templates")
    await adm.send("/add 2|Planilha Financeira|19,90|0|Excel")
    await adm.send("/estoque 1\n" + "\n".join(f"PY-KEY-{i:03}" for i in range(20)))
    check("admin: /estoque adiciona", "+20 un." in last_text(ADMIN), last_text(ADMIN))
    await adm.send("/estoque 3\nCANVA-ONLY-1")
    await adm.send("/infinito 2|https://link.exemplo/ebook.pdf"); check("admin: /infinito", "ilimitada" in last_text(ADMIN))
    await adm.send("/infinito 4|https://link.exemplo/planilha.xlsx")
    await adm.send("/cupom VEXA10|10|5"); check("admin: /cupom", "VEXA10" in last_text(ADMIN))
    await adm.send("/lista"); check("admin: /lista", "Curso Python" in last_text(ADMIN) and "∞" in last_text(ADMIN))
    await adm.send("/add 1|quebrado"); check("admin: formato inválido avisa", "Formato inválido" in last_text(ADMIN), last_text(ADMIN))
    nonadm = U(5, "Intruso"); await nonadm.send("/lista"); check("não-admin não acessa /lista", not tg.chats.get(5))

    users = [U(100 + i, n) for i, n in enumerate(
        ["Ana", "Bruno", "Carla", "Diego", "Eva", "Fabio", "Gabi", "Hugo", "Iris", "Joao"])]
    ana, bruno, carla, diego, eva, fabio, gabi, hugo, iris, joao = users

    # ── Todos dão /start (Bruno e Carla pelo link da Ana) ──
    await asyncio.gather(*[u.send("/start r100" if u in (bruno, carla) else "/start") for u in users])
    check("10 usuários: /start mostra menu", all(screen_of(u.id) and "Olá" in screen_of(u.id)["text"] for u in users))
    check("menu: sem 'Mais vendidos'/'Admin'", not any(ana.has(x) for x in ("Mais vendidos", "Admin")))
    check("indicação registrada", (await M.repo.one("SELECT COUNT(*) n FROM users WHERE ref_by=100"))["n"] == 2)

    # ── Navegação: todos os botões do menu ──
    for lbl, expect in [("Explorar catálogo", "Catálogo"), ("Meus pedidos", "Meus pedidos"), ("Minha conta", "Minha conta"),
                        ("Indique e ganhe", "Indique e ganhe"), ("Suporte", "Suporte")]:
        await ana.click("Voltar") if ana.has("Voltar") else None
        await ana.click(lbl); check(f"botão '{lbl}'", expect in ana.text, ana.text[:60])
        if lbl == "Indique e ganhe":
            check("indique: link correto", "start=r100" in ana.text, ana.text[-80:])
    check("suporte: botão atendente", ana.has("Falar com atendente"))
    await ana.click("Voltar"); check("voltar → início", "Olá" in ana.text)

    # ── Ana: catálogo → produto → quantidade → Pix com CPF ──
    await ana.click("Explorar catálogo"); check("catálogo lista 2 categorias", ana.has("Cursos") and ana.has("Templates"))
    await ana.click("Cursos"); check("categoria lista produtos", ana.has("Curso Python") and ana.has("Ebook"))
    await ana.click("Curso Python"); check("card: preço riscado e desconto", "<s>" in ana.text and "-50%" in ana.text, ana.text)
    await ana.click("+"); await ana.click("+"); check("quantidade + → 3", ana.has("3 un."))
    await ana.click("−"); check("quantidade − → 2", ana.has("2 un."))
    await ana.click("Comprar"); check("checkout total 2×29,90", "59,80" in ana.text, ana.text)
    check("checkout: sem saldo não mostra botão Saldo", not ana.has("Saldo"))
    check("checkout: USDT aparece no sandbox", ana.has("USDT"))
    await ana.click("Pix"); check("Pix pede CPF na 1ª compra", "CPF" in ana.text)
    await ana.send("123"); check("CPF inválido recusado", "CPF inválido" in last_text(ana.id))
    await ana.send("529.982.247-25"); check("Pix: QR + copia e cola", "Copia e cola" in ana.text and ana.has("Já paguei"))
    await ana.click("Já paguei")
    check("Pix → entrega automática (sandbox)", "PY-KEY-" in last_text(ana.id) and "Pagamento aprovado" in last_text(ana.id), last_text(ana.id)[:120])
    check("entrega 2 chaves", last_text(ana.id).count("PY-KEY-") == 2)
    check("admin recebe aviso de venda", "Venda" in last_text(ADMIN))
    check("canal recebe aviso", any("Acabou de sair" in m["text"] for m in tg.chats.get("@canal", [])))

    # ── Bruno (indicado pela Ana): compra Pix → Ana ganha 5% ──
    b0 = await bal(100)
    await bruno.click("Explorar catálogo"); await bruno.click("Templates"); await bruno.click("Planilha")
    await bruno.click("Comprar"); await bruno.click("Pix"); await bruno.send("11144477735"); await bruno.click("Já paguei")
    check("Bruno recebe produto ilimitado", "planilha.xlsx" in last_text(bruno.id), last_text(bruno.id)[:80])
    check("Ana ganha comissão 5% (R$ 0,99)", abs(await bal(100) - b0 - 0.99) < 0.001, await bal(100))
    check("Ana avisada da comissão", any("Comissão recebida" in m["text"] for m in tg.chats[100]))

    # ── Carla: recarga R$20 por Pix → compra com saldo ──
    await carla.click("Minha conta"); await carla.click("Adicionar saldo")
    check("recarga: valores 20/50/100/200", all(carla.has(v) for v in ("20,00", "50,00", "100,00", "200,00")))
    await carla.click("20,00"); await carla.click("Pix"); await carla.send("39053344705"); await carla.click("Já paguei")
    check("recarga creditada R$20", await bal(102) == 20, await bal(102))
    check("tela de recarga confirmada", "Recarga confirmada" in last_text(carla.id))
    await carla.click("Continuar comprando"); await carla.click("Templates"); await carla.click("Planilha"); await carla.click("Comprar")
    check("checkout mostra botão Saldo", carla.has("Saldo"))
    await carla.click("Saldo")
    check("compra com saldo entrega", "planilha.xlsx" in last_text(carla.id))
    check("saldo debitado (20-19,90)", abs(await bal(102) - 0.10) < 0.001, await bal(102))
    check("recarga não gera comissão; compra sim (5% de 19,90)", abs(await bal(100) - b0 - 1.98) < 0.001, await bal(100))

    # ── Diego: cupom VEXA10 ──
    await diego.click("Explorar catálogo"); await diego.click("Cursos"); await diego.click("Curso Python"); await diego.click("Comprar")
    await diego.click("Tenho cupom"); check("cupom: pede código", "cupom" in (diego.alert or "").lower())
    await diego.send("vexa10")
    check("cupom aplicado (−10%)", "VEXA10" in diego.text and "26,91" in diego.text, diego.text[:200])
    if diego.has("Pix"):
        await diego.click("Pix"); await diego.send("52998224725"); await diego.click("Já paguei")
        o = await M.repo.one("SELECT * FROM orders WHERE user_id=103 ORDER BY id DESC")
        check("pedido com desconto cobra 26,91", o and abs(o["amount"] - 26.91) < 0.01, o and o["amount"])
        check("uso do cupom descontado", (await M.repo.one("SELECT uses_left FROM coupons"))["uses_left"] == 4)
    await diego.send("/start"); await diego.click("Explorar catálogo"); await diego.click("Cursos"); await diego.click("Ebook")
    await diego.click("Comprar"); await diego.click("Tenho cupom"); await diego.send("NAOEXISTE")
    check("cupom inválido avisa", "Cupom inválido" in last_text(diego.id))

    # ── Eva: USDT ──
    await eva.click("Explorar catálogo"); await eva.click("Cursos"); await eva.click("Ebook"); await eva.click("Comprar")
    await eva.click("USDT")
    check("USDT: tela com Binance Pay", eva.has("Abrir Binance Pay"), eva.text[:80])
    await eva.click("Já paguei"); check("USDT: confirma e entrega", "ebook.pdf" in last_text(eva.id))

    # ── Fabio: Pix pendente → Já paguei (pendente) → cancelar ──
    real = M.asaas.status
    async def pending(_): return "PENDING"
    M.asaas.status = pending
    await fabio.click("Explorar catálogo"); await fabio.click("Cursos"); await fabio.click("Ebook"); await fabio.click("Comprar")
    await fabio.click("Pix"); await fabio.send("52998224725")
    await fabio.click("Já paguei"); check("pendente: avisa que não identificou", "Ainda não" in (fabio.alert or ""))
    await fabio.click("Cancelar"); check("cancelar volta ao início", "Olá" in fabio.text)
    check("pedido cancelado", (await M.repo.one("SELECT status FROM orders WHERE user_id=105"))["status"] == "canceled")
    # pagou e clicou Cancelar → entrega em vez de cancelar
    await fabio.click("Explorar catálogo"); await fabio.click("Cursos"); await fabio.click("Ebook"); await fabio.click("Comprar")
    await fabio.click("Pix")
    M.asaas.status = real
    await fabio.click("Cancelar")
    check("pagou e clicou Cancelar: recebe o produto", "ebook.pdf" in last_text(fabio.id), last_text(fabio.id)[:80])
    # Pix expirado: pedido expira e a cobrança é apagada no Asaas
    M.asaas.status = pending
    canceled = []
    real_cancel = M.asaas.cancel
    async def fake_cancel(pid): canceled.append(pid)
    M.asaas.cancel = fake_cancel
    await fabio.click("Continuar comprando"); await fabio.click("Cursos"); await fabio.click("Ebook"); await fabio.click("Comprar")
    await fabio.click("Pix")
    o = await M.repo.one("SELECT * FROM orders WHERE user_id=105 ORDER BY id DESC")
    await M.repo.x("UPDATE orders SET created_at=datetime('now','-2 hours') WHERE id=?", o["id"])
    await M.process(await M.repo.one("SELECT * FROM orders WHERE id=?", o["id"]))
    st = (await M.repo.one("SELECT status FROM orders WHERE id=?", o["id"]))["status"]
    check("Pix vencido: expira e apaga a cobrança no Asaas", st == "expired" and canceled == [o["ext_id"]], f"{st} {canceled}")
    M.asaas.status, M.asaas.cancel = real, real_cancel

    # ── Gabi: pedidos, reabrir, garantia → admin responde ──
    await ana.click("Início") if ana.has("Início") else await ana.send("/start")
    await ana.click("Meus pedidos"); check("Meus pedidos lista compra", ana.has("Curso Python"))
    await ana.click("Curso Python"); check("reabrir mostra chaves", ana.text.count("PY-KEY-") == 2, ana.text)
    await ana.click("Acionar garantia"); await ana.send("A chave 2 não funcionou")
    check("ticket aberto", "Chamado" in last_text(ana.id))
    check("admin recebe ticket", "🎫" in last_text(ADMIN))
    await adm.send("/responder 100 Trocamos sua chave"); check("admin responde cliente", "Trocamos" in last_text(ana.id))
    code = (await M.repo.one("SELECT code FROM orders WHERE user_id=100 AND kind='buy'"))["code"]
    await ana.send(f"/pedido {code}"); check("/pedido reenvia", "PY-KEY-" in last_text(ana.id))
    await gabi.click("Meus pedidos"); check("sem compras: mensagem vazia", "ainda não comprou" in gabi.text)

    # ── Hugo: admin dá crédito, compra com saldo ──
    await adm.send("/credito 107|50"); check("/credito", await bal(107) == 50)
    await hugo.click("Explorar catálogo"); await hugo.click("Cursos"); await hugo.click("Curso Python"); await hugo.click("Comprar")
    # duplo clique simultâneo em pagar com saldo
    s = screen_of(hugo.id)
    await asyncio.gather(hugo.click("Saldo"), hugo.click("Saldo"))
    n = (await M.repo.one("SELECT COUNT(*) n FROM orders WHERE user_id=107 AND method='saldo'"))["n"]
    check("duplo clique em Saldo não cobra 2x", n == 1 and abs(await bal(107) - 20.10) < 0.01, f"pedidos={n} saldo={await bal(107)}")

    # ── Iris e João: disputam a última unidade do Pack Canva ──
    for u in (iris, joao):
        await adm.send(f"/credito {u.id}|30")
        await u.click("Explorar catálogo"); await u.click("Templates"); await u.click("Pack Canva"); await u.click("Comprar")
    await asyncio.gather(iris.click("Saldo"), joao.click("Saldo"))
    got = [u.name for u in (iris, joao) if "CANVA-ONLY-1" in last_text(u.id)]
    refunded = [u.name for u in (iris, joao) if "Estoque esgotado" in last_text(u.id)]
    check("última unidade: 1 recebe, outro é reembolsado", len(got) == 1 and len(refunded) == 1, f"{got} {refunded}")
    check("reembolso volta ao saldo", sorted([await bal(108), await bal(109)]) == [15, 30], [await bal(108), await bal(109)])
    await iris.send("/start"); await iris.click("Explorar catálogo"); await iris.click("Templates"); await iris.click("Pack Canva")
    check("esgotado: sem botão Comprar", iris.has("Esgotado") and not iris.has("Comprar"))

    # ── Corrida: 'Já paguei' + poller ao mesmo tempo não entrega 2x ──
    M.asaas.status = pending
    await joao.send("/start"); await joao.click("Explorar catálogo"); await joao.click("Cursos"); await joao.click("Curso Python")
    await joao.click("Comprar"); await joao.click("Pix"); await joao.send("52998224725")
    M.asaas.status = real
    o = await M.repo.one("SELECT * FROM orders WHERE user_id=109 AND method='pix' ORDER BY id DESC")
    await asyncio.gather(joao.click("Já paguei"), M.process(o), M.process(o))
    k = (await M.repo.one("SELECT COUNT(*) n FROM stock WHERE order_id=?", o["id"]))["n"]
    check("pagamento confirmado 3x simultâneo entrega 1 chave só", k == 1, f"chaves={k}")

    # ── Admin: vendas, preço, off/on, aviso ──
    await adm.send("/vendas"); check("/vendas", "Total" in last_text(ADMIN) and "Clientes  " in last_text(ADMIN))
    await adm.send("/preco 2|7,50"); p = await M.repo.one("SELECT price FROM products WHERE id=2"); check("/preco", p["price"] == 7.5)
    await adm.send("/off 2"); await eva.send("/start"); await eva.click("Explorar catálogo"); await eva.click("Cursos")
    check("/off esconde produto", not eva.has("Ebook"))
    await adm.send("/on 2")
    await adm.send("/aviso Promo de Black Friday!"); check("/aviso envia a todos", "Enviado para" in last_text(ADMIN) and "Black Friday" in last_text(102))
    await adm.send("/aviso"); check("/aviso sem texto não quebra", "Formato" in last_text(ADMIN) or "Use" in last_text(ADMIN), last_text(ADMIN))

    # ── Fornecedores (4 modos) e painel ──
    await painel_e_fornecedores(adm, ana)

    # ── Estabilidade ──
    check("nenhum erro nos logs", not errors, " | ".join(errors[:5]))
    stuck = await M.repo.q("SELECT code,status FROM orders WHERE status IN('paid','delivering')")
    check("nenhum pedido travado", not stuck, [tuple(r) for r in stuck])

    print("\n".join(results))
    print(f"\n{ok_n} OK · {fail_n} FALHAS")
    os._exit(1 if fail_n else 0)


# ───────────────────────── fornecedores + painel ─────────────────────────
import json as _json
import re as _re
import html as _html


async def fake_supplier():
    """Fornecedor de mentira: /ok entrega códigos, /fail sempre 500, /hook recebe webhook assinado."""
    from aiohttp import web
    calls = {"ok": [], "fail": 0, "hook": [], "buy": [], "rl": 0}
    BK = "tgb_chave_teste_123"
    BP = "/buyer/api/v2/telegram-buyer"

    async def b_products(req):
        if req.query.get("key") != BK:
            return web.json_response({"success": False, "message": "Invalid API key"}, status=401)
        return web.json_response({"success": True, "walletCurrency": "VND", "products": [
            {"productId": "P1", "name": "Gift Card Teste", "description": "Cartão de teste", "productType": "account",
             "price": {"amount": 50000, "currency": "VND", "text": "50.000 ₫"}, "availability": {"available": 60, "sold": 40},
             "promotions": [{"type": "bulk_discount", "minQty": 3, "percent": 10, "bonusQty": 1}]},
            {"productId": "P-EMAIL", "name": "Vaga com e-mail", "productType": "slot", "price": {"text": "10 ₫"},
             "availability": {"available": 5, "sold": 0}, "promotions": [], "purchaseRequirements": {"customerEmail": True}}]})

    async def b_balance(req):
        if req.query.get("key") != BK:
            return web.json_response({"success": False, "message": "Invalid API key"}, status=401)
        return web.json_response({"success": True, "walletCurrency": "VND", "balance": 250000, "balanceText": "250.000 ₫"})

    async def b_purchase(req):
        b = await req.json()
        calls["buy"].append({"idem": req.headers.get("Idempotency-Key"), **b})
        if b.get("key") != BK:
            return web.json_response({"success": False, "message": "Invalid API key"}, status=401)
        pid, q = b["product_id"], int(b.get("quantity", 1))
        if pid == "P-OOS":
            return web.json_response({"success": False, "message": "Inventory not enough"}, status=409)
        if pid == "P-NOBAL":
            return web.json_response({"success": False, "message": "Wallet balance is not enough"}, status=400)
        if pid == "P-RL":
            calls["rl"] += 1
            if calls["rl"] == 1:
                return web.json_response({"success": False, "code": "RATE_LIMITED"}, status=429, headers={"Retry-After": "0"})
        order = {"orderCode": "SUP" + b["product_id"], "status": "paid" if pid == "P-MAN" else "completed", "quantity": q}
        pay = {"amount": 50000 * q, "amountText": f"VND {50000 * q}", "balance": 1000, "balanceText": "1.000 ₫"}
        delivery = None if pid == "P-MAN" else {"accounts": [
            {"user": f"conta{i}@exemplo.com", "password": f"senha<{i}>", "verifyEmail": "rec@exemplo.com"} for i in range(q)]}
        return web.json_response({"success": True, "order": order, "payment": pay, "delivery": delivery})

    async def ping(req):
        return web.json_response({"ok": True})

    async def ok(req):
        body = await req.json()
        calls["ok"].append({"idem": req.headers.get("Idempotency-Key"), "auth": req.headers.get("Authorization"), **body})
        n = int(body["qty"])
        # devolve o header de auth na resposta para provar que o painel mascara segredos no log
        return web.json_response({"echo": req.headers.get("Authorization"),
                                  "data": {"codes": [f"API-{body['ref']}-{i}" for i in range(n)]}})

    async def fail(req):
        calls["fail"] += 1
        return web.json_response({"erro": "fora do ar"}, status=500)

    async def hook(req):
        calls["hook"].append({"sig": req.headers.get("X-Vexa-Signature"), "raw": await req.read()})
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes([web.get(BP + "/products", b_products), web.get(BP + "/balance", b_balance),
                    web.post(BP + "/purchase", b_purchase), web.post("/ok", ok), web.post("/fail", fail), web.post("/hook", hook), web.get("/ok", ping)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", calls, runner


def csrf_of(html):
    m = _re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


async def painel_e_fornecedores(adm, ana):
    import httpx
    from bot import fornecedores as forn
    from panel import app as P
    forn.RETRY_WAIT[:] = [0, 0, 0]
    os.environ["SUP_FORN1"] = "chave-super-secreta-123"
    base, calls, runner = await fake_supplier()
    P.ctx.update(repo=M.repo, bot=bot)

    async def mk(ip, port=5000, door=True):
        """Cliente do painel; por padrão já passou pela porta (link de uso único do /painel)."""
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=P.app, client=(ip, port)),
                              base_url="https://painel.test", follow_redirects=False)
        if door:
            await c.get(f"/entrar/{await M.repo.panel_link()}")
        return c

    # ── porta: sem o link do bot, o painel não existe para a internet ──
    X = await mk("203.0.113.66", door=False)
    codes = {pth: (await X.get(pth)).status_code for pth in ("/", "/login", "/produtos", "/static/app.css", "/senha")}
    check("porta: sem o link do bot tudo responde 404", all(v == 404 for v in codes.values()), codes)
    r = await X.post("/login", data={"username": "thiago", "password": "x"})
    check("porta: login nem aceita tentativa sem a porta", r.status_code == 404)
    check("porta: health segue público", (await X.get("/health")).status_code == 200)
    n_adm = len(tg.chats[ADMIN])
    await adm.send("/painel")
    btn = tg.chats[ADMIN][-1]["kb"].inline_keyboard[0][0].url if tg.chats[ADMIN][-1]["kb"] else ""
    check("/painel: admin recebe botão com link de uso único", "/entrar/" in btn and "http" not in tg.chats[ADMIN][-1]["text"])
    tok = btn.rsplit("/", 1)[-1]
    intruso = U(131, "Intruso2"); await intruso.send("/painel")
    check("/painel: não-admin não recebe link", not any("/entrar/" in str(m["kb"]) for m in tg.chats.get(131, [])))
    r = await X.get(f"/entrar/{tok}")
    check("porta: link abre e leva ao login", r.status_code == 303 and r.headers["location"] == "/login"
          and "httponly" in r.headers.get("set-cookie", "").lower())
    check("porta: depois do link, tela de login aparece", (await X.get("/login")).status_code == 200)
    X2 = await mk("203.0.113.67", door=False)
    check("porta: link já usado não abre de novo", (await X2.get(f"/entrar/{tok}")).status_code == 404)
    old = await M.repo.panel_link(minutes=-1)
    check("porta: link vencido não abre", (await X2.get(f"/entrar/{old}")).status_code == 404)
    check("porta: token inventado não abre", (await X2.get("/entrar/abc123")).status_code == 404)
    check("porta: aviso no Telegram quando o painel é liberado", any("Painel liberado" in m["text"] for m in tg.chats[ADMIN][n_adm:]))
    await X.aclose(); await X2.aclose()
    H = await mk("203.0.113.9", 5000)

    # ── 1º deploy: usuário thiago com senha temporária só no Telegram ──
    await P.bootstrap()
    pw_msg = next((m["text"] for m in reversed(tg.chats[ADMIN]) if "Senha temporária" in m["text"]), "")
    temp = _re.search(r"Senha temporária: <code>([^<]+)</code>", pw_msg)
    u0 = await M.repo.one("SELECT * FROM panel_users WHERE username='thiago'")
    check("painel: cria 'thiago' admin e manda senha no Telegram", temp and u0 and u0["role"] == "admin" and u0["must_change"] == 1)
    check("painel: senha guardada só como hash bcrypt", u0 and u0["pass_hash"].startswith("$2") and temp.group(1) not in u0["pass_hash"])
    temp = temp.group(1)
    await P.bootstrap()
    check("painel: bootstrap não recria usuário", (await M.repo.one("SELECT COUNT(*) n FROM panel_users"))["n"] == 1)

    r = await H.get("/")
    check("painel: sem login redireciona para /login", r.status_code == 303 and r.headers["location"] == "/login")

    # ── bloqueio após 5 erros ──
    for _ in range(5):
        r = await H.post("/login", data={"username": "thiago", "password": "errada"})
    check("login: 5 erros bloqueiam", "Aguarde 15 minutos" in r.text)
    r = await H.post("/login", data={"username": "thiago", "password": temp})
    check("login: bloqueado recusa até a senha certa", r.status_code == 200 and "Aguarde 15 minutos" in r.text)
    await M.repo.x("UPDATE login_fail SET locked_until=datetime('now','-1 minute')")  # 15 min depois
    n_adm = len(tg.chats[ADMIN])
    r = await H.post("/login", data={"username": "thiago", "password": temp})
    check("login: senha certa após o bloqueio entra", r.status_code == 303 and r.headers["location"] == "/senha")
    ck = r.headers.get("set-cookie", "").lower()
    check("login: cookie HttpOnly + Secure + SameSite", all(x in ck for x in ("httponly", "secure", "samesite=lax", "max-age=43200")), ck)
    await asyncio.sleep(0.05)
    check("login: aviso no Telegram com IP", any("Login no painel" in m["text"] and "203.0.113.9" in m["text"] for m in tg.chats[ADMIN][n_adm:]))
    r = await H.get("/produtos")
    check("1º acesso obriga troca de senha", r.status_code == 303 and r.headers["location"] == "/senha")
    page = (await H.get("/senha")).text
    r = await H.post("/senha", data={"current": temp, "new": "NovaSenha123", "repeat": "NovaSenha123"})
    check("CSRF: POST sem token é recusado", r.status_code == 403)
    r = await H.post("/senha", data={"csrf": csrf_of(page), "current": temp, "new": "fraca", "repeat": "fraca"})
    check("senha fraca recusada", "pelo menos 10" in r.text)
    r = await H.post("/senha", data={"csrf": csrf_of(page), "current": temp, "new": "NovaSenha123", "repeat": "NovaSenha123"})
    check("troca de senha ok", r.status_code == 303 and r.headers["location"].startswith("/?msg"))
    r = await H.get("/")
    check("início abre com KPIs e gráfico", r.status_code == 200 and "Vendas por dia" in r.text and "Saldo em carteira" in r.text)
    C = csrf_of(r.text)

    async def post(_path, **data):
        return await H.post(_path, data={"csrf": C, **data})

    # ── cadastro pelo painel ──
    await post("/categorias/salvar", name="Fornecedor", emoji="🚚", sort="5")
    cat = await M.repo.one("SELECT id FROM categories WHERE name='Fornecedor'")
    check("painel: cria categoria", cat is not None)
    hdr = _json.dumps({"Authorization": "Bearer {secret:FORN1}"})
    body = '{"sku": "{sku}", "qty": {qtd}, "ref": "{pedido}"}'
    for name, kind, url in (("API Boa", "api", base + "/ok"), ("API Fora", "api", base + "/fail"),
                            ("Hook", "webhook", base + "/hook"), ("Balcão", "manual", "")):
        await post("/fornecedores/salvar", name=name, kind=kind, url=url, method="POST", headers=hdr, body=body,
                   path="data.codes", timeout="5", active="1", link="https://fornecedor.exemplo")
    sups = {s["name"]: s for s in await M.repo.q("SELECT * FROM suppliers")}
    check("painel: cria 4 fornecedores", len(sups) == 4)
    check("webhook ganha segredo HMAC", len(forn.conf(sups["Hook"]).get("secret", "")) >= 32)
    r = await post("/fornecedores/salvar", name="Vaza", kind="api", url=base + "/ok", headers=_json.dumps({"Authorization": "Bearer " + "x" * 40}))
    check("painel: recusa chave colada no header", "err=" in r.headers.get("location", ""))
    r = await post(f"/fornecedores/{sups['API Boa']['id']}/testar")
    check("botão Testar conexão", "msg=" in r.headers.get("location", ""), r.headers.get("location"))

    async def produto(name, mode, sup=None, price="10", cost="4"):
        await post("/produtos/salvar", name=name, category_id=str(cat["id"]), price=price, cost=cost, warranty_days="7",
                   max_qty="5", active="1", delivery_mode=mode, supplier_id=str(sups[sup]["id"]) if sup else "",
                   sku=f"SKU-{mode}", ref_link="https://ref.exemplo/item", description="Teste")
        return await M.repo.one("SELECT * FROM products WHERE name=?", name)

    p_api = await produto("Gift API", "api", "API Boa")
    p_fail = await produto("Gift Quebrado", "api", "API Fora")
    p_hook = await produto("Gift Hook", "webhook", "Hook")
    p_man = await produto("Gift Manual", "manual", "Balcão")
    p_est = await produto("Gift Estoque", "estoque", price="8", cost="3")
    check("painel: cria produtos nos 4 modos", all([p_api, p_fail, p_hook, p_man, p_est]) and p_api["cost"] == 4)
    await post(f"/produtos/{p_est['id']}/estoque", items="EST-1\nEST-2\n\nEST-3")
    check("painel: repõe estoque colando linhas", await M.repo.stock(p_est["id"]) == 3)
    r = await post("/produtos/salvar", name="Sem forn", category_id=str(cat["id"]), price="5", delivery_mode="api")
    check("painel: API sem fornecedor é recusado", "err=" in r.headers.get("location", ""))

    cli = U(120, "Kaio")
    await cli.send("/start")
    await M.repo.credit(120, 200, "ajuste: teste")

    async def comprar(nome, qty=1):
        await cli.click("Início") if cli.has("Início") else await cli.send("/start")
        await cli.click("Explorar catálogo"); await cli.click("Fornecedor"); await cli.click(nome)
        for _ in range(qty - 1):
            await cli.click("+")
        await cli.click("Comprar"); await cli.click("Saldo")
        return await M.repo.one("SELECT * FROM orders WHERE user_id=120 ORDER BY id DESC")

    # estoque
    o = await comprar("Gift Estoque")
    check("modo estoque entrega", o["status"] == "delivered" and "EST-1" in last_text(120) and abs(o["cost"] - 3) < .01)
    # api
    o = await comprar("Gift API", 2)
    check("modo API entrega 2 códigos", o["status"] == "delivered" and last_text(120).count(f"API-{o['code']}-") == 2, last_text(120)[:200])
    check("API: idempotência pelo código do pedido", calls["ok"][-1]["idem"] == o["code"] and calls["ok"][-1]["ref"] == o["code"])
    check("API: secret do ENV no header", calls["ok"][-1]["auth"] == "Bearer chave-super-secreta-123")
    check("API: log do fornecedor sem segredo", "HTTP 200" in o["supplier_log"] and "chave-super-secreta" not in o["supplier_log"], o["supplier_log"])
    check("API: custo, fornecedor e tempo gravados", abs(o["cost"] - 8) < .01 and o["supplier_id"] == sups["API Boa"]["id"] and o["delivery_ms"] is not None)
    # falha
    b0 = await bal(120)
    n_adm = len(tg.chats[ADMIN])
    o = await comprar("Gift Quebrado")
    check("API fora do ar: 3 tentativas", calls["fail"] == 3, calls["fail"])
    check("falha → estorno automático em saldo", o["status"] == "refunded" and abs(await bal(120) - b0) < .01, (o["status"], await bal(120), b0))
    check("falha → cliente avisado", "voltaram para a sua carteira" in last_text(120))
    check("falha → admin avisado", any("Fornecedor falhou" in m["text"] for m in tg.chats[ADMIN][n_adm:]))
    check("estorno registrado no extrato", await M.repo.one("SELECT 1 FROM ledger WHERE ref=? AND reason='estorno'", o["code"]) is not None)
    # webhook
    o = await comprar("Gift Hook")
    check("webhook: pedido aguardando fornecedor", o["status"] == "awaiting" and "sendo preparado" in last_text(120), last_text(120)[:120])
    h = calls["hook"][-1]
    secret = forn.conf(sups["Hook"])["secret"]
    check("webhook: POST assinado com HMAC", forn.check_sig(secret, h["raw"], h["sig"]) and _json.loads(h["raw"])["pedido"] == o["code"])
    ret = _json.dumps({"pedido": o["code"], "itens": ["HOOK-OK-1"]}).encode()
    Xw = await mk("203.0.113.70", door=False)
    r = await Xw.post(f"/webhook/{sups['Hook']['id']}", content=ret, headers={"X-Vexa-Signature": "sha256=errada"})
    await Xw.aclose()
    check("webhook: retorno com assinatura errada é recusado", r.status_code == 401)
    r = await H.post(f"/webhook/{sups['Hook']['id']}", content=ret, headers={"X-Vexa-Signature": forn.sign(secret, ret)})
    check("webhook: retorno assinado aceito", r.status_code == 200 and r.json()["status"] == "ready", r.text)
    await M.tick()
    o = await M.repo.one("SELECT * FROM orders WHERE id=?", o["id"])
    check("webhook: bot entrega ao cliente", o["status"] == "delivered" and "HOOK-OK-1" in last_text(120))
    r = await H.post(f"/webhook/{sups['Hook']['id']}", content=ret, headers={"X-Vexa-Signature": forn.sign(secret, ret)})
    check("webhook: retorno repetido é idempotente", r.status_code == 200 and (await M.repo.one(
        "SELECT COUNT(*) n FROM stock WHERE order_id=?", o["id"]))["n"] == 1)
    # webhook sem resposta → estorno
    o = await comprar("Gift Hook")
    await M.repo.x("UPDATE orders SET sent_at=datetime('now','-2 hours') WHERE id=?", o["id"])
    await M.tick()
    check("webhook sem resposta no prazo → estorno", (await M.repo.one("SELECT status FROM orders WHERE id=?", o["id"]))["status"] == "refunded")
    # manual
    o = await comprar("Gift Manual")
    check("manual: vai para a fila e avisa o cliente", o["status"] == "manual" and "Pagamento aprovado. Seu pedido está sendo preparado" in last_text(120))
    r = await H.get("/fila")
    check("fila manual lista o pedido com link de referência", o["code"] in r.text and "ref.exemplo" in r.text)
    r = await post(f"/pedidos/{o['id']}/entregar", items="", back="/fila")
    check("fila: entrega vazia recusada", "err=" in r.headers.get("location", ""))
    r = await post(f"/pedidos/{o['id']}/entregar", items="MANUAL-XYZ", back="/fila")
    await M.tick()
    o = await M.repo.one("SELECT * FROM orders WHERE id=?", o["id"])
    check("fila: entregar envia ao cliente na tela de entrega", o["status"] == "delivered" and "MANUAL-XYZ" in last_text(120)
          and "Pagamento aprovado" in last_text(120))
    # reenviar e estornar
    n = len(tg.chats[120])
    await post(f"/pedidos/{o['id']}/reenviar"); await M.tick()
    check("pedido: reenviar entrega", len(tg.chats[120]) > n and "MANUAL-XYZ" in last_text(120))
    b0 = await bal(120)
    r = await post(f"/pedidos/{o['id']}/estornar", motivo="")
    check("estorno exige motivo", "err=" in r.headers.get("location", ""))
    await post(f"/pedidos/{o['id']}/estornar", motivo="cliente desistiu")
    check("pedido: estorno em saldo", abs(await bal(120) - b0 - 10) < .01 and "Estorno do pedido" in last_text(120))
    r = await post("/clientes/120/saldo", valor="5", motivo="")
    check("ajuste de saldo exige motivo", "err=" in r.headers.get("location", ""))
    await post("/clientes/120/saldo", valor="-5,50", motivo="correção")
    check("ajuste de saldo aplicado", abs(await bal(120) - b0 - 4.5) < .01)
    acts = {a["action"] for a in await M.repo.q("SELECT action FROM audit")}
    check("auditoria registra preço/estoque/estorno/saldo", {"criou produto", "repôs estoque", "estornou em saldo", "ajustou saldo"} <= acts, acts)


    # ── API de revenda (padrão Buyer API) ──
    os.environ["SUP_BUY1"] = "tgb_chave_teste_123"
    await post("/fornecedores/salvar", name="Revenda", kind="buyer", url=base + "/buyer", key_secret="buy1",
               prefix="/api/v2/telegram-buyer", min_balance="5000", timeout="5", active="1")
    sb = await M.repo.one("SELECT * FROM suppliers WHERE name='Revenda'")
    check("buyer: fornecedor salvo com chave por nome (sem a chave no banco)",
          sb and forn.conf(sb)["key_secret"] == "BUY1" and "tgb_" not in sb["config"])
    r = await post(f"/fornecedores/{sb['id']}/testar")
    check("buyer: testar conexão mostra o saldo", "250.000" in _html.unescape(__import__("urllib.parse").parse.unquote(r.headers.get("location", ""))))
    r = await H.get(f"/fornecedores/{sb['id']}/produtos")
    check("buyer: catálogo do fornecedor no painel", r.status_code == 200 and "Gift Card Teste" in r.text and "250.000" in r.text
          and "exige dados do cliente" in r.text and "Vender na loja" in r.text)
    r = await H.get(f"/produtos/novo?fornecedor={sb['id']}&sku=P1&nome=Gift%20Card%20Teste")
    check("buyer: 'Vender na loja' abre o produto já preenchido", 'value="P1"' in r.text and "Gift Card Teste" in r.text
          and "mode:'api'" in r.text)
    async def comprar_id(pid, qty=1):
        await cli.click("x", data=f"buy:{pid}:{qty}"); await cli.click("Saldo")
        return await M.repo.one("SELECT * FROM orders WHERE user_id=120 ORDER BY id DESC")

    pb = {}
    for sku in ("P1", "P-OOS", "P-RL", "P-MAN", "P-NOBAL"):
        await post("/produtos/salvar", name=f"Rev {sku}", category_id=str(cat["id"]), price="20", cost="9", warranty_days="7",
                   max_qty="5", active="1", delivery_mode="api", supplier_id=str(sb["id"]), sku=sku)
        pb[sku] = await M.repo.one("SELECT * FROM products WHERE name=?", f"Rev {sku}")
    n_adm = len(tg.chats[ADMIN])
    o = await comprar_id(pb["P1"]["id"], 2)
    check("buyer: compra entrega login e senha formatados", o["status"] == "delivered" and last_text(120).count("login: conta") == 2
          and "senha: senha&lt;1&gt;" in last_text(120), last_text(120)[:300])
    check("buyer: Idempotency-Key pelo código do pedido", calls["buy"][-1]["idem"] == f"vexa-{o['code']}" and calls["buy"][-1]["quantity"] == 2)
    check("buyer: log do pedido sem senha nem chave", "senha" not in o["supplier_log"] and "tgb_" not in o["supplier_log"]
          and "SUPP1" in o["supplier_log"], o["supplier_log"])
    check("buyer: avisa saldo baixo no fornecedor", any("Saldo baixo no fornecedor" in m["text"] for m in tg.chats[ADMIN][n_adm:]))
    nb = len(calls["buy"]); b0 = await bal(120)
    o = await comprar_id(pb["P-OOS"]["id"])
    check("buyer: sem estoque lá → estorno sem ficar repetindo", o["status"] == "refunded" and len(calls["buy"]) - nb == 1
          and abs(await bal(120) - b0) < .01)
    o = await comprar_id(pb["P-RL"]["id"])
    check("buyer: respeita limite (429) e conclui", o["status"] == "delivered" and calls["rl"] == 2)
    n_adm = len(tg.chats[ADMIN])
    o = await comprar_id(pb["P-MAN"]["id"])
    check("buyer: fornecedor com conclusão manual → fila manual, sem estorno", o["status"] == "manual"
          and any("vai concluir" in m["text"] for m in tg.chats[ADMIN][n_adm:]))
    n_adm = len(tg.chats[ADMIN])
    o = await comprar_id(pb["P-NOBAL"]["id"])
    check("buyer: sem saldo lá → estorno e aviso", o["status"] == "refunded"
          and any("saldo insuficiente no fornecedor" in m["text"] for m in tg.chats[ADMIN][n_adm:]))
    os.environ["SUP_BUY1"] = "errada"
    r = await post(f"/fornecedores/{sb['id']}/testar")
    check("buyer: chave errada é detectada no teste", "err=" in r.headers.get("location", ""))
    os.environ["SUP_BUY1"] = "tgb_chave_teste_123"

    # ── chamado respondido pelo painel, cliente responde no bot ──
    t = await M.repo.one("SELECT * FROM tickets WHERE user_id=100 ORDER BY id DESC")
    await post(f"/chamados/{t['id']}/responder", text="Chave trocada, confira!")
    check("chamado: resposta chega como Suporte VEXA", "Suporte VEXA" in last_text(100) and "Chave trocada" in last_text(100))
    await ana.click("Responder"); await ana.send("Funcionou, valeu!")
    msgs = await M.repo.q("SELECT sender, text FROM ticket_messages WHERE ticket_id=? ORDER BY id", t["id"])
    check("chamado: resposta do cliente entra na mesma conversa", [m["sender"] for m in msgs][-2:] == ["suporte", "cliente"]
          and (await M.repo.one("SELECT status FROM tickets WHERE id=?", t["id"]))["status"] == "aberto")
    r = await H.get(f"/chamados/{t['id']}")
    check("chamado: thread no painel", "Funcionou, valeu!" in r.text and "Chave trocada" in r.text)
    check("chamado: mostra entregas do cliente (chaves)", "PY-KEY-" in r.text and "garantia até" in r.text)
    check("chamado: mostra histórico do cliente no bot", "Viu produto Curso Python" in r.text and "Pagamento confirmado" in r.text
          and "Entregue: Curso Python" in r.text and "Acionou garantia" in r.text)
    check("chamado: mostra saldo e carteira", "Carteira" in r.text and "comissao" in r.text)
    ev = [e["detail"] for e in await M.repo.q("SELECT detail FROM events")]
    check("histórico: CPF nunca é gravado", not any("52998224725" in d or "529.982" in d for d in ev)
          and any("CPF (oculto)" in d for d in ev))
    check("histórico: erros do cliente registrados", any("Cupom recusado" in d for d in ev) and any("estornados em saldo" in d for d in ev))
    check("histórico: ações do suporte registradas", any("Resposta do suporte" in d for d in ev) and any("Estorno de" in d for d in ev))
    r = await H.get("/logs?tipo=erro")
    check("logs: filtro de erros", r.status_code == 200 and "Cupom recusado" in r.text)
    r = await H.get("/logs?q=Kaio")
    check("logs: busca por cliente", "Gift API" in r.text)

    # ── configurações e cupom com validade ──
    await post("/config", store_name="VEXA SHOPPING", support_username="vexasuporte", ref_pct="5", order_timeout_min="30",
               pix_on="1", manual_eta="até 1 hora", webhook_timeout_min="30", txt_home="Texto novo do painel",
               txt_support="Fale conosco", txt_manual="Pagamento aprovado. Seu pedido está sendo preparado e chega aqui em breve.")
    await M.tick(); await cli.send("/start")
    check("config: texto editado aparece no bot", "Texto novo do painel" in cli.text)
    await cli.click("Explorar catálogo"); await cli.click("Fornecedor"); await cli.click("Gift Estoque"); await cli.click("Comprar")
    check("config: USDT desligado some do checkout", not cli.has("USDT"))
    await post("/cupons/salvar", code="VENCIDO", pct="10", uses="", expires_at="2020-01-01")
    await cli.click("Tenho cupom"); await cli.send("VENCIDO")
    check("cupom vencido recusado", "Cupom inválido" in last_text(120))

    # ── avisos com filtro ──
    r = await post("/avisos/previa", text="Novidade!", filtro="produto", produto=str(p_api["id"]))
    check("avisos: prévia conta só quem comprou o produto", "<b>1</b> cliente(s)" in r.text, _re.findall(r"<b>\d+</b> cliente", r.text))


    # ── Segurança: callback_data forjado por cliente malicioso ──
    mal = U(130, "Malicioso")
    await mal.send("/start")
    await M.repo.credit(130, 50, "ajuste: teste")
    b0 = await bal(130)
    await mal.click("x", data=f"buy:{p_est['id']}:-5")
    await mal.click("Saldo") if mal.has("Saldo") else None
    check("segurança: quantidade negativa não gera saldo", await bal(130) <= b0, (await bal(130), b0))
    await mal.click("x", data=f"buy:{p_est['id']}:999")
    d = await M.repo.one("SELECT COUNT(*) n FROM orders WHERE user_id=130 AND qty>5")
    check("segurança: quantidade acima do máximo é limitada", d["n"] == 0 and ("×999" not in mal.text), mal.text[:120])
    await M.repo.x("UPDATE products SET active=0 WHERE id=?", p_api["id"])
    await mal.click("x", data=f"buy:{p_api['id']}:1")
    check("segurança: produto inativo não abre checkout", "Resumo do pedido" not in mal.text or "Gift API" not in mal.text, mal.text[:80])
    await mal.click("x", data=f"p:{p_api['id']}:1")
    check("segurança: produto inativo não abre card", not mal.has("Comprar"))
    await M.repo.x("UPDATE products SET active=1 WHERE id=?", p_api["id"])
    await mal.click("x", data="tp:0.01")
    check("segurança: recarga com valor forjado recusada", "0,01" not in mal.text)
    await mal.click("x", data="p:abc:1"); await mal.click("x", data="buy:1"); await mal.click("x", data="od:VX-NAO-EXISTE")
    check("segurança: callback inválido não quebra o bot", True)
    # HTML injetado por cliente não quebra mensagem do admin
    n_adm = len(tg.chats[ADMIN])
    await mal.click("x", data="tk:VX-DE-OUTRO")
    check("segurança: garantia de pedido alheio recusada", "não encontrado" in (mal.alert or ""))
    await mal.click("x", data=f"buy:{p_est['id']}:1"); await mal.click("Saldo")
    mcode = (await M.repo.one("SELECT code FROM orders WHERE user_id=130 ORDER BY id DESC"))["code"]
    await mal.click("x", data=f"tk:{mcode}"); await mal.send("<b>quebra <a href='http://phish'>clique</a>")
    t_last = next((m["text"] for m in tg.chats[ADMIN][n_adm:] if "Chamado novo" in m["text"]), "")
    check("segurança: HTML do cliente é escapado para o admin", "&lt;b&gt;" in t_last and "<a href" not in t_last, t_last[:160])


    # ── Segurança do painel ──
    r = await H.get("/login")
    hd = {k.lower(): v for k, v in r.headers.items()}
    check("painel: cabeçalhos de segurança (HSTS, CSP, frame)", "max-age" in hd.get("strict-transport-security", "")
          and "frame-ancestors 'none'" in hd.get("content-security-policy", "") and hd.get("x-frame-options") == "DENY")
    check("painel: nenhum script de CDN externo", "unpkg.com" not in (await H.get("/")).text and "jsdelivr" not in (await H.get("/")).text)
    H3 = await mk("192.0.2.50", 5000)
    for i in range(5):
        await H3.post("/login", data={"username": f"x{i}", "password": "y"})
    # o atacante manda X-Forwarded-For falso; o Caddy acrescenta o IP real no fim
    r = await H3.post("/login", data={"username": "z", "password": "y"}, headers={"X-Forwarded-For": "6.6.6.6, 192.0.2.50"})
    check("painel: X-Forwarded-For forjado não burla bloqueio por IP", "Aguarde 15 minutos" in r.text)
    check("painel: bloqueio avisa no Telegram", any("bloqueio por tentativas" in m["text"] for m in tg.chats[ADMIN]))
    await H3.aclose()
    forn.ALLOW_PRIVATE = False
    r = await post("/fornecedores/salvar", name="Interno", kind="api", url="http://127.0.0.1:2019/load", headers="{}")
    check("painel: bloqueia URL interna de fornecedor (SSRF)", "err=" in r.headers.get("location", ""))
    r = await post("/fornecedores/salvar", name="Interno2", kind="webhook", url="http://169.254.169.254/latest", headers="{}")
    check("painel: bloqueia IP de metadados (SSRF)", "err=" in r.headers.get("location", ""))
    forn.ALLOW_PRIVATE = True


    real = M.asaas.status
    async def pending(_): return "PENDING"
    # ── Auditoria: rajada paralela, dono não trancado, estorno de comissão, cupom com Pix pendente, entrega grande ──
    H5 = await mk("192.0.2.77", 5000)
    n0 = (await M.repo.one("SELECT COUNT(*) n FROM audit WHERE action='login falhou' AND ip='192.0.2.77'"))["n"]
    await asyncio.gather(*[H5.post("/login", data={"username": "thiago", "password": f"chute{i}"}) for i in range(30)])
    n1 = (await M.repo.one("SELECT COUNT(*) n FROM audit WHERE action='login falhou' AND ip='192.0.2.77'"))["n"]
    check("login: rajada de 30 tentativas paralelas testa no máximo 5 senhas", n1 - n0 <= 5, n1 - n0)
    await H5.aclose()
    # 20 erros de IPs diferentes trancariam o usuário... mas não do IP onde o dono já entrou
    for i in range(25):
        Hx = await mk(f"198.18.0.{i}", 5000)
        await Hx.post("/login", data={"username": "thiago", "password": "errada"}); await Hx.aclose()
    Hd = await mk("203.0.113.9", 5000)
    r = await Hd.post("/login", data={"username": "thiago", "password": "NovaSenha123"})
    check("login: ataque distribuído não tranca o dono no IP conhecido", r.status_code == 303, r.status_code)
    Hn = await mk("198.18.9.9", 5000)
    r = await Hn.post("/login", data={"username": "thiago", "password": "NovaSenha123"})
    check("login: IP novo segue bloqueado durante o ataque", r.status_code == 200 and "Aguarde" in r.text)
    await Hd.aclose(); await Hn.aclose()
    await M.repo.x("DELETE FROM login_fail")
    # estorno desfaz comissão
    ref, buyer = U(140, "Indicador"), U(141, "Indicado")
    await ref.send("/start"); await buyer.send("/start r140")
    await M.repo.credit(141, 50, "ajuste: teste")
    await buyer.click("x", data=f"buy:{p_est['id']}:1") if await M.repo.stock(p_est["id"]) else None
    await M.repo.db.executemany("INSERT INTO stock(product_id,content) VALUES(?,?)", [(p_est["id"], "EST-R")])
    await M.repo.db.commit()
    await buyer.click("x", data=f"buy:{p_est['id']}:1"); await buyer.click("Saldo")
    ob = await M.repo.one("SELECT * FROM orders WHERE user_id=141 AND status='delivered' ORDER BY id DESC")
    c0 = await bal(140)
    await post(f"/pedidos/{ob['id']}/estornar", motivo="teste comissão")
    check("estorno desfaz a comissão do indicador", c0 > 0 and abs(await bal(140) - (c0 - round(ob['amount'] * 0.05, 2))) < .01,
          (c0, await bal(140)))
    # cupom de 1 uso não vale para vários Pix pendentes
    await post("/cupons/salvar", code="UMAVEZ", pct="50", uses="1", expires_at="")
    cp = U(142, "Cupom"); await cp.send("/start")
    await M.repo.x("UPDATE users SET cpf='52998224725' WHERE id=142")
    M.asaas.status = pending
    codes = []
    for _ in range(3):
        await cp.click("x", data=f"buy:{p_api['id']}:1"); await cp.click("Tenho cupom"); await cp.send("UMAVEZ")
        if cp.has("Pix"):
            await cp.click("Pix")
    M.asaas.status = real
    n = (await M.repo.one("SELECT COUNT(*) n FROM orders WHERE user_id=142 AND coupon='UMAVEZ' AND status='pending'"))["n"]
    check("cupom de 1 uso: só 1 pedido pendente com desconto", n == 1, n)
    o1 = await M.repo.one("SELECT * FROM orders WHERE user_id=142 AND coupon='UMAVEZ' AND status='pending'")
    await M.repo.x("UPDATE orders SET created_at=datetime('now','-2 hours') WHERE id=?", o1["id"])
    M.asaas.status = pending
    await M.process(await M.repo.one("SELECT * FROM orders WHERE id=?", o1["id"]))
    M.asaas.status = real
    check("cupom: pedido expirado devolve o uso", (await M.repo.one("SELECT uses_left FROM coupons WHERE code='UMAVEZ'"))["uses_left"] == 1)
    # entrega grande (10 códigos longos com caracteres HTML) chega inteira
    big = U(143, "Grande"); await big.send("/start")
    await M.repo.credit(143, 500, "ajuste: teste")
    await M.repo.x("UPDATE products SET max_qty=10 WHERE id=?", p_est["id"])
    await M.repo.db.executemany("INSERT INTO stock(product_id,content) VALUES(?,?)",
                                [(p_est["id"], f"LOGIN{i}<&>" + "x" * 120) for i in range(10)])
    await M.repo.db.commit()
    await big.click("x", data=f"buy:{p_est['id']}:10"); await big.click("Saldo")
    allmsg = "\n".join(m["text"] for m in tg.chats[143])
    ob = await M.repo.one("SELECT id FROM orders WHERE user_id=143 ORDER BY id DESC")
    got = [r["content"] for r in await M.repo.q("SELECT content FROM stock WHERE order_id=?", ob["id"])]
    check("entrega grande: 10 itens chegam inteiros, escapados e em mensagens válidas",
          len(got) == 10 and all(_html.escape(g, quote=False) in allmsg for g in got) and "&lt;&amp;&gt;" in allmsg
          and all(len(m["text"]) <= 4096 for m in tg.chats[143]))

    # ── todas as telas abrem ──
    o1 = await M.repo.one("SELECT id FROM orders ORDER BY id DESC")
    pages = ["/", "/produtos", f"/produtos/{p_api['id']}", "/produtos/novo", "/fornecedores", f"/fornecedores/{sups['Hook']['id']}",
             "/fornecedores/novo", "/pedidos", "/pedidos?status=delivered&metodo=saldo&cliente=Kaio", f"/pedidos/{o1['id']}",
             "/fila", "/clientes", "/clientes/120", "/financeiro", "/financeiro?conferir=1", "/financeiro/csv",
             "/financeiro/csv?tipo=extrato", "/chamados", "/chamados?status=aberto", "/cupons", "/avisos", "/config",
             "/usuarios", "/auditoria", "/senha", "/logs"]
    bad = []
    for pth in pages:
        r = await H.get(pth)
        if r.status_code != 200:
            bad.append(f"{pth}={r.status_code}")
    check("painel: todas as telas abrem (26)", not bad, bad)
    r = await H.get("/financeiro")
    check("financeiro: lucro e margem", "Lucro" in r.text and "Margem" in r.text and "Gift API" in r.text)

    # ── papéis: atendente só chamados e pedidos ──
    r = await post("/usuarios/salvar", username="ana.suporte", role="atendente")
    pw2 = _re.search(r'<code style="font-size:14px">([^<]+)</code>', r.text)
    check("usuários: cria atendente com senha temporária", pw2 is not None)
    H2 = await mk("198.51.100.7", 5000)
    await H2.post("/login", data={"username": "ana.suporte", "password": _html.unescape(pw2.group(1))})
    c2 = csrf_of((await H2.get("/senha")).text)
    await H2.post("/senha", data={"csrf": c2, "current": _html.unescape(pw2.group(1)), "new": "Atende1234X", "repeat": "Atende1234X"})
    H4 = await mk("198.51.100.8", 5000)
    await H4.post("/login", data={"username": "ana.suporte", "password": "Atende1234X"})
    c4 = csrf_of((await H4.get("/senha")).text)
    await H4.post("/senha", data={"csrf": c4, "current": "Atende1234X", "new": "Atende5678Y", "repeat": "Atende5678Y"})
    check("troca de senha derruba as outras sessões", (await H2.get("/chamados")).status_code == 303
          and (await H4.get("/chamados")).status_code == 200)
    H2 = H4
    codes = {p: (await H2.get(p)).status_code for p in ("/chamados", "/pedidos", "/fila", "/financeiro", "/clientes", "/config", "/usuarios")}
    check("atendente: acessa chamados/pedidos/fila", all(codes[p] == 200 for p in ("/chamados", "/pedidos", "/fila")), codes)
    check("atendente: bloqueado no resto", all(codes[p] == 403 for p in ("/financeiro", "/clientes", "/config", "/usuarios")), codes)
    r = await H2.post(f"/pedidos/{o1['id']}/estornar", data={"csrf": c2, "motivo": "x"})
    check("atendente: não estorna", r.status_code == 403)
    r = await H2.get(f"/chamados/{t['id']}")
    check("atendente: vê contexto completo no chamado", r.status_code == 200 and "PY-KEY-" in r.text and "O que o cliente fez" in r.text)
    check("atendente: sem acesso a logs gerais", (await H2.get("/logs")).status_code == 403)
    # sessão expirada
    await M.repo.x("UPDATE panel_sessions SET expires_at=datetime('now','-1 minute')")
    r = await H.get("/produtos")
    check("sessão expira (12 h)", r.status_code == 303 and r.headers["location"] == "/login")
    await H.aclose(); await H2.aclose(); await runner.cleanup()


async def safe():
    try:
        await run()
    except SystemExit as e:
        os._exit(e.code)
    except BaseException as e:
        import traceback; traceback.print_exc()
        print("\n".join(results)); print(f"\nINTERROMPIDO: {e!r}\n{ok_n} OK · {fail_n} FALHAS")
        os._exit(2)


if __name__ == "__main__":
    asyncio.run(safe())
