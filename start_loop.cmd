@echo off
chcp 65001 >nul
cd /d %~dp0
where python >nul 2>nul || (echo 没找到 python，请先装 Python 3.10+ 并加入 PATH & pause & exit /b 1)
echo 开始自动刷课（视频 -> 章节测验 -> 下一节）…
python course_auto.py loop --auto-solve --max-chapters 30
pause
