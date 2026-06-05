Attribute VB_Name = "OcrClient"
' ============================================================
'  OcrClient.bas
'  /api/ocr を呼び出す Excel VBA クライアント。
'  この .bas を VBE で「ファイル > ファイルのインポート」で取り込んで使います。
'
'  例(セル数式):   =OcrFile(A1, "購入数量を半角数字だけで返信")
'  例(マクロ):     s = OcrFile("C:\work\伝票.png", "合計金額を数字だけで")
' ============================================================
Option Explicit

' 必要なら変更。アプリ(サーバ)の待ち受けURL。
Private Const OCR_URL As String = "http://localhost:8800/api/ocr"
' 認証(CHAT_PASSWORD)を有効にしている場合のみ、.env の OCR_API_KEY と同じ値を設定。
' 認証を使っていなければ空のままでOK。
Private Const OCR_API_KEY As String = ""


' 画像パス + 指示文 を送り、OCR/判断結果を返す。
Public Function OcrFile(ByVal filePath As String, _
                        Optional ByVal instruction As String = "", _
                        Optional ByVal model As String = "") As String
    Dim http As Object, body As String
    On Error GoTo Failed
    Set http = CreateObject("MSXML2.XMLHTTP")

    body = "{""path"":""" & JsonEscape(filePath) & """," & _
           """instruction"":""" & JsonEscape(instruction) & """," & _
           """model"":""" & JsonEscape(model) & """}"

    http.Open "POST", OCR_URL, False
    http.setRequestHeader "Content-Type", "application/json"
    If Len(OCR_API_KEY) > 0 Then
        http.setRequestHeader "X-API-Key", OCR_API_KEY
    End If
    http.send body

    If http.Status = 200 Then
        OcrFile = ExtractJsonValue(http.responseText, "result")
    Else
        OcrFile = "ERROR " & http.Status & ": " & http.responseText
    End If
    Exit Function
Failed:
    OcrFile = "ERROR: " & Err.Description
End Function


' " \ 改行 を JSON 用にエスケープ
Private Function JsonEscape(ByVal s As String) As String
    s = Replace(s, "\", "\\")
    s = Replace(s, """", "\""")
    s = Replace(s, vbCr, "")
    s = Replace(s, vbLf, "\n")
    JsonEscape = s
End Function


' 簡易 JSON 値取り出し( "key":"値" の値。\" \n \\ などに対応 )
Private Function ExtractJsonValue(ByVal json As String, ByVal key As String) As String
    Dim marker As String, p As Long, i As Long, ch As String, nx As String, out As String
    marker = """" & key & """:"
    p = InStr(json, marker)
    If p = 0 Then ExtractJsonValue = "": Exit Function
    p = p + Len(marker)
    Do While Mid(json, p, 1) = " "
        p = p + 1
    Loop
    If Mid(json, p, 1) <> """" Then ExtractJsonValue = "": Exit Function
    i = p + 1
    Do While i <= Len(json)
        ch = Mid(json, i, 1)
        If ch = "\" Then
            nx = Mid(json, i + 1, 1)
            Select Case nx
                Case "n": out = out & vbLf
                Case "t": out = out & vbTab
                Case """": out = out & """"
                Case "\": out = out & "\"
                Case "/": out = out & "/"
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
