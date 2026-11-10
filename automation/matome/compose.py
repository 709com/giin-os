# -*- coding: utf-8 -*-
"""文面づくり。
議会（型1・型3・型4）は出典ページの表や一覧から決まった型で組み立てる（Claudeは使わない）。
区のお知らせ（型2）だけ Claude（Sonnet）に出典ページの本文を読ませ、事実を1〜2個抜き出させる。
どの型も最後に check_post() で約束ごと（リンク位置・絵文字なし・出典にない数字なし など）を機械的に確かめる。"""
import re, os, unicodedata
from .util import fmt_date, norm
from . import sources as S

HASHTAG_GIKAI = "#文京区議会"
HASHTAG_KU = "#文京区"
PRICE_NO_URL = 0.015     # 投稿1本（URLなし）  2026-11-05 docs.x.com で再確認
PRICE_WITH_URL = 0.200   # 投稿1本（URLあり）

BANNED_PHRASES = ("ぜひ", "ご覧ください", "お見逃しなく", "注目", "必見", "素晴らし", "嬉し", "おすすめ")
BANNED_T2 = BANNED_PHRASES + ("新たに", "始まります", "スタート")   # 区のお知らせはRSSの日付が開始日とは限らないため
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
URL_RE = re.compile(r"https?://\S+")

BOCHO_IINKAI = "傍聴は当日午前8時30分から、シビックセンター23階の区議会事務局で受付（定員15人、超えた場合は抽選）。会場は26階スカイホールです。"
BOCHO_HONKAIGI = "傍聴は当日午前8時30分から、シビックセンター23階の区議会事務局で受付（定員63人、超えた場合は抽選）。"
CHUKEI = "本会議の一般質問はインターネット中継があります。"


def is_honkaigi(name):
    return "本会議" in name


def has_stream(name):
    return "一般質問" in name or "予算審査特別委員会" in name or "決算審査特別委員会" in name


def _time_phrase(t):
    t = norm(t)
    return (t + "から") if t else ""


# ---------- 型1：会議1件の案内（前日に出す） ----------
def meeting_notice(m, meeting_page=None):
    """m: parse_session_page の1件。meeting_page: parse_meeting_page の結果（あれば件名を返信に並べる）"""
    import datetime
    d = datetime.date.fromisoformat(m["date"])
    name = m["name"]
    body = f"【議会】{fmt_date(d)}{_time_phrase(m['time'])}、{name}が開かれます。"
    body += (BOCHO_HONKAIGI + ("中継もあります。" if has_stream(name) else "")) if is_honkaigi(name) else BOCHO_IINKAI
    parent = body + "詳細は返信欄に。\n" + HASHTAG_GIKAI
    replies = []
    r1 = ""
    if meeting_page and meeting_page.get("published"):
        day = next((x for x in meeting_page["days"] if x["date"] == m["date"]), None) or meeting_page["days"][0]
        items = [t for _, t in day["agenda"]][:12] or [t for t, _ in day["docs"]][:12]
        if items:
            r1 = "予定されている審議内容（区議会のページより）\n" + "\n".join(f"・{t}" for t in items) + "\n"
    r1 += "傍聴の案内と当日の資料は区議会のページで。\n" + (m["url"] or S.BOCHO_URL)
    if m["url"]:
        r1 += "\n" + S.BOCHO_URL
    if has_stream(name):
        r1 += "\n中継：" + S.CHUKEI_URL
    replies.append(r1)
    return parent, replies


def day_notice(meetings, pages=None):
    """同じ日に複数の会議があるときは1本にまとめる（例：議運→全員協議会→本会議）"""
    import datetime
    pages = pages or {}
    if len(meetings) == 1:
        return meeting_notice(meetings[0], pages.get(meetings[0]["url"]))
    d = datetime.date.fromisoformat(meetings[0]["date"])
    parts = "、".join(f"{_time_phrase(m['time'])}{m['name']}" for m in meetings)
    body = f"【議会】{fmt_date(d)}は、{parts}が開かれます。"
    body += "傍聴は当日午前8時30分から、シビックセンター23階の区議会事務局で受付（本会議は定員63人、委員会は15人。超えた場合は抽選）。"
    if any(not is_honkaigi(m["name"]) for m in meetings):
        body += "委員会の会場は26階スカイホールです。"
    if any(has_stream(m["name"]) for m in meetings):
        body += CHUKEI
    parent = body + "詳細は返信欄に。\n" + HASHTAG_GIKAI
    lines = [f"{m['name']}：{m['url']}" for m in meetings if m["url"]]
    r1 = "各会議のページ（審議内容・資料）\n" + "\n".join(lines) + "\n傍聴の案内：" + S.BOCHO_URL
    return parent, [r1]


