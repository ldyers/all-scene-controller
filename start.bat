@echo off
rem all-scene-controller 启动脚本 (Windows)
rem 需要已安装 Python 3.8+ (python 在 PATH 中)
cd /d "%~dp0"
if "%SCENE_PORT%"=="" set SCENE_PORT=23480
if "%OBS_WS_HOST%"=="" set OBS_WS_HOST=127.0.0.1
if "%OBS_WS_PORT%"=="" set OBS_WS_PORT=4455
python scene_switcher.py
pause
