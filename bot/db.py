import asyncio
import os
import aiosqlite
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, name TEXT, cpf TEXT, balance REAL DEFAULT 0,
  spent REAL DEFAULT 0, ref_by INTEGER, ref_earned REAL DEFAULT 0, banner_msg INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, emoji TEXT DEFAULT '•', sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER, name TEXT, description TEXT DEFAULT '',
  price REAL, old_price REAL, warranty_days INTEGER DEFAULT 0, image TEXT, unlimited TEXT,
  max_qty INTEGER DEFAULT 10, active INTEGER DEFAULT 1, sold INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS stock(
  id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, content TEXT, sold INTEGER DEFAULT 0, order_id INTEGER);
CREATE TABLE IF NOT EXISTS coupons(
  code TEXT PRIMARY KEY, pct REAL, uses_left INTEGER DEFAULT -1, active INTEGER DEFAULT 1, expires_at TEXT);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, user_id INTEGER, kind TEXT, product_id INTEGER,
  qty INTEGER DEFAULT 1, amount REAL, discount REAL DEFAULT 0, coupon TEXT, method TEXT, usdt REAL,
  ext_id TEXT, status TEXT DEFAULT 'pending', msg_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, paid_at TEXT);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, order_code TEXT, text TEXT,
  status TEXT DEFAULT 'aberto', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS media(key TEXT PRIMARY KEY, file_id TEXT);
CREATE TABLE IF NOT EXISTS suppliers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, kind TEXT DEFAULT 'manual', config TEXT DEFAULT '{}',
  link TEXT DEFAULT '', contact TEXT DEFAULT '', notes TEXT DEFAULT '', active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, delta REAL, reason TEXT, ref TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ticket_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, sender TEXT, staff TEXT, text TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS panel_users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, pass_hash TEXT, role TEXT DEFAULT 'atendente',
  must_change INTEGER DEFAULT 1, active INTEGER DEFAULT 1, last_login TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS panel_sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER, csrf TEXT, ip TEXT, expires_at TEXT);
CREATE TABLE IF NOT EXISTS login_fail(key TEXT PRIMARY KEY, count INTEGER DEFAULT 0, locked_until TEXT);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, who TEXT, action TEXT, entity TEXT, entity_id TEXT, detail TEXT, ip TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""

MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN ref_earned REAL DEFAULT 0",
    "ALTER TABLE products ADD COLUMN cost REAL DEFAULT 0",
    "ALTER TABLE products ADD COLUMN delivery_mode TEXT DEFAULT 'estoque'",
    "ALTER TABLE products ADD COLUMN supplier_id INTEGER",
    "ALTER TABLE products ADD COLUMN sku TEXT DEFAULT ''",
    "ALTER TABLE products ADD COLUMN ref_link TEXT DEFAULT ''",
    "ALTER TABLE products ADD COLUMN notes TEXT DEFAULT ''",
    "ALTER TABLE products ADD COLUMN sort INTEGER DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN cost REAL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN supplier_id INTEGER",
    "ALTER TABLE orders ADD COLUMN supplier_log TEXT DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN sent_at TEXT",
    "ALTER TABLE orders ADD COLUMN delivered_at TEXT",
    "ALTER TABLE orders ADD COLUMN delivery_ms INTEGER",
    "ALTER TABLE orders ADD COLUMN resend INTEGER DEFAULT 0",
    "ALTER TABLE coupons ADD COLUMN expires_at TEXT",
    "ALTER TABLE tickets ADD COLUMN updated_at TEXT",
    "UPDATE tickets SET status='aberto' WHERE status='open'",
    "CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status)",
    "CREATE INDEX IF NOT EXISTS ix_orders_user ON orders(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_stock_order ON stock(order_id)",
    "CREATE INDEX IF NOT EXISTS ix_stock_prod ON stock(product_id, sold)",
    "CREATE INDEX IF NOT EXISTS ix_ledger_user ON ledger(user_id)",
]


async def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH, timeout=15)
    db.row_factory = aiosqlite.Row
    # WAL: bot e painel (processos diferentes) leem/escrevem no mesmo arquivo
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=15000")
    await db.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            await db.execute(sql)
        except Exception:
            pass  # coluna já existe
    await db.commit()
    return db


class Repo:
    def __init__(self, db):
        self.db = db
        self.lock = asyncio.Lock()

    async def q(self, sql, *a):
        return await (await self.db.execute(sql, a)).fetchall()

    async def one(self, sql, *a):
        return await (await self.db.execute(sql, a)).fetchone()

    async def x(self, sql, *a):
        cur = await self.db.execute(sql, a)
        await self.db.commit()
        return cur.lastrowid

    async def run(self, sql, *a):
        """Executa e devolve quantas linhas mudaram (para operações atômicas)."""
        cur = await self.db.execute(sql, a)
        await self.db.commit()
        return cur.rowcount

    async def credit(self, uid, delta, reason, ref=""):
        """Movimenta saldo e registra no extrato (ledger)."""
        delta = round(float(delta), 2)
        await self.db.execute("UPDATE users SET balance=round(balance+?,2) WHERE id=?", (delta, uid))
        await self.x("INSERT INTO ledger(user_id,delta,reason,ref) VALUES(?,?,?,?)", uid, delta, reason, ref)

    async def debit(self, uid, amount, reason, ref=""):
        """Débito atômico: só debita se houver saldo. Devolve True/False."""
        amount = round(float(amount), 2)
        if not await self.run("UPDATE users SET balance=round(balance-?,2) WHERE id=? AND balance>=?", amount, uid, amount):
            return False
        await self.x("INSERT INTO ledger(user_id,delta,reason,ref) VALUES(?,?,?,?)", uid, -amount, reason, ref)
        return True

    async def setting(self, key, default=None):
        r = await self.one("SELECT value FROM settings WHERE key=?", key)
        return r["value"] if r else default

    async def set_setting(self, key, value):
        await self.x("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", key, value)

    async def stock(self, pid):
        p = await self.one("SELECT unlimited, delivery_mode FROM products WHERE id=?", pid)
        if p and (p["unlimited"] or (p["delivery_mode"] or "estoque") != "estoque"):
            return None  # ilimitado ou entregue pelo fornecedor/manual
        return (await self.one("SELECT COUNT(*) c FROM stock WHERE product_id=? AND sold=0", pid))["c"]

    async def take_stock(self, pid, qty, order_id):
        """Reserva atômica: devolve os itens do pedido (idempotente) ou None se faltar estoque."""
        p = await self.one("SELECT unlimited FROM products WHERE id=?", pid)
        if p["unlimited"]:
            return [p["unlimited"]] * qty
        async with self.lock:
            have = await self.q("SELECT content FROM stock WHERE order_id=?", order_id)
            if len(have) >= qty:
                return [r["content"] for r in have]
            for _ in range(qty - len(have)):
                if not await self.run("UPDATE stock SET sold=1, order_id=? WHERE id=(SELECT id FROM stock "
                                      "WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1)", order_id, pid):
                    await self.run("UPDATE stock SET sold=0, order_id=NULL WHERE order_id=?", order_id)
                    return None
            return [r["content"] for r in await self.q("SELECT content FROM stock WHERE order_id=? ORDER BY id", order_id)]
