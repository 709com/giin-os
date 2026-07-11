# -*- coding: utf-8 -*-
"""毎週木曜：コラム下書き生成→バナー作成→本人にプレビュー送信"""
import os, glob, textwrap, datetime, requests
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from common import claude, line_push

BRAND_GREEN = (74, 138, 56)
BRAND_LIGHT = (232, 242, 220)
FONT = "automation/NotoSansJP-Bold.otf"

def generate():
    mats = ""
    for f in sorted(glob.glob("articles/*.md"), reverse=True)[:5]:
        mats += Path(f).read_text()[:3000] + "\n---\n"
    if not mats:
        mats = "（今週は新着資料なし。区政の一般的な話題＝夏の子育て支援・防災の備え等から1本書く）"
    return claude(f"""あなたは文京区議会議員たかはまなおきの広報スタッフです。
以下の直近の区政情報を素材に、公式LINE配信用コラムを1本作成してください。

# 読者
文京区の忙しい子育て世帯（スマホで30秒で読める分量）

# 形式（この形式のみ出力。前置き不要）
1行目：タイトル（15字以内・絵文字1つ）
2行目：空行
本文：400字以内。①今週の区政トピック ②暮らしへの影響 ③たかはまなおきが今後やること、の順。
文体：やわらかい丁寧語。絵文字は3〜5個。
最終行：「※ご意見はこのLINEに返信でどうぞ！」

# 厳守
素材にない事実を書かない。数字には「区の資料によると」等の出所を添える。

# 素材
{mats}""").strip()

def banner(title, out="banner.jpg"):
    img = Image.new("RGB", (1040, 585), BRAND_LIGHT)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1040, 140], fill=BRAND_GREEN)
    d.rectangle([0, 445, 1040, 585], fill=BRAND_GREEN)
    fs = ImageFont.truetype(FONT, 44); fb = ImageFont.truetype(FONT, 72)
    d.text((40, 45), "たかはまなおき 区政レポート", font=fs, fill="white")
    lines = textwrap.wrap(title, width=12)
    y = 290 - len(lines) * 45
    for ln in lines:
        w = d.textlength(ln, font=fb)
        d.text(((1040 - w) / 2, y), ln, font=fb, fill=(40, 60, 30)); y += 95
    d.text((40, 490), datetime.date.today().strftime("%Y.%m.%d") + "  文京区議会議員 たかはまなおき", font=fs, fill="white")
    img.save(out, "JPEG", quality=85)

if __name__ == "__main__":
    col = generate()
    Path("draft.txt").write_text(col)
    title = col.splitlines()[0].strip()
    banner(title)
    line_push("📝【下書きプレビュー】\n配信してよければGitHubアプリで『承認して配信』を実行。\n修正したい場合はdraft.txtを編集してから実行。\n\n" + col)
