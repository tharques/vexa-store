"""Identidade e componentes visuais da VEXA."""
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from .config import LEVELS

LINE = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"


def brl(v): return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def usd(v): return f"{v:,.2f} USDT"


def kb(*rows): return K(inline_keyboard=[list(r) for r in rows if r])


def b(text, data=None, url=None): return B(text=text, url=url) if url else B(text=text, callback_data=data)


def header(title, sub=None):
    return f"<b>{title}</b>" + (f"\n<code>{sub}</code>" if sub else "") + "\n"


def bar(frac, n=10):
    f = max(0, min(n, round(frac * n)))
    return "▰" * f + "▱" * (n - f)


def stock_badge(c):
    if c is None:
        return "∞ ilimitado"
    if c == 0:
        return "⛔ esgotado"
    if c <= 5:
        return f"🔥 últimas {c}"
    return f"🟢 {c} em estoque"


def level(spent):
    cur = LEVELS[0]
    nxt = None
    for i, lv in enumerate(LEVELS):
        if spent >= lv[0]:
            cur = lv
            nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    return cur, nxt


def home_kb(is_admin=False):
    return kb(
        [b("🛍  Catálogo", "cat"), b("🔥  Mais vendidos", "top")],
        [b("👤  Minha conta", "me"), b("📦  Pedidos", "orders")],
        [b("🤝  Indique e ganhe", "ref"), b("💬  Suporte", "sup")],
        [b("⚙️  Painel admin", "adm")] if is_admin else None,
    )


def back(to="home", label="‹ Voltar"): return [b(label, to)]
