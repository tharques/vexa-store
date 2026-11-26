"""Login do painel: bcrypt, sessões de 12 h, CSRF, bloqueio após 5 erros, papéis e auditoria."""
import hashlib
import json
import secrets
import string
from datetime import datetime, timedelta

import bcrypt

SESSION_HOURS = 12
MAX_FAILS = 5
LOCK_MIN = 15
COOKIE = "vx_session"
ROLES = {"admin": "Administrador", "atendente": "Atendente"}


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(12)).decode()


def check_pw(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), (h or "").encode())
    except ValueError:
        return False


def strong_password(n=16):
    alpha = string.ascii_letters + string.digits + "!@#%*?-_"  # sem & < > (HTML do Telegram)
    while True:
        p = "".join(secrets.choice(alpha) for _ in range(n))
        if any(c.islower() for c in p) and any(c.isupper() for c in p) and any(c.isdigit() for c in p):
            return p


def weak_reason(pw: str):
    if len(pw) < 10:
        return "A senha precisa ter pelo menos 10 caracteres."
    if pw.lower() == pw or pw.upper() == pw or not any(c.isdigit() for c in pw):
        return "Use letras maiúsculas, minúsculas e números."
    return None


def _now():
    return datetime.utcnow()


def _iso(d):
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


# limite por chave: IP e usuário+IP bloqueiam em 5 erros; o usuário sozinho só em 20 (evita que um atacante
# bloqueie o dono de propósito com poucas tentativas, mas segura ataque distribuído de muitos IPs)
LIMITS = {"ip": MAX_FAILS, "uip": MAX_FAILS, "u": 20}
DUMMY_HASH = bcrypt.hashpw(b"vexa-dummy", bcrypt.gensalt(12)).decode()


def _kind(k):
    return k.split(":", 1)[0]


async def locked(repo, *keys):
    for k in keys:
        r = await repo.one("SELECT locked_until FROM login_fail WHERE key=?", k)
        if r and r["locked_until"] and r["locked_until"] > _iso(_now()):
            return True
    return False


async def attempt(repo, *keys):
    """Conta a tentativa ANTES de checar a senha, com incremento atômico, para rajadas paralelas não passarem do
    limite. Devolve (permitido, chaves_que_acabaram_de_bloquear). Janela de 15 min."""
    now = _iso(_now())
    window_start = _iso(_now() - timedelta(minutes=LOCK_MIN))
    allowed, newly = True, []
    for k in keys:
        # recomeça a contagem se o bloqueio já venceu ou se a janela de 15 min passou
        await repo.x("UPDATE login_fail SET count=0, first_at=?, locked_until=NULL WHERE key=? AND "
                     "((locked_until IS NOT NULL AND locked_until<?) OR (locked_until IS NULL AND "
                     "(first_at IS NULL OR first_at<?)))", now, k, now, window_start)
        await repo.x("INSERT INTO login_fail(key,count,first_at) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1",
                     k, now)
        cnt = (await repo.one("SELECT count FROM login_fail WHERE key=?", k))["count"]
        lim = LIMITS.get(_kind(k), MAX_FAILS)
        if cnt > lim:
            allowed = False
        if cnt >= lim:
            if await repo.run("UPDATE login_fail SET locked_until=? WHERE key=? AND (locked_until IS NULL OR locked_until<?)",
                              _iso(_now() + timedelta(minutes=LOCK_MIN)), k, now):
                newly.append(k)
    return allowed, newly


async def success(repo, *keys):
    """Senha certa: zera os contadores dessa origem (a tentativa atual não conta como erro)."""
    for k in keys:
        await repo.x("DELETE FROM login_fail WHERE key=?", k)


async def new_session(repo, user_id, ip):
    token = secrets.token_urlsafe(32)
    await repo.x("DELETE FROM panel_sessions WHERE expires_at<?", _iso(_now()))
    await repo.x("INSERT INTO panel_sessions(token_hash,user_id,csrf,ip,expires_at) VALUES(?,?,?,?,?)",
                 _h(token), user_id, secrets.token_urlsafe(24), ip, _iso(_now() + timedelta(hours=SESSION_HOURS)))
    return token


async def session_user(repo, token):
    if not token:
        return None
    r = await repo.one("SELECT s.csrf, s.expires_at, u.* FROM panel_sessions s JOIN panel_users u ON u.id=s.user_id "
                       "WHERE s.token_hash=? AND s.expires_at>? AND u.active=1", _h(token), _iso(_now()))
    return dict(r) if r else None


async def end_session(repo, token):
    if token:
        await repo.x("DELETE FROM panel_sessions WHERE token_hash=?", _h(token))


async def audit(repo, who, action, entity="", entity_id="", detail=None, ip=""):
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    await repo.x("INSERT INTO audit(who,action,entity,entity_id,detail,ip) VALUES(?,?,?,?,?,?)",
                 who, action, entity, str(entity_id or ""), (detail or "")[:4000], ip or "")
