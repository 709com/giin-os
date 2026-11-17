@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ■ 自動実行の登録状況
powershell -NoProfile -Command "$t = Get-ScheduledTask -TaskName 'SideBooksCheck' -ErrorAction SilentlyContinue; if (-not $t) { Write-Host '  登録されていません（register-task.bat をダブルクリックしてください）' } else { $i = $t | Get-ScheduledTaskInfo; Write-Host ('  状態：' + $t.State); Write-Host ('  前回の実行：' + $i.LastRunTime + '（結果コード ' + $i.LastTaskResult + '。0なら正常）'); Write-Host ('  次回の実行：' + $i.NextRunTime) }"
echo.
echo ■ 鍵の登録（値は表示しません）
if defined SIDEBOOKS_USER (echo   SIDEBOOKS_USER あり) else (echo   SIDEBOOKS_USER なし)
if defined SIDEBOOKS_PASS (echo   SIDEBOOKS_PASS あり) else (echo   SIDEBOOKS_PASS なし)
if defined LINE_CHANNEL_TOKEN (echo   LINE_CHANNEL_TOKEN あり) else (echo   LINE_CHANNEL_TOKEN なし)
if defined LINE_USER_ID (echo   LINE_USER_ID あり) else (echo   LINE_USER_ID なし)
echo.
echo ■ 最近の記録（log.txt の終わり40行）
if exist log.txt (powershell -NoProfile -Command "Get-Content -Encoding UTF8 log.txt -Tail 40") else (echo   log.txt がありません（まだ一度も自動実行されていません）)
echo.
pause
