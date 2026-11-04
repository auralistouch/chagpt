#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 evaluate.py --days 30 --output work/pullback-evaluation.json
status=$?
if [ "$status" -eq 0 ]; then
    printf '\n报告位于 work/pullback-evaluation.json；这是历史研究，不是实际订单结果。\n'
else
    printf '\n获取或评估失败，请查看上方提示；没有生成有效评估结论。\n'
fi
printf '按回车关闭。\n'
read -r answer
exit "$status"
