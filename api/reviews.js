const { verifyInitData } = require("./_lib/telegram");
const { getValue, setValueIfNotExists } = require("./_lib/storage");
const { fetchLatestReviews } = require("./_lib/wb");
const { toCsv } = require("./_lib/csv");

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  const { initData, nmId } = req.body || {};
  const botToken = process.env.TELEGRAM_BOT_TOKEN;
  const result = verifyInitData(initData, botToken);
  if (!result.ok) {
    res.status(401).json({ error: result.error });
    return;
  }

  const user = result.user || {};
  if (!user.id) {
    res.status(400).json({ error: "Missing user in initData." });
    return;
  }

  const wbToken = await getValue(`user:${user.id}:wb_token`);
  if (!wbToken) {
    res.status(401).json({ error: "WB token not set." });
    return;
  }
  if (!nmId) {
    res.status(400).json({ error: "Missing nmId." });
    return;
  }

  const lockTtlSeconds = Number(process.env.WB_REQUEST_LOCK_SECONDS || 25);
  const lockAcquired = await setValueIfNotExists(
    `lock:${user.id}:reviews:${Number(nmId)}`,
    "1",
    lockTtlSeconds
  );
  if (!lockAcquired) {
    res.status(429).json({
      error: `Подождите ${lockTtlSeconds} сек. Предыдущая выгрузка еще обрабатывается.`,
    });
    return;
  }

  try {
    const rows = await fetchLatestReviews(wbToken, Number(nmId), 500);
    const csv = toCsv(rows);
    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="reviews_${nmId}.csv"`
    );
    res.status(200).send(csv);
  } catch (err) {
    const message = err.message || "WB API error";
    const isRateLimit = message.includes("WB API 429");
    res.status(isRateLimit ? 429 : 500).json({ error: message });
  }
};
