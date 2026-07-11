# -*- coding: utf-8 -*-
"""議員OS共通部品：LINE送信・Claude呼び出し"""
import os, requests
import anthropic

UA = {"User-Agent": "GiinOS/1.0 (takahama office; contact: SET_YOUR_EMAIL)"}

def claude(prompt, use_web=False, pdf_bytes=None, max_tokens=3000):
    """Claude APIを1回呼び、テキストを返す"""
    client = anthropic.Anthropic()
    content = []
    if pdf_bytes:
        import base64
        content.append({"type": "document", "source": {"type": "base64",
            "media_type": "application/pdf",
            "data": base64.b64encode(pdf_bytes).decode()}})
    content.append({"type": "text", "text": prompt})
    kw = {}
    if use_web:
        kw["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
    msg = client.messages.create(model="claude-sonnet-4-6",
        max_tokens=max_tokens, messages=[{"role": "user", "content": content}], **kw)
    return "".join(b.text for b in msg.content if b.type == "text")

def line_push(text):
    """本人のLINEにだけ送る（プレビュー・レポート用）"""
    token = os.environ.get("LINE_CHANNEL_TOKEN")
    uid = os.environ.get("LINE_USER_ID")
    if not token or not uid:
        print(text); return
    for i in range(0, min(len(text), 14000), 4900):
        requests.post("https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {token}"},
            json={"to": uid, "messages": [{"type": "text", "text": text[i:i+4900]}]})

def line_broadcast(messages):
    """全友だちへ一斉配信（承認後のみ使用）"""
    token = os.environ["LINE_CHANNEL_TOKEN"]
    r = requests.post("https://api.line.me/v2/bot/message/broadcast",
        headers={"Authorization": f"Bearer {token}"}, json={"messages": messages})
    print("broadcast:", r.status_code, r.text)
