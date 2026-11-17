@echo off
chcp 65001 > nul
cd /d "%~dp0"
rem 7:10にPCが切れていた日は、次にPCを使い始めた時に実行する。バッテリー動作中でも実行する。
powershell -NoProfile -ExecutionPolicy Bypass -Command "$a = New-ScheduledTaskAction -Execute (Join-Path (Get-Location) 'run.bat'); $t = New-ScheduledTaskTrigger -Daily -At '07:10'; $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2); Register-ScheduledTask -TaskName 'SideBooksCheck' -Action $a -Trigger $t -Settings $s -Force | Out-Null; if ($?) { Write-Host '登録しました。毎朝7:10に実行します。その時刻にPCが切れていた日は、次にPCを使い始めた時に実行します。' } else { Write-Host '登録に失敗しました。この画面の写真をClaudeに見せてください。' }"
pause
