/* ============================================================
   入力欄の挙動
   ============================================================ */
function autoResize() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
}

function closeSidebarMobile() {
  $("sidebar").classList.remove("open");
  const t = $("toggle-sidebar");
  if (t) t.setAttribute("aria-expanded", "false");
}

/* ============================================================
   イベント束ね
   ============================================================ */
function bindGlobalEvents() {
  $("login-btn").onclick = doLogin;
  $("login-password").addEventListener("keydown", (e) => { if (e.key === "Enter") doLogin(); });

  $("theme-toggle").onclick = toggleTheme;
  $("new-chat").onclick = () => newConversation();
  $("conv-search").addEventListener("input", (e) => onConvSearch(e.target.value));
  $("logout-btn").onclick = async () => { await api("/api/logout", { method: "POST" }); location.reload(); };
  $("toggle-sidebar").onclick = (e) => {
    const open = $("sidebar").classList.toggle("open");
    e.currentTarget.setAttribute("aria-expanded", open ? "true" : "false");
  };

  // Chat / Code タブ
  document.querySelectorAll(".mode-tab").forEach((t) =>
    (t.onclick = () => setMode(t.dataset.mode)));
  // Code: 作業フォルダ / 変更許可
  $("cb-pick").onclick = () => openFolderBrowser("workspace");
  $("cb-plan").onchange = (e) => setCodePlan(e.target.checked);
  $("cb-allow").onchange = (e) => setCodeAllow(e.target.checked);
  $("cb-autoaccept").onchange = (e) => setCodeAutoAccept(e.target.checked);
  if ($("cb-verify")) $("cb-verify").onchange = (e) => setCodeVerify(e.target.checked);
  if ($("cb-verify-cmd")) $("cb-verify-cmd").onchange = (e) => setCodeVerifyCmd(e.target.value.trim());

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
  // ファイル閲覧
  $("fv-close").onclick = closeFileViewer;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("file-modal").classList.contains("hidden")) closeFileViewer();
  });
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
