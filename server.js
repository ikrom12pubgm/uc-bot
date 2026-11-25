const express = require("express");
const crypto = require("crypto");
const path = require("path");
const { Telegraf, Markup } = require("telegraf");

const BOT_TOKEN = process.env.BOT_TOKEN;
const WEBAPP_URL = process.env.WEBAPP_URL;          // https://sizning-app.up.railway.app
const PORT = process.env.PORT || 3000;
// Kanal va guruhlar, vergul bilan: kanal_username,guruh_username  (@ belgisiz)
const CHATS = (process.env.REQUIRED_CHATS || "")
  .split(",").map(s => s.trim().replace(/^@/, "")).filter(Boolean);

if (!BOT_TOKEN || !WEBAPP_URL || CHATS.length === 0) {
  console.error("BOT_TOKEN, WEBAPP_URL va REQUIRED_CHATS o'zgaruvchilarini kiriting!");
  process.exit(1);
}

const bot = new Telegraf(BOT_TOKEN);
const app = express();
app.use(express.json());
app.get("/", (req, res) => res.sendFile(path.join(__dirname, "index.html")));

/* ===== Oddiy JSON saqlash (Railway'da Volume ulang: DATA_DIR=/data) ===== */
const fs = require("fs");
const DATA_DIR = process.env.DATA_DIR || __dirname;
const DB_FILE = path.join(DATA_DIR, "db.json");
const ADMIN_ID = Number(process.env.ADMIN_ID || 0);
let db = { users: {}, withdrawals: [] };
try { db = JSON.parse(fs.readFileSync(DB_FILE, "utf8")); } catch {}
let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    try { fs.writeFileSync(DB_FILE, JSON.stringify(db)); } catch (e) { console.error("save xato", e.message); }
  }, 300);
}

/* Birliklar: 1 UC = 10 birlik (0.2 UC = 2 birlik) */
const TAP = 2;               // 0.2 UC
const DAILY_LIMIT = 200;     // 20 UC
const REF_BONUS = 20;        // 2 UC
const MIN_WITHDRAW = 1200;   // 120 UC
const MIN_FRIENDS = 10;

const today = () => new Date().toISOString().slice(0, 10);
function getUser(tg, refId) {
  const id = String(tg.id);
  let u = db.users[id];
  if (!u) {
    u = db.users[id] = { id: tg.id, name: tg.first_name || "", balance: 0, friends: 0,
      day: today(), todayEarned: 0, lastTap: 0, ref: null };
    if (refId && String(refId) !== id && db.users[String(refId)]) {
      u.ref = Number(refId);
      const r = db.users[String(refId)];
      r.balance += REF_BONUS; r.friends += 1;
    }
    save();
  }
  if (u.day !== today()) { u.day = today(); u.todayEarned = 0; save(); }
  return u;
}
const view = (u) => ({ balance: u.balance / 10, today: u.todayEarned / 10, dailyLimit: DAILY_LIMIT / 10,
  friends: u.friends, minWithdraw: MIN_WITHDRAW / 10, minFriends: MIN_FRIENDS, tap: TAP / 10, id: u.id });

/* Telegram initData ni tekshirish va user ID olish */
function verifyInitData(initData) {
  if (!initData) return null;
  const params = new URLSearchParams(initData);
  const hash = params.get("hash");
  if (!hash) return null;
  params.delete("hash");
  const dataCheck = [...params.entries()]
    .map(([k, v]) => `${k}=${v}`).sort().join("\n");
  const secret = crypto.createHmac("sha256", "WebAppData").update(BOT_TOKEN).digest();
  const calc = crypto.createHmac("sha256", secret).update(dataCheck).digest("hex");
  if (calc !== hash) return null;
  const authDate = Number(params.get("auth_date") || 0);
  if (Date.now() / 1000 - authDate > 86400) return null;   // 24 soatdan eski
  try { return JSON.parse(params.get("user")); } catch { return null; }
}

/* Foydalanuvchi a'zo bo'lmagan chatlar ro'yxati */
async function missingChats(userId) {
  const missing = [];
  for (const c of CHATS) {
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
  res.json({ botName: bot.botInfo && bot.botInfo.username, chats: CHATS.map(u => ({ username: u, title: "@" + u })) });
});

