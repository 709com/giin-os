# -*- coding: utf-8 -*-
"""台帳（Googleスプレッドシート「文京区まとめ 投稿台帳」）の読み書き。
GSHEETS_SA_JSON（サービスアカウントの鍵、GitHub Secrets）が無いときは、
リポジトリ内の matome/ledger_local.json に同じ形で書く（試運転・ローカル確認用）。"""
import os, json
from pathlib import Path

SPREADSHEET_ID = os.environ.get("MATOME_SHEET_ID", "1pJ8DPLDQURC9IzrwyPWmKYsV58tmx4TGJwUo_v34qBo")
LOCAL_PATH = Path("matome/ledger_local.json")
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"

HEADERS = {
    "候補": ["拾った日時", "掲載日", "情報源", "型", "件名", "分野", "担当課", "URL", "文面案", "単独/まとめ", "確認", "備考"],
    "投稿台帳": ["日時", "アカウント", "型", "リンク", "単価（ドル）", "元資料", "確認", "区分", "投稿ID", "本文"],
    "月次照合": ["年月", "アカウント", "台帳の本数", "台帳の合計（ドル）", "請求額（ドル）", "差", "差の理由", "証拠の保存先"],
    "xAIクレジット": ["日付", "アカウント", "受け取った額（ドル）", "使った額（ドル）", "使い道", "備考"],
    "見張り": ["日時", "種類", "内容", "対応"],
}
DEFAULT_SETTINGS = {"試運転": "オン", "停止スイッチ": "オフ", "1日の単独投稿の上限": "3",
                    "先生の確認": "議会のみ", "投票日": "未定", "会派の月の上限（ドル）": "30"}


class Ledger:
    def __init__(self):
        self.sa = os.environ.get("GSHEETS_SA_JSON")
        self.session = None
        self.remote = bool(self.sa)
        if self.remote:
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession
            info = json.loads(self.sa)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets",
                              "https://www.googleapis.com/auth/drive.file"])
            self.session = AuthorizedSession(creds)
            self._sheet_titles = None
        else:
            self.local = json.loads(LOCAL_PATH.read_text(encoding="utf-8")) if LOCAL_PATH.exists() else {}

    # ---- 低レベル ----
    def _get(self, rng):
        r = self.session.get(f"{SHEETS_API}/{SPREADSHEET_ID}/values/{rng}")
        r.raise_for_status()
        return r.json().get("values", [])

    def _append(self, sheet, rows):
        r = self.session.post(f"{SHEETS_API}/{SPREADSHEET_ID}/values/{sheet}!A1:append",
                              params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                              json={"values": rows})
        r.raise_for_status()
        import re as _re
        m = _re.search(r"!A(\d+)", r.json().get("updates", {}).get("updatedRange", ""))
        return int(m.group(1)) if m else None

    def _update(self, rng, rows):
        r = self.session.put(f"{SHEETS_API}/{SPREADSHEET_ID}/values/{rng}",
                             params={"valueInputOption": "USER_ENTERED"}, json={"values": rows})
        r.raise_for_status()

    def _ensure_sheet(self, title):
        if self._sheet_titles is None:
            r = self.session.get(f"{SHEETS_API}/{SPREADSHEET_ID}", params={"fields": "sheets.properties.title"})
            r.raise_for_status()
            self._sheet_titles = [s["properties"]["title"] for s in r.json().get("sheets", [])]
        if title not in self._sheet_titles:
            self.session.post(f"{SHEETS_API}/{SPREADSHEET_ID}:batchUpdate",
                              json={"requests": [{"addSheet": {"properties": {"title": title}}}]}).raise_for_status()
            self._append(title, [HEADERS[title]])
            self._sheet_titles.append(title)

    def _save_local(self):
        LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_PATH.write_text(json.dumps(self.local, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- 読む ----
    def rows(self, sheet):
        """[{列名: 値}, ...]（行番号は '_row'、1始まり）"""
        if self.remote:
            self._ensure_sheet(sheet)
            vals = self._get(f"{sheet}!A1:Z2000")
        else:
            vals = [HEADERS[sheet]] + self.local.get(sheet, [])
        if not vals:
            return []
        head = vals[0]
        out = []
        for i, v in enumerate(vals[1:], start=2):
            d = {h: (v[j] if j < len(v) else "") for j, h in enumerate(head)}
            d["_row"] = i
            out.append(d)
        return out

    def settings(self):
        """設定シート → {項目: 値}"""
        d = dict(DEFAULT_SETTINGS)
        if self.remote:
            for v in self._get("設定!A2:B30"):
                if len(v) >= 2:
                    d[v[0].strip()] = v[1].strip()
        else:
            d.update(self.local.get("設定", {}))
        return d

    def set_setting(self, key, value):
        if self.remote:
            vals = self._get("設定!A1:B30")
            for i, v in enumerate(vals, start=1):
                if v and v[0].strip() == key:
                    self._update(f"設定!B{i}", [[value]]); return
            self._append("設定", [[key, value]])
        else:
            self.local.setdefault("設定", {})[key] = value; self._save_local()

    # ---- 書く ----
    def append(self, sheet, record):
        """record は {列名: 値}。列の並びはシートの見出しに合わせる"""
        head = HEADERS[sheet]
        if self.remote:
            self._ensure_sheet(sheet)
            live = self._get(f"{sheet}!1:1")
            if live and live[0]:
                head = live[0]
            return self._append(sheet, [[str(record.get(h, "")) for h in head]])
        else:
            self.local.setdefault(sheet, []).append([str(record.get(h, "")) for h in head]); self._save_local()
            return len(self.local[sheet]) + 1

    def update_cell(self, sheet, row, col_name, value):
        head = HEADERS[sheet]
        if self.remote:
            live = self._get(f"{sheet}!1:1")
            if live and live[0]:
                head = live[0]
            col = head.index(col_name)
            self._update(f"{sheet}!{chr(65 + col)}{row}", [[str(value)]])
        else:
            col = head.index(col_name)
            r = self.local[sheet][row - 2]
            while len(r) <= col:
                r.append("")
            r[col] = str(value); self._save_local()
