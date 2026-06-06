#!/usr/bin/env python3
"""実規定の見出し/チャンク構造を目視確認するツール。

phase1-regulations.md §0「実測検証(設計の裏取り)」の実施手順を1コマンド化したもの。
実規定 md(またはPDF/Word等)を `loaders.load_file → splitter.split_structured` に通し、
(見出しパス, チャンク本文) を一覧表示する。設計の前提が実物で成立するかを確認するために使う。

使い方(リポジトリ直下で実行):
    python -m evalkit.inspect_chunks data/regulations_intake/就業規則.md
    python -m evalkit.inspect_chunks data/regulations_intake/        # フォルダ一括
    python -m evalkit.inspect_chunks <path> --full                   # 本文を全文表示
    python -m evalkit.inspect_chunks <path> --chunk-size 800 --overlap 120

出力の最後に、phase1 §0 で判明した「附則/別表が直前の条に飲み込まれる」問題が
実物で起きていないか(=見出し未検出の疑い)を自動チェックして警告する。

機微情報の扱い: 出力は端末のみ。**実規定の本文はリポジトリにコミットしないこと**
(置き場 data/ は .gitignore 済み)。テストに載せるのは匿名化した最小例に限る。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# リポジトリ直下を import パスに追加(python -m でなくても動くように)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.loaders import LOADERS, load_file       # noqa: E402
from app.splitter import split_structured        # noqa: E402

# 行頭の「附則 / 別表(第N)/ 様式(第N)/ 別記 / 別紙」。phase1 §0 の未検出見出し候補。
APPENDIX_RE = re.compile(
    r"^\s*(附\s*則"
    r"|別\s*表(?:第[0-9０-９一二三四五六七八九十百]+)?"
    r"|様\s*式(?:第[0-9０-９一二三四五六七八九十百]+)?"
    r"|別\s*記|別\s*紙)",
    re.M,
)
ARTICLE_RE = re.compile(r"第[0-9０-９一二三四五六七八九十百]+\s*条")


def iter_files(target: Path):
    if target.is_dir():
        for p in sorted(target.rglob("*")):
            if p.is_file() and p.suffix.lower() in LOADERS:
                yield p
    elif target.is_file():
        yield target


def inspect(path: Path, *, chunk_size: int, overlap: int, full: bool, snippet: int):
    blocks = load_file(path)
    if not blocks:
        print(f"  (読み込み結果なし / 未対応形式: {path.name})")
        return {"chunks": 0, "headings": set(), "suspect": []}

    headings: set[str] = set()
    suspect: list[tuple[str, str]] = []      # (見出しパス, 疑いの行)
    n_article = 0
    idx = 0

    for b in blocks:
        for chunk, heading in split_structured(b["text"], chunk_size, overlap):
            idx += 1
            headings.add(heading)
            if ARTICLE_RE.search(heading):
                n_article += 1
            # §0 チェック: 本文に附則/別表等の行があるのに、見出し側がそれを表していない
            m = APPENDIX_RE.search(chunk)
            if m and not APPENDIX_RE.search(heading or ""):
                line = m.group(0).strip()
                suspect.append((heading or "(見出しなし)", line))

            body = chunk if full else (chunk[:snippet] + ("…" if len(chunk) > snippet else ""))
            print(f"--- #{idx}  [loc: {b.get('loc', '')}] ---")
            print(f"PATH: {heading or '(見出しなし)'}")
            print(f"LEN : {len(chunk)}")
            print(f"BODY: {body}")
            print()

    return {"chunks": idx, "headings": headings, "suspect": suspect, "article": n_article}


def main(argv=None):
    ap = argparse.ArgumentParser(description="実規定の見出し/チャンク構造を目視確認")
    ap.add_argument("path", help="ファイル または フォルダ")
    ap.add_argument("--chunk-size", type=int, default=800)
    ap.add_argument("--overlap", type=int, default=120)
    ap.add_argument("--full", action="store_true", help="本文を全文表示(既定は先頭のみ)")
    ap.add_argument("--snippet", type=int, default=200, help="本文表示の文字数(既定200)")
    args = ap.parse_args(argv)

    target = Path(args.path)
    if not target.exists():
        ap.error(f"見つかりません: {target}")

    files = list(iter_files(target))
    if not files:
        ap.error(f"対応ファイルがありません(対応拡張子: {sorted(LOADERS)})")

    total = 0
    all_suspect: list[tuple[str, str, str]] = []   # (file, path, line)
    total_article = 0
    for f in files:
        print("=" * 70)
        print(f"FILE: {f}")
        print("=" * 70)
        r = inspect(f, chunk_size=args.chunk_size, overlap=args.overlap,
                    full=args.full, snippet=args.snippet)
        total += r["chunks"]
        total_article += r.get("article", 0)
        for path, line in r["suspect"]:
            all_suspect.append((f.name, path, line))

    # サマリ
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"ファイル数 : {len(files)}")
    print(f"チャンク数 : {total}(うち第N条の見出し: {total_article})")
    if all_suspect:
        print()
        print("⚠ 見出し未検出の疑い(phase1 §0: 附則/別表が条に飲み込まれている可能性):")
        for fname, path, line in all_suspect:
            print(f"  - {fname}: 「{line}」が PATH『{path}』の本文に混入")
        print()
        print("  → splitter._heading に 附則/別表/様式/別記/別紙 を追加する設計("
              "phase1 §0)が実物でも必要、の裏付け。")
    else:
        print("附則/別表の見出し未検出の疑い: なし")


if __name__ == "__main__":
    main()
