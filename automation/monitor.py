# -*- coding: utf-8 -*-
"""毎朝巡回：庁議資料・委員会会議録速報版の新着検出→記事生成→LINE通知"""
import json, hashlib, requests
from pathlib import Path
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from common import claude, line_push, UA

TARGETS = {
    "chougi": {
        "url": "https://www.city.bunkyo.lg.jp/kuseijouhou/keikaku/chougi/index.html",
        "label": "庁議資料",
        "prompt": """あなたは文京区議会議員たかはまなおきの広報スタッフです。
添付の庁議資料を読み、区民向け解説記事を作成してください。
読者：文京区の忙しい子育て世帯。中学生でもわかる表現で。
構成：①一言でいうと何が決まった/動いたか ②区民の生活にどう関係するか
③たかはまなおきの視点（子育て・防災・DXの観点で注目点を最大3つ）④今後のスケジュール
末尾に「※本記事は公開資料をもとにした解説です。詳細は文京区公式サイトをご確認ください」と必ず入れる。
800字程度。X投稿用の140字要約も1本つける。""",
    },
    "iinkai": {
        "url": "https://www.city.bunkyo.lg.jp/kugikai/p007084.html",
        "label": "委員会会議録（速報版）",
        "prompt": """あなたは文京区議会議員たかはまなおきの広報スタッフです。
添付の委員会会議録（速報版）を読み、「白熱した議論ピックアップ」記事を作成してください。
選定基準：①複数会派が発言した論点 ②理事者答弁が具体的に動いた（または明確に拒否した）論点
③区民生活への影響が大きい論点。この基準で最大3つ選ぶ。
各論点：見出し／何が争点か／主なやりとりの要約（発言の趣旨。逐語引用はしない）／今後の注目点。
末尾に「※本記事は会議録速報版をもとにした要約です。正確な発言内容は会議録（確定版）をご参照ください」と必ず入れる。
1000字程度。X投稿用の140字要約も1本つける。""",
    },
}
STATE = Path("state.json")
OUT = Path("articles"); OUT.mkdir(exist_ok=True)

def get_links(url):
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status()
    soup = BeautifulSoup(r.content, "html.parser")
    return {urljoin(url, a["href"]): a.get_text(strip=True)
            for a in soup.select("a[href]")
            if a["href"].lower().endswith(".pdf") or "/p0" in a["href"]}

def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    for key, t in TARGETS.items():
        try:
            links = get_links(t["url"])
        except Exception as e:
            line_push(f"⚠️ {t['label']}の巡回に失敗しました: {e}\n（区サイトのbot対策の可能性。手動確認を）")
            continue
        known = set(state.get(key, []))
        first_run = key not in state
        new = {u: ttl for u, ttl in links.items() if u not in known}
        if first_run:
            line_push(f"✅ {t['label']}の巡回を開始しました（現在{len(links)}リンクを記録。次回から新着のみ通知します）")
        else:
            for url, title in list(new.items())[:3]:
                body = f"🆕 {t['label']}に新着\n{title}\n{url}"
                if url.lower().endswith(".pdf"):
                    try:
                        pdf = requests.get(url, headers=UA, timeout=60).content
                        art = claude(f"資料名：{title}\n\n{t['prompt']}", pdf_bytes=pdf)
                        f = OUT / f"{key}_{hashlib.md5(url.encode()).hexdigest()[:8]}.md"
                        f.write_text(f"# {title}\n出典: {url}\n\n{art}")
                        body += f"\n\n📝 記事ドラフト生成済み → {f.name}\n\n---\n{art[:1500]}"
                    except Exception as e:
                        body += f"\n（記事生成は失敗: {e}）"
                line_push(body)
        state[key] = list(links.keys())
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
