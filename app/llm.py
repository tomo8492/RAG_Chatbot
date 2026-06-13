"""
llm.py
Ollama によるチャット生成。
  - モデル一覧取得
  - ストリーミング生成(content と thinking を分離して yield)
  - effort(思考の深さ)= think パラメータ。未対応モデルは自動フォールバック
  - 生成パラメータ(temperature / num_predict 等)対応
"""
from __future__ import annotations

import re
from typing import Iterator, Optional

from .config import settings
from .logging_setup import get_logger

log = get_logger("llm")


DEFAULT_SYSTEM_PROMPT = (
    "あなたは親切で有能な日本語アシスタントです。質問には正確かつ分かりやすく答えてください。"
    "コードや表は Markdown で整形してください。"
)

# Claude 流の回答スタイル補正。ペルソナ(system_prompt)や参考資料指示に関わらず常に付与し、
# 「結論ファースト・簡潔・前置き/お世辞なし・事実と推測の区別」を効かせる。
STYLE_GUIDE = """【回答のスタイル】
- 結論ファースト: まず質問への答えを述べ、根拠・補足はその後に簡潔に書く。
- 前置き・お世辞を書かない:「承知しました」「良い質問ですね」「以下に説明します」
  「お役に立てれば幸いです」のような決まり文句は不要。いきなり本題から始める。
- 長さは内容に合わせる: 単純な質問は簡潔に(1〜3行)、説明・手順・比較が要るときは十分に詳しく。
  冗長な言い換えや同じ内容の繰り返しはしない。
- 読みやすく整形: 必要に応じて箇条書き・短い見出し・表・コードブロックを使う(Markdown)。
  ただし単純な答えを無理に箇条書きにしない。
- 推測と事実を区別する。不確かなことは断定せず「確証はないが」等と添える。
  わからないことは正直に「わからない」と述べ、事実を作らない。"""

# 出力フォーマットの強制(Markdown 記法の全列挙 + Mermaid 図種の全列挙 + 厳密フォーマット)。
# STYLE_GUIDE と同様に常時付与する。図が有効なときに必ず図にさせ、Mermaid の構文崩れを防ぐ。
MARKDOWN_MERMAID_GUIDE = """【出力フォーマット(Markdown / 図)】
読みやすさのため、適切な場合は必ず Markdown で構造化する。使用できる記法:
- 見出し(#, ##, ###) / 箇条書き・番号付きリスト / 太字(**) / 斜体(*) / リンク([text](URL))
- 表(| 列 | 列 |) / コードブロック(```言語) / 引用(>) / 数式(LaTeX: インライン $...$、ブロック $$...$$)

図で示すと分かりやすい内容(処理の流れ・手順・分岐、登場人物のやり取り、クラスやデータ構造、
状態遷移、エンティティ関係、工程表、構成・関係など)は、必ず Mermaid 図で表す。
使用できる Mermaid 図種(用途):
- flowchart(フロー・分岐) / sequenceDiagram(やり取り) / classDiagram(クラス)
- stateDiagram-v2(状態遷移) / erDiagram(ER) / gantt(工程) / journey(体験)
- gitGraph(ブランチ) / pie(構成比)

Mermaid を出力するときの厳密ルール(必ず守る):
1. ```mermaid で開始する。
2. 次の行を図種の宣言から始める(例: flowchart TD / sequenceDiagram / classDiagram)。
3. 図のコード行を書き、最後のコード行の直後に閉じ ``` を置いて終える。
4. ``` の中には図のコードだけを書く。前置き・後置き・説明文・余分な整形を混ぜない
   (説明は ``` の外に書く)。
5. ノードのラベルに記号を含むときは "..." で囲む(日本語ラベル可)。

flowchart のノードは種別ごとに次の形・クラスで表す(色は自動で付くので classDef は書かない):
- 開始/終了 : [ラベル]:::startend
- 通常処理 : [ラベル](装飾なし)
- 判定    : {ラベル}(ひし形)
- 特殊処理A(初期値設定・既定値の付与など): [ラベル]:::accent1
- 特殊処理B(状態変更・編集可へ変更など)  : [ラベル]:::accent2
- ループバック(戻り)の矢印には必ずラベルを付け、何の繰り返しかを書く
  (例: N -- 次の行へ / 行(row)処理を繰り返し --> L)。入れ子が読めるようにする。
- 向き: 処理・手順・工程など「流れ」は横向き flowchart LR を既定にし、階層・分類・組織図など
  「構造」は縦 flowchart TD にする。ノード数が多い流れは LR で横に伸ばすと読みやすい。

ノードラベルの言語(基準): 原則は日本語。ただし関数名・型・予約語・API名などの
技術用語のみ英語可(例: undefined, SetDefaultValue は英語のまま、説明的な語は日本語)。
同じ図の中で表記をぶらさない。スペルミスをしない(例: Start を "Srart" と書かない)。

【HTML/ページとして出力するとき(Claude流)】
- 1ファイルで自己完結させる。CSS は <style> にインラインで書き、外部CDN・Webフォントに依存しない
  (オフラインでも単体で開ける)。JS が必要なら最小限をインラインに置く。
- 図は <div> や手描き <svg> で作らず ```mermaid を使う(描画はシステムが行う)。生HTML内に
  図を置く場合は <pre class="mermaid">…(図のコード)…</pre> を使う(外部CDNの mermaid は読み込まない)。
- デザイン: 明確な見出し階層・十分な余白(行間1.7〜1.9)・無彩色＋アクセント1色・本文幅は
  読みやすい上限(〜820px程度)・表はヘッダ強調＋偶数行ゼブラ・レスポンシブ(@media)と
  印刷(@media print)に対応・セマンティックHTML。

ここに挙げた記法・図種・ルールは省略・要約しない。"""

