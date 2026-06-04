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

// ブランドアイコン(チャットバブル)
const LOGO_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';

const State = {
  config: null,
  mode: "chat",         // chat | code(タブ)
  conversations: [],
  current: null,        // 現在の会話(effective 含む)
  models: [],
  indexes: [],
  defaults: {},
  pendingAttachments: [],
  pendingImages: [],
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

/* ---------------- テーマ(Windows/OSの設定に追従) ----------------
   themeMode: "system"(既定=OSに追従) | "light"(固定) | "dark"(固定) */
const THEME_KEY = "themeMode";
function systemTheme() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}
function themePref() {
  const v = localStorage.getItem(THEME_KEY);
  return v === "light" || v === "dark" || v === "system" ? v : "system";
}
function applyTheme(pref) {
  pref = pref === "light" || pref === "dark" || pref === "system" ? pref : "system";
  localStorage.setItem(THEME_KEY, pref);
  const eff = pref === "system" ? systemTheme() : pref;
  document.documentElement.setAttribute("data-theme", eff);
  $("hl-light").disabled = eff === "dark";
  $("hl-dark").disabled = eff !== "dark";
  const btn = $("theme-toggle");
  if (btn) {
    const meta = {
      system: ["🌗", "テーマ: 自動(Windowsの設定に追従)"],
      light: ["☀️", "テーマ: ライト(固定)"],
      dark: ["🌙", "テーマ: ダーク(固定)"],
    }[pref];
    btn.textContent = meta[0];
    btn.title = meta[1] + " ・ クリックで切替";
  }
}
function toggleTheme() {
  const next = { system: "light", light: "dark", dark: "system" }[themePref()];
  applyTheme(next);
  toast(next === "system" ? "テーマ: 自動(Windowsの設定)"
        : next === "light" ? "テーマ: ライト" : "テーマ: ダーク");
}
// OS(Windows)のライト/ダーク変更に追従(自動モードのときのみ)
function watchSystemTheme() {
  if (!window.matchMedia) return;
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const handler = () => { if (themePref() === "system") applyTheme("system"); };
  if (mq.addEventListener) mq.addEventListener("change", handler);
  else if (mq.addListener) mq.addListener(handler); // 旧ブラウザ向け
}

/* ============================================================
   起動
   ============================================================ */
async function init() {
  applyTheme(themePref());      // 既定はOS(Windows)設定に追従
  watchSystemTheme();           // OS側の切替にライブで反応
  document.body.dataset.mode = State.mode;
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
  maybeStartPolling();   // ページ読み込み時に裏要約が進行中なら通知ポーリング再開
  await loadConversations();
  const mine = convsOfMode();
  if (mine.length) {
    await selectConversation(mine[0].id);
  } else {
    await newConversation();
  }
}

/* ============================================================
   モード(Chat / Code)タブ
   ============================================================ */
function convsOfMode() {
  return State.conversations.filter((c) => (c.kind || "chat") === State.mode);
}

