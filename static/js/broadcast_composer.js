/**
 * Shared HTML message composer (toolbar + live preview) for broadcast pages.
 * Call initBroadcastComposer(prefix, { showAlert }) per composer instance.
 */
(function () {
  const ALLOWED_TAGS = new Set([
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "a", "span", "br",
  ]);
  const TAG_ALIASES = {
    strong: "b",
    em: "i",
    ins: "u",
    strike: "s",
    del: "s",
  };
  const SAFE_HREF = /^(https?:|tg:|mailto:)/i;

  function escapeAttr(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function sanitizeNode(node, out) {
    if (node.nodeType === Node.TEXT_NODE) {
      out.appendChild(document.createTextNode(node.textContent));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;

    let tag = node.tagName.toLowerCase();
    if (TAG_ALIASES[tag]) tag = TAG_ALIASES[tag];

    if (tag === "br") {
      out.appendChild(document.createElement("br"));
      return;
    }

    if (!ALLOWED_TAGS.has(tag) && !TAG_ALIASES[node.tagName.toLowerCase()]) {
      node.childNodes.forEach((child) => sanitizeNode(child, out));
      return;
    }

    if (tag === "span") {
      const cls = node.getAttribute("class") || "";
      if (!cls.includes("tg-spoiler")) {
        node.childNodes.forEach((child) => sanitizeNode(child, out));
        return;
      }
    }

    const el = document.createElement(tag);

    if (tag === "a") {
      const href = (node.getAttribute("href") || "").trim();
      if (href && SAFE_HREF.test(href)) {
        el.setAttribute("href", href);
        el.setAttribute("rel", "noopener noreferrer");
        el.setAttribute("target", "_blank");
      } else {
        node.childNodes.forEach((child) => sanitizeNode(child, out));
        return;
      }
    }

    if (tag === "span" && (node.getAttribute("class") || "").includes("tg-spoiler")) {
      el.className = "tg-spoiler";
      el.style.background = "rgba(100,116,139,0.45)";
      el.style.borderRadius = "0.2rem";
      el.style.padding = "0 0.15rem";
    }

    node.childNodes.forEach((child) => sanitizeNode(child, el));
    out.appendChild(el);
  }

  function createComposer(prefix, options) {
    const showAlert = options.showAlert || function () {};
    const textarea = document.getElementById(prefix + "-message-html");
    const charCount = document.getElementById(prefix + "-char-count");
    const livePreview = document.getElementById(prefix + "-live-preview");
    const toolbar = document.getElementById(prefix + "-toolbar");

    if (!textarea || !charCount || !livePreview || !toolbar) {
      return null;
    }

    function renderLivePreview() {
      const raw = textarea.value;
      charCount.textContent = String(raw.length);

      if (!raw.trim()) {
        livePreview.innerHTML =
          '<p class="tg-preview-empty m-0">ستظهر معاينة الرسالة هنا أثناء الكتابة…</p>';
        return;
      }

      const parser = new DOMParser();
      const withBreaks = raw.replace(/\n/g, "<br>");
      const doc = parser.parseFromString("<div>" + withBreaks + "</div>", "text/html");
      const container = document.createElement("div");
      const source = doc.body.firstElementChild;
      if (source) {
        source.childNodes.forEach((child) => sanitizeNode(child, container));
      }
      livePreview.innerHTML = "";
      if (container.childNodes.length) {
        livePreview.appendChild(container);
      } else {
        livePreview.textContent = raw;
      }
    }

    function insertAtCursor(before, after, placeholder) {
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const value = textarea.value;
      const selected = value.slice(start, end);
      const inner = selected || placeholder || "";
      const insertion = before + inner + after;
      textarea.setRangeText(insertion, start, end, "end");
      if (!selected && placeholder) {
        const selectStart = start + before.length;
        textarea.setSelectionRange(selectStart, selectStart + placeholder.length);
      } else {
        const cursor = start + insertion.length;
        textarea.setSelectionRange(cursor, cursor);
      }
      textarea.focus();
      renderLivePreview();
    }

    function wrapSelection(tag) {
      const start = textarea.selectionStart;
      const end = textarea.selectionEnd;
      const value = textarea.value;
      const selected = value.slice(start, end);

      if (tag === "a") {
        const urlInput = window.prompt(
          "أدخل رابط URL (يجب أن يبدأ بـ https:// أو http:// أو tg:// أو mailto:):",
          "https://"
        );
        if (urlInput === null) return;
        const url = urlInput.trim();
        if (!url) {
          showAlert("لم يتم إدخال رابط.");
          return;
        }
        if (!SAFE_HREF.test(url)) {
          showAlert("رابط غير مسموح. استخدم https:// أو http:// أو tg:// أو mailto: فقط.");
          return;
        }
        const label = selected || "نص الرابط";
        insertAtCursor('<a href="' + escapeAttr(url) + '">', "</a>", label);
        return;
      }

      const open = "<" + tag + ">";
      const close = "</" + tag + ">";
      const placeholders = {
        b: "نص عريض",
        i: "نص مائل",
        u: "نص مسطّر",
        s: "نص مشطوب",
        code: "كود",
      };
      insertAtCursor(open, close, placeholders[tag] || "");
    }

    toolbar.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-format]");
      if (!btn) return;
      e.preventDefault();
      const format = btn.getAttribute("data-format");
      if (format === "a") {
        wrapSelection("a");
      } else if (format) {
        wrapSelection(format);
      }
    });

    textarea.addEventListener("input", renderLivePreview);
    textarea.addEventListener("keyup", renderLivePreview);
    textarea.addEventListener("paste", () => {
      requestAnimationFrame(renderLivePreview);
    });

    renderLivePreview();

    return {
      getMessage() {
        return textarea.value.trim();
      },
      clear() {
        textarea.value = "";
        renderLivePreview();
      },
      setDisabled(disabled) {
        textarea.disabled = disabled;
        toolbar.querySelectorAll("button").forEach((b) => {
          b.disabled = disabled;
        });
      },
    };
  }

  window.initBroadcastComposer = createComposer;
})();
