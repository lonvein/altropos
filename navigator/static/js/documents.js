(() => {
  "use strict";

  const A = window.Altropos;
  const uploadForm = document.getElementById("upload-form");
  if (!uploadForm) {
    return;
  }

  const fileInput = document.getElementById("document-file");
  const uploadSubmit = document.getElementById("upload-submit");
  const uploadLabel = uploadSubmit.querySelector(".button__label");
  const uploadSpinner = uploadSubmit.querySelector(".spinner");
  const uploadState = document.getElementById("upload-state");
  const uploadFieldError = document.getElementById("upload-field-error");
  const uploadJob = document.getElementById("upload-job");
  const uploadJobLabel = document.getElementById("upload-job-label");
  const uploadJobStatus = document.getElementById("upload-job-status");
  const uploadJobDetail = document.getElementById("upload-job-detail");
  const maxUploadMb = Number(uploadForm.dataset.maxUploadMb) || 40;

  const importForm = document.getElementById("import-form");
  const importUrl = document.getElementById("import-url");
  const importPreviewSubmit = document.getElementById("import-preview-submit");
  const importPreviewLabel = importPreviewSubmit.querySelector(".button__label");
  const importPreviewSpinner = importPreviewSubmit.querySelector(".spinner");
  const importState = document.getElementById("import-state");
  const importFieldError = document.getElementById("import-field-error");
  const importResult = document.getElementById("import-result");
  const importPageTitle = document.getElementById("import-page-title");
  const importPageExcerpt = document.getElementById("import-page-excerpt");
  const importEmpty = document.getElementById("import-empty");
  const importLinks = document.getElementById("import-links");
  const importRejectedNote = document.getElementById("import-rejected-note");
  const importSelectedSubmit = document.getElementById("import-selected-submit");
  const importSelectedState = document.getElementById("import-selected-state");
  const importJob = document.getElementById("import-job");
  const importJobLabel = document.getElementById("import-job-label");
  const importJobStatus = document.getElementById("import-job-status");
  const importJobDetail = document.getElementById("import-job-detail");

  const documentsList = document.getElementById("documents-list");
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
  const metadataLabel = metadataSubmit.querySelector(".button__label");
  const metadataSpinner = metadataSubmit.querySelector(".spinner");

  let documents = [];
  let refreshTimer = null;
  let metadataDocument = null;

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
      return `Найдено разрешённых ссылок: ${result.links.length}. Выберите документы для импорта.`;
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
    if (typeof doc.fragment_id === "string" && doc.fragment_id) {
      return `/api/sources/${encodeURIComponent(doc.fragment_id)}`;
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
      const metadataButton = fragment.querySelector(".document-card__metadata");
      const retryButton = fragment.querySelector(".document-card__retry");
      const root = fragment.querySelector(".document-card");

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
      } else {
        originalLink.removeAttribute("href");
        A.show(originalLink, false);
      }

      const href = presentationHref(doc);
      if (href) {
        presentationLink.href = href;
        A.show(presentationLink, true);
      } else {
        presentationLink.removeAttribute("href");
        A.show(presentationLink, false);
      }

      metadataButton.addEventListener("click", () => openMetadata(doc));
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

  fileInput.addEventListener("change", () => showFieldError(uploadFieldError, ""));

  uploadForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    showFieldError(uploadFieldError, "");
    const file = fileInput.files && fileInput.files[0];
    if (!file) {
      showFieldError(uploadFieldError, "Выберите файл PDF, DOCX или XLSX.");
      return;
    }
    const extension = extensionOf(file.name);
    if (![".pdf", ".docx", ".xlsx"].includes(extension)) {
      showFieldError(uploadFieldError, "Поддерживаются только файлы PDF, DOCX и XLSX.");
      return;
    }
    if (file.size > maxUploadMb * 1024 * 1024) {
      showFieldError(uploadFieldError, `Размер файла превышает ${maxUploadMb} МБ.`);
      return;
    }

    const formData = new FormData();
    formData.append("file", file);
    setButtonBusy(uploadSubmit, uploadLabel, uploadSpinner, true, "Загружаю…", "Загрузить");
    A.setText(uploadState, "Отправляю файл…");
    A.show(uploadJob, true);
    A.setText(uploadJobLabel, "Задание загрузки");
    renderJobBadge(uploadJobStatus, "queued");
    A.setText(uploadJobDetail, "Файл отправляется на сервер…");
    uploadJob.classList.remove("is-error");

    try {
      const response = await A.apiFetch("/api/documents", {
        method: "POST",
        body: formData,
      });
      const documentId =
        (response && response.document_id) ||
        (response && response.document && response.document.id) ||
        null;
      const jobId = response && response.job_id ? response.job_id : null;
      const duplicate = Boolean(response && response.duplicate);
      if (duplicate) {
        A.setText(uploadState, "Такой документ уже загружен; используется существующая запись.");
      }

      if (jobId) {
        const job = await pollJob(jobId, (update) =>
          updateJobPanel(
            uploadJob,
            uploadJobLabel,
            uploadJobStatus,
            uploadJobDetail,
            update,
            "Задание загрузки",
          ),
        );
        if (job) {
          A.setText(uploadJobLabel, `Задание загрузки: ${job.id}`);
        }
        await fetchDocuments({ silent: true });
        if (job && (job.status === "error" || job.status === "failed")) {
          A.setText(uploadState, "Обработка завершилась ошибкой. Проверьте предупреждения.");
        } else {
          A.setText(uploadState, "Загрузка завершена.");
          fileInput.value = "";
        }
      } else {
        if (documentId) {
          A.setText(uploadJobLabel, `Документ: ${documentId}`);
          renderJobBadge(
            uploadJobStatus,
            (response && response.status) || (response.document && response.document.status) || "ready",
          );
          A.setText(uploadJobDetail, "Сервер вернул документ без отдельного задания.");
        }
        await fetchDocuments({ silent: true });
        A.setText(uploadState, "Загрузка завершена.");
        fileInput.value = "";
      }
    } catch (error) {
      uploadJob.classList.add("is-error");
      renderJobBadge(uploadJobStatus, "error");
      A.setText(uploadJobDetail, A.friendlyError(error));
      A.setText(uploadState, "Загрузка не удалась. Файл сохранён в поле выбора.");
    } finally {
      setButtonBusy(uploadSubmit, uploadLabel, uploadSpinner, false, "Загружаю…", "Загрузить");
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

  function updateImportSelection() {
    const checked = importLinks.querySelectorAll(".import-checkbox:checked");
    const count = checked.length;
    importSelectedSubmit.disabled = count === 0;
    A.setText(importSelectedSubmit, count ? `Импортировать выбранные (${count})` : "Импортировать выбранные");
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
    if (!normalized.allowed) {
      checkbox.disabled = true;
      checkbox.checked = false;
      item.classList.add("is-rejected");
      A.setText(reasonNode, normalized.reason || "Ссылка не разрешена для импорта.");
      A.show(reasonNode, true);
    } else {
      checkbox.addEventListener("change", updateImportSelection);
    }
    importLinks.appendChild(fragment);
    return normalized.allowed;
  }

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

  importForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    showFieldError(importFieldError, "");
    A.setText(importSelectedState, "");
    const url = importUrl.value.trim();
    const validation = validateMiptUrl(url);
    if (validation) {
      showFieldError(importFieldError, validation);
      importUrl.focus();
      return;
    }

    setButtonBusy(
      importPreviewSubmit,
      importPreviewLabel,
      importPreviewSpinner,
      true,
      "Проверяю…",
      "Проверить страницу",
    );
    A.setText(importState, "Загружаю страницу и ищу разрешённые документы…");
    A.show(importResult, false);
    A.show(importJob, true);
    A.setText(importJobLabel, "Задание проверки страницы");
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
          "Задание проверки страницы",
        ),
      );
      if (job && (job.status === "error" || job.status === "failed")) {
        throw new Error(job.error || "Задание проверки страницы завершилось ошибкой.");
      }
      renderImportPreview(job ? job.result : response, url);
      A.setText(importState, "Страница проверена. Выберите документы.");
    } catch (error) {
      importJob.classList.add("is-error");
      renderJobBadge(importJobStatus, "error");
      A.setText(importJobDetail, A.friendlyError(error));
      A.setText(importState, "Не удалось проверить страницу. URL сохранён.");
    } finally {
      setButtonBusy(
        importPreviewSubmit,
        importPreviewLabel,
        importPreviewSpinner,
        false,
        "Проверяю…",
        "Проверить страницу",
      );
    }
  });

  importSelectedSubmit.addEventListener("click", async () => {
    showFieldError(importFieldError, "");
    A.setText(importSelectedState, "");
    const url = importUrl.value.trim();
    const validation = validateMiptUrl(url);
    if (validation) {
      showFieldError(importFieldError, validation);
      importUrl.focus();
      return;
    }
    const selected = Array.from(importLinks.querySelectorAll(".import-checkbox:checked"))
      .map((checkbox) => checkbox.dataset.url || checkbox.value)
      .filter(Boolean);
    if (selected.length === 0) {
      A.setText(importSelectedState, "Выберите хотя бы один документ.");
      return;
    }

    importSelectedSubmit.disabled = true;
    A.setText(importSelectedState, "Импортирую выбранные документы…");
    A.show(importJob, true);
    A.setText(importJobLabel, "Задание импорта");
    renderJobBadge(importJobStatus, "queued");
    A.setText(importJobDetail, "Запрос отправлен…");
    importJob.classList.remove("is-error");

    try {
      const response = await A.apiFetch("/api/import-url", {
        method: "POST",
        json: { url, selected_urls: selected },
      });
      const job = await pollOrUseDirect(response, (update) =>
        updateJobPanel(
          importJob,
          importJobLabel,
          importJobStatus,
          importJobDetail,
          update,
          "Задание импорта",
        ),
      );
      if (job && (job.status === "error" || job.status === "failed")) {
        throw new Error(job.error || "Задание импорта завершилось ошибкой.");
      }
      const result = job ? job.result : response;
      const imported = result && Array.isArray(result.documents) ? result.documents.length : 0;
      A.setText(importSelectedState, `Импорт завершён. Документов: ${imported}.`);
      showDocumentsStatus(`Импорт завершён. Добавлено документов: ${imported}.`);
      await fetchDocuments({ silent: true });
    } catch (error) {
      importJob.classList.add("is-error");
      renderJobBadge(importJobStatus, "error");
      A.setText(importJobDetail, A.friendlyError(error));
      A.setText(importSelectedState, "Импорт не выполнен. Выбор сохранён.");
    } finally {
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

  addRelation.addEventListener("click", () => {
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

  metadataForm.addEventListener("submit", async (event) => {
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
      reviewed: metaReviewed.checked,
      relations,
    };

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
