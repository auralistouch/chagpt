#!/bin/sh
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    printf '\n请先安装 Python 3.10 或更新版本：\nhttps://www.python.org/downloads/macos/\n\n安装后重新双击本文件。按回车关闭。\n'
    read -r answer
    exit 1
fi
printf '\n程序正在启动。请保持本窗口打开。\n在浏览器地址栏输入：http://127.0.0.1:8000\n按 Control+C 停止程序。\n\n'
if [ "$(uname -s)" = "Darwin" ] && command -v caffeinate >/dev/null 2>&1; then
    printf '运行期间将防止 Mac 闲置休眠；屏幕可熄灭，合盖或关机仍会停止监测。\n\n'
    caffeinate -i python3 app.py
else
    python3 app.py
fi
status=$?
if [ "$status" -ne 0 ]; then
    printf '\n启动失败，请将上方提示发给协助人员。按回车关闭。\n'
    read -r answer
fi
exit "$status"
