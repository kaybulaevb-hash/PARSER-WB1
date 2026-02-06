const { verifyInitData } = require("./_lib/telegram");
const { getValue } = require("./_lib/storage");
const { fetchLatestReviews, fetchAllQuestions } = require("./_lib/wb");
const { toCsv } = require("./_lib/csv");

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  const { initData, nmId, type } = req.body || {};
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
  if (!type || !["reviews", "questions"].includes(type)) {
    res.status(400).json({ error: "Missing or invalid type." });
    return;
  }

  try {
    const rows =
      type === "reviews"
        ? await fetchLatestReviews(wbToken, Number(nmId), 500)
        : await fetchAllQuestions(wbToken, Number(nmId), 10000);
    const csv = toCsv(rows);

    const form = new FormData();
    form.append("chat_id", String(user.id));
    form.append("caption", `${type === "reviews" ? "Отзывы" : "Вопросы"} по nmID ${nmId}`);
    form.append(
      "document",
      new Blob([csv], { type: "text/csv" }),
      `${type}_${nmId}.csv`
    );

    const response = await fetch(`https://api.telegram.org/bot${botToken}/sendDocument`, {
      method: "POST",
      body: form,
    });
    const payload = await response.json();
    if (!payload.ok) {
      throw new Error(payload.description || "Telegram sendDocument error");
    }

    res.status(200).json({ ok: true });
  } catch (err) {
    res.status(500).json({ error: err.message || "Send error" });
  }
};
