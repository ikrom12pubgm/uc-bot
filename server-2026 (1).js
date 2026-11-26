const express = require("express");
const crypto = require("crypto");
const path = require("path");
const fs = require("fs");
const { Telegraf, Markup } = require("telegraf");

const BOT_TOKEN = process.env.BOT_TOKEN;
const WEBAPP_URL = process.env.WEBAPP_URL;          // https://sizning-app.up.railway.app
const PORT = process.env.PORT || 3000;
const ADMIN_ID = Number(process.env.ADMIN_ID || 0);
const STAR_SOM = Number(process.env.STAR_SOM || 200);   // 1 Star = necha so'm (hisobga tushadi)
const NICK_API_URL = process.env.NICK_API_URL || "";    // ixtiyoriy, masalan https://api.site/pubg?id={id}
const NICK_FIELD = process.env.NICK_FIELD || "nickname";
const CASHBACK_PERCENT = Number(process.env.CASHBACK_PERCENT || 0);   // do'kon xaridlaridan qaytadigan foiz
const PAY_INFO = process.env.PAY_INFO || "";   // karta orqali to'ldirish matni: "Karta: 8600 ... (Ism). Chekni @admin ga yuboring"
const SEED_CHATS = (process.env.REQUIRED_CHATS || "")
  .split(",").map(s => s.trim().replace(/^@/, "")).filter(Boolean);

if (!BOT_TOKEN || !WEBAPP_URL) {
  console.error("BOT_TOKEN va WEBAPP_URL o'zgaruvchilarini kiriting!");
  process.exit(1);
}
console.log("VERSION: 3 (bosh, oyinlar, profil, tolash)");
console.log("WEBAPP_URL:", WEBAPP_URL);

const bot = new Telegraf(BOT_TOKEN);
bot.catch((err) => console.error("BOT XATO:", err && err.description ? err.description : err.message || err));
process.on("unhandledRejection", (e) => console.error("UNHANDLED:", e && e.message ? e.message : e));

const app = express();
app.use(express.json());
app.get("/", (req, res) => res.sendFile(path.join(__dirname, "index-2026.html")));

/* ===== Saqlash (Railway'da Volume ulang: DATA_DIR=/data) ===== */
const DATA_DIR = process.env.DATA_DIR || __dirname;
const DB_FILE = path.join(DATA_DIR, "db.json");
let db = { users: {}, orders: [], paid: {}, starsTotal: 0, chats: null };
try { db = Object.assign(db, JSON.parse(fs.readFileSync(DB_FILE, "utf8"))); } catch {}
if (!Array.isArray(db.chats)) db.chats = SEED_CHATS.slice();   // birinchi ishga tushishda env dan olinadi
const getChats = () => db.chats;
let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try { fs.writeFileSync(DB_FILE, JSON.stringify(db)); } catch (e) { console.error("save xato", e.message); }
  }, 300);
}

/* Birliklar: 1 UC = 10 birlik (0.2 UC = 2 birlik). Hisob (som) butun so'mda. */
const TAP = 2;               // 0.2 UC
const DAILY_LIMIT = 200;     // 20 UC
const REF_BONUS = 20;        // 2 UC
const MIN_WITHDRAW = 1200;   // 120 UC
const MIN_FRIENDS = 10;
const SHOP = [
  { uc: 60, som: 12500 }, { uc: 120, som: 25000 }, { uc: 180, som: 40000 },
  { uc: 240, som: 52000 }, { uc: 325, som: 62000 }
];
const STAR_PACKS = [25, 50, 100, 250, 500];
/* kind: shop = UC do'koni ishlaydi, stars = hisob to'ldirish, soon = tez orada */
const GAMES = [
  { id: "pubg", name: "PUBG Mobile (ID orqali)", icon: "🔫", c1: "#c9822b", c2: "#6b3b12", kind: "shop" },
  { id: "stars", name: "Telegram Stars", icon: "⭐", c1: "#2aa3e8", c2: "#1b4f9a", kind: "stars" },
  { id: "steam", name: "Steam", icon: "🎮", c1: "#2b5f86", c2: "#12273a", kind: "soon" },
  { id: "cod", name: "Call of Duty Mobile", icon: "🪖", c1: "#4a5b4a", c2: "#1d261d", kind: "soon" },
  { id: "mlbb", name: "Mobile Legends", icon: "⚔️", c1: "#6a3fb5", c2: "#27164a", kind: "soon" },
  { id: "roblox", name: "Roblox", icon: "🧱", c1: "#555b66", c2: "#15171c", kind: "soon" },
  { id: "standoff", name: "Standoff 2", icon: "🎯", c1: "#9aa832", c2: "#3a4011", kind: "soon" },
  { id: "freefire", name: "Free Fire", icon: "🔥", c1: "#e8662a", c2: "#7a2310", kind: "soon" },
  { id: "magicchess", name: "Magic Chess", icon: "♟️", c1: "#3b8fd0", c2: "#16305a", kind: "soon" }
];

