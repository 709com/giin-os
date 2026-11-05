# 議員OS（giin-os）作業メモ

文京区議会議員・たかはまなおき（「先生」）の事務作業を自動化するリポジトリ。全体像と使い方は `README.md`。
返事・報告は日本語。先生はコードをほとんど読めないので、専門用語は噛み砕いて書く。
設定の変更・ワークフローの有効化・対外投稿は、内容とリスクを示して先生の「OK」を得てから。ファイルは削除しない（移動のみ）。

## 装置の一覧

| 装置 | 入口 | 説明 |
|---|---|---|
| 朝の巡回（庁議・委員会速報） | `.github/workflows/01_asa-junkai.yml` → `automation/monitor.py` | 新着を検知して記事案とLINE通知。2026年10月27日に定期実行を止め、手動のみ |
| LINE週刊コラム・配信 | `02_line-shitagaki.yml` / `03_shonin-haishin.yml` | 木曜の下書き→承認ボタンで一斉配信 |
| 選挙アナリスト・財源ハンター | `04_*.yml` / `05_*.yml` | 月次レポート |
| 質問パイプライン | `.claude/agents/` | 資料分析→他自治体調査→質問案→広報 |
| **文京区まとめ（会派X @bunkyo_matome）** | `06_bunkyo-matome.yml` / `07_matome-getsuji.yml` → `automation/matome/` | 区の新着と議会の日程・資料をXに自動投稿。台帳はスプレッドシート「文京区まとめ 投稿台帳」。設定シートの「試運転」がオンの間は投稿しない。詳細は `matome/README.md` |

## 鍵（GitHub Secrets）

`ANTHROPIC_API_KEY`, `LINE_CHANNEL_TOKEN`, `LINE_USER_ID`（既存）。文京区まとめ用に `GSHEETS_SA_JSON`, `X_CLIENT_ID`, `X_CLIENT_SECRET`, `X_REFRESH_TOKEN`, `GH_SECRETS_PAT`（任意で `GDRIVE_FOLDER_ID`、OAuth 1.0a を使うなら `X_API_KEY` ほか）。
鍵はチャット・スプレッドシート・コード・コミットに書かない。
