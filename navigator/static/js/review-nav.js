(function () {
  "use strict";

  const RETURN_URL_KEY = "altropos.review.returnUrl";
  const SELECTED_DOC_KEY = "altropos.review.selectedDocumentId";
  const LAST_RESULT_KEY = "altropos.review.lastResult";

  function readStorage(key) {
    try {
      return window.sessionStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function writeStorage(key, value) {
    try {
      window.sessionStorage.setItem(key, value);
    } catch {
      // Игнорируем ошибку sessionStorage.
    }
  }

  function safeInternalUrl(url) {
    return url && url.startsWith("/") && !url.startsWith("//") ? url : null;
  }

  function initBackButtons() {
    const buttons = document.querySelectorAll("[data-review-back]");
    if (!buttons || buttons.length === 0) {
      return;
    }

    const storedReturn = safeInternalUrl(readStorage(RETURN_URL_KEY));
    const selectedId = readStorage(SELECTED_DOC_KEY) || "";

    function targetUrl(fallback) {
      let base = storedReturn || fallback || "/documents";
      if (selectedId && base.startsWith("/documents") && !base.includes("#")) {
        base = `${base}#doc-${encodeURIComponent(selectedId)}`;
      }
      return base;
    }

    buttons.forEach((button) => {
      const scope = button.getAttribute("data-review-scope") || "index";
      const fallback = button.getAttribute("data-back-fallback") || "/documents";
      if (scope !== "package" && button.tagName === "A") {
        button.setAttribute("href", targetUrl(fallback));
      }

      button.addEventListener("click", (event) => {
        event.preventDefault();
        if (scope === "package") {
          if (window.history.length > 1) {
            window.history.back();
            return;
          }
          window.location.assign(fallback);
          return;
        }
        window.location.assign(targetUrl(fallback));
      });
    });
  }

  async function initReviewWorkspace() {
    const appRoot = document.getElementById("review-app");
    if (!appRoot || !window.Altropos) {
      return;
    }

    const A = window.Altropos;
    const selectNode = document.getElementById("review-document-select");
    const statusBadge = document.getElementById("review-doc-status");
    const versionNode = document.getElementById("review-doc-version");
    const approvedNode = document.getElementById("review-doc-approved");
    const errorNode = document.getElementById("review-error");
    const stateNode = document.getElementById("review-state");
    const emptyNode = document.getElementById("review-empty");
    const detailNode = document.getElementById("review-detail");
    const warningsBox = document.getElementById("review-warnings-box");
    const warningsList = document.getElementById("review-warnings-list");
    const reviewerInput = document.getElementById("review-reviewer");
    const commentInput = document.getElementById("review-comment");
    const approveBtn = document.getElementById("review-approve-btn");
    const rejectBtn = document.getElementById("review-reject-btn");
    const reocrBtn = document.getElementById("review-reocr-btn");
    const downloadLink = document.getElementById("review-download-original");
    const pageControls = document.getElementById("review-page-controls");
    const prevPageBtn = document.getElementById("review-prev-page");
    const nextPageBtn = document.getElementById("review-next-page");
    const pageLabel = document.getElementById("review-page-label");
    const previewImg = document.getElementById("review-preview-image");
    const previewNote = document.getElementById("review-preview-note");
    const blocksList = document.getElementById("review-blocks-list");
    const resetEditsBtn = document.getElementById("review-reset-edits");
    const historyList = document.getElementById("review-history-list");

    let documents = [];
    let currentDetail = null;
    let currentPage = 1;
    let pageCount = 1;
    let dirtyBlocks = false;

    function showError(msg) {
      A.setText(errorNode, msg || "");
      A.show(errorNode, Boolean(msg));
    }

    function showState(msg) {
      A.setText(stateNode, msg || "");
      A.show(stateNode, Boolean(msg));
    }

    function formatAction(entry) {
      const actionMap = {
        review: "Проверка",
        edit_results: "Правка результатов",
        reopen_review: "Повторное открытие проверки",
        reocr: "Повторный запуск OCR",
      };
      const decisionMap = {
        approved: "Подтверждён",
        needs_review: "Требует доработки",
        rejected: "Отклонён",
        pending: "На проверке",
        queued: "Поставлен в очередь",
      };
      const act = actionMap[entry.action] || entry.action || "Действие";
      const dec = entry.decision ? ` (${decisionMap[entry.decision] || entry.decision})` : "";
      const rev = entry.reviewer ? ` · ${entry.reviewer}` : "";
      const ver = entry.version ? ` · версия ${entry.version}` : "";
      const time = entry.created ? ` · ${A.formatDateTime(entry.created)}` : "";
      const comment = entry.comment ? ` — ${entry.comment}` : "";
      return `${act}${dec}${ver}${rev}${time}${comment}`;
    }

    function renderPreview(doc, originalInfo) {
      pageCount = Math.max(1, Number(originalInfo?.page_count) || 1);
      currentPage = Math.max(1, Math.min(pageCount, currentPage));
      if (doc.media_type === "pdf") {
        A.show(pageControls, true);
        A.setText(pageLabel, `Стр. ${currentPage} / ${pageCount}`);
        prevPageBtn.disabled = currentPage <= 1;
        nextPageBtn.disabled = currentPage >= pageCount;
        A.show(previewNote, false);
        A.show(previewImg, true);
        previewImg.src = `/api/documents/${encodeURIComponent(doc.id)}/original-page/${currentPage}?scale=1.5`;
      } else {
        A.show(pageControls, false);
        A.show(previewImg, false);
        previewImg.removeAttribute("src");
        A.setText(
          previewNote,
          `Постраничное изображение доступно только для PDF (текущий формат: ${String(doc.media_type || "").toUpperCase()}). Вы можете скачать оригинал или отредактировать извлечённые блоки справа.`,
        );
        A.show(previewNote, true);
      }
    }

    function renderBlocks(blocks) {
      A.clear(blocksList);
      dirtyBlocks = false;
      if (!Array.isArray(blocks) || blocks.length === 0) {
        blocksList.appendChild(A.make("p", "muted", "Текстовые блоки не извлечены. Запустите повторный OCR."));
        return;
      }
      blocks.forEach((blk, idx) => {
        const card = A.make("div", "review-block-card");
        const metaParts = [];
        if (blk.page) {
          metaParts.push(`Стр. ${blk.page}`);
        }
        if (blk.sheet) {
          metaParts.push(`Лист «${blk.sheet}»`);
        }
        if (blk.kind) {
          metaParts.push(blk.kind);
        }
        card.appendChild(A.make("div", "review-block-card__head muted", metaParts.join(" · ") || `Блок ${idx + 1}`));

        if (typeof blk.text === "string") {
          const textarea = document.createElement("textarea");
          textarea.className = "review-block-textarea";
          textarea.rows = Math.min(8, Math.max(2, Math.ceil(blk.text.length / 75)));
          textarea.value = blk.text;
          textarea.dataset.blockIndex = String(idx);
          textarea.addEventListener("input", () => {
            dirtyBlocks = true;
          });
          card.appendChild(textarea);
        } else if (Array.isArray(blk.table || blk.rows)) {
          const rows = blk.table || blk.rows;
          const summaryText = blk.sheet
            ? `Таблица листа «${blk.sheet}» (${rows.length} строк)`
            : `Таблица (${rows.length} строк)`;
          card.appendChild(A.make("pre", "review-block-table", summaryText));
        }
        blocksList.appendChild(card);
      });
    }

    function collectBlocks(originalBlocks) {
      const copy = (originalBlocks || []).map((b) => ({ ...b }));
      blocksList.querySelectorAll("textarea[data-block-index]").forEach((ta) => {
        const idx = Number(ta.dataset.blockIndex);
        if (copy[idx]) {
          copy[idx].text = ta.value;
        }
      });
      return copy;
    }

    function renderDetail(detail) {
      currentDetail = detail;
      const doc = detail.document;
      writeStorage(SELECTED_DOC_KEY, doc.id);

      const statusInfo = A.statusInfo(doc.status, "document");
      statusBadge.className = `badge ${statusInfo.className}`;
      A.setText(statusBadge, statusInfo.label);
      A.setText(versionNode, doc.version ? `Версия: ${doc.version}` : "");
      if (doc.metadata && doc.metadata.reviewed === true) {
        A.setText(approvedNode, "Подтверждён оператором");
      } else if (doc.metadata && doc.metadata.reviewed === false) {
        A.setText(approvedNode, "Требует подтверждения");
      } else {
        A.setText(approvedNode, doc.status === "ready" ? "Опубликован" : "Ожидает решения");
      }

      const warnings = Array.isArray(detail.warnings) ? detail.warnings : [];
      A.clear(warningsList);
      warnings.forEach((w) => warningsList.appendChild(A.make("li", "", w)));
      A.show(warningsBox, warnings.length > 0);

      if (downloadLink && doc.id) {
        downloadLink.href = `/api/documents/${encodeURIComponent(doc.id)}/original`;
        A.show(downloadLink, true);
      }

      renderPreview(doc, detail.original_info);
      renderBlocks(detail.blocks);

      A.clear(historyList);
      const history = Array.isArray(detail.review_history) ? detail.review_history : [];
      if (history.length === 0) {
        historyList.appendChild(A.make("li", "muted", "Записи о проверках и повторных запусках OCR ещё не добавлялись."));
      } else {
        [...history].reverse().forEach((entry) => {
          historyList.appendChild(A.make("li", "", formatAction(entry)));
        });
      }

      A.show(detailNode, true);
    }

    async function loadDocumentDetail(docId, { resetPage = true } = {}) {
      if (!docId) {
        A.show(detailNode, false);
        return;
      }
      if (resetPage) {
        currentPage = 1;
      }
      showError("");
      showState("Загрузка данных проверки…");
      try {
        const detail = await A.apiFetch(`/api/documents/${encodeURIComponent(docId)}/review`, { method: "GET" });
        renderDetail(detail);
        showState("");
      } catch (err) {
        showState("");
        showError(A.friendlyError(err));
      }
    }

    async function loadDocumentList(preferredId) {
      showError("");
      try {
        const res = await A.apiFetch("/api/documents", { method: "GET" });
        documents = Array.isArray(res?.documents) ? res.documents : [];
        A.clear(selectNode);
        if (documents.length === 0) {
          A.show(emptyNode, true);
          A.show(detailNode, false);
          return;
        }
        A.show(emptyNode, false);
        documents.forEach((doc) => {
          const opt = document.createElement("option");
          opt.value = doc.id;
          const st = A.statusInfo(doc.status, "document").label;
          opt.textContent = `${doc.title || doc.name} [${st}]`;
          selectNode.appendChild(opt);
        });
        const chosen =
          (preferredId && documents.some((d) => d.id === preferredId) && preferredId) ||
          documents[0].id;
        selectNode.value = chosen;
        await loadDocumentDetail(chosen, { resetPage: true });
      } catch (err) {
        showError(A.friendlyError(err));
      }
    }

    async function submitDecision(decision) {
      if (!currentDetail) {
        return;
      }
      const docId = currentDetail.document.id;
      showError("");
      showState("Сохранение результатов проверки…");
      approveBtn.disabled = true;
      rejectBtn.disabled = true;
      try {
        const body = {
          action: "review",
          decision,
          reviewer: reviewerInput.value.trim() || "Оператор",
          comment: commentInput.value.trim(),
        };
        if (dirtyBlocks) {
          body.blocks = collectBlocks(currentDetail.blocks);
        }
        const updated = await A.apiFetch(`/api/documents/${encodeURIComponent(docId)}/review`, {
          method: "POST",
          json: body,
        });
        renderDetail(updated);
        const msg =
          decision === "approved"
            ? `Проверка документа «${updated.document.title || updated.document.name}» подтверждена (версия ${updated.document.version || "v1"}).`
            : `Документ «${updated.document.title || updated.document.name}» отмечен как требующий доработки.`;
        showState(msg);
        writeStorage(
          LAST_RESULT_KEY,
          JSON.stringify({ documentId: docId, decision, message: msg }),
        );
        await loadDocumentListPreserveDetail(docId);
      } catch (err) {
        showState("");
        showError(A.friendlyError(err));
      } finally {
        approveBtn.disabled = false;
        rejectBtn.disabled = false;
      }
    }

    async function loadDocumentListPreserveDetail(docId) {
      try {
        const res = await A.apiFetch("/api/documents", { method: "GET" });
        documents = Array.isArray(res?.documents) ? res.documents : [];
        A.clear(selectNode);
        documents.forEach((doc) => {
          const opt = document.createElement("option");
          opt.value = doc.id;
          const st = A.statusInfo(doc.status, "document").label;
          opt.textContent = `${doc.title || doc.name} [${st}]`;
          selectNode.appendChild(opt);
        });
        selectNode.value = docId;
      } catch {
        // Non-fatal dropdown refresh error.
      }
    }

    async function runReocr() {
      if (!currentDetail) {
        return;
      }
      const docId = currentDetail.document.id;
      showError("");
      showState("Запуск повторного OCR…");
      reocrBtn.disabled = true;
      try {
        const jobRes = await A.apiFetch(`/api/documents/${encodeURIComponent(docId)}/reocr`, {
          method: "POST",
          json: {
            reviewer: reviewerInput.value.trim() || "Оператор",
            comment: commentInput.value.trim() || "Повторный OCR из раздела проверки",
          },
        });
        const jobId = jobRes && jobRes.job_id;
        if (jobId) {
          const deadline = Date.now() + 60_000;
          while (Date.now() < deadline) {
            const job = await A.apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "GET" });
            if (job && ["ready", "needs_review", "failed", "error"].includes(job.status)) {
              break;
            }
            await A.delay(350);
          }
        }
        await loadDocumentDetail(docId, { resetPage: false });
        await loadDocumentListPreserveDetail(docId);
        const ver = currentDetail?.document?.version || "";
        const msg = `Повторный OCR выполнен${ver ? ` (версия ${ver})` : ""}. История подтверждений и оригинал сохранены.`;
        showState(msg);
        writeStorage(
          LAST_RESULT_KEY,
          JSON.stringify({ documentId: docId, action: "reocr", message: msg }),
        );
      } catch (err) {
        showState("");
        showError(A.friendlyError(err));
      } finally {
        reocrBtn.disabled = false;
      }
    }

    selectNode?.addEventListener("change", () => {
      loadDocumentDetail(selectNode.value, { resetPage: true });
    });

    prevPageBtn?.addEventListener("click", () => {
      if (currentDetail && currentPage > 1) {
        currentPage -= 1;
        renderPreview(currentDetail.document, currentDetail.original_info);
      }
    });

    nextPageBtn?.addEventListener("click", () => {
      if (currentDetail && currentPage < pageCount) {
        currentPage += 1;
        renderPreview(currentDetail.document, currentDetail.original_info);
      }
    });

    resetEditsBtn?.addEventListener("click", () => {
      if (currentDetail) {
        renderBlocks(currentDetail.blocks);
      }
    });

    approveBtn?.addEventListener("click", () => submitDecision("approved"));
    rejectBtn?.addEventListener("click", () => submitDecision("needs_review"));
    reocrBtn?.addEventListener("click", runReocr);

    const urlParams = new URLSearchParams(window.location.search);
    const initialDoc =
      urlParams.get("doc") ||
      appRoot.dataset.initialDocId ||
      readStorage(SELECTED_DOC_KEY) ||
      "";
    await loadDocumentList(initialDoc);
  }

  function init() {
    initBackButtons();
    initReviewWorkspace();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