const today = () => new Date().toISOString().slice(0, 10);
const esc = (s) => String(s || "").replace(/[<>&]/g, "");

function getUser(tg, refId) {
  const id = String(tg.id);
  let u = db.users[id];
  if (!u) {
    u = db.users[id] = { id: tg.id, name: tg.first_name || "", username: tg.username || "",
      balance: 0, som: 0, friends: 0, day: today(), todayEarned: 0, lastTap: 0, ref: null,
      joined: Date.now(), lastSeen: Date.now(), notify: true, cashback: 0 };
    if (refId && String(refId) !== id && db.users[String(refId)]) {
      u.ref = Number(refId);
      const r = db.users[String(refId)];
      r.balance += REF_BONUS; r.friends += 1;
    }
    save();
  }
  if (tg.first_name && u.name !== tg.first_name) u.name = tg.first_name;
  if (tg.username) u.username = tg.username;
  u.som = u.som || 0; u.cashback = u.cashback || 0; if (u.notify === undefined) u.notify = true;
  u.lastSeen = Date.now();
  if (u.day !== today()) { u.day = today(); u.todayEarned = 0; save(); }
  return u;
}
const view = (u) => ({
  id: u.id, balance: u.balance / 10, som: u.som, today: u.todayEarned / 10, dailyLimit: DAILY_LIMIT / 10,
  friends: u.friends, minWithdraw: MIN_WITHDRAW / 10, minFriends: MIN_FRIENDS, tap: TAP / 10,
  isAdmin: !!ADMIN_ID && u.id === ADMIN_ID, shop: SHOP, starPacks: STAR_PACKS, starSom: STAR_SOM,
  games: GAMES, notify: u.notify !== false, cashback: u.cashback || 0, cashbackPercent: CASHBACK_PERCENT,
  payInfo: PAY_INFO, name: u.name || "",
  ordersCount: db.orders.filter(o => o.user === u.id && o.type !== "stars").length,
  spent: db.orders.filter(o => o.user === u.id && o.type === "shop" && o.status === "done").reduce((s, o) => s + o.price, 0)
});

/* Telegram initData tekshiruvi */
function verifyInitData(initData) {
  if (!initData) return null;
  const params = new URLSearchParams(initData);
  const hash = params.get("hash");
  if (!hash) return null;
  params.delete("hash");
  const dataCheck = [...params.entries()].map(([k, v]) => `${k}=${v}`).sort().join("\n");
  const secret = crypto.createHmac("sha256", "WebAppData").update(BOT_TOKEN).digest();
  const calc = crypto.createHmac("sha256", secret).update(dataCheck).digest("hex");
  if (calc !== hash) return null;
  const authDate = Number(params.get("auth_date") || 0);
  if (Date.now() / 1000 - authDate > 86400) return null;
  try { return JSON.parse(params.get("user")); } catch { return null; }
}

async function missingChats(userId) {
  const missing = [];
  for (const c of getChats()) {
    try {
      const m = await bot.telegram.getChatMember("@" + c, userId);
      if (["left", "kicked"].includes(m.status)) missing.push(c);
    } catch (e) {
      console.error("getChatMember xato:", c, e.message);
      missing.push(c);
    }
  }
  return missing;
}