async function setMode(mode) {
  if (mode === State.mode || State.streaming) {
    if (State.streaming) toast("生成中は切り替えできません");
    return;
  }
  State.mode = mode;
  document.body.dataset.mode = mode;
  document.querySelectorAll(".mode-tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.mode === mode));
  $("new-chat").textContent = mode === "code" ? "＋ 新しいコード" : "＋ 新しい会話";
  $("input").placeholder = mode === "code"
    ? "依頼を入力…(例: READMEに使い方の章を追記して)"
    : "メッセージを入力…(Shift+Enterで改行)";
  renderConversationList();
  const mine = convsOfMode();
  if (mine.length) await selectConversation(mine[0].id);
  else await newConversation(mode);
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
  convsOfMode().forEach((c) => {
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

async function newConversation(kind) {
  kind = kind || State.mode;
  const conv = await api("/api/conversations", { method: "POST", body: JSON.stringify({ kind }) });
  State.conversations.unshift(conv);
  await selectConversation(conv.id);
  renderConversationList();
}

async function selectConversation(cid) {
  if (State.streaming) stopGeneration();
  const conv = await api(`/api/conversations/${cid}`);
  State.current = conv;
  State.pendingAttachments = [];
  State.pendingImages = [];
  renderAttachChips();
  renderConversationList();
  $("chat-title").value = conv.title || (conv.kind === "code" ? "新しいコード" : "新しい会話");
  syncQuickControls();
  updateHeaderBadges();
  if ((conv.kind || "chat") === "code") updateCodeBar(conv);
  renderMessages(conv.messages || []);
  closeSidebarMobile();
}

function updateCodeBar(conv) {
  const s = conv.settings || {};
  const folder = s.workspace || "";
  const f = $("cb-folder");
  f.textContent = folder || "未設定";
  f.title = folder || "";
  f.classList.toggle("set", !!folder);
  const plan = s.plan_mode !== false;   // 既定ON
  $("cb-plan").checked = plan;
  $("cb-allow").checked = !!s.allow_changes;
  // 計画モード中は「変更を許可」は計画承認が代替するため無効化(視覚的にも)
  $("cb-allow").disabled = plan;
  const allowLabel = $("cb-allow").closest(".cb-toggle");
  if (allowLabel) allowLabel.classList.toggle("disabled", plan);
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
  setTopkSeg(eff.top_k);
}
// 参照件数(top_k)のセグメント(タブ)の選択状態を更新
function setTopkSeg(val) {
  const seg = $("q-topk");
  if (!seg) return;
  let matched = false;
  seg.querySelectorAll(".qseg-btn").forEach((b) => {
    const on = String(b.dataset.v) === String(val);
    if (on) matched = true;
    b.classList.toggle("active", on);
  });
  // プリセット外の値(設定で任意指定)のときは最も近いものを強調
  if (!matched) {
    const btns = Array.from(seg.querySelectorAll(".qseg-btn"));
    let best = btns[0], diff = Infinity;
    btns.forEach((b) => { const d = Math.abs(parseInt(b.dataset.v) - (parseInt(val) || 0));
      if (d < diff) { diff = d; best = b; } });
    if (best) best.classList.add("active");
  }
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
  if (State.mode === "code") {
    w.innerHTML = `<div class="brand-big"><span class="dot">${LOGO_SVG}</span></div>
      <h2>コードエージェント</h2>
      <p class="muted">作業フォルダを選び、依頼を入力してください。<br/>
      <strong>計画モード</strong>では、AIがまず調査して<strong>実行計画</strong>を提示し、<strong>承認</strong>すると実行します。<br/>
      ファイル編集は自動適用、コマンド実行など重要操作は都度確認します。</p>`;
  } else {
    w.innerHTML = `<div class="brand-big"><span class="dot">${LOGO_SVG}</span></div>
      <h2>こんにちは</h2>
      <p class="muted">参照資料フォルダを選んで質問するか、そのまま会話を始められます。<br/>
      ファイルを添付して内容について質問することもできます。</p>`;
  }
  return w;
}

function renderMessage(m, isLastAssistant) {
  if (m.role === "user") {
    const row = el("div", "msg-row user");
    const bubble = el("div", "bubble");
    bubble.textContent = m.content;
    if (m.attachments && m.attachments.length) {
      const att = el("div", "attach-chips");
      m.attachments.forEach((a) => {
        if (a && typeof a === "object" && a.type === "image" && a.file) {
          const im = el("img", "msg-img");
          im.src = "/api/uploads/" + encodeURIComponent(a.file);
          att.appendChild(im);
        } else {
          att.appendChild(el("span", "chip", "📎 " + escapeHtml(a)));
        }
      });
      bubble.appendChild(att);
    }
    row.appendChild(bubble);
    return row;
  }
  // assistant — Codeの会話で保存済みステップ(計画/ツール/差分/TODO)があれば再現
  if (State.current && (State.current.kind || "chat") === "code" &&
      Array.isArray(m.sources) && m.sources.length && m.sources[0] && m.sources[0].type) {
    const { row, logBox } = buildAgentRow();
    renderCodeSteps(logBox, m.sources);
    return row;
  }
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
    <div class="avatar">${LOGO_SVG}</div>
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
  refs.actions.appendChild(makeSaveMenu(() => refs.row.dataset.raw || ""));
  if (isLast && (!State.current || (State.current.kind || "chat") !== "code")) {
    const regen = el("button", null, "↻ 再生成");
    regen.onclick = () => regenerate();
    refs.actions.appendChild(regen);
  }
}

/* ---------- 保存(ファイル出力) ---------- */
const SAVE_FORMATS = [
  ["Markdown (.md)", "md"], ["テキスト (.txt)", "txt"], ["HTML (.html)", "html"],
  ["Word (.docx)", "docx"], ["Excel (.xlsx)", "xlsx"], ["PowerPoint (.pptx)", "pptx"],
];
function currentTitle() { return (State.current && State.current.title) || "回答"; }

function makeSaveMenu(getContent) {
  const wrap = el("span", "save-wrap");
  const btn = el("button", null, "⬇ 保存 ▾");
  const menu = el("div", "save-menu hidden");
  SAVE_FORMATS.forEach(([label, fmt]) => {
    const item = el("div", "save-item", label);
    item.onclick = (e) => {
      e.stopPropagation();
      menu.classList.add("hidden");
      exportContent(getContent(), fmt, null, currentTitle());
    };
    menu.appendChild(item);
  });
  btn.onclick = (e) => {
    e.stopPropagation();
    document.querySelectorAll(".save-menu").forEach((m) => { if (m !== menu) m.classList.add("hidden"); });
    menu.classList.toggle("hidden");
  };
  wrap.appendChild(btn);
  wrap.appendChild(menu);
  return wrap;
}

const LANG_EXT = {
  html: "html", xml: "xml", javascript: "js", js: "js", typescript: "ts", ts: "ts",
  python: "py", py: "py", json: "json", css: "css", bash: "sh", sh: "sh", shell: "sh",
  sql: "sql", java: "java", c: "c", cpp: "cpp", "c++": "cpp", csharp: "cs", cs: "cs",
  go: "go", rust: "rs", php: "php", ruby: "rb", yaml: "yaml", yml: "yaml",
  markdown: "md", md: "md", vb: "bas", vba: "bas", vbnet: "bas", basic: "bas", vbscript: "bas",
};
function extForLang(l) { return LANG_EXT[(l || "").toLowerCase()] || "txt"; }

async function exportContent(content, fmt, ext, title) {
  if (!content || !content.trim()) { toast("内容が空です"); return; }
  try {
    const res = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, format: fmt, ext, title }),
    });
    if (res.status === 401) { showLogin(); throw new Error("認証が必要です"); }
    if (!res.ok) { let d; try { d = (await res.json()).detail; } catch (_) {} throw new Error(d || "変換に失敗"); }
    const blob = await res.blob();
    let fname = "download." + (ext || fmt);
    const xf = res.headers.get("X-Filename");
    if (xf) { try { fname = decodeURIComponent(xf); } catch (_) { fname = xf; } }
    const url = URL.createObjectURL(blob);
    const a = el("a"); a.href = url; a.download = fname;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    toast("保存しました: " + fname);
  } catch (e) { toast("保存失敗: " + e.message); }
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
    const right = el("span");
    right.style.display = "flex"; right.style.gap = "10px";
    const dl = el("button", "code-copy", "⬇ ." + extForLang(lang));
    dl.title = "コードをダウンロード";
    dl.onclick = () => exportContent(code.textContent, "code", extForLang(lang), lang || "code");
    const btn = el("button", "code-copy", "コピー");
    btn.onclick = () => navigator.clipboard.writeText(code.textContent).then(() => toast("コピーしました"));
    right.appendChild(dl); right.appendChild(btn);
    head.appendChild(right);
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
  if (!text && State.pendingImages.length === 0) return;
  $("input").value = "";
  autoResize();

  const attachments = State.pendingAttachments.slice();
  const images = State.pendingImages.map((i) => i.dataUrl);
  const imageThumbs = State.pendingImages.map((i) => i.dataUrl);
  State.pendingAttachments = [];
  State.pendingImages = [];
  renderAttachChips();

  // welcome 除去
  const welcome = $("messages").querySelector(".welcome");
  if (welcome) welcome.remove();

  // ユーザー行(楽観的)
  const urow = el("div", "msg-row user");
  const bubble = el("div", "bubble");
  bubble.textContent = text;
  if (attachments.length || imageThumbs.length) {
    const att = el("div", "attach-chips");
    imageThumbs.forEach((src) => { const im = el("img", "msg-img"); im.src = src; att.appendChild(im); });
    attachments.forEach((a) => att.appendChild(el("span", "chip", "📎 " + escapeHtml(a))));
    bubble.appendChild(att);
  }
  urow.appendChild(bubble);
  $("messages").appendChild(urow);

  await streamAssistant({ content: text, attachments, images, mode: "send" });
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

/* 送信ボタン/Enter のモード振り分け */
function onSend() {
  if (State.mode === "code") sendCode();
  else send();
}

/* ============================================================
   Code: コーディングエージェント
   ============================================================ */
async function sendCode() {
  if (State.streaming || !State.current) return;
  const s = State.current.settings || {};
  if (!s.workspace) { toast("先に「フォルダを選択」で作業フォルダを設定してください"); return; }
  const text = $("input").value.trim();
  if (!text) return;
  $("input").value = ""; autoResize();

  const welcome = $("messages").querySelector(".welcome");
  if (welcome) welcome.remove();

  const urow = el("div", "msg-row user");
  const bubble = el("div", "bubble"); bubble.textContent = text;
  urow.appendChild(bubble);
  $("messages").appendChild(urow);

  await streamAgent({ content: text });
  await loadConversations(); // タイトル更新を反映
}

/* ---- エージェントのステップ描画(ライブ/再表示で共用) ---- */
function buildAgentRow() {
  const row = el("div", "msg-row assistant");
  row.innerHTML = `<div class="avatar">${LOGO_SVG}</div>` +
    `<div class="msg-body"><div class="msg-name">Code エージェント</div><div class="agent-log"></div></div>`;
  return { row, logBox: row.querySelector(".agent-log") };
}
const SMALL_DIFF_LINES = 12;   // この変更行数以下の差分は折りたたまず開いておく
// 思考(thinking)の折りたたみボックス(チャット側の .thinking と同じ見た目)
function buildThinkBox() {
  const det = el("details", "thinking");
  det.innerHTML = '<summary>💭 考え中…</summary><div class="think-text"></div>';
  return { el: det, sum: det.querySelector("summary"), text: det.querySelector(".think-text") };
}
// unified diff から +追加 / −削除 の行数を数える
function diffStat(d) {
  let a = 0, r = 0;
  (d || "").split("\n").forEach((l) => {
    if (l.startsWith("+") && !l.startsWith("+++")) a++;
    else if (l.startsWith("-") && !l.startsWith("---")) r++;
  });
  return { a, r };
}
// Claude 風の折りたたみツールステップ。ヘッダ(ツール名・引数・状態)+ 折りたたみ本文。
// 返り値の fill(status,result,diff,opts) を tool_result 受信時に呼ぶ。
function buildToolStep(name, args) {
  const det = el("details", "step");
  const sum = document.createElement("summary");
  sum.className = "step-head";
  sum.appendChild(el("span", "step-name", escapeHtml(name)));
  sum.appendChild(el("span", "step-arg", escapeHtml(agentArgsSummary(name, args || {}))));
  const stat = el("span", "step-stat running", '<span class="spin"></span>');  // 実行中スピナー
  sum.appendChild(stat);
  det.appendChild(sum);
  const body = el("div", "step-body");
  det.appendChild(body);
  const ICON = { ok: "✓", error: "⚠", blocked: "⛔", rejected: "⊘", redirected: "↩" };
  function fill(status, result, diff, opts) {
    opts = opts || {};
    det._filled = true;
    const s = status || "ok";
    stat.className = "step-stat " + s;
    let label = ICON[s] || "✓";
    if (diff) { const { a, r } = diffStat(diff); if (a || r) label += `  +${a} −${r}`; }
    stat.textContent = label;
    if (result) {
      const rl = el("div", "step-result" + (s !== "ok" ? " " + s : ""));
      rl.textContent = trimResult(result);
      body.appendChild(rl);
    }
    let open = (s === "error" || s === "blocked" || s === "rejected");  // 問題は開いて見せる
    if (diff && !opts.skipDiffRender) {
      const { a, r } = diffStat(diff);
      if (a + r <= SMALL_DIFF_LINES) open = true;   // 小さな差分は畳まず開いておく
      const d = renderDiff(diff); d.classList.add("applied"); body.appendChild(d);
    }
    det.open = open;
  }
  return { el: det, body, fill };
}
// tool_call を伴わない結果(エラー/最大ステップ/停止など)の素朴な通知ボックス
function plainNoticeEl(status, result, diff) {
  const wrap = el("div", "step-notice");
  const div = el("div", "step-result" + (status && status !== "ok" ? " " + status : ""));
  div.textContent = trimResult(result || "");
  wrap.appendChild(div);
  if (diff) { const d = renderDiff(diff); d.classList.add("applied"); wrap.appendChild(d); }
  return wrap;
}
function agentTextEl(text) {
  const d = el("div", "md agent-text");
  renderMarkdown(d, text || "", true);
  return d;
}
function planStaticEl(plan) {
  const card = el("div", "confirm-card plan-card");
  card.appendChild(el("div", "confirm-title", "📋 実行計画(承認済み)"));
  const body = el("div", "plan-body md");
  renderMarkdown(body, plan || "", true);
  card.appendChild(body);
  return card;
}
// TODOパネル。existing を渡すと同じ要素を書き換える(進捗の更新)
function renderTodos(container, todos, existing) {
  const box = existing || el("div", "todo-panel");
  box.innerHTML = "";
  box.appendChild(el("div", "todo-title", "✅ タスク"));
  (todos || []).forEach((t) => {
    const st = t.status || "pending";
    const icon = st === "completed" ? "☑" : st === "in_progress" ? "▣" : "☐";
    box.appendChild(el("div", "todo-item " + st, `${icon} ${escapeHtml(t.content || "")}`));
  });
  if (!existing) container.appendChild(box);
  return box;
}
// 保存済みステップ(message.sources)を静的に再描画。tool_call と直後の
// tool_result を1つの折りたたみステップにまとめる(ライブ表示と同じ見た目)。
function renderCodeSteps(container, steps) {
  let todoEl = null, pend = null;   // pend: 結果待ちステップの fill 関数
  (steps || []).forEach((ev) => {
    switch (ev.type) {
      case "assistant": if (ev.text) container.appendChild(agentTextEl(ev.text)); pend = null; break;
      case "tool_call": { const s = buildToolStep(ev.name, ev.args); container.appendChild(s.el); pend = s.fill; break; }
      case "tool_result":
        if (pend) { pend(ev.status, ev.result, ev.diff); pend = null; }
        else container.appendChild(plainNoticeEl(ev.status, ev.result, ev.diff));
        break;
      case "plan": container.appendChild(planStaticEl(ev.plan)); pend = null; break;
      case "todos": todoEl = renderTodos(container, ev.todos, todoEl); break;
      case "ask": container.appendChild(askStaticEl(ev.question, ev.options, ev.context)); pend = null; break;
    }
  });
}

async function streamAgent(payload) {
  const { row, logBox } = buildAgentRow();
  $("messages").appendChild(row);
  scrollToBottom();

  setStreaming(true);
  State.controller = new AbortController();
  let curText = null;   // 連続する assistant テキストの描画先
  let todoEl = null;    // TODOパネル(更新時は同じ要素を書き換え)
  let curFill = null, curBody = null, hasConfirm = false;  // 結果待ちのツールステップ
  let curThink = null;  // 思考の折りたたみボックス(本文/ツールが始まったら畳む)

  const collapseThink = () => {
    if (curThink) { curThink.el.open = false; curThink.sum.textContent = "💭 思考"; curThink = null; }
  };
  const addThink = (t) => {
    if (!curThink) {
      curThink = buildThinkBox(); curThink._raw = "";
      logBox.appendChild(curThink.el); curThink.el.open = true;
    }
    curThink._raw += t; curThink.text.textContent = curThink._raw; scrollToBottom();
  };
  const addStepCall = (name, args) => {
    curText = null; collapseThink();
    const s = buildToolStep(name, args);
    logBox.appendChild(s.el);
    curFill = s.fill; curBody = s.body; hasConfirm = false;
    scrollToBottom();
  };
  const addStepResult = (status, result, diff) => {
    curText = null; collapseThink();
    if (curFill) {
      // 確認カードが本文にある場合、差分はカード側に表示済みなので二重表示しない
      curFill(status, result, diff, { skipDiffRender: hasConfirm });
      curFill = null; curBody = null; hasConfirm = false;
    } else {
      logBox.appendChild(plainNoticeEl(status, result, diff));
    }
    scrollToBottom();
  };
  const addText = (t) => {
    collapseThink();
    if (!curText) { curText = el("div", "md agent-text"); curText._raw = ""; logBox.appendChild(curText); }
    curText._raw += t;
    renderMarkdown(curText, curText._raw, true);
    scrollToBottom();
  };

  try {
    const res = await fetch(`/api/conversations/${State.current.id}/agent`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload), signal: State.controller.signal,
    });
    if (res.status === 401) { showLogin(); throw new Error("認証が必要です"); }
    if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) {} throw new Error(d); }

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
        switch (ev.type) {
          case "thinking": if (ev.text) addThink(ev.text); break;
          case "assistant_delta": if (ev.text) addText(ev.text); break;
          case "assistant": if (ev.text) addText(ev.text); break;
          case "tool_call": addStepCall(ev.name, ev.args || {}); break;
          case "tool_result": addStepResult(ev.status, ev.result || "", ev.diff); break;
          case "confirm": {
            curText = null;
            const card = buildConfirmCard(ev);
            if (curBody) { curBody.appendChild(card); curBody.parentElement.open = true; hasConfirm = true; }
            else logBox.appendChild(card);
            scrollToBottom(); break;
          }
          case "plan": curText = null; curFill = null; curBody = null; collapseThink(); logBox.appendChild(buildPlanCard(ev)); scrollToBottom(); break;
          case "todos": curText = null; collapseThink(); todoEl = renderTodos(logBox, ev.todos || [], todoEl); scrollToBottom(); break;
          case "ask": curText = null; curFill = null; curBody = null; collapseThink(); logBox.appendChild(buildAskCard(ev)); scrollToBottom(); break;
          case "error": addStepResult("rejected", "⚠ " + (ev.error || "エラー")); toast("エラー: " + (ev.error || "")); break;
          case "max_steps": addStepResult("blocked", "最大ステップ数に達しました。続けるには再度指示してください。"); break;
          case "done": case "user_saved": break;
        }
      }
    }
  } catch (e) {
    rejectOpenConfirms(logBox);   // 停止/切断時はサーバ側の承認待ちを解放する
    if (e.name === "AbortError") addStepResult("rejected", "停止しました");
    else { addStepResult("rejected", "⚠ " + e.message); toast("エラー: " + e.message); }
  } finally {
    finishConfirmCards(logBox);
    setStreaming(false);
    State.controller = null;
  }
}

