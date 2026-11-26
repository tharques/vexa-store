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
  Log do fornecedor fica no pedido sem segredos (`redact`).
  Tipo **buyer** ("API de revenda", padrão Buyer API): config = domínio + prefixo (`/api/v2/telegram-buyer`) + nome da
  chave (`SUP_<NOME>` no ENV_FILE). Painel lista catálogo/saldo do fornecedor e importa produto ("Vender na loja").
  Compra: POST /purchase com `Idempotency-Key: vexa-<código>`, entrega em `delivery` (accounts/codes…) formatada em
  linhas; 429 respeita Retry-After, 409/400/401/404 não repetem → estorno; status `paid` sem entrega → fila manual;
  aviso de saldo baixo no fornecedor (`min_balance`). Credenciais entregues nunca vão para o log.
- `bot/store.py` — configurações editáveis no painel (tabela `settings`): nome, @suporte, % indicação, expiração,
  Pix/USDT on/off, prazo manual, textos do bot. O bot recarrega a cada ciclo.
- `bot/db.py` — schema + migrações. `repo.credit/debit` movimentam saldo **sempre** com registro em `ledger`
  (recarga, compra, comissao, estorno, ajuste: motivo).
- `panel/app.py` + `panel/auth.py` + `panel/templates/` — painel (Jinja2 + HTMX/Alpine, identidade VEXA).
  Login bcrypt, bloqueio 15 min após 5 erros (por usuário e IP), aviso de login no Telegram, cookie HttpOnly/Secure/
  SameSite=Lax 12 h, CSRF em todo POST, papéis admin/atendente (**nega por padrão**: rota sem papel = só admin),
  auditoria de toda alteração (tabela `audit`). 1º início cria `thiago` com senha temporária enviada só no Telegram.
  O painel não entrega direto: grava itens + status `ready`/`resend=1` e o bot envia na tela de entrega.
- Histórico de suporte: tabela `events` (middleware `Activity` no bot grava cliques, mensagens — CPF nunca —,
  pedidos, pagamentos, entregas, erros; o painel grava ações do suporte). Aparece no chamado (com pedidos, entregas,
  garantia e carteira do cliente), na ficha do cliente, no pedido e em `/logs` (admin).
- **Painel invisível**: só o dono usa. Sem a "porta" tudo responde 404 (inclusive /login). A porta é um link de uso
  único (10 min) que o bot manda ao admin com `/painel` → `/entrar/<token>` grava cookie `vx_door` por 30 dias
  (tabelas `panel_links`/`panel_doors`); depois vem usuário + senha. Públicos só `/health` e `/webhook/*`.
- Segurança (auditada 26/11/2026): callback_data sempre validado (`ints`, `sellable`, qtd 1..máx, recarga só
  `TOPUP_VALUES`, pedido/garantia só do dono); texto externo escapado com `esc()` no HTML do Telegram; cupom reservado
  na criação do pedido e devolvido se não pagar; entrega longa em mensagens separadas. Painel: IP = último
  X-Forwarded-For (Caddy acrescenta), tentativa contada antes do bcrypt (atômico), bcrypt em thread e sempre executado,
  limites 5 por IP e usuário+IP e 20 por usuário (exceto IPs onde o usuário já entrou), aviso de bloqueio no Telegram,
  CSP/HSTS, JS local em `panel/static/vendor`, SSRF bloqueado em fornecedores, estorno desfaz comissão.
- `bot/asaas.py` (Pix + `received()` para conferência), `bot/binance_pay.py` (USDT), `bot/ui.py` (identidade).
- `python -m tests.sim` — simulação ponta a ponta (bot + painel + fornecedor falso local): 197 checagens (inclui ataques: callback forjado, rajada de login, XFF, SSRF, HTML). Roda no CI
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

## Pendências — atualizado 26/11/2026
Bot + painel no ar: https://vexa.traxor.pro (rota no Caddy do Gestor via API admin + persistida no Caddyfile do host).
1. Confirmar no log do deploy que `SANDBOX=0` (com 1, todo pagamento é aprovado sem pagar). O bot avisa no Telegram.
2. Thiago: mandar /painel no bot → botão "Abrir painel" → usuário `thiago` + senha temporária (Telegram) → trocar senha.
3. Cadastrar produtos reais pelo painel. Repo voltar para privado quando houver orçamento no Actions.
4. Trocar a senha root da VPS (ou usar chave SSH) e atualizar o secret correspondente.
5. Binance Pay só com conta Merchant; sem chave, USDT fica oculto.
