"""Identidade e componentes visuais da VEXA."""
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K

LINE = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"


def brl(v): return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def usd(v): return f"{v:,.2f} USDT"


def kb(*rows): return K(inline_keyboard=[list(r) for r in rows if r])


def b(text, data=None, url=None): return B(text=text, url=url) if url else B(text=text, callback_data=data)


def header(title, sub=None):
    return f"<b>{title}</b>" + (f"\n<code>{sub}</code>" if sub else "") + "\n"


def stock_badge(c):
    if c is None:
        return "∞ ilimitado"
    if c == 0:
        return "⛔ esgotado"
    if c <= 5:
        return f"🔥 últimas {c}"
    return f"🟢 {c} em estoque"


def home_kb(is_admin=False):
    return kb(
        [b("Explorar catálogo  →", "cat")],
        [b("Mais vendidos", "top"), b("Meus pedidos", "orders")],
        [b("Minha conta", "me"), b("Indique e ganhe", "ref")],
        [b("Suporte", "sup")] + ([b("Admin", "adm")] if is_admin else []),
    )


def back(to="home", label="‹ Voltar"): return [b(label, to)]