// 未応答のカードを解決してサーバ側の待機を解放(停止/切断時)
function rejectOpenConfirms(box) {
  box.querySelectorAll(".confirm-card:not([data-resolved])").forEach((card) => {
    const aid = card.dataset.actionId;
    if (!aid) return;
    if (card.dataset.kind === "ask") {
      api("/api/code/answer", { method: "POST", body: JSON.stringify({ action_id: aid, answer: "" }) }).catch(() => {});
    } else {
      api("/api/code/approve", { method: "POST", body: JSON.stringify({ action_id: aid, approved: false }) }).catch(() => {});
    }
  });
}

// ユーザーへの質問カード(選択肢 + 自由記述)
// 選択肢を {label, description, recommended} に正規化(文字列・オブジェクトの両対応)
function optOf(o) {
  if (o && typeof o === "object") {
    return { label: String(o.label || o.text || o.value || "").trim(),
             description: String(o.description || o.desc || "").trim(),
             recommended: !!(o.recommended || o.default) };
  }
  return { label: String(o == null ? "" : o).trim(), description: "", recommended: false };
}
// 選択肢ボタン(見出し + 説明 + 推奨バッジ)。クリックで label を回答として送る。
function askOptEl(o, onPick) {
  const node = el(onPick ? "button" : "div", "ask-opt" + (o.recommended ? " rec" : "") + (onPick ? "" : " static"));
  const main = el("div", "ask-opt-main");
  main.appendChild(el("span", "ask-opt-label", escapeHtml(o.label)));
  if (o.recommended) main.appendChild(el("span", "ask-opt-rec", "推奨"));
  node.appendChild(main);
  if (o.description) node.appendChild(el("div", "ask-opt-desc", escapeHtml(o.description)));
  if (onPick) node.onclick = () => onPick(o.label);
  return node;
}
function buildAskCard(ev) {
  const card = el("div", "confirm-card ask-card");
  card.dataset.actionId = ev.action_id;
  card.dataset.kind = "ask";
  card.appendChild(el("div", "confirm-title", "❓ " + (ev.question || "どう進めますか?")));
  if (ev.context) card.appendChild(el("div", "ask-context", escapeHtml(ev.context)));
  const list = (ev.options || []).map(optOf).filter((o) => o.label);
  if (list.length) {
    const opts = el("div", "ask-options");
    list.forEach((o) => opts.appendChild(askOptEl(o, (label) => submitAnswer(card, ev.action_id, label))));
    card.appendChild(opts);
  }
  const row = el("div", "ask-free");
  const input = el("input", "ask-input"); input.type = "text"; input.placeholder = "その他(自由に入力)…";
  const send = el("button", "btn ask-send", "送信");
  const submitFree = () => { const v = input.value.trim(); if (v) submitAnswer(card, ev.action_id, v); };
  send.onclick = submitFree;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitFree(); } });
  row.appendChild(input); row.appendChild(send);
  card.appendChild(row);
  card.appendChild(el("div", "confirm-status ask-status"));
  return card;
}

