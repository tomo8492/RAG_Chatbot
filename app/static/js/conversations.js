/* ============================================================
   会話一覧
   ============================================================ */
async function loadConversations() {
  State.conversations = await api("/api/conversations");
  renderConversationList();
}

let _convSearchTimer = null;
function onConvSearch(q) {
  State.searchQuery = q;
  clearTimeout(_convSearchTimer);
  if (!q.trim()) { State.searchResults = []; renderConversationList(); return; }
  _convSearchTimer = setTimeout(async () => {
    try {
      State.searchResults = await api(
        `/api/conversations?kind=${encodeURIComponent(State.mode)}&q=${encodeURIComponent(q.trim())}`);
    } catch (_) { State.searchResults = []; }
    renderConversationList();
  }, 250);
}

function renderConversationList() {
  const list = $("conv-list");
  list.innerHTML = "";
  const searching = !!(State.searchQuery && State.searchQuery.trim());
  const items = searching ? (State.searchResults || []) : convsOfMode();
  if (searching && !items.length) {
    list.appendChild(el("div", "conv-empty", "一致する会話がありません"));
    return;
  }
  // 固定(お気に入り)を常に上へ。サーバ順は維持しつつ、新規作成直後の
  // 楽観的 unshift でも固定が押し下げられないよう安定ソートで補正する
  const ordered = items.slice().sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
  ordered.forEach((c) => {
    const item = el("div", "conv-item"
      + (State.current && c.id === State.current.id ? " active" : "")
      + (c.pinned ? " pinned" : ""));
    item.appendChild(el("span", "title", escapeHtml(c.title || "新しい会話")));
    const pin = el("span", "pin", "📌");
    pin.title = c.pinned ? "固定を解除" : "お気に入り(上部に固定)";
    pin.onclick = async (ev) => {
      ev.stopPropagation();
      try {
        await api(`/api/conversations/${c.id}`, {
          method: "PATCH", body: JSON.stringify({ pinned: !c.pinned }),
        });
        await loadConversations();
      } catch (e) { toast("固定の切替に失敗: " + e.message); }
    };
    item.appendChild(pin);
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
  if ($("cb-autoaccept")) $("cb-autoaccept").checked = !!s.auto_accept_edits;
  if ($("cb-verify")) $("cb-verify").checked = s.auto_verify !== false;   // 検証ループ(既定ON)
  if ($("cb-verify-cmd")) $("cb-verify-cmd").value = s.verify_cmd || "";
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

