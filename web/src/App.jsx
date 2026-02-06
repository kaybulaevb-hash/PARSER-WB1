import { useEffect, useMemo, useState } from "react";

const apiPost = async (url, body) => {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (contentType.includes("application/json")) {
    payload = await response.json();
  } else {
    payload = await response.text();
  }
  return { ok: response.ok, status: response.status, payload };
};

const downloadBlob = (blob, filename) => {
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
};

export default function App() {
  const telegram = useMemo(() => window.Telegram?.WebApp, []);
  const initData = telegram?.initData || "";

  const [userId, setUserId] = useState(null);
  const [wbToken, setWbToken] = useState("");
  const [products, setProducts] = useState([]);
  const [selected, setSelected] = useState(null);
  const [catalogView, setCatalogView] = useState("grid");
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [needsToken, setNeedsToken] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (telegram?.ready) telegram.ready();
  }, [telegram]);

  useEffect(() => {
    if (!initData) {
      setError("Откройте Mini App внутри Telegram.");
      return;
    }
    const run = async () => {
      setLoading(true);
      const auth = await apiPost("/api/auth/telegram", { initData });
      if (!auth.ok) {
        setError(auth.payload?.error || "Ошибка авторизации Telegram.");
        setLoading(false);
        return;
      }
      setUserId(auth.payload.telegram_id);
      await loadProducts(initData);
      setLoading(false);
    };
    run();
  }, [initData]);

  const loadProducts = async (initDataValue = initData) => {
    setError("");
    setNotice("");
    const res = await apiPost("/api/products", { initData: initDataValue });
    if (!res.ok) {
      if (res.status === 401) {
        setNeedsToken(true);
        setProducts([]);
        return;
      }
      setError(res.payload?.error || "Ошибка загрузки товаров.");
      return;
    }
    setNeedsToken(false);
    setProducts(res.payload.products || []);
  };

  const handleSaveToken = async () => {
    if (!wbToken.trim()) {
      setError("Введите WB токен.");
      return;
    }
    setNotice("");
    setLoading(true);
    const res = await apiPost("/api/wb/token", { initData, wbToken: wbToken.trim() });
    if (!res.ok) {
      setError(res.payload?.error || "Не удалось сохранить токен.");
      setLoading(false);
      return;
    }
    setWbToken("");
    await loadProducts(initData);
    setLoading(false);
  };

  const handleDownload = async (type) => {
    if (!selected) return;
    setError("");
    setNotice("");
    setLoading(true);
    const response = await fetch(`/api/${type}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initData, nmId: selected.nm_id }),
    });
    if (!response.ok) {
      const payload = await response.json();
      setError(payload?.error || "Ошибка выгрузки.");
      setLoading(false);
      return;
    }
    const blob = await response.blob();
    downloadBlob(blob, `${type}_${selected.nm_id}.csv`);
    setLoading(false);
  };

  const handleSendToChat = async (type) => {
    if (!selected) return;
    setError("");
    setNotice("");
    setLoading(true);
    const res = await apiPost("/api/send", {
      initData,
      nmId: selected.nm_id,
      type,
    });
    if (!res.ok) {
      setError(res.payload?.error || "Не удалось отправить файл в чат.");
      setLoading(false);
      return;
    }
    setNotice("Файл отправлен в чат с ботом.");
    setLoading(false);
  };

  const filteredProducts = useMemo(() => {
    const query = searchQuery.trim().toLowerCase();
    if (!query) return products;
    return products.filter((product) => {
      const title = String(product.title || "").toLowerCase();
      const vendor = String(product.vendor_code || "").toLowerCase();
      const nmId = String(product.nm_id || "");
      return (
        title.includes(query) ||
        vendor.includes(query) ||
        nmId.includes(query)
      );
    });
  }, [products, searchQuery]);

  return (
    <div className="app">
      <header className="header">
        <div className="title">Парсер WB</div>
        <div className="subtitle">Telegram ID: {userId ?? "..."}</div>
      </header>

      {error && <div className="alert">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      {needsToken && (
        <section className="card">
          <h2>Подключить WB токен</h2>
          <p>В токене должны быть права: «Вопросы и отзывы» и «Контент».</p>
          <div className="row">
            <input
              value={wbToken}
              onChange={(e) => setWbToken(e.target.value)}
              placeholder="WB_API_TOKEN=..."
            />
            <button onClick={handleSaveToken} disabled={loading}>
              Сохранить
            </button>
          </div>
        </section>
      )}

      {!needsToken && !selected && (
        <section>
          <div className="toolbar">
            <div className="view-toggle">
              <button
                className={catalogView === "grid" ? "active" : ""}
                onClick={() => setCatalogView("grid")}
                disabled={loading}
              >
                Плитка
              </button>
              <button
                className={catalogView === "list" ? "active" : ""}
                onClick={() => setCatalogView("list")}
                disabled={loading}
              >
                Список
              </button>
            </div>
            <button onClick={() => loadProducts()} disabled={loading}>
              Обновить список
            </button>
          </div>
          <div className="search">
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Поиск по названию, nmID или артикулу продавца"
            />
          </div>
          <div className={catalogView === "grid" ? "grid" : "list"}>
            {filteredProducts.map((product) => (
              <div
                key={product.nm_id}
                className={catalogView === "grid" ? "product-card" : "product-row"}
                onClick={() => setSelected(product)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter") setSelected(product);
                }}
              >
                {product.photo_url ? (
                  <img src={product.photo_url} alt={product.title} />
                ) : (
                  <div className="placeholder">Нет фото</div>
                )}
                <div className="product-meta">
                  <div className="product-title">{product.title}</div>
                  <div className="product-sub">
                    WB {product.nm_id} · {product.vendor_code}
                  </div>
                </div>
                <button className="open-btn" onClick={() => setSelected(product)}>
                  Открыть
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      {!needsToken && selected && (
        <section className="card">
          <button className="back" onClick={() => setSelected(null)}>
            ← К списку
          </button>
          <div className="detail">
            {selected.photo_url ? (
              <img src={selected.photo_url} alt={selected.title} />
            ) : (
              <div className="placeholder">Нет фото</div>
            )}
            <div className="detail-info">
              <h2>{selected.title}</h2>
              <div>WB nmID: {selected.nm_id}</div>
              <div>Артикул продавца: {selected.vendor_code}</div>
              <div className="actions">
                <button onClick={() => handleDownload("reviews")} disabled={loading}>
                  Скачать отзывы CSV
                </button>
                <button onClick={() => handleDownload("questions")} disabled={loading}>
                  Скачать вопросы CSV
                </button>
                <button onClick={() => handleSendToChat("reviews")} disabled={loading}>
                  Отправить отзывы в чат
                </button>
                <button onClick={() => handleSendToChat("questions")} disabled={loading}>
                  Отправить вопросы в чат
                </button>
              </div>
            </div>
          </div>
        </section>
      )}

      {loading && <div className="loading">Загрузка...</div>}
    </div>
  );
}
