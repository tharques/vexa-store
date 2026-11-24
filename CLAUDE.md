# NEXO STORE — instruções para o Claude Code

Bot de vendas de produtos digitais no Telegram, pagamento Binance Pay (Merchant API), entrega automática.

## Stack
- Python 3.12, aiogram 3, SQLite (aiosqlite). Entrada: `python -m bot.main`.
- `bot/binance_pay.py` — assinatura HMAC-SHA512, criar pedido (v3) e consultar (v2).
- `bot/main.py` — handlers cliente/admin, poller que confirma pagamento e entrega.
- `bot/db.py` — schema e reserva atômica de estoque.

## Regras
- Dono (Thiago) não quer rodar comandos: execute tudo você mesmo, commit e push em `main`.
- Push em `main` dispara `.github/workflows/deploy.yml` (SSH na VPS + docker compose) se os secrets VPS_* existirem.
- Nunca commitar `.env` nem `data/`.
- Antes de push: `python -m compileall -q bot`; com `SANDBOX=1` o pagamento é simulado como pago.
- Só produtos digitais próprios/licenciados para revenda. Não implementar venda de contas compartilhadas,
  credenciais de terceiros ou créditos de API de terceiros.

## Roadmap sugerido
1. Webhook Binance Pay (`/binancepay/webhook`) com verificação de assinatura, em vez de só polling.
2. Cupons de desconto e programa de indicação (saldo por convite).
3. Entrega de arquivos (file_id do Telegram) além de texto.
4. Painel web admin simples.
