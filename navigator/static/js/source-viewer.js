(() => {
  "use strict";

  function attachSourceViewer() {
    const A = window.Altropos;
    if (!A) {
      return null;
    }

    const sourceDialog = document.getElementById("source-dialog");
    if (!sourceDialog) {
      return null;
    }

    const sourceTitle = document.getElementById("source-title");
    const sourceLocator = document.getElementById("source-locator");
    const sourceQuote = document.getElementById("source-quote");
    const sourcePresentationBtn = document.getElementById("source-presentation-btn");
    const sourceOriginal = document.getElementById("source-original");
    const sourceNewTab = document.getElementById("source-new-tab");
    const sourceDownloadOriginal = document.getElementById("source-download-original");
    const sourcePageControls = document.getElementById("source-page-controls");
    const sourcePrevPage = document.getElementById("source-prev-page");
    const sourcePageInput = document.getElementById("source-page-input");
    const sourcePageCount = document.getElementById("source-page-count");
    const sourceNextPage = document.getElementById("source-next-page");
    const sourceZoomControls = document.getElementById("source-zoom-controls");
    const sourceZoomOut = document.getElementById("source-zoom-out");
    const sourceZoomReset = document.getElementById("source-zoom-reset");
    const sourceZoomIn = document.getElementById("source-zoom-in");
    const sourceZoomFit = document.getElementById("source-zoom-fit");
    const sourceHighlightNotice = document.getElementById("source-highlight-notice");
    const sourceMessage = document.getElementById("source-message");
    const sourceViewport = document.getElementById("source-viewport");
    const sourceOriginalStage = document.getElementById("source-original-stage");
    const sourceOriginalCanvas = document.getElementById("source-original-canvas");
    const sourceOriginalImage = document.getElementById("source-original-image");
    const sourceHighlightLayer = document.getElementById("source-highlight-layer");
    const sourceOriginalEmpty = document.getElementById("source-original-empty");
    const sourceFrameWrap = document.getElementById("source-frame-wrap");
    const sourceFrame = document.getElementById("source-frame");
    const sourceFooterHint = document.getElementById("source-footer-hint");

    const state = {
      openToken: 0,
      mode: "presentation", // "presentation" | "original"
      documentId: null,
      fragmentId: null,
      mediaType: "pdf",
      quote: "",
      sourcePath: null,
      downloadUrl: null,
      currentPage: 1,
      targetPage: 1,
      pageCount: 1,
      zoom: 1.0,
      fitWidth: true,
      highlightData: null,
      originalInfo: null,
    };

    function clearError() {
      A.setText(sourceMessage, "");
      A.show(sourceMessage, false);
    }

    function showError(msg) {
      A.setText(sourceMessage, msg);
      A.show(sourceMessage, Boolean(msg));
    }

    function clearNotice() {
      A.setText(sourceHighlightNotice, "");
      A.show(sourceHighlightNotice, false);
    }

    function showNotice(msg) {
      A.setText(sourceHighlightNotice, msg);
      A.show(sourceHighlightNotice, Boolean(msg));
    }

    function clearHighlights() {
      if (sourceHighlightLayer) {
        A.clear(sourceHighlightLayer);
      }
    }

    function closeFrame() {
      if (sourceFrame) {
        sourceFrame.src = "about:blank";
        A.show(sourceFrame, false);
      }
      if (sourceFrameWrap) {
        sourceFrameWrap.hidden = true;
      }
    }

    function updateZoomLabel() {
      if (sourceZoomReset) {
        A.setText(sourceZoomReset, `${Math.round(state.zoom * 100)}%`);
      }
    }

    function supportsOriginalPages() {
      if (state.originalInfo && typeof state.originalInfo.supports_bbox === "boolean") {
        return state.originalInfo.supports_bbox;
      }
      return state.mediaType === "pdf" || state.mediaType === "docx";
    }

    function updatePageControls() {
      const hasPages = supportsOriginalPages();
      const canPage = state.mode === "original" && hasPages && state.pageCount >= 1;
      A.show(sourcePageControls, canPage || (hasPages && state.pageCount > 1));
      if (sourcePageInput) {
        sourcePageInput.max = String(Math.max(1, state.pageCount));
        sourcePageInput.value = String(state.currentPage);
      }
      if (sourcePageCount) {
        A.setText(sourcePageCount, `из ${state.pageCount}`);
      }
      if (sourcePrevPage) {
        sourcePrevPage.disabled = state.currentPage <= 1;
      }
      if (sourceNextPage) {
        sourceNextPage.disabled = state.currentPage >= state.pageCount;
      }
      A.show(sourceZoomControls, true);
      updateZoomLabel();
    }

    function computeImageTargetWidth() {
      const natW = sourceOriginalImage?.naturalWidth || 780;
      const natH = sourceOriginalImage?.naturalHeight || 1050;
      if (!sourceViewport) {
        return Math.round(natW * state.zoom);
      }
      const availW = Math.max(320, sourceViewport.clientWidth - 40);
      if (state.fitWidth) {
        state.zoom = Math.max(0.4, Math.min(2.5, availW / Math.max(natW, 1)));
        updateZoomLabel();
        return Math.round(availW);
      }
      const baseScale = Math.min(1.0, availW / Math.max(natW, 1));
      const baseW = natW * baseScale;
      if (!natW || !natH) {
        return Math.round(760 * state.zoom);
      }
      return Math.max(200, Math.round(baseW * state.zoom));
    }

    function applyCanvasSize() {
      if (sourceViewport) {
        sourceViewport.classList.toggle("source-viewport--presentation", state.mode === "presentation");
      }
      if (state.mode === "original") {
        if (!sourceOriginalImage || sourceOriginalImage.hidden) {
          return;
        }
        const widthPx = computeImageTargetWidth();
        sourceOriginalImage.style.width = `${widthPx}px`;
        sourceOriginalImage.style.height = "auto";
        if (sourceOriginalCanvas) {
          sourceOriginalCanvas.style.width = `${widthPx}px`;
        }
      } else if (sourceFrame) {
        sourceFrame.style.transformOrigin = "top left";
        if (state.fitWidth || Math.abs(state.zoom - 1.0) < 0.01) {
          sourceFrame.style.transform = "";
          sourceFrame.style.width = "100%";
          sourceFrame.style.height = "100%";
        } else {
          const pct = 100 / Math.max(0.4, state.zoom);
          sourceFrame.style.transform = `scale(${state.zoom})`;
          sourceFrame.style.width = `${pct}%`;
          sourceFrame.style.height = `${pct}%`;
        }
      }
    }

    function renderHighlightBoxes({ scrollIntoView = false } = {}) {
      clearHighlights();
      if (state.mode !== "original" || !sourceHighlightLayer || !state.highlightData) {
        return;
      }
      const hl = state.highlightData;
      const pageBoxes = Array.isArray(hl.boxes)
        ? hl.boxes.filter((b) => Number(b.page || hl.page) === Number(state.currentPage))
        : [];

      if (hl.exact === false && Number(hl.page) === Number(state.currentPage)) {
        showNotice(
          hl.notice ||
            `Точное положение фрагмента на странице определить невозможно; открыта страница ${state.currentPage} оригинала.`,
        );
        return;
      }

      if (pageBoxes.length > 0) {
        clearNotice();
        pageBoxes.forEach((box, idx) => {
          if (!box || !box.norm) {
            return;
          }
          const rect = document.createElement("div");
          rect.className = "original-highlight-box";
          rect.dataset.lineIndex = String(idx);
          rect.style.left = `${(Number(box.norm.left) * 100).toFixed(4)}%`;
          rect.style.top = `${(Number(box.norm.top) * 100).toFixed(4)}%`;
          rect.style.width = `${(Number(box.norm.width) * 100).toFixed(4)}%`;
          rect.style.height = `${(Number(box.norm.height) * 100).toFixed(4)}%`;
          sourceHighlightLayer.appendChild(rect);
        });

        if (scrollIntoView && sourceViewport && sourceOriginalCanvas) {
          const firstBox = sourceHighlightLayer.querySelector(".original-highlight-box");
          if (firstBox) {
            window.requestAnimationFrame(() => {
              const vpRect = sourceViewport.getBoundingClientRect();
              const boxRect = firstBox.getBoundingClientRect();
              const delta = boxRect.top - vpRect.top - sourceViewport.clientHeight * 0.35;
              if (Number.isFinite(delta)) {
                sourceViewport.scrollTop = Math.max(0, sourceViewport.scrollTop + delta);
              }
            });
          }
        }
      } else {
        clearNotice();
      }
    }

    function setModeButtons() {
      if (sourceOriginal) {
        sourceOriginal.classList.toggle("button--primary", state.mode === "original");
        sourceOriginal.classList.toggle("button--secondary", state.mode !== "original");
      }
      if (sourcePresentationBtn) {
        sourcePresentationBtn.classList.toggle("button--primary", state.mode === "presentation");
        sourcePresentationBtn.classList.toggle("button--ghost", state.mode !== "presentation");
      }
    }

    function showPresentationMode() {
      state.mode = "presentation";
      setModeButtons();
      clearNotice();
      if (sourceViewport) {
        sourceViewport.classList.add("source-viewport--presentation");
      }
      A.show(sourceOriginalStage, false);
      clearHighlights();

      if (state.sourcePath && sourceFrame) {
        const hash = state.fragmentId ? `#fragment-${encodeURIComponent(state.fragmentId)}` : "";
        const targetSrc = `${state.sourcePath}${hash}`;
        if (sourceFrame.getAttribute("data-loaded-src") !== targetSrc) {
          sourceFrame.setAttribute("data-loaded-src", targetSrc);
          sourceFrame.src = targetSrc;
        }
        A.show(sourceFrameWrap, true);
        A.show(sourceFrame, true);
      } else {
        closeFrame();
        showError("Сервер не передал корректную ссылку на HTML-представление источника.");
      }

      updatePageControls();
      applyCanvasSize();
      if (sourceFooterHint) {
        A.setText(
          sourceFooterHint,
          state.documentId
            ? "Нажмите «Открыть оригинал» для перехода к исходной странице документа."
            : "",
        );
      }
    }

    function loadOriginalPage(pageNum, { scrollToHighlight = true } = {}) {
      if (!state.documentId) {
        return;
      }
      const clamped = Math.max(1, Math.min(Math.max(1, state.pageCount), Number(pageNum) || 1));
      state.currentPage = clamped;
      updatePageControls();

      if (!supportsOriginalPages()) {
        A.show(sourceOriginalImage, false);
        clearHighlights();
        const nonPageMsg =
          (state.highlightData && state.highlightData.notice) ||
          "Точное координатное выделение поверх страниц поддерживается только для PDF и DOCX оригиналов. Для проверки этого файла вы можете скачать оригинал или открыть распознанный текст.";
        showNotice(nonPageMsg);
        if (sourceOriginalEmpty) {
          A.setText(
            sourceOriginalEmpty,
            "Постраничный предпросмотр недоступен для этого формата. Используйте кнопку «Скачать оригинал» или вкладку «Распознанный текст».",
          );
          A.show(sourceOriginalEmpty, true);
        }
        return;
      }

      A.show(sourceOriginalEmpty, false);
      const token = state.openToken;
      const imgUrl = `/api/documents/${encodeURIComponent(state.documentId)}/original-page/${clamped}?scale=1.75`;
      A.show(sourceOriginalImage, true);
      clearHighlights();

      sourceOriginalImage.onload = () => {
        if (token !== state.openToken || state.mode !== "original") {
          return;
        }
        clearError();
        applyCanvasSize();
        renderHighlightBoxes({ scrollIntoView: scrollToHighlight });
      };
      sourceOriginalImage.onerror = () => {
        if (token !== state.openToken) {
          return;
        }
        A.show(sourceOriginalImage, false);
        clearHighlights();
        showError("Не удалось загрузить страницу оригинала. Вы можете скачать исходный файл.");
      };
      sourceOriginalImage.src = imgUrl;
    }

    async function showOriginalMode({ jumpToTarget = true } = {}) {
      if (!state.documentId) {
        return;
      }
      state.mode = "original";
      setModeButtons();
      if (sourceViewport) {
        sourceViewport.classList.remove("source-viewport--presentation");
      }
      A.show(sourceFrameWrap, false);
      A.show(sourceOriginalStage, true);
      clearError();

      const token = state.openToken;
      const pending = [];
      if (!state.originalInfo) {
        pending.push(
          A.apiFetch(`/api/documents/${encodeURIComponent(state.documentId)}/original-info`, { method: "GET" })
            .then((info) => {
              if (token !== state.openToken || !info) {
                return;
              }
              state.originalInfo = info;
              if (info.media_type) {
                state.mediaType = info.media_type;
              }
              if (Number(info.page_count) > 0) {
                state.pageCount = Math.max(state.pageCount, Number(info.page_count));
              }
            })
            .catch(() => null),
        );
      }
      if (state.fragmentId && !state.highlightData) {
        pending.push(
          A.apiFetch(
            `/api/sources/${encodeURIComponent(state.fragmentId)}/highlight?quote=${encodeURIComponent(state.quote)}`,
            { method: "GET" },
          )
            .then((hl) => {
              if (token !== state.openToken || !hl) {
                return;
              }
              state.highlightData = hl;
              if (hl.media_type) {
                state.mediaType = hl.media_type;
              }
              if (Number(hl.page) > 0) {
                state.targetPage = Number(hl.page);
              }
              if (Number(hl.page_count) > 0) {
                state.pageCount = Math.max(state.pageCount, Number(hl.page_count));
              }
            })
            .catch(() => null),
        );
      }
      if (pending.length > 0) {
        await Promise.all(pending);
      }
      if (token !== state.openToken) {
        return;
      }

      if (jumpToTarget && state.targetPage > 0) {
        state.currentPage = state.targetPage;
      }
      if (sourceFooterHint) {
        A.setText(
          sourceFooterHint,
          supportsOriginalPages()
            ? "Исходная страница документа. Колёсико и кнопки масштаба изменяют размер без смещения подсветки."
            : "",
        );
      }
      loadOriginalPage(state.currentPage, { scrollToHighlight: true });
    }

    async function openCitation({ source, quote }) {
      const token = ++state.openToken;
      state.documentId = typeof source?.document_id === "string" ? source.document_id : null;
      state.fragmentId = typeof source?.fragment_id === "string" ? source.fragment_id : null;
      state.mediaType = source?.locator?.kind || "pdf";
      state.quote = quote || "";
      state.targetPage =
        source?.locator?.kind === "pdf" && Number(source?.locator?.page) > 0
          ? Number(source.locator.page)
          : 1;
      state.currentPage = state.targetPage;
      state.pageCount = Math.max(1, state.targetPage);
      state.zoom = 1.0;
      state.fitWidth = true;
      state.highlightData = null;
      state.originalInfo = null;
      state.sourcePath = A.safeApiPath(source?.url, ["/api/sources/", "/api/documents/"]);
      state.downloadUrl = state.documentId
        ? `/api/documents/${encodeURIComponent(state.documentId)}/original`
        : null;

      A.setText(sourceTitle, source?.title || "Источник");
      A.setText(sourceLocator, A.formatLocator(source?.locator) || "Адрес фрагмента не указан");
      if (sourceQuote) {
        A.setText(sourceQuote, quote || "");
        sourceQuote.title = quote || "";
        A.show(sourceQuote, Boolean(quote));
      }
      clearError();
      clearNotice();

      A.show(sourcePresentationBtn, Boolean(state.sourcePath));
      if (state.downloadUrl && sourceOriginal) {
        let href = state.downloadUrl;
        if (state.mediaType === "pdf" && state.targetPage > 0) {
          href += `#page=${encodeURIComponent(String(state.targetPage))}`;
        }
        sourceOriginal.href = href;
        A.show(sourceOriginal, true);
      } else {
        sourceOriginal?.removeAttribute("href");
        A.show(sourceOriginal, false);
      }

      if (state.downloadUrl && sourceDownloadOriginal) {
        sourceDownloadOriginal.href = state.downloadUrl;
        A.show(sourceDownloadOriginal, true);
      } else {
        sourceDownloadOriginal?.removeAttribute("href");
        A.show(sourceDownloadOriginal, false);
      }

      if (state.sourcePath && sourceNewTab) {
        sourceNewTab.href = state.sourcePath;
        A.show(sourceNewTab, true);
      } else {
        sourceNewTab?.removeAttribute("href");
        A.show(sourceNewTab, false);
      }

      if (sourceFrame) {
        sourceFrame.removeAttribute("data-loaded-src");
      }

      A.openDialog(sourceDialog);

      const tasks = [];
      if (state.documentId) {
        tasks.push(
          A.apiFetch(`/api/documents/${encodeURIComponent(state.documentId)}/original-info`, { method: "GET" })
            .then((info) => {
              if (token !== state.openToken || !info) {
                return;
              }
              state.originalInfo = info;
              if (info.media_type) {
                state.mediaType = info.media_type;
              }
              if (Number(info.page_count) > 0) {
                state.pageCount = Math.max(state.pageCount, Number(info.page_count));
              }
            })
            .catch(() => null),
        );
      }
      if (state.fragmentId) {
        tasks.push(
          A.apiFetch(
            `/api/sources/${encodeURIComponent(state.fragmentId)}/highlight?quote=${encodeURIComponent(state.quote)}`,
            { method: "GET" },
          )
            .then((hl) => {
              if (token !== state.openToken || !hl) {
                return;
              }
              state.highlightData = hl;
              if (hl.media_type) {
                state.mediaType = hl.media_type;
              }
              if (Number(hl.page) > 0) {
                state.targetPage = Number(hl.page);
              }
              if (Number(hl.page_count) > 0) {
                state.pageCount = Math.max(state.pageCount, Number(hl.page_count));
              }
            })
            .catch(() => null),
        );
      }

      await Promise.all(tasks);
      if (token !== state.openToken) {
        return;
      }

      if (state.documentId && supportsOriginalPages()) {
        await showOriginalMode({ jumpToTarget: true });
      } else {
        showPresentationMode();
      }
    }

    async function openDocumentViewer(doc, { startMode = "presentation" } = {}) {
      const token = ++state.openToken;
      state.documentId = doc?.id || null;
      state.fragmentId = doc?.fragment_id || null;
      state.mediaType = doc?.media_type || "pdf";
      state.quote = "";
      state.targetPage = 1;
      state.currentPage = 1;
      state.pageCount = 1;
      state.zoom = 1.0;
      state.fitWidth = true;
      state.highlightData = null;
      state.originalInfo = null;
      state.sourcePath =
        A.safeApiPath(doc?.presentation_url, ["/api/documents/", "/api/sources/"]) ||
        (doc?.status === "ready" && doc?.id
          ? `/api/documents/${encodeURIComponent(doc.id)}/view`
          : null);
      state.downloadUrl = state.documentId
        ? `/api/documents/${encodeURIComponent(state.documentId)}/original`
        : null;

      A.setText(sourceTitle, doc?.title || doc?.name || "Документ");
      A.setText(
        sourceLocator,
        [doc?.media_type ? String(doc.media_type).toUpperCase() : null, doc?.version ? `версия ${doc.version}` : null]
          .filter(Boolean)
          .join(" · "),
      );
      if (sourceQuote) {
        A.show(sourceQuote, false);
      }
      clearError();
      clearNotice();

      A.show(sourcePresentationBtn, Boolean(state.sourcePath));
      if (state.downloadUrl && sourceOriginal) {
        sourceOriginal.href = state.downloadUrl;
        A.show(sourceOriginal, true);
      } else {
        A.show(sourceOriginal, false);
      }
      if (state.downloadUrl && sourceDownloadOriginal) {
        sourceDownloadOriginal.href = state.downloadUrl;
        A.show(sourceDownloadOriginal, true);
      } else {
        A.show(sourceDownloadOriginal, false);
      }
      if (state.sourcePath && sourceNewTab) {
        sourceNewTab.href = state.sourcePath;
        A.show(sourceNewTab, true);
      } else {
        A.show(sourceNewTab, false);
      }
      if (sourceFrame) {
        sourceFrame.removeAttribute("data-loaded-src");
      }

      if (startMode === "original" || !state.sourcePath) {
        A.openDialog(sourceDialog);
        await showOriginalMode({ jumpToTarget: true });
      } else {
        showPresentationMode();
        A.openDialog(sourceDialog);
      }

      if (token === state.openToken && state.documentId && !state.originalInfo) {
        try {
          const info = await A.apiFetch(
            `/api/documents/${encodeURIComponent(state.documentId)}/original-info`,
            { method: "GET" },
          );
          if (token === state.openToken && info) {
            state.originalInfo = info;
            state.pageCount = Math.max(1, Number(info.page_count) || 1);
            updatePageControls();
          }
        } catch {
          // Ignore optional metadata preload error.
        }
      }
    }

    sourcePresentationBtn?.addEventListener("click", () => {
      showPresentationMode();
    });

    sourceOriginal?.addEventListener("click", (event) => {
      event.preventDefault();
      showOriginalMode({ jumpToTarget: true });
    });

    sourcePrevPage?.addEventListener("click", () => {
      if (state.mode !== "original") {
        state.currentPage = Math.max(1, state.currentPage - 1);
        showOriginalMode({ jumpToTarget: false });
        return;
      }
      loadOriginalPage(state.currentPage - 1, { scrollToHighlight: false });
    });

    sourceNextPage?.addEventListener("click", () => {
      if (state.mode !== "original") {
        state.currentPage = Math.min(state.pageCount, state.currentPage + 1);
        showOriginalMode({ jumpToTarget: false });
        return;
      }
      loadOriginalPage(state.currentPage + 1, { scrollToHighlight: false });
    });

    sourcePageInput?.addEventListener("change", () => {
      const target = Math.max(1, Math.min(state.pageCount, Number(sourcePageInput.value) || 1));
      if (state.mode !== "original") {
        state.currentPage = target;
        showOriginalMode({ jumpToTarget: false });
        return;
      }
      loadOriginalPage(target, { scrollToHighlight: target === state.targetPage });
    });

    function setZoom(nextZoom, fit = false) {
      state.fitWidth = Boolean(fit);
      state.zoom = Math.max(0.4, Math.min(2.5, Number(nextZoom) || 1.0));
      updateZoomLabel();
      applyCanvasSize();
      renderHighlightBoxes({ scrollIntoView: false });
    }

    sourceZoomOut?.addEventListener("click", () => setZoom(state.zoom - 0.15, false));
    sourceZoomIn?.addEventListener("click", () => setZoom(state.zoom + 0.15, false));
    sourceZoomReset?.addEventListener("click", () => setZoom(1.0, false));
    sourceZoomFit?.addEventListener("click", () => setZoom(1.0, true));

    window.addEventListener("resize", () => {
      if (sourceDialog.open) {
        applyCanvasSize();
        renderHighlightBoxes({ scrollIntoView: false });
      }
    });

    sourceFrame?.addEventListener("load", () => {
      if (sourceFrame.src && sourceFrame.src !== "about:blank") {
        clearError();
      }
    });

    sourceFrame?.addEventListener("error", () => {
      showError("Не удалось загрузить представление источника. Откройте оригинал.");
    });

    sourceDialog.addEventListener("close", () => {
      state.openToken += 1;
      closeFrame();
      clearHighlights();
      if (sourceOriginalImage) {
        sourceOriginalImage.removeAttribute("src");
        A.show(sourceOriginalImage, false);
      }
    });

    return {
      openCitation,
      openDocumentViewer,
    };
  }

  if (window.Altropos) {
    window.Altropos.createSourceViewer = attachSourceViewer;
  } else {
    document.addEventListener("DOMContentLoaded", () => {
      if (window.Altropos) {
        window.Altropos.createSourceViewer = attachSourceViewer;
      }
    });
  }
})();
