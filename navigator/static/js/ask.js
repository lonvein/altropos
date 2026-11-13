(() => {
  "use strict";

  const A = window.Altropos;
  const form = document.getElementById("ask-form");
  if (!form) {
    return;
  }

  const question = document.getElementById("question");
  const asOf = document.getElementById("as-of");
  const submitButton = document.getElementById("ask-submit");
  const submitLabel = submitButton.querySelector(".button__label");
  const submitSpinner = submitButton.querySelector(".spinner");
  const clearButton = document.getElementById("ask-clear");
  const state = document.getElementById("ask-state");
  const fieldError = document.getElementById("ask-field-error");
  const requestError = document.getElementById("ask-request-error");
  const requestErrorText = document.getElementById("ask-request-error-text");

  const answerRegion = document.getElementById("answer-region");
  const answerStatus = document.getElementById("answer-status");
  const answerStatusBadge = document.getElementById("answer-status-badge");
  const answerStatusTitle = document.getElementById("answer-status-title");
  const answerStatusDescription = document.getElementById("answer-status-description");

  const clarificationPanel = document.getElementById("clarification-panel");
  const clarificationText = document.getElementById("clarification-text");
  const warningsPanel = document.getElementById("warnings-panel");
  const warningsList = document.getElementById("warnings-list");

  const summarySection = document.getElementById("summary-section");
  const summaryList = document.getElementById("summary-list");
  const stepsSection = document.getElementById("steps-section");
  const stepsList = document.getElementById("steps-list");
  const sourcesSection = document.getElementById("sources-section");
  const sourcesList = document.getElementById("sources-list");
  const answerMeta = document.getElementById("answer-meta");
  const metaMode = document.getElementById("meta-mode");
  const metaAnswerMode = document.getElementById("meta-answer-mode");
  const metaVerification = document.getElementById("meta-verification");
  const metaTimings = document.getElementById("meta-timings");

  const sourceDialog = document.getElementById("source-dialog");
  const sourceTitle = document.getElementById("source-title");
  const sourceLocator = document.getElementById("source-locator");
  const sourceQuote = document.getElementById("source-quote");
  const sourceOriginal = document.getElementById("source-original");
  const sourceNewTab = document.getElementById("source-new-tab");
  const sourceFrame = document.getElementById("source-frame");
  const sourceMessage = document.getElementById("source-message");

  const draftKey = "altropos.ask.draft.v1";
  const jobStateKey = "altropos.ask.job.v1";
  const maxQuestion = Number(form.dataset.maxQuestion) || 4000;
  const timeoutSeconds = Number(form.dataset.timeoutSeconds) || 105;
  const sourceById = new Map();
  let currentController = null;
  let activePollRun = 0;
  let isUnloading = false;
  let userCancelled = false;

  window.addEventListener("pagehide", () => {
    isUnloading = true;
  });
  window.addEventListener("beforeunload", () => {
    isUnloading = true;
  });

  function generateRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return `req-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
  }

  try {
    window.sessionStorage.setItem(
      "altropos.review.returnUrl",
      `${window.location.pathname}${window.location.search}`,
    );
  } catch {
    // Игнорируем недоступность sessionStorage.
  }

  function saveJobState(entry) {
    try {
      window.sessionStorage.setItem(jobStateKey, JSON.stringify(entry));
    } catch {
      // Игнорируем недоступность sessionStorage.
    }
  }

  function readJobState() {
    try {
      const raw = window.sessionStorage.getItem(jobStateKey);
      if (!raw) {
        return null;
      }
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch {
      return null;
    }
  }

  function clearJobState() {
    try {
      window.sessionStorage.removeItem(jobStateKey);
    } catch {
      // Игнорируем ошибку хранилища.
    }
  }

  function saveDraft() {
    try {
      window.localStorage.setItem(
        draftKey,
        JSON.stringify({ question: question.value, asOf: asOf.value }),
      );
    } catch {
      /* Storage may be unavailable; the input still stays in the form. */
    }
  }

  function restoreDraft() {
    try {
      const raw = window.localStorage.getItem(draftKey);
      if (!raw) {
        return;
      }
      const draft = JSON.parse(raw);
      if (typeof draft.question === "string" && !question.value) {
        question.value = draft.question;
      }
      if (typeof draft.asOf === "string" && !asOf.value) {
        asOf.value = draft.asOf;
      }
    } catch {
      /* Ignore malformed or unavailable storage. */
    }
  }

  function clearDraft() {
    try {
      window.localStorage.removeItem(draftKey);
    } catch {
      /* Ignore unavailable storage. */
    }
  }

  function showFieldError(message) {
    A.setText(fieldError, message);
    A.show(fieldError, true);
  }

  function clearFieldError() {
    A.setText(fieldError, "");
    A.show(fieldError, false);
  }

  function showRequestError(message) {
    A.setText(requestErrorText, message);
    A.show(requestError, true);
    A.show(answerRegion, false);
  }

  function clearRequestError() {
    A.setText(requestErrorText, "");
    A.show(requestError, false);
  }

  function setLoading(loading) {
    submitButton.disabled = loading;
    clearButton.disabled = loading;
    question.disabled = loading;
    asOf.disabled = loading;
    A.show(submitSpinner, loading);
    A.setText(submitLabel, loading ? "Ищу ответ…" : "Найти ответ");
    form.setAttribute("aria-busy", loading ? "true" : "false");
    if (loading) {
      A.setText(state, "Ищу ответ… Это может занять до минуты.");
    }
  }

  function clearAnswer() {
    sourceById.clear();
    A.show(answerRegion, false);
    A.show(clarificationPanel, false);
    A.show(warningsPanel, false);
    A.show(summarySection, false);
    A.show(stepsSection, false);
    A.show(sourcesSection, false);
    A.show(answerMeta, false);
    A.clear(warningsList);
    A.clear(summaryList);
    A.clear(stepsList);
    A.clear(sourcesList);
  }

  function quoteMapFor(data) {
    const quotes = new Map();
    const claims = []
      .concat(Array.isArray(data.summary) ? data.summary : [])
      .concat(Array.isArray(data.steps) ? data.steps : []);
    claims.forEach((claim) => {
      const evidence = Array.isArray(claim.evidence) ? claim.evidence : [];
      evidence.forEach((item) => {
        if (
          item &&
          typeof item.fragment_id === "string" &&
          typeof item.quote === "string" &&
          !quotes.has(item.fragment_id)
        ) {
          quotes.set(item.fragment_id, item.quote);
        }
      });
    });
    return quotes;
  }

  function renderClaims(list, claims, quotes) {
    A.clear(list);
    const items = Array.isArray(claims) ? claims : [];
    items.forEach((claim, index) => {
      const fragment = A.cloneTemplate("claim-template");
      const number = fragment.querySelector(".claim__number");
      const textNode = fragment.querySelector(".claim__text");
      const evidenceBox = fragment.querySelector(".claim__evidence");
      A.setText(number, `${index + 1}.`);
      A.setText(textNode, claim && claim.text ? claim.text : "Утверждение без текста.");

      const evidence = claim && Array.isArray(claim.evidence) ? claim.evidence : [];
      if (evidence.length === 0) {
        evidenceBox.appendChild(A.make("span", "muted", "Подтверждение не указано сервером."));
      }
      evidence.forEach((item) => {
        const source = item && item.fragment_id ? sourceById.get(item.fragment_id) : null;
        const buttonFragment = A.cloneTemplate("evidence-template");
        const button = buttonFragment.querySelector(".evidence-button");
        const label = buttonFragment.querySelector(".evidence-button__text");
        if (!source) {
          button.disabled = true;
          A.setText(label, "Источник недоступен");
          button.title = "Сервер не передал источник для этого подтверждения.";
        } else {
          const locator = A.formatLocator(source.locator);
          A.setText(
            label,
            `Источник: ${source.title || "без названия"}${locator ? ` · ${locator}` : ""}`,
          );
          button.title = "Открыть документ и дословную цитату";
          button.addEventListener("click", () => {
            openSource(source, item.quote || quotes.get(item.fragment_id) || "");
          });
        }
        evidenceBox.appendChild(buttonFragment);
      });
      list.appendChild(fragment);
    });
    return items.length;
  }

  function renderSources(sources, quotes) {
    A.clear(sourcesList);
    sources.forEach((source) => {
      if (!source || typeof source.fragment_id !== "string") {
        return;
      }
      const fragment = A.cloneTemplate("source-template");
      const titleNode = fragment.querySelector(".source-item__title");
      const locatorNode = fragment.querySelector(".source-item__locator");
      const button = fragment.querySelector(".source-item__button");
      A.setText(titleNode, source.title || "Источник без названия");
      A.setText(locatorNode, A.formatLocator(source.locator) || "Адрес фрагмента не указан");
      button.addEventListener("click", () => {
        openSource(source, quotes.get(source.fragment_id) || "");
      });
      sourcesList.appendChild(fragment);
    });
  }

  function renderMeta(data) {
    const modeLabels = {
      hybrid: "Гибридный поиск (BM25 + векторы)",
      bm25: "BM25 без векторов",
    };
    const answerLabels = {
      generated: "Сгенерированный ответ",
      extracts: "Проверенные выдержки",
      sources_only: "Только источники, без выводов",
    };
    const verification = data.verification;
    let verificationText = "Не проводилась";
    if (verification && typeof verification === "object") {
      const parts = [];
      parts.push(verification.supported ? "подтверждено" : "не подтверждено");
      parts.push(verification.complete ? "полнота подтверждена" : "полнота не подтверждена");
      if (verification.conflict) {
        parts.push("обнаружен конфликт");
      }
      verificationText = parts.join(", ");
      if (Array.isArray(verification.issues) && verification.issues.length > 0) {
        verificationText += `. ${verification.issues.join(" ")}`;
      }
    }
    const timings = data.timings || {};
    const timingParts = [];
    if (Number(timings.search_ms) > 0) {
      timingParts.push(`поиск ${Math.round(Number(timings.search_ms))} мс`);
    }
    if (Number(timings.generation_ms) > 0) {
      timingParts.push(`генерация ${Math.round(Number(timings.generation_ms))} мс`);
    }
    if (Number(timings.verification_ms) > 0) {
      timingParts.push(`проверка ${Math.round(Number(timings.verification_ms))} мс`);
    }
    A.setText(metaMode, modeLabels[data.mode] || data.mode || "не указан");
    A.setText(metaAnswerMode, answerLabels[data.answer_mode] || data.answer_mode || "не указан");
    A.setText(metaVerification, verificationText);
    A.setText(metaTimings, timingParts.length ? timingParts.join(" · ") : "нет данных");
    A.show(answerMeta, true);
  }

  function renderAnswer(data) {
    const info =
      A.ASK_STATUS[data.status] || {
        label: "Неизвестный статус",
        className: "status-unknown",
        title: "Неизвестный статус ответа",
        description: "Сервер вернул статус, который интерфейс не знает. Не считайте ответ проверенным.",
      };

    sourceById.clear();
    const sources = Array.isArray(data.sources) ? data.sources : [];
    sources.forEach((source) => {
      if (source && typeof source.fragment_id === "string") {
        sourceById.set(source.fragment_id, source);
      }
    });
    const quotes = quoteMapFor(data);

    answerStatus.className = `status-banner is-${data.status || "unknown"}`;
    answerStatusBadge.className = `badge ${info.className}`;
    A.setText(answerStatusBadge, info.label);
    A.setText(answerStatusTitle, info.title);
    A.setText(answerStatusDescription, info.description);

    const clarification = data.clarification;
    A.setText(clarificationText, clarification || "");
    A.show(clarificationPanel, Boolean(clarification));

    const warnings = Array.isArray(data.warnings) ? data.warnings : [];
    A.clear(warningsList);
    warnings.forEach((warning) => {
      const item = A.cloneTemplate("warning-template").querySelector("li");
      A.setText(item, warning);
      warningsList.appendChild(item);
    });
    A.show(warningsPanel, warnings.length > 0);

    const summaryCount = renderClaims(summaryList, data.summary, quotes);
    A.show(summarySection, summaryCount > 0);
    const stepsCount = renderClaims(stepsList, data.steps, quotes);
    A.show(stepsSection, stepsCount > 0);

    renderSources(sources, quotes);
    A.show(sourcesSection, sources.length > 0);

    const hasMeta = Boolean(data.mode || data.answer_mode || data.verification || data.timings);
    A.show(answerMeta, hasMeta);
    if (hasMeta) {
      renderMeta(data);
    }

    A.show(answerRegion, true);
  }

  function closeSourceFrame() {
    if (sourceFrame) {
      sourceFrame.src = "about:blank";
      A.show(sourceFrame, false);
    }
  }

  function openSource(source, quote) {
    A.setText(sourceTitle, source.title || "Источник");
    A.setText(sourceLocator, A.formatLocator(source.locator) || "Адрес фрагмента не указан");
    A.setText(sourceQuote, quote || "Дословная цитата не передана сервером.");
    A.setText(sourceMessage, "");
    A.show(sourceMessage, false);

    const documentId = source.document_id;
    if (typeof documentId === "string" && documentId) {
      let href = `/api/documents/${encodeURIComponent(documentId)}/original`;
      if (
        source.locator &&
        source.locator.kind === "pdf" &&
        Number(source.locator.page) > 0
      ) {
        href += `#page=${encodeURIComponent(String(source.locator.page))}`;
      }
      sourceOriginal.href = href;
      A.show(sourceOriginal, true);
    } else {
      sourceOriginal.removeAttribute("href");
      A.show(sourceOriginal, false);
    }

    const sourcePath = A.safeApiPath(source.url, ["/api/sources/"]);
    if (sourcePath) {
      sourceNewTab.href = sourcePath;
      A.show(sourceNewTab, true);
      sourceFrame.src = sourcePath;
      A.show(sourceFrame, true);
    } else {
      sourceNewTab.removeAttribute("href");
      A.show(sourceNewTab, false);
      closeSourceFrame();
      A.setText(
        sourceMessage,
        "Сервер не передал корректную ссылку на HTML-представление источника.",
      );
      A.show(sourceMessage, true);
    }

    A.openDialog(sourceDialog);
  }

  sourceFrame.addEventListener("load", () => {
    if (sourceFrame.src && sourceFrame.src !== "about:blank") {
      A.setText(sourceMessage, "");
      A.show(sourceMessage, false);
    }
  });

  sourceFrame.addEventListener("error", () => {
    A.setText(sourceMessage, "Не удалось загрузить представление источника. Откройте оригинал.");
    A.show(sourceMessage, true);
  });

  sourceDialog.addEventListener("close", () => {
    closeSourceFrame();
  });

  question.addEventListener("input", () => {
    saveDraft();
    clearFieldError();
  });
  asOf.addEventListener("change", saveDraft);

  async function pollAskJob(requestId, questionText, asOfValue, startedAt) {
    const pollRun = ++activePollRun;
    const deadlineMs = Math.max(10, timeoutSeconds) * 1000;
    const originMs = typeof startedAt === "number" ? startedAt : Date.now();
    userCancelled = false;
    setLoading(true);
    A.setText(state, "Запрос выполняется на сервере… Ответ сохранится даже при переходе на другую вкладку.");

    while (pollRun === activePollRun && !isUnloading && !userCancelled) {
      if (Date.now() - originMs > deadlineMs + 15000) {
        showRequestError(
          "Превышено время ожидания ответа. Вопрос сохранён; повторите проверку или отправку позже.",
        );
        saveJobState({
          requestId,
          question: questionText,
          asOf: asOfValue || "",
          status: "error",
          startedAt: originMs,
          errorMessage: "Превышено время ожидания ответа.",
        });
        A.setText(state, "Запрос завершился ошибкой.");
        setLoading(false);
        return;
      }

      try {
        const statusData = await A.apiFetch(`/api/ask/status/${encodeURIComponent(requestId)}`, {
          method: "GET",
        });
        if (pollRun !== activePollRun || isUnloading || userCancelled) {
          return;
        }
        if (statusData && statusData.status === "completed" && statusData.result) {
          saveJobState({
            requestId: statusData.request_id || requestId,
            question: questionText,
            asOf: asOfValue || "",
            status: "completed",
            startedAt: originMs,
            result: statusData.result,
          });
          clearRequestError();
          renderAnswer(statusData.result);
          A.setText(state, "Ответ получен.");
          setLoading(false);
          return;
        }
        if (statusData && statusData.status === "error") {
          const message =
            (statusData.error && statusData.error.message) ||
            "Не удалось обработать запрос.";
          saveJobState({
            requestId: statusData.request_id || requestId,
            question: questionText,
            asOf: asOfValue || "",
            status: "error",
            startedAt: originMs,
            errorMessage: message,
          });
          showRequestError(message);
          A.setText(state, "Запрос завершился ошибкой.");
          setLoading(false);
          return;
        }
      } catch (pollError) {
        if (pollError && pollError.status === 404) {
          clearJobState();
          showRequestError(
            "Запрос не найден на сервере (возможно, сервер был перезапущен). Нажмите «Найти ответ» для повторной отправки.",
          );
          A.setText(state, "Требуется повторная отправка.");
          setLoading(false);
          return;
        }
      }
      await A.delay(800);
    }
  }

  async function restoreAskState() {
    const saved = readJobState();
    if (saved) {
      if (typeof saved.question === "string" && !question.value) {
        question.value = saved.question;
      }
      if (typeof saved.asOf === "string" && !asOf.value) {
        asOf.value = saved.asOf;
      }
      if (saved.status === "running" && saved.requestId) {
        await pollAskJob(saved.requestId, saved.question || question.value, saved.asOf || "", saved.startedAt);
        return;
      }
      if (saved.status === "completed" && saved.result) {
        renderAnswer(saved.result);
        A.setText(state, "Ответ получен.");
        return;
      }
      if (saved.status === "error" && saved.errorMessage) {
        showRequestError(saved.errorMessage);
        A.setText(state, "Запрос завершился ошибкой.");
        return;
      }
    }

    try {
      const data = await A.apiFetch("/api/ask/active", { method: "GET" });
      const active = data && data.active ? data.active : null;
      if (active && active.status === "running" && active.request_id) {
        if (typeof active.question === "string") {
          question.value = active.question;
        }
        if (typeof active.as_of === "string") {
          asOf.value = active.as_of;
        }
        saveDraft();
        saveJobState({
          requestId: active.request_id,
          question: question.value,
          asOf: asOf.value || "",
          status: "running",
          startedAt: Date.now(),
        });
        await pollAskJob(active.request_id, question.value, asOf.value || "", Date.now());
      }
    } catch {
      // Эндпоинт /api/ask/active может отсутствовать в синтетических тестах UI.
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearFieldError();
    clearRequestError();

    const value = question.value.trim();
    if (value.length < 3) {
      showFieldError("Введите содержательный вопрос: не менее 3 символов.");
      question.focus();
      return;
    }
    if (value.length > maxQuestion) {
      showFieldError(`Вопрос длиннее ${maxQuestion} символов. Сократите его.`);
      question.focus();
      return;
    }

    const asOfValue = asOf.value || "";
    const existing = readJobState();
    if (
      existing &&
      existing.status === "running" &&
      existing.requestId &&
      existing.question === value &&
      (existing.asOf || "") === asOfValue
    ) {
      await pollAskJob(existing.requestId, value, asOfValue, existing.startedAt);
      return;
    }

    const payload = { question: value };
    if (asOfValue) {
      payload.as_of = asOfValue;
    }
    const requestId = generateRequestId();
    const startedAt = Date.now();
    userCancelled = false;
    activePollRun++;
    saveDraft();
    clearAnswer();
    saveJobState({
      requestId,
      question: value,
      asOf: asOfValue,
      status: "running",
      startedAt,
    });
    setLoading(true);
    A.setText(state, "Ищу ответ… Обработка продолжится на сервере даже при переключении вкладки.");

    const controller = new AbortController();
    currentController = controller;
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, Math.max(10, timeoutSeconds) * 1000);

    let recoveringViaPoll = false;
    try {
      const data = await A.apiFetch("/api/ask", {
        method: "POST",
        headers: { "X-Request-Id": requestId },
        json: payload,
        signal: controller.signal,
      });
      if (isUnloading || userCancelled) {
        return;
      }
      saveJobState({
        requestId,
        question: value,
        asOf: asOfValue,
        status: "completed",
        startedAt,
        result: data,
      });
      renderAnswer(data);
      A.setText(state, "Ответ получен.");
    } catch (error) {
      if (isUnloading || userCancelled) {
        return;
      }
      const isNetworkOrAbort =
        (error && error.name === "AbortError") ||
        (error && !error.status);
      if (isNetworkOrAbort && !timedOut) {
        recoveringViaPoll = true;
        window.clearTimeout(timer);
        if (currentController === controller) {
          currentController = null;
        }
        await pollAskJob(requestId, value, asOfValue, startedAt);
        return;
      }
      if (timedOut) {
        const msg =
          "Превышено время ожидания ответа. Вопрос сохранён; сервер мог продолжить обработку. Повторите проверку или отправку позже.";
        saveJobState({
          requestId,
          question: value,
          asOf: asOfValue,
          status: "error",
          startedAt,
          errorMessage: msg,
        });
        showRequestError(msg);
      } else {
        const msg = A.friendlyError(error);
        saveJobState({
          requestId,
          question: value,
          asOf: asOfValue,
          status: "error",
          startedAt,
          errorMessage: msg,
        });
        showRequestError(msg);
      }
      A.setText(state, "Запрос завершился ошибкой.");
    } finally {
      window.clearTimeout(timer);
      if (currentController === controller) {
        currentController = null;
      }
      if (!recoveringViaPoll && !isUnloading) {
        setLoading(false);
      }
    }
  });

  clearButton.addEventListener("click", () => {
    userCancelled = true;
    activePollRun++;
    if (currentController) {
      currentController.abort();
      currentController = null;
    }
    question.value = "";
    asOf.value = "";
    clearDraft();
    clearJobState();
    clearFieldError();
    clearRequestError();
    clearAnswer();
    A.setText(state, "");
    question.focus();
  });

  restoreDraft();
  restoreAskState();
})();