app.get("/api/config", (req, res) => {
  res.json({ botName: bot.botInfo && bot.botInfo.username, chats: getChats().map(u => ({ username: u, title: "@" + u })) });
});

app.post("/api/check-sub", async (req, res) => {
  const user = verifyInitData(req.body.initData);
  if (!user) return res.status(401).json({ ok: false, error: "auth" });
  const missing = await missingChats(user.id);
  res.json({ ok: missing.length === 0, missing });
});

async function requireSub(req, res, next) {
  const user = verifyInitData(req.body.initData);
  if (!user) return res.status(401).json({ ok: false, error: "auth" });
  const missing = await missingChats(user.id);
  if (missing.length) return res.status(403).json({ ok: false, missing });
  req.tgUser = user;
  next();
}
function requireAdmin(req, res, next) {
  const user = verifyInitData(req.body.initData);
  if (!user || !ADMIN_ID || user.id !== ADMIN_ID) return res.status(403).json({ ok: false, error: "admin emas" });
  req.tgUser = user;
  next();
}

/* ===== Asosiy ===== */
app.post("/api/me", requireSub, (req, res) => res.json({ ok: true, ...view(getUser(req.tgUser)) }));

app.post("/api/tap", requireSub, (req, res) => {
  const u = getUser(req.tgUser);
  const now = Date.now();
  let taps = Math.max(0, Math.min(30, parseInt(req.body.taps) || 0));
  if (now - u.lastTap < 700) taps = 0;
  u.lastTap = now;
  taps = Math.min(taps, Math.floor((DAILY_LIMIT - u.todayEarned) / TAP));
  u.balance += taps * TAP; u.todayEarned += taps * TAP;
  if (taps) save();
  res.json({ ok: true, ...view(u) });
});

const cleanPubg = (v) => String(v || "").replace(/\D/g, "");

async function lookupNick(id) {
  if (!NICK_API_URL) return null;
  try {
    const r = await fetch(NICK_API_URL.replace("{id}", encodeURIComponent(id)));
    const j = await r.json();
    const n = j && (j[NICK_FIELD] || (j.data && j.data[NICK_FIELD]));
    return n ? esc(n).slice(0, 40) : null;
  } catch { return null; }
}

app.post("/api/nick", requireSub, async (req, res) => {
  const pubg = cleanPubg(req.body.pubgId);
  if (pubg.length < 5 || pubg.length > 12) return res.json({ ok: false, error: "PUBG ID noto'g'ri" });
  const nick = await lookupNick(pubg);
  if (!nick) return res.json({ ok: false, error: NICK_API_URL ? "Nik topilmadi" : "Nik tekshiruvi sozlanmagan" });
  res.json({ ok: true, nick });
});

function notifyUser(id, text, force) {
  const u = db.users[String(id)];
  if (force || !u || u.notify !== false) bot.telegram.sendMessage(id, text).catch(() => {});
}
function notifyAdmin(text) {
  if (ADMIN_ID) bot.telegram.sendMessage(ADMIN_ID, text).catch(() => {});
}
function newOrder(o) {
  const order = Object.assign({ id: db.orders.length + 1, status: "pending", at: Date.now() }, o);
  db.orders.push(order); save();
  return order;
}

/* UC yechish (tap bilan yig'ilgan UC -> PUBG ID) */
app.post("/api/withdraw", requireSub, (req, res) => {
  const u = getUser(req.tgUser);
  const pubg = cleanPubg(req.body.pubgId);
  if (pubg.length < 5 || pubg.length > 12) return res.json({ ok: false, error: "PUBG ID noto'g'ri" });
  if (u.balance < MIN_WITHDRAW) return res.json({ ok: false, error: "Kamida " + MIN_WITHDRAW / 10 + " UC kerak" });
  if (u.friends < MIN_FRIENDS) return res.json({ ok: false, error: "Kamida " + MIN_FRIENDS + " do'st taklif qiling" });
  const amount = u.balance;
  u.balance = 0;
  const o = newOrder({ type: "withdraw", user: u.id, pubg, nick: String(req.body.nick || "").slice(0, 40), uc: amount / 10 });
  notifyAdmin(`💸 Yechish #${o.id}\nUser: ${u.id} (${u.name})\nPUBG ID: ${pubg}\nUC: ${o.uc}`);
  res.json({ ok: true, ...view(u) });
});

