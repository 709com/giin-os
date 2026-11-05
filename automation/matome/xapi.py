# -*- coding: utf-8 -*-
"""X API（v2）で投稿する部品。鍵はすべて環境変数（GitHub Secrets）から。
優先順位：
 1) OAuth 2.0（X_CLIENT_ID / X_CLIENT_SECRET / X_REFRESH_TOKEN）。リフレッシュトークンは使うたびに新しくなるので、
    GH_SECRETS_PAT（repo の secrets を書ける PAT）があれば GitHub Secrets を自動で更新する。無ければ警告を返す。
 2) OAuth 1.0a（X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET）。期限切れが無く、運用が楽。
どちらも無ければ投稿できない（試運転扱い）。"""
import os, base64, json, requests

API = "https://api.x.com/2"


class XClient:
    def __init__(self, dry_run=True):
        self.dry_run = dry_run
        self.warnings = []
        self.mode = None
        self.session = None
        self.bearer = None
        self.me = None
        if os.environ.get("X_CLIENT_ID") and os.environ.get("X_REFRESH_TOKEN"):
            self.mode = "oauth2"
        elif os.environ.get("X_API_KEY") and os.environ.get("X_ACCESS_TOKEN"):
            self.mode = "oauth1a"

    @property
    def can_post(self):
        return self.mode is not None

    # ---- 認証 ----
    def _auth(self):
        if self.mode == "oauth2" and not self.bearer:
            cid, csec, rt = os.environ["X_CLIENT_ID"], os.environ.get("X_CLIENT_SECRET", ""), os.environ["X_REFRESH_TOKEN"]
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if csec:
                headers["Authorization"] = "Basic " + base64.b64encode(f"{cid}:{csec}".encode()).decode()
            r = requests.post(f"{API}/oauth2/token", headers=headers,
                              data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid}, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"トークンの更新に失敗（{r.status_code}）: {r.text[:200]}")
            tok = r.json()
            self.bearer = tok["access_token"]
            new_rt = tok.get("refresh_token")
            if new_rt and new_rt != rt:
                self._rotate_secret("X_REFRESH_TOKEN", new_rt)
        elif self.mode == "oauth1a" and not self.session:
            from requests_oauthlib import OAuth1Session
            self.session = OAuth1Session(os.environ["X_API_KEY"], os.environ["X_API_SECRET"],
                                         os.environ["X_ACCESS_TOKEN"], os.environ["X_ACCESS_SECRET"])

    def _rotate_secret(self, name, value):
        """GitHub Secrets を更新（PyNaCl で封をして PUT）。できなければ警告"""
        pat, repo = os.environ.get("GH_SECRETS_PAT"), os.environ.get("GITHUB_REPOSITORY")
        if not pat or not repo:
            self.warnings.append("新しいリフレッシュトークンを保存できません（GH_SECRETS_PAT が未設定）。次回の実行で期限切れになる恐れ")
            return
        try:
            from nacl import encoding, public
            h = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
            k = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key", headers=h, timeout=30).json()
            box = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder()))
            enc = base64.b64encode(box.encrypt(value.encode())).decode()
            r = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/{name}", headers=h,
                             json={"encrypted_value": enc, "key_id": k["key_id"]}, timeout=30)
            if r.status_code not in (201, 204):
                self.warnings.append(f"GitHub Secrets の更新に失敗（{r.status_code}）")
        except Exception as e:
            self.warnings.append(f"GitHub Secrets の更新でエラー: {e}")

    def _request(self, method, path, **kw):
        self._auth()
        if self.mode == "oauth2":
            kw.setdefault("headers", {})["Authorization"] = f"Bearer {self.bearer}"
            r = requests.request(method, f"{API}{path}", timeout=30, **kw)
        else:
            r = self.session.request(method, f"{API}{path}", timeout=30, **kw)
        return r

    # ---- 投稿 ----
    def post(self, text, reply_to=None):
        """投稿して投稿IDを返す。試運転なら 'DRY-xxxx' を返す"""
        if self.dry_run or not self.can_post:
            return "DRY-" + str(abs(hash(text)) % 10**8)
        body = {"text": text}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        r = self._request("POST", "/tweets", json=body)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"投稿に失敗（{r.status_code}）: {r.text[:300]}")
        return r.json()["data"]["id"]

    # ---- 読む（月次の表示数用。自分の投稿の読み取り）----
    def my_id(self):
        if not self.me:
            r = self._request("GET", "/users/me")
            r.raise_for_status()
            self.me = r.json()["data"]["id"]
        return self.me

    def my_posts(self, start_iso, end_iso):
        """期間内の自分の投稿 [{id, text, created_at, impressions}]"""
        out, token = [], None
        while True:
            params = {"max_results": 100, "tweet.fields": "created_at,public_metrics",
                      "start_time": start_iso, "end_time": end_iso}
            if token:
                params["pagination_token"] = token
            r = self._request("GET", f"/users/{self.my_id()}/tweets", params=params)
            r.raise_for_status()
            j = r.json()
            for t in j.get("data", []):
                out.append({"id": t["id"], "text": t.get("text", ""), "created_at": t.get("created_at", ""),
                            "impressions": t.get("public_metrics", {}).get("impression_count", 0)})
            token = j.get("meta", {}).get("next_token")
            if not token:
                return out

    def usage(self):
        r = self._request("GET", "/usage/tweets")
        return r.json() if r.status_code == 200 else {"error": r.status_code, "body": r.text[:200]}