# 図の指示が不要な通常QAで付ける短い整形ガイド(コンテキストを節約し、根拠提示に集中させる)。
MARKDOWN_GUIDE_BRIEF = """【出力フォーマット】
読みやすさのため適切に Markdown で構造化する: 見出し / 箇条書き・番号付き / 太字 / 表 /
コードブロック(```言語) / 引用 / 数式(インライン $...$、ブロック $$...$$)。
単純な答えを無理に箇条書きにしない。処理の流れ・手順・分岐・やり取り・構造・状態遷移・
関係・工程など「図で示すと明確な内容」は Mermaid 図(```mermaid)で表してよい。"""

# 図・作図・可視化・HTML等を求める意図の検出(これに該当する時だけ詳細な図ガイドを付与)
_DIAGRAM_HINT = re.compile(
    r"(図解|作図|可視化|フロー|フローチャート|ダイアグラム|ダイヤグラム|チャート|"
    r"シーケンス|クラス図|状態遷移|ER図|ガント|工程表|構成図|関係図|ツリー図|"
    r"mermaid|diagram|flowchart|sequence|gantt|html|スライド|描いて|図示|図にして)",
    re.IGNORECASE,
)


def wants_diagram(text: str) -> bool:
    """質問が図・作図・可視化・HTML等を求めているか(詳細な図ガイド付与の判定)。"""
    return bool(_DIAGRAM_HINT.search(text or ""))


# RAG 用の指示(参考資料がある場合に付与)
RAG_INSTRUCTION = """以下の【参考資料】を最優先の根拠として回答してください。

ルール:
1. 回答は可能な限り【参考資料】の内容に基づくこと。資料に無い事項は一般知識で補ってよいが、その場合は資料に基づかない旨を明示する。
2. 資料を根拠にした箇所では、文末に「(出典: ファイル名 場所)」の形式で出典を示す。
3. 専門用語や条項はできるだけ原文のまま正確に引用する。
4. 資料内に該当が全く無い場合は「参考資料内に該当する記載は見つかりませんでした」と述べたうえで、一般知識で回答する。

【参考資料】
{context}
"""

# 厳格RAG(参照フォルダ選択時)。参考資料の内容だけで回答し、一般知識・外部情報は使わない。
RAG_INSTRUCTION_STRICT = """あなたは【参考資料】の内容だけを根拠に回答する専用アシスタントです。\
参考資料(選択された参照フォルダ・添付ファイル)に書かれていないことは、一般知識やその他の情報で補ってはいけません。

ルール:
1. 回答は必ず【参考資料】に書かれている内容のみに基づくこと。資料に無い事柄を推測や一般知識で補わない。
2. 回答の該当箇所には、文末に「(出典: ファイル名 場所)」の形式で出典を示す。
3. 専門用語・数値・条項・固有名詞は、資料の原文どおり正確に引用する。
4. 質問の答えが【参考資料】内に見つからない場合は、無理に答えず「参考資料内には、その内容に関する記載が見つかりませんでした。」とだけ回答する(資料外の知識で答えない)。

【参考資料】
{context}
"""


