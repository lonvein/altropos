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
      loadedScale: 0,
      loadedPage: 0,
    };
    const CSS_PX_PER_PT = 96 / 72; // 100% zoom = the page at its natural CSS size
    const MIN_ZOOM = 0.4;
    const MAX_ZOOM = 3;

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

    // Page width in PDF points as the server renders it (viewer frame).
    function pageWidthPt() {
      const info = state.originalInfo;
      const meta = info && Array.isArray(info.pages)
        ? info.pages.find((p) => Number(p.page) === Number(state.currentPage))
        : null;
      const w = Number(meta?.width) || Number(state.highlightData?.page_width);
      if (w > 0) {
        return w;
      }
      const nat = sourceOriginalImage?.naturalWidth || 0;
      return nat > 0 && state.loadedScale > 0 ? nat / state.loadedScale : 595;
    }

    function availableWidth() {
      if (!sourceViewport) {
        return 780;
      }
      const cs = window.getComputedStyle(sourceViewport);
      const pad = (parseFloat(cs.paddingLeft) || 0) + (parseFloat(cs.paddingRight) || 0);
      return Math.max(280, sourceViewport.clientWidth - pad - 4);
    }

    function computeImageTargetWidth() {
      const natural = pageWidthPt() * CSS_PX_PER_PT;
      const avail = availableWidth();
      if (state.fitWidth) {
        state.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, avail / Math.max(natural, 1)));
        updateZoomLabel();
        return Math.round(avail);
      }
      return Math.max(200, Math.round(natural * state.zoom));
    }

    // Rendered bitmap resolution follows what is on screen, so zoomed text stays sharp.
    function desiredScale(widthPx) {
      const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
      const raw = (widthPx * dpr) / Math.max(pageWidthPt(), 1);
      return Math.max(1, Math.min(3.5, Math.ceil(raw * 4) / 4));
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
        // The canvas owns the width; image and highlight layer both fill it exactly, so
        // percentage boxes can never drift away from the page bitmap.
        if (sourceOriginalCanvas) {
          sourceOriginalCanvas.style.width = `${widthPx}px`;
        }
        sourceOriginalImage.style.width = "100%";
        sourceOriginalImage.style.height = "auto";
        maybeUpgradeBitmap(widthPx);
      } else if (sourceFrame) {
        sourceFrame.style.transformOrigin = "top left";
        if (state.fitWidth || Math.abs(state.zoom - 1.0) < 0.01) {
          sourceFrame.style.transform = "";
          sourceFrame.style.width = "100%";
          sourceFrame.style.height = "100%";
        } else {
          const pct = 100 / Math.max(MIN_ZOOM, state.zoom);
          sourceFrame.style.transform = `scale(${state.zoom})`;
          sourceFrame.style.width = `${pct}%`;
          sourceFrame.style.height = `${pct}%`;
        }
      }
    }

    let upgradeTimer = 0;
    function maybeUpgradeBitmap(widthPx) {
      if (!state.documentId || !state.loadedPage) {
        return;
      }
      const wanted = desiredScale(widthPx);
      if (wanted <= state.loadedScale + 0.01) {
        return;
      }
      window.clearTimeout(upgradeTimer);
      const token = state.openToken;
      upgradeTimer = window.setTimeout(() => {
        if (token === state.openToken && state.mode === "original" && sourceDialog.open) {
          requestPageBitmap(state.currentPage, wanted, { keepScroll: true });
        }
      }, 180);
    }

    function highlightUnion() {
      const boxes = sourceHighlightLayer ? sourceHighlightLayer.querySelectorAll(".original-highlight-box") : [];
      if (!boxes.length) {
        return null;
      }
      let top = Infinity;
      let left = Infinity;
      let bottom = -Infinity;
      let right = -Infinity;
      boxes.forEach((el) => {
        const r = el.getBoundingClientRect();
        top = Math.min(top, r.top);
        left = Math.min(left, r.left);
        bottom = Math.max(bottom, r.bottom);
        right = Math.max(right, r.right);
      });
      return { top, left, bottom, right };
    }

    // Bring the highlighted passage into view: vertically near the upper third, horizontally
    // only when zoom made the page wider than the viewport.
    function scrollToHighlight() {
      if (!sourceViewport) {
        return;
      }
      const union = highlightUnion();
      if (!union) {
        return;
      }
      const vp = sourceViewport.getBoundingClientRect();
      const height = union.bottom - union.top;
      const margin = sourceViewport.clientHeight * (height > sourceViewport.clientHeight * 0.7 ? 0.08 : 0.3);
      let dy = union.top - vp.top - margin;
      let dx = 0;
      if (union.right > vp.right - 12 || union.left < vp.left + 12) {
        dx = union.left - vp.left - Math.max(24, (sourceViewport.clientWidth - (union.right - union.left)) / 2);
      }
      if (!Number.isFinite(dy)) {
        dy = 0;
      }
      sourceViewport.scrollTo({
        top: Math.max(0, sourceViewport.scrollTop + dy),
        left: Math.max(0, sourceViewport.scrollLeft + (Number.isFinite(dx) ? dx : 0)),
        behavior: "auto",
      });
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

        if (scrollIntoView && sourceViewport) {
          // Two frames: the first lets the new canvas size and image settle into layout.
          window.requestAnimationFrame(() => window.requestAnimationFrame(scrollToHighlight));
        }
        const other = (hl.pages || []).filter((n) => Number(n) !== Number(state.currentPage));
        if (other.length > 0) {
          showNotice(`Фрагмент продолжается на стр. ${other.join(", ")}.`);
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

    function requestPageBitmap(pageNum, scale, { keepScroll = false, scrollToTarget = false } = {}) {
      const token = state.openToken;
      const url = `/api/documents/${encodeURIComponent(state.documentId)}/original-page/${pageNum}?scale=${scale}`;
      const apply = () => {
        if (token !== state.openToken || state.mode !== "original") {
          return;
        }
        state.loadedScale = scale;
        state.loadedPage = pageNum;
        sourceOriginalImage.onload = () => {
          if (token !== state.openToken || state.mode !== "original") {
            return;
          }
          clearError();
          if (!keepScroll) {
            applyCanvasSize();
          }
          renderHighlightBoxes({ scrollIntoView: scrollToTarget });
        };
        sourceOriginalImage.onerror = () => {
          if (token !== state.openToken) {
            return;
          }
          A.show(sourceOriginalImage, false);
          clearHighlights();
          showError("Не удалось загрузить страницу оригинала. Вы можете скачать исходный файл.");
        };
        sourceOriginalImage.src = url;
      };
      if (!keepScroll) {
        apply();
        return;
      }
      // Sharper bitmap for the current zoom: decode off-screen, then swap without flicker.
      const probe = new Image();
      probe.onload = apply;
      probe.src = url;
    }

    function loadOriginalPage(pageNum, { scrollToHighlight: scrollToTarget = true } = {}) {
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
      A.show(sourceOriginalImage, true);
      clearHighlights();
      // Size the canvas before the bitmap arrives so highlights are placed on the final box.
      applyCanvasSizeWithoutUpgrade();
      const scale = desiredScale(sourceOriginalCanvas?.clientWidth || availableWidth());
      requestPageBitmap(clamped, scale, { scrollToTarget });
    }

    function applyCanvasSizeWithoutUpgrade() {
      const saved = state.loadedPage;
      state.loadedPage = 0; // suppress bitmap upgrade while the first bitmap is still pending
      applyCanvasSize();
      state.loadedPage = saved;
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
      state.loadedScale = 0;
      state.loadedPage = 0;
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
      state.loadedScale = 0;
      state.loadedPage = 0;
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

    // Keep the same spot of the page under the viewport centre while the page is rescaled.
    function captureAnchor() {
      if (state.mode !== "original" || !sourceViewport || !sourceOriginalCanvas) {
        return null;
      }
      const vp = sourceViewport.getBoundingClientRect();
      const c = sourceOriginalCanvas.getBoundingClientRect();
      if (!c.width || !c.height) {
        return null;
      }
      return {
        fx: (vp.left + sourceViewport.clientWidth / 2 - c.left) / c.width,
        fy: (vp.top + sourceViewport.clientHeight / 2 - c.top) / c.height,
      };
    }

    function restoreAnchor(anchor) {
      if (!anchor || !sourceViewport || !sourceOriginalCanvas) {
        return;
      }
      const vp = sourceViewport.getBoundingClientRect();
      const c = sourceOriginalCanvas.getBoundingClientRect();
      const dx = c.left + anchor.fx * c.width - (vp.left + sourceViewport.clientWidth / 2);
      const dy = c.top + anchor.fy * c.height - (vp.top + sourceViewport.clientHeight / 2);
      if (Number.isFinite(dx) && Number.isFinite(dy)) {
        sourceViewport.scrollLeft += dx;
        sourceViewport.scrollTop += dy;
      }
    }

    function setZoom(nextZoom, fit = false) {
      const anchor = captureAnchor();
      state.fitWidth = Boolean(fit);
      state.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, Number(nextZoom) || 1.0));
      updateZoomLabel();
      applyCanvasSize();
      renderHighlightBoxes({ scrollIntoView: false });
      restoreAnchor(anchor);
    }

    sourceZoomOut?.addEventListener("click", () => setZoom(state.zoom - 0.15, false));
    sourceZoomIn?.addEventListener("click", () => setZoom(state.zoom + 0.15, false));
    sourceZoomReset?.addEventListener("click", () => setZoom(1.0, false));
    sourceZoomFit?.addEventListener("click", () => setZoom(1.0, true));

    // Ctrl/Cmd + wheel (and trackpad pinch) zooms the page, not the whole app.
    sourceViewport?.addEventListener(
      "wheel",
      (event) => {
        if (state.mode !== "original" || !(event.ctrlKey || event.metaKey)) {
          return;
        }
        event.preventDefault();
        setZoom(state.zoom * (event.deltaY < 0 ? 1.1 : 1 / 1.1), false);
      },
      { passive: false },
    );

    // Fit-to-width pages follow the dialog size; manual zoom keeps its scale. Boxes are
    // percentages of the canvas, so only the canvas needs resizing.
    let resizeFrame = 0;
    function onViewportResize() {
      if (!sourceDialog.open || state.mode !== "original") {
        return;
      }
      window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        const anchor = captureAnchor();
        applyCanvasSize();
        renderHighlightBoxes({ scrollIntoView: false });
        restoreAnchor(anchor);
      });
    }
    window.addEventListener("resize", onViewportResize);
    if (typeof ResizeObserver === "function" && sourceViewport) {
      let lastWidth = 0;
      new ResizeObserver((entries) => {
        const width = Math.round(entries[0]?.contentRect?.width || 0);
        if (width !== lastWidth) {
          lastWidth = width;
          onViewportResize();
        }
      }).observe(sourceViewport);
    }

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
      state.loadedScale = 0;
      state.loadedPage = 0;
      window.clearTimeout(upgradeTimer);
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
