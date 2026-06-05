# OCR API (`/api/ocr`)

画像ファイルのパス（またはbase64）と「指示文」を送ると、ローカルの Vision モデルで
画像を読み取り、**指示に沿った結果**を返す API です。VBA / Python など、HTTP を投げられる
環境ならどこからでも呼べます。

- アプリ本体（`python run.py`）が動いていれば同時に有効になります。
- 既定の待ち受け: `http://localhost:8800/api/ocr`

---

## リクエスト

`POST /api/ocr`  (Content-Type: `application/json`)

| パラメータ | 必須 | 説明 |
|---|---|---|
| `path` | △ | サーバ（アプリを動かすPC）上の画像ファイルパス。`image_b64` を使う場合は不要 |
| `image_b64` | △ | 画像の base64 / data URL。`path` の代わりに直接渡す場合に使用 |
| `instruction` | 任意 | 読み取り後の指示。空なら「全文OCR」。例: `購入数量を数字だけで返信` |
| `model` | 任意 | 使うモデル名。空なら設定の Vision モデル（`VISION_MODEL` / 設定画面） |
| `num_predict` | 任意 | 応答の最大トークン（既定 512） |
| `temperature` | 任意 | 既定 0.1（OCRは低めが安定） |

`path` か `image_b64` の**どちらか一方**は必須です。

### 認証
`CHAT_PASSWORD` を設定して認証を有効にしている場合のみ必要です。
`.env` の `OCR_API_KEY` に任意のキーを設定し、リクエストヘッダ `X-API-Key` に同じ値を入れて送ります。
（認証を使っていない＝パスワード未設定なら、ヘッダ不要でそのまま呼べます。）

---

## レスポンス

```json
{ "ok": true, "model": "qwen2.5vl:7b", "result": "42" }
```

`result` に、指示に沿ったテキストが入ります。

エラー時は HTTP ステータス＋ `{"detail": "..."}`（FastAPI標準）で返ります。
代表例:
- `400 モデル『glm-ocr』は画像入力に対応していません...` → そのモデルは Vision 非対応。`qwen2.5vl` 等を指定。
- `404 ファイルが見つかりません` → `path` を確認。
- `503 Ollama に接続できません` → `ollama serve` を確認。

---

## ⚠️ モデルについて（重要）

`glm-ocr`（`ollama show` の Capabilities が `completion` のみ）は**画像入力に対応していません**。
このAPIで画像を読むには、Vision 対応モデルが必要です。次のいずれかを推奨します。

```bash
ollama pull qwen2.5vl:7b      # OCRに強く軽量。日本語も良好
# ollama pull llama3.2-vision
# ollama pull gemma3:27b
```

`model` パラメータで都度指定するか、設定画面の「画像認識モデル」/ `.env` の `VISION_MODEL` を
これらに設定してください。

---

## 使い方の例

### Python

```python
import requests

def ocr(path, instruction="", model="", base_url="http://localhost:8800", api_key=""):
    headers = {"X-API-Key": api_key} if api_key else {}
    r = requests.post(f"{base_url}/api/ocr", headers=headers, json={
        "path": path,
        "instruction": instruction,
        "model": model,
    }, timeout=120)
    r.raise_for_status()
    return r.json()["result"]

# 全文OCR
print(ocr(r"C:\work\伝票.png"))

# 内容に基づく判断 + フォーマット指定
qty = ocr(r"C:\work\伝票.png", instruction="購入数量を半角数字だけで返信。数量が無ければ 0")
print(qty)   # 例: 42
```

### Excel VBA

`modules/OcrClient.bas` をインポートするか、以下を標準モジュールに貼り付けて使います。

```vba
' セル参照例: =OcrFile(A1, "購入数量を半角数字だけで返信")
Public Function OcrFile(ByVal filePath As String, _
                        Optional ByVal instruction As String = "", _
                        Optional ByVal model As String = "") As String
    Dim http As Object, body As String, json As String
    Set http = CreateObject("MSXML2.XMLHTTP")

    ' JSON 文字列を組み立て(パスの \ をエスケープ)
    body = "{""path"":""" & JsonEscape(filePath) & """," & _
           """instruction"":""" & JsonEscape(instruction) & """," & _
           """model"":""" & JsonEscape(model) & """}"

    http.Open "POST", "http://localhost:8800/api/ocr", False
    http.setRequestHeader "Content-Type", "application/json"
    ' 認証を有効にしている場合は次の行のコメントを外し、キーを設定
    ' http.setRequestHeader "X-API-Key", "ここにOCR_API_KEYの値"
    http.send body

    If http.Status = 200 Then
        json = http.responseText
        OcrFile = ExtractJsonValue(json, "result")
    Else
        OcrFile = "ERROR " & http.Status & ": " & http.responseText
    End If
End Function

' " と \ と改行を JSON 用にエスケープ
Private Function JsonEscape(ByVal s As String) As String
    s = Replace(s, "\", "\\")
    s = Replace(s, """", "\""")
    s = Replace(s, vbCr, "")
    s = Replace(s, vbLf, "\n")
    JsonEscape = s
End Function

' 簡易 JSON 値取り出し( "key":"値" の値部分。\" を含む値にも一応対応 )
Private Function ExtractJsonValue(ByVal json As String, ByVal key As String) As String
    Dim marker As String, p As Long, i As Long, ch As String, out As String
    marker = """" & key & """:"
    p = InStr(json, marker)
    If p = 0 Then ExtractJsonValue = "": Exit Function
    p = p + Len(marker)
    Do While Mid(json, p, 1) = " ": p = p + 1: Loop
    If Mid(json, p, 1) <> """" Then ExtractJsonValue = "": Exit Function
    p = p + 1
    i = p
    Do While i <= Len(json)
        ch = Mid(json, i, 1)
        If ch = "\" Then
            Dim nx As String: nx = Mid(json, i + 1, 1)
            Select Case nx
                Case "n": out = out & vbLf
                Case "t": out = out & vbTab
                Case """": out = out & """"
                Case "\": out = out & "\"
                Case Else: out = out & nx
            End Select
            i = i + 2
        ElseIf ch = """" Then
            Exit Do
        Else
            out = out & ch
            i = i + 1
        End If
    Loop
    ExtractJsonValue = out
End Function
```

#### VBA 使用例
```vba
Sub Test()
    Dim s As String
    s = OcrFile("C:\work\伝票.png", "購入数量を半角数字だけで返信")
    MsgBox s
End Sub
```
セル数式としても使えます: `=OcrFile(A1, "合計金額を数字だけで")`

> 補足: 画像のパスは**アプリ(サーバ)を動かしているPCから見えるパス**です。VBAと同じPCで
> アプリを動かしていれば、そのままローカルパスでOKです。