# ---------- 型1：定例会の全体日程（日程ページが新しく出たとき） ----------
def session_overview(sess):
    ms = sess["meetings"]
    if not ms:
        return None
    import datetime
    first = next((x for x in ms if is_honkaigi(x["name"])), ms[0])   # 「本会議から始まる」と案内する
    fd = datetime.date.fromisoformat(first["date"])
    title = sess["title"].replace("区議会の日程・審議内容", "").strip("（） ")
    q_dates = sorted({x["date"] for x in ms if "一般質問" in x["name"]})
    q_time = next((x["time"] for x in ms if "一般質問" in x["name"]), "")
    last = ms[-1]
    ld = datetime.date.fromisoformat(last["date"])
    parent = f"【議会】{title}は、{fmt_date(fd)}{_time_phrase(first['time'])}の{first['name']}で始まります。"
    if q_dates:
        parent += "一般質問は" + "、".join(fmt_date(datetime.date.fromisoformat(x)) for x in q_dates) + f"{_time_phrase(q_time)}。"
    parent += f"最終日は{fmt_date(ld)}の{last['name']}です。詳細は返信欄に。\n" + HASHTAG_GIKAI
    coms = [x for x in ms if not is_honkaigi(x["name"]) and "全員協議会" not in x["name"] and "議会運営" not in x["name"]]
    reply = ""
    if coms:
        reply += f"委員会は{fmt_date(datetime.date.fromisoformat(coms[0]['date']))}から{fmt_date(datetime.date.fromisoformat(coms[-1]['date']))}にかけて開かれます。"
    reply += "傍聴は当日午前8時30分から、シビックセンター23階の区議会事務局で受け付けます。日程の一覧は区議会のページで見られます。\n" + sess["url"]
    return parent, [reply]


# ---------- 型1：一般質問の日（スレッド） ----------
def questions_thread(q, date_iso, meeting_time="午後2時"):
    import datetime
    d = datetime.date.fromisoformat(date_iso)
    day = next((x for x in q["days"] if x["date"] == date_iso), None)
    if not day or not day["speakers"]:
        return None
    n = len(day["speakers"])
    parent = (f"【議会】{fmt_date(d)}{_time_phrase(meeting_time)}、本会議で一般質問が行われます。登壇は{n}人。"
              f"{CHUKEI}{BOCHO_HONKAIGI}通告の項目は返信欄に。\n" + HASHTAG_GIKAI)
    replies = ["通告の一覧（区議会のページ）と中継のページ\n" + q["url"] + "\n" + S.CHUKEI_URL]
    for i, sp in enumerate(day["speakers"], start=1):
        t = f"{i}人目　{sp['name']} 議員（{sp['party']}）\n" if sp["party"] else f"{i}人目　{sp['name']} 議員\n"
        t += "\n".join(f"{k}. {it}" for k, it in enumerate(sp["items"], start=1))
        replies.append(t)
    return parent, replies


# ---------- 型3：資料・通告・会議録の公開 ----------
def materials_notice(mp, date_iso=None):
    """委員会ページに審議内容・資料が載ったとき"""
    import datetime
    day = next((x for x in mp["days"] if x["date"] == date_iso), None) if date_iso else None
    day = day or next((x for x in mp["days"] if x["agenda"] or x["docs"]), None)
    if not day:
        return None
    name = re.sub(r"（.*?）$", "", mp["title"])
    when = fmt_date(datetime.date.fromisoformat(day["date"])) + "の" if day["date"] else ""
    nd = len(day["docs"])
    parent = f"【会議録・資料】{when}{name}の審議内容" + ("と資料" if nd else "") + "が区議会のページに公開されました。"
    if nd:
        parent += f"資料は{nd}件。"
    parent += "件名は返信欄に。\n" + HASHTAG_GIKAI
    items = [t for _, t in day["agenda"]] or [t for t, _ in day["docs"]]
    reply = "\n".join(f"・{t}" for t in items[:20])
    if len(items) > 20:
        reply += f"\nほか{len(items)-20}件"
    reply += "\n" + mp["url"]
    return parent, [reply]


