module.exports = async (req, res) => {
  const hasBotToken = Boolean(process.env.TELEGRAM_BOT_TOKEN);
  const hasKV = Boolean(process.env.KV_REST_API_URL && process.env.KV_REST_API_TOKEN);
  res.status(200).json({
    ok: true,
    has_bot_token: hasBotToken,
    has_kv: hasKV,
    node_env: process.env.NODE_ENV || "unknown",
  });
};
