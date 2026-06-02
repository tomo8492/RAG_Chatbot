/* ============================================================
   社内文書アシスタント — フロントエンド
   ============================================================ */
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
};

const State = {
  config: null,
  conversations: [],
  current: null,        // 現在の会話(effective 含む)
  models: [],
  indexes: [],
  defaults: {},
  pendingAttachments: [],
  streaming: false,
  controller: null,
};

/* ---------------- API ---------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 401) {
    showLogin();
    throw new Error("認証が必要です");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

/* ---------------- Toast ---------------- */
let toastTimer = null;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
}

/* ---------------- テーマ ---------------- */
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  $("hl-light").disabled = theme === "dark";
  $("hl-dark").disabled = theme !== "dark";
  localStorage.setItem("theme", theme);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme");
  applyTheme(cur === "dark" ? "light" : "dark");
}

/* ============================================================
   起動
   ============================================================ */
async function init() {
  applyTheme(localStorage.getItem("theme") || "light");
  bindGlobalEvents();
  try {
    State.config = await api("/api/config");
  } catch (e) {
    State.config = { auth_enabled: true, authenticated: false };
  }
  $("brand-title").textContent = State.config.app_title || "アシスタント";
  $("login-title").textContent = State.config.app_title || "アシスタント";
  document.title = State.config.app_title || "アシスタント";

  if (State.config.auth_enabled && !State.config.authenticated) {
    showLogin();
  } else {
    await boot();
  }
}

function showLogin() {
  $("app").classList.add("hidden");
  $("login-overlay").classList.remove("hidden");
  $("login-password").focus();
}

async function doLogin() {
  const pw = $("login-password").value;
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ password: pw }) });
    $("login-overlay").classList.add("hidden");
    $("login-error").classList.add("hidden");
    await boot();
  } catch (e) {
    const err = $("login-error");
    err.textContent = e.message || "ログインに失敗しました";
    err.classList.remove("hidden");
  }
}

async function boot() {
  $("app").classList.remove("hidden");
  if (State.config.auth_enabled) $("logout-btn").classList.remove("hidden");
  await Promise.all([loadModels(), loadDefaults(), loadIndexes()]);
  await loadConversations();
  if (State.conversations.length) {
    await selectConversation(State.conversations[0].id);
  } else {
    await newConversation();
  }
}

/* ============================================================
   モデル / 既定設定 / インデックス
   ============================================================ */
async function loadModels() {
  try {
    const data = await api("/api/models");
    State.models = data.models || [];
  } catch (_) { State.models = []; }
  fillModelSelect($("q-model"));
  fillModelSelect($("set-model"));
  if (!State.models.length) {
    const o = el("option", null, "(モデルなし — ollama pull が必要)");
    o.value = "";
    $("q-model").appendChild(o.cloneNode(true));
    $("set-model").appendChild(o);
  }
}
function fillModelSelect(sel) {
  sel.innerHTML = "";
  State.models.forEach((m) => {
    const o = el("option", null, m.name);
    o.value = m.name;
    sel.appendChild(o);
  });
}

async function loadDefaults() {
  try { State.defaults = await api("/api/settings"); } catch (_) { State.defaults = {}; }
}

async function loadIndexes() {
  try { State.indexes = await api("/api/indexes"); } catch (_) { State.indexes = []; }
}

/* ============================================================
   会話一覧
   ============================================================ */
async function loadConversations() {
  State.conversations = await api("/api/conversations");
  renderConversationList();
}

function renderConversationList() {
  const list = $("conv-list");
  list.innerHTML = "";
  State.conversations.forEach((c) => {
    const item = el("div", "conv-item" + (State.current && c.id === State.current.id ? " active" : ""));
    item.appendChild(el("span", "title", escapeHtml(c.title || "新しい会話")));
    const del = el("span", "del", "🗑");
    del.title = "削除";
    del.onclick = (ev) => { ev.stopPropagation(); deleteConversation(c.id); };
    item.appendChild(del);
    item.onclick = () => selectConversation(c.id);
    list.appendChild(item);
  });
}

async function newConversation() {
  const conv = await api("/api/conversations", { method: "POST", body: JSON.stringify({}) });
  State.conversations.unshift(conv);
  await selectConversation(conv.id);
  renderConversationList();
}

async function selectConversation(cid) {
  if (State.streaming) stopGeneration();
  const conv = await api(`/api/conversations/${cid}`);
  State.current = conv;
  State.pendingAttachments = [];
  renderAttachChips();
  renderConversationList();
  $("chat-title").value = conv.title || "新しい会話";
  syncQuickControls();
  updateHeaderBadges();
  renderMessages(conv.messages || []);
  closeSidebarMobile();
}

