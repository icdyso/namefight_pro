@echo off
rem 一键启动：自动挑空闲端口 -> 启动服务器 -> 打开浏览器（等效 python start.py）
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 goto nopython
python start.py %*
goto end
:nopython
echo 未找到 python，请先安装 Python 3 并加入 PATH。
pause
exit /b 1
:end
pause
