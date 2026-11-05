# -*- coding: utf-8 -*-
"""毎月1日の処理（文京区まとめ）
 1) 前月分の投稿台帳を書き出す（CSV）→ matome/reports/YYYY-MM/ に保存し、GDRIVE_FOLDER_ID があればGoogleドライブにも上げる
 2) 台帳の合計と請求額を照らし合わせる行を「月次照合」に足す（請求額は開発者コンソールにログインが要るので先生に保存を頼む）
 3) 前月の投稿の表示数（インプレッション）を X API の「自分の投稿の読み取り」で集めて報告する
 4) 投稿台帳の元資料URLのリンク切れを点検する
報告は朝刊が読めるよう「見張り」シートに書き、LINEにも送る。"""
import os, csv, io, json, datetime, requests
from pathlib import Path
from .util import now_jst, fmt_dt, UA
from .ledger import Ledger
from .xapi import XClient
from .patrol import line, ACCOUNT

PRICE_READ = 0.005  # 自分の投稿の読み取り 1件あたり（2026-11-05 docs.x.com：Posts: Read $0.005）


def prev_month(d):
    first = d.replace(day=1)
    last_prev = first - datetime.timedelta(days=1)
    return last_prev.year, last_prev.month


def main():
    now = now_jst()
    y, m = prev_month(now.date())
    ym = f"{y:04d}-{m:02d}"
    ledger = Ledger()
    rows = [r for r in ledger.rows("投稿台帳") if r.get("日時", "").startswith(ym) and r.get("アカウント") == ACCOUNT]
    total = 0.0
    for r in rows:
        try:
            total += float(r.get("単価（ドル）") or 0)
        except ValueError:
            pass
    posted_days = sorted({r["日時"][:10] for r in rows if not str(r.get("投稿ID", "")).startswith(("DRY", "未投稿"))})
    all_days = [(datetime.date(y, m, 1) + datetime.timedelta(days=i)).isoformat()
                for i in range(31) if (datetime.date(y, m, 1) + datetime.timedelta(days=i)).month == m]
    missing = [d for d in all_days if d not in posted_days]

    # 1) 書き出し
    out_dir = Path("matome/reports") / ym
    out_dir.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    head = ["日時", "アカウント", "型", "リンク", "単価（ドル）", "元資料", "確認", "区分", "投稿ID", "本文"]
    w = csv.writer(buf); w.writerow(head)
    for r in rows:
        w.writerow([r.get(h, "") for h in head])
    csv_path = out_dir / f"投稿台帳_{ym}.csv"
    csv_path.write_text(buf.getvalue(), encoding="utf-8-sig")
    saved_to = str(csv_path)
    folder = os.environ.get("GDRIVE_FOLDER_ID")
    if folder and ledger.remote:
        try:
            saved_to = upload_to_drive(ledger.session, folder, csv_path, ym) or saved_to
        except Exception as e:
            print("Driveへの保存に失敗", e)

    # 3) 表示数
    impressions, imp_note = None, "X鍵なし（集計せず）"
    x = XClient(dry_run=False)
    if x.can_post:
        try:
            start = f"{y:04d}-{m:02d}-01T00:00:00+09:00"
            end = (datetime.date(y, m, 1) + datetime.timedelta(days=32)).replace(day=1).isoformat() + "T00:00:00+09:00"
            posts = x.my_posts(start, end)
            impressions = sum(p["impressions"] for p in posts)
            imp_note = f"{len(posts)}本・表示数合計 {impressions}（読み取り費用 約{len(posts)*PRICE_READ:.3f}ドル）"
            ledger.append("投稿台帳", {"日時": fmt_dt(now), "アカウント": ACCOUNT, "型": "読み取り", "リンク": "—",
                                     "単価（ドル）": round(len(posts) * PRICE_READ, 3), "元資料": "", "確認": "月次",
                                     "区分": "政務活動費（会派）", "投稿ID": "", "本文": f"{ym} の表示数集計（{len(posts)}本）"})
            (out_dir / f"表示数_{ym}.json").write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            imp_note = f"表示数の取得に失敗: {e}"
        try:
            (out_dir / f"usage_{ym}.json").write_text(json.dumps(x.usage(), ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            print("usage取得失敗", e)

    # 4) リンク切れ点検
    broken = []
    for u in sorted({r.get("元資料", "") for r in rows if r.get("元資料", "").startswith("http")}):
        try:
            s = requests.head(u, headers=UA, timeout=20, allow_redirects=True).status_code
            if s >= 400:
                broken.append(f"{u}（{s}）")
        except Exception as e:
            broken.append(f"{u}（{e.__class__.__name__}）")

    # 2) 月次照合
    ledger.append("月次照合", {"年月": ym, "アカウント": ACCOUNT, "台帳の本数": len(rows), "台帳の合計（ドル）": round(total, 3),
                             "請求額（ドル）": "", "差": "", "差の理由": "請求額は開発者コンソールから先生が転記",
                             "証拠の保存先": saved_to})
    report = (f"【文京区まとめ 月次報告 {ym}】\n本数 {len(rows)}本／台帳の合計 {total:.3f}ドル\n"
              f"投稿がなかった日 {len(missing)}日" + (f"（{', '.join(missing[:10])}…）" if missing else "") +
              f"\n表示数：{imp_note}\nリンク切れ：{len(broken)}件" + ("\n" + "\n".join(broken[:10]) if broken else "") +
              f"\n台帳の書き出し：{saved_to}\n"
              "お願い：X開発者コンソール（console.x.com）の請求書と使用量を保存し、月次照合シートの「請求額」に転記してください。")
    ledger.append("見張り", {"日時": fmt_dt(now), "種類": "monthly", "内容": report, "対応": "先生：請求額の転記"})
    line(report)
    print(report)


def upload_to_drive(session, folder_id, path, ym):
    """ドライブの「0_AI作業用／政活費_X発信／年月」に置く（年月フォルダが無ければ作る）"""
    q = f"name='{ym}' and '{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
    r = session.get("https://www.googleapis.com/drive/v3/files", params={"q": q, "fields": "files(id,name)"}); r.raise_for_status()
    files = r.json().get("files", [])
    if files:
        sub = files[0]["id"]
    else:
        r = session.post("https://www.googleapis.com/drive/v3/files",
                         json={"name": ym, "mimeType": "application/vnd.google-apps.folder", "parents": [folder_id]}); r.raise_for_status()
        sub = r.json()["id"]
    meta = {"name": path.name, "parents": [sub]}
    files_payload = {"metadata": ("metadata", json.dumps(meta), "application/json; charset=UTF-8"),
                     "file": (path.name, path.read_bytes(), "text/csv")}
    r = session.post("https://www.googleapis.com/upload/drive/v3/files", params={"uploadType": "multipart", "fields": "id,webViewLink"},
                     files=files_payload); r.raise_for_status()
    return r.json().get("webViewLink")


if __name__ == "__main__":
    main()
