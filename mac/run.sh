#!/bin/zsh
cd ~/aqd
rm -f STOP STOP_ALL
nohup caffeinate -dimsu ./venv/bin/python alt.py > alt.nohup 2>&1 &
echo started