/* Do'kon: hisobdagi so'mdan UC sotib olish */
app.post("/api/shop/buy", requireSub, (req, res) => {
  const u = getUser(req.tgUser);
  const pubg = cleanPubg(req.body.pubgId);
  const pack = SHOP.find(p => p.uc === Number(req.body.uc));
  if (!pack) return res.json({ ok: false, error: "Paket topilmadi" });
  if (pubg.length < 5 || pubg.length > 12) return res.json({ ok: false, error: "PUBG ID noto'g'ri" });
  if (u.som < pack.som) return res.json({ ok: false, error: "Hisobda mablag' yetarli emas", need: pack.som - u.som });
  u.som -= pack.som;
  const o = newOrder({ type: "shop", user: u.id, pubg, nick: String(req.body.nick || "").slice(0, 40), uc: pack.uc, price: pack.som });
  notifyAdmin(`🛒 Buyurtma #${o.id}\nUser: ${u.id} (${u.name})\nPUBG ID: ${pubg}\nUC: ${pack.uc}\nNarx: ${pack.som} so'm`);
  res.json({ ok: true, ...view(u) });
});

/* Telegram Stars bilan hisobni to'ldirish */
app.post("/api/topup", requireSub, async (req, res) => {
  const u = getUser(req.tgUser);
  const stars = Number(req.body.stars);
  if (!STAR_PACKS.includes(stars)) return res.json({ ok: false, error: "Noto'g'ri miqdor" });
  try {
    const link = await bot.telegram.callApi("createInvoiceLink", {
      title: "Hisobni to'ldirish",
      description: `${stars} Stars = ${stars * STAR_SOM} so'm`,
      payload: `topup:${u.id}:${stars}`,
      provider_token: "",
      currency: "XTR",
      prices: [{ label: "Stars", amount: stars }]
    });
    res.json({ ok: true, link });
  } catch (e) {
    console.error("createInvoiceLink xato:", e.description || e.message);
    res.json({ ok: false, error: "To'lov havolasi yaratilmadi" });
  }
});

app.post("/api/settings", requireSub, (req, res) => {
  const u = getUser(req.tgUser);
  if (typeof req.body.notify === "boolean") u.notify = req.body.notify;
  save();
  res.json({ ok: true, ...view(u) });
});

/* Karta orqali to'ldirish: foydalanuvchi so'rov yuboradi, admin chekni ko'rib tasdiqlaydi */
app.post("/api/topup-manual", requireSub, (req, res) => {
  const u = getUser(req.tgUser);
  const amount = Math.round(Number(req.body.amount));
  if (!PAY_INFO) return res.json({ ok: false, error: "Bu usul hozircha yoqilmagan" });
  if (!(amount >= 5000 && amount <= 5000000)) return res.json({ ok: false, error: "Miqdor 5 000 dan 5 000 000 so'mgacha" });
  const pend = db.orders.filter(o => o.user === u.id && o.type === "topup" && o.status === "pending").length;
  if (pend >= 3) return res.json({ ok: false, error: "Avvalgi so'rovlar ko'rib chiqilsin" });
  const o = newOrder({ type: "topup", user: u.id, amount });
  notifyAdmin(`💳 To'ldirish so'rovi #${o.id}\nUser: ${u.id} (${u.name})\nMiqdor: ${amount} so'm`);
  res.json({ ok: true, ...view(u) });
});

app.post("/api/orders", requireSub, (req, res) => {
  const u = getUser(req.tgUser);
  const list = db.orders.filter(o => o.user === u.id).slice(-50).reverse();
  res.json({ ok: true, orders: list });
});

