#!/bin/sh
cd "$(dirname "$0")" || exit 1
printf '\n评估下载工具 v5.0：请保持本窗口打开，进度会逐页显示。\n下载中断会保留进度，下次双击可继续，不用从头下载。\n\n'
python3 -u evaluate.py --days 30 --output work/pullback-evaluation.json
status=$?
if [ "$status" -eq 0 ]; then
    printf '\n评估成功，报告已生成：\n%s/work/pullback-evaluation.json\n' "$PWD"
    if [ "$(uname -s)" = "Darwin" ]; then
        /usr/bin/open -R "$PWD/work/pullback-evaluation.json"
    fi
    printf '\nFinder已选中报告时，可将它拖到聊天里。这是历史研究，不是实际订单结果。\n'
else
    printf '\n本次未生成新报告，完整下载页已保留。网络恢复后再次双击本文件继续。\n'
fi
printf '按回车关闭。\n'
read -r answer
exit "$status"