async function deleteConversation(cid) {
  if (!confirm("この会話を削除しますか?")) return;
  await api(`/api/conversations/${cid}`, { method: "DELETE" });
  State.conversations = State.conversations.filter((c) => c.id !== cid);
  if (State.current && State.current.id === cid) {
    if (State.conversations.length) await selectConversation(State.conversations[0].id);
    else await newConversation();
  }
  renderConversationList();
}

async function renameConversation() {
  if (!State.current) return;
  const title = $("chat-title").value.trim() || "新しい会話";
  await api(`/api/conversations/${State.current.id}`, {
    method: "PATCH", body: JSON.stringify({ title }),
  });
  const c = State.conversations.find((x) => x.id === State.current.id);
  if (c) c.title = title;
  renderConversationList();
}

/* ============================================================
   クイック設定(チャット欄)
   ============================================================ */
function syncQuickControls() {
  const eff = State.current.effective || {};
  setSelect($("q-model"), eff.model);
  setSelect($("q-effort"), eff.effort);
  setSelect($("q-length"), String(eff.num_predict));
  $("q-topk").value = eff.top_k;
  $("q-topk-val").textContent = eff.top_k;
}
function setSelect(sel, val) {
  if (val == null) return;
  const has = Array.from(sel.options).some((o) => o.value === String(val));
  if (!has && String(val)) {
    const o = el("option", null, String(val)); o.value = String(val); sel.appendChild(o);
  }
  sel.value = String(val);
}

let quickTimer = null;
async function pushQuick(patch) {
  if (!State.current) return;
  clearTimeout(quickTimer);
  quickTimer = setTimeout(async () => {
    const body = {};
    if (patch.model !== undefined) body.model = patch.model;
    const settings = {};
    ["effort", "num_predict", "top_k"].forEach((k) => {
      if (patch[k] !== undefined) settings[k] = patch[k];
    });
    if (Object.keys(settings).length) body.settings = settings;
    const conv = await api(`/api/conversations/${State.current.id}`, {
      method: "PATCH", body: JSON.stringify(body),
    });
    State.current = conv;
    updateHeaderBadges();
  }, 250);
}

function updateHeaderBadges() {
  const eff = (State.current && State.current.effective) || {};
  $("model-indicator").textContent = eff.model || "モデル未設定";
  const active = (State.current && State.current.active_indexes) || [];
  const ind = $("kb-indicator");
  if (active.length) {
    ind.textContent = `📁 資料 ${active.length}`;
    ind.classList.remove("hidden");
  } else {
    ind.classList.add("hidden");
  }
}

/* ============================================================
   メッセージ描画
   ============================================================ */
function renderMessages(messages) {
  const box = $("messages");
  box.innerHTML = "";
  if (!messages.length) {
    box.appendChild(buildWelcome());
    return;
  }
  messages.forEach((m, i) => {
    const isLastAssistant = m.role === "assistant" && i === messages.length - 1;
    box.appendChild(renderMessage(m, isLastAssistant));
  });
  scrollToBottom();
}

function buildWelcome() {
  const w = el("div", "welcome");
  w.innerHTML = `<div class="brand-big"><span class="dot">✻</span></div>
    <h2>こんにちは</h2>
    <p class="muted">参照資料フォルダを選んで質問するか、そのまま会話を始められます。<br/>
    ファイルを添付して内容について質問することもできます。</p>`;
  return w;
}

function renderMessage(m, isLastAssistant) {
  if (m.role === "user") {
    const row = el("div", "msg-row user");
    const bubble = el("div", "bubble");
    bubble.textContent = m.content;
    if (m.attachments && m.attachments.length) {
      const att = el("div", "attach-chips");
      m.attachments.forEach((a) => att.appendChild(el("span", "chip", "📎 " + escapeHtml(a))));
      bubble.appendChild(att);
    }
    row.appendChild(bubble);
    return row;
  }
  // assistant
  const { row, refs } = createAssistantRow();
  renderMarkdown(refs.md, m.content, true);
  refs.row.dataset.raw = m.content;
  if (m.sources && m.sources.length) renderSources(refs.src, m.sources);
  buildAssistantActions(refs, isLastAssistant);
  return row;
}

