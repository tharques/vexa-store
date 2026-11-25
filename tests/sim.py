"""Simulação ponta a ponta: roda os handlers reais com um Telegram falso, 10 clientes + admin.
Uso: python -m tests.sim   (SANDBOX=1, banco temporário, sem rede do Telegram)"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime

os.environ.update(BOT_TOKEN="123:abc", SANDBOX="1", ADMIN_IDS="1323196351", CHANNEL_ID="@canal",
                  DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"), SUPPORT_USERNAME="vexasuporte")

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

    # ── Estabilidade ──
    check("nenhum erro nos logs", not errors, " | ".join(errors[:5]))
    stuck = await M.repo.q("SELECT code,status FROM orders WHERE status IN('paid','delivering')")
    check("nenhum pedido travado", not stuck, [tuple(r) for r in stuck])

    print("\n".join(results))
    print(f"\n{ok_n} OK · {fail_n} FALHAS")
    os._exit(1 if fail_n else 0)


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
