# ◆ VEXA SHOPPING — comprou, chegou.

Loja automática no Telegram: catálogo em botões, pagamento **Binance Pay**, confirmação e **entrega automática**, carteira com saldo, garantia/tickets e painel admin por comandos.

<img src="assets/wordmark-light.png" width="480">

## Funções
- 🛍 Catálogo com preço e estoque ao vivo (❌ quando esgota, ∞ para produtos digitais ilimitados)
- 💳 Checkout Binance Pay (USDT) → poller confirma a cada 15 s → entrega a chave/link na hora
- 👛 Carteira: recarga via Binance e compra com saldo; estorno automático em saldo se o estoque acabar
- 🛡 Garantia + `/ticket` → chega para o admin, que responde com `/responder`
- 📢 Aviso de venda no canal e broadcast `/aviso`
- 🔧 Admin: `/admin` lista todos os comandos

## Subir
1. @BotFather → `/newbot` → token. @userinfobot → seu ID.
2. Binance: conta **Merchant** (merchant.binance.com) → API Key/Secret.
3. `cp .env.example .env` e preencha. Teste com `SANDBOX=1`.
4. `docker compose up -d --build` (ou `pip install -r requirements.txt && python -m bot.main`).
5. Deploy contínuo: secrets do GitHub `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`.

## Primeiro produto
```
/cat E-books|📚
/add 1|Guia Energia Solar 2026|9.90|7|PDF completo + planilhas
/infinito 1|https://seu-link-de-download
```
Produto com chaves únicas:
```
/estoque 2
CHAVE-AAAA-1111
CHAVE-BBBB-2222
```
