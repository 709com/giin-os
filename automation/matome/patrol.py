# -*- coding: utf-8 -*-
"""文京区まとめ（@bunkyo_matome）の巡回本体。
30分ごと（8時〜22時台）に動き、
  1) 議会（型1・型3）の候補を見つけて候補シートに並べ、先生に知らせる（確認「OK」で次の巡回に投稿）
  2) 区のお知らせ（型2）を拾い、文面を作って投稿する（1日3本まで。4本目からは23時台のまとめ＝型4）
  3) 失敗した返信のやり直し、見張り、設定シートの「最終巡回」更新
設定シートの「試運転」がオンの間は候補を並べるだけで投稿しない。「停止スイッチ」がオンなら何もしない。

使い方（リポジトリのルートで）：
  PYTHONPATH=automation python -m matome.patrol            # 通常の巡回
  PYTHONPATH=automation python -m matome.patrol digest     # 23時台のまとめ（型4）だけ
"""
import re, sys, json, datetime, hashlib, traceback
from pathlib import Path
from .util import now_jst, today_jst, fmt_dt, fmt_date, load_state, save_state, fetch, page_text, norm
from . import sources as S
from . import compose as C
from .ledger import Ledger
from .xapi import XClient

ACCOUNT = "@bunkyo_matome"
KUBUN = "政務活動費（会派）"
ARCHIVE = Path("matome/archive")
OK_WORDS = {"ok", "ＯＫ", "オッケー", "おっけー", "了解", "投稿"}
SKIP_WORDS_CONFIRM = ("見送り", "ボツ", "不要", "却下", "NG", "ng")


def line(text):
    """本人のLINEにだけ送る（common.line_push と同じ経路。鍵が無ければ標準出力）"""
    import os, requests
    token, uid = os.environ.get("LINE_CHANNEL_TOKEN"), os.environ.get("LINE_USER_ID")
    if not token or not uid:
        print("[LINE鍵なし] " + text); return
    for i in range(0, min(len(text), 14000), 4900):
        try:
            requests.post("https://api.line.me/v2/bot/message/push", headers={"Authorization": f"Bearer {token}"},
                          json={"to": uid, "messages": [{"type": "text", "text": text[i:i+4900]}]}, timeout=30)
        except Exception as e:
            print("[LINE送信できず]", e)