function createAssistantRow() {
  const row = el("div", "msg-row assistant");
  row.innerHTML = `
    <div class="avatar">✻</div>
    <div class="msg-body">
      <details class="thinking hidden"><summary>💭 思考過程</summary><div class="think-text"></div></details>
      <div class="md"></div>
      <div class="sources"></div>
      <div class="msg-actions"></div>
    </div>`;
  const refs = {
    row,
    think: row.querySelector(".thinking"),
    thinkText: row.querySelector(".think-text"),
    md: row.querySelector(".md"),
    src: row.querySelector(".sources"),
    actions: row.querySelector(".msg-actions"),
  };
  return { row, refs };
}

function buildAssistantActions(refs, isLast) {
  refs.actions.innerHTML = "";
  const copy = el("button", null, "📋 コピー");
  copy.onclick = () => {
    navigator.clipboard.writeText(refs.row.dataset.raw || "").then(() => toast("コピーしました"));
  };
  refs.actions.appendChild(copy);
  if (isLast) {
    const regen = el("button", null, "↻ 再生成");
    regen.onclick = () => regenerate();
    refs.actions.appendChild(regen);
  }
}

function renderSources(container, sources) {
  container.innerHTML = "";
  const title = el("span", "src-title", "📎 参照: ");
  container.appendChild(title);
  sources.forEach((s) => {
    const label = `${s.source}${s.loc ? " " + s.loc : ""}${s.attachment ? " (添付)" : ""}`;
    container.appendChild(el("span", "src-item", escapeHtml(label)));
  });
}

/* ---------- Markdown ---------- */
marked.setOptions({ breaks: true, gfm: true });
function renderMarkdown(target, text, final) {
  const html = DOMPurify.sanitize(marked.parse(text || ""));
  target.innerHTML = html;
  if (final) enhanceCode(target);
}
function enhanceCode(container) {
  container.querySelectorAll("pre code").forEach((code) => {
    try { hljs.highlightElement(code); } catch (_) {}
    const pre = code.parentElement;
    if (pre.querySelector(".code-head")) return;
    let lang = "code";
    code.classList.forEach((c) => { if (c.startsWith("language-")) lang = c.slice(9); });
    const head = el("div", "code-head");
    head.appendChild(el("span", null, lang));
    const btn = el("button", "code-copy", "コピー");
    btn.onclick = () => navigator.clipboard.writeText(code.textContent).then(() => toast("コピーしました"));
    head.appendChild(btn);
    pre.insertBefore(head, code);
  });
}

function scrollToBottom() {
  const box = $("messages");
  box.scrollTop = box.scrollHeight;
}

/* ============================================================
   送信 / ストリーミング
   ============================================================ */
async function send() {
  if (State.streaming || !State.current) return;
  const text = $("input").value.trim();
  if (!text) return;
  $("input").value = "";
  autoResize();

  const attachments = State.pendingAttachments.slice();
  State.pendingAttachments = [];
  renderAttachChips();

  // welcome 除去
  const welcome = $("messages").querySelector(".welcome");
  if (welcome) welcome.remove();

  // ユーザー行(楽観的)
  const urow = el("div", "msg-row user");
  const bubble = el("div", "bubble");
  bubble.textContent = text;
  if (attachments.length) {
    const att = el("div", "attach-chips");
    attachments.forEach((a) => att.appendChild(el("span", "chip", "📎 " + escapeHtml(a))));
    bubble.appendChild(att);
  }
  urow.appendChild(bubble);
  $("messages").appendChild(urow);

  await streamAssistant({ content: text, attachments, mode: "send" });
  await loadConversations(); // タイトル更新反映
}

async function regenerate() {
  if (State.streaming || !State.current) return;
  // 末尾の assistant 行を削除
  const rows = $("messages").querySelectorAll(".msg-row.assistant");
  if (rows.length) rows[rows.length - 1].remove();
  await streamAssistant({ mode: "regenerate" });
}

