const memoryStore = new Map();

function hasKV() {
  return Boolean(process.env.KV_REST_API_URL && process.env.KV_REST_API_TOKEN);
}

async function kvCommand(command) {
  const url = process.env.KV_REST_API_URL;
  const token = process.env.KV_REST_API_TOKEN;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(command),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`KV error ${response.status}: ${text}`);
  }
  const payload = await response.json();
  return payload.result;
}

async function getValue(key) {
  if (!hasKV()) {
    return memoryStore.get(key) ?? null;
  }
  try {
    const result = await kvCommand(["GET", key]);
    return result ?? null;
  } catch (err) {
    console.warn("KV GET failed, fallback to memory:", err.message);
    return memoryStore.get(key) ?? null;
  }
}

async function setValue(key, value) {
  if (!hasKV()) {
    memoryStore.set(key, value);
    return true;
  }
  try {
    await kvCommand(["SET", key, value]);
    return true;
  } catch (err) {
    console.warn("KV SET failed, fallback to memory:", err.message);
    memoryStore.set(key, value);
    return false;
  }
}

async function deleteValue(key) {
  if (!hasKV()) {
    return memoryStore.delete(key);
  }
  try {
    await kvCommand(["DEL", key]);
    return true;
  } catch (err) {
    console.warn("KV DEL failed, fallback to memory:", err.message);
    return memoryStore.delete(key);
  }
}

module.exports = {
  getValue,
  setValue,
  deleteValue,
  hasKV,
};