# effort -> think パラメータ / 補助設定
EFFORT_LEVELS = {
    # think=思考の有無 / num_predict_boost=思考時に上乗せする出力トークン
    # (思考は num_predict を消費するため、思考時は予算を増やして回答が切れないようにする)
    "off":      {"think": False, "num_predict_boost": 0},
    "low":      {"think": False, "num_predict_boost": 0},
    "medium":   {"think": True,  "num_predict_boost": 1536},
    "high":     {"think": True,  "num_predict_boost": 3072},
    "max":      {"think": True,  "num_predict_boost": 6144},
}


def _client(timeout: Optional[float] = None):
    import ollama
    if timeout is not None:
        return ollama.Client(host=settings.ollama_host, timeout=timeout)
    return ollama.Client(host=settings.ollama_host)


def list_models() -> list[dict]:
    """インストール済みモデル一覧。失敗時は空リスト。"""
    try:
        data = _client(timeout=10).list()
        models = []
        for m in data.get("models", []):
            name = m.get("model") or m.get("name")
            if not name:
                continue
            size = m.get("size", 0)
            models.append({"name": name, "size": size})
        models.sort(key=lambda x: x["name"])
        return models
    except Exception as e:
        log.warning("モデル一覧の取得に失敗: %s", e)
        return []


def resolve_installed(name: str) -> str:
    """'qwen2.5vl' のようなタグ無し指定を、インストール済みの実タグに解決する。"""
    if not name:
        return name
    models = [m["name"] for m in list_models()]
    if name in models:
        return name
    base = name.split(":")[0]
    for m in models:
        if m.split(":")[0] == base:
            return m
    return name  # 見つからなければそのまま(Ollama側でエラー判定)


def is_model_installed(name: str) -> bool:
    if not name:
        return False
    base = name.split(":")[0]
    return any(m.split(":")[0] == base for m in (x["name"] for x in list_models()))


def is_ollama_available() -> bool:
    try:
        _client(timeout=5).list()
        return True
    except Exception:
        log.debug("is_ollama_available: 例外を無視して継続", exc_info=True)
        return False


# モデル能力(thinking/vision/tools 等)のキャッシュ。Ollama show から取得。
_CAPS_CACHE: dict = {}


def model_capabilities(name: str) -> list:
    """モデルの能力一覧(小文字)。取得できなければ []。結果はキャッシュする。"""
    if not name:
        return []
    if name in _CAPS_CACHE:
        return _CAPS_CACHE[name]
    caps: list = []
    try:
        info = _client(timeout=10).show(name)
        raw = info.get("capabilities") if isinstance(info, dict) else getattr(info, "capabilities", None)
        caps = [str(c).lower() for c in (raw or [])]
    except Exception as e:
        log.info("モデル能力の取得に失敗(%s): %s", name, e)
        caps = []
    _CAPS_CACHE[name] = caps
    return caps


def model_has_vision(name: str) -> bool:
    """モデルが画像入力(vision)対応か。能力情報が取れないときは False(=Code側の自動切替を抑制)。"""
    return "vision" in model_capabilities(name)


def supports_thinking(name: str):
    """思考(reasoning)対応か。True=対応 / False=非対応 / None=不明(判定材料なし)。"""
    caps = model_capabilities(name)
    if not caps:
        return None                 # 不明 → 呼び出し側は思考を無効化しない(安全側)
    return "thinking" in caps


# コンテキスト超過を防ぐための上限(参照件数を ∞ にしても溢れないように)
RAG_CONTEXT_CHAR_BUDGET = 12000   # 参考資料ブロックの最大文字数(num_ctx 連動時の上限)
MAX_HISTORY_MESSAGES = 30         # 文脈に含める直近の発話数

# num_ctx(トークン)から「収まる文字数」を見積もる係数。日本語/コード混在で 1トークンに収まる
# 文字数を安全側(小さめ)に見積もり、コンテキスト超過時に Ollama が先頭(system=RAG指示・
# 参考資料)を静かに切り捨てて精度が落ちるのを防ぐ。
_CHARS_PER_TOKEN = 0.85
_CONTEXT_RESERVE_CHARS = 2500     # system+スタイル/書式ガイド等で常に確保する文字数


