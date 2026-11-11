@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo ==== %date% %time% ==== >> log.txt
node check.js %* >> log.txt 2>&1