async function submitAnswer(card, actionId, answer) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const input = card.querySelector(".ask-input"); if (input) input.disabled = true;
  const status = card.querySelector(".ask-status");
  try {
    await api("/api/code/answer", { method: "POST", body: JSON.stringify({ action_id: actionId, answer }) });
    if (status) { status.textContent = "回答: " + answer; status.className = "confirm-status ask-status ok"; }
    card.dataset.resolved = "1";
  } catch (e) {
    if (status) { status.textContent = "送信失敗: " + e.message; status.className = "confirm-status ask-status no"; }
    card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    if (input) input.disabled = false;
  }
}

// 保存済みステップ再表示用:質問(静的)
function askStaticEl(question, options, context) {
  const card = el("div", "confirm-card ask-card");
  card.appendChild(el("div", "confirm-title", "❓ " + (question || "")));
  if (context) card.appendChild(el("div", "ask-context", escapeHtml(context)));
  const list = (options || []).map(optOf).filter((o) => o.label);
  if (list.length) {
    const opts = el("div", "ask-options");
    list.forEach((o) => opts.appendChild(askOptEl(o, null)));
    card.appendChild(opts);
  }
  return card;
}

function agentArgsSummary(name, args) {
  if (name === "write_file") return `${args.path || ""} (${args.length || 0}字)`;
  if (name === "edit_file") return args.path || "";
  if (name === "run_command" || name === "run_background") return args.command || "";
  if (name === "command_output" || name === "stop_command") return "job " + (args.job_id || "");
  if (name === "read_file" || name === "summarize_path") return args.path || "";
  if (name === "glob" || name === "grep") return args.pattern || "";
  return "";
}