class Patrol:
    def __init__(self, mode="patrol"):
        self.mode = mode
        self.now = now_jst()
        self.today = self.now.date()
        self.state = load_state()
        self.state.setdefault("rss_seen", {}); self.state.setdefault("council_index", {})
        self.state.setdefault("sessions", {}); self.state.setdefault("meeting_pages", {})
        self.state.setdefault("questions_pages", {}); self.state.setdefault("sokuho", [])
        self.state.setdefault("candidates", {}); self.state.setdefault("posted", {})
        self.state.setdefault("digest_pool", {}); self.state.setdefault("pending_replies", [])
        self.state.setdefault("alerts", {})
        self.ledger = Ledger()
        self.settings = self.ledger.settings()
        self.log, self.alerts, self.notices = [], [], []
        self.dry = self.settings.get("試運転", "オン") == "オン"
        m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", self.settings.get("本番開始日", ""))
        if m and self.today < datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))):
            self.dry = True   # 本番開始日より前は試運転と同じ（投稿しない）
        self.slots = self.settings.get("運用", "") == "3枠"   # 1日3枠（7時 自動／12時 区の施策 OK後／20時 議会 OK後）
        self.state.setdefault("slots_done", {})
        self.x = XClient(dry_run=self.dry)
        if not self.dry and not self.x.can_post:
            self.dry = True
            self.alert("x_keys", "Xの鍵（Secrets）が未設定のため、試運転として動きました")
        self.no_post_today = self.is_election_day()
        self.confirm_mode = self.settings.get("先生の確認", "議会のみ")  # 議会のみ／すべて／なし
        self.cap = int(re.sub(r"\D", "", self.settings.get("1日の単独投稿の上限", "3")) or 3)

    # ---------- 小道具 ----------
    def say(self, s):
        print(s); self.log.append(s)

    def alert(self, kind, text):
        """見張り：同じ種類は1日1回だけ"""
        key = f"{kind}:{self.today.isoformat()}"
        if key in self.state["alerts"]:
            return
        self.state["alerts"][key] = fmt_dt(self.now)
        self.alerts.append(text)
        try:
            self.ledger.append("見張り", {"日時": fmt_dt(self.now), "種類": kind, "内容": text, "対応": ""})
        except Exception as e:
            print("見張りシートに書けず", e)

    def is_election_day(self):
        v = self.settings.get("投票日", "")
        m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", v)
        if not m:
            return False
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) == self.today

    def today_rec(self):
        return self.state["posted"].setdefault(self.today.isoformat(), {"singles": 0, "digest": False})

    def month_spend(self):
        ym = self.today.strftime("%Y-%m")
        total = 0.0
        for r in self.ledger.rows("投稿台帳"):
            if r.get("日時", "").startswith(ym) and r.get("アカウント") == ACCOUNT:
                try:
                    total += float(r.get("単価（ドル）") or 0)
                except ValueError:
                    pass
        return total

    def may_post_now(self):
        """今この瞬間に本番投稿してよいか（理由つき）"""
        if self.dry:
            return False, "試運転"
        if self.no_post_today:
            return False, "投票日"
        limit = float(re.sub(r"[^\d.]", "", self.settings.get("会派の月の上限（ドル）", "30")) or 30)
        if self.month_spend() >= limit:
            self.alert("budget", f"今月の投稿費用が上限（{limit}ドル）に達したので投稿を止めています")
            return False, "月の上限"
        return True, ""

    def post_thread(self, parent, replies, meta):
        """親→返信の順に投稿し、投稿台帳に1本ずつ記録する。返信に失敗したら次回やり直す"""
        ok, why = self.may_post_now()
        if not ok:
            return "未投稿（" + why + "）"   # 試運転・投票日・上限のときは台帳にも書かない
        confirm = meta.get("確認", "自動")
        pid = self.x.post(parent)
        self.ledger.append("投稿台帳", {"日時": fmt_dt(self.now), "アカウント": ACCOUNT, "型": meta["型"],
                                     "リンク": "なし", "単価（ドル）": C.PRICE_NO_URL,
                                     "元資料": meta.get("url", ""), "確認": confirm, "区分": KUBUN, "投稿ID": pid, "本文": parent})
        self.state["last_post_at"] = fmt_dt(self.now)
        failed = []
        for i, r in enumerate(replies):
            try:
                rid = self.x.post(r, reply_to=pid)
                self.ledger.append("投稿台帳", {"日時": fmt_dt(self.now), "アカウント": ACCOUNT, "型": meta["型"],
                                             "リンク": "あり" if C.URL_RE.search(r) else "なし", "単価（ドル）": C.price_of(r),
                                             "元資料": meta.get("url", ""), "確認": confirm, "区分": KUBUN, "投稿ID": rid, "本文": r})
            except Exception as e:
                failed = replies[i:]
                self.alert("reply_fail", f"返信の投稿に失敗（親は投稿済み）。次の巡回でやり直します: {e}")
                break
        if failed:
            self.state["pending_replies"].append({"parent_id": pid, "replies": failed, "meta": meta})
        return pid

    def retry_replies(self):
        rest = []
        for pr in self.state["pending_replies"]:
            ok, _ = self.may_post_now()
            if not ok:
                rest.append(pr); continue
            try:
                pid, failed = pr["parent_id"], []
                for i, r in enumerate(pr["replies"]):
                    try:
                        rid = self.x.post(r, reply_to=pid)
                        self.ledger.append("投稿台帳", {"日時": fmt_dt(self.now), "アカウント": ACCOUNT, "型": pr["meta"]["型"],
                                                     "リンク": "あり" if C.URL_RE.search(r) else "なし", "単価（ドル）": C.price_of(r),
                                                     "元資料": pr["meta"].get("url", ""), "確認": pr["meta"].get("確認", ""),
                                                     "区分": KUBUN, "投稿ID": rid, "本文": r})
                    except Exception:
                        failed = pr["replies"][i:]; break
                if failed:
                    pr["replies"] = failed; rest.append(pr)
                else:
                    self.say(f"やり直した返信を投稿: 親 {pid}")
            except Exception as e:
                rest.append(pr); self.say(f"返信のやり直しに失敗: {e}")
        self.state["pending_replies"] = rest

    # ---------- 候補の登録・承認 ----------
    def add_candidate(self, key, typ, title, url, parent, replies, post_on, expires, source="議会の日程", note="",
                      subject="議会", creator="区議会事務局", single="単独", listed_date=""):
        if key in self.state["candidates"]:
            return None
        problems = C.check_post(parent, replies)
        if problems:
            note = (note + "／" if note else "") + "機械チェック: " + "、".join(problems)
        needs_ok = self.needs_confirm(typ)
        row = self.ledger.append("候補", {
            "拾った日時": fmt_dt(self.now), "掲載日": listed_date or self.today.isoformat(), "情報源": source, "型": typ,
            "件名": title, "分野": subject, "担当課": creator, "URL": url, "文面案": C.to_cell(parent, replies),
            "単独/まとめ": single, "確認": "未" if needs_ok else ("試運転" if self.dry else "自動"),
            "備考": (f"投稿予定日 {post_on}／期限 {expires}" + ("／" + note if note else ""))})
        self.state["candidates"][key] = {"型": typ, "title": title, "url": url, "row": row, "status": "未" if needs_ok else "自動",
                                         "post_on": post_on, "expires": expires, "created": fmt_dt(self.now)}
        self.say(f"候補を追加（{typ}）: {title}")
        if needs_ok:
            how = "20時の枠で投稿します" if self.slots else "次の巡回で投稿します"
            self.notices.append(f"■{typ}（投稿予定 {post_on}）{title}\n{C.to_cell(parent, replies)}\n→ 台帳の候補シート{row}行目の「確認」に OK と書くと{how}")
        return key

    def needs_confirm(self, typ):
        if self.confirm_mode == "なし":
            return False
        if self.confirm_mode == "すべて":
            return True
        return typ in ("型1", "型3")

    def process_approvals(self):
        """候補シートの「確認」列を読み、OKなら投稿する（このスクリプトが登録した行だけ）"""
        rows = {r["_row"]: r for r in self.ledger.rows("候補")}
        for key, c in list(self.state["candidates"].items()):
            if c["status"] not in ("未",):
                continue
            r = rows.get(c.get("row"))
            if not r or r.get("URL") != c["url"]:
                continue
            v = norm(r.get("確認", ""))
            if v.lower() in OK_WORDS or v.upper() == "OK" or re.match(r"^OK（.*のため待機）$", v):
                if self.today.isoformat() < c["post_on"]:
                    continue  # 投稿予定日まで待つ
                if self.today.isoformat() > c["expires"]:
                    c["status"] = "期限切れ"
                    self.ledger.update_cell("候補", r["_row"], "確認", "期限切れ（未投稿）"); continue
                parent, replies = C.from_cell(r.get("文面案", ""))
                problems = C.check_post(parent, replies)
                if problems:
                    self.ledger.update_cell("候補", r["_row"], "備考", (r.get("備考", "") + "／直してから再度OK: " + "、".join(problems)))
                    self.ledger.update_cell("候補", r["_row"], "確認", "要修正")
                    c["status"] = "要修正"; continue
                pid = self.post_thread(parent, replies, {"型": c["型"], "url": c["url"], "確認": "先生OK"})
                if pid.startswith("未投稿"):
                    want = "OK（" + pid[3:].strip("（）") + "のため待機）"
                    if v != want:
                        self.ledger.update_cell("候補", r["_row"], "確認", want)
                    continue   # 状態は「未」のまま。投稿できるようになったら次の巡回で投稿する
                c["status"] = "投稿済"
                self.ledger.update_cell("候補", r["_row"], "確認", f"投稿済 {fmt_dt(self.now)} {pid}")
            elif any(w in v for w in SKIP_WORDS_CONFIRM):
                c["status"] = "見送り"
            elif self.today.isoformat() > c["expires"]:
                c["status"] = "期限切れ"
                self.ledger.update_cell("候補", r["_row"], "確認", "期限切れ（未確認）")

    def auto_or_candidate(self, key, typ, title, url, parent, replies, post_on, expires, **kw):
        """確認が要らない型は、投稿予定日になったら投稿。要るなら候補として並べる"""
        k = self.add_candidate(key, typ, title, url, parent, replies, post_on, expires, **kw)
        c = self.state["candidates"].get(key)
        if c and c["status"] == "自動" and post_on <= self.today.isoformat() <= expires:
            pid = self.post_thread(parent, replies, {"型": typ, "url": url, "確認": "自動"})
            c["status"] = "投稿済" if not pid.startswith("未投稿") else "自動"
            if c["status"] == "投稿済" and c.get("row"):
                self.ledger.update_cell("候補", c["row"], "確認", f"投稿済 {fmt_dt(self.now)} {pid}")

    # ---------- 議会 ----------
    def check_council(self):
        years = [self.today.year] + ([self.today.year + 1] if self.today.month == 12 else [])
        for y in years:
            idx = S.council_index_url(y)
            try:
                links = S.council_index_links(idx)
            except Exception as e:
                if y == self.today.year:
                    self.alert("council_index", f"議会の日程一覧が読めません: {e}")
                continue
            if not links and y == self.today.year:
                self.alert("council_index_empty", "議会の日程一覧からリンクを取り出せません（ページの形が変わった可能性）")
            known = set(self.state["council_index"].get(idx, []))
            first = idx not in self.state["council_index"]
            for u, t in links:
                if u not in known and not first:
                    self.say(f"新しい日程ページ: {t}")
                    try:
                        sess = S.parse_session_page(u)
                        ov = C.session_overview(sess)
                        if ov:
                            key = f"型1|overview|{u}"
                            self.auto_or_candidate(key, "型1", t, u, ov[0], ov[1], self.today.isoformat(),
                                                   sess["meetings"][0]["date"], note="日程ページの公開")
                        self.state["sessions"][u] = {"title": t}
                    except Exception as e:
                        self.alert("session_parse", f"日程ページが読めません {u}: {e}")
                elif u not in self.state["sessions"]:
                    self.state["sessions"][u] = {"title": t}
            self.state["council_index"][idx] = [u for u, _ in links]
        # 直近の定例会の日程ページ（過去30日〜未来のもの）を見る
        for u, info in list(self.state["sessions"].items()):
            last = info.get("last_date")
            if last and datetime.date.fromisoformat(last) < self.today - datetime.timedelta(days=30):
                continue
            try:
                sess = S.parse_session_page(u)
            except Exception as e:
                self.say(f"日程ページ取得失敗 {u}: {e}"); continue
            if not sess["meetings"]:
                continue
            info["last_date"] = sess["meetings"][-1]["date"]
            info["first_date"] = sess["meetings"][0]["date"]
            self.check_meetings(sess)

    def check_meetings(self, sess):
        seen_q = set()
        pages, by_day = {}, {}
        for m in sess["meetings"]:
            d = datetime.date.fromisoformat(m["date"])
            days_to = (d - self.today).days
            # 会議ページ（審議内容・資料の公開＝型3）：開催の2週間前〜2週間後に見る
            if m["url"] and -14 <= days_to <= 14 and "一般質問" not in m["name"]:
                pages[m["url"]] = self.watch_meeting_page(m)
            # 前日の案内（型1）：3日前から候補にする
            if 0 <= days_to <= 3:
                post_on = max(d - datetime.timedelta(days=1), self.today).isoformat()
                if "一般質問" in m["name"]:
                    q = self.watch_questions_page(m["url"], seen_q) if m["url"] else None
                    th = C.questions_thread(q, m["date"], m["time"]) if q and q["published"] else None
                    if th:
                        plain = next((c for k, c in self.state["candidates"].items()
                                      if k.startswith(f"型1|meeting|{m['date']}|") and m["name"] in k), None)
                        if plain and plain["status"] == "未":   # 通告が出たので、素の案内はスレッド版に差し替える
                            plain["status"] = "差し替え"
                            if plain.get("row"):
                                self.ledger.update_cell("候補", plain["row"], "確認", "差し替え（通告一覧が出たためスレッド版へ）")
                        self.auto_or_candidate(f"型1|questions|{m['date']}", "型1", f"{fmt_date(d)} 本会議（一般質問）", m["url"],
                                               th[0], th[1], post_on, m["date"], note="一般質問のスレッド")
                        continue
                by_day.setdefault(m["date"], []).append(m)
            elif "一般質問" in m["name"] and -3 <= days_to <= 30 and m["url"]:
                self.watch_questions_page(m["url"], seen_q)
        for date_iso, ms in by_day.items():   # 同じ日の会議は1本にまとめる（例：議運→全員協議会→本会議）
            d = datetime.date.fromisoformat(date_iso)
            post_on = max(d - datetime.timedelta(days=1), self.today).isoformat()
            p, r = C.day_notice(ms, pages)
            names = "・".join(m["name"] for m in ms)
            self.auto_or_candidate(f"型1|meeting|{date_iso}|{names}", "型1", f"{fmt_date(d)} {names}",
                                   ms[0]["url"] or sess["url"], p, r, post_on, date_iso)

    def watch_meeting_page(self, m):
        u = m["url"]
        try:
            mp = S.parse_meeting_page(u)
        except Exception as e:
            self.say(f"会議ページ取得失敗 {u}: {e}"); return None
        prev = self.state["meeting_pages"].get(u, {})
        sig = hashlib.md5(json.dumps(mp["days"], ensure_ascii=False).encode()).hexdigest()
        if mp["published"] and (not prev.get("published") or prev.get("sig") != sig) and u in self.state["meeting_pages"]:
            notice = C.materials_notice(mp, m["date"])
            if notice:
                key = f"型3|materials|{u}|{sig[:8]}"
                self.auto_or_candidate(key, "型3", f"{mp['title']} の審議内容・資料", u, notice[0], notice[1],
                                       self.today.isoformat(), (self.today + datetime.timedelta(days=7)).isoformat(),
                                       source="議会の会議ページ", note="審議内容・資料の公開")
        self.state["meeting_pages"][u] = {"published": mp["published"], "sig": sig}
        return mp

    def watch_questions_page(self, u, seen):
        if u in seen:
            return self.state["questions_pages"].get(u, {}).get("_obj")
        seen.add(u)
        try:
            q = S.parse_questions_page(u)
        except Exception as e:
            self.say(f"一般質問ページ取得失敗 {u}: {e}"); return None
        prev = self.state["questions_pages"].get(u, {})
        if q["published"] and not prev.get("published") and u in self.state["questions_pages"]:
            n = C.questions_published_notice(q)
            self.auto_or_candidate(f"型3|questions|{u}", "型3", f"{q['title']} の公開", u, n[0], n[1],
                                   self.today.isoformat(), (self.today + datetime.timedelta(days=7)).isoformat(),
                                   source="議会の会議ページ", note="通告一覧の公開")
        self.state["questions_pages"][u] = {"published": q["published"]}
        self.state["questions_pages"][u]["_obj"] = q
        return q

    def check_sokuho(self):
        try:
            pdfs = S.sokuho_pdfs()
        except Exception as e:
            self.alert("sokuho", f"会議録速報版のページが読めません: {e}"); return
        if not pdfs:
            self.alert("sokuho_empty", "会議録速報版のページからPDFを取り出せません（形が変わった可能性）"); return
        known = set(self.state["sokuho"])
        first = not self.state["sokuho"]
        new = [x for x in pdfs if x[2] not in known]
        if new and not first:
            saved = []
            for c, t, u in new:  # 速報版は正式版が出ると消えるので、投稿と同時に保存する
                try:
                    (ARCHIVE / "sokuho").mkdir(parents=True, exist_ok=True)
                    fn = ARCHIVE / "sokuho" / (self.today.isoformat() + "_" + u.rsplit("/", 1)[-1])
                    fn.write_bytes(fetch(u, timeout=60, binary=True)); saved.append(str(fn))
                except Exception as e:
                    self.say(f"速報版の保存に失敗 {u}: {e}")
            p, r = C.sokuho_notice(new)
            self.auto_or_candidate(f"型3|sokuho|{self.today.isoformat()}|{len(known)}", "型3",
                                   "委員会会議録（速報版）の公開 " + "、".join(sorted({c for c, _, _ in new})), S.SOKUHO_URL, p, r,
                                   self.today.isoformat(), (self.today + datetime.timedelta(days=7)).isoformat(),
                                   source="会議録速報版", note="保存: " + ", ".join(saved))
        self.state["sokuho"] = sorted(known | {x[2] for x in pdfs})

    # ---------- 区のお知らせ ----------
    def reserved_slots(self):
        """今日投稿予定で未投稿の議会候補の数（型2がその枠を使わないようにする）"""
        t = self.today.isoformat()
        n = sum(1 for c in self.state["candidates"].values()
                if c["型"] in ("型1", "型3") and c["status"] in ("未", "自動") and c["post_on"] <= t <= c["expires"])
        return min(n, max(self.cap - 1, 0))   # 区のお知らせの枠を最低1つは残す

    def check_rss(self):
        try:
            items = S.rss_items()
        except Exception as e:
            self.alert("rss", f"区の新着RSSが読めません: {e}"); return
        if not items:
            self.alert("rss_empty", "区の新着RSSが空です（形が変わった可能性）"); return
        seen = self.state["rss_seen"]
        first = not seen
        pool = self.state["digest_pool"].setdefault(self.today.isoformat(), [])
        for it in items:
            u, rec = it["url"], seen.get(it["url"])
            if first:
                seen[u] = {"first": it["date"], "last": it["date"], "title": it["title"], "count": 1}; continue
            if rec and rec.get("last") == it["date"] and rec.get("title") == it["title"]:
                continue  # 変化なし
            self.state["last_new_item_at"] = fmt_dt(self.now)
            if S.is_skipped_title(it["title"]):
                seen[u] = {"first": (rec or {}).get("first", it["date"]), "last": it["date"], "title": it["title"],
                           "count": (rec or {}).get("count", 0) + 1, "skipped": True}
                self.say(f"見送り（件名に障害等）: {it['title']}"); continue
            is_repeat = bool(rec) and not rec.get("skipped")
            seen[u] = {"first": (rec or {}).get("first", it["date"]), "last": it["date"], "title": it["title"],
                       "count": (rec or {}).get("count", 0) + 1}
            if is_repeat:
                self.say(f"再掲載→まとめへ: {it['title']}")
                self.to_pool(pool, it, "2回目以降の掲載"); continue
            self.handle_new_item(it, pool)

    def to_pool(self, pool, it, why):
        if any(x["url"] == it["url"] for x in pool):
            return
        pool.append({"title": it["title"], "creator": it["creator"], "url": it["url"]})
        self.ledger.append("候補", {"拾った日時": fmt_dt(self.now), "掲載日": it["date"], "情報源": "区の新着", "型": "型4",
                                 "件名": it["title"], "分野": it["subject"], "担当課": it["creator"], "URL": it["url"],
                                 "文面案": "", "単独/まとめ": "まとめ", "確認": "—", "備考": why})

    def handle_new_item(self, it, pool):
        if self.slots:
            return self.slot_candidate(it)
        rec = self.today_rec()
        free = self.cap - rec["singles"] - self.reserved_slots()
        if free <= 0:
            self.to_pool(pool, it, "単独の上限（本日分）に達したためまとめへ"); return
        try:
            text = page_text(fetch(it["url"]))
        except Exception as e:
            self.say(f"ページ取得失敗 {it['url']}: {e}"); text = ""
        parent, replies, problems = C.compose_type2(it, text)
        key = f"型2|{it['url']}|{it['date']}"
        note = ("Claude文面が約束に外れたため件名とリンクだけに: " + "、".join(problems)) if problems else "Claude（Sonnet）が本文から抜き出し"
        needs_ok = self.needs_confirm("型2")
        row = self.ledger.append("候補", {"拾った日時": fmt_dt(self.now), "掲載日": it["date"], "情報源": "区の新着", "型": "型2",
                                       "件名": it["title"], "分野": it["subject"], "担当課": it["creator"], "URL": it["url"],
                                       "文面案": C.to_cell(parent, replies), "単独/まとめ": "単独",
                                       "確認": "未" if needs_ok else ("試運転" if self.dry else "自動"), "備考": note})
        self.state["candidates"][key] = {"型": "型2", "title": it["title"], "url": it["url"], "row": row,
                                         "status": "未" if needs_ok else "自動", "post_on": self.today.isoformat(),
                                         "expires": self.today.isoformat(), "created": fmt_dt(self.now)}
        if needs_ok:
            self.notices.append(f"■型2 {it['title']}\n{C.to_cell(parent, replies)}\n→ 候補シート{row}行目の「確認」に OK で投稿")
            return
        pid = self.post_thread(parent, replies, {"型": "型2", "url": it["url"], "確認": "自動"})
        if not pid.startswith("未投稿"):
            rec["singles"] += 1
            self.state["candidates"][key]["status"] = "投稿済"
            self.ledger.update_cell("候補", row, "確認", f"投稿済 {fmt_dt(self.now)} {pid}")
        else:
            rec["singles"] += 1 if self.dry else 0  # 試運転でも「枠」は数えて本番と同じ動きにする

    # ---------- 1日3枠（2026-11-12 先生決定、11-14 から） ----------
    SLOTS = ((7, 12, "自動", "区"), (12, 20, "OK", "区"), (20, 24, "OK", "議会"))   # (始まり, 終わり, 出し方, 分野)

    def slot_candidate(self, it):
        """区のお知らせ1件を文面にして候補シートへ。無難なものは「自動待ち」（7時の枠）、それ以外は先生のOK待ち（12時の枠）"""
        try:
            text = page_text(fetch(it["url"]))
        except Exception as e:
            self.say(f"ページ取得失敗 {it['url']}: {e}"); text = ""
        parent, replies, problems = C.compose_type2(it, text)
        safe = C.is_safe_item(it["title"], it.get("subject", "")) and not problems
        note = ("Claude文面が約束に外れたため件名とリンクだけに: " + "、".join(problems)) if problems else "Claude（Sonnet）が本文から抜き出し"
        note += "／7時の枠（自動）" if safe else "／12時の枠（先生のOK後）"
        status = "自動待ち" if safe else "未"   # 本番開始日より前に拾ったものも、開始後の枠で出せるように並べておく
        row = self.ledger.append("候補", {"拾った日時": fmt_dt(self.now), "掲載日": it["date"], "情報源": "区の新着", "型": "型2",
                                       "件名": it["title"], "分野": it["subject"], "担当課": it["creator"], "URL": it["url"],
                                       "文面案": C.to_cell(parent, replies), "単独/まとめ": "単独", "確認": status, "備考": note})
        self.state["candidates"][f"型2|{it['url']}|{it['date']}"] = {
            "型": "型2", "title": it["title"], "url": it["url"], "row": row, "status": status,
            "post_on": self.today.isoformat(), "expires": (self.today + datetime.timedelta(days=7)).isoformat(),
            "created": fmt_dt(self.now)}
        if status == "未":
            self.notices.append(f"■区の施策（12時の枠）{it['title']}\n{C.to_cell(parent, replies)}\n→ 候補シート{row}行目の「確認」に OK と書くと12時の枠で投稿します")

    def pick_slot(self, how, area):
        """候補シートから、この枠で出す1行を選ぶ（なければ None）"""
        today = self.today.isoformat()
        picks = []
        for r in self.ledger.rows("候補"):
            v = norm(r.get("確認", ""))
            typ = r.get("型", "")
            if area == "区" and typ != "型2":
                continue
            if area == "議会" and typ not in ("型1", "型3"):
                continue
            if how == "自動":
                if v != "自動待ち":
                    continue
            elif not (v.lower() in OK_WORDS or v.upper() == "OK" or re.match(r"^OK（.*のため待機）$", v)):
                continue
            m = re.search(r"投稿予定日 (\d{4}-\d\d-\d\d)／期限 (\d{4}-\d\d-\d\d)", r.get("備考", ""))
            post_on, expires = (m.group(1), m.group(2)) if m else ("", "9999-12-31")
            if how == "自動" and not m:   # 自動の枠は、日付の指定がなければ拾ってから7日以内のものだけ
                expires = min(expires, (datetime.date.fromisoformat(r.get("拾った日時", today)[:10]) + datetime.timedelta(days=7)).isoformat())
            if today > expires:
                self.ledger.update_cell("候補", r["_row"], "確認", "期限切れ（未投稿）"); continue
            if post_on and today < post_on:
                continue
            picks.append((expires, -r["_row"] if how == "自動" else r["_row"], r))
        picks.sort(key=lambda x: (x[0], x[1]))
        for _, _, r in picks:
            parent, replies = C.from_cell(r.get("文面案", ""))
            problems = C.check_post(parent, replies)
            if problems:
                self.ledger.update_cell("候補", r["_row"], "確認", "要修正")
                self.alert(f"slot_check_{r['_row']}", f"候補シート{r['_row']}行目は機械チェックで止めました（{'、'.join(problems)}）。直して再度「OK」にしてください")
                continue
            return r, parent, replies
        return None

    def slot_run(self):
        """今の時刻の枠で、まだ出していなければ1本だけ出す（GitHubの遅れに備えて、次の枠の時刻までは待つ）"""
        done = self.state["slots_done"].setdefault(self.today.isoformat(), [])
        for start, end, how, area in self.SLOTS:
            if not (start <= self.now.hour < end) or start in done:
                continue
            ok, why = self.may_post_now()
            if not ok:
                self.say(f"{start}時の枠：投稿しない（{why}）"); return
            got = self.pick_slot(how, area)
            if not got:
                self.say(f"{start}時の枠：出せる候補なし（{'自動待ち' if how == '自動' else '先生のOK'}の候補がありません）"); return
            r, parent, replies = got
            pid = self.post_thread(parent, replies, {"型": r.get("型", ""), "url": r.get("URL", ""),
                                                      "確認": "自動" if how == "自動" else "先生OK"})
            if pid.startswith("未投稿"):
                self.say(f"{start}時の枠：{pid}"); return
            done.append(start)
            self.ledger.update_cell("候補", r["_row"], "確認", f"投稿済 {fmt_dt(self.now)} {pid}")
            self.say(f"{start}時の枠で投稿: 候補シート{r['_row']}行目 {r.get('件名', '')}")
            return

    def post_digest(self):
        rec = self.today_rec()
        pool = self.state["digest_pool"].get(self.today.isoformat(), [])
        if rec.get("digest") or not pool:
            self.say("まとめ：対象なし" if not pool else "まとめ：投稿済み"); return
        p, r = C.daily_digest(pool, self.today)
        key = f"型4|{self.today.isoformat()}"
        self.state["candidates"][key] = {"型": "型4", "title": f"{fmt_date(self.today)}のまとめ", "url": S.SHINCHAKU_INDEX,
                                         "row": None, "status": "自動", "post_on": self.today.isoformat(),
                                         "expires": self.today.isoformat(), "created": fmt_dt(self.now)}
        row = self.ledger.append("候補", {"拾った日時": fmt_dt(self.now), "掲載日": self.today.isoformat(), "情報源": "区の新着",
                                       "型": "型4", "件名": f"{fmt_date(self.today)}のまとめ（{len(pool)}件）", "分野": "", "担当課": "",
                                       "URL": S.SHINCHAKU_INDEX, "文面案": C.to_cell(p, r), "単独/まとめ": "まとめ",
                                       "確認": "試運転" if self.dry else "自動", "備考": ""})
        pid = self.post_thread(p, r, {"型": "型4", "url": S.SHINCHAKU_INDEX, "確認": "自動"})
        if not pid.startswith("未投稿"):
            rec["digest"] = True
            self.state["candidates"][key]["status"] = "投稿済"
            self.ledger.update_cell("候補", row, "確認", f"投稿済 {fmt_dt(self.now)} {pid}")

    # ---------- 見張り ----------
    def watch(self):
        for w in self.x.warnings:
            self.alert("x_token", w)
        if not self.dry and self.state.get("last_new_item_at"):
            lp = self.state.get("last_post_at")
            ln = datetime.datetime.strptime(self.state["last_new_item_at"], "%Y-%m-%d %H:%M")
            if (not lp or datetime.datetime.strptime(lp, "%Y-%m-%d %H:%M") < ln) and \
               (self.now.replace(tzinfo=None) - ln).total_seconds() > 24 * 3600:
                self.alert("no_post_24h", "新着や議会の動きがあったのに24時間投稿がありません")

    def prune(self):
        """古い記録を整理（state.json を太らせない）"""
        old = (self.today - datetime.timedelta(days=60)).isoformat()
        for k in ("posted", "digest_pool", "slots_done"):
            self.state[k] = {d: v for d, v in self.state[k].items() if d >= old}
        self.state["alerts"] = {k: v for k, v in self.state["alerts"].items() if k.rsplit(":", 1)[-1] >= old}
        self.state["candidates"] = {k: v for k, v in self.state["candidates"].items() if v.get("expires", "9999") >= old}
        for q in self.state["questions_pages"].values():
            q.pop("_obj", None)

    # ---------- 本体 ----------
    def run(self):
        if self.settings.get("停止スイッチ", "オフ") == "オン":
            self.say("停止スイッチがオンのため何もしません"); return
        self.say(f"巡回開始 {fmt_dt(self.now)} 試運転={'オン' if self.dry else 'オフ'} 投票日={'はい' if self.no_post_today else 'いいえ'} "
                 f"X鍵={self.x.mode or 'なし'} 台帳={'スプレッドシート' if self.ledger.remote else 'ローカル'}")
        try:
            if self.slots:   # 1日3枠の運用：区の新着と議会を候補にし、枠の時刻に1本ずつ出す（まとめ＝型4は出さない）
                steps = [self.retry_replies]
                if self.mode != "digest":
                    steps += [self.check_council, self.check_sokuho, self.check_rss]
                for step in steps + [self.slot_run, self.watch]:   # どこかで失敗しても、枠の投稿までは進める
                    try:
                        step()
                    except Exception as e:
                        self.alert(f"crash_{step.__name__}", f"巡回の「{step.__name__}」でエラー: {e}")
                        traceback.print_exc()
            elif self.mode == "digest":
                self.retry_replies(); self.post_digest()
            else:
                self.retry_replies()
                self.process_approvals()
                self.check_council()
                self.check_sokuho()
                self.check_rss()
                if self.now.hour >= 23:
                    self.post_digest()
                self.watch()
        except Exception as e:
            self.alert("crash", f"巡回でエラー: {e}")
            traceback.print_exc()
        finally:
            self.prune()
            self.state["last_run"] = fmt_dt(self.now)
            save_state(self.state)
            try:
                self.ledger.set_setting("最終巡回", fmt_dt(self.now) + ("（試運転）" if self.dry else ""))
            except Exception as e:
                print("最終巡回の更新に失敗", e)
            msg = ""
            if self.notices:
                msg += "【文京区まとめ】議会の投稿候補があります\n\n" + "\n\n".join(self.notices)
            if self.alerts:
                msg += ("\n\n" if msg else "") + "【文京区まとめ 見張り】\n" + "\n".join("・" + a for a in self.alerts)
            if msg:
                line(msg)
        self.say("巡回終了")


