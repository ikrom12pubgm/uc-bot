"""UC Shop Mini App (bitta fayl): do'kon + hamyon + tekin UC (tap) + yechish + admin panel.
O'rnatish:  pip install fastapi uvicorn "aiogram>=3.4" httpx pydantic
Ishga tushirish: uvicorn bot:app --host 0.0.0.0 --port 8000   (Railway: --port $PORT)

Muhit o'zgaruvchilari:
  BOT_TOKEN, ADMIN_ID, WEBAPP_URL, BOT_USERNAME            (majburiy)
  CARD_NUMBER, CARD_HOLDER                                  (balans to'ldirish uchun karta)
  SUPPORT_USERNAME, CHANNEL_URL                             (ixtiyoriy)
  NICK_API_URL, NICK_API_KEY, ALLOW_UNVERIFIED              (PUBG nik tekshiruv)
  STAR_PRICE (1 Stars narxi, so'm), CASHBACK_PCT (foiz)
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LabeledPrice, Message,
                           PreCheckoutQuery, WebAppInfo)
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# ================= SOZLAMALAR =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
NICK_API_URL = os.getenv("NICK_API_URL", "")
NICK_API_KEY = os.getenv("NICK_API_KEY", "")
ALLOW_UNVERIFIED = os.getenv("ALLOW_UNVERIFIED", "0") == "1"
CARD_NUMBER = os.getenv("CARD_NUMBER", "")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
SUPPORT = os.getenv("SUPPORT_USERNAME", "").lstrip("@")
CHANNEL_URL = os.getenv("CHANNEL_URL", "")
STAR_PRICE = int(os.getenv("STAR_PRICE", "230"))      # 1 ta Star narxi (so'm), o'zingizga moslang
CASHBACK_PCT = float(os.getenv("CASHBACK_PCT", "0"))  # xariddan qaytadigan foiz

U = 1000                  # 1 UC = 1000 ichki birlik
TAP_REWARD = 200          # bir bosish = 0.2 UC
MAX_ENERGY = 100          # kuniga 100 bosish = 20 UC
REGEN_PER_SEC = MAX_ENERGY / 86400
MAX_TAPS_PER_REQ = 30
REF_REWARD = 2 * U        # har bir do'st = 2 UC
MIN_WITHDRAW = 120        # minimal yechish (UC)
MIN_REFS = 10             # yechish uchun kamida 10 ta do'st
MIN_TOPUP, MAX_TOPUP = 5000, 5_000_000
STAR_MIN = 50

# UC paketining Telegram Stars narxi (UC miqdori -> Stars). Faqat PUBG UC paketlari uchun.
STARS_UC = {60: 70, 120: 150, 180: 200, 240: 250, 360: 300}

# ================= KATALOG (narxlarni shu yerdan o'zgartiring) =================
CATALOG = {
    "pubg": {"name": "PUBG Mobile V3 (ID orqali)", "short": "PUBG Mobile V3", "emoji": "🔫", "tabs": {
        "UC": [("60 UC", 12000), ("120 UC", 25000), ("180 UC", 40000), ("240 UC", 52000),
               ("325 UC", 60000), ("360 UC", 72000), ("660 UC", 116000),
               ("1800 UC", 290000), ("3850 UC", 570000), ("8100 UC", 1125000)],
        "Prime": [("Prime (1 Month)", 12490), ("Prime (3 Months)", 37470), ("Prime (6 Months)", 74940),
                  ("Prime Plus (1 Month)", 124900), ("Prime (12 Months)", 149880), ("Elite Pass Plus", 359924)],
        "Growing Pack": [("First Purchase", 12490), ("Upgradable", 36661),
                         ("Weekly Mythic", 36661), ("Mythic Emblem", 61258)],
        "WOW Coins": [],
    }},
    "steam": {"name": "Steam", "short": "Steam", "emoji": "🎮", "tabs": {}},
    "cod": {"name": "Call of Duty Mobile", "short": "Call of Duty Mobile", "emoji": "🪖", "tabs": {}},
    "mlbb": {"name": "Mobile Legends", "short": "Mobile Legends", "emoji": "⚔️", "tabs": {}},
    "roblox": {"name": "Roblox", "short": "Roblox", "emoji": "🧱", "tabs": {}},
    "standoff2": {"name": "Standoff 2", "short": "Standoff 2", "emoji": "🔫", "tabs": {}},
}
PREMIUM = [("Premium 3 oy", 190000), ("Premium 6 oy", 250000), ("Premium 12 oy", 420000)]  # narxni tekshiring!

PRODUCTS = {}   # key -> {title, price, uc}
for gk, g in CATALOG.items():
    for tab, items in g["tabs"].items():
        for title, price in items:
            m = re.match(r"^(\d+) UC$", title)
            PRODUCTS[f"{gk}|{tab}|{title}"] = {
                "title": title, "price": price, "uc": int(m.group(1)) if m else 0,
                "stars": STARS_UC.get(int(m.group(1))) if (m and gk == "pubg" and tab == "UC") else None}


def catalog_public():
    return {gk: {"name": g["name"], "short": g["short"], "emoji": g["emoji"],
                 "tabs": {t: [{"k": f"{gk}|{t}|{n}", "t": n, "p": p, "s": PRODUCTS[f"{gk}|{t}|{n}"]["stars"]} for n, p in items]
                          for t, items in g["tabs"].items()}}
            for gk, g in CATALOG.items()}


# ================= BAZA =================
DB_PATH = os.getenv("DB_PATH", "bot.db")
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, balance INTEGER DEFAULT 0,
  energy REAL DEFAULT 100, energy_ts REAL DEFAULT 0,
  referrer INTEGER, refs INTEGER DEFAULT 0, taps INTEGER DEFAULT 0, created REAL,
  wallet INTEGER DEFAULT 0, spent INTEGER DEFAULT 0, cashback INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT,
  pubg_id TEXT, nick TEXT, uc INTEGER, price INTEGER, status TEXT, created REAL, note TEXT, pay TEXT, stars INTEGER, charge TEXT);
CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, amount INTEGER, uses INTEGER);
CREATE TABLE IF NOT EXISTS promo_used(user_id INTEGER, code TEXT, PRIMARY KEY(user_id, code));
""")
for _sql in ("ALTER TABLE users ADD COLUMN wallet INTEGER DEFAULT 0",
             "ALTER TABLE users ADD COLUMN spent INTEGER DEFAULT 0",
             "ALTER TABLE users ADD COLUMN cashback INTEGER DEFAULT 0",
             "ALTER TABLE orders ADD COLUMN note TEXT",
             "ALTER TABLE orders ADD COLUMN pay TEXT",
             "ALTER TABLE orders ADD COLUMN stars INTEGER",
             "ALTER TABLE orders ADD COLUMN charge TEXT"):
    try:
        db.execute(_sql)
    except sqlite3.OperationalError:
        pass
db.commit()

bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


def q(sql, args=(), one=False):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.fetchone() if one else cur.fetchall()


def x(sql, args=()):
    """Yozish; o'zgargan qatorlar sonini qaytaradi (atomik tekshiruvlar uchun)."""
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.rowcount


def ins(sql, args=()):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.lastrowid


def get_user(uid, name="", referrer=None):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if u:
        return u, False
    x("INSERT OR IGNORE INTO users(id,name,energy,energy_ts,referrer,created) VALUES(?,?,?,?,?,?)",
      (uid, name, MAX_ENERGY, time.time(), referrer, time.time()))
    if referrer and referrer != uid and q("SELECT 1 FROM users WHERE id=?", (referrer,), one=True):
        x("UPDATE users SET balance=balance+?, refs=refs+1 WHERE id=?", (REF_REWARD, referrer))
    return q("SELECT * FROM users WHERE id=?", (uid,), one=True), True


# ================= Telegram initData =================
def verify_init_data(init_data: str) -> dict:
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        got = data.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            raise ValueError
        if time.time() - int(data.get("auth_date", 0)) > 86400:
            raise HTTPException(401, "Sessiya eskirgan, qayta oching")
        return json.loads(data["user"])
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Auth xato")


def auth(init_data: str):
    tg = verify_init_data(init_data)
    u, _ = get_user(tg["id"], tg.get("first_name", ""))
    return u


# ================= PUBG nik =================
async def lookup_nick(pubg_id: str):
    """Rasmiy ochiq API yo'q. Ishonchli UC supplier API'sini ulang:
    NICK_API_URL?id=<ID> -> {"nickname": "..."}  (supplieringizga moslab o'zgartiring)."""
    if not NICK_API_URL or not pubg_id.isdigit() or not (5 <= len(pubg_id) <= 13):
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(NICK_API_URL, params={"id": pubg_id},
                            headers={"Authorization": f"Bearer {NICK_API_KEY}"})
            return r.json().get("nickname") or None
    except Exception:
        return None