/* ===== Admin ===== */
app.post("/api/admin/stats", requireAdmin, (req, res) => {
  const users = Object.values(db.users);
  const d = today();
  const dayStart = new Date(d + "T00:00:00Z").getTime();
  const cnt = (type, st) => db.orders.filter(o => o.type === type && o.status === st).length;
  const top = users.filter(x => x.friends > 0)
    .sort((a, b) => b.friends - a.friends).slice(0, 10)
    .map(x => ({ id: x.id, name: x.name || "—", username: x.username || "", friends: x.friends }));
  res.json({
    ok: true,
    stats: {
      users: users.length,
      newToday: users.filter(x => x.joined >= dayStart).length,
      activeToday: users.filter(x => x.day === d && x.todayEarned > 0).length,
      withReferrals: users.filter(x => x.friends > 0).length,
      totalFriends: users.reduce((s, x) => s + x.friends, 0),
      totalUc: users.reduce((s, x) => s + x.balance, 0) / 10,
      totalSom: users.reduce((s, x) => s + (x.som || 0), 0),
      starsTotal: db.starsTotal || 0,
      shopPending: cnt("shop", "pending"), shopDone: cnt("shop", "done"), shopRejected: cnt("shop", "rejected"),
      topupPending: cnt("topup", "pending"), wdPending: cnt("withdraw", "pending"), wdDone: cnt("withdraw", "done"), wdRejected: cnt("withdraw", "rejected")
    },
    top
  });
});

app.post("/api/admin/orders", requireAdmin, (req, res) => {
  const status = req.body.status === "done" ? "done" : "pending";
  const list = db.orders.filter(o => status === "pending" ? o.status === "pending" : o.status !== "pending")
    .slice(-50).reverse().map(o => {
      const u = db.users[String(o.user)] || {};
      return Object.assign({}, o, { name: u.name || "", username: u.username || "" });
    });
  res.json({ ok: true, orders: list });
});

app.post("/api/admin/chats", requireAdmin, (req, res) => res.json({ ok: true, chats: getChats() }));