// 実行計画の承認カード(承認すると実行フェーズへ)
function buildPlanCard(ev) {
  const card = el("div", "confirm-card plan-card");
  card.dataset.actionId = ev.action_id;
  card.appendChild(el("div", "confirm-title", "📋 実行計画 — 承認すると実行フェーズに進みます"));
  const body = el("div", "plan-body md");
  renderMarkdown(body, ev.plan || "(計画なし)", true);
  card.appendChild(body);
  const actions = el("div", "confirm-actions");
  const ok = el("button", "btn primary", "承認して実行");
  const no = el("button", "btn", "却下");
  const status = el("span", "confirm-status");
  ok.onclick = () => respondConfirm(card, ev.action_id, true, status, [ok, no]);
  no.onclick = () => respondConfirm(card, ev.action_id, false, status, [ok, no]);
  actions.appendChild(ok); actions.appendChild(no); actions.appendChild(status);
  card.appendChild(actions);
  return card;
}
function trimResult(s) {
  s = String(s == null ? "" : s);
  return s.length > 4000 ? s.slice(0, 4000) + "\n…(省略)" : s;
}

function buildConfirmCard(ev) {
  const card = el("div", "confirm-card");
  card.dataset.actionId = ev.action_id;
  const isCmd = ev.name === "run_command" || ev.name === "run_background";
  const title = ev.name === "run_background" ? "⚠ このコマンドをバックグラウンド実行しますか?"
    : ev.name === "run_command" ? "⚠ このコマンドを実行しますか?"
    : "⚠ このファイル変更を適用しますか?";
  card.appendChild(el("div", "confirm-title", title));
  if (isCmd) {
    card.appendChild(el("div", "confirm-cmd", "$ " + escapeHtml(ev.command || "")));
  } else {
    card.appendChild(el("div", "confirm-meta",
      `${escapeHtml(ev.path || "")} ・ ${ev.exists ? "上書き" : "新規作成"}`));
    card.appendChild(renderDiff(ev.diff || ""));
  }
  const actions = el("div", "confirm-actions");
  const ok = el("button", "btn primary", isCmd ? "実行する" : "適用する");
  const no = el("button", "btn", "拒否");
  const status = el("span", "confirm-status");
  ok.onclick = () => respondConfirm(card, ev.action_id, true, status, [ok, no]);
  no.onclick = () => respondConfirm(card, ev.action_id, false, status, [ok, no]);
  actions.appendChild(ok); actions.appendChild(no); actions.appendChild(status);
  card.appendChild(actions);
  return card;
}

// Claude Code 風の差分表示。unified diff を行番号つき・行ごと色分けで描画する。
function renderDiff(diffText) {
  const wrap = el("div", "cc-diff");
  const body = el("div", "cc-diff-body");
  let oldLn = 0, newLn = 0, adds = 0, dels = 0, firstHunk = true, rows = 0;
  (diffText || "").split("\n").forEach((ln) => {
    if (ln.startsWith("+++") || ln.startsWith("---")) return;     // ファイルヘッダ行は隠す
    const m = ln.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/); // ハンクヘッダ → 行番号を復元
    if (m) {
      oldLn = parseInt(m[1], 10); newLn = parseInt(m[2], 10);
      if (!firstHunk) body.appendChild(el("div", "cc-diff-gap", "⋯"));
      firstHunk = false;
      return;
    }
    if (ln === "" ) return;
    let cls, gutter, mark, text;
    if (ln.startsWith("+")) { cls = "add"; gutter = newLn++; mark = "+"; text = ln.slice(1); adds++; }
    else if (ln.startsWith("-")) { cls = "del"; gutter = oldLn++; mark = "-"; text = ln.slice(1); dels++; }
    else { cls = "ctx"; gutter = newLn++; oldLn++; mark = " "; text = ln.startsWith(" ") ? ln.slice(1) : ln; }
    const row = el("div", "cc-diff-row " + cls);
    row.appendChild(el("span", "cc-ln", String(gutter)));
    row.appendChild(el("span", "cc-mark", mark === " " ? "&nbsp;" : mark));
    const code = el("span", "cc-code");
    code.textContent = text;                                       // textContent で自動エスケープ
    row.appendChild(code);
    body.appendChild(row);
    rows++;
  });
  const head = el("div", "cc-diff-head");
  head.appendChild(el("span", "cc-add-cnt", "+" + adds));
  head.appendChild(el("span", "cc-del-cnt", "−" + dels));
  wrap.appendChild(head);
  if (rows) wrap.appendChild(body);
  else wrap.appendChild(el("div", "cc-diff-empty", "(差分なし)"));
  return wrap;
}

async function respondConfirm(card, actionId, approved, statusEl, btns) {
  btns.forEach((b) => (b.disabled = true));
  try {
    await api("/api/code/approve", {
      method: "POST", body: JSON.stringify({ action_id: actionId, approved }),
    });
    statusEl.textContent = approved ? "承認しました" : "拒否しました";
    statusEl.className = "confirm-status " + (approved ? "ok" : "no");
    card.dataset.resolved = "1";
  } catch (e) {
    statusEl.textContent = "送信失敗: " + e.message;
    statusEl.className = "confirm-status no";
    btns.forEach((b) => (b.disabled = false));
  }
}

