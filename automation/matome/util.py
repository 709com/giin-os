# -*- coding: utf-8 -*-
"""共通の小道具：日本時間・日付表記・区サイトの取得・本文抽出"""
import os, re, json, datetime, unicodedata
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

JST = datetime.timezone(datetime.timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (compatible; GiinOS-matome/1.0; +https://x.com/bunkyo_matome)"}
WEEKDAYS = "月火水木金土日"
STATE_PATH = Path("matome/state.json")

def now_jst():
    """今の日本時間。MATOME_FAKE_NOW（例 2026-11-24 09:00）を置くと、その日時のつもりで動く（試験用）"""
    fake = os.environ.get("MATOME_FAKE_NOW")
    if fake:
        return datetime.datetime.strptime(fake, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    return datetime.datetime.now(JST)

def today_jst():
    return now_jst().date()

def fmt_date(d):
    """2026-11-05 -> 11月5日（木）"""
    return f"{d.month}月{d.day}日（{WEEKDAYS[d.weekday()]}）"

def fmt_dt(dt=None):
    return (dt or now_jst()).strftime("%Y-%m-%d %H:%M")

def reiwa(year):
    return year - 2018

def norm(s):
    """全角数字・空白などをそろえる"""
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("(", "（").replace(")", "）")   # 日本語の文面では全角のかっこにそろえる
    return re.sub(r"[ \t　]+", " ", s).strip()

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

def fetch(url, timeout=30, binary=False, tries=3):
    """区サイトを取得（一時的な接続切れに備えて3回まで試す）"""
    import time
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            break
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e; time.sleep(2 * (i + 1))
    else:
        raise last
    if binary:
        return r.content
    r.encoding = r.apparent_encoding if not r.encoding or r.encoding.lower() == "iso-8859-1" else r.encoding
    return r.text

def soup_of(html):
    return BeautifulSoup(html, "html.parser")

def contents_div(soup):
    """区サイトの本文部分（id=tmp_contents）。無ければ全体"""
    return soup.find(id="tmp_contents") or soup

def page_text(html):
    """本文の読みやすいテキスト（Claudeに読ませる用）"""
    soup = soup_of(html)
    div = contents_div(soup)
    inquiry = soup.find(id="tmp_inquiry")   # 「お問い合わせ先」（担当課・電話）も事実として使えるように含める
    for t in div.find_all(["script", "style"]):
        t.decompose()
    for br in div.find_all("br"):
        br.replace_with("\n")
    text = div.get_text("\n")
    if inquiry is not None and inquiry not in div.find_all(True):
        text += "\n" + inquiry.get_text("\n")
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.replace("シェア　ポスト", "")
    return norm_lines(text)

def norm_lines(text):
    return "\n".join(norm(l) for l in text.splitlines() if norm(l))

def page_title(html):
    soup = soup_of(html)
    h1 = contents_div(soup).find("h1")
    return norm(h1.get_text()) if h1 else norm(soup.title.get_text() if soup.title else "")

def links_in(html, base):
    """本文中のリンク [(絶対URL, 文言)]"""
    soup = soup_of(html)
    out = []
    for a in contents_div(soup).find_all("a", href=True):
        out.append((urljoin(base, a["href"]), norm(a.get_text())))
    return out

def parse_md(s):
    """'11月17日' -> (11, 17)"""
    m = re.search(r"(\d{1,2})月(\d{1,2})日", norm(s))
    return (int(m.group(1)), int(m.group(2))) if m else None

def year_from_title(title):
    """「令和8年11月定例議会」→ 2026。無ければ None"""
    m = re.search(r"令和\s*(\d{1,2})\s*年", norm(title or ""))
    if m:
        return 2018 + int(m.group(1))
    m = re.search(r"(20\d{2})年", norm(title or ""))
    return int(m.group(1)) if m else None


def md_to_date(md, base=None, year=None):
    """(月, 日) を日付にする。年が分かればそれを使い、分からなければ今日に近い年にする（年またぎ対策）"""
    m, d = md
    if year:
        try:
            return datetime.date(year, m, d)
        except ValueError:
            pass
    base = base or today_jst()
    cands = []
    for y in (base.year - 1, base.year, base.year + 1):
        try:
            cands.append(datetime.date(y, m, d))
        except ValueError:
            pass
    return min(cands, key=lambda x: abs((x - base).days))
