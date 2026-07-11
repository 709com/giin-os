# -*- coding: utf-8 -*-
"""選挙アナリスト：月次（選挙前6ヶ月は週次）の3層情勢レポート"""
import datetime
from pathlib import Path
from common import claude, line_push

TODAY = datetime.date.today()
ELECTION_DATE = datetime.date(2027, 4, 25)  # 仮置き。正式日程が出たら更新
INTENSIVE = (ELECTION_DATE - TODAY).days <= 180
# 通常期は第1月曜のみ実行（週次起動される前提での自己判定）
if not INTENSIVE and not (TODAY.weekday() == 0 and TODAY.day <= 7):
    print("通常期・第1月曜以外のためスキップ"); exit()

report = claude(f"""あなたは政治情勢専門のアナリストです。
クライアントは文京区議会議員・たかはまなおき（子育て・防災・まちづくり・DXが看板政策、
次回改選は2027年春の統一地方選想定）。
本日は{TODAY.isoformat()}。Web検索を使い、直近1ヶ月の動きを調査し情勢レポートを作成。

# レイヤー1：国政（分量2割）
内閣・政党支持率の最新動向（調査主体と調査日を必ず明記）／重要日程／
子育て・教育・地方財政・防災に関わる法改正や国の予算の動き

# レイヤー2：東京23区情勢（分量3割）
直近・今後の区長選や区議補選／他区で話題の政策（特に子育て・DX）／都政の波及事項

# レイヤー3：文京区情勢（分量5割）
区政・区議会の直近の動き／区内で話題の地域課題／出馬表明など選挙に関わる兆候／
【たかはまなおきへの示唆】今月やるべきこと最大3つ

# 厳守事項
すべての事実に出典（媒体名・日付）。世論調査は調査主体・時期・数値をセットで。
当落予測は「仮説」と明記。結論ファースト・2000字以内。冒頭に「今月の情勢を一言で」を1行。
{"文京区パートを最優先し他候補・他会派の動きを重点調査。" if INTENSIVE else ""}""",
    use_web=True, max_tokens=4000)

Path("reports").mkdir(exist_ok=True)
Path(f"reports/senkyo_{TODAY.isoformat()}.md").write_text(report)
mode = "【選挙前モード・週次】" if INTENSIVE else "【通常モード・月次】"
line_push(f"🗳 選挙情勢レポート {TODAY.strftime('%Y.%m.%d')}\n{mode}\n\n{report}")
