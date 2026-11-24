# VEXA SHOPPING — instruções para o Claude Code

Loja automática de produtos digitais no Telegram (@vexashopping_bot). Slogan: **"comprou, chegou."**
Pagamento: **Pix (Asaas)**, **USDT (Binance Pay Merchant)** e **saldo em carteira**. Entrega automática.

## Dono e forma de trabalho
- Dono: Thiago (Telegram ID `1323196351`, já é ADMIN). Não quer rodar comandos: **execute tudo você mesmo**,
  commit e push em `main`. Respostas diretas e curtas, em português.
- Nunca commitar `.env`, `data/` nem chaves. Segredos só em GitHub Secrets.
- Só produtos digitais próprios/licenciados. **Não** implementar venda de contas compartilhadas,
  credenciais de terceiros ou créditos de API de terceiros (o print de referência "NIMO SHOP" fazia isso).

## Stack e arquivos
- Python 3.12, aiogram 3.13, SQLite (aiosqlite). Entrada: `python -m bot.main`. Docker + compose.
- `bot/main.py` — handlers. Padrão **tela única**: `screen()` edita a mesma mensagem (banner + legenda +
  teclado inline). Banners em `assets/banners/*.png`; file_id cacheado na tabela `media`.
- `bot/ui.py` — identidade: `header()`, `LINE`, `brl()`, `usd()`, `bar()`, níveis, menu inicial.
- `bot/asaas.py` — Pix: cliente (CPF validado, pedido só na 1ª compra) → cobrança PIX → QR + copia-e-cola → status.
- `bot/binance_pay.py` — USDT: cotação USDTBRL da Binance (+`USDT_SPREAD`), criar/consultar pedido (HMAC-SHA512).
- `bot/db.py` — schema (users, categories, products, stock, coupons, orders, tickets, media) e reserva atômica de estoque.
- Poller a cada `POLL_SECONDS` confirma pagamentos e entrega; recupera pedidos `paid` após queda.
- `SANDBOX=1` simula pagamento aprovado (testes). Produção: `SANDBOX=0`.

## Funcionalidades prontas (testadas ponta a ponta no Telegram em 24/11/2026, em SANDBOX)
Catálogo por categoria com paginação · mais vendidos · card de produto (preço antigo riscado, estoque,
quantidade −/+) · checkout com cupom · Pix/USDT/saldo · entrega editando a tela · recarga de saldo ·
minha conta com níveis (Bronze/Prata/Ouro/Black) · pedidos com reabertura · indique e ganhe (5% em saldo,
link `?start=r<ID>`) · garantia/ticket → admin · aviso de venda ao admin e ao canal · painel admin por comandos
(`/admin`, `/cat`, `/add`, `/infinito`, `/estoque`, `/img`, `/preco`, `/on`/`/off`, `/lista`, `/cupom`,
`/vendas`, `/credito`, `/responder`, `/aviso`).
Bot no BotFather: nome, descrição, descrição curta, comandos e foto (logo) já configurados.

## Identidade visual
Logo = "V" em duas lâminas brancas sobre degradê `#635BFF → #8A5CFF → #FF6BB5`; fundo navy `#0A2540`;
fonte Inter. Arquivos: `assets/logo.png` (avatar), `icon.png`, `wordmark-light/dark.png`, `banners/`.
Tom: limpo, estilo Stripe. Nada de "◆" ou linhas longas; títulos em negrito + código do pedido em `<code>`.

## Deploy (VPS Contabo 161.97.143.189, root) — via GitHub Actions
`.github/workflows/deploy.yml`: a cada push em `main`, entra por SSH na VPS, instala Docker se faltar,
clona em `/opt/vexa`, grava `.env` a partir do secret e roda `docker compose up -d --build`.
Secrets necessários no repositório (o Thiago cadastra; você não digita senhas/tokens em sites):
- `VPS_HOST` = 161.97.143.189 · `VPS_PASSWORD` = senha root (ou `VPS_SSH_KEY`) · `ENV_FILE` = conteúdo do .env
A sandbox do Claude Code não alcança a VPS por SSH direto; use o workflow (`gh workflow run deploy`) e
acompanhe com `gh run watch`. A VPS também roda o bot "Gestor" (OpenClaw) — não mexer nele.

## Pendências (em ordem)
1. Thiago gera a chave Asaas `vexa-bot` (SMS) e cadastra os 3 secrets → disparar deploy com `SANDBOX=1`,
   testar `/start` no Telegram, depois `SANDBOX=0`.
2. Binance Pay exige conta **Merchant** (verificação da empresa). Já implementado: sem `BINANCE_API_KEY`
   (e fora do SANDBOX) o botão USDT some e a loja opera só Pix + saldo.
3. Cadastrar produtos reais (hoje só há catálogo de exemplo local, não versionado).
4. Webhook Asaas/Binance no lugar do polling (opcional), entrega de arquivos (file_id), canal @vexashopping.
5. Recomendar ao Thiago trocar a senha root da VPS (foi exposta em chat).