def context_char_budget(num_ctx: int, num_predict: int = 1024) -> int:
    """num_ctx から、参考資料ブロックに割ける文字数を見積もる。

    num_ctx<=0(モデル既定で不明)のときは従来の保守値 RAG_CONTEXT_CHAR_BUDGET。
    回答(num_predict)と system ぶんを差し引き、上限は RAG_CONTEXT_CHAR_BUDGET。
    """
    try:
        n = int(num_ctx)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return RAG_CONTEXT_CHAR_BUDGET
    total = int(n * _CHARS_PER_TOKEN)
    reserve = _CONTEXT_RESERVE_CHARS + int(max(0, num_predict) * _CHARS_PER_TOKEN)
    return max(1500, min(RAG_CONTEXT_CHAR_BUDGET, total - reserve))


def trim_history(history: list[dict], max_messages: int = MAX_HISTORY_MESSAGES,
                 max_chars: int = 0) -> list[dict]:
    """会話履歴を直近 max_messages 件かつ合計 max_chars 文字以内に収める(新しい発話を優先)。

    max_chars<=0 のときは文字数制限なし(件数のみ)= 従来動作。
    """
    hist = [m for m in (history or []) if m.get("role") in ("user", "assistant")]
    hist = hist[-max_messages:]
    if max_chars and max_chars > 0:
        out: list[dict] = []
        total = 0
        for m in reversed(hist):
            c = len(m.get("content") or "")
            if out and total + c > max_chars:
                break
            out.append(m)
            total += c
        hist = list(reversed(out))
    return hist


def build_context_block(hits: list[dict], max_chars: int = RAG_CONTEXT_CHAR_BUDGET) -> str:
    """関連度順のヒットを、文字数上限に収まる範囲で連結する。"""
    blocks: list[str] = []
    total = 0
    used = 0
    for i, h in enumerate(hits, 1):
        loc = f" {h['loc']}" if h.get("loc") else ""
        blk = f"[資料{i}] (出典: {h['source']}{loc})\n{h['text']}"
        if blocks and total + len(blk) > max_chars:
            break
        blocks.append(blk)
        total += len(blk)
        used = i
    if used < len(hits):
        blocks.append(f"...(コンテキスト上限のため、関連度の高い {used} 件のみ使用。全 {len(hits)} 件中)")
    return "\n\n".join(blocks)


def build_messages(system_prompt: str, history: list[dict], hits: list[dict],
                   strict: bool = False,
                   max_context_chars: Optional[int] = None,
                   diagram_hint: Optional[bool] = None,
                   num_ctx: int = 0, num_predict: int = 1024) -> list[dict]:
    """system + 履歴からOllama用messagesを組み立てる。

    - strict=True(参照フォルダ選択時): 参考資料の内容だけで回答する厳格指示を必ず付与。
      ヒットが無くても付与し、「資料内に記載なし」と答えさせる(一般知識で答えない)。
    - strict=False: ヒットがあるときだけ通常のRAG指示を付与(一般知識での補完を許容)。
    - diagram_hint: True=詳細な図ガイド / False=簡潔な整形ガイド / None=従来どおり詳細。
      図が不要な通常QAでは簡潔ガイドにして根拠提示に集中させ、コンテキストも節約する。
    参考資料・履歴はコンテキスト超過を防ぐため上限でトリムする。
    """
    # 参考資料の文字数予算は num_ctx から逆算(明示指定が無いとき)。コンテキスト超過時に
    # Ollama が先頭(system=RAG指示)から暗黙に切り捨てて精度が落ちるのを防ぐ。
    if max_context_chars is None:
        max_context_chars = context_char_budget(num_ctx, num_predict)
    sys_text = (system_prompt or DEFAULT_SYSTEM_PROMPT).strip()
    # ペルソナを上書きされてもスタイル補正・出力フォーマットは常に効かせる。
    # 図ガイドは意図に応じて詳細/簡潔を切り替える(None は後方互換で詳細)。
    fmt_guide = MARKDOWN_GUIDE_BRIEF if diagram_hint is False else MARKDOWN_MERMAID_GUIDE
    sys_text = sys_text + "\n\n" + STYLE_GUIDE + "\n\n" + fmt_guide
    if strict:
        context = (build_context_block(hits, max_context_chars) if hits
                   else "(参照フォルダ内に該当する資料が見つかりませんでした)")
        sys_text = sys_text + "\n\n" + RAG_INSTRUCTION_STRICT.format(context=context)
    elif hits:
        sys_text = sys_text + "\n\n" + RAG_INSTRUCTION.format(
            context=build_context_block(hits, max_context_chars))
    messages = [{"role": "system", "content": sys_text}]
    # 履歴も num_ctx に収まるよう、件数に加えて文字数でも直近を優先して詰める
    # (num_ctx 不明=0 のときは従来どおり件数のみ)。
    hist_chars = 0
    if int(num_ctx or 0) > 0:
        hist_chars = max(1000, int(int(num_ctx) * _CHARS_PER_TOKEN) - _CONTEXT_RESERVE_CHARS
                         - int(max_context_chars) - int(max(0, num_predict) * _CHARS_PER_TOKEN))
    for m in trim_history(history, MAX_HISTORY_MESSAGES, hist_chars):
        messages.append({"role": m["role"], "content": m["content"]})
    return messages


