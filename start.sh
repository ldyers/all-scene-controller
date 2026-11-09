#!/usr/bin/env bash
# all-scene-controller 启动脚本 (Ubuntu/Linux)
cd "$(dirname "$0")"
export SCENE_PORT="${SCENE_PORT:-23480}"
export OBS_WS_HOST="${OBS_WS_HOST:-127.0.0.1}"
export OBS_WS_PORT="${OBS_WS_PORT:-4455}"
export OBS_PASSWORD="${OBS_PASSWORD:-}"
exec python3 scene_switcher.py
