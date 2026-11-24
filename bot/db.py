import os
import aiosqlite
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, balance REAL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, emoji TEXT DEFAULT '📦');
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER, name TEXT,
  description TEXT DEFAULT '', price REAL, warranty_days INTEGER DEFAULT 0,
  unlimited TEXT DEFAULT NULL, active INTEGER DEFAULT 1);
-- estoque: cada linha é 1 unidade entregável (chave, link, texto, file_id)
CREATE TABLE IF NOT EXISTS stock(
  id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, content TEXT,
  sold INTEGER DEFAULT 0, order_id INTEGER);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT,  -- 'buy' | 'topup'
  product_id INTEGER, qty INTEGER DEFAULT 1, amount REAL,
  status TEXT DEFAULT 'pending',  -- pending|paid|delivered|expired|failed
  merchant_trade_no TEXT UNIQUE, prepay_id TEXT, checkout_url TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, paid_at TEXT);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, order_id INTEGER,
  text TEXT, status TEXT DEFAULT 'open', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


async def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    return db


class Repo:
    def __init__(self, db):
        self.db = db

    async def q(self, sql, *a):
        cur = await self.db.execute(sql, a)
        return await cur.fetchall()

    async def one(self, sql, *a):
        cur = await self.db.execute(sql, a)
        return await cur.fetchone()

    async def x(self, sql, *a):
        cur = await self.db.execute(sql, a)
        await self.db.commit()
        return cur.lastrowid

    async def ensure_user(self, uid, username):
        await self.x("INSERT INTO users(id,username) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username",
                     uid, username)

    async def stock_count(self, pid):
        p = await self.one("SELECT unlimited FROM products WHERE id=?", pid)
        if p and p["unlimited"]:
            return None  # infinito
        r = await self.one("SELECT COUNT(*) c FROM stock WHERE product_id=? AND sold=0", pid)
        return r["c"]

    async def take_stock(self, pid, qty, order_id):
        """Reserva atômica de unidades. Retorna lista de conteúdos ou None se faltar."""
        p = await self.one("SELECT unlimited FROM products WHERE id=?", pid)
        if p["unlimited"]:
            return [p["unlimited"]] * qty
        await self.db.execute("BEGIN IMMEDIATE")
        rows = await (await self.db.execute(
            "SELECT id,content FROM stock WHERE product_id=? AND sold=0 LIMIT ?", (pid, qty))).fetchall()
        if len(rows) < qty:
            await self.db.rollback()
            return None
        await self.db.executemany("UPDATE stock SET sold=1, order_id=? WHERE id=?",
                                  [(order_id, r["id"]) for r in rows])
        await self.db.commit()
        return [r["content"] for r in rows]
