/* Soldium Catalog core UI — Phase 2–5 (sources, commercial, pricing, readiness). */
(function () {
  const API = "/api/soldium-catalog";

  const STATUS_AR = {
    draft: "مسودة",
    active: "نشطة",
    archived: "مؤرشفة",
  };

  const SOURCE_STATUS_AR = {
    active: "نشط",
    historical: "سابق",
  };

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  function readinessBadge(r) {
    if (!r) return `<span class="text-slate-500 text-xs">—</span>`;
    if (r.ready || r.state === "ready") {
      return `<span class="text-emerald-300 text-xs">✓ جاهزة</span>`;
    }
    return `<span class="text-amber-300 text-xs">⚠ تحتاج مراجعة</span>`;
  }

  function readinessPanelHtml(readiness) {
    const r = readiness || {};
    const checks = r.checks || [];
    const issues = r.issues || [];
    const checksHtml = checks
      .map((c) => {
        const mark = c.ok ? "✓" : "❌";
        const color = c.ok ? "text-emerald-300" : "text-rose-300";
        return `<div class="${color} text-sm">${mark} ${esc(c.label_ar || c.check)}</div>`;
      })
      .join("");
    const stateHtml =
      r.ready || r.state === "ready"
        ? `<div class="text-emerald-300 font-medium mt-2">الحالة: ✓ جاهزة</div>
           <p class="text-[11px] text-slate-500 mt-1">جاهزة للنشر لاحقًا</p>`
        : `<div class="text-amber-300 font-medium mt-2">الحالة: ⚠ تحتاج مراجعة</div>`;
    const issuesHtml = issues.length
      ? `<div class="mt-3">
           <div class="text-xs text-slate-400 mb-1">سبب عدم الجاهزية</div>
           <ul class="list-disc ps-5 space-y-1 text-sm text-rose-200">
             ${issues
               .map(
                 (i) =>
                   `<li><span class="font-medium">${esc(i.title)}</span>
                    <span class="text-slate-400 text-xs block">${esc(i.message)}</span></li>`
               )
               .join("")}
           </ul>
         </div>`
      : "";
    const fixBtns = [];
    const actions = new Set(issues.map((i) => i.fix_action).filter(Boolean));
    if (actions.has("source")) {
      fixBtns.push(
        `<button type="button" id="btn-fix-source" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">إصلاح مصدر التنفيذ</button>`
      );
    }
    if (actions.has("price")) {
      fixBtns.push(
        `<button type="button" id="btn-fix-price" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">إضافة السعر</button>`
      );
    }
    if (actions.has("commercial")) {
      fixBtns.push(
        `<button type="button" id="btn-fix-commercial" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">إصلاح البيانات التجارية</button>`
      );
    }
    if (actions.has("placement")) {
      fixBtns.push(
        `<a href="/catalog/structure" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs inline-block">إصلاح التنظيم</a>`
      );
    }
    if (actions.has("target")) {
      fixBtns.push(
        `<button type="button" id="btn-fix-target" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">إصلاح متطلبات الرابط</button>`
      );
    }
    if (actions.has("fulfillment")) {
      fixBtns.push(
        `<button type="button" id="btn-fix-fulfillment" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">إصلاح طريقة التنفيذ</button>`
      );
    }
    const fixesHtml = fixBtns.length
      ? `<div class="flex flex-wrap gap-2 mt-3">${fixBtns.join("")}</div>`
      : "";
    return `
      <div class="cat-section" id="readiness-section">
        <h4>جاهزية الخدمة</h4>
        <div class="space-y-1">${checksHtml || `<div class="text-slate-500 text-sm">—</div>`}</div>
        ${stateHtml}
        ${issuesHtml}
        ${fixesHtml}
      </div>`;
  }

  function sourceLabel(src) {
    if (!src) return "بدون مصدر";
    const provider = src.provider_name || src.provider_slug || "—";
    const account = src.account_display_name || src.provider_account_key || "—";
    return `${provider} / ${account}`;
  }

  function sourceFullLabel(src) {
    if (!src) return "بدون مصدر";
    return `${sourceLabel(src)} / ${opaqueProviderId(src) || "—"}`;
  }

  /** Opaque Provider Service ID — never coerce to Number. */
  function opaqueProviderId(src) {
    if (!src || src.external_service_id == null || src.external_service_id === "") {
      return null;
    }
    return String(src.external_service_id);
  }

  function copyProviderIdBtn(id) {
    if (id == null || id === "") return "";
    const text = String(id);
    return `<button type="button" class="cat-copy-pid rounded border border-slate-600 px-1.5 py-0.5 text-[10px] text-slate-300 hover:text-white hover:border-slate-400" data-copy-pid="${esc(
      text
    )}" aria-label="نسخ معرّف المزود" title="نسخ">نسخ</button>`;
  }

  /** Central presentation: Provider Service ID first when rendering a service row. */
  function providerIdPrimaryHtml(src, { compact = false } = {}) {
    const id = opaqueProviderId(src);
    if (!id) {
      return `<div class="text-slate-500 ${compact ? "text-[11px]" : "text-xs"}">بدون معرّف مزود</div>`;
    }
    return `<div class="cat-provider-id space-y-0.5">
      <div class="text-[10px] text-slate-500">معرّف المزود</div>
      <div class="flex items-center gap-2 flex-wrap">
        <code class="text-sky-200 font-mono ${compact ? "text-xs" : "text-sm"}" dir="ltr">${esc(
          id
        )}</code>
        ${copyProviderIdBtn(id)}
      </div>
    </div>`;
  }

  function wireCopyProviderIds(root) {
    const scope = root || document;
    scope.querySelectorAll("[data-copy-pid]").forEach((btn) => {
      if (btn.dataset.copyBound === "1") return;
      btn.dataset.copyBound = "1";
      btn.addEventListener("click", async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const raw = btn.getAttribute("data-copy-pid") || "";
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(raw);
          } else {
            const ta = document.createElement("textarea");
            ta.value = raw;
            ta.setAttribute("readonly", "");
            ta.style.position = "fixed";
            ta.style.opacity = "0";
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
          }
          alertBox("تم نسخ معرّف المزود");
        } catch (_) {
          alertBox("تعذر النسخ", "err");
        }
      });
    });
  }

  function httpErrorMessage(data, res, rawText) {
    const detail = data && data.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim();
    if (detail && typeof detail === "object") {
      if (typeof detail.message === "string" && detail.message.trim()) {
        return detail.message.trim();
      }
      if (Array.isArray(detail) && detail.length) {
        const parts = detail
          .map((d) => (d && (d.msg || d.message)) || "")
          .filter(Boolean);
        if (parts.length) return parts.join(" · ");
      }
    }
    if (data && typeof data.message === "string" && data.message.trim()) {
      return data.message.trim();
    }
    const text = (rawText || "").trim();
    // Avoid surfacing bare Starlette/nginx "Internal Server Error" when we have status.
    if (text && !/^internal server error$/i.test(text) && text.length < 400) {
      return text;
    }
    if (res && res.status === 409) return "تعارض في البيانات — تعذر إكمال العملية";
    if (res && res.status >= 500) {
      return "فشل الخادم أثناء العملية — أعد المحاولة أو راجع السجلات";
    }
    if (res && res.statusText && !/^internal server error$/i.test(res.statusText)) {
      return res.statusText;
    }
    return "فشل الطلب";
  }

  async function json(url, options = {}) {
    const res = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const rawText = await res.text();
    let data = {};
    if (rawText) {
      try {
        data = JSON.parse(rawText);
      } catch (_) {
        data = {};
      }
    }
    if (!res.ok) {
      // #region agent log
      fetch('http://127.0.0.1:7300/ingest/fb230002-436a-430b-84ba-58e474409a6c',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'3df71b'},body:JSON.stringify({sessionId:'3df71b',runId:'pre-fix',hypothesisId:'C',location:'catalog_core_ui.js:json',message:'fetch not ok',data:{url:String(url),method:(options&&options.method)||'GET',status:res.status,statusText:res.statusText,bodyPreview:String(rawText||'').slice(0,800),parsedDetail:data&&data.detail,reqBodyPreview:options&&options.body?String(options.body).slice(0,400):null},timestamp:Date.now()})}).catch(()=>{});
      // #endregion
      throw new Error(httpErrorMessage(data, res, rawText));
    }
    return data;
  }

  function alertBox(message, type = "ok") {
    const el = document.getElementById("cat-alert");
    if (!el) return;
    el.classList.remove(
      "hidden",
      "border-emerald-700",
      "bg-emerald-950/40",
      "text-emerald-200",
      "border-red-700",
      "bg-red-950/40",
      "text-red-200"
    );
    if (type === "ok") {
      el.classList.add("border-emerald-700", "bg-emerald-950/40", "text-emerald-200");
    } else {
      el.classList.add("border-red-700", "bg-red-950/40", "text-red-200");
    }
    el.textContent = message;
  }

  function closeModal() {
    const root = document.getElementById("modal-root");
    if (!root) return;
    root.classList.add("hidden");
    root.innerHTML = "";
    root.setAttribute("aria-hidden", "true");
  }

  function showModalError(message) {
    const root = document.getElementById("modal-root");
    const errEl = root && root.querySelector("#cat-modal-error");
    if (errEl) {
      errEl.textContent = message || "فشل الطلب";
      errEl.classList.remove("hidden");
      errEl.scrollIntoView({ block: "nearest" });
      return;
    }
    alertBox(message, "err");
  }

  function clearModalError() {
    const root = document.getElementById("modal-root");
    const errEl = root && root.querySelector("#cat-modal-error");
    if (!errEl) return;
    errEl.textContent = "";
    errEl.classList.add("hidden");
  }

  function openModal({ title, bodyHtml, onSubmit, submitLabel = "حفظ", hideFormActions = false }) {
    const root = document.getElementById("modal-root");
    if (!root) return;
    root.classList.remove("hidden");
    root.setAttribute("aria-hidden", "false");
    const actions = hideFormActions
      ? ""
      : `<div class="flex justify-end gap-2 pt-2">
            <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إلغاء</button>
            <button type="submit" class="rounded-lg bg-accent-600 hover:bg-accent-500 px-3 py-2">${esc(submitLabel)}</button>
          </div>`;
    root.innerHTML = `
      <div class="cat-modal-backdrop" data-close="1"></div>
      <div class="cat-modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="flex items-center justify-between gap-2 mb-3">
          <h3 class="text-base font-semibold text-white">${esc(title)}</h3>
          <button type="button" class="text-slate-400 hover:text-white text-sm" data-close="1">إغلاق</button>
        </div>
        <form id="cat-modal-form" class="space-y-3 text-sm">
          ${bodyHtml}
          <div id="cat-modal-error" class="hidden rounded-lg border border-red-700 bg-red-950/40 text-red-200 px-3 py-2 text-sm" role="alert"></div>
          ${actions}
        </form>
      </div>`;
    root.querySelectorAll("[data-close]").forEach((el) => {
      el.addEventListener("click", closeModal);
    });
    root.querySelector("#cat-modal-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!onSubmit) return;
      clearModalError();
      try {
        const result = await onSubmit(new FormData(e.target));
        if (result !== false) closeModal();
      } catch (err) {
        // Keep modal open; show error inside the dialog (above backdrop/page alert).
        showModalError(err.message || "فشل الطلب");
      }
    });
    return root;
  }

  async function loadProviderOptions() {
    const data = await json("/api/providers");
    const providers = data.providers || data.items || data || [];
    // providers_admin returns a list directly or wrapped — normalize
    const list = Array.isArray(providers) ? providers : [];
    return list.map((p) => ({
      slug: p.slug,
      name: p.name || p.slug,
      accounts: (p.accounts || []).map((a) => ({
        key: a.account_key,
        label: a.display_name || a.account_key,
      })),
    }));
  }

  function flattenNodeEntries(tree, acc = [], path = []) {
    for (const item of tree || []) {
      if (item.entry_type === "node") {
        const nextPath = [...path, item.name_ar];
        acc.push({
          entry_id: item.id,
          label: nextPath.join(" › "),
          name_ar: item.name_ar,
        });
        flattenNodeEntries(item.children || [], acc, nextPath);
      }
    }
    return acc;
  }

  function destinationSelectHtml(tree, selectedEntryId) {
    const nodes = flattenNodeEntries(tree);
    const options = [
      `<option value="">الجذر (أعلى المستوى)</option>`,
      ...nodes.map(
        (n) =>
          `<option value="${esc(n.entry_id)}" ${
            n.entry_id === selectedEntryId ? "selected" : ""
          }>${esc(n.label)}</option>`
      ),
    ];
    return `
      <label class="block text-xs text-slate-500 mb-1">المكان</label>
      <select name="parent_entry_id" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">
        ${options.join("")}
      </select>`;
  }

  // ── Structure page ──

  function mountStructure() {
    let tree = [];
    let selected = null; // { type, id, entry_id, parent_entry_id, name_ar, status, node_id|service_id }
    let sheetOpen = false;
    let savedScrollY = 0;
    let lastFocusEl = null;

    const DESKTOP_MQ = "(min-width: 1280px)";

    function isDesktopLayout() {
      return window.matchMedia(DESKTOP_MQ).matches;
    }

    function detailsPane() {
      return document.getElementById("structure-details-pane");
    }
    function sheetBackdrop() {
      return document.getElementById("structure-sheet-backdrop");
    }

    function openStructureSheet() {
      if (isDesktopLayout()) return;
      const pane = detailsPane();
      const backdrop = sheetBackdrop();
      if (!pane || !backdrop) return;
      if (!sheetOpen) {
        savedScrollY = window.scrollY || window.pageYOffset || 0;
        lastFocusEl = document.activeElement;
      }
      sheetOpen = true;
      pane.classList.add("is-open");
      backdrop.classList.add("is-open");
      pane.setAttribute("aria-hidden", "false");
      backdrop.setAttribute("aria-hidden", "false");
      pane.setAttribute("role", "dialog");
      pane.setAttribute("aria-modal", "true");
      document.body.classList.add("cat-structure-sheet-open");
      const closeBtn = document.getElementById("structure-sheet-close");
      if (closeBtn) closeBtn.focus();
    }

    function closeStructureSheet() {
      const pane = detailsPane();
      const backdrop = sheetBackdrop();
      if (!pane) return;
      const wasOpen = sheetOpen;
      sheetOpen = false;
      pane.classList.remove("is-open");
      if (backdrop) {
        backdrop.classList.remove("is-open");
        backdrop.setAttribute("aria-hidden", "true");
      }
      pane.setAttribute("aria-hidden", "true");
      pane.setAttribute("role", "region");
      pane.removeAttribute("aria-modal");
      document.body.classList.remove("cat-structure-sheet-open");
      if (wasOpen && !isDesktopLayout()) {
        window.scrollTo(0, savedScrollY);
        if (lastFocusEl && typeof lastFocusEl.focus === "function") {
          try {
            lastFocusEl.focus();
          } catch (_) {
            /* ignore */
          }
        }
      }
    }

    function syncPresentationAfterInspector() {
      if (isDesktopLayout()) {
        closeStructureSheet();
        const pane = detailsPane();
        if (pane) {
          pane.setAttribute("aria-hidden", "false");
          pane.setAttribute("role", "region");
        }
        return;
      }
      if (selected) openStructureSheet();
      else closeStructureSheet();
    }

    async function withPreservedScroll(fn) {
      const y = window.scrollY || window.pageYOffset || 0;
      try {
        await fn();
      } finally {
        window.scrollTo(0, y);
      }
    }

    function renderTree(nodes, depth = 0) {
      return (nodes || [])
        .map((n) => {
          const isNode = n.entry_type === "node";
          const key = isNode ? n.node_id : n.service_id;
          const active =
            selected && selected.entry_id === n.id ? "active" : "";
          const kind = isNode ? "قسم" : "خدمة";
          const kids =
            isNode && (n.children || []).length
              ? `<div class="cat-tree-children">${renderTree(n.children, depth + 1)}</div>`
              : "";
          return `<div>
            <button type="button" class="cat-tree-item ${active}"
              data-entry="${esc(n.id)}"
              data-type="${esc(n.entry_type)}"
              data-target="${esc(key)}"
              data-parent="${esc(n.parent_entry_id || "")}"
              data-name="${esc(n.name_ar)}"
              data-status="${esc(n.status)}">
              <span class="text-[10px] text-slate-500 shrink-0">${kind}</span>
              <span class="cat-tree-item-label">${esc(n.name_ar)}</span>
              <span class="ms-auto text-[10px] text-slate-500 shrink-0">${esc(
                STATUS_AR[n.status] || n.status || ""
              )}</span>
            </button>
            ${kids}
          </div>`;
        })
        .join("");
    }

    async function loadTree() {
      const data = await json(`${API}/tree`);
      tree = data.tree || [];
      document.getElementById("tree-root").innerHTML =
        renderTree(tree) ||
        '<p class="text-slate-500 p-2">لا توجد أقسام بعد. أضف قسماً جذرياً للبدء.</p>';
      if (selected) showInspector(selected, { reopenSheet: false });
    }

    function showInspector(item, opts = {}) {
      const reopenSheet = opts.reopenSheet !== false;
      selected = item;
      const isNode = item.type === "node";
      const el = document.getElementById("inspector");
      if (!el) return;
      el.innerHTML = `
        <div class="space-y-3">
          <div>
            <div class="text-xs text-slate-500">${isNode ? "قسم" : "خدمة"}</div>
            <div class="text-lg font-semibold text-white break-words">${esc(item.name_ar)}</div>
            <div class="text-xs text-slate-500 mt-1">الحالة: ${esc(STATUS_AR[item.status] || item.status)}</div>
          </div>
          <div class="flex flex-wrap gap-2">
            ${
              isNode
                ? `<button type="button" id="act-rename" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">إعادة تسمية</button>
                   <button type="button" id="act-move" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">نقل</button>
                   <button type="button" id="act-up" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">أعلى</button>
                   <button type="button" id="act-down" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">أسفل</button>
                   <button type="button" id="act-archive" class="rounded-lg border border-red-700/60 text-red-300 px-3 py-1.5 text-xs">أرشفة</button>`
                : `<button type="button" id="act-edit" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">تعديل</button>
                   <button type="button" id="act-move" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">نقل</button>
                   <button type="button" id="act-up" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">أعلى</button>
                   <button type="button" id="act-down" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">أسفل</button>
                   <button type="button" id="act-archive" class="rounded-lg border border-red-700/60 text-red-300 px-3 py-1.5 text-xs">أرشفة</button>`
            }
          </div>
          <p class="text-[11px] text-slate-500">المكان يُدار عبر النقل — هوية العنصر لا تتغيّر عند نقله.</p>
        </div>`;

      const titleEl = document.getElementById("structure-details-title");
      if (titleEl) titleEl.textContent = "التفاصيل";

      const rename = () => {
        openModal({
          title: "إعادة تسمية القسم",
          submitLabel: "حفظ",
          bodyHtml: `
            <div>
              <label class="block text-xs text-slate-500 mb-1">الاسم</label>
              <input name="name_ar" required value="${esc(item.name_ar)}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2"/>
            </div>`,
          onSubmit: async (fd) => {
            await json(`${API}/nodes/${encodeURIComponent(item.id)}`, {
              method: "PATCH",
              body: JSON.stringify({ name_ar: fd.get("name_ar") }),
            });
            alertBox("تم حفظ الاسم");
            await withPreservedScroll(() => loadTree());
          },
        });
      };

      const editService = async () => {
        const data = await json(`${API}/services/${encodeURIComponent(item.id)}`);
        const s = data.service;
        openModal({
          title: "تعديل الخدمة",
          submitLabel: "حفظ",
          bodyHtml: `
            <div>
              <label class="block text-xs text-slate-500 mb-1">الاسم</label>
              <input name="name_ar" required value="${esc(s.name_ar)}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2"/>
            </div>
            <div>
              <label class="block text-xs text-slate-500 mb-1">الملاحظة</label>
              <textarea name="note_ar" rows="3" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${esc(s.note_ar || "")}</textarea>
            </div>
            <div>
              <label class="block text-xs text-slate-500 mb-1">الحالة</label>
              <select name="status" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">
                ${["draft", "active", "archived"]
                  .map(
                    (st) =>
                      `<option value="${st}" ${s.status === st ? "selected" : ""}>${STATUS_AR[st]}</option>`
                  )
                  .join("")}
              </select>
            </div>`,
          onSubmit: async (fd) => {
            await json(`${API}/services/${encodeURIComponent(item.id)}`, {
              method: "PATCH",
              body: JSON.stringify({
                name_ar: fd.get("name_ar"),
                note_ar: fd.get("note_ar"),
                status: fd.get("status"),
              }),
            });
            alertBox("تم حفظ الخدمة");
            await withPreservedScroll(() => loadTree());
          },
        });
      };

      const move = () => {
        openModal({
          title: isNode ? "نقل القسم" : "نقل الخدمة",
          submitLabel: "تأكيد النقل",
          bodyHtml: `
            <p class="text-xs text-slate-400 mb-2">اختر الوجهة الجديدة. الهوية تبقى كما هي.</p>
            ${destinationSelectHtml(tree, item.parent_entry_id || "")}`,
          onSubmit: async (fd) => {
            const parent = String(fd.get("parent_entry_id") || "").trim() || null;
            const path = isNode
              ? `${API}/nodes/${encodeURIComponent(item.id)}/move`
              : `${API}/services/${encodeURIComponent(item.id)}/move`;
            await json(path, {
              method: "POST",
              body: JSON.stringify({ parent_entry_id: parent }),
            });
            alertBox("تم النقل");
            await withPreservedScroll(() => loadTree());
          },
        });
      };

      const archive = () => {
        openModal({
          title: "تأكيد الأرشفة",
          submitLabel: "أرشفة",
          bodyHtml: `<p class="text-sm text-slate-300">هل تريد أرشفة «${esc(item.name_ar)}»؟</p>`,
          onSubmit: async () => {
            const path = isNode
              ? `${API}/nodes/${encodeURIComponent(item.id)}/archive`
              : `${API}/services/${encodeURIComponent(item.id)}/archive`;
            await json(path, { method: "POST", body: "{}" });
            selected = null;
            document.getElementById("inspector").textContent =
              "اختر قسماً أو خدمة من الهيكل…";
            closeStructureSheet();
            alertBox("تمت الأرشفة");
            await withPreservedScroll(() => loadTree());
          },
        });
      };

      document.getElementById("act-rename")?.addEventListener("click", rename);
      document
        .getElementById("act-edit")
        ?.addEventListener("click", () => editService().catch((e) => alertBox(e.message, "err")));
      document.getElementById("act-move")?.addEventListener("click", move);
      document.getElementById("act-archive")?.addEventListener("click", archive);
      document.getElementById("act-up")?.addEventListener("click", async () => {
        try {
          await json(`${API}/entries/${encodeURIComponent(item.entry_id)}/reorder`, {
            method: "POST",
            body: JSON.stringify({ position: "up" }),
          });
          await withPreservedScroll(() => loadTree());
        } catch (e) {
          alertBox(e.message, "err");
        }
      });
      document.getElementById("act-down")?.addEventListener("click", async () => {
        try {
          await json(`${API}/entries/${encodeURIComponent(item.entry_id)}/reorder`, {
            method: "POST",
            body: JSON.stringify({ position: "down" }),
          });
          await withPreservedScroll(() => loadTree());
        } catch (e) {
          alertBox(e.message, "err");
        }
      });

      if (reopenSheet) syncPresentationAfterInspector();
    }

    document.getElementById("tree-root").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-entry]");
      if (!btn) return;
      lastFocusEl = btn;
      showInspector({
        type: btn.getAttribute("data-type"),
        id: btn.getAttribute("data-target"),
        entry_id: btn.getAttribute("data-entry"),
        parent_entry_id: btn.getAttribute("data-parent") || null,
        name_ar: btn.getAttribute("data-name"),
        status: btn.getAttribute("data-status"),
      });
      withPreservedScroll(() => loadTree()).catch((err) => alertBox(err.message, "err"));
    });

    document.querySelectorAll("[data-structure-sheet-close]").forEach((el) => {
      el.addEventListener("click", () => closeStructureSheet());
    });

    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const modal = document.getElementById("modal-root");
      if (modal && !modal.classList.contains("hidden")) return;
      if (sheetOpen && !isDesktopLayout()) {
        e.preventDefault();
        closeStructureSheet();
      }
    });

    window.matchMedia(DESKTOP_MQ).addEventListener("change", () => {
      if (isDesktopLayout()) {
        document.body.classList.remove("cat-structure-sheet-open");
        const pane = detailsPane();
        const backdrop = sheetBackdrop();
        if (pane) {
          pane.classList.remove("is-open");
          pane.setAttribute("aria-hidden", "false");
          pane.setAttribute("role", "region");
          pane.removeAttribute("aria-modal");
        }
        if (backdrop) {
          backdrop.classList.remove("is-open");
          backdrop.setAttribute("aria-hidden", "true");
        }
        sheetOpen = false;
      } else if (selected) {
        openStructureSheet();
      } else {
        closeStructureSheet();
      }
    });

    document.getElementById("btn-add-root").onclick = () => {
      openModal({
        title: "إضافة قسم جذري",
        submitLabel: "إضافة",
        bodyHtml: `
          <div>
            <label class="block text-xs text-slate-500 mb-1">الاسم</label>
            <input name="name_ar" required class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2" placeholder="مثال: إنستغرام"/>
          </div>`,
        onSubmit: async (fd) => {
          await json(`${API}/nodes`, {
            method: "POST",
            body: JSON.stringify({ name_ar: fd.get("name_ar"), parent_entry_id: null }),
          });
          alertBox("تمت إضافة القسم");
          await withPreservedScroll(() => loadTree());
        },
      });
    };

    document.getElementById("btn-add-child").onclick = () => {
      if (!selected || selected.type !== "node") {
        alertBox("اختر قسماً أولاً لإضافة قسم فرعي", "err");
        return;
      }
      openModal({
        title: "إضافة قسم فرعي",
        submitLabel: "إضافة",
        bodyHtml: `
          <p class="text-xs text-slate-400">تحت: <b class="text-slate-200">${esc(selected.name_ar)}</b></p>
          <div>
            <label class="block text-xs text-slate-500 mb-1">الاسم</label>
            <input name="name_ar" required class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2"/>
          </div>`,
        onSubmit: async (fd) => {
          await json(`${API}/nodes`, {
            method: "POST",
            body: JSON.stringify({
              name_ar: fd.get("name_ar"),
              parent_entry_id: selected.entry_id,
            }),
          });
          alertBox("تمت الإضافة");
          await withPreservedScroll(() => loadTree());
        },
      });
    };

    document.getElementById("btn-add-service").onclick = () => {
      const parent =
        selected && selected.type === "node"
          ? selected.entry_id
          : selected?.parent_entry_id || null;
      openModal({
        title: "إضافة خدمة",
        submitLabel: "إضافة",
        bodyHtml: `
          <div>
            <label class="block text-xs text-slate-500 mb-1">الاسم</label>
            <input name="name_ar" required class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2"/>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">الملاحظة</label>
            <textarea name="note_ar" rows="2" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2"></textarea>
          </div>
          ${destinationSelectHtml(tree, parent || "")}`,
        onSubmit: async (fd) => {
          const p = String(fd.get("parent_entry_id") || "").trim() || null;
          await json(`${API}/services`, {
            method: "POST",
            body: JSON.stringify({
              name_ar: fd.get("name_ar"),
              note_ar: fd.get("note_ar"),
              status: "draft",
              parent_entry_id: p,
            }),
          });
          alertBox("تمت إضافة الخدمة");
          await withPreservedScroll(() => loadTree());
        },
      });
    };

    loadTree().catch((e) => alertBox(e.message, "err"));
  }

  // ── Services page ──

  function mountServices() {
    let page = 1;
    let tree = [];
    let commercial = {
      service_types: [],
      ordering_modes: [],
      pricing_modes: [],
      fulfillment_modes: [],
      target_platforms: [],
      target_link_types: [],
      target_link_prompt_keys: [],
    };

    function typeOptionsHtml(selected) {
      return (commercial.service_types || [])
        .map(
          (t) =>
            `<option value="${esc(t.code)}" ${selected === t.code ? "selected" : ""}>${esc(
              t.label_ar
            )}</option>`
        )
        .join("");
    }

    function modeOptionsHtml(selected) {
      return (commercial.ordering_modes || [])
        .map(
          (m) =>
            `<option value="${esc(m.code)}" ${selected === m.code ? "selected" : ""}>${esc(
              m.label_ar
            )}</option>`
        )
        .join("");
    }

    function pricingModeOptionsHtml(selected) {
      return (commercial.pricing_modes || [])
        .map(
          (m) =>
            `<option value="${esc(m.code)}" ${selected === m.code ? "selected" : ""}>${esc(
              m.label_ar
            )}</option>`
        )
        .join("");
    }

    function fulfillmentOptionsHtml(selected) {
      const cur = selected || "auto";
      return (commercial.fulfillment_modes || [])
        .map(
          (m) =>
            `<option value="${esc(m.code)}" ${cur === m.code ? "selected" : ""}>${esc(
              m.label_ar
            )}</option>`
        )
        .join("");
    }

    function platformOptionsHtml(selected) {
      const cur = selected || "";
      return (
        `<option value="">— اختر المنصة —</option>` +
        (commercial.target_platforms || [])
          .map(
            (p) =>
              `<option value="${esc(p.code)}" ${cur === p.code ? "selected" : ""}>${esc(
                p.label_ar
              )}</option>`
          )
          .join("")
      );
    }

    function linkTypeOptionsHtml(selected) {
      const cur = selected || "";
      return (
        `<option value="">— بدون —</option>` +
        (commercial.target_link_types || [])
          .map(
            (t) =>
              `<option value="${esc(t.code)}" ${cur === t.code ? "selected" : ""}>${esc(
                t.label_ar
              )}</option>`
          )
          .join("")
      );
    }

    function linkPromptOptionsHtml(selected) {
      const cur = selected || "";
      return (
        `<option value="">— افتراضي حسب المنصة/القسم —</option>` +
        (commercial.target_link_prompt_keys || [])
          .map(
            (t) =>
              `<option value="${esc(t.code)}" ${cur === t.code ? "selected" : ""}>${esc(
                t.label_ar
              )}</option>`
          )
          .join("")
      );
    }

    function priceLabel(price) {
      return price && price.display_ar ? price.display_ar : "بدون سعر";
    }

    function commercialFormFields(s) {
      const st = (s && s.service_type) || "other";
      const om = (s && s.ordering_mode) || "quantity_based";
      const minQ = s && s.min_quantity != null ? s.min_quantity : 1;
      const maxQ = s && s.max_quantity != null ? s.max_quantity : 1000000;
      const fm = (s && s.fulfillment_mode) || "auto";
      return `
        <div class="cat-section space-y-3" id="section-basic">
          <h4>المعلومات الأساسية</h4>
          <div>
            <label class="block text-xs text-slate-500 mb-1">الاسم</label>
            <input name="name_ar" required value="${esc((s && s.name_ar) || "")}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2"/>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">الوصف / الملاحظة</label>
            <textarea name="note_ar" rows="3" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${esc(
              (s && s.note_ar) || ""
            )}</textarea>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">نوع الخدمة</label>
            <select name="service_type" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${typeOptionsHtml(
              st
            )}</select>
          </div>
        </div>
        <div class="cat-section space-y-3" id="section-commercial">
          <h4>الإعدادات التجارية</h4>
          <div>
            <label class="block text-xs text-slate-500 mb-1">طريقة الطلب</label>
            <select name="ordering_mode" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${modeOptionsHtml(
              om
            )}</select>
          </div>
          <div class="grid grid-cols-2 gap-2">
            <div>
              <label class="block text-xs text-slate-500 mb-1">الحد الأدنى</label>
              <input name="min_quantity" type="number" min="0" step="1" required value="${esc(
                minQ
              )}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2" dir="ltr"/>
            </div>
            <div>
              <label class="block text-xs text-slate-500 mb-1">الحد الأقصى</label>
              <input name="max_quantity" type="number" min="0" step="1" required value="${esc(
                maxQ
              )}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2" dir="ltr"/>
            </div>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">الحالة</label>
            <select name="status" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">
              ${["draft", "active", "archived"]
                .map(
                  (stCode) =>
                    `<option value="${stCode}" ${
                      (s && s.status) === stCode || (!s && stCode === "draft") ? "selected" : ""
                    }>${STATUS_AR[stCode]}</option>`
                )
                .join("")}
            </select>
          </div>
        </div>
        <div class="cat-section space-y-3" id="section-target">
          <h4>متطلبات الرابط / الهدف</h4>
          <div>
            <label class="block text-xs text-slate-500 mb-1">المنصة <span class="text-rose-300">*</span></label>
            <select name="target_platform_key" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${platformOptionsHtml(
              (s && s.target_platform_key) || ""
            )}</select>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">القسم <span class="text-rose-300">*</span></label>
            <input name="target_section_key" value="${esc(
              (s && s.target_section_key) || ""
            )}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2" dir="ltr" placeholder="مثال: followers أو likes"/>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">القسم الفرعي</label>
            <input name="target_subsection_key" value="${esc(
              (s && s.target_subsection_key) || ""
            )}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2" dir="ltr" placeholder="اختياري"/>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">نوع الرابط</label>
            <select name="target_link_type" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${linkTypeOptionsHtml(
              (s && s.target_link_type) || ""
            )}</select>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">موجّه إدخال الرابط</label>
            <select name="target_link_prompt_key" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${linkPromptOptionsHtml(
              (s && s.target_link_prompt_key) || ""
            )}</select>
          </div>
        </div>
        <div class="cat-section space-y-3" id="section-fulfillment">
          <h4>طريقة التنفيذ</h4>
          <div>
            <label class="block text-xs text-slate-500 mb-1">طريقة التنفيذ</label>
            <select name="fulfillment_mode" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${fulfillmentOptionsHtml(
              fm
            )}</select>
            <p class="text-[11px] text-slate-500 mt-1">تلقائي عبر المزود، أو يدوي/أدمن.</p>
          </div>
        </div>`;
    }

    function payloadFromCommercialForm(fd) {
      const emptyToNull = (v) => {
        const t = String(v ?? "").trim();
        return t ? t : null;
      };
      return {
        name_ar: fd.get("name_ar"),
        note_ar: fd.get("note_ar"),
        status: fd.get("status"),
        service_type: fd.get("service_type"),
        ordering_mode: fd.get("ordering_mode"),
        min_quantity: Number(fd.get("min_quantity")),
        max_quantity: Number(fd.get("max_quantity")),
        fulfillment_mode: fd.get("fulfillment_mode") || "auto",
        target_platform_key: emptyToNull(fd.get("target_platform_key")),
        target_section_key: emptyToNull(fd.get("target_section_key")),
        target_subsection_key: emptyToNull(fd.get("target_subsection_key")),
        target_link_type: emptyToNull(fd.get("target_link_type")),
        target_link_prompt_key: emptyToNull(fd.get("target_link_prompt_key")),
      };
    }

    async function loadTree() {
      const data = await json(`${API}/tree`);
      tree = data.tree || [];
      refreshPlacementFilters();
    }

    function nodeChildren(parentNodes) {
      return (parentNodes || []).filter((n) => n.entry_type === "node");
    }

    function findNodeByEntryId(nodes, entryId) {
      for (const n of nodes || []) {
        if (n.entry_type === "node" && n.id === entryId) return n;
        const found = findNodeByEntryId(n.children || [], entryId);
        if (found) return found;
      }
      return null;
    }

    function fillSelect(el, items, allLabel, selected) {
      if (!el) return;
      const opts =
        `<option value="">${esc(allLabel)}</option>` +
        (items || [])
          .map(
            (n) =>
              `<option value="${esc(n.id)}" ${selected === n.id ? "selected" : ""}>${esc(
                n.name_ar || n.id
              )}</option>`
          )
          .join("");
      el.innerHTML = opts;
    }

    function refreshPlacementFilters() {
      const platEl = document.getElementById("svc-platform");
      const secEl = document.getElementById("svc-section");
      const subEl = document.getElementById("svc-subsection");
      if (!platEl || !secEl || !subEl) return;

      const platforms = nodeChildren(tree);
      const selectedPlat = platEl.value;
      const selectedSec = secEl.value;
      const selectedSub = subEl.value;

      fillSelect(platEl, platforms, "كل المنصات", selectedPlat);

      const platNode =
        selectedPlat && platforms.some((p) => p.id === selectedPlat)
          ? findNodeByEntryId(platforms, selectedPlat)
          : null;
      if (!platNode) {
        platEl.value = "";
        fillSelect(secEl, [], "كل الأقسام", "");
        secEl.disabled = true;
        fillSelect(subEl, [], "كل الأقسام الفرعية", "");
        subEl.disabled = true;
        return;
      }

      const sections = nodeChildren(platNode.children);
      const secStillValid = selectedSec && sections.some((s) => s.id === selectedSec);
      fillSelect(secEl, sections, "كل الأقسام", secStillValid ? selectedSec : "");
      secEl.disabled = false;

      const secNode = secStillValid ? findNodeByEntryId(sections, selectedSec) : null;
      if (!secNode) {
        fillSelect(subEl, [], "كل الأقسام الفرعية", "");
        subEl.disabled = true;
        return;
      }

      const subsections = nodeChildren(secNode.children);
      const subStillValid =
        selectedSub && subsections.some((s) => s.id === selectedSub);
      fillSelect(
        subEl,
        subsections,
        "كل الأقسام الفرعية",
        subStillValid ? selectedSub : ""
      );
      subEl.disabled = subsections.length === 0;
    }

    function selectedUnderEntryId() {
      const sub = document.getElementById("svc-subsection")?.value || "";
      const sec = document.getElementById("svc-section")?.value || "";
      const plat = document.getElementById("svc-platform")?.value || "";
      return sub || sec || plat || "";
    }

    function anyFilterActive() {
      if (document.getElementById("svc-search")?.value.trim()) return true;
      if (selectedUnderEntryId()) return true;
      return [
        "svc-status",
        "svc-type",
        "svc-ordering",
        "svc-source",
        "svc-price",
        "svc-pricing-mode",
        "svc-readiness",
      ].some((id) => {
        const el = document.getElementById(id);
        return el && el.value;
      });
    }

    async function loadCommercialOptions() {
      const data = await json(`${API}/commercial-options`);
      commercial = {
        service_types: data.service_types || [],
        ordering_modes: data.ordering_modes || [],
        pricing_modes: data.pricing_modes || [],
        fulfillment_modes: data.fulfillment_modes || [],
        target_platforms: data.target_platforms || [],
        target_link_types: data.target_link_types || [],
        target_link_prompt_keys: data.target_link_prompt_keys || [],
      };
      const typeSel = document.getElementById("svc-type");
      const modeSel = document.getElementById("svc-ordering");
      const pricingModeSel = document.getElementById("svc-pricing-mode");
      if (typeSel) {
        typeSel.innerHTML =
          `<option value="">كل الأنواع</option>` +
          (commercial.service_types || [])
            .map((t) => `<option value="${esc(t.code)}">${esc(t.label_ar)}</option>`)
            .join("");
      }
      if (modeSel) {
        modeSel.innerHTML =
          `<option value="">كل طرق الطلب</option>` +
          (commercial.ordering_modes || [])
            .map((m) => `<option value="${esc(m.code)}">${esc(m.label_ar)}</option>`)
            .join("");
      }
      if (pricingModeSel) {
        pricingModeSel.innerHTML =
          `<option value="">كل طرق التسعير</option>` +
          (commercial.pricing_modes || [])
            .map((m) => `<option value="${esc(m.code)}">${esc(m.label_ar)}</option>`)
            .join("");
      }
    }

    async function load() {
      const q = new URLSearchParams({ page, limit: "50" });
      const search = document.getElementById("svc-search").value.trim();
      const status = document.getElementById("svc-status").value;
      const source = document.getElementById("svc-source")?.value || "";
      const serviceType = document.getElementById("svc-type")?.value || "";
      const ordering = document.getElementById("svc-ordering")?.value || "";
      const priceFilter = document.getElementById("svc-price")?.value || "";
      const pricingMode = document.getElementById("svc-pricing-mode")?.value || "";
      const readinessFilter = document.getElementById("svc-readiness")?.value || "";
      const underEntryId = selectedUnderEntryId();
      if (search) q.set("search", search);
      if (status) q.set("status", status);
      if (source) q.set("source", source);
      if (serviceType) q.set("service_type", serviceType);
      if (ordering) q.set("ordering_mode", ordering);
      if (priceFilter) q.set("price", priceFilter);
      if (pricingMode) q.set("pricing_mode", pricingMode);
      if (readinessFilter) q.set("readiness", readinessFilter);
      if (underEntryId) q.set("under_entry_id", underEntryId);
      const data = await json(`${API}/services?` + q);
      const tb = document.getElementById("svc-tbody");
      const emptyMsg = anyFilterActive()
        ? "لا توجد خدمات تطابق الفلاتر المحددة."
        : "لا توجد خدمات";
      tb.innerHTML =
        (data.services || [])
          .map((s) => {
            const path = (s.location_path || []).join(" › ") || "الجذر";
            const typeLabel = s.service_type_label_ar || s.service_type || "—";
            const priceTxt = priceLabel(s.current_price);
            const matchHint =
              opaqueProviderId(s.current_source) &&
              document.getElementById("svc-search").value.trim() ===
                opaqueProviderId(s.current_source)
                ? `<span class="text-[10px] text-emerald-400">مطابقة نشطة</span>`
                : "";
            return `<tr class="border-t border-slate-800">
              <td class="p-2 text-start">
                ${providerIdPrimaryHtml(s.current_source, { compact: true })}
                ${matchHint}
              </td>
              <td class="p-2 text-start">
                <div class="font-medium text-slate-100">${esc(s.name_ar)}</div>
                <div class="text-[10px] text-slate-500">${esc(s.note_ar || "")}</div>
              </td>
              <td class="p-2 text-start text-slate-300">${esc(typeLabel)}</td>
              <td class="p-2 text-start text-slate-300">${esc(path)}</td>
              <td class="p-2 text-start text-slate-300">${esc(priceTxt)}</td>
              <td class="p-2 text-start text-slate-300 text-xs">${esc(
                sourceLabel(s.current_source)
              )}</td>
              <td class="p-2 text-center">${esc(STATUS_AR[s.status] || s.status)}</td>
              <td class="p-2 text-center">${readinessBadge(s.readiness)}</td>
              <td class="p-2 text-center">
                <button type="button" class="text-sky-300 text-xs underline" data-edit="${esc(s.id)}">تفاصيل</button>
                <button type="button" class="text-sky-300 text-xs underline ms-2" data-move="${esc(s.id)}">نقل</button>
                ${
                  s.status !== "archived"
                    ? `<button type="button" class="text-rose-300 text-xs underline ms-2" data-delete="${esc(s.id)}" data-delete-name="${esc(s.name_ar)}">حذف</button>`
                    : `<button type="button" class="text-emerald-300 text-xs underline ms-2" data-restore="${esc(s.id)}">استعادة</button>`
                }
              </td>
            </tr>`;
          })
          .join("") ||
        `<tr><td colspan="9" class="p-4 text-center text-slate-500">${esc(emptyMsg)}</td></tr>`;
      wireCopyProviderIds(tb);
      document.getElementById("svc-page-info").textContent =
        `صفحة ${data.current_page}/${data.total_pages} · ${data.total_items}`;
    }

    function historyHtml(history) {
      const current = (history || []).filter((h) => h.status === "active");
      const past = (history || []).filter((h) => h.status !== "active");
      const block = (items, title) => {
        if (!items.length) {
          return `<div class="text-xs text-slate-500">${esc(title)}: لا يوجد</div>`;
        }
        return `<div class="space-y-2">
          <div class="text-xs text-slate-400">${esc(title)}</div>
          ${items
            .map(
              (h) => `<div class="rounded-lg border border-slate-700 px-3 py-2 text-xs space-y-0.5">
                <div>${providerIdPrimaryHtml(h, { compact: true })}</div>
                <div class="text-slate-200">${esc(sourceLabel(h))}</div>
                <div class="text-slate-500">
                  ${h.status === "active" ? "منذ" : ""} ${esc(h.assigned_at || "—")}
                  ${h.ended_at ? ` → ${esc(h.ended_at)}` : ""}
                  · ${esc(SOURCE_STATUS_AR[h.status] || h.status)}
                </div>
              </div>`
            )
            .join("")}
        </div>`;
      };
      return `${block(current, "المصدر الحالي")}<div class="mt-3">${block(past, "المصادر السابقة")}</div>`;
    }

    function priceHistoryHtml(history) {
      const items = history || [];
      if (!items.length) {
        return `<div class="text-xs text-slate-500">لا يوجد سجل أسعار بعد.</div>`;
      }
      return `<div class="space-y-2">${items
        .map((h) => {
          const range =
            h.status === "active"
              ? `من ${esc(h.effective_from || "—")} · الحالي`
              : `من ${esc(h.effective_from || "—")} إلى ${esc(h.effective_to || "—")}`;
          return `<div class="rounded-lg border border-slate-700 px-3 py-2 text-xs space-y-0.5">
            <div class="text-slate-200">${esc(h.display_ar || priceLabel(h))}</div>
            <div class="text-slate-500">${range}</div>
          </div>`;
        })
        .join("")}</div>`;
    }

    function openDeleteConfirm(serviceId, nameHint) {
      return (async () => {
        const data = await json(
          `${API}/services/${encodeURIComponent(serviceId)}/delete-preview`
        );
        const preview = data.preview || {};
        const warnings = (preview.warnings || [])
          .map((w) => `<li>${esc(w)}</li>`)
          .join("");
        const modal = openModal({
          title: "حذف الخدمة",
          hideFormActions: true,
          bodyHtml: `
            <div class="space-y-3 text-sm">
              <p class="text-slate-200">هل أنت متأكد من حذف هذه الخدمة؟</p>
              <p class="text-white font-semibold">«${esc(
                preview.name_ar || nameHint || "—"
              )}»</p>
              <p class="text-xs text-slate-400">${esc(
                preview.message_ar ||
                  "سيتم أرشفة الخدمة بأمان. لن تُحذف الطلبات ولا السجلات التاريخية."
              )}</p>
              ${
                warnings
                  ? `<ul class="list-disc ps-5 text-xs text-amber-200 space-y-1">${warnings}</ul>`
                  : ""
              }
              <div class="flex justify-end gap-2 pt-2">
                <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إلغاء</button>
                <button type="button" id="btn-confirm-delete" class="rounded-lg bg-rose-700 hover:bg-rose-600 px-3 py-2 text-white">حذف الخدمة</button>
              </div>
            </div>`,
        });
        modal.querySelector("#btn-confirm-delete")?.addEventListener("click", async () => {
          try {
            const res = await json(
              `${API}/services/${encodeURIComponent(serviceId)}/delete`,
              { method: "POST", body: "{}" }
            );
            closeModal();
            alertBox(res.message_ar || "تم حذف الخدمة (أرشفة)");
            await load();
          } catch (err) {
            alertBox(err.message, "err");
          }
        });
      })();
    }

    async function openDetails(serviceId) {
      const [svcData, srcData, priceData, readyData, pubData] = await Promise.all([
        json(`${API}/services/${encodeURIComponent(serviceId)}`),
        json(`${API}/services/${encodeURIComponent(serviceId)}/execution-source`),
        json(`${API}/services/${encodeURIComponent(serviceId)}/price`),
        json(`${API}/services/${encodeURIComponent(serviceId)}/readiness`),
        json(`${API}/services/${encodeURIComponent(serviceId)}/publication`),
      ]);
      const s = svcData.service;
      const current = srcData.current || s.current_source;
      const history = srcData.history || [];
      const currentPrice = priceData.current || s.current_price;
      const priceHistory = priceData.history || [];
      const readiness = readyData.readiness || s.readiness;
      const pub = pubData || {};
      const path = (s.location_path || []).join(" › ") || "الجذر";

      const root = openModal({
        title: "تفاصيل الخدمة",
        hideFormActions: true,
        bodyHtml: `
          ${readinessPanelHtml(readiness)}
          <div class="cat-section">
            <h4>النشر</h4>
            <div class="space-y-1 text-sm">
              <div><span class="text-slate-500">حالة النشر:</span>
                <span class="${pub.publication_status === "published" ? "text-emerald-300" : "text-slate-300"}">${esc(
                  pub.publication_status_label_ar || "غير منشورة"
                )}</span>
              </div>
              <div><span class="text-slate-500">حالة الجاهزية:</span> ${readinessBadge(readiness)}</div>
              ${
                pub.has_unpublished_changes
                  ? `<div class="text-amber-200 text-xs mt-1">تغييرات غير منشورة</div>`
                  : ""
              }
            </div>
            <div class="flex flex-wrap gap-2 mt-3">
              <button type="button" id="btn-pub-preview" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">معاينة النشر</button>
              <button type="button" id="btn-pub-publish" class="rounded-lg bg-accent-600 hover:bg-accent-500 px-3 py-1.5 text-xs">نشر الخدمة</button>
              ${
                pub.publication_status === "published"
                  ? `<button type="button" id="btn-pub-unpublish" class="rounded-lg border border-amber-700/60 text-amber-200 px-3 py-1.5 text-xs">إلغاء النشر</button>`
                  : ""
              }
              <button type="button" id="btn-pub-history" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">سجل النشر</button>
            </div>
            <p class="text-[11px] text-slate-500 mt-2">النشر إداري صريح ولا يغيّر تليجرام في هذه المرحلة. تعديلات الحفظ/السعر/المصدر للخدمات المربوطة تُزامن مباشرة إلى smm_services الذي يقرأه تليجرام.</p>
          </div>
          <div class="cat-section">
            <h4>معلومات الخدمة</h4>
            <div class="text-base font-semibold text-white">${esc(s.name_ar)}</div>
            <div class="text-xs text-slate-400 mt-1">${esc(s.note_ar || "بدون ملاحظة")}</div>
            <div class="text-xs text-slate-500 mt-2">نوع الخدمة: ${esc(
              s.service_type_label_ar || s.service_type || "—"
            )}</div>
            <div class="flex flex-wrap gap-2 mt-3">
              <button type="button" id="btn-edit-identity" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs">تعديل بيانات الخدمة</button>
              ${
                s.status !== "archived"
                  ? `<button type="button" id="btn-delete-service" class="rounded-lg border border-rose-700/70 text-rose-200 px-3 py-1.5 text-xs">حذف الخدمة</button>`
                  : `<button type="button" id="btn-restore-service" class="rounded-lg border border-emerald-700/60 text-emerald-200 px-3 py-1.5 text-xs">استعادة الخدمة</button>`
              }
            </div>
          </div>
          <div class="cat-section">
            <h4>طريقة الطلب والحدود</h4>
            <div class="text-sm text-slate-300">طريقة الطلب: ${esc(
              s.ordering_mode_label_ar || s.ordering_mode || "—"
            )}</div>
            <div class="text-xs text-slate-500 mt-1">الحد الأدنى: ${esc(s.min_quantity)}</div>
            <div class="text-xs text-slate-500">الحد الأقصى: ${esc(s.max_quantity)}</div>
            <div class="text-xs text-slate-500 mt-2">الحالة: ${esc(STATUS_AR[s.status] || s.status)}</div>
          </div>
          <div class="cat-section">
            <h4>متطلبات الرابط / الهدف</h4>
            <div class="text-sm text-slate-300">المنصة: ${esc(
              (commercial.target_platforms || []).find((p) => p.code === s.target_platform_key)
                ?.label_ar || s.target_platform_key || "—"
            )}</div>
            <div class="text-xs text-slate-500 mt-1">القسم: ${esc(s.target_section_key || "—")}</div>
            <div class="text-xs text-slate-500">القسم الفرعي: ${esc(s.target_subsection_key || "—")}</div>
            <div class="text-xs text-slate-500">نوع الرابط: ${esc(
              (commercial.target_link_types || []).find((t) => t.code === s.target_link_type)
                ?.label_ar || s.target_link_type || "—"
            )}</div>
          </div>
          <div class="cat-section">
            <h4>طريقة التنفيذ</h4>
            <div class="text-sm text-slate-300">${esc(
              s.fulfillment_mode_label_ar || s.fulfillment_mode || "—"
            )}</div>
          </div>
          <div class="cat-section">
            <h4>التسعير</h4>
            <div class="text-sm text-slate-200">${esc(priceLabel(currentPrice))}</div>
            <button type="button" id="btn-change-price" class="mt-3 rounded-lg bg-accent-600 hover:bg-accent-500 px-3 py-1.5 text-xs">تعديل السعر</button>
          </div>
          <div class="cat-section">
            <h4>سجل الأسعار</h4>
            ${priceHistoryHtml(priceHistory)}
          </div>
          <div class="cat-section">
            <h4>مكان الخدمة</h4>
            <div class="text-sm text-slate-300">${esc(path)}</div>
            <p class="text-[11px] text-slate-500 mt-1">المكان مستقل عن السعر ومصدر التنفيذ.</p>
          </div>
          <div class="cat-section">
            <h4>مصدر التنفيذ</h4>
            ${
              current
                ? `<div class="space-y-2 text-sm">
                    <div class="rounded-lg border border-sky-800/50 bg-sky-950/20 px-3 py-2">
                      ${providerIdPrimaryHtml(current)}
                      <div class="text-[10px] text-slate-500 mt-1">Provider Service ID</div>
                    </div>
                    <div><span class="text-slate-500">المزود:</span> ${esc(
                      current.provider_name || current.provider_slug
                    )}</div>
                    <div><span class="text-slate-500">الحساب:</span> ${esc(
                      current.account_display_name || current.provider_account_key
                    )}</div>
                    <div class="text-emerald-300 text-xs mt-1">● نشط</div>
                  </div>`
                : `<p class="text-sm text-slate-400">بدون مصدر</p>`
            }
            <button type="button" id="btn-change-source" class="mt-3 rounded-lg bg-accent-600 hover:bg-accent-500 px-3 py-1.5 text-xs">استبدال مصدر التنفيذ</button>
          </div>
          <div class="cat-section">
            <h4>سجل مصادر التنفيذ</h4>
            ${historyHtml(history)}
          </div>
          <div class="flex justify-end pt-1">
            <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إغلاق</button>
          </div>`,
      });

      wireCopyProviderIds(root);

      const refreshAfter = async () => {
        // List refresh is best-effort (active search/filters may hide the row).
        // Details must always reopen for the known service id.
        try {
          await load();
        } catch (_) {
          /* ignore list refresh errors */
        }
        await openDetails(serviceId);
      };

      const openIdentityEditor = (focusSection) => {
        const modal = openModal({
          title: "تعديل بيانات الخدمة",
          submitLabel: "حفظ",
          bodyHtml: `
            ${commercialFormFields(s)}
            <p class="text-[11px] text-slate-500">تعديل هذه البيانات لا يغيّر مكان الخدمة ولا مصدر التنفيذ ولا السعر.</p>`,
          onSubmit: async (fd) => {
            await json(`${API}/services/${encodeURIComponent(serviceId)}`, {
              method: "PATCH",
              body: JSON.stringify(payloadFromCommercialForm(fd)),
            });
            alertBox("تم حفظ بيانات الخدمة");
            await refreshAfter();
          },
        });
        if (focusSection) {
          const el = modal.querySelector(`#${focusSection}`);
          if (el) {
            el.scrollIntoView({ block: "nearest" });
            el.classList.add("ring-1", "ring-sky-500/50");
          }
        }
      };

      const confirmDeleteService = () =>
        openDeleteConfirm(serviceId, s.name_ar);

      root.querySelector("#btn-edit-identity").addEventListener("click", () => {
        openIdentityEditor(null);
      });

      root.querySelector("#btn-delete-service")?.addEventListener("click", () => {
        confirmDeleteService().catch((err) => alertBox(err.message, "err"));
      });
      root.querySelector("#btn-restore-service")?.addEventListener("click", async () => {
        try {
          await json(`${API}/services/${encodeURIComponent(serviceId)}/restore`, {
            method: "POST",
            body: JSON.stringify({ status: "active" }),
          });
          alertBox("تمت استعادة الخدمة");
          await refreshAfter();
        } catch (err) {
          alertBox(err.message, "err");
        }
      });

      root.querySelector("#btn-change-price").addEventListener("click", () => {
        openChangePrice(serviceId, s, currentPrice, refreshAfter).catch((err) =>
          alertBox(err.message, "err")
        );
      });

      root.querySelector("#btn-change-source").addEventListener("click", () => {
        openChangeSource(serviceId, s, current, refreshAfter).catch((err) =>
          alertBox(err.message, "err")
        );
      });

      const fixSource = root.querySelector("#btn-fix-source");
      if (fixSource) {
        fixSource.addEventListener("click", () => {
          openChangeSource(serviceId, s, current, refreshAfter).catch((err) =>
            alertBox(err.message, "err")
          );
        });
      }
      const fixPrice = root.querySelector("#btn-fix-price");
      if (fixPrice) {
        fixPrice.addEventListener("click", () => {
          openChangePrice(serviceId, s, currentPrice, refreshAfter).catch((err) =>
            alertBox(err.message, "err")
          );
        });
      }
      const fixCommercial = root.querySelector("#btn-fix-commercial");
      if (fixCommercial) {
        fixCommercial.addEventListener("click", () => {
          openIdentityEditor("section-commercial");
        });
      }
      const fixTarget = root.querySelector("#btn-fix-target");
      if (fixTarget) {
        fixTarget.addEventListener("click", () => {
          openIdentityEditor("section-target");
        });
      }
      const fixFulfillment = root.querySelector("#btn-fix-fulfillment");
      if (fixFulfillment) {
        fixFulfillment.addEventListener("click", () => {
          openIdentityEditor("section-fulfillment");
        });
      }

      const openPubPreview = async (confirmPublish) => {
        const data = await json(
          `${API}/services/${encodeURIComponent(serviceId)}/publication-preview`
        );
        const p = data.preview || {};
        const snap = p.snapshot || {};
        const reasons = (p.blocking_reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");
        const bodyHtml = `
          <div class="space-y-3 text-sm">
            <div class="cat-section space-y-1">
              <h4>معاينة النشر</h4>
              <div><span class="text-slate-500">الاسم:</span> ${esc(snap.name_ar || "—")}</div>
              <div><span class="text-slate-500">الوصف:</span> ${esc(snap.note_ar || "—")}</div>
              <div><span class="text-slate-500">المسار:</span> ${esc(
                snap.location_path_label_ar || "الجذر"
              )}</div>
              <div><span class="text-slate-500">النوع:</span> ${esc(
                snap.service_type_label_ar || snap.service_type || "—"
              )}</div>
              <div><span class="text-slate-500">طريقة الطلب:</span> ${esc(
                snap.ordering_mode_label_ar || snap.ordering_mode || "—"
              )}</div>
              <div><span class="text-slate-500">الحدود:</span> ${esc(snap.min_quantity)} – ${esc(
                snap.max_quantity
              )}</div>
              <div><span class="text-slate-500">السعر:</span> ${esc(snap.amount_dh || "—")} ${esc(
                snap.currency || ""
              )} · ${esc(snap.pricing_mode_label_ar || snap.pricing_mode || "")}</div>
              <div><span class="text-slate-500">مصدر التنفيذ:</span> ${esc(
                snap.source_label_ar || "—"
              )}</div>
              <div><span class="text-slate-500">الجاهزية:</span> ${readinessBadge(p.readiness)}</div>
            </div>
            ${
              p.can_publish
                ? `<p class="text-sm text-slate-300">${esc(p.message_ar || "")}</p>`
                : `<p class="text-sm text-rose-200">${esc(p.message_ar || "النشر غير متاح")}</p>
                   ${reasons ? `<ul class="list-disc ps-5 text-xs text-rose-200">${reasons}</ul>` : ""}`
            }
            <div class="flex justify-end gap-2 pt-1">
              <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إلغاء</button>
              ${
                p.can_publish && (confirmPublish || p.would_change)
                  ? `<button type="button" id="btn-do-publish" class="rounded-lg bg-accent-600 hover:bg-accent-500 px-3 py-2">نشر الخدمة</button>`
                  : ""
              }
            </div>
          </div>`;
        const modal = openModal({
          title: "معاينة النشر",
          hideFormActions: true,
          bodyHtml,
        });
        const doBtn = modal.querySelector("#btn-do-publish");
        if (doBtn) {
          doBtn.addEventListener("click", async () => {
            try {
              const res = await json(
                `${API}/services/${encodeURIComponent(serviceId)}/publish`,
                {
                  method: "POST",
                  body: JSON.stringify({
                    expected_content_fingerprint: p.content_fingerprint || null,
                  }),
                }
              );
              alertBox(res.message_ar || "تم النشر");
              closeModal();
              await refreshAfter();
            } catch (err) {
              alertBox(err.message, "err");
            }
          });
        }
      };

      root.querySelector("#btn-pub-preview").addEventListener("click", () => {
        openPubPreview(false).catch((e) => alertBox(e.message, "err"));
      });
      root.querySelector("#btn-pub-publish").addEventListener("click", () => {
        openPubPreview(true).catch((e) => alertBox(e.message, "err"));
      });
      const unpubBtn = root.querySelector("#btn-pub-unpublish");
      if (unpubBtn) {
        unpubBtn.addEventListener("click", () => {
          openModal({
            title: "إلغاء النشر",
            submitLabel: "إلغاء النشر",
            bodyHtml: `
              <p class="text-sm text-slate-300">سيتم إخراج الخدمة من الكتالوج المنشور لاحقًا للعملاء.</p>
              <p class="text-xs text-slate-500">لن تُحذف الخدمة ولا الأسعار ولا مصادر التنفيذ ولا سجل النشر.</p>`,
            onSubmit: async () => {
              const res = await json(
                `${API}/services/${encodeURIComponent(serviceId)}/unpublish`,
                { method: "POST", body: "{}" }
              );
              alertBox(res.message_ar || "تم إلغاء النشر");
              await refreshAfter();
            },
          });
        });
      }
      root.querySelector("#btn-pub-history").addEventListener("click", async () => {
        try {
          const data = await json(
            `${API}/services/${encodeURIComponent(serviceId)}/publications`
          );
          const rows = data.publications || [];
          openModal({
            title: "سجل النشر",
            hideFormActions: true,
            bodyHtml: `
              <div class="space-y-2 text-sm">
                ${
                  rows.length
                    ? rows
                        .map((h) => {
                          if (h.event_type === "unpublish") {
                            return `<div class="rounded-lg border border-slate-700 px-3 py-2 text-xs space-y-0.5">
                              <div class="text-amber-200">إلغاء نشر</div>
                              <div class="text-slate-500">${esc(h.published_at || "—")}</div>
                            </div>`;
                          }
                          return `<div class="rounded-lg border border-slate-700 px-3 py-2 text-xs space-y-0.5">
                            <div class="text-slate-200">${esc(h.name_ar || "—")} · ${esc(
                              h.amount_dh || ""
                            )} ${esc(h.currency || "")}</div>
                            <div class="text-slate-500">حدود ${esc(h.min_quantity)}–${esc(
                              h.max_quantity
                            )} · ${esc(h.source_label_ar || "")}</div>
                            <div class="text-slate-500">${esc(h.published_at || "—")}</div>
                          </div>`;
                        })
                        .join("")
                    : `<p class="text-slate-400">لا يوجد سجل نشر</p>`
                }
                <div class="flex justify-end pt-1">
                  <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إغلاق</button>
                </div>
              </div>`,
          });
        } catch (err) {
          alertBox(err.message, "err");
        }
      });
    }

    async function openChangePrice(serviceId, service, currentPrice, afterSave) {
      const selectedMode =
        (currentPrice && currentPrice.pricing_mode) ||
        (commercial.pricing_modes[0] && commercial.pricing_modes[0].code) ||
        "per_1000";
      openModal({
        title: "تعديل السعر",
        submitLabel: "متابعة",
        bodyHtml: `
          <p class="text-xs text-slate-400">الخدمة: <span class="text-slate-200">${esc(
            service.name_ar
          )}</span></p>
          <p class="text-xs text-slate-500">السعر الحالي: ${esc(priceLabel(currentPrice))}</p>
          <div>
            <label class="block text-xs text-slate-500 mb-1">طريقة التسعير</label>
            <select name="pricing_mode" required class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${pricingModeOptionsHtml(
              selectedMode
            )}</select>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">السعر (DH)</label>
            <input name="amount_dh" required value="${esc(
              (currentPrice && currentPrice.amount_dh) || ""
            )}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2" dir="ltr" placeholder="مثال: 2 أو 0.01 أو 1.48"/>
          </div>`,
        onSubmit: async (fd) => {
          const pricing_mode = String(fd.get("pricing_mode") || "").trim();
          const amount_dh = String(fd.get("amount_dh") || "").trim();
          const modeLabel =
            (commercial.pricing_modes || []).find((m) => m.code === pricing_mode)?.label_ar ||
            pricing_mode;
          closeModal();
          openModal({
            title: "حفظ السعر",
            submitLabel: "حفظ السعر",
            bodyHtml: `
              <div class="space-y-2 text-sm">
                <div><span class="text-slate-500">الخدمة:</span> ${esc(service.name_ar)}</div>
                <div><span class="text-slate-500">السعر الحالي:</span> ${esc(priceLabel(currentPrice))}</div>
                <div><span class="text-slate-500">السعر الجديد:</span> ${esc(amount_dh)} DH · ${esc(
              modeLabel
            )}</div>
              </div>`,
            onSubmit: async () => {
              const result = await json(
                `${API}/services/${encodeURIComponent(serviceId)}/price`,
                {
                  method: "POST",
                  body: JSON.stringify({
                    amount_dh,
                    pricing_mode,
                    currency: "MAD",
                  }),
                }
              );
              alertBox(result.message || "تم حفظ السعر");
              // Close confirm before afterSave so openModal's success closeModal
              // cannot wipe the details modal opened by refreshAfter.
              closeModal();
              try {
                if (typeof afterSave === "function") await afterSave();
                else await load();
              } catch (refreshErr) {
                alertBox(
                  "تم حفظ السعر، لكن تعذر تحديث الشاشة: " +
                    ((refreshErr && refreshErr.message) || "خطأ غير معروف"),
                  "err"
                );
              }
              return false;
            },
          });
          return false;
        },
      });
    }

    async function openChangeSource(serviceId, service, current, afterSave) {
      const providers = await loadProviderOptions();
      if (!providers.length) {
        throw new Error("لا توجد موردين مهيأين في النظام");
      }
      let pubStatus = {};
      try {
        pubStatus = await json(
          `${API}/services/${encodeURIComponent(serviceId)}/publication`
        );
      } catch (_) {
        pubStatus = {};
      }
      const providerOptions = providers
        .map(
          (p) =>
            `<option value="${esc(p.slug)}" ${
              current && current.provider_slug === p.slug ? "selected" : ""
            }>${esc(p.name)}</option>`
        )
        .join("");

      openModal({
        title: "استبدال مصدر التنفيذ",
        submitLabel: "معاينة الاستبدال",
        bodyHtml: `
          <p class="text-xs text-slate-400">الخدمة: <span class="text-slate-200">${esc(
            service.name_ar
          )}</span></p>
          <div class="cat-section">
            <h4>المصدر الحالي</h4>
            ${
              current
                ? `<div class="space-y-1 text-sm">
                    <div>${providerIdPrimaryHtml(current, { compact: true })}</div>
                    <div><span class="text-slate-500">المزود:</span> ${esc(
                      current.provider_name || current.provider_slug
                    )}</div>
                    <div><span class="text-slate-500">الحساب:</span> ${esc(
                      current.account_display_name || current.provider_account_key
                    )}</div>
                  </div>`
                : `<p class="text-sm text-slate-400">بدون مصدر</p>`
            }
          </div>
          <p class="text-[11px] text-slate-500">يمكنك استبدال معرّف المزود فقط مع الإبقاء على المزود والحساب، أو استبدال المصدر بالكامل.</p>
          <div>
            <label class="block text-xs text-slate-500 mb-1">المزود</label>
            <select name="provider_slug" id="src-provider" required class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2">${providerOptions}</select>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">الحساب</label>
            <select name="provider_account_key" id="src-account" required class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2"></select>
          </div>
          <div>
            <label class="block text-xs text-slate-500 mb-1">معرّف المزود (الجديد)</label>
            <input name="external_service_id" required value="${esc(
              opaqueProviderId(current) || ""
            )}" class="w-full rounded-lg border border-slate-600 bg-surface-800 px-3 py-2 font-mono" dir="ltr" autocomplete="off" spellcheck="false"/>
            <p class="text-[10px] text-slate-500 mt-1">نص شفاف — بدون تحويل رقمي</p>
          </div>`,
        onSubmit: async (fd) => {
          const provider_slug = String(fd.get("provider_slug") || "").trim();
          const provider_account_key = String(fd.get("provider_account_key") || "").trim();
          const external_service_id = String(fd.get("external_service_id") || "").trim();
          if (!external_service_id) {
            throw new Error("معرّف المزود مطلوب");
          }
          const provider = providers.find((p) => p.slug === provider_slug);
          const account = (provider?.accounts || []).find(
            (a) => a.key === provider_account_key
          );
          const pubLabel =
            pubStatus.publication_status === "published" ? "منشورة" : "غير منشورة";
          const driftNote =
            pubStatus.publication_status === "published"
              ? "يوجد تغيير غير منشور بعد التأكيد (لقطة النشر الحالية لن تتغير تلقائيًا)"
              : "لا يوجد نشر حالي — لن يُنشر تلقائيًا";

          closeModal();
          openModal({
            title: "معاينة الاستبدال",
            submitLabel: "تأكيد الاستبدال",
            bodyHtml: `
              <div class="space-y-3 text-sm">
                <div><span class="text-slate-500">الخدمة:</span> ${esc(service.name_ar)}</div>
                <div class="cat-section">
                  <h4>المصدر الحالي</h4>
                  <div>المزود: ${esc(
                    (current && (current.provider_name || current.provider_slug)) || "—"
                  )}</div>
                  <div>الحساب: ${esc(
                    (current &&
                      (current.account_display_name || current.provider_account_key)) ||
                      "—"
                  )}</div>
                  <div>معرّف المزود: <code dir="ltr" class="font-mono text-sky-200">${esc(
                    opaqueProviderId(current) || "—"
                  )}</code></div>
                </div>
                <div class="cat-section">
                  <h4>المصدر الجديد</h4>
                  <div>المزود: ${esc(provider?.name || provider_slug)}</div>
                  <div>الحساب: ${esc(account?.label || provider_account_key)}</div>
                  <div>معرّف المزود: <code dir="ltr" class="font-mono text-sky-200">${esc(
                    external_service_id
                  )}</code></div>
                </div>
                <div class="cat-section space-y-1 text-xs">
                  <div><span class="text-slate-500">هوية SOLDIUM:</span> لن تتغير</div>
                  <div><span class="text-slate-500">الطلبات السابقة:</span> لن تتغير</div>
                  <div><span class="text-slate-500">النشر الحالي:</span> لن يتغير مباشرة</div>
                  <div><span class="text-slate-500">حالة النشر:</span> ${esc(pubLabel)} — ${esc(
              driftNote
            )}</div>
                </div>
              </div>`,
            onSubmit: async () => {
              // #region agent log
              fetch('http://127.0.0.1:7300/ingest/fb230002-436a-430b-84ba-58e474409a6c',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'3df71b'},body:JSON.stringify({sessionId:'3df71b',runId:'pre-fix',hypothesisId:'D',location:'catalog_core_ui.js:confirmReplace',message:'confirm replace submit',data:{serviceId,provider_slug,provider_account_key,external_service_id},timestamp:Date.now()})}).catch(()=>{});
              // #endregion
              const result = await json(
                `${API}/services/${encodeURIComponent(serviceId)}/execution-source`,
                {
                  method: "POST",
                  body: JSON.stringify({
                    provider_slug,
                    provider_account_key,
                    external_service_id,
                  }),
                }
              );
              // #region agent log
              fetch('http://127.0.0.1:7300/ingest/fb230002-436a-430b-84ba-58e474409a6c',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'3df71b'},body:JSON.stringify({sessionId:'3df71b',runId:'pre-fix',hypothesisId:'A',location:'catalog_core_ui.js:confirmReplace',message:'confirm replace HTTP 200',data:{serviceId,external_service_id,ok:!!(result&&result.ok),message:result&&result.message,wt:result&&result.legacy_write_through&&result.legacy_write_through.outcome},timestamp:Date.now()})}).catch(()=>{});
              // #endregion
              alertBox(result.message || "تم تغيير مصدر التنفيذ");
              // Critical: close confirm BEFORE afterSave. Otherwise openModal's
              // submit handler runs `closeModal()` after onSubmit returns and
              // destroys the details modal that refreshAfter just opened —
              // looking like "nothing changed" / a failed replace.
              // Also: afterSave failures must NOT surface as replace ISE.
              closeModal();
              try {
                if (typeof afterSave === "function") await afterSave();
                else await load();
              } catch (refreshErr) {
                // #region agent log
                fetch('http://127.0.0.1:7300/ingest/fb230002-436a-430b-84ba-58e474409a6c',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'3df71b'},body:JSON.stringify({sessionId:'3df71b',runId:'pre-fix',hypothesisId:'A',location:'catalog_core_ui.js:afterSave',message:'afterSave failed after successful replace',data:{serviceId,err:String(refreshErr&&refreshErr.message||refreshErr)},timestamp:Date.now()})}).catch(()=>{});
                // #endregion
                alertBox(
                  "تم تغيير مصدر التنفيذ، لكن تعذر تحديث الشاشة: " +
                    ((refreshErr && refreshErr.message) || "خطأ غير معروف"),
                  "err"
                );
              }
              return false;
            },
          });
          return false;
        },
      });

      wireCopyProviderIds(document.getElementById("modal-root"));

      const fillAccounts = () => {
        const slug = document.getElementById("src-provider").value;
        const provider = providers.find((p) => p.slug === slug);
        const sel = document.getElementById("src-account");
        const accounts = provider?.accounts || [];
        sel.innerHTML = accounts.length
          ? accounts
              .map(
                (a) =>
                  `<option value="${esc(a.key)}" ${
                    current &&
                    current.provider_slug === slug &&
                    current.provider_account_key === a.key
                      ? "selected"
                      : ""
                  }>${esc(a.label)}</option>`
              )
              .join("")
          : `<option value="">لا توجد حسابات</option>`;
      };
      document.getElementById("src-provider").addEventListener("change", fillAccounts);
      fillAccounts();
    }

    async function openMove(serviceId) {
      const data = await json(`${API}/services/${encodeURIComponent(serviceId)}`);
      const s = data.service;
      openModal({
        title: "نقل الخدمة",
        submitLabel: "تأكيد النقل",
        bodyHtml: `
          <p class="text-sm text-slate-300 mb-2">نقل «${esc(s.name_ar)}» — الهوية ومصدر التنفيذ لن يتغيّرا.</p>
          ${destinationSelectHtml(tree, s.parent_entry_id || "")}`,
        onSubmit: async (fd) => {
          const parent = String(fd.get("parent_entry_id") || "").trim() || null;
          await json(`${API}/services/${encodeURIComponent(serviceId)}/move`, {
            method: "POST",
            body: JSON.stringify({ parent_entry_id: parent }),
          });
          alertBox("تم النقل");
          await load();
        },
      });
    }

    document.getElementById("svc-tbody").addEventListener("click", (e) => {
      const edit = e.target.closest("[data-edit]");
      const move = e.target.closest("[data-move]");
      const del = e.target.closest("[data-delete]");
      const restore = e.target.closest("[data-restore]");
      if (edit) openDetails(edit.getAttribute("data-edit")).catch((err) => alertBox(err.message, "err"));
      if (move) openMove(move.getAttribute("data-move")).catch((err) => alertBox(err.message, "err"));
      if (del) {
        openDeleteConfirm(
          del.getAttribute("data-delete"),
          del.getAttribute("data-delete-name") || ""
        ).catch((err) => alertBox(err.message, "err"));
      }
      if (restore) {
        const sid = restore.getAttribute("data-restore");
        json(`${API}/services/${encodeURIComponent(sid)}/restore`, {
          method: "POST",
          body: JSON.stringify({ status: "active" }),
        })
          .then(() => {
            alertBox("تمت استعادة الخدمة");
            return load();
          })
          .catch((err) => alertBox(err.message, "err"));
      }
    });

    document.getElementById("svc-reload").onclick = () => {
      page = 1;
      load().catch((e) => alertBox(e.message, "err"));
    };
    document.getElementById("svc-search").onkeydown = (e) => {
      if (e.key === "Enter") {
        page = 1;
        load().catch((err) => alertBox(err.message, "err"));
      }
    };
    document.getElementById("svc-status").onchange = () => {
      page = 1;
      load().catch((e) => alertBox(e.message, "err"));
    };
    ["svc-source", "svc-type", "svc-ordering", "svc-price", "svc-pricing-mode", "svc-readiness"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) {
        el.onchange = () => {
          page = 1;
          load().catch((e) => alertBox(e.message, "err"));
        };
      }
    });
    const platEl = document.getElementById("svc-platform");
    const secEl = document.getElementById("svc-section");
    const subEl = document.getElementById("svc-subsection");
    if (platEl) {
      platEl.onchange = () => {
        const sec = document.getElementById("svc-section");
        const sub = document.getElementById("svc-subsection");
        if (sec) sec.value = "";
        if (sub) sub.value = "";
        refreshPlacementFilters();
        page = 1;
        load().catch((e) => alertBox(e.message, "err"));
      };
    }
    if (secEl) {
      secEl.onchange = () => {
        const sub = document.getElementById("svc-subsection");
        if (sub) sub.value = "";
        refreshPlacementFilters();
        page = 1;
        load().catch((e) => alertBox(e.message, "err"));
      };
    }
    if (subEl) {
      subEl.onchange = () => {
        page = 1;
        load().catch((e) => alertBox(e.message, "err"));
      };
    }
    const clearBtn = document.getElementById("svc-clear-filters");
    if (clearBtn) {
      clearBtn.onclick = () => {
        const search = document.getElementById("svc-search");
        if (search) search.value = "";
        [
          "svc-platform",
          "svc-section",
          "svc-subsection",
          "svc-status",
          "svc-type",
          "svc-ordering",
          "svc-source",
          "svc-price",
          "svc-pricing-mode",
          "svc-readiness",
        ].forEach((id) => {
          const el = document.getElementById(id);
          if (el) el.value = "";
        });
        refreshPlacementFilters();
        page = 1;
        load().catch((e) => alertBox(e.message, "err"));
      };
    }
    document.getElementById("svc-prev").onclick = () => {
      if (page > 1) {
        page -= 1;
        load().catch((e) => alertBox(e.message, "err"));
      }
    };
    document.getElementById("svc-next").onclick = () => {
      page += 1;
      load().catch((e) => alertBox(e.message, "err"));
    };

    document.getElementById("svc-create").onclick = () => {
      openModal({
        title: "إضافة خدمة",
        submitLabel: "إضافة",
        bodyHtml: `
          ${commercialFormFields(null)}
          <div class="cat-section space-y-2">
            <h4>مكان الخدمة</h4>
            ${destinationSelectHtml(tree, "")}
            <p class="text-[11px] text-slate-500">مصدر التنفيذ يُعيَّن لاحقاً من تفاصيل الخدمة.</p>
          </div>`,
        onSubmit: async (fd) => {
          const parent = String(fd.get("parent_entry_id") || "").trim() || null;
          await json(`${API}/services`, {
            method: "POST",
            body: JSON.stringify({
              ...payloadFromCommercialForm(fd),
              parent_entry_id: parent,
            }),
          });
          alertBox("تمت الإضافة");
          page = 1;
          await load();
        },
      });
    };

    Promise.all([loadTree(), loadCommercialOptions()])
      .then(async () => {
        await load();
        const openId = new URLSearchParams(window.location.search).get("open");
        if (openId) {
          await openDetails(openId);
        }
      })
      .catch((e) => alertBox(e.message, "err"));
  }

  function mountReview() {
    let page = 1;
    let provPage = 1;
    let commercial = { service_types: [], ordering_modes: [] };
    let providerAccounts = []; // {provider_slug, account_key} from summary
    let activeTab = "readiness";

    function switchTab(tab) {
      activeTab = tab;
      document.querySelectorAll("[data-review-tab]").forEach((btn) => {
        const selected = btn.getAttribute("data-review-tab") === tab;
        btn.setAttribute("aria-selected", selected ? "true" : "false");
      });
      const readinessPanel = document.getElementById("panel-readiness");
      const providerPanel = document.getElementById("panel-provider");
      if (readinessPanel) readinessPanel.classList.toggle("hidden", tab !== "readiness");
      if (providerPanel) providerPanel.classList.toggle("hidden", tab !== "provider");
      if (tab === "provider") {
        loadProviderSummary()
          .then(loadProviderReview)
          .catch((e) => alertBox(e.message, "err"));
      }
    }

    document.querySelectorAll("[data-review-tab]").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-review-tab")));
    });

    async function loadOptions() {
      const data = await json(`${API}/commercial-options`);
      commercial = {
        service_types: data.service_types || [],
        ordering_modes: data.ordering_modes || [],
      };
      const typeSel = document.getElementById("review-filter-type");
      const modeSel = document.getElementById("review-filter-ordering");
      if (typeSel) {
        typeSel.innerHTML =
          `<option value="">الكل</option>` +
          commercial.service_types
            .map((t) => `<option value="${esc(t.code)}">${esc(t.label_ar)}</option>`)
            .join("");
      }
      if (modeSel) {
        modeSel.innerHTML =
          `<option value="">الكل</option>` +
          commercial.ordering_modes
            .map((m) => `<option value="${esc(m.code)}">${esc(m.label_ar)}</option>`)
            .join("");
      }
    }

    async function loadSummary() {
      const data = await json(`${API}/review/summary`);
      const s = data.summary || {};
      const counts = [
        ["إجمالي الخدمات", s.total_services],
        ["جاهزة", s.ready],
        ["تحتاج مراجعة", s.needs_review],
        ["بدون مصدر", s.without_source],
        ["بدون سعر", s.without_price],
      ];
      const el = document.getElementById("review-counts");
      if (!el) return;
      el.innerHTML = counts
        .map(
          ([label, n]) =>
            `<div class="cat-review-count"><div class="n">${esc(n ?? 0)}</div><div class="l">${esc(
              label
            )}</div></div>`
        )
        .join("");
    }

    async function load() {
      const q = new URLSearchParams({ page: String(page), limit: "50" });
      const readiness = document.getElementById("review-filter-readiness")?.value;
      const status = document.getElementById("review-filter-status")?.value || "";
      const serviceType = document.getElementById("review-filter-type")?.value || "";
      const ordering = document.getElementById("review-filter-ordering")?.value || "";
      const search = document.getElementById("review-filter-search")?.value.trim() || "";
      if (readiness) q.set("readiness", readiness);
      if (status) q.set("status", status);
      if (serviceType) q.set("service_type", serviceType);
      if (ordering) q.set("ordering_mode", ordering);
      if (search) q.set("search", search);
      const data = await json(`${API}/review?` + q);
      const list = document.getElementById("review-list");
      const services = data.services || [];
      if (!services.length) {
        list.innerHTML = `<div class="cat-review-card text-center text-slate-400 text-sm">لا توجد خدمات مطابقة</div>`;
      } else {
        list.innerHTML = services
          .map((s, idx) => {
            const path = (s.location_path || []).join(" › ") || "الجذر";
            const issues = (s.readiness && s.readiness.issues) || [];
            const issuesHtml = issues.length
              ? `<ul class="cat-review-issues">${issues
                  .map((i) => `<li>${esc(i.title)}</li>`)
                  .join("")}</ul>`
              : `<p class="text-xs text-emerald-300 mt-2 mb-0">لا توجد مشاكل</p>`;
            return `<article class="cat-review-card space-y-2">
              <div class="flex flex-wrap justify-between gap-2 items-start">
                <div>
                  <div class="text-xs text-slate-500">${idx + 1 + (page - 1) * 50}</div>
                  ${providerIdPrimaryHtml(s.current_source, { compact: true })}
                  <h3 class="text-sm font-semibold text-white m-0 mt-1">${esc(s.name_ar)}</h3>
                </div>
                <div>${readinessBadge(s.readiness)}</div>
              </div>
              <div class="flex flex-wrap gap-2 text-[11px] text-slate-400">
                <span>الحالة: ${esc(STATUS_AR[s.status] || s.status)}</span>
                <span>النوع: ${esc(s.service_type_label_ar || s.service_type || "—")}</span>
                <span>المصدر: ${esc(sourceLabel(s.current_source))}</span>
                <span>السعر: ${esc(
                  s.current_price && s.current_price.display_ar
                    ? s.current_price.display_ar
                    : "بدون سعر"
                )}</span>
                <span>المكان: ${esc(path)}</span>
              </div>
              <div>
                <div class="text-xs text-slate-400">المشاكل:</div>
                ${issuesHtml}
              </div>
              <div>
                <a class="text-sky-300 text-xs underline" href="/catalog/services?open=${esc(
                  s.id
                )}">فتح الخدمة</a>
              </div>
            </article>`;
          })
          .join("");
        wireCopyProviderIds(list);
      }
      document.getElementById("review-page-info").textContent =
        `صفحة ${data.current_page}/${data.total_pages} · ${data.total_items}`;
    }

    function fillProviderFilters(accounts) {
      providerAccounts = accounts || [];
      const provSel = document.getElementById("prov-filter-provider");
      const accSel = document.getElementById("prov-filter-account");
      if (!provSel || !accSel) return;
      const providers = [...new Set(providerAccounts.map((a) => a.provider_slug))];
      const selectedProv = provSel.value;
      provSel.innerHTML =
        `<option value="">الكل</option>` +
        providers.map((p) => `<option value="${esc(p)}">${esc(p)}</option>`).join("");
      if (selectedProv) provSel.value = selectedProv;
      refreshAccountOptions();
    }

    function refreshAccountOptions() {
      const provSel = document.getElementById("prov-filter-provider");
      const accSel = document.getElementById("prov-filter-account");
      if (!provSel || !accSel) return;
      const selectedProv = provSel.value;
      const selectedAcc = accSel.value;
      const accounts = providerAccounts.filter(
        (a) => !selectedProv || a.provider_slug === selectedProv
      );
      const keys = [...new Set(accounts.map((a) => a.provider_account_key))];
      accSel.innerHTML =
        `<option value="">الكل</option>` +
        keys.map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join("");
      if (selectedAcc && keys.includes(selectedAcc)) accSel.value = selectedAcc;
    }

    async function loadProviderSummary() {
      const q = new URLSearchParams();
      const provider = document.getElementById("prov-filter-provider")?.value || "";
      const account = document.getElementById("prov-filter-account")?.value || "";
      if (provider) q.set("provider_slug", provider);
      if (account) q.set("account_key", account);
      const data = await json(`${API}/provider-review/summary?` + q);
      const s = data.summary || {};
      fillProviderFilters(s.accounts || []);
      const counts = [
        ["تحتاج مراجعة", s.needs_review],
        ["خدمات جديدة", s.new_count],
        ["خدمات متغيرة", s.changed_count],
        ["خدمات مفقودة", s.missing_count],
      ];
      const el = document.getElementById("prov-review-counts");
      if (el) {
        el.innerHTML = counts
          .map(
            ([label, n]) =>
              `<div class="cat-review-count"><div class="n">${esc(n ?? 0)}</div><div class="l">${esc(
                label
              )}</div></div>`
          )
          .join("");
      }
    }

    function providerItemFieldsHtml(item) {
      if (!item) return `<p class="text-sm text-slate-400">لا توجد بيانات حالية</p>`;
      const rows = [
        ["معرّف الخدمة لدى المزود", item.external_service_id],
        ["الاسم", item.provider_service_name],
        ["التصنيف", item.provider_category],
        ["النوع", item.provider_type],
        ["الوصف", item.provider_description],
        ["الحد الأدنى", item.min_quantity],
        ["الحد الأقصى", item.max_quantity],
        ["سعر المزود", item.provider_rate],
        ["إعادة التعبئة", item.refill === true ? "نعم" : item.refill === false ? "لا" : null],
        ["الإلغاء", item.cancel === true ? "نعم" : item.cancel === false ? "لا" : null],
        ["التوزيع التدريجي", item.dripfeed === true ? "نعم" : item.dripfeed === false ? "لا" : null],
      ];
      return `<div class="space-y-1 text-sm">${rows
        .map(([label, val]) => {
          if (val === null || val === undefined || val === "") return "";
          return `<div><span class="text-slate-500">${esc(label)}:</span> ${esc(val)}</div>`;
        })
        .join("")}</div>`;
    }

    function mappingStatusLabel(item) {
      const st = item.mapping_status;
      if (st && st.label_ar) return st.label_ar;
      return item.soldium_link_note_ar || "غير مرتبط";
    }

    function mappingActionsHtml(item, prefix) {
      const mapped = !!(item.mapping_status && item.mapping_status.mapped);
      if (!mapped) {
        return `<button type="button" class="rounded-lg bg-accent-600 hover:bg-accent-500 px-3 py-1.5 text-xs" data-${prefix}-map="1">ربط بخدمة موجودة</button>
          <a href="/catalog/services" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs inline-block">إنشاء خدمة Soldium يدويًا</a>`;
      }
      const applied = !!(item.mapping_status && item.mapping_status.applied);
      const applyBtns = applied
        ? ""
        : `<button type="button" class="rounded-lg border border-sky-700/60 px-3 py-1.5 text-xs text-sky-200" data-${prefix}-preview="1">معاينة التطبيق</button>
           <button type="button" class="rounded-lg bg-accent-600 hover:bg-accent-500 px-3 py-1.5 text-xs" data-${prefix}-apply="1">تطبيق المصدر</button>`;
      return `<button type="button" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs" data-${prefix}-view="1">عرض الربط</button>
        <button type="button" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs" data-${prefix}-change="1">تغيير الربط</button>
        <button type="button" class="rounded-lg border border-amber-700/60 px-3 py-1.5 text-xs text-amber-200" data-${prefix}-unmap="1">إنهاء الربط</button>
        <button type="button" class="rounded-lg border border-slate-600 px-3 py-1.5 text-xs" data-${prefix}-history="1">سجل الربط</button>
        ${applyBtns}`;
    }

    function bindMappingActions(root, item, prefix) {
      const refresh = () =>
        loadProviderSummary()
          .then(loadProviderReview)
          .catch((e) => alertBox(e.message, "err"));
      const mapBtn = root.querySelector(`[data-${prefix}-map]`);
      const changeBtn = root.querySelector(`[data-${prefix}-change]`);
      const viewBtn = root.querySelector(`[data-${prefix}-view]`);
      const unmapBtn = root.querySelector(`[data-${prefix}-unmap]`);
      const histBtn = root.querySelector(`[data-${prefix}-history]`);
      const previewBtn = root.querySelector(`[data-${prefix}-preview]`);
      const applyBtn = root.querySelector(`[data-${prefix}-apply]`);
      if (mapBtn) mapBtn.addEventListener("click", () => openMapToSoldiumPicker(item, refresh));
      if (changeBtn) changeBtn.addEventListener("click", () => openMapToSoldiumPicker(item, refresh, true));
      if (viewBtn) viewBtn.addEventListener("click", () => openMappingDetails(item));
      if (unmapBtn) unmapBtn.addEventListener("click", () => openUnmapConfirm(item, refresh));
      if (histBtn) histBtn.addEventListener("click", () => openMappingHistory(item));
      if (previewBtn) previewBtn.addEventListener("click", () => openApplyPreview(item, refresh, false));
      if (applyBtn) applyBtn.addEventListener("click", () => openApplyPreview(item, refresh, true));
    }

    function applyStatusBlockHtml(st) {
      if (!st || !st.mapped) return "";
      const m = st.mapping || {};
      const mapLabel = `${m.provider_name || m.provider_slug || "—"} / ${
        m.account_display_name || m.provider_account_key || "—"
      } / ${m.external_service_id || "—"}`;
      return `<div class="rounded-lg border border-slate-700 px-3 py-2 text-xs space-y-1">
        <div><span class="text-slate-500">الربط:</span> <span dir="ltr">${esc(mapLabel)}</span></div>
        <div><span class="text-slate-500">المصدر الحالي:</span> ${esc(
          st.current_source_label_ar || "بدون مصدر"
        )}</div>
        <div><span class="text-slate-500">حالة المصدر:</span>
          <span class="${st.applied ? "text-emerald-300" : "text-amber-200"}">${esc(
            st.apply_label_ar || (st.applied ? "مطبق" : "غير مطبق")
          )}</span>
        </div>
      </div>`;
    }

    async function openApplyPreview(item, onDone, openConfirm) {
      const m = item.mapping_status && item.mapping_status.mapping;
      if (!m || !m.id) {
        alertBox("لا يوجد ربط نشط", "err");
        return;
      }
      const data = await json(
        `${API}/provider-mappings/${encodeURIComponent(m.id)}/apply-preview`
      );
      const p = data.preview || {};
      const readyHtml =
        p.readiness && (p.readiness.ready || p.readiness.state === "ready")
          ? `<span class="text-emerald-300">✓ جاهزة</span>`
          : `<span class="text-amber-300">⚠ تحتاج مراجعة</span>`;
      const blockNote = !p.can_apply
        ? `<p class="text-sm text-rose-200">${esc(p.message_ar || "التطبيق غير متاح")}</p>
           ${
             p.blocking_code === "readiness_failed"
               ? `<p class="text-xs text-slate-400 mt-1">راجع تبويب جاهزية الخدمات لإصلاح المشاكل.</p>
                  <a href="/catalog/review" class="text-sky-300 text-xs underline">فتح مراجعة الجاهزية</a>`
               : ""
           }`
        : `<p class="text-sm text-slate-300">${esc(p.message_ar || "")}</p>`;

      const bodyHtml = `
        <div class="space-y-3 text-sm">
          <div class="cat-section space-y-1">
            <h4>معاينة التطبيق</h4>
            <div><span class="text-slate-500">الخدمة:</span> ${esc(
              p.soldium_service_name_ar || "—"
            )}</div>
            <div><span class="text-slate-500">المصدر الحالي:</span> ${esc(
              p.current_source_label_ar || "بدون مصدر"
            )}</div>
            <div><span class="text-slate-500">المصدر الجديد:</span> ${esc(
              p.proposed_source_label_ar || "—"
            )}</div>
            <div><span class="text-slate-500">الجاهزية:</span> ${readyHtml}</div>
            <div><span class="text-slate-500">النتيجة:</span> ${esc(
              p.outcome_label_ar || "—"
            )}</div>
          </div>
          ${blockNote}
          ${
            p.can_apply && p.would_change
              ? `<p class="text-[11px] text-slate-500">المصدر الحالي سيبقى محفوظًا في السجل كمصدر تاريخي.</p>`
              : ""
          }
          <div class="flex justify-end gap-2 pt-1">
            <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إلغاء</button>
            ${
              p.can_apply && (openConfirm || p.would_change)
                ? `<button type="button" id="btn-do-apply" class="rounded-lg bg-accent-600 hover:bg-accent-500 px-3 py-2" ${
                    p.can_apply && p.would_change ? "" : p.applied_already ? "" : "disabled"
                  }>${p.would_change ? "تطبيق المصدر" : "تأكيد"}</button>`
                : ""
            }
          </div>
        </div>`;

      const root = openModal({
        title: "هل تريد تطبيق مصدر التنفيذ؟",
        hideFormActions: true,
        bodyHtml,
      });

      const doBtn = root.querySelector("#btn-do-apply");
      if (doBtn && p.can_apply) {
        doBtn.addEventListener("click", async () => {
          try {
            const cur = p.current_source || {};
            const body = p.current_source
              ? {
                  expect_no_current_source: false,
                  expected_current_provider_slug: cur.provider_slug,
                  expected_current_provider_account_key: cur.provider_account_key,
                  expected_current_external_service_id: cur.external_service_id,
                }
              : {
                  expect_no_current_source: true,
                  expected_current_provider_slug: null,
                  expected_current_provider_account_key: null,
                  expected_current_external_service_id: null,
                };
            const res = await json(
              `${API}/provider-mappings/${encodeURIComponent(m.id)}/apply`,
              { method: "POST", body: JSON.stringify(body) }
            );
            alertBox(res.message_ar || "تم تطبيق المصدر");
            closeModal();
            if (onDone) await onDone();
          } catch (err) {
            alertBox(err.message, "err");
          }
        });
      }
    }

    function openMappingDetails(item) {
      const st = item.mapping_status || {};
      const m = st.mapping || null;
      const provName =
        (item.item && item.item.provider_service_name) ||
        (item.previous_item && item.previous_item.provider_service_name) ||
        "—";
      if (!m) {
        openModal({
          title: "الربط",
          hideFormActions: true,
          bodyHtml: `<p class="text-sm text-slate-300">غير مرتبط</p>
            <div class="flex justify-end pt-2"><button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إغلاق</button></div>`,
        });
        return;
      }
      const stale =
        m.seen_in_latest_snapshot === false
          ? `<p class="text-xs text-amber-200">تحذير: معرّف المزود غير موجود في أحدث لقطة ناجحة — الربط محفوظ ولا يُحذف تلقائيًا.</p>`
          : "";
      const root = openModal({
        title: "عرض الربط",
        hideFormActions: true,
        bodyHtml: `
          <div class="space-y-3 text-sm">
            <div class="cat-section space-y-1">
              <h4>خدمة المزود</h4>
              <div><span class="text-slate-500">المزود:</span> ${esc(m.provider_name || m.provider_slug)}</div>
              <div><span class="text-slate-500">الحساب:</span> ${esc(m.account_display_name || m.provider_account_key)}</div>
              <div><span class="text-slate-500">معرّف الخدمة لدى المزود:</span> <span dir="ltr">${esc(m.external_service_id)}</span></div>
              <div><span class="text-slate-500">اسم الخدمة لدى المزود:</span> ${esc(provName)}</div>
            </div>
            <div class="text-center text-slate-500 text-xs">↓ علاقة إدارية صريحة ↓</div>
            <div class="cat-section space-y-1">
              <h4>خدمة Soldium</h4>
              <div class="text-base text-white font-semibold">${esc(m.soldium_service_name_ar || "—")}</div>
              <div class="text-xs text-slate-500">الحالة: ${esc(STATUS_AR[m.soldium_service_status] || m.soldium_service_status || "—")}</div>
              <div class="text-xs text-emerald-300 mt-1">● ${esc(m.status_label_ar || "نشط")}</div>
            </div>
            ${applyStatusBlockHtml(st)}
            ${stale}
            <p class="text-[11px] text-slate-500">الربط لا يغيّر مصدر التنفيذ تلقائيًا — استخدم تطبيق المصدر صراحةً.</p>
            <div class="flex flex-wrap gap-2">${mappingActionsHtml(item, "view")}</div>
            <div class="flex justify-end pt-1">
              <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إغلاق</button>
            </div>
          </div>`,
      });
      bindMappingActions(root, item, "view");
    }

    async function openMappingHistory(item) {
      const q = new URLSearchParams({
        provider_slug: item.provider_slug,
        account_key: item.provider_account_key,
        external_service_id: item.external_service_id,
      });
      const data = await json(`${API}/provider-mappings/history?` + q);
      const rows = data.history || [];
      openModal({
        title: "سجل الربط",
        hideFormActions: true,
        bodyHtml: `
          <div class="space-y-2 text-sm">
            ${
              rows.length
                ? rows
                    .map(
                      (h) => `<div class="rounded-lg border border-slate-700 px-3 py-2 text-xs space-y-0.5">
                        <div class="text-slate-200">${esc(h.soldium_service_name_ar || "—")}</div>
                        <div class="text-slate-500">${esc(h.status_label_ar || h.status)} · من ${esc(
                          h.mapped_at || "—"
                        )} ${h.ended_at ? `إلى ${esc(h.ended_at)}` : ""}</div>
                      </div>`
                    )
                    .join("")
                : `<p class="text-slate-400">لا يوجد سجل ربط</p>`
            }
            <div class="flex justify-end pt-1">
              <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إغلاق</button>
            </div>
          </div>`,
      });
    }

    function openUnmapConfirm(item, onDone) {
      openModal({
        title: "إنهاء الربط",
        submitLabel: "إنهاء الربط",
        bodyHtml: `
          <p class="text-sm text-slate-300">سيتم إنهاء الربط النشط وحفظه في السجل. لن يتأثر مصدر التنفيذ أو السعر أو الجاهزية.</p>
          <p class="text-xs text-slate-500">الخدمة المرتبطة: ${esc(mappingStatusLabel(item))}</p>`,
        onSubmit: async () => {
          await json(`${API}/provider-mappings/unmap`, {
            method: "POST",
            body: JSON.stringify({
              provider_slug: item.provider_slug,
              provider_account_key: item.provider_account_key,
              external_service_id: item.external_service_id,
            }),
          });
          alertBox("تم إنهاء الربط");
          if (onDone) await onDone();
        },
      });
    }

    function openMapToSoldiumPicker(item, onDone, isChange) {
      const root = openModal({
        title: isChange ? "تغيير الربط" : "ربط بخدمة موجودة",
        hideFormActions: true,
        bodyHtml: `
          <div class="space-y-3 text-sm">
            <p class="text-xs text-slate-400">اختر خدمة Soldium موجودة. لا يتم إنشاء خدمة جديدة من هذه الشاشة.</p>
            <label class="grid gap-1 text-xs text-slate-500">
              بحث
              <input type="search" id="map-svc-search" placeholder="اسم الخدمة أو النوع…"
                     class="rounded-lg border border-slate-600 bg-surface-800 px-3 py-2 text-sm" />
            </label>
            <div id="map-svc-results" class="space-y-2 max-h-64 overflow-y-auto"></div>
            <div class="flex justify-end gap-2 pt-1">
              <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إلغاء</button>
            </div>
          </div>`,
      });

      const renderResults = async (term) => {
        const box = root.querySelector("#map-svc-results");
        const q = new URLSearchParams({ limit: "30", page: "1" });
        if (term) q.set("search", term);
        q.set("status", "draft");
        // Load draft + active (exclude archived via two calls merged)
        const [drafts, actives] = await Promise.all([
          json(`${API}/services?` + new URLSearchParams({ ...Object.fromEntries(q), status: "draft" })),
          json(`${API}/services?` + new URLSearchParams({ ...Object.fromEntries(q), status: "active" })),
        ]);
        const services = [...(drafts.services || []), ...(actives.services || [])];
        if (!services.length) {
          box.innerHTML = `<p class="text-xs text-slate-400">لا توجد خدمات مطابقة</p>`;
          return;
        }
        box.innerHTML = services
          .map((s) => {
            const path = (s.location_path || []).join(" › ") || "الجذر";
            return `<button type="button" class="w-full text-right rounded-lg border border-slate-700 hover:border-sky-600 px-3 py-2 space-y-0.5" data-pick-svc="${esc(
              s.id
            )}">
              <div class="text-sm text-white font-medium">${esc(s.name_ar)}</div>
              <div class="text-[11px] text-slate-500">${esc(path)} · ${esc(
                s.service_type_label_ar || s.service_type || "—"
              )} · ${esc(STATUS_AR[s.status] || s.status)}</div>
            </button>`;
          })
          .join("");
        box.querySelectorAll("[data-pick-svc]").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const sid = btn.getAttribute("data-pick-svc");
            try {
              await json(`${API}/provider-mappings`, {
                method: "POST",
                body: JSON.stringify({
                  provider_slug: item.provider_slug,
                  provider_account_key: item.provider_account_key,
                  external_service_id: item.external_service_id,
                  soldium_service_id: sid,
                }),
              });
              alertBox(isChange ? "تم تغيير الربط" : "تم حفظ الربط");
              closeModal();
              if (onDone) await onDone();
            } catch (err) {
              alertBox(err.message, "err");
            }
          });
        });
      };

      const searchEl = root.querySelector("#map-svc-search");
      let t = null;
      searchEl.addEventListener("input", () => {
        clearTimeout(t);
        t = setTimeout(() => renderResults(searchEl.value.trim()).catch((e) => alertBox(e.message, "err")), 250);
      });
      renderResults("").catch((e) => alertBox(e.message, "err"));
    }

    function openProviderReviewDetails(item) {
      const changesHtml = (item.field_changes || [])
        .map(
          (f) => `<div class="cat-field-change">
            <div class="text-slate-200 font-medium">${esc(f.label_ar)}</div>
            <div class="text-slate-500">السابق: ${esc(f.previous_display ?? "—")}</div>
            <div class="text-slate-300">الحالي: ${esc(f.current_display ?? "—")}</div>
          </div>`
        )
        .join("");
      const body =
        item.change_type === "changed"
          ? `<div class="space-y-2">${changesHtml || `<p class="text-sm text-slate-400">لا توجد حقول متغيرة</p>`}</div>`
          : item.change_type === "missing"
            ? providerItemFieldsHtml(item.previous_item)
            : providerItemFieldsHtml(item.item);

      const root = openModal({
        title: `${item.change_type_label_ar || "تفاصيل"} · ${item.external_service_id}`,
        hideFormActions: true,
        bodyHtml: `
          <div class="space-y-3 text-sm">
            <p class="text-xs text-slate-400">${esc(item.note_ar || "")}</p>
            <div class="flex flex-wrap gap-2 text-[11px] text-slate-400">
              <span>المزود: ${esc(item.provider_slug)}</span>
              <span>الحساب: ${esc(item.provider_account_key)}</span>
            </div>
            <div class="text-[11px] text-slate-500">
              الاكتشاف الحالي: ${esc(item.current_discovered_at || "—")}<br/>
              الاكتشاف السابق: ${esc(item.previous_discovered_at || "—")}
            </div>
            <div class="cat-section space-y-2">
              <h4>الربط</h4>
              <div class="text-sm text-slate-200">${esc(mappingStatusLabel(item))}</div>
              ${item.mapping_status && item.mapping_status.mapped ? applyStatusBlockHtml(item.mapping_status) : ""}
              <div class="flex flex-wrap gap-2">${mappingActionsHtml(item, "detail")}</div>
            </div>
            ${body}
            <div class="flex justify-end pt-1">
              <button type="button" class="rounded-lg border border-slate-600 px-3 py-2" data-close="1">إغلاق</button>
            </div>
          </div>`,
      });
      bindMappingActions(root, item, "detail");
    }

    async function loadProviderReview() {
      const q = new URLSearchParams({ page: String(provPage), limit: "50" });
      const provider = document.getElementById("prov-filter-provider")?.value || "";
      const account = document.getElementById("prov-filter-account")?.value || "";
      const changeType = document.getElementById("prov-filter-change")?.value || "";
      const search = document.getElementById("prov-filter-search")?.value.trim() || "";
      if (provider) q.set("provider_slug", provider);
      if (account) q.set("account_key", account);
      if (changeType) q.set("change_type", changeType);
      if (search) q.set("search", search);
      const data = await json(`${API}/provider-review?` + q);
      const list = document.getElementById("prov-review-list");
      const items = data.items || [];
      const accounts = data.accounts || [];
      const baselineOnly = accounts.filter((a) => a.is_baseline_only);
      if (!items.length) {
        const msg =
          baselineOnly.length && !accounts.some((a) => a.has_comparison)
            ? "لا توجد مقارنة سابقة — تم إنشاء اللقطة الأساسية فقط."
            : "لا توجد عناصر تحتاج مراجعة";
        list.innerHTML = `<div class="cat-review-card text-center text-slate-400 text-sm">${esc(msg)}</div>`;
      } else {
        list.innerHTML = items
          .map((item, idx) => {
            const title =
              (item.item && item.item.provider_service_name) ||
              (item.previous_item && item.previous_item.provider_service_name) ||
              item.external_service_id;
            const fieldPreview =
              item.change_type === "changed"
                ? `<ul class="cat-review-issues">${(item.field_changes || [])
                    .map(
                      (f) =>
                        `<li>${esc(f.label_ar)}: ${esc(f.previous_display ?? "—")} → ${esc(
                          f.current_display ?? "—"
                        )}</li>`
                    )
                    .join("")}</ul>`
                : `<p class="text-xs text-slate-400 mt-1 mb-0">${esc(item.note_ar || "")}</p>`;
            return `<article class="cat-review-card space-y-2">
              <div class="flex flex-wrap justify-between gap-2 items-start">
                <div>
                  <div class="text-xs text-slate-500">${idx + 1 + (provPage - 1) * 50}</div>
                  <h3 class="text-sm font-semibold text-white m-0">${esc(title)}</h3>
                </div>
                <span class="text-xs text-amber-200">${esc(item.change_type_label_ar)}</span>
              </div>
              <div class="flex flex-wrap gap-2 text-[11px] text-slate-400">
                <span>المزود: ${esc(item.provider_slug)}</span>
                <span>الحساب: ${esc(item.provider_account_key)}</span>
                <span dir="ltr">ID: ${esc(item.external_service_id)}</span>
              </div>
              ${fieldPreview}
              <div class="text-[11px] text-slate-300 space-y-2">
                <div>${esc(mappingStatusLabel(item))}</div>
                ${item.mapping_status && item.mapping_status.mapped ? applyStatusBlockHtml(item.mapping_status) : ""}
              </div>
              <div class="flex flex-wrap gap-2 items-center" data-card-actions="${esc(String(idx))}">
                <button type="button" class="text-sky-300 text-xs underline" data-prov-detail="${esc(
                  String(idx)
                )}">فتح التفاصيل</button>
                ${mappingActionsHtml(item, "card")}
              </div>
            </article>`;
          })
          .join("");
        list.querySelectorAll("[data-prov-detail]").forEach((btn) => {
          btn.addEventListener("click", () => {
            const i = Number(btn.getAttribute("data-prov-detail"));
            if (items[i]) openProviderReviewDetails(items[i]);
          });
        });
        list.querySelectorAll("[data-card-actions]").forEach((wrap) => {
          const i = Number(wrap.getAttribute("data-card-actions"));
          if (items[i]) bindMappingActions(wrap, items[i], "card");
        });
      }
      document.getElementById("prov-page-info").textContent =
        `صفحة ${data.current_page}/${data.total_pages} · ${data.total_items}`;
    }

    document.getElementById("review-filter-apply").onclick = () => {
      page = 1;
      load().catch((e) => alertBox(e.message, "err"));
    };
    document.getElementById("review-filter-search").onkeydown = (e) => {
      if (e.key === "Enter") {
        page = 1;
        load().catch((err) => alertBox(err.message, "err"));
      }
    };
    ["review-filter-readiness", "review-filter-status", "review-filter-type", "review-filter-ordering"].forEach(
      (id) => {
        const el = document.getElementById(id);
        if (el) {
          el.onchange = () => {
            page = 1;
            load().catch((e) => alertBox(e.message, "err"));
          };
        }
      }
    );
    document.getElementById("review-prev").onclick = () => {
      if (page > 1) {
        page -= 1;
        load().catch((e) => alertBox(e.message, "err"));
      }
    };
    document.getElementById("review-next").onclick = () => {
      page += 1;
      load().catch((e) => alertBox(e.message, "err"));
    };

    const provApply = document.getElementById("prov-filter-apply");
    if (provApply) {
      provApply.onclick = () => {
        provPage = 1;
        loadProviderSummary()
          .then(loadProviderReview)
          .catch((e) => alertBox(e.message, "err"));
      };
    }
    const provSearch = document.getElementById("prov-filter-search");
    if (provSearch) {
      provSearch.onkeydown = (e) => {
        if (e.key === "Enter") {
          provPage = 1;
          loadProviderReview().catch((err) => alertBox(err.message, "err"));
        }
      };
    }
    ["prov-filter-provider", "prov-filter-account", "prov-filter-change"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) {
        el.onchange = () => {
          if (id === "prov-filter-provider") refreshAccountOptions();
          provPage = 1;
          loadProviderSummary()
            .then(loadProviderReview)
            .catch((e) => alertBox(e.message, "err"));
        };
      }
    });
    const provPrev = document.getElementById("prov-prev");
    const provNext = document.getElementById("prov-next");
    if (provPrev) {
      provPrev.onclick = () => {
        if (provPage > 1) {
          provPage -= 1;
          loadProviderReview().catch((e) => alertBox(e.message, "err"));
        }
      };
    }
    if (provNext) {
      provNext.onclick = () => {
        provPage += 1;
        loadProviderReview().catch((e) => alertBox(e.message, "err"));
      };
    }

    const hash = (window.location.hash || "").replace("#", "");
    Promise.all([loadOptions(), loadSummary(), load()])
      .then(() => {
        if (hash === "provider") switchTab("provider");
      })
      .catch((e) => alertBox(e.message, "err"));
  }

  window.CatalogCoreUI = { mountStructure, mountServices, mountReview };
})();
