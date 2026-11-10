# -*- coding: utf-8 -*-
"""情報源の読み取り：区の新着RSS／議会の日程一覧／定例会の日程ページ／一般質問要旨／委員会ページ／会議録速報版"""
import re
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from .util import fetch, soup_of, contents_div, norm, parse_md, md_to_date, reiwa, today_jst, links_in, page_title, year_from_title

BASE = "https://www.city.bunkyo.lg.jp"
RSS_URL = f"{BASE}/shinchaku/shinchaku.xml"
SHINCHAKU_INDEX = f"{BASE}/shinchaku/index.html"
SOKUHO_URL = f"{BASE}/kugikai/p007084.html"          # 委員会会議録（速報版）
CHUKEI_URL = f"{BASE}/kugikai/p007427.html"          # 議会中継の案内
STREAM_URL = "https://bunkyo-city.stream.jfit.co.jp/"
BOCHO_URL = f"{BASE}/kugikai/p007423.html"           # 傍聴のご案内

# 件名にこれが入るものは流さない（11月5日決定）
SKIP_WORDS = ("障害", "メンテナンス", "復旧")


def council_index_url(year=None):
    y = year or today_jst().year
    return f"{BASE}/kugikai/oshirase/honkaigi/r{reiwa(y):02d}/index.html"


# ---------- 区の新着RSS ----------
def rss_items():
    """[{url, title, date(YYYY-MM-DD), subject, creator}] 直近50件"""
    xml = fetch(RSS_URL)
    soup = BeautifulSoup(xml, "xml")
    items = []
    for it in soup.find_all("item"):
        url = it.get("rdf:about") or (it.link.get_text() if it.link else "")
        title = norm(it.title.get_text() if it.title else "")
        date = (it.find("dc:date").get_text() if it.find("dc:date") else "")[:10]
        subject = "・".join(norm(s.get_text()) for s in it.find_all("dc:subject"))
        creator = norm(it.find("dc:creator").get_text() if it.find("dc:creator") else "")
        items.append({"url": url, "title": title, "date": date, "subject": subject, "creator": creator})
    return items


def is_skipped_title(title):
    return any(w in title for w in SKIP_WORDS)


# ---------- 議会の日程一覧（年ごとの索引） ----------
def council_index_links(url):
    """索引ページにある『区議会の日程・審議内容（…）』のリンク [(url, 文言)]"""
    html = fetch(url)
    out = []
    for u, t in links_in(html, url):
        if t.startswith("区議会の日程・審議内容") and "（" in t:
            out.append((u, t))
    return out


# ---------- 定例会の日程ページ ----------
def parse_session_page(url, html=None):
    """日程表を [{date, weekday, time, name, url}] に。rowspan（同じ日・同じ会議の複製）を展開する"""
    html = html or fetch(url)
    soup = soup_of(html)
    title = page_title(html)
    year = year_from_title(title)
    table = contents_div(soup).find("table")
    rows = []
    if not table:
        return {"title": title, "url": url, "meetings": rows}
    cur_date = cur_wd = None
    pending_name = None       # rowspan で下の行に続く会議名
    pending_name_rows = 0
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells or cells[0].name == "th":
            continue
        texts = [norm(c.get_text(" ")) for c in cells]
        # 行の先頭が「月日」ならその日に移る
        i = 0
        if parse_md(texts[0]):
            cur_date = md_to_date(parse_md(texts[0]), year=year)
            i = 1
            if i < len(texts) and len(texts[i]) == 1 and texts[i] in "月火水木金土日":
                cur_wd = texts[i]; i += 1
        time = texts[i] if i < len(texts) else ""
        i += 1
        if i < len(cells):
            c = cells[i]
            a = c.find("a", href=True)
            name = norm(c.get_text(" "))
            link = urljoin(url, a["href"]) if a else ""
            rs = int(c.get("rowspan", 1) or 1)
            if rs > 1:
                pending_name, pending_link, pending_name_rows = name, link, rs - 1
        elif pending_name and pending_name_rows > 0:
            name, link = pending_name, pending_link
            pending_name_rows -= 1
        else:
            name, link = "", ""
        if cur_date and name:
            rows.append({"date": cur_date.isoformat(), "weekday": cur_wd or "", "time": time, "name": name, "url": link})
    return {"title": title, "url": url, "meetings": rows}