def questions_published_notice(q):
    import datetime
    days = [x for x in q["days"] if x["speakers"]]
    total = sum(len(x["speakers"]) for x in days)
    parent = (f"【会議録・資料】本会議の一般質問の通告一覧が公開されました。"
              + "、".join(fmt_date(datetime.date.fromisoformat(x["date"])) + f"（{len(x['speakers'])}人）" for x in days)
              + f"、あわせて{total}人が登壇します。各議員の項目は返信欄に。\n" + HASHTAG_GIKAI)
    reply = "\n".join(f"{fmt_date(datetime.date.fromisoformat(x['date']))}：" + "、".join(sp["name"] for sp in x["speakers"]) for x in days)
    reply += "\n" + q["url"]
    return parent, [reply]


def sokuho_notice(new_pdfs):
    """[(委員会名, 文言, url)] 新しく出た速報版"""
    coms = []
    for c, _, _ in new_pdfs:
        if c not in coms:
            coms.append(c)
    parent = (f"【会議録・資料】委員会会議録の速報版が公開されました（{'、'.join(coms)}）。"
              "速報版は未定稿で、正式な会議録が出ると差し替えられます。日付と一覧は返信欄に。\n" + HASHTAG_GIKAI)
    reply = "\n".join(f"・{c}　{t}" for c, t, _ in new_pdfs[:20]) + "\n" + S.SOKUHO_URL
    return parent, [reply]


# ---------- 型4：その日の区のお知らせまとめ ----------
def daily_digest(items, d):
    """items: [{title, creator}]"""
    parent = f"【区のお知らせまとめ】{fmt_date(d)}に区のホームページで更新されたお知らせです（{len(items)}件）。一覧へのリンクは返信欄に。\n"
    for it in items:
        c = re.sub(r"^.+?部", "", it.get("creator", "")) or it.get("creator", "")
        parent += f"・{it['title']}（{c}）\n" if c else f"・{it['title']}\n"
    parent += HASHTAG_KU
    return parent, ["区の新着情報の一覧\n" + S.SHINCHAKU_INDEX]


# ---------- 型2：区のお知らせ（Claude） ----------
PROMPT_T2 = """あなたは文京区の会派が運営する区政情報アカウント「文京区まとめ」の担当です。
区のホームページの新着1件について、X（旧Twitter）の投稿文を「親」と「返信」の2本に分けて作ります。

# 出典（これ以外の情報は使わない）
件名：{title}
担当課：{creator}
分野：{subject}
URL：{url}
---本文ここから---
{text}
---本文ここまで---

# 書き方の決まり（すべて守る）
- 親：「【区のお知らせ】」で始め、次に「誰に関係があるか」を書く（例「小学6年生から高校1年生相当の男の子がいるご家庭へ。」）。続けて「誰が・何を・いつまで」を出典の本文から抜き出した事実だけで書く。最後は「詳細は返信欄に。」で締める。リンクは入れない。
- 返信：「どうやって・どこで・誰に聞くか」（申込み方法、場所、問い合わせ先、電話番号など）を出典の本文から抜き出して書く。文末は出典のURLを1行で置く。
- 本文にない数字・名前・日付は書かない。推測や評価の言葉（便利、おすすめ、ぜひ、注目 など）は書かない。
- 「新たに」「始まります」「スタート」は書かない（更新日と開始日が違うことがあるため）。
- 絵文字、感嘆符、決まり文句の添え文（「ぜひご覧ください」等）は書かない。ハッシュタグは書かない（こちらで付ける）。
- 難しい役所言葉は言い換える。丁寧語。親は140字前後、返信は200字以内。
- 日付は「11月5日（水）」のように曜日まで書く。ただし本文に曜日が無い場合は曜日を付けない。
- 抜き出せる事実が本文に無ければ、親は「【区のお知らせ】{title}（{creator}）。詳細は返信欄に。」とだけ書き、返信はURLだけにする。

# 出力形式（この形式だけ。前置きや説明は書かない）
===親===
（親の本文）
===返信===
（返信の本文）
"""