async function streamAssistant(payload) {
  const { row, refs } = createAssistantRow();
  $("messages").appendChild(row);
  refs.md.classList.add("cursor-blink");
  scrollToBottom();

  setStreaming(true);
  State.controller = new AbortController();

  let acc = "", think = "", renderScheduled = false, gotContent = false, finished = false;
  const scheduleRender = () => {
    if (renderScheduled) return;
    renderScheduled = true;
    requestAnimationFrame(() => {
      renderScheduled = false;
      if (finished) return;
      renderMarkdown(refs.md, acc, false);
      scrollToBottom();
    });
  };

  try {
    const res = await fetch(`/api/conversations/${State.current.id}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: State.controller.signal,
    });
    if (res.status === 401) { showLogin(); throw new Error("認証が必要です"); }
    if (!res.ok) {
      let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) {}
      throw new Error(d);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
        const line = chunk.replace(/^data: /, "");
        if (!line) continue;
        let ev; try { ev = JSON.parse(line); } catch (_) { continue; }
        handleStreamEvent(ev, refs, {
          onThink: (d) => { think += d; refs.think.classList.remove("hidden");
            refs.think.open = true; refs.thinkText.textContent = think; scrollToBottom(); },
          onContent: (d) => {
            if (!gotContent) { gotContent = true; refs.think.open = false; }
            acc += d; scheduleRender();
          },
          getAcc: () => acc,
        });
      }
    }
    // 正常終了
    renderMarkdown(refs.md, acc || "*(応答なし)*", true);
  } catch (e) {
    if (e.name === "AbortError") {
      renderMarkdown(refs.md, acc || "*(停止しました)*", true);  // 部分内容は保持
    } else {
      renderMarkdown(refs.md, (acc ? acc + "\n\n" : "") + `⚠️ **エラー:** ${escapeHtml(e.message)}`, true);
      toast("エラー: " + e.message);
    }
  } finally {
    finished = true;
    refs.md.classList.remove("cursor-blink");
    refs.row.dataset.raw = acc;
    buildAssistantActions(refs, true);
    setStreaming(false);
    State.controller = null;
  }
}

function handleStreamEvent(ev, refs, cb) {
  switch (ev.type) {
    case "thinking": cb.onThink(ev.delta); break;
    case "content": cb.onContent(ev.delta); break;
    case "sources": if (ev.sources && ev.sources.length) renderSources(refs.src, ev.sources); break;
    case "done": if (ev.message && ev.message.sources && ev.message.sources.length)
      renderSources(refs.src, ev.message.sources); break;
    case "error": throw new Error(ev.error || "生成エラー");
    case "user_saved": break;
  }
}

function setStreaming(on) {
  State.streaming = on;
  $("send-btn").classList.toggle("hidden", on);
  $("stop-btn").classList.toggle("hidden", !on);
}
function stopGeneration() {
  if (State.controller) State.controller.abort();
}

/* ============================================================
   添付ファイル
   ============================================================ */
async function handleFiles(files) {
  if (!State.current) return;
  for (const file of files) {
    toast(`「${file.name}」を取り込み中…`);
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await fetch(`/api/conversations/${State.current.id}/attachments`, {
        method: "POST", body: fd,
      });
      if (!res.ok) { let d; try { d = (await res.json()).detail; } catch (_) {} throw new Error(d || "失敗"); }
      const data = await res.json();
      State.pendingAttachments.push(data.name);
      renderAttachChips();
      toast(`「${data.name}」を取り込みました(${data.chunks}チャンク)`);
    } catch (e) {
      toast(`添付失敗: ${e.message}`);
    }
  }
  $("file-input").value = "";
}

function renderAttachChips() {
  const box = $("attach-chips");
  box.innerHTML = "";
  State.pendingAttachments.forEach((name, i) => {
    const chip = el("span", "chip", "📎 " + escapeHtml(name));
    const x = el("span", "x", "✕");
    x.onclick = () => { State.pendingAttachments.splice(i, 1); renderAttachChips(); };
    chip.appendChild(x);
    box.appendChild(chip);
  });
}

/* ============================================================
   設定モーダル
   ============================================================ */
function openSettings() {
  const d = State.defaults;
  setSelect($("set-model"), d.model);
  $("set-system").value = d.system_prompt || "";
  setSelect($("set-effort"), d.effort);
  bindRange("set-numpredict", "set-numpredict-val", d.num_predict);
  bindRange("set-temp", "set-temp-val", d.temperature);
  bindRange("set-topp", "set-topp-val", d.top_p);
  bindRange("set-topk", "set-topk-val", d.top_k);
  bindRange("set-numctx", "set-numctx-val", d.num_ctx);
  bindRange("set-chunk", "set-chunk-val", d.chunk_size);
  bindRange("set-overlap", "set-overlap-val", d.chunk_overlap);
  $("set-embed-info").textContent =
    `${State.config.embed_backend} / ${State.config.embed_model}`;
  $("settings-modal").classList.remove("hidden");
}
function bindRange(id, valId, value) {
  const r = $(id); r.value = value;
  const v = $(valId); if (v) v.textContent = value;
  r.oninput = () => { if (v) v.textContent = r.value; };
}
async function saveSettings() {
  const patch = {
    model: $("set-model").value,
    system_prompt: $("set-system").value,
    effort: $("set-effort").value,
    num_predict: parseInt($("set-numpredict").value),
    temperature: parseFloat($("set-temp").value),
    top_p: parseFloat($("set-topp").value),
    top_k: parseInt($("set-topk").value),
    num_ctx: parseInt($("set-numctx").value),
    chunk_size: parseInt($("set-chunk").value),
    chunk_overlap: parseInt($("set-overlap").value),
  };
  State.defaults = await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
  $("settings-modal").classList.add("hidden");
  toast("設定を保存しました");
}

/* ============================================================
   参照資料(ナレッジベース)
   ============================================================ */
async function openKb() {
  await loadIndexes();
  renderKbList();
  $("kb-modal").classList.remove("hidden");
}

function renderKbList() {
  const list = $("kb-list");
  list.innerHTML = "";
  if (!State.indexes.length) {
    list.appendChild(el("p", "muted small", "まだ資料がありません。「フォルダを選んで追加」から作成してください。"));
    return;
  }
  const active = (State.current && State.current.active_indexes) || [];
  State.indexes.forEach((idx) => {
    const card = el("div", "kb-card");
    const top = el("div", "kb-top");
    const left = el("div");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = active.includes(idx.id);
    cb.disabled = idx.status !== "ready";
    cb.onchange = () => toggleActiveIndex(idx.id, cb.checked);
    const nameLabel = el("label");
    nameLabel.style.cursor = "pointer";
    nameLabel.appendChild(cb);
    nameLabel.appendChild(el("span", "kb-name", " " + escapeHtml(idx.name)));
    left.appendChild(nameLabel);
    top.appendChild(left);
    const st = el("span", "kb-status " + idx.status,
      idx.status === "ready" ? "準備完了" : idx.status === "building" ? "作成中…" : "エラー");
    top.appendChild(st);
    card.appendChild(top);
    card.appendChild(el("div", "kb-meta",
      `${idx.file_count} ファイル / ${idx.chunk_count} チャンク`));
    card.appendChild(el("div", "kb-paths", escapeHtml((idx.paths || []).join("  ・  "))));
    if (idx.error) card.appendChild(el("div", "kb-status error", escapeHtml(idx.error)));
    const actions = el("div", "kb-actions");
    actions.style.marginTop = "8px";
    const rebuild = el("button", "btn", "↻ 再構築");
    rebuild.onclick = () => rebuildIndex(idx.id);
    const del = el("button", "btn", "🗑 削除");
    del.onclick = () => deleteIndex(idx.id);
    actions.appendChild(rebuild); actions.appendChild(del);
    card.appendChild(actions);
    list.appendChild(card);
  });

  // 作成中があれば自動更新
  if (State.indexes.some((i) => i.status === "building")) {
    setTimeout(async () => { await loadIndexes(); if (!$("kb-modal").classList.contains("hidden")) renderKbList(); }, 1500);
  }
}

async function toggleActiveIndex(iid, on) {
  if (!State.current) return;
  let active = (State.current.active_indexes || []).slice();
  if (on && !active.includes(iid)) active.push(iid);
  if (!on) active = active.filter((x) => x !== iid);
  State.current = await api(`/api/conversations/${State.current.id}`, {
    method: "PATCH", body: JSON.stringify({ active_indexes: active }),
  });
  updateHeaderBadges();
}

async function rebuildIndex(iid) {
  await api(`/api/indexes/${iid}/rebuild`, { method: "POST" });
  toast("再構築を開始しました");
  await loadIndexes(); renderKbList();
}
async function deleteIndex(iid) {
  if (!confirm("この資料インデックスを削除しますか?")) return;
  await api(`/api/indexes/${iid}`, { method: "DELETE" });
  await loadIndexes(); renderKbList();
}

/* ============================================================
   フォルダブラウザ
   ============================================================ */
const FB = { path: null, selected: null };

async function openFolderBrowser() {
  $("folder-modal").classList.remove("hidden");
  try {
    const { roots } = await api("/api/fs/roots");
    const rootBox = $("fb-roots"); rootBox.innerHTML = "";
    roots.forEach((r) => {
      const b = el("button", null, r.name);
      b.onclick = () => fbNavigate(r.path);
      rootBox.appendChild(b);
    });
    await fbNavigate(roots[0] ? roots[0].path : null);
  } catch (e) { toast("フォルダ取得失敗: " + e.message); }
}

async function fbNavigate(path) {
  try {
    const data = await api("/api/fs?path=" + encodeURIComponent(path || ""));
    FB.path = data.path;
    FB.selected = data.path;
    $("fb-path").textContent = data.path;
    $("fb-current").textContent = data.path;
    $("fb-pick").disabled = false;

    const list = $("fb-list"); list.innerHTML = "";
    if (data.parent) {
      const up = el("div", "fb-entry", "📁 .. (上の階層)");
      up.onclick = () => fbNavigate(data.parent);
      list.appendChild(up);
    }
    data.dirs.forEach((d) => {
      const row = el("div", "fb-entry", "📁 " + escapeHtml(d.name));
      row.onclick = () => fbNavigate(d.path);
      list.appendChild(row);
    });
    data.files.forEach((f) => {
      list.appendChild(el("div", "fb-entry file", "📄 " + escapeHtml(f.name)));
    });
    if (!data.dirs.length && !data.files.length) {
      list.appendChild(el("div", "fb-entry file muted", "(空のフォルダ)"));
    }
    // 再帰件数の見積り
    $("fb-count").textContent = "…";
    api("/api/fs/estimate", { method: "POST", body: JSON.stringify({ paths: [data.path] }) })
      .then((r) => { $("fb-count").textContent = `対応ファイル ${r.count} 件`; })
      .catch(() => { $("fb-count").textContent = ""; });
  } catch (e) { toast(e.message); }
}

async function pickFolder() {
  if (!FB.selected) return;
  $("folder-modal").classList.add("hidden");
  try {
    const idx = await api("/api/indexes", {
      method: "POST", body: JSON.stringify({ paths: [FB.selected] }),
    });
    toast(`「${idx.name}」のインデックス作成を開始しました`);
    await loadIndexes(); renderKbList();
  } catch (e) { toast("作成失敗: " + e.message); }
}

/* ============================================================
   入力欄の挙動
   ============================================================ */
function autoResize() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
}

function closeSidebarMobile() { $("sidebar").classList.remove("open"); }

/* ============================================================
   イベント束ね
   ============================================================ */
function bindGlobalEvents() {
  $("login-btn").onclick = doLogin;
  $("login-password").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });

  $("theme-toggle").onclick = toggleTheme;
  $("new-chat").onclick = newConversation;
  $("logout-btn").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };
  $("toggle-sidebar").onclick = () => $("sidebar").classList.toggle("open");

  $("chat-title").addEventListener("change", renameConversation);
  $("chat-title").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("chat-title").blur(); } });

  $("send-btn").onclick = send;
  $("stop-btn").onclick = stopGeneration;
  const input = $("input");
  input.addEventListener("input", autoResize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });

  $("attach-btn").onclick = () => $("file-input").click();
  $("file-input").addEventListener("change", (e) => handleFiles(e.target.files));

  // ドラッグ&ドロップ
  const main = $("main");
  ["dragover", "drop"].forEach((evt) => main.addEventListener(evt, (e) => e.preventDefault()));
  main.addEventListener("drop", (e) => { if (e.dataTransfer.files.length) handleFiles(e.dataTransfer.files); });

  // クイック設定
  $("q-model").onchange = (e) => pushQuick({ model: e.target.value });
  $("q-effort").onchange = (e) => pushQuick({ effort: e.target.value });
  $("q-length").onchange = (e) => pushQuick({ num_predict: parseInt(e.target.value) });
  $("q-topk").oninput = (e) => { $("q-topk-val").textContent = e.target.value; };
  $("q-topk").onchange = (e) => pushQuick({ top_k: parseInt(e.target.value) });

  // 設定モーダル
  $("open-settings").onclick = openSettings;
  $("save-settings").onclick = saveSettings;
  // KBモーダル
  $("open-kb").onclick = openKb;
  $("add-kb").onclick = openFolderBrowser;
  // フォルダ
  $("fb-pick").onclick = pickFolder;

  // モーダル閉じる
  document.querySelectorAll(".close-modal").forEach((b) =>
    b.onclick = () => b.closest(".overlay").classList.add("hidden"));
  document.querySelectorAll(".close-folder").forEach((b) =>
    b.onclick = () => $("folder-modal").classList.add("hidden"));
  document.querySelectorAll(".overlay").forEach((ov) =>
    ov.addEventListener("click", (e) => { if (e.target === ov && ov.id !== "login-overlay") ov.classList.add("hidden"); }));
}

/* ---------------- util ---------------- */
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

window.addEventListener("DOMContentLoaded", init);