def public(u):
    now = time.time()
    e = min(MAX_ENERGY, u["energy"] + (now - u["energy_ts"]) * REGEN_PER_SEC)
    cnt = q("SELECT COUNT(*) n FROM orders WHERE user_id=? AND kind IN ('buy','stars') AND status!='unpaid'", (u["id"],), one=True)["n"]
    return {"id": u["id"], "name": u["name"], "wallet": u["wallet"], "spent": u["spent"],
            "cashback": u["cashback"], "orders": cnt,
            "balance": u["balance"] / U, "energy": int(e), "max_energy": MAX_ENERGY, "refs": u["refs"],
            "ref_link": f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}",
            "min_withdraw": MIN_WITHDRAW, "min_refs": MIN_REFS,
            "tap_reward": TAP_REWARD / U, "regen": REGEN_PER_SEC,
            "is_admin": bool(ADMIN_ID) and u["id"] == ADMIN_ID,
            "allow_unverified": ALLOW_UNVERIFIED, "support": SUPPORT, "channel": CHANNEL_URL,
            "star_price": STAR_PRICE, "star_min": STAR_MIN,
            "premium": [{"t": n, "p": p} for n, p in PREMIUM],
            "min_topup": MIN_TOPUP, "catalog": catalog_public()}


# ================= API =================
class Base(BaseModel):
    pass


class TapReq(Base):
    count: int


class IdReq(Base):
    pubg_id: str


class UserReq(Base):
    username: str


class WithdrawReq(Base):
    pubg_id: str
    uc: int


class BuyReq(Base):
    pubg_id: str
    items: dict[str, int]


class StarsPayReq(Base):
    pubg_id: str
    items: dict[str, int]


class TgReq(Base):
    username: str
    stars: int = 0
    plan: str = ""


class TopupReq(Base):
    amount: int
    method: str = ""


class ReceiptReq(Base):
    order: int
    image: str


class PromoReq(Base):
    code: str


class DecideReq(Base):
    order: int
    action: str  # "ok" | "no"


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(dp.start_polling(bot)) if bot else None
    yield
    if task:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/api/me")
async def me(x_init_data: str = Header("")):
    return public(auth(x_init_data))


@app.post("/api/tap")
async def tap(req: TapReq, x_init_data: str = Header("")):
    u = auth(x_init_data)
    now = time.time()
    e = min(MAX_ENERGY, u["energy"] + (now - u["energy_ts"]) * REGEN_PER_SEC)
    n = max(0, min(req.count, MAX_TAPS_PER_REQ, int(e)))
    x("UPDATE users SET energy=?, energy_ts=?, balance=balance+?, taps=taps+? WHERE id=?",
      (e - n, now, n * TAP_REWARD, n, u["id"]))
    return public(q("SELECT * FROM users WHERE id=?", (u["id"],), one=True))


@app.post("/api/check_id")
async def check_id(req: IdReq, x_init_data: str = Header("")):
    auth(x_init_data)
    nick = await lookup_nick(req.pubg_id.strip())
    if not nick:
        return {"ok": False, "error": "Nik topilmadi yoki tekshiruv xizmati ulanmagan"}
    return {"ok": True, "nick": nick}


@app.post("/api/check_username")
async def check_username(req: UserReq, x_init_data: str = Header("")):
    auth(x_init_data)
    un = req.username.strip().lstrip("@")
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{4,31}$", un):
        return {"ok": False, "error": "Username noto'g'ri (kamida 5 belgi, lotin harflari)"}
    name = None
    if bot:
        try:  # Bot API faqat ba'zi akkauntlarni ko'rsata oladi
            ch = await bot.get_chat("@" + un)
            if ch.type == "private":
                name = ((ch.first_name or "") + " " + (ch.last_name or "")).strip() or None
        except Exception:
            pass
    return {"ok": True, "username": "@" + un, "name": name, "verified": bool(name)}


def admin_kb(oid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ok:{oid}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"no:{oid}")]])


KIND = {"buy": "XARID", "withdraw": "YECHISH", "topup": "TO'LDIRISH", "stars": "TELEGRAM"}


def order_text(o, u):
    t = (f"🆕 Buyurtma #{o['id']} ({KIND.get(o['kind'], o['kind'])})\n"
         f"Foydalanuvchi: {u['name']} (id {u['id']})\n")
    if o["kind"] in ("buy", "withdraw"):
        t += f"Player ID: {o['pubg_id']}\nNik: {o['nick'] or '❓ tasdiqlanmagan'}\n"
    if o["kind"] == "stars":
        t += f"Username: {o['pubg_id']}\n"
    if o["uc"]:
        t += f"Miqdor: {o['uc']}\n"
    if o["note"]:
        t += f"{o['note']}\n"
    if o["pay"] == "stars":
        t += f"To'lov: ⭐ {o['stars']} Stars (to'langan)\n"
    if o["price"]:
        t += f"Summa: {o['price']:,} so'm"
    return t


async def notify_admin(oid):
    if not (bot and ADMIN_ID):
        return
    o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    u = q("SELECT * FROM users WHERE id=?", (o["user_id"],), one=True)
    try:
        await bot.send_message(ADMIN_ID, order_text(o, u), reply_markup=admin_kb(oid))
    except Exception:
        pass


@app.post("/api/withdraw")
async def withdraw(req: WithdrawReq, x_init_data: str = Header("")):
    u = auth(x_init_data)
    if req.uc < MIN_WITHDRAW:
        raise HTTPException(400, f"Minimal yechish {MIN_WITHDRAW} UC")
    if u["refs"] < MIN_REFS:
        raise HTTPException(400, f"Yechish uchun kamida {MIN_REFS} ta do'st taklif qiling (hozir {u['refs']})")
    pid = req.pubg_id.strip()
    nick = await lookup_nick(pid)
    if not nick and not ALLOW_UNVERIFIED:
        raise HTTPException(400, "Player ID tasdiqlanmadi")
    if x("UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
         (req.uc * U, u["id"], req.uc * U)) != 1:
        raise HTTPException(400, "Balans yetarli emas")
    oid = ins("INSERT INTO orders(user_id,kind,pubg_id,nick,uc,status,created) VALUES(?,?,?,?,?,?,?)",
              (u["id"], "withdraw", pid, nick, req.uc, "pending", time.time()))
    await notify_admin(oid)
    return {"ok": True, "order": oid, "nick": nick}


@app.post("/api/buy")
async def buy(req: BuyReq, x_init_data: str = Header("")):
    u = auth(x_init_data)
    total, uc, lines = 0, 0, []
    for k, n in req.items.items():
        if n == 0:
            continue
        p = PRODUCTS.get(k)
        if not p or not (1 <= n <= 50):
            raise HTTPException(400, "Mahsulot xato")
        total += p["price"] * n
        uc += p["uc"] * n
        lines.append(f"{p['title']} x{n}")
    if not total:
        raise HTTPException(400, "Savat bo'sh")
    pid = req.pubg_id.strip()
    nick = await lookup_nick(pid)
    if not nick and not ALLOW_UNVERIFIED:
        raise HTTPException(400, "Player ID tasdiqlanmadi")
    if x("UPDATE users SET wallet=wallet-? WHERE id=? AND wallet>=?", (total, u["id"], total)) != 1:
        raise HTTPException(400, "Balans yetarli emas. Avval balansni to'ldiring")
    oid = ins("INSERT INTO orders(user_id,kind,pubg_id,nick,uc,price,status,created,note) VALUES(?,?,?,?,?,?,?,?,?)",
              (u["id"], "buy", pid, nick, uc, total, "pending", time.time(), ", ".join(lines)))
    await notify_admin(oid)
    return {"ok": True, "order": oid, "nick": nick}


@app.post("/api/stars_invoice")
async def stars_invoice(req: StarsPayReq, x_init_data: str = Header("")):
    """UC ni Telegram Stars bilan to'lash: buyurtma + Stars hisob-fakturasi (invoice) havolasi."""
    u = auth(x_init_data)
    if not bot:
        raise HTTPException(400, "Bot sozlanmagan")
    stars, uc, lines = 0, 0, []
    for k, n in req.items.items():
        if n == 0:
            continue
        p = PRODUCTS.get(k)
        if not p or not (1 <= n <= 50):
            raise HTTPException(400, "Mahsulot xato")
        if not p["stars"]:
            raise HTTPException(400, f"{p['title']} ni Stars bilan to'lab bo'lmaydi")
        stars += p["stars"] * n
        uc += p["uc"] * n
        lines.append(f"{p['title']} x{n}")
    if not stars:
        raise HTTPException(400, "Savat bo'sh")
    pid = req.pubg_id.strip()
    nick = await lookup_nick(pid)
    if not nick and not ALLOW_UNVERIFIED:
        raise HTTPException(400, "Player ID tasdiqlanmadi")
    x("DELETE FROM orders WHERE user_id=? AND pay='stars' AND status='unpaid'", (u["id"],))
    oid = ins("INSERT INTO orders(user_id,kind,pubg_id,nick,uc,price,status,created,note,pay,stars) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (u["id"], "buy", pid, nick, uc, 0, "unpaid", time.time(), ", ".join(lines), "stars", stars))
    try:
        link = await bot.create_invoice_link(
            title="UC xaridi", description=", ".join(lines)[:200], payload=f"o:{oid}",
            currency="XTR", prices=[LabeledPrice(label="UC", amount=stars)])
    except Exception:
        raise HTTPException(400, "Stars to'lovini ochib bo'lmadi, keyinroq urinib ko'ring")
    return {"ok": True, "order": oid, "link": link, "stars": stars}


@app.post("/api/tgbuy")
async def tgbuy(req: TgReq, x_init_data: str = Header("")):
    u = auth(x_init_data)
    un = req.username.strip().lstrip("@")
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]{4,31}$", un):
        raise HTTPException(400, "Username noto'g'ri")
    if req.plan:
        pl = dict(PREMIUM).get(req.plan)
        if not pl:
            raise HTTPException(400, "Tarif topilmadi")
        price, stars, note = pl, 0, req.plan
    else:
        if not (STAR_MIN <= req.stars <= 100000):
            raise HTTPException(400, f"Minimal {STAR_MIN} ta Stars")
        price, stars, note = req.stars * STAR_PRICE, req.stars, f"{req.stars} Stars"
    if x("UPDATE users SET wallet=wallet-? WHERE id=? AND wallet>=?", (price, u["id"], price)) != 1:
        raise HTTPException(400, "Balans yetarli emas. Avval balansni to'ldiring")
    oid = ins("INSERT INTO orders(user_id,kind,pubg_id,uc,price,status,created,note) VALUES(?,?,?,?,?,?,?,?)",
              (u["id"], "stars", "@" + un, stars, price, "pending", time.time(), note))
    await notify_admin(oid)
    return {"ok": True, "order": oid}