# ============================================================
#  会話履歴をふまえたクエリ書き換え(多ターン検索の改善)
# ============================================================
_FOLLOWUP_HINT = re.compile(
    r"(それ|その|これ|この|あれ|あの|どれ|そこ|ここ|同じ|前述|上記|先ほど|さっき|"
    r"続き|もっと|詳しく|具体的|なぜ|理由|つまり|では|じゃあ|他には|以外)")


def should_rewrite_query(history: list[dict], query: str) -> bool:
    """直近に応答があり、かつ追問らしい(短い/指示語を含む)ときだけ書き換える。"""
    if not any(m.get("role") == "assistant" for m in (history or [])):
        return False
    q = (query or "").strip()
    if len(q) <= 24:
        return True
    return bool(_FOLLOWUP_HINT.search(q))


def build_rewrite_prompt(history: list[dict], query: str, max_turns: int = 6) -> str:
    """直近のやり取りを添えて『自立した検索クエリ』を作らせるプロンプト。"""
    turns = [m for m in (history or []) if m.get("role") in ("user", "assistant")][-max_turns:]
    lines = []
    for m in turns:
        who = "ユーザー" if m.get("role") == "user" else "アシスタント"
        c = " ".join((m.get("content") or "").split())
        if c:
            lines.append(f"{who}: {c[:300]}")
    convo = "\n".join(lines) or "(なし)"
    return (
        "次の会話の流れをふまえ、最後のユーザーの質問を、それだけで意味が通じる"
        "『検索用の独立した質問』に書き換えてください。指示語(それ・その等)は文脈から"
        "具体的な語に置き換え、新しい情報は足さないこと。前置き・説明・引用符は書かず、"
        "書き換え後の質問文だけを1行で出力してください。\n\n"
        f"会話:\n{convo}\n\n最後の質問: {query}\n\n書き換え後の質問:"
    )


def rewrite_query(history: list[dict], query: str, model: str) -> str:
    """追問を、履歴をふまえた独立クエリに書き換える。対象外/失敗時は元のクエリを返す。"""
    if not model or not should_rewrite_query(history, query):
        return query
    try:
        out = complete_text(build_rewrite_prompt(history, query), model,
                            num_predict=80, temperature=0.0)
    except Exception:
        log.debug("rewrite_query: 例外を無視して継続", exc_info=True)
        return query
    out = " ".join((out or "").split()).strip(' 　"\'「」『』')
    if not out or len(out) > 200:
        return query
    return out


def _extract(part, key: str) -> Optional[str]:
    """ChatResponse(オブジェクト/辞書)から message.<key> を安全に取り出す。"""
    msg = getattr(part, "message", None)
    if msg is None and isinstance(part, dict):
        msg = part.get("message")
    if msg is None:
        return None
    val = getattr(msg, key, None)
    if val is None and isinstance(msg, dict):
        val = msg.get(key)
    return val


