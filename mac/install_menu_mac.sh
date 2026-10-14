#!/bin/bash
# Mac mini: run beside launchd aqd.alt; does not touch the fetch queue.
set -e; cd ~/aqd; mkdir -p bak_menu; cp -n *.py bak_menu/ 2>/dev/null || true
cp menu_ocr.py menu_worker.py menu_vocab.json ~/aqd/ 2>/dev/null || true
python3 -m pip install --user easyocr
AQD_ROOT=~/aqd AQD_MENU_FLOOR=0 nohup python3 ~/aqd/menu_worker.py >> ~/aqd/menu_worker.out 2>&1 &
echo started
