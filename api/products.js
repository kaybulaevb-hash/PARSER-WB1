const { verifyInitData } = require("./_lib/telegram");
const { getValue } = require("./_lib/storage");
const { fetchProductCards } = require("./_lib/wb");

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  const { initData } = req.body || {};
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

  try {
    const products = await fetchProductCards(wbToken, {
      pageSize: 100,
      maxItems: Number(process.env.WB_PRODUCTS_MAX_ITEMS || 500),
    });
    res.status(200).json({ products });
  } catch (err) {
    const message = err.message || "WB API error";
    const needsToken = /WB API (401|403)|Missing WB token/.test(message);
    res.status(needsToken ? 400 : 500).json({
      error: needsToken
        ? "Сохранённый WB токен больше не подходит. Загрузите новый токен с правами «Вопросы и отзывы» и «Контент»."
        : message,
      needsToken,
    });
  }
};