function finishConfirmCards(box) {
  box.querySelectorAll(".confirm-card:not([data-resolved])").forEach((card) => {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const st = card.querySelector(".confirm-status");
    if (st && !st.textContent) { st.textContent = "(終了)"; st.className = "confirm-status no"; }
  });
}

/* ============================================================
   添付ファイル
   ============================================================ */
async function handleFiles(files) {
  if (!State.current) return;
  if (State.mode === "code") { toast("コードモードでは添付は使いません(作業フォルダを操作します)"); return; }
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
  State.pendingImages.forEach((img, i) => {
    const chip = el("span", "chip img-chip");
    const im = el("img"); im.src = img.dataUrl; im.alt = img.name || "image";
    chip.appendChild(im);
    const x = el("span", "x", "✕");
    x.onclick = () => { State.pendingImages.splice(i, 1); renderAttachChips(); };
    chip.appendChild(x);
    box.appendChild(chip);
  });
  State.pendingAttachments.forEach((name, i) => {
    const chip = el("span", "chip", "📎 " + escapeHtml(name));
    const x = el("span", "x", "✕");
    x.onclick = () => { State.pendingAttachments.splice(i, 1); renderAttachChips(); };
    chip.appendChild(x);
    box.appendChild(chip);
  });
}

// 画像(スクショ等)を取り込む
function addImageFile(file) {
  if (!file.type || !file.type.startsWith("image/")) return;
  const reader = new FileReader();
  reader.onload = () => {
    State.pendingImages.push({ dataUrl: reader.result, name: file.name || "screenshot.png" });
    renderAttachChips();
    toast("画像を添付しました");
  };
  reader.readAsDataURL(file);
}

// クリップボードから画像を貼り付け(スクショの Ctrl+V)
function handlePaste(e) {
  if (State.mode === "code") return;   // コードモードは画像添付なし
  const items = (e.clipboardData && e.clipboardData.items) || [];
  let found = false;
  for (const it of items) {
    if (it.type && it.type.startsWith("image/")) {
      const f = it.getAsFile();
      if (f) { addImageFile(f); found = true; }
    }
  }
  if (found) e.preventDefault();
}

// ドロップされたファイルを画像 / 文書に振り分け
function routeDroppedFiles(files) {
  const arr = Array.from(files);
  const imgs = arr.filter((f) => f.type && f.type.startsWith("image/"));
  const docs = arr.filter((f) => !(f.type && f.type.startsWith("image/")));
  imgs.forEach(addImageFile);
  if (docs.length) handleFiles(docs);
}

function showDropOverlay() { $("drop-overlay").classList.remove("hidden"); }
function hideDropOverlay() { $("drop-overlay").classList.add("hidden"); }

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
    if (idx.status === "ready") {
      const sm = idx.summary || {};
      const bg = (idx.file_count || 0) >= (idx.bg_threshold || 100);
      if (sm.status === "running") {
        card.appendChild(el("div", "kb-status building", "⏳ 要約中… " + escapeHtml(sm.msg || "")));
        const stop = el("button", "btn stop", "■ 中止");
        stop.onclick = () => cancelSummary(idx.id);
        actions.appendChild(stop);
      } else {
        const sum = el("button", "btn", "📝 要約" + (bg ? "(裏で実行)" : ""));
        sum.onclick = () => summarizeIndex(idx.id, idx.name, idx.file_count, idx.bg_threshold);
        actions.appendChild(sum);
        if (sm.status === "done" && sm.has_result) {
          const view = el("button", "btn", "📄 要約を表示");
          view.onclick = () => viewSummary(idx.id, idx.name);
          actions.appendChild(view);
        } else if (sm.status === "error") {
          card.appendChild(el("div", "kb-status error", "要約エラー: " + escapeHtml(sm.msg || "")));
        }
      }
    }
    const rebuild = el("button", "btn", "↻ 再構築");
    rebuild.onclick = () => rebuildIndex(idx.id);
    const del = el("button", "btn", "🗑 削除");
    del.onclick = () => deleteIndex(idx.id);
    actions.appendChild(rebuild); actions.appendChild(del);
    card.appendChild(actions);
    list.appendChild(card);
  });

  maybeStartPolling();
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

/* ---------- 資料の一括要約(map-reduce) ---------- */
const SummaryState = { iid: null, controller: null, running: false, text: "", categories: [] };

const SUMMARY_PRESETS = {
  "規程・規定": ["目的", "適用範囲・対象者", "定義", "主な規定内容・手続き", "責任者・体制", "罰則・例外", "改廃・施行日"],
  "契約書": ["当事者", "目的・対象", "期間", "金額・支払条件", "義務・責任", "解除・違約", "特記事項"],
  "議事録": ["会議名・日時・出席者", "議題", "決定事項", "対応・TODO(担当/期限)", "保留・課題"],
  "マニュアル/手順": ["目的", "対象・前提", "手順の流れ", "注意点・禁止事項", "トラブル時の対応"],
};

function renderSummaryPresets() {
  const wrap = $("summary-presets");
  wrap.innerHTML = "";
  Object.keys(SUMMARY_PRESETS).forEach((name) => {
    const cats = SUMMARY_PRESETS[name];
    const active = JSON.stringify(SummaryState.categories) === JSON.stringify(cats);
    const b = el("button", "chip-btn" + (active ? " active" : ""), name);
    b.onclick = () => {
      SummaryState.categories = active ? [] : cats.slice();
      renderSummaryPresets();
    };
    wrap.appendChild(b);
  });
}

function fillSummaryMapModel() {
  const sel = $("summary-map-model");
  sel.innerHTML = "";
  const none = el("option", null, "(メインモデルと同じ=二段なし)");
  none.value = "";
  sel.appendChild(none);
  const sorted = [...(State.models || [])].sort((a, b) => (a.size || 0) - (b.size || 0));
  sorted.forEach((m) => {
    const gb = m.size ? ` (${(m.size / 1e9).toFixed(1)}GB)` : "";
    const o = el("option", null, m.name + gb);
    o.value = m.name;
    sel.appendChild(o);
  });
  // 既定: 2つ以上あれば最小モデルを下書き用に(=二段ON)
  sel.value = sorted.length > 1 ? sorted[0].name : "";
}

