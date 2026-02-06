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
  const [loading, setLoading] = useState(false);
  const [needsToken, setNeedsToken] = useState(false);
  const [error, setError] = useState("");

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

  return (
    <div className="app">
      <header className="header">
        <div className="title">WB Mini App</div>
        <div className="subtitle">Telegram ID: {userId ?? "..."}</div>
      </header>

      {error && <div className="alert">{error}</div>}

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
            <button onClick={() => loadProducts()} disabled={loading}>
              Обновить список
            </button>
          </div>
          <div className="grid">
            {products.map((product) => (
              <div key={product.nm_id} className="product-card">
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
                <button onClick={() => setSelected(product)}>Открыть</button>
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
              </div>
            </div>
          </div>
        </section>
      )}

      {loading && <div className="loading">Загрузка...</div>}
    </div>
  );
}
