import asyncio
import os
import aiosqlite
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, name TEXT, cpf TEXT, balance REAL DEFAULT 0,
  spent REAL DEFAULT 0, ref_by INTEGER, ref_earned REAL DEFAULT 0, banner_msg INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, emoji TEXT DEFAULT '◆', sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER, name TEXT, description TEXT DEFAULT '',
  price REAL, old_price REAL, warranty_days INTEGER DEFAULT 0, image TEXT, unlimited TEXT,
  max_qty INTEGER DEFAULT 10, active INTEGER DEFAULT 1, sold INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS stock(
  id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, content TEXT, sold INTEGER DEFAULT 0, order_id INTEGER);
CREATE TABLE IF NOT EXISTS coupons(
  code TEXT PRIMARY KEY, pct REAL, uses_left INTEGER DEFAULT -1, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, user_id INTEGER, kind TEXT, product_id INTEGER,
  qty INTEGER DEFAULT 1, amount REAL, discount REAL DEFAULT 0, coupon TEXT, method TEXT, usdt REAL,
  ext_id TEXT, status TEXT DEFAULT 'pending', msg_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, paid_at TEXT);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, order_code TEXT, text TEXT,
  status TEXT DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS media(key TEXT PRIMARY KEY, file_id TEXT);
"""


async def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    try:
        await db.execute("ALTER TABLE users ADD COLUMN ref_earned REAL DEFAULT 0")
    except Exception:
        pass
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

    async def stock(self, pid):
        p = await self.one("SELECT unlimited FROM products WHERE id=?", pid)
        if p and p["unlimited"]:
            return None
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
