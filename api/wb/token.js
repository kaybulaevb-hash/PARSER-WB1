const { verifyInitData } = require("../_lib/telegram");
const { setValue } = require("../_lib/storage");

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  const { initData, wbToken } = req.body || {};
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
  if (!wbToken || typeof wbToken !== "string") {
    res.status(400).json({ error: "Missing WB token." });
    return;
  }

  await setValue(`user:${user.id}:wb_token`, wbToken.trim());
  res.status(200).json({ ok: true });
};
