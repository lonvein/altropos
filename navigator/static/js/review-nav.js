(function () {
  const buttons = document.querySelectorAll("[data-review-back]");
  if (!buttons || buttons.length === 0) {
    return;
  }

  let storedReturn = null;
  try {
    storedReturn = window.sessionStorage.getItem("altropos.review.returnUrl");
  } catch {
    storedReturn = null;
  }
  const safeStored =
    storedReturn && storedReturn.startsWith("/") && !storedReturn.startsWith("//")
      ? storedReturn
      : null;

  buttons.forEach((button) => {
    const scope = button.getAttribute("data-review-scope") || "index";
    const fallback = button.getAttribute("data-back-fallback") || "/documents";
    if (scope !== "package" && safeStored && button.tagName === "A") {
      button.setAttribute("href", safeStored);
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
      const target = safeStored || fallback;
      if (window.history.length > 1 && safeStored) {
        window.history.back();
        return;
      }
      window.location.assign(target);
    });
  });
})();
