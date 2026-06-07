// フロントは複数のクラシック script を順に読み込み「共有グローバルスコープ」で動く。
// CI では結合(_eslint_bundle.js)に no-undef を検査し、未定義参照/タイプミスを検出する。
// (ES モジュール化の前提となる安全網。ESLint は npx で取得=リポジトリに node 依存を持たない)
const ro = (names) => names.reduce((o, k) => ((o[k] = "readonly"), o), {});
const BROWSER = ro([
  "window", "document", "navigator", "localStorage", "sessionStorage", "fetch",
  "FormData", "FileReader", "Blob", "File", "FileList", "DataTransfer", "URL",
  "URLSearchParams", "TextDecoder", "TextEncoder", "requestAnimationFrame",
  "cancelAnimationFrame", "setTimeout", "clearTimeout", "setInterval", "clearInterval",
  "queueMicrotask", "console", "alert", "confirm", "prompt", "matchMedia",
  "AbortController", "AbortSignal", "Image", "DOMParser", "XMLSerializer",
  "XMLHttpRequest", "getComputedStyle", "getSelection", "history", "location",
  "CustomEvent", "Event", "KeyboardEvent", "MouseEvent", "DragEvent", "ClipboardEvent",
  "MutationObserver", "ResizeObserver", "IntersectionObserver", "performance",
  "structuredClone", "btoa", "atob", "crypto", "Node", "NodeList", "Element",
  "HTMLElement", "Range", "Headers", "Request", "Response", "WebSocket", "EventSource",
  "scrollTo", "scrollBy", "open", "ClipboardItem", "FontFace",
]);
module.exports = [
  {
    files: ["_eslint_bundle.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...BROWSER, marked: "readonly", DOMPurify: "readonly", hljs: "readonly", mermaid: "readonly" },
    },
    rules: { "no-undef": "error", "no-unused-vars": "off" },
  },
];
