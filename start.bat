@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  社内文書アシスタント  セットアップ ＆ 起動
echo ============================================================
echo.

REM --- Python の確認 ---
python --version >nul 2>&1
if errorlevel 1 (
  echo [エラー] python コマンドが見つかりません。
  echo   Python 3.10 以上をインストールし、インストール時に
  echo   「Add Python to PATH」にチェックを入れてください。
  echo   https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

echo [1/2] 必要なパッケージを確認・インストールしています...
echo       (初回は torch 等のダウンロードで時間がかかります)
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [エラー] パッケージのインストールに失敗しました。
  pause
  exit /b 1
)

echo.
echo [2/2] アプリを起動します。表示されるURLをブラウザで開いてください。
echo       終了するには、このウィンドウで Ctrl+C を押してください。
echo.
python run.py

echo.
echo アプリが終了しました。
pause
