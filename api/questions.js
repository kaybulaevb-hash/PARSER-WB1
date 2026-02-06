const { verifyInitData } = require("./_lib/telegram");
const { getValue } = require("./_lib/storage");
const { fetchAllQuestions } = require("./_lib/wb");
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

  try {
    const rows = await fetchAllQuestions(wbToken, Number(nmId), 10000);
    const csv = toCsv(rows);
    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="questions_${nmId}.csv"`
    );
    res.status(200).send(csv);
  } catch (err) {
    res.status(500).json({ error: err.message || "WB API error" });
  }
};
