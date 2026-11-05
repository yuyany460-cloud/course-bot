@echo off
chcp 65001 >nul
cd /d %~dp0
set PY=python
where python >nul 2>nul || set PY=D:\code\python\python.exe
echo 开始自动刷课（视频 -> 章节测验 -> 下一节）…
%PY% course_auto.py loop --auto-solve --max-chapters 30
pause
