# -*- coding: utf-8 -*-
"""承認ボタン後に実行：draft.txtを全友だちへ一斉配信（画像つき）"""
import os
from pathlib import Path
from common import line_broadcast

text = Path("draft.txt").read_text()
msgs = []
url = os.environ.get("BANNER_URL", "")
if url:
    msgs.append({"type": "image", "originalContentUrl": url, "previewImageUrl": url})
msgs.append({"type": "text", "text": text})
line_broadcast(msgs)
