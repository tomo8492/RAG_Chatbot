"""本文に書かれた JSON ツール呼び出しの復元(_parse_text_tool_calls)の単体テスト。

gemma3 等、Ollama のネイティブ tool_calls を返さないモデルでもエージェントを動かすための
フォールバックを検証する。純関数(Ollama 非接続)。pytest でも単体実行でも動く。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import _impl   # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "list_files"}},
         {"type": "function", "function": {"name": "read_file"}},
         {"type": "function", "function": {"name": "edit_file"}}]


def test_recovers_simple_tool_call_in_fence():
    content = '調べます。\n```json\n{"name": "list_files", "arguments": {}}\n```'
    out = _impl._parse_text_tool_calls(content, TOOLS)
    assert len(out) == 1
    assert out[0].function.name == "list_files" and out[0].function.arguments == {}


def test_recovers_arguments_dict():
    out = _impl._parse_text_tool_calls('{"name": "read_file", "arguments": {"path": "a.py", "offset": 10}}', TOOLS)
    assert out[0].function.name == "read_file"
    assert out[0].function.arguments == {"path": "a.py", "offset": 10}


def test_recovers_tool_call_wrapper_and_parameters():
    out = _impl._parse_text_tool_calls('{"tool_call": {"name": "list_files", "parameters": {}}}', TOOLS)
    assert len(out) == 1 and out[0].function.name == "list_files"


def test_ignores_unknown_name():
    assert _impl._parse_text_tool_calls('{"name": "John", "arguments": {}}', TOOLS) == []


def test_ignores_plain_prose():
    assert _impl._parse_text_tool_calls("ただの説明文です(JSONなし)。", TOOLS) == []


def test_recovers_multiple_and_braces_in_strings():
    content = ('まず\n{"name":"read_file","arguments":{"path":"x"}}\n'
               'つぎに\n{"name":"edit_file","arguments":{"path":"x","old_string":"a{b}c","new_string":"d"}}')
    out = _impl._parse_text_tool_calls(content, TOOLS)
    assert [c.function.name for c in out] == ["read_file", "edit_file"]
    assert out[1].function.arguments["old_string"] == "a{b}c"   # 文字列内の波括弧も正しく抽出


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