def check_keys():
    """鍵の確認だけをする（投稿しない）。Xに「このアカウントは誰か」を1回だけ尋ねる"""
    import os
    x = XClient(dry_run=False)
    print("X鍵の方式:", x.mode or "未登録")
    print("台帳の鍵（GSHEETS_SA_JSON）:", "あり" if os.environ.get("GSHEETS_SA_JSON") else "なし（リポジトリ内のファイルに書きます）")
    print("Claudeの鍵（ANTHROPIC_API_KEY）:", "あり" if os.environ.get("ANTHROPIC_API_KEY") else "なし")
    if not x.can_post:
        print("結果: Xの鍵が見つかりません。Secretsの名前を確かめてください"); return 1
    try:
        r = x._request("GET", "/users/me")
        if r.status_code == 200:
            print("結果: Xにつながりました。アカウントは @" + r.json()["data"]["username"]); return 0
        print(f"結果: Xが鍵を受け付けません（{r.status_code}）: {r.text[:300]}"); return 1
    except Exception as e:
        print("結果: Xへの接続でエラー:", e); return 1


def weighted_len(text):
    """Xの数え方での長さ（全角は2、URLは23）。280を超えると長文投稿（プレミアム等）が必要"""
    t = C.URL_RE.sub("", text)
    n = sum(1 if ord(ch) < 0x1100 else 2 for ch in t)
    return n + 23 * len(C.URL_RE.findall(text))


