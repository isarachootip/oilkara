@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul && (python server.py & goto end)
where py >nul 2>nul && (py server.py & goto end)
echo ไม่พบ Python ในเครื่องนี้ ติดตั้งได้ที่ https://www.python.org/downloads/
echo ตอนติดตั้งให้ติ๊ก "Add python.exe to PATH"
:end
pause
