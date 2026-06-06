"""
summarize.py
多数のファイルを map-reduce で要約・集約する。
  map    : ファイルごとに個別要約(長いファイルは分割して部分要約→結合)
  reduce : 要約同士を、文脈に収まるまで階層的に統合
  final  : 依頼(instruction)に答える形で最終整形

LLM 呼び出しは summarize_fn(text, role) として注入できる(テスト容易・モデル非依存)。
進捗は stream_summarize がイベントで yield する(SSE 用)。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator

import ollama

from .config import settings
from .loaders import load_file
from .logging_setup import get_logger

log = get_logger("summarize")

MAP_CHARS = 8000        # 1回の要約入力に入れる最大文字数(超えたら分割)
REDUCE_BUDGET = 9000    # reduce 1回に渡す要約群の最大文字数
MAX_PARTS_PER_FILE = 12  # 1ファイルを分割する最大数(巨大ファイルの暴走防止)
MAX_FILE_CHARS = 200000  # 1ファイルから読む最大文字数
SUMMARY_TOKENS = 500

# summarize_fn(text, role) -> summary。role: file | merge | final
SummFn = Callable[[str, str], str]

_SYS = {
    "file": "次の資料を、後で集約しやすいように日本語で簡潔に要約してください。"
            "重要な事実・数値・固有名詞・結論を箇条書き中心で示し、出典の文脈を保ってください。",
    "merge": "次の複数の要約を、重複を排して日本語で簡潔に統合してください。重要点は落とさないこと。",
    "final": "次の要約群をもとに、ユーザーの依頼に答える形で日本語で分かりやすくまとめてください。"
             "見出しと箇条書きで構造化し、必要なら表も使ってください。",
}


def model_summarize_fn(model: str, instruction: str = "",
                       map_model: str | None = None,
                       categories: list[str] | None = None) -> SummFn:
    """Ollama を使う summarize_fn を返す。
    map_model  : file/merge(下書き=大量呼び出し)を担うモデル。空/None ならメイン model を使用。
    categories : ガイド付き要約の観点。指定すると抽出・最終整形を見出し構造化する。
    """
    client = ollama.Client(host=settings.ollama_host)
    focus = f"\n特に「{instruction}」に関係する点を重視してください。" if instruction else ""
    cats = [c.strip() for c in (categories or []) if c and c.strip()]
    cat_extract = ("\n次の観点に該当する情報を重点的に抽出してください: " + " / ".join(cats)) if cats else ""

    def fn(text: str, role: str) -> str:
        # 下書き(file/merge)は高速モデル、最終整形(final)は品質モデル
        use_model = map_model if (role in ("file", "merge") and map_model) else model
        if role == "final":
            sys = _SYS["final"]
            if cats:
                sys += ("\n次の見出しで構造化してまとめてください(該当情報が無い見出しは「記載なし」と明記):\n"
                        + "\n".join(f"{i}. {c}" for i, c in enumerate(cats, 1)))
            user = f"【依頼】{instruction or '全体の概要をまとめる'}\n\n【要約群】\n{text}"
        else:
            sys = _SYS.get(role, _SYS["file"]) + focus + (cat_extract if role == "file" else "")
            user = text
        try:
            r = client.chat(model=use_model, messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user},
            ], options={"num_predict": SUMMARY_TOKENS})
            return (getattr(r.message, "content", "") or "").strip()
        except Exception as e:
            log.warning("要約呼び出し失敗(model=%s): %s", use_model, e)
            return ""

    return fn


def _extract_text(f: Path) -> str:
    try:
        blocks = load_file(f)
    except Exception as e:
        log.warning("読込失敗 %s: %s", f, e)
        return ""
    text = "\n".join(b.get("text", "") for b in (blocks or []))
    return text[:MAX_FILE_CHARS]


def _summarize_long(text: str, fn: SummFn) -> str:
    """1ファイル分。長ければ分割して部分要約→結合。"""
    text = text.strip()
    if not text:
        return ""
    if len(text) <= MAP_CHARS:
        return fn(text, "file")
    parts = [text[i:i + MAP_CHARS] for i in range(0, len(text), MAP_CHARS)][:MAX_PARTS_PER_FILE]
    partials = [s for s in (fn(p, "file") for p in parts) if s]
    combined = "\n".join(partials)
    if len(combined) > MAP_CHARS:
        return fn(combined, "merge")
    return combined


def _reduce(items: list[str], fn: SummFn) -> str:
    """要約群を REDUCE_BUDGET に収まるまで階層的に統合。"""
    while sum(len(x) for x in items) > REDUCE_BUDGET and len(items) > 1:
        new: list[str] = []
        batch: list[str] = []
        size = 0
        for it in items:
            if batch and size + len(it) > REDUCE_BUDGET:
                new.append(fn("\n\n".join(batch), "merge"))
                batch, size = [], 0
            batch.append(it)
            size += len(it)
        if batch:
            new.append(fn("\n\n".join(batch), "merge"))
        new = [n for n in new if n]
        if not new or len(new) >= len(items):
            break   # 収束しない場合の保険
        items = new
    return "\n\n".join(items)


def stream_summarize(files: list[Path], instruction: str, fn: SummFn) -> Iterator[dict]:
    """map-reduce 要約を進捗付きで実行。
    yield: {"type":"progress","msg":...} / {"type":"result","text":...} / {"type":"error","error":...}
    クライアント切断時は GeneratorExit で自然に停止する。
    """
    n = len(files)
    file_summaries: list[str] = []
    for i, f in enumerate(files, 1):
        yield {"type": "progress", "msg": f"要約中 {i}/{n}: {f.name}"}
        text = _extract_text(f)
        if not text.strip():
            continue
        s = _summarize_long(text, fn)
        if s:
            file_summaries.append(f"【{f.name}】\n{s}")
    if not file_summaries:
        yield {"type": "result", "text": "(要約できる本文がありませんでした)"}
        return
    yield {"type": "progress", "msg": f"{len(file_summaries)} 件の要約を統合中..."}
    combined = _reduce(file_summaries, fn)
    final = fn(combined, "final") or combined
    yield {"type": "result", "text": final}


def run_summarize(files: list[Path], instruction: str, fn: SummFn) -> str:
    """同期版(Code エージェントのツール用)。最終要約テキストを返す。"""
    result = ""
    for ev in stream_summarize(files, instruction, fn):
        if ev.get("type") == "result":
            result = ev.get("text", "")
    return result
