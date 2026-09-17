@echo off
rem Toy static launcher: build package, serve, open browser (same as python start_toy.py)
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 goto nopython
python start_toy.py %*
goto end
:nopython
echo 未找到 python，请先安装 Python 3 并加入 PATH。
pause
exit /b 1
:end
pause
