#!/bin/sh
cd "$(dirname "$0")" || exit 1
python3 compare.py --days 30 --output work/comparison.json
printf '\n完成。请将终端结果与 work/comparison.json 提供用于复核。按回车退出。\n'
read answer
