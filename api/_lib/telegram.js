const crypto = require("crypto");

function parseInitData(initData) {
  const params = new URLSearchParams(initData || "");
  const data = {};
  for (const [key, value] of params.entries()) {
    data[key] = value;
  }
  return data;
}

function buildDataCheckString(data) {
  const keys = Object.keys(data)
    .filter((key) => key !== "hash")
    .sort();
  return keys.map((key) => `${key}=${data[key]}`).join("\n");
}

function verifyInitData(initData, botToken, maxAgeSeconds = 86400) {
  if (!initData || !botToken) {
    return { ok: false, error: "Missing initData or bot token." };
  }

  const data = parseInitData(initData);
  if (!data.hash) {
    return { ok: false, error: "Missing hash in initData." };
  }

  const dataCheckString = buildDataCheckString(data);
  const secretKey = crypto
    .createHmac("sha256", "WebAppData")
    .update(botToken)
    .digest();
  const computedHash = crypto
    .createHmac("sha256", secretKey)
    .update(dataCheckString)
    .digest("hex");

  if (computedHash !== data.hash) {
    return { ok: false, error: "Invalid initData signature." };
  }

  const authDate = Number(data.auth_date || 0);
  if (maxAgeSeconds && authDate) {
    const now = Math.floor(Date.now() / 1000);
    if (now - authDate > maxAgeSeconds) {
      return { ok: false, error: "initData expired." };
    }
  }

  let user = null;
  try {
    user = data.user ? JSON.parse(data.user) : null;
  } catch (err) {
    return { ok: false, error: "Invalid user JSON." };
  }

  return { ok: true, data, user };
}

module.exports = {
  parseInitData,
  verifyInitData,
};
