@echo off
chcp 65001 > nul
cd /d "%~dp0"
schtasks /create /tn "SideBooksCheck" /tr "\"%~dp0run.bat\"" /sc daily /st 07:10 /f
echo.
echo 登録しました。毎朝7:10に実行します（PCの電源が入っていてログインしている時）。
pause
