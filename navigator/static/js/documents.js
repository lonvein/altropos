(() => {
  "use strict";

  const A = window.Altropos;
  const documentsList = document.getElementById("documents-list");
  if (!documentsList) {
    return;
  }
  const uploadForm = document.getElementById("upload-form");

  const fileInput = document.getElementById("document-file");
  const uploadSubmit = document.getElementById("upload-submit");
  const uploadState = document.getElementById("upload-state");
  const uploadFieldError = document.getElementById("upload-field-error");
  const uploadDropzone = document.getElementById("upload-dropzone");
  const uploadQueueNode = document.getElementById("upload-queue");
  const uploadRetryFailed = document.getElementById("upload-retry-failed");
  const uploadClear = document.getElementById("upload-clear");
  const maxUploadMb = Number(uploadForm?.dataset.maxUploadMb) || 40;

  const importForm = document.getElementById("import-form");
  const importUrl = document.getElementById("import-url");
  const importPreviewSubmit = document.getElementById("import-preview-submit");
  const importPreviewLabel = importPreviewSubmit?.querySelector(".button__label");
  const importPreviewSpinner = importPreviewSubmit?.querySelector(".spinner");
  const importState = document.getElementById("import-state");
  const importFieldError = document.getElementById("import-field-error");
  const importResult = document.getElementById("import-result");
  const importPageTitle = document.getElementById("import-page-title");
  const importPageExcerpt = document.getElementById("import-page-excerpt");
  const importEmpty = document.getElementById("import-empty");
  const importFilter = document.getElementById("import-filter");
  const importFilterField = document.getElementById("import-filter-field");
  const importNoMatch = document.getElementById("import-no-match");
  const importLinks = document.getElementById("import-links");
  const importRejectedNote = document.getElementById("import-rejected-note");
  const importSelectedSubmit = document.getElementById("import-selected-submit");
  const importSelectedState = document.getElementById("import-selected-state");
  const importOutcomes = document.getElementById("import-outcomes");
  const importJob = document.getElementById("import-job");
  const importJobLabel = document.getElementById("import-job-label");
  const importJobStatus = document.getElementById("import-job-status");
  const importJobDetail = document.getElementById("import-job-detail");

  const documentsLoading = document.getElementById("documents-loading");
  const documentsEmpty = document.getElementById("documents-empty");
  const documentsError = document.getElementById("documents-error");
  const documentsErrorText = document.getElementById("documents-error-text");
  const documentsRetry = document.getElementById("documents-retry");
  const documentsRefresh = document.getElementById("documents-refresh");
  const documentsStatus = document.getElementById("documents-status");

  const metadataDialog = document.getElementById("metadata-dialog");
  const metadataForm = document.getElementById("metadata-form");
  const metadataDocumentId = document.getElementById("metadata-document-id");
  const metadataDocName = document.getElementById("metadata-doc-name");
  const metaTitle = document.getElementById("meta-title");
  const metaNumber = document.getElementById("meta-number");
  const metaIssued = document.getElementById("meta-issued");
  const metaValidFrom = document.getElementById("meta-valid-from");
  const metaValidTo = document.getElementById("meta-valid-to");
  const metaReviewed = document.getElementById("meta-reviewed");
  const relationsList = document.getElementById("relations-list");
  const addRelation = document.getElementById("add-relation");
  const metadataError = document.getElementById("metadata-error");
  const metadataSubmit = document.getElementById("metadata-submit");
  const metadataLabel = metadataSubmit?.querySelector(".button__label");
  const metadataSpinner = metadataSubmit?.querySelector(".spinner");

  let documents = [];
  let refreshTimer = null;
  let metadataDocument = null;
  let previewUrl = "";
  let lastPreviewUrl = "";
  let importBusy = false;
  // The server accepts 10 links per request; larger selections are sent in sequential batches.
  const importBatchSize = 10;
  const maxImportSelection = 200;
  // Selected URLs live here, not in the DOM: filtering, re-rendering or resizing never drops them.
  const importSelection = new Set();
  const importBulk = document.getElementById("import-bulk");
  const importSelectAll = document.getElementById("import-select-all");
  const importSelectNone = document.getElementById("import-select-none");
  const importCount = document.getElementById("import-count");
  const sourceViewer = typeof A.createSourceViewer === "function" ? A.createSourceViewer() : null;
  const selectedDocKey = "altropos.review.selectedDocumentId";
  const lastReviewKey = "altropos.review.lastResult";

  function showFieldError(node, message) {
    A.setText(node, message);
    A.show(node, Boolean(message));
  }

  function showDocumentsStatus(message, isError = false) {
    if (!documentsStatus) {
      return;
    }
    A.setText(documentsStatus, message);
    documentsStatus.classList.toggle("is-error", Boolean(isError));
  }

  function setButtonBusy(button, labelNode, spinnerNode, busy, busyLabel, idleLabel) {
    button.disabled = busy;
    A.show(spinnerNode, busy);
    if (labelNode) {
      A.setText(labelNode, busy ? busyLabel : idleLabel);
    }
  }

  function extensionOf(name) {
    const index = String(name || "").lastIndexOf(".");
    return index >= 0 ? String(name).slice(index).toLowerCase() : "";
  }

  function normalizeDocuments(payload) {
    if (Array.isArray(payload)) {
      return payload;
    }
    if (payload && Array.isArray(payload.documents)) {
      return payload.documents;
    }
    return [];
  }

  function renderJobBadge(node, status) {
    const info = A.statusInfo(status, "document");
    node.className = `badge ${info.className}`;
    A.setText(node, info.label);
  }

  function jobDetailText(job) {
    if (!job) {
      return "";
    }
    if (job.error) {
      return String(job.error);
    }
    const result = job.result;
    if (!result || typeof result !== "object") {
      if (A.isTerminalStatus(job.status)) {
        return "Задание завершено.";
      }
      return "Задание выполняется…";
    }
    if (Array.isArray(result.documents)) {
      const titles = result.documents
        .slice(0, 4)
        .map((item) => (item && (item.title || item.name)) || "без названия")
        .join("; ");
      const rejected = Array.isArray(result.rejected) ? result.rejected.length : 0;
      return [
        `Импортировано документов: ${result.documents.length}.`,
        titles ? `Названия: ${titles}.` : "",
        rejected ? `Отклонено ссылок: ${rejected}.` : "",
      ]
        .filter(Boolean)
        .join(" ");
    }
    if (Array.isArray(result.links)) {
      return result.links.length
        ? `Найдено разрешённых ссылок: ${result.links.length}. Выберите документы для импорта.`
        : "Разрешённых документов по этой ссылке не найдено.";
    }
    if (result.document_id) {
      const info = A.statusInfo(result.status, "document");
      return `Документ ${result.document_id}: ${info.label.toLowerCase()}.`;
    }
    return "Задание завершено.";
  }

  function updateJobPanel(panel, labelNode, badgeNode, detailNode, job, label) {
    A.show(panel, true);
    A.setText(labelNode, job && job.id ? `${label}: ${job.id}` : label);
    renderJobBadge(badgeNode, (job && job.status) || "queued");
    A.setText(detailNode, jobDetailText(job));
    panel.classList.toggle(
      "is-error",
      Boolean(job && (job.status === "error" || job.status === "failed")),
    );
  }

  async function pollJob(jobId, onUpdate, timeoutMs = 120_000) {
    const started = Date.now();
    while (Date.now() - started < timeoutMs) {
      const job = await A.apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "GET" });
      onUpdate(job);
      if (A.isTerminalStatus(job && job.status)) {
        return job;
      }
      await A.delay(1000);
    }
    throw new Error("Превышено время ожидания обработки задания. Обновите список вручную.");
  }

  function scheduleRefresh() {
    if (refreshTimer) {
      window.clearTimeout(refreshTimer);
      refreshTimer = null;
    }
    const busy = documents.some(
      (doc) => doc && (doc.status === "queued" || doc.status === "processing"),
    );
    if (busy) {
      refreshTimer = window.setTimeout(() => fetchDocuments({ silent: true }), 4000);
    }
  }

  function renderRelationsSummary(value) {
    const relations = Array.isArray(value) ? value : [];
    if (relations.length === 0) {
      return "";
    }
    const confirmed = relations.filter((relation) => relation && relation.confirmed === true).length;
    if (confirmed === relations.length) {
      return `${relations.length}, все подтверждены`;
    }
    return `${relations.length}, подтверждено ${confirmed}; есть неподтверждённые`;
  }

  function presentationHref(doc) {
    const candidates = [doc.presentation_url, doc.html_url, doc.source_view_url];
    for (const value of candidates) {
      const path = A.safeApiPath(value, ["/api/sources/", "/api/documents/"]);
      if (path) {
        return path;
      }
    }
    // A fragment is a citation, not a complete document representation.
    if (doc.status === "ready" && typeof doc.id === "string" && doc.id) {
      return `/api/documents/${encodeURIComponent(doc.id)}/view`;
    }
    return null;
  }

  function fillDetail(root, name, value, formatter = (item) => item) {
    const box = root.querySelector(`[data-detail="${name}"]`);
    if (!box) {
      return;
    }
    if (value === undefined || value === null || value === "") {
      A.show(box, false);
      return;
    }
    A.setText(box.querySelector("dd"), formatter(value));
    A.show(box, true);
  }

  function renderDocuments() {
    A.clear(documentsList);
    A.show(documentsEmpty, documents.length === 0);
    const ready = documents.filter((doc) => doc.status === "ready").length;
    const review = documents.filter((doc) => doc.status === "needs_review").length;
    const errors = documents.filter((doc) => ["failed", "error"].includes(doc.status)).length;
    A.setText(documentsStatus, `Всего: ${documents.length}. Доступно для поиска: ${ready}. Требуют проверки: ${review}. Ошибок: ${errors}.`);
    documents.forEach((doc) => {
      if (!doc || typeof doc !== "object") {
        return;
      }
      const fragment = A.cloneTemplate("document-template");
      const titleNode = fragment.querySelector(".document-card__title");
      const metaNode = fragment.querySelector(".document-card__meta");
      const statusNode = fragment.querySelector(".document-card__status");
      const warningsNode = fragment.querySelector(".document-card__warnings");
      const sourceNode = fragment.querySelector(".document-card__source");
      const originalLink = fragment.querySelector(".document-card__original");
      const presentationLink = fragment.querySelector(".document-card__presentation");
      const reviewButton = fragment.querySelector(".document-card__review");
      const reocrButton = fragment.querySelector(".document-card__reocr");
      const metadataButton = fragment.querySelector(".document-card__metadata");
      const retryButton = fragment.querySelector(".document-card__retry");
      const root = fragment.querySelector(".document-card");

      if (doc.id) {
        root.id = `doc-${doc.id}`;
        root.dataset.documentId = doc.id;
      }

      const title = doc.title || doc.name || "Документ без названия";
      A.setText(titleNode, title);

      const metaParts = [];
      if (doc.media_type) {
        metaParts.push(String(doc.media_type).toUpperCase());
      }
      if (doc.created) {
        metaParts.push(`добавлен ${A.formatDateTime(doc.created)}`);
      }
      if (doc.version) {
        metaParts.push(`версия ${doc.version}`);
      }
      A.setText(metaNode, metaParts.join(" · "));

      const status = doc.status || "queued";
      const statusInfo = A.statusInfo(status, "document");
      statusNode.className = `badge document-card__status ${statusInfo.className}`;
      A.setText(statusNode, statusInfo.label);

      const warnings = Array.isArray(doc.warnings) ? doc.warnings : [];
      A.clear(warningsNode);
      warnings.forEach((warning) => {
        warningsNode.appendChild(A.make("li", "", warning));
      });
      A.show(warningsNode, warnings.length > 0);

      const metadata = doc.metadata && typeof doc.metadata === "object" ? doc.metadata : {};
      fillDetail(fragment, "number", metadata.number);
      fillDetail(fragment, "issued", metadata.issued, A.formatDate);
      const validity = [];
      if (metadata.valid_from) {
        validity.push(`с ${A.formatDate(metadata.valid_from)}`);
      }
      if (metadata.valid_to) {
        validity.push(`по ${A.formatDate(metadata.valid_to)}`);
      }
      fillDetail(fragment, "validity", validity.join(" "));
      if (metadata.reviewed === true) {
        fillDetail(fragment, "reviewed", "подтверждена администратором");
      } else if (metadata.reviewed === false) {
        fillDetail(fragment, "reviewed", "не подтверждена");
      }
      fillDetail(fragment, "relations", renderRelationsSummary(metadata.relations));

      if (doc.source_url) {
        A.setText(sourceNode, `URL источника: ${doc.source_url}`);
        A.show(sourceNode, true);
      } else {
        A.show(sourceNode, false);
      }

      if (typeof doc.id === "string" && doc.id && doc.original_available !== false) {
        originalLink.href = `/api/documents/${encodeURIComponent(doc.id)}/original`;
        A.show(originalLink, true);
        if (sourceViewer) {
          originalLink.addEventListener("click", (event) => {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
              return;
            }
            event.preventDefault();
            sourceViewer.openDocumentViewer(doc, { startMode: "original" });
          });
        }
      } else {
        originalLink.removeAttribute("href");
        A.show(originalLink, false);
      }

      const href = presentationHref(doc);
      if (href) {
        presentationLink.href = href;
        A.show(presentationLink, true);
        if (sourceViewer) {
          presentationLink.addEventListener("click", (event) => {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
              return;
            }
            event.preventDefault();
            sourceViewer.openDocumentViewer(doc, { startMode: "presentation" });
          });
        }
      } else {
        presentationLink.removeAttribute("href");
        A.show(presentationLink, false);
      }

      const isBusy = status === "queued" || status === "processing";
      if (reviewButton && typeof doc.id === "string" && doc.id) {
        reviewButton.disabled = isBusy;
        A.setText(reviewButton, status === "ready" ? "Повторить проверку" : "Проверить распознавание");
        reviewButton.addEventListener("click", () => {
          saveDocumentsUiState();
          try {
            window.sessionStorage.setItem(
              "altropos.review.returnUrl",
              `${window.location.pathname}${window.location.search}#doc-${encodeURIComponent(doc.id)}`,
            );
            window.sessionStorage.setItem(selectedDocKey, doc.id);
          } catch {
            // Игнорируем ошибку sessionStorage.
          }
          window.location.assign(`/review?doc=${encodeURIComponent(doc.id)}`);
        });
      }

      if (reocrButton && typeof doc.id === "string" && doc.id) {
        reocrButton.disabled = isBusy;
        reocrButton.addEventListener("click", async () => {
          reocrButton.disabled = true;
          A.setText(reocrButton, "Запуск OCR…");
          try {
            await A.apiFetch(`/api/documents/${encodeURIComponent(doc.id)}/reocr`, {
              method: "POST",
              json: { reviewer: "Оператор", comment: "Повторный OCR со страницы документов" },
            });
            showDocumentsStatus("Повторный OCR поставлен в очередь. Оригинал и история подтверждений сохранены.");
            await fetchDocuments({ silent: true });
          } catch (error) {
            A.setText(reocrButton, "Повторить OCR");
            reocrButton.disabled = false;
            showDocumentsStatus(A.friendlyError(error), true);
          }
        });
      }

      metadataButton?.addEventListener("click", () => openMetadata(doc));
      if (retryButton && ["needs_review", "failed", "error"].includes(status)) {
        A.show(retryButton, true);
        retryButton.addEventListener("click", async () => {
          retryButton.disabled = true;
          A.setText(retryButton, "Постановка в очередь…");
          try {
            await A.apiFetch(`/api/documents/${encodeURIComponent(doc.id)}/retry`, { method: "POST" });
            await fetchDocuments({ silent: true });
          } catch (error) {
            A.setText(retryButton, "Не удалось повторить — попробуйте ещё раз");
            retryButton.disabled = false;
          }
        });
      }
      if (status === "needs_review") {
        root.classList.add("document-card--review");
      }
      documentsList.appendChild(fragment);
    });
  }

  const docStateKey = "altropos.documents.uiState.v1";
  let scrollRestored = false;

  try {
    window.sessionStorage.setItem(
      "altropos.review.returnUrl",
      `${window.location.pathname}${window.location.search}`,
    );
  } catch {
    // Игнорируем ошибку sessionStorage.
  }

  function saveDocumentsUiState() {
    try {
      const fields = {};
      document.querySelectorAll("input[id], textarea[id], select[id]").forEach((el) => {
        if (el.type === "file") {
          return;
        }
        fields[el.id] = el.type === "checkbox" ? el.checked : el.value;
      });
      window.sessionStorage.setItem(
        docStateKey,
        JSON.stringify({
          scrollY: window.scrollY || window.pageYOffset || 0,
          fields,
        }),
      );
    } catch {
      // Игнорируем ошибку sessionStorage.
    }
  }

  function restoreDocumentsUiState() {
    try {
      const raw = window.sessionStorage.getItem(docStateKey);
      if (!raw) {
        return;
      }
      const parsed = JSON.parse(raw);
      if (parsed && parsed.fields && typeof parsed.fields === "object") {
        Object.entries(parsed.fields).forEach(([id, val]) => {
          const el = document.getElementById(id);
          if (!el || el.type === "file") {
            return;
          }
          if (el.type === "checkbox") {
            el.checked = Boolean(val);
          } else if (typeof val === "string" && !el.value) {
            el.value = val;
          }
        });
      }
      if (!scrollRestored) {
        scrollRestored = true;
        let selectedId = "";
        try {
          selectedId = window.sessionStorage.getItem(selectedDocKey) || "";
          const lastReviewRaw = window.sessionStorage.getItem(lastReviewKey);
          if (lastReviewRaw) {
            const lastReview = JSON.parse(lastReviewRaw);
            if (lastReview && lastReview.message) {
              showDocumentsStatus(lastReview.message);
            }
          }
        } catch {
          selectedId = "";
        }
        const selectedEl = selectedId ? document.getElementById(`doc-${selectedId}`) : null;
        if (selectedEl) {
          document.querySelectorAll(".document-card--selected").forEach((el) => {
            el.classList.remove("document-card--selected");
          });
          selectedEl.classList.add("document-card--selected");
          window.requestAnimationFrame(() => selectedEl.scrollIntoView({ block: "center", behavior: "auto" }));
        } else if (parsed && typeof parsed.scrollY === "number" && parsed.scrollY > 0) {
          window.requestAnimationFrame(() => window.scrollTo(0, parsed.scrollY));
        }
      }
    } catch {
      // Игнорируем ошибку sessionStorage.
    }
  }

  window.addEventListener("pagehide", saveDocumentsUiState);
  window.addEventListener("beforeunload", saveDocumentsUiState);

  async function fetchDocuments(options = {}) {
    const silent = Boolean(options.silent);
    if (!silent) {
      A.show(documentsLoading, true);
      A.show(documentsError, false);
    }
    try {
      const payload = await A.apiFetch("/api/documents", { method: "GET" });
      documents = normalizeDocuments(payload);
      A.show(documentsError, false);
      A.show(documentsLoading, false);
      renderDocuments();
      restoreDocumentsUiState();
      scheduleRefresh();
    } catch (error) {
      A.show(documentsLoading, false);
      A.setText(documentsErrorText, A.friendlyError(error));
      A.show(documentsError, true);
      if (silent) {
        scheduleRefresh();
      }
    }
  }

  // ---------------------------------------------------------------- multi-file upload
  const uploadAccepted = [".pdf", ".docx", ".xlsx", ".html", ".htm"];
  const uploadItems = [];
  const maxParallelUploads = 2;
  const maxAutoRetries = 2;
  const processingPollMs = 2000;
  let uploadSeq = 0;
  let processingTimer = null;
  const isAdmin = document.body.dataset.role === "admin";

  const uploadPhase = {
    pending: { label: "Ожидает", className: "status-queued" },
    uploading: { label: "Загрузка", className: "status-processing" },
    queued: { label: "В очереди", className: "status-queued" },
    processing: { label: "Обрабатывается", className: "status-processing" },
    ready: { label: "Готов", className: "status-ready" },
    needs_review: { label: "Требует проверки", className: "status-needs_review" },
    failed: { label: "Ошибка", className: "status-error" },
    rejected: { label: "Отклонён", className: "status-error" },
  };

  function formatBytes(bytes) {
    if (bytes >= 1024 * 1024) {
      return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
    }
    return `${Math.max(1, Math.round(bytes / 1024))} КБ`;
  }

  function uploadPhaseDetail(item) {
    switch (item.phase) {
      case "pending":
        return "Ожидает очереди на загрузку.";
      case "uploading":
        return `Отправка на сервер: ${Math.round(item.progress)}%.`;
      case "queued":
        return item.duplicate
          ? "Такой файл уже есть в системе; используется существующая запись."
          : "Файл принят. Ожидает распознавания и индексации.";
      case "processing":
        return "Извлечение текста, распознавание (OCR) и индексация…";
      case "ready":
        return "Документ опубликован: доступен в поиске и в ответах на вопросы.";
      case "needs_review":
        return isAdmin
          ? "Распознавание сомнительное. Откройте проверку, чтобы подтвердить результат."
          : "Распознавание сомнительное: документ появится в поиске после проверки администратором.";
      case "failed":
        return item.error || "Не удалось обработать файл.";
      case "rejected":
        return item.error || "Файл не принят.";
      default:
        return "";
    }
  }

  function renderUploadItem(item) {
    if (!item.node) {
      const fragment = A.cloneTemplate("upload-item-template");
      item.node = fragment.querySelector(".upload-item");
      item.node.querySelector(".upload-item__name").textContent = item.file.name;
      item.node.querySelector(".upload-item__size").textContent = formatBytes(item.file.size);
      item.node.querySelector(".upload-item__remove").addEventListener("click", () => removeUploadItem(item));
      item.node.querySelector(".upload-item__retry").addEventListener("click", () => retryUploadItem(item));
      uploadQueueNode.appendChild(fragment);
    }
    const info = uploadPhase[item.phase] || uploadPhase.pending;
    const badge = item.node.querySelector(".upload-item__badge");
    badge.className = `badge upload-item__badge ${info.className}`;
    badge.textContent = info.label;
    item.node.dataset.phase = item.phase;
    const progress = item.node.querySelector(".upload-item__progress");
    progress.value = item.progress;
    A.show(progress, item.phase === "uploading");
    item.node.querySelector(".upload-item__detail").textContent = uploadPhaseDetail(item);
    item.node.classList.toggle("is-error", item.phase === "failed" || item.phase === "rejected");
    A.show(item.node.querySelector(".upload-item__retry"), item.phase === "failed");
    item.node.querySelector(".upload-item__remove").textContent =
      item.phase === "uploading" ? "Отменить" : "Убрать";
    updateUploadSummary();
  }

  function updateUploadSummary() {
    if (!uploadQueueNode) {
      return;
    }
    A.show(uploadQueueNode, uploadItems.length > 0);
    const count = (...phases) => uploadItems.filter((item) => phases.includes(item.phase)).length;
    const failed = count("failed", "rejected");
    A.show(uploadRetryFailed, count("failed") > 0);
    A.show(uploadClear, uploadItems.some((item) => ["ready", "needs_review", "failed", "rejected"].includes(item.phase)));
    if (uploadItems.length === 0) {
      A.setText(uploadState, "");
      return;
    }
    const parts = [`Файлов: ${uploadItems.length}`];
    const active = count("pending", "uploading");
    const working = count("queued", "processing");
    if (active) {
      parts.push(`загружается: ${active}`);
    }
    if (working) {
      parts.push(`обрабатывается: ${working}`);
    }
    if (count("ready")) {
      parts.push(`готово: ${count("ready")}`);
    }
    if (count("needs_review")) {
      parts.push(`на проверке: ${count("needs_review")}`);
    }
    if (failed) {
      parts.push(`с ошибкой: ${failed}`);
    }
    A.setText(uploadState, `${parts.join(" · ")}.`);
  }

  function validateUploadFile(file) {
    if (!uploadAccepted.includes(extensionOf(file.name))) {
      return "Неподдерживаемый формат. Разрешены PDF, DOCX, XLSX и HTML.";
    }
    if (file.size === 0) {
      return "Файл пустой.";
    }
    if (file.size > maxUploadMb * 1024 * 1024) {
      return `Размер файла превышает ${maxUploadMb} МБ.`;
    }
    return "";
  }

  function addUploadFiles(fileList) {
    showFieldError(uploadFieldError, "");
    const files = Array.from(fileList || []);
    if (files.length === 0) {
      showFieldError(uploadFieldError, "Выберите хотя бы один файл PDF, DOCX, XLSX или HTML.");
      return;
    }
    let skipped = 0;
    files.forEach((file) => {
      const same = uploadItems.some(
        (item) =>
          item.file.name === file.name &&
          item.file.size === file.size &&
          item.file.lastModified === file.lastModified &&
          !["failed", "rejected"].includes(item.phase),
      );
      if (same) {
        skipped += 1;
        return;
      }
      const problem = validateUploadFile(file);
      const item = {
        id: ++uploadSeq,
        file,
        phase: problem ? "rejected" : "pending",
        progress: 0,
        error: problem,
        attempts: 0,
        documentId: null,
        xhr: null,
        startedAt: 0,
      };
      uploadItems.push(item);
      renderUploadItem(item);
    });
    if (skipped) {
      showFieldError(uploadFieldError, `Уже в списке: ${skipped}.`);
    }
    pumpUploads();
  }

  function pumpUploads() {
    let running = uploadItems.filter((item) => item.phase === "uploading").length;
    for (const item of uploadItems) {
      if (running >= maxParallelUploads) {
        break;
      }
      if (item.phase === "pending") {
        running += 1;
        startUpload(item);
      }
    }
  }

  function uploadErrorMessage(status, payload) {
    const info = payload && payload.error && typeof payload.error === "object" ? payload.error : {};
    if (status === 413) {
      return `Файл слишком большой (не более ${maxUploadMb} МБ).`;
    }
    if (status === 409 && info.message) {
      return info.message;
    }
    if (status === 422) {
      return info.message || "Проверьте формат, имя и размер файла.";
    }
    if (status === 403) {
      return info.message || "Запрос отклонён. Обновите страницу и повторите.";
    }
    if (status === 429) {
      return "Слишком много запросов. Повторите чуть позже.";
    }
    if (status >= 500 || status === 0) {
      return "Сервер временно недоступен. Повторите попытку.";
    }
    return info.message || `Сервер отказал в загрузке (код ${status}).`;
  }

  function startUpload(item) {
    item.phase = "uploading";
    item.progress = 0;
    item.error = "";
    item.attempts += 1;
    renderUploadItem(item);

    const formData = new FormData();
    formData.append("file", item.file, item.file.name);
    const xhr = new XMLHttpRequest();
    item.xhr = xhr;
    xhr.open("POST", "/api/documents");
    xhr.setRequestHeader("Accept", "application/json");
    xhr.responseType = "text";
    xhr.timeout = 15 * 60 * 1000;

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        item.progress = Math.min(99, (event.loaded / event.total) * 100);
        renderUploadItem(item);
      }
    };

    const fail = (message, retryable) => {
      item.xhr = null;
      if (retryable && item.attempts <= maxAutoRetries) {
        item.phase = "pending";
        item.error = message;
        renderUploadItem(item);
        window.setTimeout(pumpUploads, 1500 * item.attempts);
        return;
      }
      item.phase = "failed";
      item.error = message;
      renderUploadItem(item);
      pumpUploads();
    };

    xhr.onload = () => {
      let payload = null;
      try {
        payload = JSON.parse(xhr.responseText);
      } catch {
        payload = null;
      }
      if (xhr.status === 401) {
        item.phase = "failed";
        item.error = "Сессия завершена. Выполните вход заново.";
        renderUploadItem(item);
        window.setTimeout(
          () => window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`),
          800,
        );
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300 && payload && payload.document_id) {
        item.xhr = null;
        item.documentId = payload.document_id;
        item.duplicate = Boolean(payload.duplicate);
        item.progress = 100;
        item.phase = ["ready", "needs_review", "failed"].includes(payload.status) ? payload.status : "queued";
        renderUploadItem(item);
        schedulePolling();
        pumpUploads();
        return;
      }
      fail(uploadErrorMessage(xhr.status, payload), xhr.status === 0 || xhr.status >= 500 || xhr.status === 429);
    };
    xhr.onerror = () => fail("Не удалось связаться с сервером. Проверьте соединение.", true);
    xhr.ontimeout = () => fail("Загрузка заняла слишком много времени.", true);
    xhr.onabort = () => {
      item.xhr = null;
    };
    xhr.send(formData);
  }

  function retryUploadItem(item) {
    item.attempts = 0;
    item.phase = "pending";
    item.error = "";
    renderUploadItem(item);
    pumpUploads();
  }

  function removeUploadItem(item) {
    if (item.xhr) {
      item.xhr.abort();
    }
    const index = uploadItems.indexOf(item);
    if (index >= 0) {
      uploadItems.splice(index, 1);
    }
    item.node?.remove();
    updateUploadSummary();
    pumpUploads();
  }

  function schedulePolling() {
    if (processingTimer || !uploadItems.some((item) => ["queued", "processing"].includes(item.phase))) {
      return;
    }
    processingTimer = window.setTimeout(async () => {
      processingTimer = null;
      await pollProcessing();
      schedulePolling();
    }, processingPollMs);
  }

  async function pollProcessing() {
    const waiting = uploadItems.filter((item) => ["queued", "processing"].includes(item.phase));
    if (waiting.length === 0) {
      return;
    }
    let list;
    try {
      list = normalizeDocuments(await A.apiFetch("/api/documents", { method: "GET" }));
    } catch {
      return; // transient: the next tick retries
    }
    documents = list;
    renderDocuments();
    const byId = new Map(list.map((doc) => [doc.id, doc]));
    waiting.forEach((item) => {
      const doc = byId.get(item.documentId);
      if (!doc) {
        return;
      }
      if (doc.status === "ready" || doc.status === "needs_review") {
        item.phase = doc.status;
      } else if (doc.status === "failed" || doc.status === "error") {
        item.phase = "failed";
        item.error = (Array.isArray(doc.warnings) && doc.warnings[0]) || "Обработка завершилась ошибкой.";
      } else {
        item.phase = doc.status === "processing" ? "processing" : "queued";
      }
      renderUploadItem(item);
    });
    scheduleRefresh();
  }

  uploadForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (uploadItems.length === 0) {
      showFieldError(uploadFieldError, "Выберите хотя бы один файл PDF, DOCX, XLSX или HTML.");
      fileInput?.click();
      return;
    }
    showFieldError(uploadFieldError, "");
    uploadItems.filter((item) => item.phase === "failed").forEach(retryUploadItem);
    pumpUploads();
  });

  fileInput?.addEventListener("change", () => {
    if (fileInput.files && fileInput.files.length > 0) {
      addUploadFiles(fileInput.files);
      fileInput.value = "";
    }
  });
  uploadRetryFailed?.addEventListener("click", () => {
    uploadItems.filter((item) => item.phase === "failed").forEach(retryUploadItem);
  });
  uploadClear?.addEventListener("click", () => {
    uploadItems
      .filter((item) => ["ready", "needs_review", "failed", "rejected"].includes(item.phase))
      .forEach(removeUploadItem);
  });

  if (uploadDropzone) {
    ["dragenter", "dragover"].forEach((name) =>
      uploadDropzone.addEventListener(name, (event) => {
        event.preventDefault();
        uploadDropzone.classList.add("is-dragover");
      }),
    );
    ["dragleave", "dragend", "drop"].forEach((name) =>
      uploadDropzone.addEventListener(name, (event) => {
        event.preventDefault();
        uploadDropzone.classList.remove("is-dragover");
      }),
    );
    uploadDropzone.addEventListener("drop", (event) => {
      if (event.dataTransfer && event.dataTransfer.files.length > 0) {
        addUploadFiles(event.dataTransfer.files);
      }
    });
  }
  // A file dropped beside the zone must not navigate away from the page and cancel uploads.
  window.addEventListener("dragover", (event) => event.preventDefault());
  window.addEventListener("drop", (event) => event.preventDefault());
  window.addEventListener("beforeunload", (event) => {
    if (uploadItems.some((item) => item.phase === "uploading")) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  function validateMiptUrl(value) {
    if (!value) {
      return "Укажите URL страницы mipt.ru.";
    }
    let url;
    try {
      url = new URL(value);
    } catch {
      return "Укажите корректный URL http(s).";
    }
    if (!["http:", "https:"].includes(url.protocol)) {
      return "Разрешены только http(s)-адреса.";
    }
    const host = url.hostname.toLowerCase();
    if (host !== "mipt.ru" && !host.endsWith(".mipt.ru")) {
      return "Разрешены только mipt.ru и его поддомены.";
    }
    return "";
  }

  function normalizeLink(link) {
    if (typeof link === "string") {
      const name = link.split("/").pop() || link;
      return { url: link, title: name, allowed: true };
    }
    if (!link || typeof link !== "object") {
      return null;
    }
    const url = link.url || link.href || link.source_url;
    if (typeof url !== "string" || !url) {
      return null;
    }
    return {
      url,
      title: link.title || link.name || url.split("/").pop() || url,
      kind: link.kind || link.media_type || null,
      allowed: link.allowed !== false,
      reason: link.reason || link.error || "",
    };
  }

  function allowedImportBoxes() {
    return Array.from(importLinks.querySelectorAll(".import-checkbox:not(:disabled)"));
  }

  function updateImportSelection() {
    if (!importLinks || !importSelectedSubmit) {
      return;
    }
    const total = allowedImportBoxes().length;
    const count = importSelection.size;
    importSelectedSubmit.disabled = importBusy || !previewUrl || count === 0 || count > maxImportSelection;
    A.setText(importSelectedSubmit, count ? `Импортировать выбранные (${count})` : "Импортировать выбранные");
    A.show(importBulk, total > 0);
    A.setText(importCount, total ? `Выбрано: ${count} из ${total}` : "");
    if (importSelectAll) {
      importSelectAll.disabled = importBusy || total === 0;
    }
    if (importSelectNone) {
      importSelectNone.disabled = importBusy || count === 0;
    }
    if (count > maxImportSelection) {
      A.setText(importSelectedState, `Можно выбрать не больше ${maxImportSelection} документов за раз.`);
    }
  }

  function appendImportLink(link) {
    const normalized = normalizeLink(link);
    if (!normalized) {
      return false;
    }
    const fragment = A.cloneTemplate("import-link-template");
    const item = fragment.querySelector(".import-link");
    const checkbox = fragment.querySelector(".import-checkbox");
    const titleNode = fragment.querySelector(".import-link__title");
    const urlNode = fragment.querySelector(".import-link__url");
    const reasonNode = fragment.querySelector(".import-link__reason");
    A.setText(titleNode, normalized.title);
    A.setText(urlNode, normalized.url);
    checkbox.value = normalized.url;
    checkbox.dataset.url = normalized.url;
    item.dataset.title = normalized.title;
    if (!normalized.allowed) {
      checkbox.disabled = true;
      checkbox.checked = false;
      item.classList.add("is-rejected");
      A.setText(reasonNode, normalized.reason || "Ссылка не разрешена для импорта.");
      A.show(reasonNode, true);
    } else {
      checkbox.checked = importSelection.has(normalized.url);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          importSelection.add(normalized.url);
        } else {
          importSelection.delete(normalized.url);
        }
        if (!importBusy) {
          A.setText(importSelectedState, "");
          updateImportSelection();
        }
      });
    }
    importLinks.appendChild(fragment);
    return normalized.allowed;
  }

  importFilter?.addEventListener("input", () => {
    const query = importFilter.value.trim().toLocaleLowerCase("ru");
    let matches = 0;
    for (const item of importLinks.children) {
      const text = `${item.querySelector(".import-link__title").textContent} ${
        item.querySelector(".import-link__url").textContent}`;
      const visible = text.toLocaleLowerCase("ru").includes(query);
      A.show(item, visible);
      matches += Number(visible);
    }
    A.show(importNoMatch, matches === 0);
  });

  importSelectAll?.addEventListener("click", () => {
    // Only what is currently listed: respect an active filter, never hidden rows.
    const visible = allowedImportBoxes().filter((box) => !box.closest(".import-link").hidden);
    for (const box of visible) {
      if (importSelection.size >= maxImportSelection) {
        A.setText(importSelectedState, `Выбрано максимум: ${maxImportSelection} документов.`);
        break;
      }
      box.checked = true;
      importSelection.add(box.dataset.url || box.value);
    }
    updateImportSelection();
  });

  importSelectNone?.addEventListener("click", () => {
    importSelection.clear();
    allowedImportBoxes().forEach((box) => {
      box.checked = false;
    });
    A.setText(importSelectedState, "");
    updateImportSelection();
  });

  function renderImportPreview(result, fallbackUrl) {
    const data = result || {};
    A.setText(importPageTitle, data.page_title || data.title || "Страница проверена");
    A.setText(
      importPageExcerpt,
      data.page_excerpt || data.excerpt || data.text || `URL: ${fallbackUrl}`,
    );
    A.clear(importLinks);

    const links = []
      .concat(Array.isArray(data.links) ? data.links : [])
      .concat(Array.isArray(data.allowed_links) ? data.allowed_links : [])
      .concat(Array.isArray(data.allowed) ? data.allowed : [])
      .concat(Array.isArray(data.documents) ? data.documents : []);
    let allowedCount = 0;
    links.forEach((link) => {
      if (appendImportLink(link)) {
        allowedCount += 1;
      }
    });

    const rejected = Array.isArray(data.rejected) ? data.rejected : [];
    rejected.forEach((link) => appendImportLink({ ...link, allowed: false }));
    A.setText(
      importRejectedNote,
      rejected.length ? `Отклонено ссылок: ${rejected.length}. Они не будут импортированы.` : "",
    );
    A.show(importRejectedNote, rejected.length > 0);

    A.show(importEmpty, allowedCount === 0);
    importFilter.value = "";
    A.show(importFilterField, allowedCount > 1);
    A.show(importNoMatch, false);
    A.show(importResult, true);
    updateImportSelection();
  }

  async function pollOrUseDirect(payload, onUpdate) {
    if (payload && payload.job_id) {
      return pollJob(payload.job_id, onUpdate);
    }
    if (payload && typeof payload === "object") {
      return { id: null, status: "ready", result: payload, error: null };
    }
    throw new Error("Сервер не вернул идентификатор задания импорта.");
  }

  importUrl?.addEventListener("input", () => {
    previewUrl = "";
    A.show(importResult, false);
    A.show(importJob, false);
    A.setText(importState, "");
    updateImportSelection();
  });

  importForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (importBusy) {
      return;
    }
    showFieldError(importFieldError, "");
    A.setText(importSelectedState, "");
    const url = importUrl.value.trim();
    const validation = validateMiptUrl(url);
    if (validation) {
      showFieldError(importFieldError, validation);
      importUrl.focus();
      return;
    }

    importBusy = true;
    if (url !== lastPreviewUrl) {
      importSelection.clear();
    }
    previewUrl = "";
    importUrl.readOnly = true;
    A.clear(importOutcomes);
    A.show(importOutcomes, false);
    setButtonBusy(
      importPreviewSubmit,
      importPreviewLabel,
      importPreviewSpinner,
      true,
      "Проверяю…",
      "Найти документы",
    );
    A.setText(importState, "Ищу документы по ссылке…");
    A.show(importResult, false);
    A.show(importJob, true);
    A.setText(importJobLabel, "Поиск документов");
    renderJobBadge(importJobStatus, "queued");
    A.setText(importJobDetail, "Запрос отправлен…");
    importJob.classList.remove("is-error");

    try {
      const response = await A.apiFetch("/api/import-url", {
        method: "POST",
        json: { url, selected_urls: [] },
      });
      const job = await pollOrUseDirect(response, (update) =>
        updateJobPanel(
          importJob,
          importJobLabel,
          importJobStatus,
          importJobDetail,
          update,
          "Поиск документов",
        ),
      );
      if (job && (job.status === "error" || job.status === "failed")) {
        throw new Error(job.error || "Не удалось найти документы по ссылке.");
      }
      const result = job ? job.result : response;
      previewUrl = url;
      lastPreviewUrl = url;
      renderImportPreview(result, url);
      A.setText(importState, "");
    } catch (error) {
      importJob.classList.add("is-error");
      renderJobBadge(importJobStatus, "error");
      A.setText(importJobDetail, A.friendlyError(error));
      A.setText(importState, "Не удалось проверить ссылку. URL сохранён.");
    } finally {
      importBusy = false;
      importUrl.readOnly = false;
      updateImportSelection();
      setButtonBusy(
        importPreviewSubmit,
        importPreviewLabel,
        importPreviewSpinner,
        false,
        "Проверяю…",
        "Найти документы",
      );
    }
  });

  function addImportOutcome(doc) {
    const status = A.statusInfo(doc.status, "document").label;
    const item = A.make("li", "", `${doc.title || doc.name || doc.id || "Документ"} · ${status}`);
    if (isAdmin && ["needs_review", "ready", "failed"].includes(doc.status) && doc.id) {
      const review = A.make("a", "button button--ghost", "Открыть проверку");
      review.href = `/review?doc=${encodeURIComponent(doc.id)}`;
      item.appendChild(review);
    }
    importOutcomes.appendChild(item);
  }

  function forgetImported(urls) {
    urls.forEach((url) => importSelection.delete(url));
    allowedImportBoxes().forEach((box) => {
      box.checked = importSelection.has(box.dataset.url || box.value);
    });
  }

  importSelectedSubmit?.addEventListener("click", async () => {
    if (importBusy || !previewUrl) {
      return;
    }
    const url = previewUrl;
    const selected = Array.from(importSelection);
    if (selected.length === 0 || selected.length > maxImportSelection) {
      return;
    }
    const batches = [];
    for (let i = 0; i < selected.length; i += importBatchSize) {
      batches.push(selected.slice(i, i + importBatchSize));
    }

    importBusy = true;
    importUrl.readOnly = true;
    importPreviewSubmit.disabled = true;
    updateImportSelection();
    A.clear(importOutcomes);
    A.show(importOutcomes, false);
    A.show(importJob, true);
    A.setText(importJobLabel, "Задание импорта");
    importJob.classList.remove("is-error");

    let importedTotal = 0;
    let rejectedTotal = 0;
    let failedMessage = "";
    try {
      for (let index = 0; index < batches.length; index += 1) {
        const batch = batches[index];
        const part = batches.length > 1 ? ` (пакет ${index + 1} из ${batches.length})` : "";
        A.setText(importSelectedState, `Импортирую выбранные документы${part}…`);
        renderJobBadge(importJobStatus, "queued");
        A.setText(importJobDetail, `Запрос отправлен${part}…`);
        let job;
        let response;
        try {
          response = await A.apiFetch("/api/import-url", {
            method: "POST",
            json: { url, selected_urls: batch },
          });
          job = await pollOrUseDirect(response, (update) =>
            updateJobPanel(importJob, importJobLabel, importJobStatus, importJobDetail, update, `Задание импорта${part}`),
          );
          if (job && (job.status === "error" || job.status === "failed")) {
            throw new Error(job.error || "Задание импорта завершилось ошибкой.");
          }
        } catch (error) {
          failedMessage = A.friendlyError(error);
          break;
        }
        const result = job ? job.result : response;
        const imported = Array.isArray(result?.documents) ? result.documents : [];
        const rejected = Array.isArray(result?.rejected) ? result.rejected : [];
        importedTotal += imported.length;
        rejectedTotal += rejected.length;
        imported.forEach(addImportOutcome);
        rejected.forEach(({ url: rejectedUrl, reason }) => {
          importOutcomes.appendChild(A.make("li", "muted", `${rejectedUrl} — ${reason}`));
        });
        // Imported and refused links leave the selection; unfinished batches stay selected.
        forgetImported(batch);
        A.show(importOutcomes, importOutcomes.childElementCount > 0);
        await fetchDocuments({ silent: true });
      }
      if (failedMessage) {
        importJob.classList.add("is-error");
        renderJobBadge(importJobStatus, "error");
        A.setText(importJobDetail, failedMessage);
        A.setText(
          importSelectedState,
          `Импорт прерван: получено ${importedTotal}. Не импортированные документы остались выбранными; повторите вручную.`,
        );
      } else {
        A.setText(importSelectedState, `Получено документов: ${importedTotal}. Отклонено ссылок: ${rejectedTotal}.`);
      }
    } finally {
      importBusy = false;
      importUrl.readOnly = false;
      importPreviewSubmit.disabled = false;
      await fetchDocuments({ silent: true });
      updateImportSelection();
    }
  });

  function relationStateText(confirmed) {
    return confirmed
      ? "Подтверждена администратором."
      : "Не подтверждена. Автоматическое предложение не считается проверенным.";
  }

  function buildRelationRow(relation) {
    const data = relation && typeof relation === "object" ? relation : {};
    const fragment = A.cloneTemplate("relation-template");
    const row = fragment.querySelector(".relation-row");
    const target = fragment.querySelector(".relation-target");
    const kind = fragment.querySelector(".relation-kind");
    const section = fragment.querySelector(".relation-section");
    const confirmed = fragment.querySelector(".relation-confirmed");
    const stateNode = fragment.querySelector(".relation-state");
    const remove = fragment.querySelector(".relation-remove");

    target.value = data.target_id || "";
    const kindValue = data.kind || "amends";
    kind.value = ["amends", "repeals", "appendix"].includes(kindValue) ? kindValue : "amends";
    section.value = data.target_section || "";
    confirmed.checked = data.confirmed === true;
    A.setText(stateNode, relationStateText(confirmed.checked));

    confirmed.addEventListener("change", () => {
      A.setText(stateNode, relationStateText(confirmed.checked));
    });
    remove.addEventListener("click", () => row.remove());
    return fragment;
  }

  function renderRelations(relations) {
    A.clear(relationsList);
    const items = Array.isArray(relations) ? relations : [];
    items.forEach((relation) => relationsList.appendChild(buildRelationRow(relation)));
  }

  addRelation?.addEventListener("click", () => {
    relationsList.appendChild(
      buildRelationRow({ target_id: "", kind: "amends", target_section: "", confirmed: false }),
    );
    const lastTarget = relationsList.querySelector(".relation-row:last-child .relation-target");
    if (lastTarget) {
      lastTarget.focus();
    }
  });

  function openMetadata(doc) {
    metadataDocument = doc;
    const metadata = doc.metadata && typeof doc.metadata === "object" ? doc.metadata : {};
    metadataDocumentId.value = doc.id || "";
    A.setText(metadataDocName, doc.title || doc.name || doc.id || "");
    metaTitle.value = metadata.title || doc.title || "";
    metaNumber.value = metadata.number || "";
    metaIssued.value = metadata.issued || "";
    metaValidFrom.value = metadata.valid_from || "";
    metaValidTo.value = metadata.valid_to || "";
    metaReviewed.checked = metadata.reviewed === true;
    renderRelations(metadata.relations || doc.relations || []);
    showFieldError(metadataError, "");
    A.openDialog(metadataDialog);
  }

  metadataForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    showFieldError(metadataError, "");
    const documentId = metadataDocumentId.value;
    if (!documentId) {
      showFieldError(metadataError, "Документ не выбран.");
      return;
    }
    const validFrom = metaValidFrom.value;
    const validTo = metaValidTo.value;
    if (validFrom && validTo && validFrom > validTo) {
      showFieldError(metadataError, "Дата начала действия позже даты окончания.");
      return;
    }

    const relations = [];
    relationsList.querySelectorAll(".relation-row").forEach((row) => {
      const targetId = row.querySelector(".relation-target").value.trim();
      if (!targetId) {
        return;
      }
      const kind = row.querySelector(".relation-kind").value;
      const targetSection = row.querySelector(".relation-section").value.trim();
      const confirmed = row.querySelector(".relation-confirmed").checked;
      relations.push({
        target_id: targetId,
        kind,
        target_section: targetSection || null,
        confirmed,
      });
    });

    const patch = {
      title: metaTitle.value.trim() || null,
      number: metaNumber.value.trim() || null,
      issued: metaIssued.value || null,
      valid_from: metaValidFrom.value || null,
      valid_to: metaValidTo.value || null,
      relations,
    };
    if (isAdmin) {
      // The review mark can approve a doubtful extraction: only the administrator sends it.
      patch.reviewed = metaReviewed.checked;
    }

    setButtonBusy(metadataSubmit, metadataLabel, metadataSpinner, true, "Сохраняю…", "Сохранить");
    try {
      await A.apiFetch(
        `/api/documents/${encodeURIComponent(documentId)}/metadata`,
        { method: "PATCH", json: patch },
      );
      A.closeDialog(metadataDialog);
      showDocumentsStatus("Метаданные сохранены. Неподтверждённые связи остались предложениями.");
      await fetchDocuments({ silent: true });
    } catch (error) {
      showFieldError(metadataError, A.friendlyError(error));
    } finally {
      setButtonBusy(metadataSubmit, metadataLabel, metadataSpinner, false, "Сохраняю…", "Сохранить");
    }
  });

  documentsRefresh.addEventListener("click", () => fetchDocuments({ silent: false }));
  documentsRetry.addEventListener("click", () => fetchDocuments({ silent: false }));

  fetchDocuments({ silent: false });
})();
