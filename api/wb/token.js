const { verifyInitData } = require("../_lib/telegram");
const { setValue } = require("../_lib/storage");
const { fetchProductCards, normalizeWbToken } = require("../_lib/wb");

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

  const normalizedToken = normalizeWbToken(wbToken);
  if (!normalizedToken) {
    res.status(400).json({ error: "Пустой WB токен." });
    return;
  }

  try {
    await fetchProductCards(normalizedToken, { pageSize: 1, maxItems: 1 });
    const stored = await setValue(`user:${user.id}:wb_token`, normalizedToken);
    if (!stored) {
      res.status(503).json({
        error: "Не удалось сохранить токен в хранилище. Проверьте Vercel KV и повторите.",
      });
      return;
    }
    res.status(200).json({ ok: true });
  } catch (err) {
    const message = err.message || "WB API error";
    const isAuthError = /WB API (401|403)/.test(message);
    res.status(isAuthError ? 400 : 500).json({
      error: isAuthError
        ? "WB токен не прошёл проверку. Нужен токен с правами «Вопросы и отзывы» и «Контент»."
        : message,
    });
  }
};
