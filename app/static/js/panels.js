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
  setSelect($("set-vision-model"), d.vision_model);
  setSelect($("set-code-model"), d.code_model || "");
  $("set-system").value = d.system_prompt || "";
  setSelect($("set-effort"), d.effort);
  bindRange("set-numpredict", "set-numpredict-val", d.num_predict);
  bindRange("set-temp", "set-temp-val", d.temperature);
  bindRange("set-topp", "set-topp-val", d.top_p);
  bindRange("set-topk", "set-topk-val", d.top_k);
  bindRange("set-numctx", "set-numctx-val", d.num_ctx);
  bindRange("set-chunk", "set-chunk-val", d.chunk_size);
  bindRange("set-overlap", "set-overlap-val", d.chunk_overlap);
  if ($("set-contextual")) $("set-contextual").checked = !!d.contextual_embeddings;
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
    vision_model: $("set-vision-model").value,
    code_model: $("set-code-model").value,
    system_prompt: $("set-system").value,
    effort: $("set-effort").value,
    num_predict: parseInt($("set-numpredict").value),
    temperature: parseFloat($("set-temp").value),
    top_p: parseFloat($("set-topp").value),
    top_k: parseInt($("set-topk").value),
    num_ctx: parseInt($("set-numctx").value),
    chunk_size: parseInt($("set-chunk").value),
    chunk_overlap: parseInt($("set-overlap").value),
    contextual_embeddings: !!($("set-contextual") && $("set-contextual").checked),
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

async function setCodeAutoAccept(on) {
  if (!State.current) return;
  try {
    const conv = await api(`/api/conversations/${State.current.id}`, {
      method: "PATCH", body: JSON.stringify({ settings: { auto_accept_edits: !!on } }),
    });
    State.current = conv;
    if ($("cb-autoaccept")) $("cb-autoaccept").checked = !!on;
  } catch (e) { toast("設定失敗: " + e.message); if ($("cb-autoaccept")) $("cb-autoaccept").checked = !on; }
}

async function setCodeVerify(on) {
  if (!State.current) return;
  try {
    const conv = await api(`/api/conversations/${State.current.id}`, {
      method: "PATCH", body: JSON.stringify({ settings: { auto_verify: !!on } }),
    });
    State.current = conv;
  } catch (e) { toast("設定失敗: " + e.message); if ($("cb-verify")) $("cb-verify").checked = !on; }
}

async function setCodeVerifyCmd(cmd) {
  if (!State.current) return;
  try {
    const conv = await api(`/api/conversations/${State.current.id}`, {
      method: "PATCH", body: JSON.stringify({ settings: { verify_cmd: cmd } }),
    });
    State.current = conv;
  } catch (e) { toast("設定失敗: " + e.message); }
}