@app.post("/api/topup")
async def topup(req: TopupReq, x_init_data: str = Header("")):
    u = auth(x_init_data)
    if not CARD_NUMBER:
        raise HTTPException(400, "To'lov kartasi hali sozlanmagan, support bilan bog'laning")
    if not (MIN_TOPUP <= req.amount <= MAX_TOPUP):
        raise HTTPException(400, f"Summa {MIN_TOPUP:,} dan {MAX_TOPUP:,} gacha bo'lishi kerak")
    if q("SELECT COUNT(*) n FROM orders WHERE user_id=? AND kind='topup' AND status='pending'",
         (u["id"],), one=True)["n"] >= 5:
        raise HTTPException(400, "Kutilayotgan to'ldirishlar ko'p, avval ular tasdiqlansin")
    oid = ins("INSERT INTO orders(user_id,kind,price,status,created,note) VALUES(?,?,?,?,?,?)",
              (u["id"], "topup", req.amount, "pending", time.time(), req.method[:40]))
    return {"ok": True, "order": oid, "card": CARD_NUMBER, "holder": CARD_HOLDER, "amount": req.amount}


@app.post("/api/receipt")
async def receipt(req: ReceiptReq, x_init_data: str = Header("")):
    u = auth(x_init_data)
    o = q("SELECT * FROM orders WHERE id=? AND user_id=? AND kind='topup' AND status='pending'",
          (req.order, u["id"]), one=True)
    if not o:
        raise HTTPException(400, "Buyurtma topilmadi")
    try:
        raw = base64.b64decode(req.image.split(",")[-1])
    except Exception:
        raise HTTPException(400, "Rasm xato")
    if len(raw) > 5_000_000:
        raise HTTPException(400, "Rasm juda katta")
    if bot and ADMIN_ID:
        await bot.send_photo(ADMIN_ID, BufferedInputFile(raw, "chek.jpg"),
                             caption=order_text(o, u), reply_markup=admin_kb(o["id"]))
    return {"ok": True}


@app.post("/api/promo")
async def promo(req: PromoReq, x_init_data: str = Header("")):
    u = auth(x_init_data)
    code = req.code.strip().upper()
    p = q("SELECT * FROM promos WHERE code=?", (code,), one=True)
    if not p or p["uses"] <= 0:
        raise HTTPException(400, "Promo kod topilmadi yoki tugagan")
    if x("INSERT OR IGNORE INTO promo_used(user_id,code) VALUES(?,?)", (u["id"], code)) != 1:
        raise HTTPException(400, "Bu promo kodni allaqachon ishlatgansiz")
    if x("UPDATE promos SET uses=uses-1 WHERE code=? AND uses>0", (code,)) != 1:
        x("DELETE FROM promo_used WHERE user_id=? AND code=?", (u["id"], code))
        raise HTTPException(400, "Promo kod tugagan")
    x("UPDATE users SET wallet=wallet+? WHERE id=?", (p["amount"], u["id"]))
    return {"ok": True, "amount": p["amount"]}


@app.get("/api/orders")
async def orders(x_init_data: str = Header("")):
    u = auth(x_init_data)
    rows = q("SELECT id,kind,pubg_id,nick,uc,price,status,note,pay,stars FROM orders WHERE user_id=? "
             "ORDER BY id DESC LIMIT 40", (u["id"],))
    return [dict(r) for r in rows]


# ---------- Admin panel ----------
def need_admin(x_init_data):
    u = auth(x_init_data)
    if not ADMIN_ID or u["id"] != ADMIN_ID:
        raise HTTPException(403, "Faqat admin")
    return u


def admin_stats():
    c = lambda s: q(f"SELECT COUNT(*) n FROM orders WHERE {s}", one=True)["n"]
    return {"users": q("SELECT COUNT(*) n FROM users", one=True)["n"],
            "pending": c("status='pending'"), "done": c("status='done'"), "rejected": c("status='rejected'")}


async def apply_decision(oid, act):
    """Tasdiqlash (ok) / rad etish (no). Bot va admin panel bir xil ishlatadi."""
    o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
    if not o:
        return False
    new = "done" if act == "ok" else "rejected"
    if x("UPDATE orders SET status=? WHERE id=? AND status='pending'", (new, oid)) != 1:
        return False
    uid, kind, by_stars = o["user_id"], o["kind"], o["pay"] == "stars"
    if act == "ok":
        if kind == "topup":
            x("UPDATE users SET wallet=wallet+? WHERE id=?", (o["price"], uid))
            msg = f"✅ Balansingiz {o['price']:,} so'mga to'ldirildi"
        elif kind == "withdraw":
            msg = f"✅ {o['uc']} UC {o['pubg_id']} ID ga yuborildi"
        else:
            cb = 0
            if not by_stars:
                cb = int(o["price"] * CASHBACK_PCT / 100)
                x("UPDATE users SET spent=spent+?, wallet=wallet+?, cashback=cashback+? WHERE id=?",
                  (o["price"], cb, cb, uid))
            msg = f"✅ Buyurtma #{oid} bajarildi" + (f"\nCashback: +{cb:,} so'm" if cb else "")
    else:
        if kind == "withdraw":
            x("UPDATE users SET balance=balance+? WHERE id=?", (o["uc"] * U, uid))
            msg = f"❌ So'rov #{oid} rad etildi, UC balansga qaytarildi"
        elif by_stars:
            try:
                await bot.refund_star_payment(user_id=uid, telegram_payment_charge_id=o["charge"])
                msg = f"❌ Buyurtma #{oid} rad etildi, {o['stars']} Stars qaytarildi"
            except Exception:
                msg = f"❌ Buyurtma #{oid} rad etildi. Stars qaytarish uchun support bilan bog'laning"
                if bot and ADMIN_ID:
                    try:
                        await bot.send_message(ADMIN_ID, f"⚠️ #{oid}: Stars avtomatik qaytarilmadi "
                                                         f"(user {uid}, {o['stars']} Stars). Qo'lda qaytaring.")
                    except Exception:
                        pass
        elif kind in ("buy", "stars"):
            x("UPDATE users SET wallet=wallet+? WHERE id=?", (o["price"], uid))
            msg = f"❌ Buyurtma #{oid} rad etildi, pul balansga qaytarildi"
        else:
            msg = f"❌ To'ldirish #{oid} rad etildi. Savol bo'lsa support bilan bog'laning"
    if bot:
        try:
            await bot.send_message(uid, msg)
        except Exception:
            pass
    return True


@app.get("/api/admin/orders")
async def admin_orders(x_init_data: str = Header("")):
    need_admin(x_init_data)
    rows = q("SELECT o.*, u.name FROM orders o JOIN users u ON u.id=o.user_id "
             "WHERE o.status='pending' ORDER BY o.id")
    return {"stats": admin_stats(), "orders": [dict(r) for r in rows]}


@app.post("/api/admin/decide")
async def admin_decide(req: DecideReq, x_init_data: str = Header("")):
    need_admin(x_init_data)
    if req.action not in ("ok", "no"):
        raise HTTPException(400, "action xato")
    if not await apply_decision(req.order, req.action):
        raise HTTPException(400, "Allaqachon ko'rib chiqilgan")
    return {"ok": True}


# ================= BOT =================
@dp.message(CommandStart())
async def start(m: Message, command: CommandObject):
    ref = None
    if command.args and command.args.startswith("ref_") and command.args[4:].isdigit():
        ref = int(command.args[4:])
    get_user(m.from_user.id, m.from_user.first_name, ref)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎮 Ochish", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("UC do'koni va tekin UC ishlash 👇", reply_markup=kb)