def post_row(row):
    """候補シートの1行を、先生の指示でその場で投稿する（試運転がオンでも投稿する。停止スイッチと投票日は守る）"""
    p = Patrol("post")
    if p.settings.get("停止スイッチ", "オフ") == "オン":
        print("結果: 停止スイッチがオンのため投稿しません"); return 1
    if not p.ledger.remote:
        print("結果: 台帳の鍵が無いので候補シートを読めません"); return 1
    r = next((x for x in p.ledger.rows("候補") if x["_row"] == row), None)
    if not r or not r.get("文面案"):
        print(f"結果: 候補シート{row}行目に文面がありません"); return 1
    parent, replies = C.from_cell(r["文面案"])
    problems = C.check_post(parent, replies, max_len=None)   # 長さはこの下でプレミアムの有無を見て判断
    if problems:
        print("結果: 機械チェックで止めました:", "、".join(problems)); return 1
    x = XClient(dry_run=False)
    if not x.can_post:
        print("結果: Xの鍵がありません"); return 1
    longest = max(weighted_len(t) for t in [parent] + replies)
    if longest > 280:
        me = x._request("GET", "/users/me", params={"user.fields": "subscription_type,verified_type"})
        sub = (me.json().get("data", {}) if me.status_code == 200 else {})
        print("アカウントの種別:", sub.get("subscription_type"), sub.get("verified_type"))
        if str(sub.get("subscription_type", "None")) in ("None", "") and str(sub.get("verified_type", "none")) in ("none", "None", ""):
            print(f"結果: 文面が長く（最長{longest}、上限280）、長文投稿できるプラン（Xプレミアム）が確認できないため、投稿を止めました"); return 1
    p.dry, p.x = False, x
    pid = p.post_thread(parent, replies, {"型": r.get("型", ""), "url": r.get("URL", ""), "確認": "先生の指示で手動実行"})
    if pid.startswith("未投稿"):
        print("結果:", pid); return 1
    pending = bool(p.state["pending_replies"])
    p.ledger.update_cell("候補", row, "確認", f"投稿済 {fmt_dt(p.now)} {pid}" + ("（返信は未完了）" if pending else ""))
    save_state(p.state)
    print(f"結果: 投稿しました https://x.com/bunkyo_matome/status/{pid}" + ("　※返信の投稿に失敗。次の巡回でやり直します" if pending else "（返信も投稿済み）"))
    for a in p.alerts:
        print("注意:", a)
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "patrol"
    if mode == "check":
        sys.exit(check_keys())
    if mode == "post":
        sys.exit(post_row(int(sys.argv[2])))
    Patrol(mode).run()
