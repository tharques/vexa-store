# VEXA SHOPPING — instruções para o Claude Code

Loja automática de produtos digitais no Telegram (@vexashopping_bot). Slogan: **"comprou, chegou."**
Pagamento: **Pix (Asaas)**, **USDT (Binance Pay Merchant)** e **saldo em carteira**. Entrega automática.

## Dono e forma de trabalho
- Dono: Thiago (Telegram ID `1323196351`, já é ADMIN). Não quer rodar comandos: **execute tudo você mesmo**,
  commit e push em `main`. Respostas diretas e curtas, em português.
- Nunca commitar `.env`, `data/` nem chaves. Segredos só em GitHub Secrets.

## Stack e arquivos
- Python 3.12, aiogram 3.13, SQLite (aiosqlite, **WAL**: bot e painel usam o mesmo `data/store.db`). Docker + compose.
- Serviços no compose: `vexa` (bot, `python -m bot.main`), `painel` (FastAPI/uvicorn, porta 8000 só interna) e
  `caddy` (profile `https`, HTTPS automático para `vexa.traxor.pro`; DNS A já criado na Hostinger → 161.97.143.189).
- `bot/main.py` — handlers. Padrão **tela única**: `screen()` edita a mesma mensagem. `tick()` = 1 ciclo do poller
  (confirma pagamentos, entrega `paid`, conclui `ready` vindos do painel, reenvios, estorna webhook vencido).
- `bot/fornecedores.py` — camada de fornecedores. Modo de entrega por produto: **estoque** (reserva atômica),
  **api** (URL/método/headers/corpo com `{sku}{qtd}{pedido}`, `{secret:NOME}` lê `SUP_NOME` do .env, caminho JSON,
  3 tentativas, `Idempotency-Key` = código do pedido), **webhook** (POST com HMAC-SHA256 em `X-Vexa-Signature`;
  retorno em `POST /webhook/{id}` do painel) e **manual** (fila manual). Falhou → estorno automático em saldo + avisos.
  Log do fornecedor fica no pedido sem segredos (`redact`). Sem userbot e sem integração com Nimo (vetado pelo dono).
- `bot/store.py` — configurações editáveis no painel (tabela `settings`): nome, @suporte, % indicação, expiração,
  Pix/USDT on/off, prazo manual, textos do bot. O bot recarrega a cada ciclo.
- `bot/db.py` — schema + migrações. `repo.credit/debit` movimentam saldo **sempre** com registro em `ledger`
  (recarga, compra, comissao, estorno, ajuste: motivo).
- `panel/app.py` + `panel/auth.py` + `panel/templates/` — painel (Jinja2 + HTMX/Alpine, identidade VEXA).
  Login bcrypt, bloqueio 15 min após 5 erros (por usuário e IP), aviso de login no Telegram, cookie HttpOnly/Secure/
  SameSite=Lax 12 h, CSRF em todo POST, papéis admin/atendente (**nega por padrão**: rota sem papel = só admin),
  auditoria de toda alteração (tabela `audit`). 1º início cria `thiago` com senha temporária enviada só no Telegram.
  O painel não entrega direto: grava itens + status `ready`/`resend=1` e o bot envia na tela de entrega.
- `bot/asaas.py` (Pix + `received()` para conferência), `bot/binance_pay.py` (USDT), `bot/ui.py` (identidade).
- `python -m tests.sim` — simulação ponta a ponta (bot + painel + fornecedor falso local): 141 checagens. Roda no CI
  antes do deploy; rode sempre após mudar bot ou painel.

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
`.github/workflows/deploy.yml`: a cada push em `main`, roda a simulação, entra por SSH na VPS, clona em `/opt/vexa`,
grava `.env` a partir do secret e sobe o compose. **Antes do Caddy verifica 80/443**: se outro serviço estiver usando,
não sobe o Caddy (avisa no log) e mantém bot + painel. Secrets: `VPS_HOST`, `VPS_PASSWORD` (ou `VPS_SSH_KEY`), `ENV_FILE`.
ENV_FILE opcional: `PANEL_DOMAIN` (padrão vexa.traxor.pro) e `SUP_<NOME>=chave` para cada fornecedor API.
A VPS também roda o bot "Gestor" (OpenClaw) — não mexer em nada fora de /opt/vexa.

## Pendências — atualizado 24/11/2026 22h
Bot **no ar** na VPS (deploy verde, /start, botões e /admin testados no Telegram). Descrição do BotFather já fala em Pix.
Chave Asaas atual: `vexa-bot-2` (a `vexa-bot` vazou num log público, foi desabilitada). `.env` escapa `$` no deploy.
1. Cadastrar produtos reais (Thiago informa nome, preço, garantia, descrição e o que é entregue).
2. Repo **público** temporariamente (Actions sem orçamento). Voltar para **privado** ao terminar —
   privado sem orçamento/cartão bloqueia o deploy automático.
3. Thiago trocar a senha root da VPS e atualizar o secret `VPS_PASSWORD` (você não digita senhas).
4. Binance Pay só com conta **Merchant**; sem chave, botão USDT fica oculto.
5. Opcional: webhook Asaas, entrega de arquivos (file_id), canal @vexashopping, notificação de chamados por e-mail/painel.
