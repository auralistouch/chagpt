@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 启动后，在浏览器地址栏输入 http://127.0.0.1:8000
echo 关闭这个窗口即可停止程序。不需要币安密钥，不会自动下单。
py -3 app.py
pause
