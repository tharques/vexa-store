"""Configurações editáveis pelo painel (tabela settings), com padrão vindo do .env."""
from .config import BINANCE_API_KEY, ORDER_TIMEOUT_MIN, REF_BONUS_PCT, SANDBOX, STORE_NAME, SUPPORT_USERNAME

DEFAULTS = {
    "store_name": STORE_NAME,
    "support_username": SUPPORT_USERNAME,
    "ref_pct": str(REF_BONUS_PCT),
    "order_timeout_min": str(ORDER_TIMEOUT_MIN),
    "pix_on": "1",
    "usdt_on": "1" if (BINANCE_API_KEY or SANDBOX) else "0",
    "manual_eta": "até 2 horas",
    "webhook_timeout_min": "30",
    "txt_home": "Pix ou USDT · entrega automática em segundos",
    "txt_support": "Problema com um produto? Abra a garantia pelo pedido — repomos ou devolvemos em saldo.\n\n"
                   "🕘 Atendimento humano todos os dias.",
    "txt_manual": "Pagamento aprovado. Seu pedido está sendo preparado e chega aqui em breve.",
}

LABELS = {
    "store_name": "Nome da loja",
    "support_username": "@ do suporte (sem @)",
    "ref_pct": "% de comissão de indicação",
    "order_timeout_min": "Expiração do pedido (min)",
    "pix_on": "Pix ligado",
    "usdt_on": "USDT ligado",
    "manual_eta": "Prazo da entrega manual",
    "webhook_timeout_min": "Tempo máx. de resposta do webhook (min)",
    "txt_home": "Texto da tela inicial",
    "txt_support": "Texto da tela de suporte",
    "txt_manual": "Mensagem de pedido em preparo (manual/webhook)",
}

CFG = dict(DEFAULTS)


async def load(repo):
    CFG.clear()
    CFG.update(DEFAULTS)
    for r in await repo.q("SELECT key, value FROM settings"):
        if r["key"] in DEFAULTS:
            CFG[r["key"]] = r["value"]
    return CFG


def cfg(key):
    return CFG.get(key, DEFAULTS.get(key, ""))


def num(key):
    try:
        return float(str(cfg(key)).replace(",", "."))
    except ValueError:
        return float(DEFAULTS[key])


def on(key):
    return str(cfg(key)) == "1"