function summarizeIndex(iid, name, fileCount, threshold) {
  SummaryState.iid = iid;
  SummaryState.text = "";
  SummaryState.categories = [];
  SummaryState.fileCount = fileCount || 0;
  SummaryState.threshold = threshold || 100;
  const bg = SummaryState.fileCount >= SummaryState.threshold;
  $("summary-progress").textContent = bg
    ? `${SummaryState.fileCount} 件と多いため、実行すると裏(バックグラウンド)で処理します。`
    : "";
  $("summary-result").innerHTML = "";
  $("summary-save-wrap").innerHTML = "";
  $("summary-instruction").value = "";
  $("summary-run").textContent = bg ? "裏で要約を開始" : "要約を実行";
  renderSummaryPresets();
  fillSummaryMapModel();
  $("summary-modal").querySelector("h2").textContent = "📝 一括要約: " + name;
  $("summary-modal").classList.remove("hidden");
}

async function viewSummary(iid, name) {
  let data;
  try { data = await api(`/api/indexes/${iid}/summary`); } catch (e) { toast(e.message); return; }
  SummaryState.iid = iid;
  SummaryState.text = data.result || "";
  SummaryState.categories = data.categories || [];
  SummaryState.fileCount = data.files || 0;
  $("summary-modal").querySelector("h2").textContent = "📄 要約結果: " + name;
  $("summary-run").textContent = "再実行";
  renderSummaryPresets();
  fillSummaryMapModel();
  $("summary-instruction").value = data.instruction || "";
  $("summary-progress").textContent = "前回の結果" + (data.files ? `(${data.files}件)` : "");
  $("summary-result").innerHTML = "";
  renderMarkdown($("summary-result"), SummaryState.text, true);
  $("summary-save-wrap").innerHTML = "";
  $("summary-save-wrap").appendChild(makeSaveMenu(() => SummaryState.text));
  $("summary-modal").classList.remove("hidden");
}

async function cancelSummary(iid) {
  try { await api(`/api/indexes/${iid}/summary/cancel`, { method: "POST" }); } catch (_) {}
  await loadIndexes();
  if (!$("kb-modal").classList.contains("hidden")) renderKbList();
}

async function startBackgroundSummary() {
  const instruction = $("summary-instruction").value.trim();
  const mapModel = $("summary-map-model").value || null;
  try {
    await api(`/api/indexes/${SummaryState.iid}/summarize/start`, {
      method: "POST",
      body: JSON.stringify({ instruction, map_model: mapModel, categories: SummaryState.categories }),
    });
    toast("裏で要約を開始しました。完了後に通知します。");
    $("summary-modal").classList.add("hidden");
    await loadIndexes();
    if (!$("kb-modal").classList.contains("hidden")) renderKbList();
    maybeStartPolling();
  } catch (e) { toast("要約の開始に失敗: " + e.message); }
}

/* バックグラウンド要約/索引作成の進捗をポーリングし、完了時に通知 */
let _summaryPolling = false;
const _summaryPrev = {};
function maybeStartPolling() {
  const active = (State.indexes || []).some(
    (i) => i.status === "building" || (i.summary && i.summary.status === "running"));
  if (active) pollSummaries();
}
async function pollSummaries() {
  if (_summaryPolling) return;
  _summaryPolling = true;
  try {
    while (true) {
      await new Promise((r) => setTimeout(r, 2000));
      await loadIndexes();
      (State.indexes || []).forEach((idx) => {
        const now = (idx.summary && idx.summary.status) || "none";
        const prev = _summaryPrev[idx.id];
        if (prev === "running" && now === "done") toast("📄 要約が完了しました: " + idx.name);
        else if (prev === "running" && now === "error") toast("要約でエラー: " + idx.name);
        _summaryPrev[idx.id] = now;
      });
      if (!$("kb-modal").classList.contains("hidden")) renderKbList();
      const stillActive = (State.indexes || []).some(
        (i) => i.status === "building" || (i.summary && i.summary.status === "running"));
      if (!stillActive) break;
    }
  } finally { _summaryPolling = false; }
}

function closeSummary() {
  if (SummaryState.running && SummaryState.controller) SummaryState.controller.abort();
  $("summary-modal").classList.add("hidden");
}

async function runSummary() {
  if (SummaryState.running || !SummaryState.iid) return;
  // 参照ファイルが多いときはウィンドウを出さず裏で実行
  if ((SummaryState.fileCount || 0) >= (SummaryState.threshold || 100)) {
    return startBackgroundSummary();
  }
  SummaryState.running = true;
  SummaryState.text = "";
  SummaryState.controller = new AbortController();
  $("summary-run").classList.add("hidden");
  $("summary-stop").classList.remove("hidden");
  $("summary-result").innerHTML = "";
  $("summary-save-wrap").innerHTML = "";
  $("summary-progress").textContent = "準備中…";
  const instruction = $("summary-instruction").value.trim();
  const mapModel = $("summary-map-model").value || null;
  try {
    const res = await fetch(`/api/indexes/${SummaryState.iid}/summarize`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction, map_model: mapModel, categories: SummaryState.categories }),
      signal: SummaryState.controller.signal,
    });
    if (res.status === 401) { showLogin(); throw new Error("認証が必要です"); }
    if (!res.ok) { let d = res.statusText; try { d = (await res.json()).detail || d; } catch (_) {} throw new Error(d); }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, idx).replace(/^data: /, ""); buf = buf.slice(idx + 2);
        if (!line) continue;
        let ev; try { ev = JSON.parse(line); } catch (_) { continue; }
        if (ev.type === "start") $("summary-progress").textContent =
          `${ev.files} 件を要約します…` + (ev.map_model ? `(下書き: ${ev.map_model})` : "");
        else if (ev.type === "progress") $("summary-progress").textContent = ev.msg || "";
        else if (ev.type === "result") {
          SummaryState.text = ev.text || "";
          renderMarkdown($("summary-result"), SummaryState.text, true);
          $("summary-progress").textContent = "完了";
          $("summary-save-wrap").appendChild(makeSaveMenu(() => SummaryState.text));
        } else if (ev.type === "error") {
          $("summary-progress").textContent = "エラー: " + (ev.error || "");
          toast("要約エラー: " + (ev.error || ""));
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") $("summary-progress").textContent = "停止しました";
    else { $("summary-progress").textContent = "エラー: " + e.message; toast(e.message); }
  } finally {
    SummaryState.running = false;
    SummaryState.controller = null;
    $("summary-run").classList.remove("hidden");
    $("summary-stop").classList.add("hidden");
  }
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
const FB = { path: null, selected: null, purpose: "index", wsOk: true, wsReason: "" };

