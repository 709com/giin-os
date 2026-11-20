@echo off
chcp 65001 > nul
cd /d "%~dp0"
rem use the browser installed inside node_modules (README step 1)
set PLAYWRIGHT_BROWSERS_PATH=0
echo ==== %date% %time% ==== >> log.txt
node check.js %* >> log.txt 2>&1
