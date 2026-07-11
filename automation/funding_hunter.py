# -*- coding: utf-8 -*-
"""財源ハンター：毎月第3月曜「文京区がまだ取っていないお金」リスト"""
import datetime
from pathlib import Path
from common import claude, line_push

TODAY = datetime.date.today()
if not (TODAY.weekday() == 0 and 15 <= TODAY.day <= 21):
    print("第3月曜以外のためスキップ"); exit()

report = claude(f"""あなたは自治体財政の専門家です。Web検索を使い、直近2ヶ月に公表された
以下の情報を調査し「文京区がまだ取っていない可能性のあるお金」リストを作成。本日は{TODAY.isoformat()}。

# 調査対象
1. 国の新設・拡充補助金：こども家庭庁、文部科学省、総務省（地域DX）、国土交通省、内閣府（地方創生）
2. 東京都の新設・拡充補助制度、区市町村向けモデル事業の公募
3. 他の23区が最近獲得・活用を発表した補助金・交付金

# 出力形式（各件）
制度名／所管／金額規模・補助率／申請期限／対象事業／
文京区の活用状況（「活用済み」「未活用の可能性」「不明」を根拠つきで）／
議会質問への変換案（「○○補助金を活用し△△を実施すべきでは」の1行）

# 厳守事項
出典（省庁・都の公式ページURL）のない制度は書かない。期限切れは除外し期限が近い順。
最大10件。冒頭に「今月の一押し財源」を1件選び理由を添える。""",
    use_web=True, max_tokens=4000)

Path("reports").mkdir(exist_ok=True)
Path(f"reports/zaigen_{TODAY.isoformat()}.md").write_text(report)
line_push(f"💰 財源ハンター月次レポート {TODAY.strftime('%Y.%m')}\n\n{report}")
