"""長文コードファイルの読み込み(read_file)が破綻しないことの検証。

大きなファイルを実際に生成し、行範囲(offset/limit)の窓送り・文字上限・
「続きは offset=…」での全文到達・1行が極端に長いファイル・末尾到達/範囲外を確認する。
Ollama 等には接続しない(純粋なファイル操作)。pytest/単体実行両対応。
"""
import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import agent                                   # noqa: E402
from app.agent.constants import READ_DEFAULT_LINES, READ_CHAR_CAP   # noqa: E402


@contextlib.contextmanager
def workspace():
    d = tempfile.mkdtemp(prefix="readbig_")
    try:
        yield Path(d).resolve()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _note_offset(text: str):
    """結果末尾の『続きは offset=N』から N を取り出す(無ければ None)。"""
    import re
    m = re.search(r"続きは offset=(\d+)", text)
    return int(m.group(1)) if m else None


def test_large_file_paginates_to_end_without_loss():
    """5000行のコードを read_file の案内に従って読み進め、全行に到達できる。"""
    with workspace() as ws:
        lines = [f"line_{i} = {i}  # コメント{i}" for i in range(1, 5001)]
        agent.t_write_file(ws, "big.py", "\n".join(lines))
        seen = set()
        offset = 1
        for _ in range(200):   # 無限ループ防止
            out = agent.t_read_file(ws, "big.py", offset=offset, limit=400)
            assert not out.startswith("[エラー]"), out
            for ln in out.splitlines():
                if ln.startswith("line_"):
                    seen.add(int(ln.split("_", 1)[1].split(" ", 1)[0]))
            nxt = _note_offset(out)
            if nxt is None:
                break
            assert nxt > offset       # 必ず前進する(停滞しない)
            offset = nxt
        # 5000行すべてに到達(取りこぼしなし)
        assert len(seen) == 5000, f"到達 {len(seen)}/5000"


def test_default_window_and_continue_note():
    with workspace() as ws:
        agent.t_write_file(ws, "n.txt", "\n".join(str(i) for i in range(1, 2001)))
        out = agent.t_read_file(ws, "n.txt")   # 既定 800 行
        body = [ln for ln in out.splitlines() if ln.isdigit()]
        assert body[0] == "1" and len(body) == READ_DEFAULT_LINES
        assert _note_offset(out) == READ_DEFAULT_LINES + 1   # 続きの案内が正しい


def test_char_cap_truncates_long_lines_file():
    """1行が非常に長いファイルでも、文字上限で安全に切り、案内を出す。"""
    with workspace() as ws:
        # 1行 = 5万字 を 3行(行数は少ないが文字数が巨大)
        agent.t_write_file(ws, "wide.txt", "\n".join("x" * 50000 for _ in range(3)))
        out = agent.t_read_file(ws, "wide.txt", offset=1, limit=10)
        assert len(out) <= READ_CHAR_CAP + 200          # 上限+案内文の余白
        assert "文字で省略" in out


def test_offset_out_of_range_is_clear_error():
    with workspace() as ws:
        agent.t_write_file(ws, "s.txt", "a\nb\nc")
        out = agent.t_read_file(ws, "s.txt", offset=999)
        assert out.startswith("[エラー]") and "範囲外" in out


def test_tail_window_has_no_continue_note():
    with workspace() as ws:
        agent.t_write_file(ws, "m.txt", "\n".join(str(i) for i in range(1, 101)))
        out = agent.t_read_file(ws, "m.txt", offset=90, limit=50)   # 末尾まで
        assert "100" in out and _note_offset(out) is None           # 続き案内なし=末尾到達


def test_grep_finds_match_deep_in_large_file():
    """大きいファイルでも grep が末尾付近の一致を行番号つきで見つける。"""
    with workspace() as ws:
        lines = [f"x{i}" for i in range(1, 4000)] + ["NEEDLE_TOKEN here"] + ["y"]
        agent.t_write_file(ws, "code.py", "\n".join(lines))
        out = agent.t_grep(ws, "NEEDLE_TOKEN")
        assert "code.py:4000:" in out and "NEEDLE_TOKEN" in out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERROR {fn.__name__}: {e!r}")
    print(f"{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
