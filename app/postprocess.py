"""LLM 出力の後処理。

Qwen3 などは推論を <think>...</think> で囲んで出力することがあり、Ollama の think 分離が
効かない場合(工数=最小/低、または think 非対応)に本文(content)へ混入して Markdown や
Mermaid コードブロックを壊す。ここでその除去と、Mermaid フェンスの簡易検証・補修を行う。

ストリーミング表示ではフロント側で同等の strip を毎回バッファ全体へ適用するため、
開きタグのみ・閉じタグのみの不完全ケースも考慮する。
"""
from __future__ import annotations

import re

# 完全な <think>...</think> ブロック(複数行対応)
_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
# 開きタグのみ(以降が未完の思考): <think> から末尾まで
_THINK_OPEN_TAIL = re.compile(r"<think\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)
# 閉じタグのみ(冒頭が思考の続き): 先頭から最初の </think> まで
_THINK_CLOSE_HEAD = re.compile(r"\A.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_HAS_OPEN = re.compile(r"<think\b", re.IGNORECASE)
_HAS_CLOSE = re.compile(r"</think\s*>", re.IGNORECASE)

# コードフェンス ``` の行(言語指定の有無は問わない)
_FENCE = re.compile(r"^[ \t]*```", re.MULTILINE)

# Mermaid の図種宣言として認めるトークン
MERMAID_DECLS = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram",
    "stateDiagram-v2", "stateDiagram", "erDiagram", "gantt", "journey",
    "gitGraph", "pie", "mindmap", "timeline", "quadrantChart",
    "requirementDiagram", "C4Context", "sankey-beta", "xychart-beta",
)


def strip_think(text: str) -> str:
    """content に混入した <think> 系を除去する。

    1) 完全な <think>...</think> を全て除去
    2) 残りに </think> だけがある(開きが無い)なら、先頭〜その閉じタグまでを思考として除去
    3) 残りに <think> だけがある(閉じが無い)なら、その開きタグ〜末尾までを除去
    """
    if not text:
        return text or ""
    s = _THINK_BLOCK.sub("", text)
    if _HAS_CLOSE.search(s) and not _HAS_OPEN.search(s):
        s = _THINK_CLOSE_HEAD.sub("", s, count=1)
    if _HAS_OPEN.search(s):
        s = _THINK_OPEN_TAIL.sub("", s)
    return s


def close_unclosed_fence(text: str) -> str:
    """コードフェンス ``` の数が奇数(未閉じ)なら、末尾に閉じフェンスを補う。"""
    if not text:
        return text or ""
    if len(_FENCE.findall(text)) % 2 == 1:
        return text + ("" if text.endswith("\n") else "\n") + "```"
    return text


def validate_mermaid(text: str) -> list[str]:
    """```mermaid ブロックの簡易検証。問題点の説明リストを返す(空 = 問題なし)。

    - 図種宣言(flowchart 等)がブロック先頭にあるか
    - ブロックが ``` で閉じられているか
    """
    issues: list[str] = []
    for m in re.finditer(r"```[ \t]*mermaid[ \t]*\r?\n", text, re.IGNORECASE):
        rest = text[m.end():]
        close = re.search(r"\r?\n[ \t]*```", rest)
        body = rest[: close.start()] if close else rest
        if close is None:
            issues.append("Mermaid ブロックが ``` で閉じられていません")
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        if not first.startswith(MERMAID_DECLS):
            issues.append(f"Mermaid の図種宣言が先頭にありません(先頭: '{first[:30]}')")
    return issues


# 図ラベルに混入しやすい英単語の頻出スペルミス → 正しい綴り(1か所に集約・拡張可)
MERMAID_SPELL = {
    "Srart": "Start", "srart": "start", "Strat": "Start", "strat": "start",
    "undefine": "undefined",
    "Defualt": "Default", "defualt": "default", "Defalt": "Default", "defalt": "default",
    "Reciept": "Receipt", "Recieve": "Receive", "recieve": "receive", "recieved": "received",
    "Lenght": "Length", "lenght": "length",
    "Vaule": "Value", "vaule": "value",
    "Retrun": "Return", "retrun": "return",
    "Funtion": "Function", "funtion": "function",
    "Paramter": "Parameter", "paramter": "parameter",
    "Sucess": "Success", "sucess": "success", "Succes": "Success",
    "Initalize": "Initialize", "initalize": "initialize",
    "Feild": "Field", "feild": "field",
    "seperate": "separate", "occured": "occurred", "vaild": "valid",
}
_SPELL_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(MERMAID_SPELL, key=len, reverse=True)) + r")\b"
)
# ```mermaid 〜 ```(未閉じなら末尾まで)を本文の他部分と区別して取り出す
_MERMAID_FENCE_RE = re.compile(
    r"(```[ \t]*mermaid[ \t]*\r?\n)(.*?)(\r?\n[ \t]*```|\Z)", re.DOTALL | re.IGNORECASE
)


def fix_spelling(s: str) -> str:
    """既知の頻出スペルミスを補正する(単語境界一致)。"""
    return _SPELL_RE.sub(lambda m: MERMAID_SPELL[m.group(1)], s or "")


# flowchart/graph のノード括弧の不一致・閉じ忘れを補修する(LLM が [..} や途中切れを出しても描画可能に)
_BR_PAIRS = {"[": "]", "(": ")", "{": "}"}
_BR_CLOSERS = set(_BR_PAIRS.values())


def _balance_brackets(line: str) -> str:
    """1行内の () [] {} を均衡させる。

    - 不一致の閉じ括弧は、対応する開きに合わせて矯正する(例: `F[問題発覚?}` → `F[問題発覚?]`)。
    - 行末で閉じ忘れ(途中で切れた図など)があれば補う(例: `M[手順書承` → `M[手順書承]`)。
    - 引用符 "..." の内側は対象外。**括弧が正しく閉じている行は一切変更しない**(冪等)。
    """
    if line.lstrip().startswith("%%"):       # mermaid コメント行は触らない
        return line
    out, stack, in_q = [], [], False
    for ch in line:
        if ch == '"':
            in_q = not in_q
            out.append(ch)
        elif in_q:
            out.append(ch)
        elif ch in _BR_PAIRS:
            stack.append(_BR_PAIRS[ch])
            out.append(ch)
        elif ch in _BR_CLOSERS:
            out.append(stack.pop() if stack else ch)   # 期待される閉じに矯正(余分な閉じはそのまま)
        else:
            out.append(ch)
    while stack:                              # 閉じ忘れを補完
        out.append(stack.pop())
    return "".join(out)


def _repair_flowchart(body: str) -> str:
    """ブロック先頭が flowchart/graph のときだけ、各行のノード括弧を均衡化する。"""
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    if not first.startswith(("flowchart", "graph")):
        return body
    return "\n".join(_balance_brackets(ln) for ln in body.split("\n"))


def normalize_mermaid(text: str) -> str:
    """```mermaid ブロックの内側だけ補正する(本文・他言語コードは変更しない)。
    スペル補正 → flowchart のノード括弧の補修。"""
    if not text or "```" not in text:
        return text or ""
    return _MERMAID_FENCE_RE.sub(
        lambda m: m.group(1) + _repair_flowchart(fix_spelling(m.group(2))) + m.group(3), text)


def clean(text: str) -> str:
    """保存・表示前の総合後処理:
    <think> 除去 → mermaid内スペル補正 → 未閉じフェンス補完 → 前後空白整理。"""
    s = strip_think(text or "")
    s = normalize_mermaid(s)
    s = close_unclosed_fence(s)
    return s.strip()