app.post("/api/admin/chat-add", requireAdmin, async (req, res) => {
  const name = String(req.body.username || "").trim().replace(/^https?:\/\/t\.me\//i, "").replace(/^@/, "").split(/[/?]/)[0];
  if (!/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(name)) return res.json({ ok: false, error: "Username noto'g'ri (masalan: kanal_nomi)" });
  if (getChats().some(c => c.toLowerCase() === name.toLowerCase())) return res.json({ ok: false, error: "Bu allaqachon ro'yxatda" });
  let title = "@" + name, warn = "";
  try {
    const chat = await bot.telegram.getChat("@" + name);
    title = chat.title || title;
    const me = await bot.telegram.getChatMember("@" + name, bot.botInfo ? bot.botInfo.id : (await bot.telegram.getMe()).id);
    if (!["administrator", "creator"].includes(me.status)) warn = "Bot bu yerda admin emas. Uni admin qiling, aks holda tekshiruv ishlamaydi.";
  } catch (e) {
    return res.json({ ok: false, error: "Topilmadi yoki bot u yerda yo'q. Botni kanal/guruhga admin qilib qo'shing va qayta urining." });
  }
  db.chats.push(name); save();
  res.json({ ok: true, chats: getChats(), title, warn });
});

app.post("/api/admin/chat-del", requireAdmin, (req, res) => {
  const name = String(req.body.username || "").replace(/^@/, "");
  db.chats = db.chats.filter(c => c.toLowerCase() !== name.toLowerCase()); save();
  res.json({ ok: true, chats: getChats() });
});

app.post("/api/admin/act", requireAdmin, (req, res) => {
  const o = db.orders.find(x => x.id === Number(req.body.id));
  if (!o || o.status !== "pending") return res.json({ ok: false, error: "Topilmadi yoki ko'rib chiqilgan" });
  const u = db.users[String(o.user)];
  const label = o.type === "shop" ? `${o.uc} UC (do'kon)` : o.type === "topup" ? `${o.amount} so'm (to'ldirish)` : `${o.uc} UC (yechish)`;
  if (req.body.action === "approve") {
    o.status = "done";
    if (o.type === "topup" && u) { u.som += o.amount; }
    if (o.type === "shop" && u && CASHBACK_PERCENT > 0) {
      const cb = Math.floor(o.price * CASHBACK_PERCENT / 100);
      u.som += cb; u.cashback = (u.cashback || 0) + cb;
    }
    notifyUser(o.user, o.type === "topup"
      ? `✅ Hisobingiz ${o.amount} so'mga to'ldirildi (#${o.id}).`
      : `✅ Buyurtma #${o.id} tasdiqlandi: ${label}. PUBG ID: ${o.pubg}`);
  } else if (req.body.action === "reject") {
    o.status = "rejected";
    if (u && o.type === "shop") u.som += o.price;
    if (u && o.type === "withdraw") u.balance += Math.round(o.uc * 10);
    notifyUser(o.user, o.type === "topup"
      ? `❌ To'ldirish so'rovi #${o.id} rad etildi.`
      : `❌ Buyurtma #${o.id} rad etildi (${label}). Mablag' hisobingizga qaytarildi.`);
  } else return res.json({ ok: false, error: "Noto'g'ri amal" });
  o.doneAt = Date.now();
  save();
  res.json({ ok: true });
});

/* ===== Bot ===== */
const welcome = (ctx) => ctx.reply("🪙 UC Tap'ga xush kelibsiz!",
  Markup.inlineKeyboard([[Markup.button.webApp("🚀 O'ynash", WEBAPP_URL)]]));

bot.start(async (ctx) => {
  const m0 = /^ref_(\d+)$/.exec(ctx.startPayload || "");
  getUser(ctx.from, m0 && m0[1]);
  const missing = await missingChats(ctx.from.id);
  if (missing.length) {
    return ctx.reply("⚠️ Kanalga a'zo bo'ling!", Markup.inlineKeyboard([
      ...missing.map(c => [Markup.button.url("➕ @" + c, "https://t.me/" + c)]),
      [Markup.button.callback("✅ Tekshirish", "check_sub")]
    ]));
  }
  return welcome(ctx);
});

bot.action("check_sub", async (ctx) => {
  const missing = await missingChats(ctx.from.id);
  if (missing.length) return ctx.answerCbQuery("Kanalga a'zo bo'ling!", { show_alert: true });
  await ctx.answerCbQuery("✅ Tasdiqlandi");
  return welcome(ctx);
});

/* Stars to'lovi */
bot.on("pre_checkout_query", (ctx) => ctx.answerPreCheckoutQuery(true).catch(() => {}));
bot.on("message", (ctx, next) => {
  const sp = ctx.message && ctx.message.successful_payment;
  if (!sp) return next();
  const m = /^topup:(\d+):(\d+)$/.exec(sp.invoice_payload || "");
  const chargeId = sp.telegram_payment_charge_id;
  if (!m || Number(m[1]) !== ctx.from.id || db.paid[chargeId]) return;
  const stars = Number(m[2]);
  const u = getUser(ctx.from);
  u.som += stars * STAR_SOM;
  db.paid[chargeId] = { user: u.id, stars, at: Date.now() };
  db.starsTotal = (db.starsTotal || 0) + stars;
  db.orders.push({ id: db.orders.length + 1, type: "stars", user: u.id, stars, amount: stars * STAR_SOM, status: "done", at: Date.now() });
  save();
  notifyAdmin(`⭐ To'lov: ${u.name} (${u.id}) ${stars} Stars → ${stars * STAR_SOM} so'm`);
  return ctx.reply(`✅ Hisobingiz ${stars * STAR_SOM} so'mga to'ldirildi.`);
});

app.listen(PORT, () => console.log("Server:", PORT));
bot.launch().then(() => console.log("Bot ishga tushdi"));
bot.telegram.getMe().then(i => { bot.botInfo = i; });
process.once("SIGINT", () => bot.stop("SIGINT"));
process.once("SIGTERM", () => bot.stop("SIGTERM"));