@dp.callback_query(F.data.regexp(r"^(ok|no):\d+$"))
async def decide(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    act, oid = c.data.split(":")
    if not await apply_decision(int(oid), act):
        return await c.answer("Allaqachon ko'rib chiqilgan")
    mark = f"\n\n→ {'TASDIQLANDI' if act == 'ok' else 'RAD ETILDI'}"
    if c.message.photo:
        await c.message.edit_caption(caption=(c.message.caption or "") + mark)
    else:
        await c.message.edit_text((c.message.text or "") + mark)
    await c.answer("Tayyor")


@dp.message(Command("admin"))
async def admin_cmd(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    rows = q("SELECT * FROM orders WHERE status='pending' ORDER BY id")
    st = admin_stats()
    await m.answer(f"👑 Admin panel\nFoydalanuvchilar: {st['users']}\nKutilayotgan: {len(rows)}\n"
                   f"Bajarilgan: {st['done']}\nRad etilgan: {st['rejected']}\n\n"
                   f"Promo kod: /promo KOD SUMMA SONI")
    for o in rows:
        await notify_admin(o["id"])


@dp.pre_checkout_query()
async def pre_checkout(pcq: PreCheckoutQuery):
    ok = False
    try:
        oid = int(pcq.invoice_payload.split(":")[1])
        o = q("SELECT * FROM orders WHERE id=?", (oid,), one=True)
        ok = bool(o and o["status"] == "unpaid" and o["pay"] == "stars" and o["user_id"] == pcq.from_user.id
                  and o["stars"] == pcq.total_amount and pcq.currency == "XTR")
    except Exception:
        pass
    await pcq.answer(ok=ok, error_message=None if ok else "Buyurtma topilmadi, qaytadan urinib ko'ring")


@dp.message(F.successful_payment)
async def got_payment(m: Message):
    sp = m.successful_payment
    try:
        oid = int(sp.invoice_payload.split(":")[1])
    except Exception:
        return
    if x("UPDATE orders SET status='pending', charge=? WHERE id=? AND status='unpaid'",
         (sp.telegram_payment_charge_id, oid)) == 1:
        await notify_admin(oid)
        await m.answer(f"✅ {sp.total_amount} Stars to'landi. Buyurtma #{oid} adminga yuborildi, tez orada UC tushadi.")


@dp.message(Command("promo"))
async def promo_cmd(m: Message, command: CommandObject):
    if m.from_user.id != ADMIN_ID:
        return
    a = (command.args or "").split()
    if len(a) != 3 or not a[1].isdigit() or not a[2].isdigit():
        return await m.answer("Format: /promo KOD SUMMA SONI\nMasalan: /promo BONUS10 10000 50")
    x("INSERT OR REPLACE INTO promos(code,amount,uses) VALUES(?,?,?)", (a[0].upper(), int(a[1]), int(a[2])))
    await m.answer(f"✅ Promo kod {a[0].upper()}: {int(a[1]):,} so'm, {a[2]} marta")


INDEX_HTML = r"""<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<title>UC Shop</title>
<style>
:root{--bg:#09090d;--card:#16161c;--card2:#1c1c24;--line:#25252f;--blue:#3b82f6;--green:#34d399;--red:#ef4444;--txt:#f2f2f7;--mut:#8b8b9a}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--txt);font-family:system-ui,-apple-system,sans-serif;padding-bottom:96px}
.hide{display:none!important}
.wrap{padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:16px;margin-bottom:12px}
h2{margin:0 0 8px;font-size:18px}h3{margin:18px 0 10px;font-size:20px}
.mut{color:var(--mut);font-size:13px}.up{font-size:12px;letter-spacing:.06em;color:var(--mut);font-weight:700;margin:14px 0 8px}
input{width:100%;padding:14px;border-radius:12px;border:1px solid var(--line);background:#09090d;color:var(--txt);font-size:16px;margin:6px 0;outline:none}
input:focus{border-color:var(--blue)}
button{border:0;border-radius:12px;padding:14px;font-size:16px;font-weight:700;background:var(--blue);color:#fff;width:100%;margin-top:6px}
button.sec{background:#1f2745;color:#9cc0ff}button.red{background:#b91c1c}button:disabled{opacity:.4}
.row{display:flex;gap:10px;align-items:center}.row>*{margin-top:0}
.back{width:40px;height:40px;border-radius:50%;background:var(--card);border:1px solid var(--line);color:var(--txt);padding:0;margin:0;flex:none}
.top{display:flex;align-items:center;gap:12px;margin-bottom:14px}.top .tt{font-size:20px;font-weight:800;color:#60a5fa}
.hello{color:#60a5fa;font-weight:800;font-size:13px;letter-spacing:.05em}.uname{font-size:28px;font-weight:900;margin:2px 0 14px;font-family:Georgia,serif}
.balcard{background:linear-gradient(135deg,#1e2a5a,#3a1d6b);border:1px solid #3a3f8f;border-radius:20px;padding:18px;display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.balcard small{color:#a5b4fc;font-weight:700;letter-spacing:.05em}.big{font-size:34px;font-weight:900}.big small{font-size:14px;color:#c7d2fe;font-weight:600}
.balcard button{width:auto;padding:12px 16px;background:linear-gradient(90deg,#2563eb,#7c3aed);margin:0}
.wide{background:#12182b;border:1px solid #243056;color:#cbd5e1}
.banner{position:relative;border-radius:20px;overflow:hidden;height:170px;margin:14px 0 6px;border:1px solid #3a2a1a}
.slide{position:absolute;inset:0;padding:20px;display:flex;flex-direction:column;justify-content:center;transition:opacity .5s}
.dots{display:flex;gap:6px;justify-content:center;margin:8px 0}.dots i{width:8px;height:8px;border-radius:50%;background:#333}.dots i.on{width:26px;border-radius:5px;background:var(--blue)}
.hd{display:flex;justify-content:space-between;align-items:baseline}.hd a{color:#60a5fa;font-weight:700;font-size:14px}
.gg{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.gt{text-align:center;font-size:12px;font-weight:700}
.gi{position:relative;aspect-ratio:3/4;border-radius:16px;background:linear-gradient(160deg,#242a45,#12121a);border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-size:36px;margin-bottom:6px}
.st{position:absolute;bottom:6px;right:6px;background:#0008;border-radius:8px;padding:2px 6px;font-size:11px;color:#fff}
.free .gi{background:linear-gradient(160deg,#7c4a05,#2a1a05);border-color:#7c4a05}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.chip{padding:11px 16px;border-radius:22px;background:var(--card);border:1px solid var(--line);font-weight:700;font-size:15px;color:#cbd5e1}
.chip.on{background:var(--blue);border-color:var(--blue);color:#fff}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.pc{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;text-align:center;display:flex;flex-direction:column;gap:6px}
.pc.sel{border-color:var(--blue)}.ps{color:#fbbf24;font-weight:700;font-size:13px}.pt{font-weight:800;font-size:16px;min-height:40px;display:flex;align-items:center;justify-content:center}.pp{color:var(--green);font-weight:700;font-size:14px}
.step{display:flex;align-items:center;background:#0e0e13;border-radius:12px;overflow:hidden;border:1px solid var(--line)}
.step button{width:44px;margin:0;border-radius:0;background:#1b2b24;color:var(--green);padding:10px 0;font-size:20px}.step button[data-d="-1"]{background:#1d1d26;color:#777}
.step span{flex:1;text-align:center;font-weight:800;font-size:17px}
.bar2{position:fixed;left:12px;right:12px;bottom:84px;z-index:5;background:#101827;border:1px solid #28406e;border-radius:16px;padding:10px 12px;display:flex;flex-direction:column;gap:8px}
.bar2 .row2{display:flex;align-items:center;gap:10px}
.bar2 button{margin:0;padding:12px;width:auto;flex:1}.bar2 b{white-space:nowrap}
.bar2 button.stbtn{width:100%;background:linear-gradient(90deg,#d97706,#f59e0b);color:#1a1000}
.nick{color:var(--green);font-weight:700;margin:4px 0}.err{color:#f87171;margin:4px 0}.info{color:#fbbf24;margin:4px 0;font-size:13px}
.tap{width:230px;height:230px;border-radius:50%;margin:10px auto 16px;display:flex;align-items:center;justify-content:center;font-size:70px;background:radial-gradient(circle at 35% 30%,#ffd86b,#f59e0b 60%,#b45309);box-shadow:0 0 40px #f59e0b55;user-select:none;touch-action:manipulation;transition:transform .05s}
.tap:active{transform:scale(.94)}
.bar{height:10px;background:#222;border-radius:8px;overflow:hidden}.bar i{display:block;height:100%;background:var(--green)}
.fl{position:fixed;pointer-events:none;font-weight:800;color:#fff;animation:up .8s forwards;z-index:20}@keyframes up{to{transform:translateY(-70px);opacity:0}}
.menu{background:var(--card);border:1px solid var(--line);border-radius:18px;overflow:hidden;margin-bottom:12px}
.mi{display:flex;align-items:center;gap:14px;padding:14px 16px;border-bottom:1px solid var(--line)}.mi:last-child{border:0}
.mi .ic{width:44px;height:44px;border-radius:12px;background:#1d2236;display:flex;align-items:center;justify-content:center;font-size:20px;flex:none}
.mi .tx{flex:1}.mi b{display:block;font-size:16px}.mi small{color:var(--mut)}.mi .ar{color:var(--mut);font-size:20px}
.badge{background:#0f2f26;color:var(--green);border:1px solid #1d6b50;border-radius:8px;padding:3px 8px;font-size:12px;font-weight:700}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 6px;text-align:center}.stat b{font-size:24px;display:block}.stat small{color:var(--mut);font-size:11px;letter-spacing:.05em}
.prof{background:linear-gradient(160deg,#171a33,#0f0f18);border:1px solid #2a2f5a;border-radius:22px;padding:18px;margin-bottom:12px}
.av{width:64px;height:64px;border-radius:50%;background:linear-gradient(135deg,#3b82f6,#7c3aed);display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:900}
.copy{font-family:monospace;font-size:22px;letter-spacing:.06em;background:#0e0e13;border:1px dashed #3b82f6;border-radius:12px;padding:14px;text-align:center;margin:8px 0}
.ord{padding:12px 0;border-bottom:1px solid var(--line)}.ord:last-child{border:0}
.s-pending{color:#fbbf24}.s-done{color:var(--green)}.s-rejected{color:#f87171}
.nav{position:fixed;bottom:0;left:0;right:0;display:flex;background:#0d0d13;border-top:1px solid var(--line);z-index:10;height:72px}
.nav>div{flex:1;text-align:center;padding-top:10px;font-size:12px;color:var(--mut);font-weight:600}.nav>div b{display:block;font-size:22px}
.nav>div.on{color:#60a5fa}
.nav .fab{position:relative}.nav .fab i{position:absolute;left:50%;top:-26px;transform:translateX(-50%);width:62px;height:62px;border-radius:20px;background:linear-gradient(135deg,#3b82f6,#2563eb);border:5px solid #0d0d13;color:#fff;font-style:normal;font-size:34px;display:flex;align-items:center;justify-content:center;box-shadow:0 0 22px #3b82f688}
.nav .fab span{position:absolute;bottom:8px;left:0;right:0}
</style>
</head>
<body>
<div class="wrap">

<!-- BOSH -->
<section id="pg-home" class="pg">
  <div class="hello">XUSH KELIBSIZ 👋</div><div class="uname" id="uname"></div>
  <div class="balcard"><div><small>BALANS</small><div class="big"><span class="wal">0</span> <small>so'm</small></div></div>
    <button onclick="nav('topup')">+ To'ldirish</button></div>
  <button class="wide" onclick="support()">🎧 Yordam</button>
  <div class="banner" id="banner"></div><div class="dots" id="dots"></div>
  <div class="hd"><h3>Ommabop o'yinlar</h3><a onclick="nav('games')">Barchasi</a></div>
  <div class="gg" id="homegames"></div>
</section>

<!-- O'YINLAR -->
<section id="pg-games" class="pg hide">
  <h3 style="margin-top:0">O'yinlar</h3><div class="gg" id="allgames"></div>
</section>

<!-- O'YIN (PUBG va boshqalar) -->
<section id="pg-game" class="pg hide">
  <div class="top"><button class="back" onclick="back()">←</button><div class="tt" id="gtitle"></div></div>
  <div class="chips" id="gtabs"></div>
  <div class="up" id="gup">MAHSULOTNI TANLANG</div>
  <div class="pgrid" id="prodlist"></div>
  <div class="card" id="gidcard" style="margin-top:14px"><h2>Player ID</h2>
    <div class="row"><input id="gid" inputmode="numeric" placeholder="Masalan: 5123456789"><button class="sec" id="gchk" style="width:auto;padding:14px 16px">Tekshirish</button></div>
    <div id="gnick"></div></div>
  <div style="height:130px"></div>
  <div class="bar2 hide" id="gbar"><div class="row2"><div><div class="mut">Jami</div><b id="gtotal">0 so'm</b></div><button id="gbuy">Sotib olish</button></div>
    <button class="stbtn hide" id="gstars">⭐ Stars bilan to'lash</button></div>
</section>

<!-- TELEGRAM STARS -->
<section id="pg-stars" class="pg hide">
  <div class="top"><button class="back" onclick="back()">←</button><div class="tt">TELEGRAM STARS</div></div>
  <div class="chips"><span class="chip on" id="tt-stars" onclick="tgTabSet('stars')">Stars</span><span class="chip" id="tt-prem" onclick="tgTabSet('prem')">Premium</span></div>
  <div class="card"><div class="up" style="margin-top:0">TELEGRAM USERNAME</div>
    <div class="row"><input id="tgu" placeholder="@username" autocapitalize="off"><button class="sec" id="tgchk" style="width:auto;padding:14px 16px">Tekshirish</button></div>
    <div id="tgnick"></div></div>
  <div id="tg-stars"><div class="up">IXTIYORIY SUMMA</div>
    <div class="card"><div class="mut">Yulduzlar sonini kiriting</div><input id="tgn" inputmode="numeric" placeholder="50">
      <div class="row" style="justify-content:space-between;border-top:1px solid var(--line);padding-top:10px;margin-top:8px"><span class="up" style="margin:0">TO'LOV SUMMASI:</span><b id="tgsum">-</b></div></div></div>
  <div id="tg-prem" class="hide"><div class="up">TARIFNI TANLANG</div><div class="pgrid" id="premlist"></div></div>
  <button id="tgbuy" style="margin-top:14px">Sotib olish</button>
</section>

<!-- TEKIN UC -->
<section id="pg-free" class="pg hide">
  <div class="top"><button class="back" onclick="back()">←</button><div class="tt">🪙 Tekin UC</div></div>
  <div class="card"><div class="mut">UC balans</div><div class="big"><span id="ucbal">0.000</span> UC</div>
    <div class="mut">Yechish: minimal <span id="minw">120</span> UC va kamida <span id="minr">10</span> ta do'st</div></div>
  <div class="tap" id="tap">🪙</div>
  <div class="card"><div class="mut">Energiya <span id="en">100</span>/<span id="mx">100</span> (kuniga 20 UC)</div><div class="bar"><i id="bar" style="width:100%"></i></div></div>
  <div class="card"><h2>👥 Referal</h2><div class="mut">Har bir do'st uchun +2 UC. Taklif qilinganlar: <b id="refs">0</b></div>
    <button class="sec" id="share">Havolani ulashish</button></div>
  <div class="card"><h2>💸 UC yechish</h2><div class="mut">Ishlagan UC ni PUBG ID ga yuboramiz</div>
    <input id="wdid" inputmode="numeric" placeholder="Player ID"><button class="sec" id="wdchk">Tekshirish</button><div id="wdnick"></div>
    <input id="wdamt" inputmode="numeric" placeholder="Necha UC? (min 120)"><button id="wdgo" disabled>Yechish</button></div>
</section>

<!-- TO'LDIRISH -->
<section id="pg-topup" class="pg hide">
  <div class="hd"><h3 style="margin-top:0">Balansni to'ldirish</h3><span class="badge"><span class="wal">0</span> so'm</span></div>
  <div class="menu">
    <div class="mi" onclick="go('guide')"><div class="ic" style="background:#0e6fb5">📄</div><div class="tx"><b>Qo'llanma</b></div><div class="ar">›</div></div>
    <div class="mi" onclick="openTopup('Karta (Humo/Uzcard)')"><div class="ic">💳</div><div class="tx"><b>Karta orqali (Humo/Uzcard)</b></div><span class="badge">so'm</span><div class="ar">›</div></div>
    <div class="mi" onclick="openTopup('Bankamat')"><div class="ic">🏧</div><div class="tx"><b>BANKAMAT</b></div><span class="badge">so'm</span><div class="ar">›</div></div>
    <div class="mi" onclick="support()"><div class="ic">🇷🇺</div><div class="tx"><b>СБП</b><small>Support orqali</small></div><span class="badge">rubl</span><div class="ar">›</div></div>
  </div>
</section>

<section id="pg-topupform" class="pg hide">
  <div class="top"><button class="back" onclick="back()">←</button><div class="tt" id="tfname">To'ldirish</div></div>
  <div class="card" id="tf1"><div class="up" style="margin-top:0">TO'LDIRISH SUMMASI (SO'M)</div>
    <input id="tfamt" inputmode="numeric" placeholder="Masalan: 25000"><button id="tfgo">Karta olish</button></div>
  <div class="card hide" id="tf2">
    <div class="mut">Quyidagi kartaga <b>aynan shu summani</b> o'tkazing:</div>
    <div class="copy" id="tfcard" onclick="copyCard()"></div><div class="mut" id="tfholder"></div>
    <div class="big" style="margin:10px 0" id="tfsum"></div>
    <div class="mut">To'lovdan keyin chek (screenshot) yuboring, admin tasdiqlagach balans to'ladi.</div>
    <input type="file" id="tffile" accept="image/*" class="hide"><button id="tfsend" onclick="$('tffile').click()">📎 Chek yuborish</button>
    <div id="tfres"></div></div>
</section>

<section id="pg-guide" class="pg hide">
  <div class="top"><button class="back" onclick="back()">←</button><div class="tt">Qo'llanma</div></div>
  <div class="card"><h2 style="color:#60a5fa">Balansni qanday to'ldiraman?</h2><div class="mut" style="font-size:15px;line-height:1.5">"To'ldirish" bo'limidan usulni tanlaysiz, summani kiritasiz va ko'rsatmaga amal qilasiz. Admin tasdiqlagach balans ko'payadi.</div></div>
  <div class="card"><h2 style="color:#fbbf24">Karta orqali</h2><div class="mut" style="font-size:15px;line-height:1.7">1. Summani kiriting.<br>2. Tizim karta raqamini beradi.<br>3. Ko'rsatilgan summani aynan shu holicha o'tkazing.<br>4. Chek (screenshot) yuboring.<br>5. Admin tasdiqlaydi va balans to'ladi.</div></div>
</section>

<!-- BUYURTMALAR -->
<section id="pg-orders" class="pg hide"><h3 style="margin-top:0">Buyurtmalar</h3><div class="card" id="hist">Yuklanmoqda...</div></section>

<!-- PROFIL -->
<section id="pg-profile" class="pg hide">
  <div class="prof"><div class="row"><div class="av" id="pav">U</div><div><div style="font-size:22px;font-weight:900" id="pname"></div><div class="mut" style="font-family:monospace">ID: <span id="pid"></span></div></div></div>
    <div class="balcard" style="margin:14px 0 0;background:#0e0e14;border-color:var(--line)"><div><small>BALANS</small><div class="big"><span class="wal">0</span> <small>so'm</small></div></div><button onclick="nav('topup')">+ To'ldirish</button></div></div>
  <div class="stats"><div class="stat"><b id="pcnt">0</b><small>BUYURTMALAR</small></div><div class="stat"><b id="pspent">0</b><small>SARFLANDI</small></div><div class="stat"><b id="pcb" style="color:var(--green)">0</b><small>CASHBACK</small></div></div>
  <div class="up">HISOB</div>
  <div class="menu"><div class="mi" onclick="nav('topup')"><div class="ic">💳</div><div class="tx"><b>Balans to'ldirish</b><small>Humo, Uzcard</small></div><div class="ar">›</div></div>
    <div class="mi" onclick="nav('orders')"><div class="ic">📋</div><div class="tx"><b>Tarix</b><small>Buyurtmalar va to'lovlar</small></div><div class="ar">›</div></div>
    <div class="mi hide" id="admrow" onclick="go('adm')"><div class="ic">👑</div><div class="tx"><b>Admin panel</b><small>Buyurtmalarni tasdiqlash</small></div><div class="ar">›</div></div></div>
  <div class="up">SOZLAMALAR</div>
  <div class="menu"><div class="mi" onclick="tg.showAlert('Sessiya Telegram orqali tasdiqlanadi. Hech kimga kod yoki parol bermang.')"><div class="ic">🔒</div><div class="tx"><b>Xavfsizlik</b><small>Sessiya, tasdiqlash</small></div><span class="badge">✓ Himoyalangan</span></div>
    <div class="mi" id="notirow"><div class="ic">🔔</div><div class="tx"><b>Bildirishnomalar</b><small id="notism">Yoqilgan</small></div><span class="badge" id="notib">ON</span></div>
    <div class="mi"><div class="ic">🌐</div><div class="tx"><b>Til</b><small>O'zbek</small></div></div></div>
  <div class="up">BOSHQA</div>
  <div class="menu"><div class="mi" onclick="$('promobox').classList.toggle('hide')"><div class="ic">🎁</div><div class="tx"><b>Promo kod</b><small>Chegirma kodi kiriting</small></div><div class="ar">›</div></div>
    <div class="hide" id="promobox" style="padding:0 16px 14px"><div class="row"><input id="promoin" placeholder="KOD" autocapitalize="characters"><button id="promogo" style="width:auto;padding:14px 18px">OK</button></div></div>
    <div class="mi" onclick="tg.showAlert('1) UC faqat to\'g\'ri Player ID ga yuboriladi.\n2) Xato ID uchun javobgarlik foydalanuvchida.\n3) Bajarilgan buyurtma qaytarilmaydi.')"><div class="ic">📃</div><div class="tx"><b>Qoidalar</b><small>Foydalanish shartlari</small></div><div class="ar">›</div></div>
    <div class="mi" onclick="support()"><div class="ic">🎧</div><div class="tx"><b>Yordam / Support</b><small>Muammo yoki savol bo'lsa</small></div><span class="mut" id="supname"></span></div></div>
</section>

<!-- ADMIN -->
<section id="pg-adm" class="pg hide">
  <div class="top"><button class="back" onclick="back()">←</button><div class="tt">👑 Admin panel</div></div>
  <div class="card mut" id="admstat"></div><div id="admlist"></div>
</section>

</div>

<div class="nav" id="nav">
  <div data-n="home" onclick="nav('home')"><b>🏠</b>Bosh</div>
  <div data-n="games" onclick="nav('games')"><b>🎮</b>O'yinlar</div>
  <div class="fab" data-n="topup" onclick="nav('topup')"><i>+</i><span>To'ldirish</span></div>
  <div data-n="orders" onclick="nav('orders')"><b>🛍</b>Buyurtmalar</div>
  <div data-n="profile" onclick="nav('profile')"><b>👤</b>Profil</div>
</div>

<script>
const tg = Telegram.WebApp; tg.ready(); tg.expand();
try{ tg.setHeaderColor("#09090d"); tg.setBackgroundColor("#09090d"); }catch(e){}
const H = {"Content-Type":"application/json","X-Init-Data":tg.initData};
const $ = id => document.getElementById(id);
const esc = s => String(s==null?"":s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => Number(n||0).toLocaleString("ru-RU");
let me=null, CAT={}, PRICE={}, TITLE={}, STAR={}, cart={}, curGame="pubg", curTab="", idNick=null, stack=["home"], pending=0, wdNick=null;
let tgMode="stars", tgPlan="", tgOk=false, topMethod="", topOrder=0, slide=0;

async function api(path, body){
  const r = await fetch(path, body ? {method:"POST",headers:H,body:JSON.stringify(body)} : {headers:H});
  let j = {}; try{ j = await r.json(); }catch(e){}
  if(!r.ok) throw new Error(typeof j.detail==="string" ? j.detail : "Xatolik");
  return j;
}

/* ---------- Router ---------- */
const NAVMAP={home:"home",games:"games",game:"games",stars:"games",free:"games",topup:"topup",topupform:"topup",guide:"topup",orders:"orders",profile:"profile",adm:"profile"};
const ON={orders:()=>loadHist(), adm:()=>loadAdm(), home:()=>renderHome(), games:()=>renderGames(), profile:()=>renderProfile()};
function go(p, push=true){
  if(push) stack.push(p);
  document.querySelectorAll(".pg").forEach(e=>e.classList.add("hide"));
  $("pg-"+p).classList.remove("hide");
  document.querySelectorAll("#nav [data-n]").forEach(e=>e.classList.toggle("on", e.dataset.n===NAVMAP[p]));
  try{ stack.length>1 ? tg.BackButton.show() : tg.BackButton.hide(); }catch(e){}
  window.scrollTo(0,0);
  if(ON[p]) ON[p]();
}
function nav(p){ stack=[p]; go(p,false); }
function back(){ if(stack.length>1){ stack.pop(); go(stack[stack.length-1],false); } }
try{ tg.BackButton.onClick(back); }catch(e){}

/* ---------- Umumiy render ---------- */
function render(){
  document.querySelectorAll(".wal").forEach(e=>e.textContent=fmt(me.wallet));
  $("ucbal").textContent=me.balance.toFixed(3);
  $("en").textContent=me.energy; $("mx").textContent=me.max_energy;
  $("bar").style.width=(me.energy/me.max_energy*100)+"%";
  $("refs").textContent=me.refs; $("minw").textContent=me.min_withdraw; $("minr").textContent=me.min_refs;
}
function support(){
  if(me && me.support) tg.openTelegramLink("https://t.me/"+me.support);
  else tg.showAlert("Support hali sozlanmagan");
}
async function refresh(){ me=await api("/api/me"); CAT=me.catalog; PRICE={}; TITLE={}; STAR={};
  Object.values(CAT).forEach(g=>Object.values(g.tabs).forEach(a=>a.forEach(p=>{PRICE[p.k]=p.p;TITLE[p.k]=p.t;STAR[p.k]=p.s;}))); render(); }

/* ---------- Bosh ---------- */
function tile(onclk,emoji,name,cls,badge){
  return `<div class="gt ${cls||""}" onclick="${onclk}"><div class="gi">${emoji}<span class="st">${badge||"★ 5"}</span></div>${esc(name)}</div>`;
}
function gameTiles(){
  let h = tile("openGame('free')","🪙","Tekin UC","free","FREE");
  Object.entries(CAT).forEach(([k,g])=>{ h += tile(`openGame('${k}')`,g.emoji,g.short); });
  h += tile("openGame('stars')","⭐","Telegram Stars/Premium");
  return h;
}
function renderHome(){
  $("uname").textContent=me.name||"Do'st";
  $("homegames").innerHTML=gameTiles();
}
function renderGames(){ $("allgames").innerHTML=gameTiles(); }
const SLIDES=[
  {bg:"linear-gradient(135deg,#7f1d1d,#1f0b0b)",t:"24/7 UC SERVICE",s:"Tez va ishonchli yetkazish",f:()=>openGame("pubg")},
  {bg:"linear-gradient(135deg,#7c4a05,#1a1005)",t:"🎁 TEKIN UC ISHLANG",s:"Bosing, do'st taklif qiling, UC yeching",f:()=>openGame("free")},
  {bg:"linear-gradient(135deg,#1e3a8a,#0b1020)",t:"KANALIMIZGA OBUNA BO'LING",s:"Yangiliklar va so'nggi ma'lumotlar",f:()=>{ me.channel ? tg.openTelegramLink(me.channel) : support(); }}
];
function showSlide(){
  const s=SLIDES[slide%SLIDES.length];
  $("banner").innerHTML=`<div class="slide" style="background:${s.bg}" onclick="SLIDES[${slide%SLIDES.length}].f()"><div style="font-size:26px;font-weight:900">${s.t}</div><div class="mut" style="color:#cbd5e1;margin-top:6px">${s.s}</div></div>`;
  $("dots").innerHTML=SLIDES.map((_,i)=>`<i class="${i===slide%SLIDES.length?"on":""}"></i>`).join("");
}
setInterval(()=>{ slide++; if(me && !$("pg-home").classList.contains("hide")) showSlide(); },4000);

/* ---------- O'yin sahifasi ---------- */
function openGame(k){
  if(k==="free") return go("free");
  if(k==="stars"){ tgTabSet("stars"); return go("stars"); }
  curGame=k; const tabs=Object.keys(CAT[k].tabs); curTab=tabs[0]||""; cart={}; idNick=null;
  $("gid").value=""; $("gnick").innerHTML=""; go("game"); renderGame();
}
function setTab(i){ curTab=Object.keys(CAT[curGame].tabs)[i]; renderGame(); }
function renderGame(){
  const g=CAT[curGame], tabs=Object.keys(g.tabs);
  $("gtitle").textContent=g.name;
  const empty = !tabs.length || tabs.every(t=>!g.tabs[t].length);
  $("gtabs").innerHTML=tabs.map((t,i)=>`<span class="chip ${t===curTab?"on":""}" onclick="setTab(${i})">${esc(t)}</span>`).join("");
  $("gidcard").classList.toggle("hide",empty);
  $("gup").classList.toggle("hide",empty && false);
  const list=(g.tabs[curTab]||[]);
  $("prodlist").innerHTML = list.length ? list.map(p=>`<div class="pc ${cart[p.k]?"sel":""}"><div class="pt">${esc(p.t)}</div><div class="pp">${fmt(p.p)} so'm</div>${p.s?`<div class="ps">⭐ ${p.s} Stars</div>`:""}
    <div class="step"><button data-k="${esc(p.k)}" data-d="-1">−</button><span>${cart[p.k]||0}</span><button data-k="${esc(p.k)}" data-d="1">+</button></div></div>`).join("")
    : `<div class="card mut" style="grid-column:1/3">Tez orada qo'shiladi</div>`;
  updBar();
}
$("prodlist").onclick=e=>{ const b=e.target.closest("button[data-k]"); if(!b) return;
  const k=b.dataset.k; const n=Math.max(0,Math.min(50,(cart[k]||0)+Number(b.dataset.d)));
  if(n) cart[k]=n; else delete cart[k]; renderGame(); try{tg.HapticFeedback.impactOccurred("light")}catch(x){} };
function cartTotal(){ return Object.entries(cart).reduce((s,[k,n])=>s+(PRICE[k]||0)*n,0); }
function starsTotal(){ let s=0; for(const [k,n] of Object.entries(cart)){ if(!STAR[k]) return 0; s+=STAR[k]*n; } return s; }
function updBar(){
  const t=cartTotal(); $("gbar").classList.toggle("hide",!t);
  $("gtotal").textContent=fmt(t)+" so'm";
  const idok=/^\d{5,13}$/.test($("gid").value.trim()) && (idNick || me.allow_unverified);
  $("gbuy").disabled=!idok; $("gbuy").textContent = t>me.wallet ? "Balans yetarli emas" : "Sotib olish";
  const st=starsTotal(); $("gstars").classList.toggle("hide",!st);
  if(st){ $("gstars").textContent="⭐ "+st+" Stars bilan to'lash"; $("gstars").disabled=!idok; }
}
$("gid").oninput=()=>{ idNick=null; $("gnick").innerHTML=""; updBar(); };
async function checkId(inputId,outId){
  const out=$(outId); out.innerHTML="<div class='mut'>Tekshirilmoqda...</div>";
  try{ const r=await api("/api/check_id",{pubg_id:$(inputId).value.trim()});
    if(r.ok){ out.innerHTML=`<div class="nick">✅ Nik: ${esc(r.nick)}</div>`; return r.nick; }
    out.innerHTML=`<div class="err">${esc(r.error)}</div>`;
  }catch(e){ out.innerHTML=`<div class="err">${esc(e.message)}</div>`; }
  return null;
}
$("gchk").onclick=async()=>{ idNick=await checkId("gid","gnick"); updBar(); };
$("gbuy").onclick=async()=>{
  if(cartTotal()>me.wallet){ tg.showAlert("Balans yetarli emas. Avval balansni to'ldiring."); return nav("topup"); }
  $("gbuy").disabled=true;
  try{ const r=await api("/api/buy",{pubg_id:$("gid").value.trim(),items:cart}); cart={}; await refresh();
    tg.showAlert("Buyurtma #"+r.order+" qabul qilindi ✅"); renderGame();
  }catch(e){ tg.showAlert(e.message); updBar(); }
};

$("gstars").onclick=()=>{
  const st=starsTotal(); if(!st) return;
  const names=Object.entries(cart).map(([k,n])=>TITLE[k]+(n>1?" x"+n:"")).join(", ");
  tg.showConfirm(names+" uchun Telegram hisobingizdan "+st+" Stars yechiladi. Tasdiqlaysizmi?", async ok=>{
    if(!ok) return;
    try{ const r=await api("/api/stars_invoice",{pubg_id:$("gid").value.trim(),items:cart});
      tg.openInvoice(r.link, async status=>{
        if(status==="paid"){ cart={}; await new Promise(res=>setTimeout(res,1500)); await refresh();
          tg.showAlert("✅ "+r.stars+" Stars to'landi. Buyurtma #"+r.order+" adminga yuborildi."); renderGame(); }
        else if(status==="failed"){ tg.showAlert("To'lov amalga oshmadi. Qaytadan urinib ko'ring."); }
      });
    }catch(e){ tg.showAlert(e.message); }
  });
};

/* ---------- Telegram Stars / Premium ---------- */
function tgTabSet(m){
  tgMode=m; tgPlan="";
  $("tt-stars").classList.toggle("on",m==="stars"); $("tt-prem").classList.toggle("on",m==="prem");
  $("tg-stars").classList.toggle("hide",m!=="stars"); $("tg-prem").classList.toggle("hide",m!=="prem");
  $("premlist").innerHTML=me.premium.map(p=>`<div class="pc" data-t="${esc(p.t)}"><div class="pt">${esc(p.t)}</div><div class="pp">${fmt(p.p)} so'm</div></div>`).join("");
  tgUpd();
}
$("premlist").onclick=e=>{ const c=e.target.closest(".pc"); if(!c) return; tgPlan=c.dataset.t;
  document.querySelectorAll("#premlist .pc").forEach(x=>x.classList.toggle("sel",x===c)); tgUpd(); };
function tgUpd(){
  const n=parseInt($("tgn").value||"0"); const sum=n>=me.star_min ? n*me.star_price : 0;
  $("tgsum").textContent=sum?fmt(sum)+" so'm":"-";
  $("tgbuy").disabled=!tgOk || (tgMode==="stars" ? !sum : !tgPlan);
}
$("tgn").oninput=tgUpd;
$("tgu").oninput=()=>{ tgOk=false; $("tgnick").innerHTML=""; tgUpd(); };
$("tgchk").onclick=async()=>{
  try{ const r=await api("/api/check_username",{username:$("tgu").value});
    if(r.ok){ tgOk=true; $("tgnick").innerHTML = r.verified ? `<div class="nick">✅ ${esc(r.name)} (${esc(r.username)})</div>`
      : `<div class="nick">✅ ${esc(r.username)} qabul qilindi</div><div class="info">Akkauntni avtomatik tekshirib bo'lmadi, username to'g'riligiga ishonch hosil qiling.</div>`; }
    else { tgOk=false; $("tgnick").innerHTML=`<div class="err">${esc(r.error)}</div>`; }
  }catch(e){ $("tgnick").innerHTML=`<div class="err">${esc(e.message)}</div>`; }
  tgUpd();
};
$("tgbuy").onclick=async()=>{
  try{ const body={username:$("tgu").value}; if(tgMode==="stars") body.stars=parseInt($("tgn").value); else body.plan=tgPlan;
    const sum = tgMode==="stars" ? body.stars*me.star_price : (me.premium.find(p=>p.t===tgPlan)||{}).p;
    if(sum>me.wallet){ tg.showAlert("Balans yetarli emas. Avval balansni to'ldiring."); return nav("topup"); }
    const r=await api("/api/tgbuy",body); await refresh(); tg.showAlert("Buyurtma #"+r.order+" qabul qilindi ✅");
  }catch(e){ tg.showAlert(e.message); }
};

/* ---------- Tekin UC ---------- */
$("tap").addEventListener("pointerdown",e=>{
  if(me.energy<1){ try{tg.HapticFeedback.notificationOccurred("error")}catch(x){} return; }
  me.energy--; me.balance+=me.tap_reward; pending++; render();
  try{tg.HapticFeedback.impactOccurred("light")}catch(x){}
  const f=document.createElement("div"); f.className="fl"; f.textContent="+"+me.tap_reward;
  f.style.left=e.clientX+"px"; f.style.top=(e.clientY-20)+"px"; document.body.appendChild(f); setTimeout(()=>f.remove(),800);
});
setInterval(async()=>{
  if(!me) return;
  if(!pending){ if(me.energy<me.max_energy){ me.energy=Math.min(me.max_energy,me.energy+me.regen); render(); } return; }
  const n=pending; pending=0;
  try{ const r=await api("/api/tap",{count:n}); me.balance=r.balance; me.energy=r.energy; render(); }catch(e){}
},1000);
$("share").onclick=()=>{ tg.openTelegramLink("https://t.me/share/url?url="+encodeURIComponent(me.ref_link)+"&text="+encodeURIComponent("Tekin UC ishlang! 🎁")); };
$("wdchk").onclick=async()=>{ wdNick=await checkId("wdid","wdnick"); $("wdgo").disabled=!(wdNick||me.allow_unverified); };
$("wdid").oninput=()=>{ wdNick=null; $("wdnick").innerHTML=""; $("wdgo").disabled=!me.allow_unverified; };
$("wdgo").onclick=async()=>{
  try{ await api("/api/withdraw",{pubg_id:$("wdid").value.trim(),uc:parseInt($("wdamt").value||"0")});
    tg.showAlert("So'rov yuborildi ✅ Admin tasdiqlagach UC tushadi."); await refresh();
  }catch(e){ tg.showAlert(e.message); }
};

/* ---------- To'ldirish ---------- */
function openTopup(m){ topMethod=m; topOrder=0; $("tfname").textContent=m; $("tf1").classList.remove("hide"); $("tf2").classList.add("hide"); $("tfres").innerHTML=""; $("tfamt").value=""; go("topupform"); }
$("tfgo").onclick=async()=>{
  try{ const r=await api("/api/topup",{amount:parseInt($("tfamt").value||"0"),method:topMethod}); topOrder=r.order;
    $("tfcard").textContent=r.card; $("tfholder").textContent=r.holder||""; $("tfsum").textContent=fmt(r.amount)+" so'm";
    $("tf1").classList.add("hide"); $("tf2").classList.remove("hide");
  }catch(e){ tg.showAlert(e.message); }
};
function copyCard(){ try{ navigator.clipboard.writeText($("tfcard").textContent.replace(/\s/g,"")); tg.showAlert("Karta raqami nusxalandi"); }catch(e){} }
function readImg(file){ return new Promise(res=>{ const r=new FileReader(); r.onload=()=>{ const im=new Image(); im.onload=()=>{
  const s=Math.min(1,1100/Math.max(im.width,im.height)); const c=document.createElement("canvas"); c.width=im.width*s; c.height=im.height*s;
  c.getContext("2d").drawImage(im,0,0,c.width,c.height); res(c.toDataURL("image/jpeg",.8)); }; im.src=r.result; }; r.readAsDataURL(file); }); }
$("tffile").onchange=async e=>{ const f=e.target.files[0]; if(!f) return;
  $("tfres").innerHTML="<div class='mut'>Yuborilmoqda...</div>";
  try{ await api("/api/receipt",{order:topOrder,image:await readImg(f)}); $("tfres").innerHTML="<div class='nick'>✅ Chek yuborildi. Admin tasdiqlashini kuting.</div>"; }
  catch(err){ $("tfres").innerHTML=`<div class="err">${esc(err.message)}</div>`; } };

/* ---------- Tarix ---------- */
const KN={buy:"Xarid",withdraw:"UC yechish",topup:"To'ldirish",stars:"Telegram"};
const SN={unpaid:"To'lov kutilmoqda",pending:"Kutilmoqda",done:"Bajarildi",rejected:"Rad etildi"};
async function loadHist(){
  try{ const rows=await api("/api/orders");
    $("hist").innerHTML=rows.length?rows.map(o=>`<div class="ord"><b>#${o.id} ${KN[o.kind]||o.kind}</b>${o.price?" · "+fmt(o.price)+" so'm":""}${o.pay==="stars"?" · ⭐ "+o.stars+" Stars":""}${o.kind==="withdraw"?" · "+o.uc+" UC":""}
      <div class="mut">${esc(o.note||"")} ${esc(o.pubg_id||"")} ${esc(o.nick||"")}</div><div class="s-${o.status}">${SN[o.status]||o.status}</div></div>`).join(""):"Hozircha bo'sh";
  }catch(e){ $("hist").textContent=e.message; }
}

/* ---------- Profil ---------- */
function notiState(){ try{ return localStorage.getItem("noti")!=="off"; }catch(e){ return true; } }
function renderProfile(){
  $("pav").textContent=(me.name||"U").slice(0,1).toUpperCase(); $("pname").textContent=me.name||"";
  $("pid").textContent=me.id; $("pcnt").textContent=me.orders; $("pspent").textContent=fmt(me.spent); $("pcb").textContent=fmt(me.cashback);
  $("supname").textContent=me.support?"@"+me.support:""; $("admrow").classList.toggle("hide",!me.is_admin);
  const on=notiState(); $("notib").textContent=on?"ON":"OFF"; $("notism").textContent=on?"Yoqilgan":"O'chirilgan";
}
$("notirow").onclick=()=>{ try{ localStorage.setItem("noti",notiState()?"off":"on"); }catch(e){} renderProfile(); };
$("promogo").onclick=async()=>{
  try{ const r=await api("/api/promo",{code:$("promoin").value}); $("promoin").value=""; await refresh(); tg.showAlert("Balansga +"+fmt(r.amount)+" so'm qo'shildi 🎁"); }
  catch(e){ tg.showAlert(e.message); }
};

/* ---------- Admin ---------- */
async function loadAdm(){
  try{ const r=await api("/api/admin/orders"), s=r.stats;
    $("admstat").textContent=`Foydalanuvchilar: ${s.users} · Kutilmoqda: ${s.pending} · Bajarilgan: ${s.done} · Rad: ${s.rejected}`;
    $("admlist").innerHTML=r.orders.length?r.orders.map(o=>`<div class="card"><b>#${o.id} ${KN[o.kind]||o.kind}${o.price?" · "+fmt(o.price)+" so'm":""}</b>
      <div class="mut">${esc(o.name)} (id ${o.user_id})</div>
      ${o.pay==="stars"?`<div class="nick">⭐ ${o.stars} Stars to'langan</div>`:""}
      ${o.pubg_id?`<div>ID/Username: <b>${esc(o.pubg_id)}</b></div>`:""}
      ${o.kind==="buy"||o.kind==="withdraw"?`<div class="${o.nick?"nick":"err"}">${o.nick?"Nik: "+esc(o.nick):"Nik tasdiqlanmagan"}</div>`:""}
      ${o.uc?`<div>Miqdor: ${o.uc}</div>`:""}${o.note?`<div class="mut">${esc(o.note)}</div>`:""}
      <div class="row" style="margin-top:8px"><button onclick="decide(${o.id},'ok')">✅ Tasdiqlash</button><button class="red" onclick="decide(${o.id},'no')">❌ Rad etish</button></div></div>`).join("")
      :"<div class='card mut'>Kutilayotgan buyurtma yo'q</div>";
  }catch(e){ $("admlist").innerHTML=`<div class="card err">${esc(e.message)}</div>`; }
}
async function decide(order,action){
  try{ await api("/api/admin/decide",{order,action}); await loadAdm(); try{tg.HapticFeedback.notificationOccurred("success")}catch(x){} }
  catch(e){ tg.showAlert(e.message); }
}

/* ---------- Start ---------- */
refresh().then(()=>{ showSlide(); go("home",false); }).catch(e=>{ document.body.innerHTML="<div style='padding:30px;text-align:center'>Telegram ichida oching.<br><small>"+esc(e.message)+"</small></div>"; });
</script>
</body>
</html>
"""