app.post("/api/check-sub", async (req, res) => {
  const user = verifyInitData(req.body.initData);
  if (!user) return res.status(401).json({ ok: false, error: "auth" });
  const missing = await missingChats(user.id);
  res.json({ ok: missing.length === 0, missing });
});

/* Boshqa API lar (tap, yechish, xarid) uchun himoya: */
async function requireSub(req, res, next) {
  const user = verifyInitData(req.body.initData);
  if (!user) return res.status(401).json({ ok: false, error: "auth" });
  const missing = await missingChats(user.id);
  if (missing.length) return res.status(403).json({ ok: false, missing });
  req.tgUser = user;
  next();
}
// Misol: app.post("/api/tap", requireSub, (req, res) => { ... });

app.post("/api/me", requireSub, (req, res) => {
  res.json({ ok: true, ...view(getUser(req.tgUser)) });
});

app.post("/api/tap", requireSub, (req, res) => {
  const u = getUser(req.tgUser);
  const now = Date.now();
  let taps = Math.max(0, Math.min(30, parseInt(req.body.taps) || 0));
  if (now - u.lastTap < 700) taps = 0;            // juda tez so'rovlarni rad etish
  u.lastTap = now;
  const room = Math.floor((DAILY_LIMIT - u.todayEarned) / TAP);
  taps = Math.min(taps, room);
  u.balance += taps * TAP; u.todayEarned += taps * TAP;
  if (taps) save();
  res.json({ ok: true, ...view(u) });
});

app.post("/api/withdraw", requireSub, async (req, res) => {
  const u = getUser(req.tgUser);
  const pubg = String(req.body.pubgId || "").replace(/\D/g, "");
  if (pubg.length < 5 || pubg.length > 12) return res.json({ ok: false, error: "PUBG ID noto'g'ri" });
  if (u.balance < MIN_WITHDRAW) return res.json({ ok: false, error: "Kamida " + MIN_WITHDRAW / 10 + " UC kerak" });
  if (u.friends < MIN_FRIENDS) return res.json({ ok: false, error: "Kamida " + MIN_FRIENDS + " do'st taklif qiling" });
  const amount = u.balance;
  u.balance = 0;
  const w = { id: db.withdrawals.length + 1, user: u.id, pubg, amount, status: "pending", at: Date.now() };
  db.withdrawals.push(w); save();
  if (ADMIN_ID) bot.telegram.sendMessage(ADMIN_ID,
    `💸 Yechish #${w.id}\nUser: ${u.id} (${u.name})\nPUBG ID: ${pubg}\nSumma: ${amount / 10} UC`).catch(() => {});
  res.json({ ok: true, ...view(u) });
});

/* Bot */
bot.start(async (ctx) => {
  const m0 = /^ref_(\d+)$/.exec(ctx.startPayload || "");
  getUser(ctx.from, m0 && m0[1]);
  const missing = await missingChats(ctx.from.id);
  if (missing.length) {
    return ctx.reply(
      "⚠️ Kanalga a'zo bo'ling!",
      Markup.inlineKeyboard([
        ...missing.map(c => [Markup.button.url("➕ @" + c, "https://t.me/" + c)]),
        [Markup.button.callback("✅ Tekshirish", "check_sub")]
      ])
    );
  }
  return ctx.reply("🪙 UC Tap'ga xush kelibsiz!",
    Markup.inlineKeyboard([[Markup.button.webApp("🚀 O'ynash", WEBAPP_URL)]]));
});

bot.action("check_sub", async (ctx) => {
  const missing = await missingChats(ctx.from.id);
  if (missing.length) return ctx.answerCbQuery("Kanalga a'zo bo'ling!", { show_alert: true });
  await ctx.answerCbQuery("✅ Tasdiqlandi");
  return ctx.reply("🪙 UC Tap'ga xush kelibsiz!",
    Markup.inlineKeyboard([[Markup.button.webApp("🚀 O'ynash", WEBAPP_URL)]]));
});

app.listen(PORT, () => console.log("Server:", PORT));
bot.launch().then(() => console.log("Bot ishga tushdi"));
bot.telegram.getMe().then(i => { bot.botInfo = i; });
process.once("SIGINT", () => bot.stop("SIGINT"));
process.once("SIGTERM", () => bot.stop("SIGTERM"));
