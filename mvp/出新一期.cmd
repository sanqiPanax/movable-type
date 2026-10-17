@echo off
rem 出新一期:抓48小时新闻 -> 打分 -> AI主编 -> 配图 -> 生成报纸并打开
cd /d "F:\DoMyProject\魔法日报\mvp"
python -X utf8 build_paper.py --fetch
if errorlevel 1 (
  echo.
  echo 构建失败,按任意键退出...
  pause >nul
  exit /b 1
)
start "" "F:\DoMyProject\魔法日报\mvp\out\index.html"
