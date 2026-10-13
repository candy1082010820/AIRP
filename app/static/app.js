"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const labels = {
    world: "世界設定",
    character: "角色檔案",
    lore: "背景知識",
    scenario: "情境劇本",
    rule: "扮演規則",
  };
  const icons = { world: "◎", character: "♙", lore: "▤", scenario: "◇", rule: "☷" };
  const state = {
    kind: "", offset: 0, limit: 24, total: 0, entries: [], worlds: [],
    current: null, listController: null, key: "", saving: false, context: "",
  };
  try { state.key = sessionStorage.getItem("airp-admin-key") || ""; } catch (_) { /* Private browsers may disable storage. */ }
  let toastTimer;
  let searchTimer;

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  function errorMessage(error) {
    if (error instanceof ApiError) return error.message;
    return "暫時無法連線到資料庫。請確認服務已啟動，然後重新讀取。";
  }

  function detailMessage(detail, status) {
    if (status === 401 || status === 403) return "管理金鑰未填寫或無效。請在「金鑰設定」填入有效的 Admin API key。";
    if (status === 404) return "這筆資料已不存在，可能已在其他分頁刪除。請關閉視窗並重新讀取。";
    if (status === 409) {
      if (typeof detail === "string") return detail + (detail.includes("更新") ? "。請保留你的修改，重新讀取最新資料後再操作。" : "");
      return "資料版本或 ID 發生衝突。可能已由其他分頁更新，或匯入檔含有現存 ID。請保留你的修改，重新讀取最新資料後再操作。";
    }
    if (status === 413) return "資料或備份檔超過伺服器允許的大小，請縮小內容後重試。";
    if (status === 422) {
      if (Array.isArray(detail)) {
        return "欄位格式有誤：" + detail.map((item) => {
          const names = { title: "標題", content: "完整設定", tags: "標籤", summary: "摘要", world_id: "所屬世界", revision: "版本", expected_revision: "目前版本" };
          const path = (item.loc || []).filter((part) => part !== "body").map((part) => names[part] || String(part)).join(" / ");
          return `${path || "資料"} — ${item.msg || "請檢查輸入"}`;
        }).join("；");
      }
      return typeof detail === "string" ? detail : "欄位格式有誤，請檢查輸入的資料。";
    }
    return typeof detail === "string" ? detail : `操作失敗（HTTP ${status}），請稍後再試。`;
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.key) headers.set("Authorization", `Bearer ${state.key}`);
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    const response = await fetch(path, { ...options, headers });
    if (response.status === 204) return null;
    let data;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) throw new ApiError(detailMessage(data?.detail, response.status), response.status);
    if (data === null) throw new ApiError("伺服器傳回的資料格式不正確。請重新讀取。", response.status);
    return data;
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function showError(target, message) {
    target.textContent = message;
    target.hidden = !message;
  }

  function toast(message) {
    clearTimeout(toastTimer);
    const openDialogs = document.querySelectorAll("dialog[open]");
    const container = openDialogs.length ? openDialogs[openDialogs.length - 1] : document.body;
    container.append($("toast"));
    $("toast").textContent = message;
    $("toast").hidden = false;
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500);
  }

  function connection(online) {
    $("connection-dot").className = `connection-dot ${online ? "online" : "error"}`;
    $("connection-status").textContent = online ? "資料庫已連線" : "連線待確認";
  }

  function date(value, withTime = false) {
    if (!value) return "—";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.valueOf())) return "—";
    return new Intl.DateTimeFormat("zh-TW", {
      year: "numeric", month: "2-digit", day: "2-digit",
      ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
    }).format(parsed);
  }

  function worldName(id) {
    if (!id) return "獨立設定";
    return state.worlds.find((world) => world.id === id)?.title || "未載入的世界";
  }

  function populateWorldSelect(select, firstText, selected = select.value) {
    select.replaceChildren();
    const empty = element("option", "", firstText);
    empty.value = "";
    select.append(empty);
    for (const world of state.worlds) {
      const option = element("option", "", world.title + (world.status === "draft" ? "（草稿）" : ""));
      option.value = world.id;
      select.append(option);
    }
    if (selected && !state.worlds.some((world) => world.id === selected)) {
      const option = element("option", "", "尚未載入的世界");
      option.value = selected;
      select.append(option);
    }
    select.value = selected || "";
  }

  async function loadWorlds() {
    const worlds = [];
    let offset = 0;
    let total;
    do {
      const result = await api(`/api/entries?kind=world&limit=100&offset=${offset}`);
      worlds.push(...result.items);
      total = result.total;
      offset += result.items.length;
      if (!result.items.length) break;
    } while (offset < total);
    state.worlds = worlds;
    populateWorldSelect($("world-filter"), "所有世界");
    populateWorldSelect($("entry-world"), "未指定世界");
    populateWorldSelect($("context-world"), "所有世界");
    if (state.entries.length) renderEntries();
  }

  function renderEntries() {
    const grid = $("entry-grid");
    grid.replaceChildren();
    for (const entry of state.entries) {
      const card = element("button", "entry-card");
      card.type = "button";
      card.setAttribute("aria-label", `編輯${labels[entry.kind] || "資料"}：${entry.title}`);
      const top = element("div", "entry-card-top");
      const badge = element("span", "kind-badge");
      badge.dataset.kind = entry.kind;
      const icon = element("span", "", icons[entry.kind] || "▤");
      icon.setAttribute("aria-hidden", "true");
      badge.append(icon, document.createTextNode(labels[entry.kind] || entry.kind));
      top.append(badge, element("span", `status-badge ${entry.status === "draft" ? "draft" : ""}`, entry.status === "published" ? "已發佈" : "草稿"));
      card.append(top, element("h3", "", entry.title));
      card.append(element("p", "entry-card-summary", entry.summary || "尚未填寫摘要。點選查看完整設定。"));
      const tags = element("div", "tags");
      for (const tag of (entry.tags || []).slice(0, 3)) {
        const tagNode = element("span", "tag", tag);
        tagNode.title = tag;
        tags.append(tagNode);
      }
      if ((entry.tags || []).length > 3) tags.append(element("span", "tag", `+${entry.tags.length - 3}`));
      const bottom = element("div", "entry-card-bottom");
      bottom.append(element("span", "", entry.kind === "world" ? `更新於 ${date(entry.updated_at)}` : worldName(entry.world_id)), element("span", "entry-version", `v${entry.revision}`));
      card.append(tags, bottom);
      card.addEventListener("click", async () => {
        card.disabled = true;
        card.setAttribute("aria-busy", "true");
        try {
          const fullEntry = await api(`/api/entries/${entry.id}`);
          if (!$("editor-dialog").open) openEditor(fullEntry);
        } catch (error) { toast(errorMessage(error)); }
        finally { card.disabled = false; card.removeAttribute("aria-busy"); }
      });
      grid.append(card);
    }
    const hasFilters = Boolean(state.kind || $("search").value.trim() || $("world-filter").value || $("status-filter").value);
    $("empty-state").hidden = Boolean(state.entries.length);
    $("empty-title").textContent = hasFilters ? "這裡還沒有符合條件的資料" : "第一個故事，從這裡開始";
    $("empty-description").textContent = hasFilters ? "試試其他關鍵字或篩選條件，也可以新增一筆設定。" : "先建立世界設定，再加入角色、背景與規則。\n儲存為「已發佈」後，GPT 就能讀取。";
    $("empty-new-button").textContent = hasFilters ? "＋ 新增資料" : "＋ 新增第一筆資料";
    $("results-count").textContent = `${state.total} 筆資料`;
    $("pagination").hidden = state.total <= state.limit;
    $("page-info").textContent = `${state.offset + 1}–${Math.min(state.offset + state.entries.length, state.total)} / ${state.total} 筆`;
    $("previous-page").disabled = state.offset === 0;
    $("next-page").disabled = state.offset + state.limit >= state.total;
  }

  async function loadEntries() {
    if (state.listController) state.listController.abort();
    const controller = new AbortController();
    state.listController = controller;
    $("loading").hidden = false;
    $("entry-grid").hidden = true;
    $("empty-state").hidden = true;
    $("pagination").hidden = true;
    $("error-banner").hidden = true;
    const query = new URLSearchParams({ limit: state.limit, offset: state.offset });
    if (state.kind) query.set("kind", state.kind);
    if ($("search").value.trim()) query.set("q", $("search").value.trim());
    if ($("world-filter").value) query.set("world_id", $("world-filter").value);
    if ($("status-filter").value) query.set("status", $("status-filter").value);
    try {
      const result = await api(`/api/entries?${query}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      state.entries = result.items;
      state.total = result.total;
      if (state.offset && !state.entries.length && state.total) {
        state.offset = Math.floor((state.total - 1) / state.limit) * state.limit;
        return loadEntries();
      }
      renderEntries();
      connection(true);
      // The navigation count should describe this result set; avoid implying an unfiltered total.
      $("total-count").textContent = state.kind ? "—" : state.total;
    } catch (error) {
      if (error.name === "AbortError") return;
      $("error-text").textContent = errorMessage(error);
      $("error-banner").hidden = false;
      $("results-count").textContent = "尚未讀取";
      connection(false);
    } finally {
      if (!controller.signal.aborted) {
        $("loading").hidden = true;
        $("entry-grid").hidden = false;
      }
    }
  }

  async function refresh() {
    const results = await Promise.allSettled([loadWorlds(), loadEntries()]);
    if (results[0].status === "rejected" && results[1].status === "fulfilled" && state.entries.length) {
      toast("世界選單讀取失敗，請重新讀取後再調整所屬世界。");
    }
  }

  function openEditor(entry = null) {
    state.current = entry;
    $("editor-form").reset();
    showError($("editor-error"), "");
    $("editor-title").textContent = entry ? "編輯資料" : "新增資料";
    $("entry-kind").value = entry?.kind || state.kind || "world";
    $("entry-title").value = entry?.title || "";
    $("entry-summary").value = entry?.summary || "";
    $("entry-content").value = entry?.content || "";
    $("entry-tags").value = (entry?.tags || []).join("、");
    populateWorldSelect($("entry-world"), "未指定世界", entry?.world_id || $("world-filter").value);
    $("editor-form").elements.status.value = entry?.status || "draft";
    $("entry-world").disabled = $("entry-kind").value === "world";
    $("delete-button").hidden = !entry;
    $("history-button").hidden = !entry;
    $("entry-meta").hidden = !entry;
    $("entry-meta").textContent = entry ? `版本 ${entry.revision} · 建立於 ${date(entry.created_at, true)} · 更新於 ${date(entry.updated_at, true)}` : "";
    $("editor-dialog").showModal();
    $("entry-title").focus();
  }

  function setSaving(saving) {
    state.saving = saving;
    $("save-button").disabled = saving;
    $("delete-button").disabled = saving;
    $("history-button").disabled = saving;
    $("save-button").textContent = saving ? "儲存中…" : "儲存資料";
    for (const button of $("editor-dialog").querySelectorAll("[data-close]")) button.disabled = saving;
  }

  async function saveEntry(event) {
    event.preventDefault();
    if (state.saving) return;
    const current = state.current;
    const payload = {
      kind: $("entry-kind").value,
      title: $("entry-title").value.trim(),
      summary: $("entry-summary").value.trim(),
      content: $("entry-content").value.trim(),
      tags: [...new Set($("entry-tags").value.split(/[,，、]/).map((tag) => tag.trim()).filter(Boolean))],
      world_id: $("entry-kind").value === "world" ? null : $("entry-world").value || null,
      status: $("editor-form").elements.status.value,
    };
    if (!payload.title || !payload.content) {
      showError($("editor-error"), "請填寫標題與完整設定，不能只有空白。");
      return;
    }
    if (current) payload.expected_revision = current.revision;
    setSaving(true);
    showError($("editor-error"), "");
    try {
      await api(current ? `/api/entries/${current.id}` : "/api/entries", { method: current ? "PUT" : "POST", body: JSON.stringify(payload) });
      $("editor-dialog").close();
      toast(payload.status === "published" ? "已儲存，GPT 現在可以讀取最新設定。" : "草稿已儲存。");
      await refresh();
    } catch (error) { showError($("editor-error"), errorMessage(error)); }
    finally { setSaving(false); }
  }

  async function deleteEntry() {
    const current = state.current;
    if (!current || state.saving) return;
    if (!confirm(`確定刪除「${current.title}」？資料與版本紀錄都會移除，無法復原。建議先匯出備份。`)) return;
    setSaving(true);
    try {
      await api(`/api/entries/${current.id}?expected_revision=${current.revision}`, { method: "DELETE" });
      $("editor-dialog").close();
      toast("資料已刪除。");
      await refresh();
    } catch (error) { showError($("editor-error"), errorMessage(error)); }
    finally { setSaving(false); }
  }

  async function openHistory() {
    const current = state.current;
    if (!current) return;
    $("history-list").replaceChildren(element("p", "dialog-description", "正在讀取版本紀錄…"));
    showError($("history-error"), "");
    $("history-dialog").showModal();
    try {
      const result = await api(`/api/entries/${current.id}/history`);
      $("history-list").replaceChildren();
      if (!result.items.length) $("history-list").append(element("p", "dialog-description", "尚無版本紀錄。"));
      for (const revision of [...result.items].sort((a, b) => b.revision - a.revision)) {
        const item = element("article", "history-item");
        const heading = element("div", "history-item-heading");
        const meta = element("div");
        meta.append(element("strong", "", `版本 ${revision.revision}`), element("time", "", date(revision.created_at, true)));
        const button = element("button", "button button-quiet", revision.revision === current.revision ? "目前版本" : "還原此版本");
        button.type = "button";
        button.disabled = revision.revision === current.revision;
        button.addEventListener("click", () => restoreRevision(revision, button));
        heading.append(meta, button);
        const snapshot = revision.snapshot;
        item.append(heading, element("h3", "", snapshot.title), element("p", "", snapshot.summary || "無摘要"));
        const details = element("details");
        details.append(element("summary", "", "查看當時的完整設定"), element("div", "history-content", snapshot.content));
        item.append(details);
        $("history-list").append(item);
      }
    } catch (error) {
      $("history-list").replaceChildren();
      showError($("history-error"), errorMessage(error));
    }
  }

  async function restoreRevision(revision, button) {
    const current = state.current;
    if (!current || !confirm(`確定還原「${current.title}」至版本 ${revision.revision}？尚未儲存的編輯會被捨棄，還原結果將建立新版本。`)) return;
    const buttons = $("history-list").querySelectorAll("button");
    buttons.forEach((node) => { node.disabled = true; });
    button.textContent = "正在還原…";
    showError($("history-error"), "");
    try {
      const restored = await api(`/api/entries/${current.id}/restore`, { method: "POST", body: JSON.stringify({ revision: revision.revision, expected_revision: current.revision }) });
      $("history-dialog").close();
      $("editor-dialog").close();
      openEditor(restored);
      toast(`已還原為版本 ${restored.revision}。`);
      await refresh();
    } catch (error) {
      showError($("history-error"), errorMessage(error));
      buttons.forEach((node) => { node.disabled = node.textContent === "目前版本"; });
      button.textContent = "還原此版本";
    }
  }

  async function exportBackup() {
    $("export-button").disabled = true;
    try {
      const backup = await api("/api/export");
      const blob = new Blob([JSON.stringify(backup, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = element("a");
      link.href = url;
      link.download = `airp-backup-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("備份已匯出，包含資料與版本紀錄。");
    } catch (error) { toast(errorMessage(error)); }
    finally { $("export-button").disabled = false; }
  }

  async function importBackup(event) {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    if (file.size > 20 * 1024 * 1024) { toast("備份檔超過 20 MB，請分批匯入。"); return; }
    let backup;
    try { backup = JSON.parse(await file.text()); }
    catch (_) { toast("無法讀取 JSON。請選擇由 AIRP 匯出的備份檔。"); return; }
    if (!backup || backup.format !== "airp-backup-v1" || !Array.isArray(backup.entries) || !Array.isArray(backup.histories)) {
      toast("備份格式不正確，請使用 AIRP 匯出的 airp-backup-v1 檔案。");
      return;
    }
    if (!confirm(`將匯入 ${backup.entries.length} 筆資料及版本紀錄。只允許新增 ID；若任何 ID 已存在，整批匯入會取消，不會覆寫現有資料。繼續匯入？`)) return;
    $("import-button").disabled = true;
    try {
      const result = await api("/api/import", { method: "POST", body: JSON.stringify(backup) });
      toast(`已匯入 ${result.imported} 筆資料。`);
      state.offset = 0;
      await refresh();
    } catch (error) { toast(errorMessage(error)); }
    finally { $("import-button").disabled = false; }
  }

  async function generateContext(event) {
    event.preventDefault();
    const query = new URLSearchParams({ limit: "30" });
    if ($("context-world").value) query.set("world_id", $("context-world").value);
    if ($("context-search").value.trim()) query.set("q", $("context-search").value.trim());
    $("generate-context").disabled = true;
    $("generate-context").textContent = "讀取中…";
    $("copy-context").disabled = true;
    showError($("context-error"), "");
    try {
      const result = await api(`/api/context?${query}`);
      state.context = result.context;
      $("context-output").textContent = result.context || "目前沒有符合條件的已發佈資料。請先發佈設定，或調整篩選條件。";
      $("context-count").textContent = `${result.entries.length} 筆已發佈資料${result.truncated ? " · 內容過長，已截短" : ""} · ${date(result.generated_at, true)}`;
      $("copy-context").disabled = !result.context;
    } catch (error) { showError($("context-error"), errorMessage(error)); }
    finally {
      $("generate-context").disabled = false;
      $("generate-context").textContent = "產生預覽";
    }
  }

  async function copyContext() {
    try {
      await navigator.clipboard.writeText(state.context);
      toast("GPT 讀取內容已複製。");
    } catch (_) {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents($("context-output"));
      selection.removeAllRanges();
      selection.addRange(range);
      toast("內容已選取，請使用 Ctrl+C（Mac：⌘C）複製。");
    }
  }

  for (const button of document.querySelectorAll("[data-close]")) {
    button.addEventListener("click", () => $(button.dataset.close).close());
  }
  $("editor-dialog").addEventListener("cancel", (event) => { if (state.saving) event.preventDefault(); });
  $("kind-nav").addEventListener("click", (event) => {
    const button = event.target.closest("[data-kind]");
    if (!button) return;
    state.kind = button.dataset.kind;
    state.offset = 0;
    for (const item of $("kind-nav").querySelectorAll("button")) {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", String(active));
    }
    $("library-heading").textContent = labels[state.kind] || "所有資料";
    loadEntries();
  });
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.offset = 0; loadEntries(); }, 250);
  });
  for (const id of ["world-filter", "status-filter"]) {
    $(id).addEventListener("change", () => { state.offset = 0; loadEntries(); });
  }
  $("previous-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadEntries(); });
  $("next-page").addEventListener("click", () => { state.offset += state.limit; loadEntries(); });
  $("retry-button").addEventListener("click", refresh);
  $("new-button").addEventListener("click", () => openEditor());
  $("empty-new-button").addEventListener("click", () => openEditor());
  $("entry-kind").addEventListener("change", () => { $("entry-world").disabled = $("entry-kind").value === "world"; });
  $("editor-form").addEventListener("submit", saveEntry);
  $("delete-button").addEventListener("click", deleteEntry);
  $("history-button").addEventListener("click", openHistory);
  $("auth-button").addEventListener("click", () => {
    $("admin-key").value = state.key;
    $("auth-dialog").showModal();
  });
  $("auth-form").addEventListener("submit", (event) => {
    event.preventDefault();
    state.key = $("admin-key").value.trim();
    try {
      if (state.key) sessionStorage.setItem("airp-admin-key", state.key);
      else sessionStorage.removeItem("airp-admin-key");
    } catch (_) { toast("瀏覽器不允許暫存金鑰，本次會在記憶體中使用。"); }
    $("admin-key").value = "";
    $("auth-dialog").close();
    refresh();
  });
  $("export-button").addEventListener("click", exportBackup);
  $("import-button").addEventListener("click", () => $("import-file").click());
  $("import-file").addEventListener("change", importBackup);
  $("context-button").addEventListener("click", () => {
    populateWorldSelect($("context-world"), "所有世界", $("world-filter").value);
    $("context-dialog").showModal();
  });
  $("context-form").addEventListener("submit", generateContext);
  $("copy-context").addEventListener("click", copyContext);
  refresh();
})();
