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
  State.searchQuery = ""; State.searchResults = [];        // モード切替で検索リセット
  if ($("conv-search")) $("conv-search").value = "";
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
  fillModelSelect($("set-vision-model"));
  fillModelSelect($("set-code-model"));
  // Code用モデルは「既定モデルと同じ(空)」を先頭に置いて選べるようにする
  const codeEmpty = el("option", null, "(既定モデルと同じ)");
  codeEmpty.value = "";
  $("set-code-model").insertBefore(codeEmpty, $("set-code-model").firstChild);
  if (!State.models.length) {
    const o = el("option", null, "(モデルなし — ollama pull が必要)");
    o.value = "";
    $("q-model").appendChild(o.cloneNode(true));
    $("set-model").appendChild(o.cloneNode(true));
    $("set-vision-model").appendChild(o);
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

