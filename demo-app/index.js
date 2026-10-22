// Demo Shop — a deliberately breakable website for demoing the Liara Ops Assistant.
//
// Failure modes (all simulated, clearly labelled):
//   * Missing DATABASE_URL  -> the home page and /api/orders return HTTP 500
//   * GET /api/leak?mb=100  -> retains memory (memory-leak scenario, push RAM toward the plan limit)
//   * GET /api/cpu?ms=3000  -> burns CPU (CPU-saturation scenario)
//   * LEAK_PER_REQUEST_MB=5 -> leaks a little memory on every request (gradual leak under traffic)
const express = require("express");

const app = express();
const PORT = process.env.PORT || 3000;
const leaked = [];

function log(level, msg) {
  const line = `${new Date().toISOString()} [${level}] ${msg}`;
  (level === "ERROR" ? console.error : console.log)(line);
}

function memoryMb() {
  return Math.round(process.memoryUsage().rss / 1024 / 1024);
}

function leak(mb) {
  const chunk = Buffer.alloc(mb * 1024 * 1024, 1); // filled so the pages are really used
  leaked.push(chunk);
}

// request logging + optional per-request leak
app.use((req, res, next) => {
  const t0 = Date.now();
  res.on("finish", () => {
    log(res.statusCode >= 500 ? "ERROR" : "INFO",
      `${req.method} ${req.originalUrl} ${res.statusCode} ${Date.now() - t0}ms rss=${memoryMb()}MB`);
  });
  const perReq = Number(process.env.LEAK_PER_REQUEST_MB || 0);
  if (perReq > 0) leak(perReq);
  next();
});

// "Database" access. The demo doesn't run a real DB; it only needs a configured connection string.
function getOrders() {
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error("DATABASE_URL is not set — cannot connect to the orders database");
  }
  if (!/^(postgres|postgresql|mysql|mongodb)(\+srv)?:\/\//.test(url)) {
    throw new Error("DATABASE_URL is invalid (expected postgres://, mysql:// or mongodb://)");
  }
  return [
    { id: 1001, item: "کتاب", price: 250000 },
    { id: 1002, item: "هدفون", price: 1850000 },
    { id: 1003, item: "ماگ", price: 190000 },
  ];
}

app.get("/health", (req, res) => res.json({ ok: true, rss_mb: memoryMb() }));

app.get("/", (req, res, next) => {
  try {
    const orders = getOrders();
    res.send(`<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">
<title>فروشگاه نمونه</title>
<body style="font-family:Tahoma,sans-serif;max-width:640px;margin:40px auto">
<h1>🛍️ فروشگاه نمونه</h1><p>آخرین سفارش‌ها:</p>
<ul>${orders.map((o) => `<li>#${o.id} — ${o.item} — ${o.price.toLocaleString("fa-IR")} تومان</li>`).join("")}</ul>
<p style="color:#888">rss: ${memoryMb()}MB</p></body></html>`);
  } catch (err) {
    next(err);
  }
});

app.get("/api/orders", (req, res, next) => {
  try {
    res.json({ orders: getOrders() });
  } catch (err) {
    next(err);
  }
});

app.get("/api/leak", (req, res) => {
  const mb = Math.min(Number(req.query.mb || 50), 400);
  leak(mb);
  log("WARN", `leak endpoint retained ${mb}MB (total chunks=${leaked.length}, rss=${memoryMb()}MB)`);
  res.json({ retained_mb: mb, chunks: leaked.length, rss_mb: memoryMb() });
});

app.get("/api/cpu", (req, res) => {
  const ms = Math.min(Number(req.query.ms || 2000), 20000);
  const end = Date.now() + ms;
  let x = 0;
  while (Date.now() < end) x += Math.sqrt(Math.random());
  res.json({ burned_ms: ms, x: Math.round(x) });
});

// error handler -> HTTP 500 with a stack trace in the logs
app.use((err, req, res, next) => { // eslint-disable-line no-unused-vars
  log("ERROR", `${err.message}\n${err.stack}`);
  res.status(500).send("500 — Internal Server Error");
});

app.listen(PORT, () => log("INFO", `demo-shop listening on port ${PORT}`));
