const FEEDBACK_BASE =
  process.env.WB_FEEDBACK_BASE_URL || "https://feedbacks-api.wildberries.ru";
const CONTENT_BASE =
  process.env.WB_CONTENT_BASE_URL || "https://content-api.wildberries.ru";
const WB_MAX_RETRIES = Number(process.env.WB_MAX_RETRIES || 4);
const WB_RETRY_BASE_MS = Number(process.env.WB_RETRY_BASE_MS || 800);

function normalizeWbToken(rawToken) {
  if (typeof rawToken !== "string") {
    return "";
  }

  let token = rawToken.trim();
  if (!token) {
    return "";
  }

  const envLikeMatch = token.match(/^[A-Z0-9_]*TOKEN\s*=\s*(.+)$/i);
  if (envLikeMatch) {
    token = envLikeMatch[1].trim();
  }

  if (
    (token.startsWith('"') && token.endsWith('"')) ||
    (token.startsWith("'") && token.endsWith("'"))
  ) {
    token = token.slice(1, -1).trim();
  }

  return token;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getRetryDelayMs(response, attempt) {
  const retryAfter = response.headers.get("retry-after");
  if (retryAfter) {
    const asNumber = Number(retryAfter);
    if (Number.isFinite(asNumber) && asNumber >= 0) {
      return asNumber * 1000;
    }
  }
  return Math.min(WB_RETRY_BASE_MS * (2 ** attempt), 10_000);
}

function parseWbError(status, text) {
  let detail = text;
  try {
    const parsed = JSON.parse(text);
    detail =
      parsed?.statusText ||
      parsed?.title ||
      parsed?.detail ||
      parsed?.errorText ||
      text;
  } catch (_) {
    // Keep original text when response is not JSON.
  }
  if (status === 429) {
    return new Error("WB API 429: Слишком много запросов. Подождите 10-30 секунд и повторите.");
  }
  return new Error(`WB API ${status}: ${detail}`);
}

async function fetchJson(url, options = {}) {
  for (let attempt = 0; attempt <= WB_MAX_RETRIES; attempt += 1) {
    const response = await fetch(url, options);
    if (response.ok) {
      const payload = await response.json();
      if (payload && payload.error === true) {
        throw new Error(payload.errorText || "WB API error");
      }
      return payload;
    }

    const text = await response.text();
    const retriable = response.status === 429 || response.status >= 500;
    if (!retriable || attempt === WB_MAX_RETRIES) {
      throw parseWbError(response.status, text);
    }

    await sleep(getRetryDelayMs(response, attempt));
  }

  throw new Error("WB API error");
}

function appendPhotoVersion(url, card) {
  const versionRaw =
    card.updatedAt || card.updateAt || card.modifiedAt || card.createdAt;
  if (!versionRaw) return url;
  const version = String(versionRaw).trim();
  if (!version) return url;
  const joiner = url.includes("?") ? "&" : "?";
  return `${url}${joiner}wbv=${encodeURIComponent(version)}`;
}

function selectPhotoUrl(card) {
  const keyScores = {
    big: 60,
    c516x688: 55,
    c246x328: 50,
    tm: 45,
    url: 40,
  };

  const collect = (items, baseScore) => {
    const candidates = [];
    if (!Array.isArray(items)) return candidates;
    items.forEach((item, index) => {
      if (typeof item === "string" && item.startsWith("http")) {
        candidates.push([baseScore, 0, 0, -index, item]);
        return;
      }
      if (item && typeof item === "object") {
        const isMain = item.isMain === true ? 1 : 0;
        for (const [key, score] of Object.entries(keyScores)) {
          const value = item[key];
          if (typeof value === "string" && value.startsWith("http")) {
            candidates.push([baseScore, isMain, score, -index, value]);
          }
        }
      }
    });
    return candidates;
  };

  const candidates = [
    ...collect(card.photos, 30),
    ...collect(card.mediaFiles, 20),
    ...collect(card.images, 10),
  ];

  if (candidates.length > 0) {
    candidates.sort((a, b) => {
      for (let i = 0; i < 4; i += 1) {
        if (a[i] !== b[i]) return b[i] - a[i];
      }
      return 0;
    });
    return appendPhotoVersion(candidates[0][4], card);
  }

  for (const key of ["photo", "image", "imageUrl"]) {
    const value = card[key];
    if (typeof value === "string" && value.startsWith("http")) {
      return appendPhotoVersion(value, card);
    }
  }
  return null;
}

function normalizeProducts(cards) {
  const seen = new Set();
  const products = [];

  for (const card of cards) {
    const nmRaw = card.nmID ?? card.nmId;
    const nmId = Number(nmRaw);
    if (!Number.isFinite(nmId)) continue;
    if (seen.has(nmId)) continue;
    seen.add(nmId);

    products.push({
      nm_id: nmId,
      title: String(card.title || card.subjectName || "Без названия").trim(),
      vendor_code: String(card.vendorCode || "-").trim() || "-",
      photo_url: selectPhotoUrl(card),
    });
  }

  products.sort((a, b) => {
    const titleCompare = a.title.localeCompare(b.title, "ru", { sensitivity: "base" });
    if (titleCompare !== 0) return titleCompare;
    return a.nm_id - b.nm_id;
  });
  return products;
}

async function fetchProductCards(token, { pageSize = 100, maxItems = 500 } = {}) {
  const authToken = normalizeWbToken(token);
  if (!authToken) {
    throw new Error("Missing WB token.");
  }

  let cursorUpdatedAt = null;
  let cursorNmId = null;
  const cards = [];

  while (true) {
    const cursor = { limit: Math.min(pageSize, 100) };
    if (cursorUpdatedAt && cursorNmId) {
      cursor.updatedAt = cursorUpdatedAt;
      cursor.nmID = cursorNmId;
    }

    const body = {
      settings: {
        sort: { ascending: false },
        filter: { withPhoto: -1 },
        cursor,
      },
    };

    const payload = await fetchJson(`${CONTENT_BASE}/content/v2/get/cards/list`, {
      method: "POST",
      headers: {
        Authorization: authToken,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });

    const batch = Array.isArray(payload.cards) ? payload.cards : [];
    cards.push(...batch);

    if (maxItems && cards.length >= maxItems) {
      break;
    }
    if (batch.length < cursor.limit) {
      break;
    }
    const cursorPayload = payload.cursor || {};
    if (!cursorPayload.updatedAt || !cursorPayload.nmID) {
      break;
    }
    cursorUpdatedAt = String(cursorPayload.updatedAt);
    cursorNmId = Number(cursorPayload.nmID);
    if (!cursorNmId) break;
  }

  return normalizeProducts(cards.slice(0, maxItems));
}

async function fetchFeedbacks(token, { nmId, isAnswered, take = 1000, skip = 0, order = "dateDesc" }) {
  const authToken = normalizeWbToken(token);
  if (!authToken) {
    throw new Error("Missing WB token.");
  }
  const params = new URLSearchParams({
    isAnswered: String(isAnswered),
    take: String(take),
    skip: String(skip),
    order,
  });
  if (nmId) params.set("nmId", String(nmId));
  const url = `${FEEDBACK_BASE}/api/v1/feedbacks?${params.toString()}`;
  const payload = await fetchJson(url, {
    headers: { Authorization: authToken, Accept: "application/json" },
  });
  const data = payload.data || payload;
  return Array.isArray(data.feedbacks) ? data.feedbacks : Array.isArray(data) ? data : [];
}

async function fetchQuestions(token, { nmId, isAnswered, take = 1000, skip = 0, order = "dateDesc" }) {
  const authToken = normalizeWbToken(token);
  if (!authToken) {
    throw new Error("Missing WB token.");
  }
  const params = new URLSearchParams({
    isAnswered: String(isAnswered),
    take: String(take),
    skip: String(skip),
    order,
  });
  if (nmId) params.set("nmId", String(nmId));
  const url = `${FEEDBACK_BASE}/api/v1/questions?${params.toString()}`;
  const payload = await fetchJson(url, {
    headers: { Authorization: authToken, Accept: "application/json" },
  });
  const data = payload.data || payload;
  return Array.isArray(data.questions) ? data.questions : Array.isArray(data) ? data : [];
}

function parseDate(value) {
  if (!value) return 0;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 0;
  return date.getTime();
}

function latestByDate(rows, limit) {
  const map = new Map();
  const withId = [];
  const withoutId = [];

  for (const row of rows) {
    if (row && row.id != null) {
      map.set(String(row.id), row);
    } else {
      withoutId.push(row);
    }
  }
  for (const value of map.values()) withId.push(value);

  const all = withId.concat(withoutId);
  all.sort((a, b) => {
    const aDate =
      parseDate(a.createdDate) ||
      parseDate(a.createdAt) ||
      parseDate(a.date) ||
      parseDate(a.created) ||
      parseDate(a.updatedDate);
    const bDate =
      parseDate(b.createdDate) ||
      parseDate(b.createdAt) ||
      parseDate(b.date) ||
      parseDate(b.created) ||
      parseDate(b.updatedDate);
    return bDate - aDate;
  });
  return limit ? all.slice(0, limit) : all;
}

async function fetchLatestReviews(token, nmId, limit = 500) {
  const take = Math.min(Math.max(limit, 1), 500);
  const [unanswered, answered] = await Promise.all([
    fetchFeedbacks(token, { nmId, isAnswered: false, take, skip: 0, order: "dateDesc" }),
    fetchFeedbacks(token, { nmId, isAnswered: true, take, skip: 0, order: "dateDesc" }),
  ]);
  return latestByDate(unanswered.concat(answered), limit);
}

async function fetchAllQuestions(token, nmId, limit = 10000) {
  const take = 1000;
  let skip = 0;
  const items = [];
  while (items.length < limit) {
    const batch = await fetchQuestions(token, {
      nmId,
      isAnswered: false,
      take,
      skip,
      order: "dateDesc",
    });
    if (batch.length === 0) break;
    items.push(...batch);
    if (batch.length < take) break;
    skip += batch.length;
    if (skip > 10000) break;
  }
  // fetch answered questions too
  skip = 0;
  while (items.length < limit) {
    const batch = await fetchQuestions(token, {
      nmId,
      isAnswered: true,
      take,
      skip,
      order: "dateDesc",
    });
    if (batch.length === 0) break;
    items.push(...batch);
    if (batch.length < take) break;
    skip += batch.length;
    if (skip > 10000) break;
  }
  return latestByDate(items, limit);
}

module.exports = {
  normalizeWbToken,
  fetchProductCards,
  fetchLatestReviews,
  fetchAllQuestions,
};