def chat_stream(messages: list[dict], model: str, *,
                temperature: float = 0.3, top_p: float = 0.9,
                num_predict: int = 1024, num_ctx: Optional[int] = None,
                effort: str = "medium") -> Iterator[dict]:
    """
    Ollama でストリーミング生成。イベントを順次 yield:
      {"type": "thinking", "text": ...}  -- 思考過程
      {"type": "content",  "text": ...}  -- 本文
    """
    eff = EFFORT_LEVELS.get((effort or "medium").lower(), EFFORT_LEVELS["medium"])
    think = bool(eff["think"])
    # モデル別最適化: 「思考」を確実に非対応なら最初から思考しない(無駄な失敗→再試行を回避)。
    # 不明(None)/対応(True)のときは従来どおり思考を試す(下の例外フォールバックが安全網)。
    if think and supports_thinking(model) is False:
        think = False
    np = int(num_predict)
    boost = eff["num_predict_boost"] if think else 0   # 思考するときだけ予算を上乗せ
    options = {
        "temperature": float(temperature),
        "top_p": float(top_p),
        # num_predict<=0 は「上限なし」(EOSまで生成。途中で切れない)。
        "num_predict": -1 if np <= 0 else np + boost,
    }
    if num_ctx:
        options["num_ctx"] = int(num_ctx)

    client = _client()

    def _run(use_think: Optional[bool]):
        kwargs = dict(model=model, messages=messages, stream=True, options=options)
        if use_think is not None:
            kwargs["think"] = use_think
        return client.chat(**kwargs)

    started = False
    try:
        try:
            stream = _run(think)
        except TypeError:
            # 古い ollama-python は think 未対応
            log.debug("chat_stream: 例外を無視して継続", exc_info=True)
            stream = _run(None)

        for part in stream:
            th = _extract(part, "thinking")
            if th:
                started = True
                yield {"type": "thinking", "text": th}
            ct = _extract(part, "content")
            if ct:
                started = True
                yield {"type": "content", "text": ct}
    except Exception as e:
        msg = str(e).lower()
        # think 非対応モデル -> think を外して再試行(まだ何も出力していない場合のみ)
        if (not started) and think and ("think" in msg or "thinking" in msg):
            log.info("このモデルは thinking 非対応のため通常生成に切替: %s", model)
            stream = _run(None)
            for part in stream:
                ct = _extract(part, "content")
                if ct:
                    yield {"type": "content", "text": ct}
        else:
            raise


def vision_complete(image_b64s: list[str], instruction: str, model: str, *,
                    temperature: float = 0.1, num_predict: int = 512,
                    num_ctx: Optional[int] = None) -> str:
    """画像 + 指示文を Vision モデルに渡し、応答テキストをまとめて返す(非ストリーミング)。

    OCR API 用。instruction に「画像の文字を読み取って」「購入数量を数字で返信」など
    具体的な指示を入れると、その指示に沿った結果(整形・判断済み)が返る。
    """
    if not image_b64s:
        raise ValueError("画像がありません")
    options = {"temperature": float(temperature), "num_predict": int(num_predict)}
    if num_ctx:
        options["num_ctx"] = int(num_ctx)
    messages = [{"role": "user", "content": instruction, "images": list(image_b64s)}]
    resp = _client().chat(model=model, messages=messages, stream=False, options=options)
    content = _extract(resp, "content")
    return content or ""


def complete_text(prompt: str, model: str, *, system: str = "",
                  num_predict: int = 64, temperature: float = 0.2,
                  num_ctx: Optional[int] = None) -> str:
    """短い非ストリーミング生成(会話タイトル等)。思考は使わない。失敗時は ''。

    num_ctx を渡すと、長いプロンプト(リランク採点など)でも先頭が切り捨てられないよう
    コンテキスト長を確保する。
    """
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    opts = {"temperature": float(temperature), "num_predict": int(num_predict)}
    if num_ctx and int(num_ctx) > 0:
        opts["num_ctx"] = int(num_ctx)
    try:
        try:
            resp = _client(timeout=60).chat(model=model, messages=msgs, stream=False, think=False, options=opts)
        except TypeError:                       # 古い ollama-python は think 未対応
            log.debug("complete_text: 例外を無視して継続", exc_info=True)
            resp = _client(timeout=60).chat(model=model, messages=msgs, stream=False, options=opts)
        return (_extract(resp, "content") or "").strip()
    except Exception as e:
        log.info("complete_text 失敗: %s", e)
        return ""
