# VEXA SHOPPING — instruções para o Claude Code

Loja automática de produtos digitais no Telegram (@vexashopping_bot). Slogan: **"comprou, chegou."**
Pagamento: **Pix (Asaas)**, **USDT (Binance Pay Merchant)** e **saldo em carteira**. Entrega automática.

## Dono e forma de trabalho
- Dono: Thiago (Telegram ID `1323196351`, já é ADMIN). Não quer rodar comandos: **execute tudo você mesmo**,
  commit e push em `main`. Respostas diretas e curtas, em português.
- Nunca commitar `.env`, `data/` nem chaves. Segredos só em GitHub Secrets.

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
- `python -m tests.sim` — simulação ponta a ponta (admin + 10 clientes, Telegram falso, todos os botões,
  Pix/USDT/saldo/recarga/cupom/garantia, corridas de duplo clique e última unidade). Roda no CI antes do deploy;
  rode sempre após mudar o bot.

## Funcionalidades prontas (testadas ponta a ponta no Telegram em 24/11/2026, em SANDBOX)
Catálogo por categoria com paginação · card de produto (preço antigo riscado, estoque,
quantidade −/+) · checkout com cupom · Pix/USDT/saldo · entrega editando a tela · recarga de saldo ·
minha conta (saldo, compras, comissões; sem níveis — removidos a pedido) · pedidos com reabertura · indique e ganhe (5% em saldo,
link `?start=r<ID>`) · garantia/ticket → admin · aviso de venda ao admin e ao canal · painel admin só por comandos (sem botão no menu; "mais vendidos" removido a pedido)
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

## Pendências — atualizado 24/11/2026 22h
Bot **no ar** na VPS (deploy verde, /start, botões e /admin testados no Telegram). Descrição do BotFather já fala em Pix.
Chave Asaas atual: `vexa-bot-2` (a `vexa-bot` vazou num log público, foi desabilitada). `.env` escapa `$` no deploy.
1. Cadastrar produtos reais (Thiago informa nome, preço, garantia, descrição e o que é entregue).
2. Repo **público** temporariamente (Actions sem orçamento). Voltar para **privado** ao terminar —
   privado sem orçamento/cartão bloqueia o deploy automático.
3. Thiago trocar a senha root da VPS e atualizar o secret `VPS_PASSWORD` (você não digita senhas).
4. Binance Pay só com conta **Merchant**; sem chave, botão USDT fica oculto.
5. Opcional: webhook Asaas, entrega de arquivos (file_id), canal @vexashopping, notificação de chamados por e-mail/painel.
