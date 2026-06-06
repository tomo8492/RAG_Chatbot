"""tests/ 配下の test_*.py をまとめて実行する簡易ランナー。

pytest が無い環境でも `python tests/run_all.py` でひと通り回せる。
各テストファイルは個別の `__main__` ランナーを持つので、サブプロセスとして
順に実行し、終了コードを集計する。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    files = sorted(
        f for f in os.listdir(HERE)
        if f.startswith("test_") and f.endswith(".py")
    )
    failed = []
    for f in files:
        print(f"\n===== {f} =====")
        r = subprocess.run([sys.executable, os.path.join(HERE, f)])
        if r.returncode != 0:
            failed.append(f)
    print("\n=============================")
    if failed:
        print(f"NG: {len(failed)} ファイル失敗 -> {', '.join(failed)}")
        return 1
    print(f"OK: {len(files)} ファイルすべて成功")
    return 0


if __name__ == "__main__":
    sys.exit(main())