# ---------- 一般質問要旨 ----------
def parse_questions_page(url, html=None):
    """{title, days: [{date, speakers: [{name, party, items:[...]}]}], published: bool}"""
    html = html or fetch(url)
    soup = soup_of(html)
    div = contents_div(soup)
    title = page_title(html)
    year = year_from_title(title)
    days, cur = [], None
    for el in div.find_all(["h2", "h3", "ol", "ul"]):
        if el.name == "h2":
            md = parse_md(el.get_text())
            if md:
                cur = {"date": md_to_date(md, year=year).isoformat(), "speakers": []}
                days.append(cur)
            else:
                cur = None
        elif el.name == "h3" and cur is not None:
            t = norm(el.get_text())
            m = re.match(r"(.+?)[（(](.+?)[）)]\s*$", t)
            cur["speakers"].append({"name": norm(m.group(1)) if m else t,
                                    "party": norm(m.group(2)) if m else "", "items": []})
        elif el.name in ("ol", "ul") and cur is not None and cur["speakers"]:
            cur["speakers"][-1]["items"].extend(norm(li.get_text()) for li in el.find_all("li"))
    published = any(s["items"] for d in days for s in d["speakers"])
    return {"title": title, "url": url, "days": days, "published": published}


# ---------- 委員会・本会議の個別ページ ----------
def parse_meeting_page(url, html=None):
    """{title, published: bool, days: [{date, agenda:[(見出し, [件名...])], docs:[(件名, url)]}]}"""
    html = html or fetch(url)
    soup = soup_of(html)
    div = contents_div(soup)
    title = page_title(html)
    year = year_from_title(title)
    days, cur, section, sub = [], None, None, None
    for el in div.find_all(["h2", "h3", "h4", "h5", "ol", "ul", "p"]):
        if el.name == "h2":
            md = parse_md(el.get_text())
            cur = {"date": md_to_date(md, year=year).isoformat() if md else "", "agenda": [], "docs": []}
            days.append(cur); section = sub = None
        elif el.name == "h3":
            section = norm(el.get_text())
        elif el.name in ("h4", "h5"):
            sub = norm(el.get_text())
        elif el.name in ("ol", "ul", "p") and cur is not None:
            items = [li for li in el.find_all("li")] if el.name != "p" else [el]
            if el.name == "p" and el.find_parent("li"):
                continue
            for li in items:
                a = li.find("a", href=True)
                t = norm(li.get_text(" "))
                if not t:
                    continue
                if a and a["href"].lower().endswith(".pdf"):
                    cur["docs"].append((re.sub(r"（PDF[：:].*?）$", "", t), urljoin(url, a["href"])))
                elif section and "審議" in section:
                    cur["agenda"].append((sub or "", t))
    if not days:  # h2 が無い単一日のページ
        days = [{"date": "", "agenda": [], "docs": []}]
    published = any(d["agenda"] or d["docs"] for d in days)
    return {"title": title, "url": url, "days": days, "published": published}


# ---------- 委員会会議録（速報版） ----------
def sokuho_pdfs(html=None):
    """[(委員会名, 文言, PDFのURL)]"""
    html = html or fetch(SOKUHO_URL)
    soup = soup_of(html)
    div = contents_div(soup)
    out, committee = [], ""
    for el in div.find_all(["h2", "h3", "a"]):
        if el.name in ("h2", "h3"):
            committee = norm(el.get_text())
        elif el.get("href", "").lower().endswith(".pdf"):
            out.append((committee, re.sub(r"（PDF[：:].*?）$", "", norm(el.get_text())), urljoin(SOKUHO_URL, el["href"])))
    return out
