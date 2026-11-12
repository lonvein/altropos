(() => {
  "use strict";

  const ASK_STATUS = {
    answered: {
      label: "Полный ответ",
      className: "status-answered",
      title: "Ответ подтверждён источниками",
      description: "Полнота ответа проверена. У каждого вывода есть ссылка на подтверждение.",
    },
    partial: {
      label: "Частичный ответ",
      className: "status-partial",
      title: "Частичный ответ",
      description:
        "Есть проверенные выводы, но полнота не подтверждена. Это не полная инструкция.",
    },
    not_found: {
      label: "Информация не найдена",
      className: "status-not_found",
      title: "Информация не найдена",
      description: "В доступных документах недостаточно оснований для ответа.",
    },
    ambiguous: {
      label: "Нужно уточнение",
      className: "status-ambiguous",
      title: "Нужно уточнить вопрос",
      description: "Вопрос допускает разные толкования; без уточнения ответ будет ненадёжным.",
    },
    conflict: {
      label: "Противоречие",
      className: "status-conflict",
      title: "Источники противоречат друг другу",
      description:
        "Найдены подтверждённые, но несовместимые основания. Вывод нельзя считать окончательным.",
    },
    unavailable: {
      label: "Ответ недоступен",
      className: "status-unavailable",
      title: "Ответ недоступен",
      description:
        "Сервис не смог подготовить результат. Причина указана в предупреждениях; можно повторить позже.",
    },
  };

  const DOCUMENT_STATUS = {
    queued: { label: "В очереди", className: "status-queued" },
    processing: { label: "Обрабатывается", className: "status-processing" },
    ready: { label: "Готов", className: "status-ready" },
    needs_review: { label: "Требует проверки", className: "status-needs_review" },
    error: { label: "Ошибка", className: "status-error" },
    failed: { label: "Ошибка", className: "status-error" },
    done: { label: "Готово", className: "status-ready" },
    completed: { label: "Готово", className: "status-ready" },
    success: { label: "Готово", className: "status-ready" },
  };

  const TERMINAL_STATUSES = new Set([
    "ready",
    "needs_review",
    "error",
    "failed",
    "done",
    "completed",
    "success",
  ]);

  class ApiError extends Error {
    constructor(message, options = {}) {
      super(message);
      this.name = "ApiError";
      this.status = options.status || 0;
      this.code = options.code || null;
      this.retryable = Boolean(options.retryable);
      this.details = options.details || null;
    }
  }

  function clear(node) {
    if (!node) {
      return;
    }
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function make(tag, className, textValue) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (textValue !== undefined && textValue !== null) {
      node.textContent = String(textValue);
    }
    return node;
  }

  function setText(node, value) {
    if (node) {
      node.textContent = value === undefined || value === null ? "" : String(value);
    }
  }

  function show(node, visible = true) {
    if (node) {
      node.hidden = !visible;
    }
  }

  function cloneTemplate(id) {
    const template = document.getElementById(id);
    if (!template) {
      throw new Error(`Не найден шаблон ${id}`);
    }
    return template.content.cloneNode(true);
  }

  function extractError(payload) {
    let code = null;
    let message = "";
    let retryable = false;
    if (payload && payload.error && typeof payload.error === "object") {
      code = payload.error.code || null;
      message = payload.error.message || "";
      retryable = Boolean(payload.error.retryable);
    }
    if (payload && payload.detail !== undefined) {
      if (typeof payload.detail === "string") {
        message = message || payload.detail;
      } else if (Array.isArray(payload.detail)) {
        message =
          message ||
          payload.detail
            .map((item) => {
              if (typeof item === "string") {
                return item;
              }
              if (item && typeof item === "object") {
                const location = Array.isArray(item.loc) ? item.loc.join(".") : "";
                return [location, item.msg || item.message].filter(Boolean).join(": ");
              }
              return "";
            })
            .filter(Boolean)
            .join("; ");
      } else if (typeof payload.detail === "object") {
        code = code || payload.detail.code || null;
        message = message || payload.detail.message || "";
        retryable = retryable || Boolean(payload.detail.retryable);
      }
    }
    return {
      code,
      message: message || "Сервер вернул ошибку без пояснения.",
      retryable,
    };
  }

  async function readJson(response) {
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("json")) {
      return null;
    }
    try {
      return await response.json();
    } catch {
      return null;
    }
  }

  async function apiFetch(url, options = {}) {
    const { json, headers: suppliedHeaders, ...fetchOptions } = options;
    const headers = new Headers(suppliedHeaders || {});
    if (!headers.has("Accept")) {
      headers.set("Accept", "application/json");
    }
    let body = fetchOptions.body;
    if (json !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(json);
    }
    let response;
    try {
      response = await fetch(url, {
        credentials: "same-origin",
        cache: "no-store",
        ...fetchOptions,
        headers,
        body,
      });
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw error;
      }
      throw new ApiError("Не удалось связаться с сервером. Проверьте соединение.", {
        code: "network",
        details: error,
      });
    }
    const payload = await readJson(response);
    if (!response.ok) {
      const info = extractError(payload);
      throw new ApiError(info.message, {
        status: response.status,
        code: info.code,
        retryable: info.retryable,
        details: payload,
      });
    }
    return payload;
  }

  function friendlyError(error) {
    if (!(error instanceof ApiError)) {
      if (error && error.name === "AbortError") {
        return "Запрос отменён.";
      }
      return error && error.message
        ? error.message
        : "Неизвестная ошибка. Повторите попытку вручную.";
    }
    switch (error.code) {
      case "network":
        return "Не удалось связаться с сервером. Проверьте соединение и повторите вручную.";
      case "model_timeout":
        return "Модель не ответила вовремя. Вопрос сохранён; повторная отправка — только вручную.";
      case "model_unavailable":
        return "Модель или ML-сервис недоступны. Вопрос сохранён; можно повторить позже.";
      case "invalid_model_output":
        return "Модель вернула некорректный ответ. Результат не показан, чтобы не вводить в заблуждение.";
      case "context_exceeded":
        return "Вопрос или найденные материалы не помещаются в контекст модели. Сократите вопрос.";
      case "busy":
        return "Сервис занят обработкой другого запроса. Повторите вручную через некоторое время.";
      case "not_ready":
        return "Нужные документы ещё не готовы к поиску. Дождитесь обработки и повторите.";
      case "index_mismatch":
        return "Индекс не соответствует текущей конфигурации. Нужна проверка администратора.";
      case "invalid_request":
        return error.message || "Некорректный запрос.";
      default:
        break;
    }
    if (error.status === 0) {
      return "Сеть недоступна. Вопрос сохранён; повторите отправку вручную.";
    }
    if (error.status === 504) {
      return "Сервер не успел ответить. Вопрос сохранён; повторите отправку вручную.";
    }
    if (error.status === 503) {
      return "Сервис временно недоступен. Вопрос сохранён; повторите позже.";
    }
    if (error.status === 502) {
      return "Сервис модели вернул ошибку. Вопрос сохранён.";
    }
    if (error.status === 422) {
      return error.message || "Проверьте параметры запроса.";
    }
    if (error.status === 404) {
      return error.message || "Запрошенный объект не найден.";
    }
    if (error.status === 409) {
      return error.message || "Объект ещё не готов или изменился.";
    }
    return error.message || "Сервер вернул ошибку.";
  }

  function formatLocator(locator) {
    if (!locator || typeof locator !== "object") {
      return "";
    }
    if (locator.kind === "pdf") {
      return locator.page ? `PDF, страница ${locator.page}` : "PDF";
    }
    if (locator.kind === "docx") {
      const block = Number.isFinite(Number(locator.block)) ? Number(locator.block) + 1 : 1;
      return `DOCX, блок ${block}`;
    }
    if (locator.kind === "xlsx") {
      const sheet = locator.sheet ? `лист «${locator.sheet}»` : "лист";
      const range = locator.range ? `, диапазон ${locator.range}` : "";
      return `XLSX, ${sheet}${range}`;
    }
    if (locator.kind === "html") {
      const block = Number.isFinite(Number(locator.block)) ? Number(locator.block) + 1 : 1;
      return `HTML, блок ${block}`;
    }
    return "";
  }

  function formatDate(value) {
    if (!value) {
      return "—";
    }
    const raw = String(value);
    const date = /^\d{4}-\d{2}-\d{2}$/.test(raw)
      ? new Date(`${raw}T00:00:00`)
      : new Date(raw);
    if (Number.isNaN(date.getTime())) {
      return raw;
    }
    return new Intl.DateTimeFormat("ru-RU", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(date);
  }

  function formatDateTime(value) {
    if (!value) {
      return "—";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    return new Intl.DateTimeFormat("ru-RU", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) {
      return "—";
    }
    if (bytes < 1024) {
      return `${bytes} Б`;
    }
    if (bytes < 1024 * 1024) {
      return `${(bytes / 1024).toFixed(1)} КБ`;
    }
    return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
  }

  function statusInfo(status, kind = "document") {
    const mapping = kind === "ask" ? ASK_STATUS : DOCUMENT_STATUS;
    return mapping[status] || { label: status || "Неизвестно", className: "status-unknown" };
  }

  function createBadge(status, kind = "document") {
    const info = statusInfo(status, kind);
    return make("span", `badge ${info.className}`, info.label);
  }

  function isTerminalStatus(status) {
    return TERMINAL_STATUSES.has(String(status || ""));
  }

  function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
  }

  function safeApiPath(value, prefixes) {
    if (typeof value !== "string" || !value) {
      return null;
    }
    try {
      const url = new URL(value, window.location.origin);
      if (url.origin !== window.location.origin) {
        return null;
      }
      if (!prefixes.some((prefix) => url.pathname.startsWith(prefix))) {
        return null;
      }
      return `${url.pathname}${url.search}${url.hash}`;
    } catch {
      return null;
    }
  }

  function openDialog(dialog) {
    if (!dialog) {
      return;
    }
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) {
        dialog.showModal();
      }
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function closeDialog(dialog) {
    if (!dialog) {
      return;
    }
    if (typeof dialog.close === "function" && dialog.open) {
      dialog.close();
    } else {
      dialog.removeAttribute("open");
    }
  }

  function bindDialogs() {
    document.addEventListener("click", (event) => {
      const closeButton = event.target.closest("[data-close-dialog]");
      if (closeButton) {
        closeDialog(closeButton.closest("dialog"));
        return;
      }
      if (event.target instanceof HTMLDialogElement) {
        closeDialog(event.target);
      }
    });
  }

  const HEALTH_LABELS = {
    ok: "Работает",
    limited: "Ограниченный режим",
    degraded: "Есть ограничения",
    unavailable: "Недоступно",
    disabled: "Отключено",
    unknown: "Неизвестно",
  };

  const MODE_LABELS = {
    real: "Реальные API",
    synthetic: "Демонстрационные заглушки",
    partial: "Часть API ограничена",
    unavailable: "API не подключены",
    degraded: "Ограниченный режим",
  };

  function healthLabel(status) {
    return HEALTH_LABELS[status] || status || "Неизвестно";
  }

  function updateHealthUi(data) {
    const button = document.getElementById("health-button");
    const pillText = document.getElementById("health-pill-text");
    const status = data && data.status ? data.status : "degraded";
    if (button) {
      button.classList.remove("is-ok", "is-limited", "is-degraded");
      button.classList.add(
        status === "ok" ? "is-ok" : status === "limited" ? "is-limited" : "is-degraded",
      );
    }
    if (pillText) {
      setText(pillText, `Состояние: ${healthLabel(status).toLowerCase()}`);
    }
    if (!data) {
      return;
    }
    const application = data.application || {};
    setText(document.getElementById("health-summary"), healthLabel(data.status));
    setText(document.getElementById("health-app"), healthLabel(application.status));
    setText(
      document.getElementById("health-mode"),
      `Данные: ${MODE_LABELS[application.data_mode] || application.data_mode || "неизвестно"}`,
    );
    setText(document.getElementById("health-ml"), healthLabel(data.ml && data.ml.status));
    setText(document.getElementById("health-llm"), healthLabel(data.llm && data.llm.status));

    const reasons = document.getElementById("health-reasons");
    if (reasons) {
      clear(reasons);
      const items = Array.isArray(data.reasons) ? data.reasons : [];
      if (items.length === 0) {
        reasons.appendChild(make("li", "", "Ограничений не обнаружено."));
      } else {
        items.forEach((reason) => reasons.appendChild(make("li", "", reason)));
      }
    }
  }

  async function refreshHealth() {
    try {
      const data = await apiFetch("/health", { method: "GET" });
      updateHealthUi(data);
    } catch (error) {
      updateHealthUi({
        status: "degraded",
        application: { status: "degraded", data_mode: "unavailable" },
        ml: { status: "unknown" },
        llm: { status: "unknown" },
        reasons: [friendlyError(error)],
      });
    }
  }

  function init() {
    bindDialogs();
    const button = document.getElementById("health-button");
    if (button) {
      button.addEventListener("click", () => {
        openDialog(document.getElementById("health-dialog"));
        refreshHealth();
      });
    }
    refreshHealth();
    window.setInterval(refreshHealth, 60_000);
  }

  window.Altropos = {
    ApiError,
    ASK_STATUS,
    DOCUMENT_STATUS,
    apiFetch,
    clear,
    cloneTemplate,
    closeDialog,
    createBadge,
    delay,
    formatBytes,
    formatDate,
    formatDateTime,
    formatLocator,
    friendlyError,
    isTerminalStatus,
    make,
    openDialog,
    refreshHealth,
    safeApiPath,
    setText,
    show,
    statusInfo,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