def compose_type2(item, text):
    """Claude（Sonnet）に型2を書かせ、約束ごとに外れたら件名とリンクだけの安全な形に落とす"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        parent, reply = fallback_type2(item)
        return parent, [reply], ["ANTHROPIC_API_KEY が無いので件名とリンクだけ"]
    import anthropic
    client = anthropic.Anthropic()
    prompt = PROMPT_T2.format(title=item["title"], creator=item.get("creator", ""), subject=item.get("subject", ""),
                              url=item["url"], text=text[:7000])
    msg = client.messages.create(model=os.environ.get("MATOME_MODEL", "claude-sonnet-5-5"),
                                 max_tokens=1500, messages=[{"role": "user", "content": prompt}])
    out = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"===親===\s*(.*?)\s*===返信===\s*(.*)", out, re.S)
    parent = norm_block(m.group(1)) if m else ""
    reply = norm_block(m.group(2)) if m else ""
    parent = URL_RE.sub("", parent).strip()
    if "詳細は返信欄に" not in parent:
        parent = parent.rstrip("。") + "。詳細は返信欄に。"
    parent = parent + "\n" + HASHTAG_KU
    if item["url"] not in reply:
        reply = (reply + "\n" + item["url"]).strip()
    problems = check_post(parent, [reply], source_text=text + "\n" + item["title"] + "\n" + item.get("creator", ""),
                          hashtag=HASHTAG_KU, banned=BANNED_T2)
    if problems:
        parent, reply = fallback_type2(item)
        return parent, [reply], problems
    return parent, [reply], []


def fallback_type2(item):
    parent = f"【区のお知らせ】{item['title']}（{item.get('creator','')}）。詳細は返信欄に。\n{HASHTAG_KU}"
    return parent, item["url"]


def norm_block(s):
    return "\n".join(norm(l) for l in s.strip().splitlines() if norm(l))


# ---------- 機械チェック ----------
def check_post(parent, replies, source_text=None, hashtag=None, banned=BANNED_PHRASES):
    """守れていない点を文字列のリストで返す（空なら合格）"""
    p = []
    if URL_RE.search(parent):
        p.append("親にリンクがある")
    if "返信欄に" not in parent and "続きはコメントに" not in parent:
        p.append("親に「続きはコメントに」（または「詳細は返信欄に」）が無い")
    tags = re.findall(r"#\S+", parent)
    if len(tags) != 1 or tags[0] not in (HASHTAG_GIKAI, HASHTAG_KU):
        p.append(f"ハッシュタグが1個でない: {tags}")
    if hashtag and tags and tags[0] != hashtag:
        p.append("ハッシュタグの種類が違う")
    whole = parent + "\n" + "\n".join(replies)
    if EMOJI_RE.search(whole):
        p.append("絵文字がある")
    for w in banned:
        if w in whole:
            p.append(f"使わない言葉がある: {w}")
    if "！" in whole or "!" in whole:
        p.append("感嘆符がある")
    if replies and not URL_RE.search(replies[0]):
        p.append("最初の返信にリンクが無い")
    if source_text is not None:
        src = unicodedata.normalize("NFKC", source_text)
        src_digits = set(re.findall(r"\d+", src))
        body_wo_url = URL_RE.sub("", unicodedata.normalize("NFKC", whole))
        for num in set(re.findall(r"\d+", body_wo_url)):
            if num not in src_digits:
                p.append(f"出典にない数字がある: {num}")
    return p


def price_of(text):
    return PRICE_WITH_URL if URL_RE.search(text) else PRICE_NO_URL


def thread_cost(parent, replies):
    return round(price_of(parent) + sum(price_of(r) for r in replies), 3)


def to_cell(parent, replies):
    """候補シートの「文面案」セルの形"""
    return "【親】\n" + parent + "".join("\n【返信】\n" + r for r in replies)


def from_cell(cell):
    """「文面案」セルから（親, [返信...]）を読み戻す（先生が手で直した文面も拾える）"""
    parts = re.split(r"\n?【返信\d*】\n?", cell)
    parent = re.sub(r"^\s*【親】\s*\n?", "", parts[0]).strip()
    replies = [x.strip() for x in parts[1:] if x.strip()]
    return parent, replies
