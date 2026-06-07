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
// エラー文に相関ID(req)を付与。利用者が管理者へ報告しやすく、サーバログと突合できる。
function reqSuffix(rid) { return rid ? ` (req: ${rid})` : ""; }

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
    throw new Error(detail + reqSuffix(res.headers.get("X-Request-ID")));
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
// スクリーンリーダーへ状態を読み上げさせる(視覚的には非表示の live region)
function srAnnounce(msg) {
  const e = $("sr-status");
  if (e) e.textContent = msg || "";
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
  rerenderMermaidTheme();   // 既存の Mermaid 図を新テーマで再描画(未初期化なら no-op)
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

