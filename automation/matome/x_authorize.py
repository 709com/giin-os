# -*- coding: utf-8 -*-
"""X の OAuth 2.0 で「リフレッシュトークン」を1回だけ取り出す道具（先生のPCで実行）。
使い方：
  pip install requests
  X_CLIENT_ID=（開発者コンソールの Client ID） X_CLIENT_SECRET=（Client Secret） python automation/matome/x_authorize.py
→ 表示されたURLをブラウザで開き、@bunkyo_matome でログインして許可 → 戻り先URL（http://127.0.0.1:8765/callback?code=... ）に
  飛ぶとこの画面にトークンが表示される。表示された X_REFRESH_TOKEN を GitHub Secrets に登録する（画面以外には残さない）。
前提：開発者コンソールのアプリ設定で、Type of App = Web App / Automated App、Callback URL に http://127.0.0.1:8765/callback、
      App permissions = Read and write を設定しておく。"""
import os, base64, hashlib, secrets, urllib.parse, http.server, requests

CLIENT_ID = os.environ["X_CLIENT_ID"]
CLIENT_SECRET = os.environ.get("X_CLIENT_SECRET", "")
REDIRECT = "http://127.0.0.1:8765/callback"
SCOPES = "tweet.read tweet.write users.read offline.access"

verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
state = secrets.token_urlsafe(16)
url = "https://x.com/i/oauth2/authorize?" + urllib.parse.urlencode({
    "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "scope": SCOPES,
    "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
print("\nこのURLをブラウザで開いて、@bunkyo_matome でログインし「許可」を押してください：\n\n" + url + "\n")

code_holder = {}

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] != state:
            self.send_response(400); self.end_headers(); self.wfile.write(b"state mismatch"); return
        code_holder["code"] = q.get("code", [""])[0]
        self.send_response(200); self.send_header("Content-Type", "text/plain; charset=utf-8"); self.end_headers()
        self.wfile.write("許可を受け取りました。ターミナルの画面に戻ってください。".encode())
    def log_message(self, *a):
        pass

srv = http.server.HTTPServer(("127.0.0.1", 8765), H)
srv.handle_request()
headers = {"Content-Type": "application/x-www-form-urlencoded"}
if CLIENT_SECRET:
    headers["Authorization"] = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
r = requests.post("https://api.x.com/2/oauth2/token", headers=headers, data={
    "grant_type": "authorization_code", "code": code_holder["code"], "redirect_uri": REDIRECT,
    "client_id": CLIENT_ID, "code_verifier": verifier}, timeout=30)
if r.status_code != 200:
    print("失敗:", r.status_code, r.text); raise SystemExit(1)
tok = r.json()
print("\n=== GitHub Secrets に登録する値（この画面以外に残さない）===")
print("X_REFRESH_TOKEN =", tok.get("refresh_token"))
print("（アクセストークンは2時間で切れるので登録不要。巡回が毎回リフレッシュトークンから作り直します）")