async function openFolderBrowser(purpose) {
  FB.purpose = purpose || "index";
  FB.selected = null;
  $("fb-pick").textContent = FB.purpose === "workspace" ? "このフォルダを使う" : "このフォルダを追加";
  $("fb-pick").disabled = true;
  $("fb-note").classList.add("hidden");
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
    $("fb-path-input").value = data.path;
    $("fb-current").textContent = data.path;
    FB.wsOk = data.workspace_ok !== false;
    FB.wsReason = data.workspace_reason || "";
    applyPickGate();

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
      .then((r) => { $("fb-count").textContent = `対応ファイル ${r.count}${r.capped ? "以上" : ""} 件`; })
      .catch(() => { $("fb-count").textContent = ""; });
  } catch (e) {
    // 失敗時は一覧領域にエラーを表示(クリックが効いていることが分かるように)
    const list = $("fb-list"); list.innerHTML = "";
    list.appendChild(el("div", "fb-entry file", "⚠ " + escapeHtml(e.message)));
    FB.selected = null; applyPickGate();
    toast(e.message);
  }
}

// 「このフォルダを使う/追加」ボタンの有効・無効と注意書きを更新
function applyPickGate() {
  const note = $("fb-note");
  if (FB.purpose === "workspace") {
    const ok = !!FB.selected && FB.wsOk !== false;
    $("fb-pick").disabled = !ok;
    if (FB.selected && FB.wsOk === false) {
      note.textContent = "⚠ " + (FB.wsReason || "このフォルダは作業フォルダに使えません");
      note.classList.remove("hidden");
    } else {
      note.textContent = ""; note.classList.add("hidden");
    }
  } else {
    note.textContent = ""; note.classList.add("hidden");
    $("fb-pick").disabled = !FB.selected;
  }
}

async function pickFolder() {
  if (!FB.selected) return;
  $("folder-modal").classList.add("hidden");
  if (FB.purpose === "workspace") {
    await setCodeWorkspace(FB.selected);
    return;
  }
  try {
    const idx = await api("/api/indexes", {
      method: "POST", body: JSON.stringify({ paths: [FB.selected] }),
    });
    toast(`「${idx.name}」のインデックス作成を開始しました`);
    await loadIndexes(); renderKbList();
  } catch (e) { toast("作成失敗: " + e.message); }
}

/* Code: 作業フォルダの設定 / 変更許可トグル(会話設定に保存) */
async function setCodeWorkspace(path) {
  if (!State.current) return;
  try {
    const conv = await api(`/api/conversations/${State.current.id}`, {
      method: "PATCH", body: JSON.stringify({ settings: { workspace: path } }),
    });
    State.current = conv;
    updateCodeBar(conv);
    toast("作業フォルダを設定しました");
  } catch (e) { toast("設定失敗: " + e.message); }
}

async function setCodeAllow(on) {
  if (!State.current) return;
  try {
    const conv = await api(`/api/conversations/${State.current.id}`, {
      method: "PATCH", body: JSON.stringify({ settings: { allow_changes: !!on } }),
    });
    State.current = conv;
  } catch (e) { toast("設定失敗: " + e.message); $("cb-allow").checked = !on; }
}

async function setCodePlan(on) {
  if (!State.current) return;
  try {
    const conv = await api(`/api/conversations/${State.current.id}`, {
      method: "PATCH", body: JSON.stringify({ settings: { plan_mode: !!on } }),
    });
    State.current = conv;
    updateCodeBar(conv);
  } catch (e) { toast("設定失敗: " + e.message); $("cb-plan").checked = !on; }
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
  $("new-chat").onclick = () => newConversation();
  $("logout-btn").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };
  $("toggle-sidebar").onclick = () => $("sidebar").classList.toggle("open");

  // Chat / Code タブ
  document.querySelectorAll(".mode-tab").forEach((t) =>
    (t.onclick = () => setMode(t.dataset.mode)));
  // Code: 作業フォルダ / 変更許可
  $("cb-pick").onclick = () => openFolderBrowser("workspace");
  $("cb-plan").onchange = (e) => setCodePlan(e.target.checked);
  $("cb-allow").onchange = (e) => setCodeAllow(e.target.checked);

  $("chat-title").addEventListener("change", renameConversation);
  $("chat-title").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("chat-title").blur(); } });

  $("send-btn").onclick = onSend;
  $("stop-btn").onclick = stopGeneration;
  const input = $("input");
  input.addEventListener("input", autoResize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSend(); }
  });

  $("attach-btn").onclick = () => $("file-input").click();
  $("file-input").addEventListener("change", (e) => handleFiles(e.target.files));

  // スクショ等のペースト(Ctrl+V)
  $("input").addEventListener("paste", handlePaste);

  // ドラッグ&ドロップ(ウィンドウ全体で受け付け、オーバーレイ表示)
  let dragDepth = 0;
  const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  window.addEventListener("dragenter", (e) => { if (hasFiles(e)) { e.preventDefault(); dragDepth++; showDropOverlay(); } });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("dragleave", (e) => { dragDepth = Math.max(0, dragDepth - 1); if (dragDepth === 0) hideDropOverlay(); });
  window.addEventListener("drop", (e) => {
    e.preventDefault(); dragDepth = 0; hideDropOverlay();
    if (State.mode === "code") return;   // コードモードは添付なし
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      if (!State.current) { toast("先に会話を開いてください"); return; }
      routeDroppedFiles(e.dataTransfer.files);
    }
  });

  // クイック設定
  $("q-model").onchange = (e) => pushQuick({ model: e.target.value });
  $("q-effort").onchange = (e) => pushQuick({ effort: e.target.value });
  $("q-length").onchange = (e) => pushQuick({ num_predict: parseInt(e.target.value) });
  $("q-topk").querySelectorAll(".qseg-btn").forEach((b) => {
    b.onclick = () => {
      const v = parseInt(b.dataset.v);
      setTopkSeg(v);
      pushQuick({ top_k: v });
    };
  });

  // 設定モーダル
  $("open-settings").onclick = openSettings;
  $("save-settings").onclick = saveSettings;
  // KBモーダル
  $("open-kb").onclick = openKb;
  $("add-kb").onclick = () => openFolderBrowser("index");
  // 一括要約モーダル
  $("summary-run").onclick = runSummary;
  $("summary-stop").onclick = () => { if (SummaryState.controller) SummaryState.controller.abort(); };
  $("summary-close").onclick = closeSummary;
  $("summary-close2").onclick = closeSummary;
  // フォルダ
  $("fb-pick").onclick = pickFolder;
  $("fb-go").onclick = () => fbNavigate($("fb-path-input").value.trim());
  $("fb-path-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); fbNavigate($("fb-path-input").value.trim()); }
  });

  // 保存メニューを外側クリックで閉じる
  document.addEventListener("click", () =>
    document.querySelectorAll(".save-menu").forEach((m) => m.classList.add("hidden")));

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
