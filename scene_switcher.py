#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
all-scene-controller v1.0 — 弹幕关键词 OBS 场景切换独立服务
从 live-brain(:23460) 完整剥离的弹幕切场景子系统，单文件、零第三方依赖。
Ubuntu / Windows 双平台原生运行（Python 3.8+，无需 pip install）。

功能 = 23460 同款：
  - 三级触发词体系（一级=品类入口词 → 二级=品类词 → 三级=档位·毫米）
  - scenes/ 目录品类配置 + mtime 轮询热重载（WebUI/手动改文件免重启）
  - /scenes 控件化配置页（品类标签页/触发词tag/OBS场景下拉映射/意图模拟器）
  - OBS websocket 5.x 纯标准库客户端（_MiniWS，含鉴权，断线自愈重连）
  - 弹幕文件 tail（用户发言记录*.txt / 淘宝桥 用户发言记录_tb.txt）
  - 切换延迟/锁定保持/到期回默认 + 场景切换后哑源修复(guard)
  - duck 音量账本（TTS 播报静音视频源的场景可复用；本服务仅保留账本+修复能力）

来源: ~/live-brain-sys/live-brain/live_brain.py (3765行) — 剥离日 2026-11-08
"""
import base64
import hashlib
import http.client
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_PORT     = int(os.environ.get("SCENE_PORT", "23480"))
LOG_DIR      = os.environ.get("SCENE_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
OBS_HOST     = os.environ.get("OBS_WS_HOST", "127.0.0.1")
OBS_PORT     = int(os.environ.get("OBS_WS_PORT", "4455"))
OBS_PASSWORD = os.environ.get("OBS_PASSWORD", "")   # 本机OBS无鉴权时留空
DUCK_KINDS     = ("ffmpeg_source", "vlc_source", "browser_source", "game_capture")
DUCK_LEDGER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "duck_ledger.json")
POLL_INTERVAL  = float(os.environ.get("POLL_INTERVAL", "0.5"))

# ---------------- 可调配置(WebUI可改, 持久化config.json) ----------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG = {
    "duck_delay_ms": 0,        # 预留: 静音后延迟播放(ms)
    "duck_fade_ms": 0,         # 播报时视频声音淡出时长(0=立刻静音)
    "unduck_fade_ms": 0,       # 播完后视频声音淡入时长(0=立刻恢复)
    "duck_scene_guard": True,  # 切景后场景感知兜底: 自动救当前场景泄漏哑源
    "danmu_log_dir": "",       # 弹幕文件目录; 空=程序目录/logs
    "scene_enabled": True,     # 弹幕关键词自动切场景
    "scene_switch_delay_s": 3, # 检测到指令后延迟几秒切换(窗口内新指令覆盖旧定时)
    "scene_hold_s": 180,       # 切换后保持秒数, 到期自动回默认场景; 期间锁定忽略新指令
}
CONFIG = dict(DEFAULT_CONFIG)

# ---------------- 场景规则: scenes/ 目录配置 + 热重载 ----------------
# 目录里 .json 文件 = 品类规则文件（文件和文件夹均支持，递归扫描）;
# 文件一保存(含手动改) -> mtime轮询自动重载内存表, 不重启生效。
SCENES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenes")

# 三级触发词体系:
#   一级触发词 = 品类入口词(trigger_words)  —— 命中但未指名品类 → 落到缺省品类
#   二级触发词 = 品类词(bubble_words)       —— 命中 → 本品类; 缺省 = 白水
#   三级触发词 = 档位(levels)               —— 数字就近归档; 缺省 = 10米
# 文件里两种词汇都认; 同时存在时以英文键为准(保证手改 trigger_words 不会被忽略)
TIER1_KEYS = ("trigger_words", "一级触发词")
TIER2_KEYS = ("bubble_words", "二级触发词")
TIER3_KEYS = ("levels", "三级触发词", "档位")
DEFAULT_TIER2 = "白水"          # 二级触发词缺省品类
DEFAULT_TIER3 = 10              # 三级触发词缺省档位(10米)

_DEFAULT_SCENE_FILES = {
    "_global.json": {
        "default_scene": "默认",       # 锁定期结束回到的场景
        "hold_s": 180,                 # 切换后保持秒数, 0=不回默认
        "switch_delay_s": 3,           # 检测到指令后延迟几秒切换
    },
    "白水.json": {
        "name": "白水",
        "trigger_words": ["看", "卡", "试", "展示"],
        "bubble_words": ["白水"],
        "levels": [8, 10, 12, 14],
        "aliases": {},
        "priority": 100,
        "default_level": 10,
        "scenes": [{"cmd": "8", "obs_scene": "8米"}, {"cmd": "10", "obs_scene": "10米"},
                   {"cmd": "12", "obs_scene": "12米"}, {"cmd": "14", "obs_scene": "14米"}],
    },
    "泡泡珠.json": {
        "name": "泡泡珠",
        "trigger_words": ["看", "卡", "试", "展示"],
        "bubble_words": ["泡泡珠", "泡泡", "泡子", "珠珠"],
        "levels": [10, 12, 14],
        "aliases": {},
        "priority": 200,
        "default_level": 10,
        "scenes": [{"cmd": "10泡", "obs_scene": "10泡"}, {"cmd": "12泡", "obs_scene": "12泡"},
                   {"cmd": "14泡", "obs_scene": "14泡"}],
    },
    "紫水.json": {
        "name": "紫水",
        "trigger_words": ["看", "卡", "试", "展示"],
        "bubble_words": ["紫水", "紫水晶"],
        "levels": [6, 8, 10],
        "aliases": {},
        "priority": 300,
        "default_level": 10,
        "scenes": [{"cmd": "6紫", "obs_scene": "6紫"}, {"cmd": "8紫", "obs_scene": "8紫"},
                   {"cmd": "10紫", "obs_scene": "10紫"}],
    },
    "黄水.json": {
        "name": "黄水",
        "trigger_words": ["看", "卡", "试", "展示"],
        "bubble_words": ["黄水", "黄水晶"],
        "levels": [6, 8, 10],
        "aliases": {},
        "priority": 300,
        "default_level": 10,
        "scenes": [{"cmd": "6黄", "obs_scene": "6黄"}, {"cmd": "8黄", "obs_scene": "8黄"},
                   {"cmd": "10黄", "obs_scene": "10黄"}],
    },
}

def _scene_seed_files():
    """首启生成初始品类文件; 目录不存在时整体建目录"""
    os.makedirs(SCENES_DIR, exist_ok=True)
    for fname, body in _DEFAULT_SCENE_FILES.items():
        p = os.path.join(SCENES_DIR, fname)
        if not os.path.exists(p):
            try:
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(body, f, ensure_ascii=False, indent=2)
                log("scenes: 生成初始配置 %s" % fname)
            except Exception as e:
                err("scenes: 写初始配置 %s 失败: %s" % (fname, e))

def scenes_scan():
    """递归扫描 scenes/ 下所有 .json(支持文件+文件夹), 返回 {relpath: mtime}"""
    out = {}
    try:
        for root, _dirs, files in os.walk(SCENES_DIR):
            for fn in files:
                if fn.lower().endswith(".json"):
                    p = os.path.join(root, fn)
                    try:
                        out[os.path.relpath(p, SCENES_DIR)] = os.path.getmtime(p)
                    except OSError:
                        pass
    except OSError:
        pass
    return out

def _load_category(body, fname):
    """校验+归一化单个品类文件; 返回dict或None"""
    if not isinstance(body, dict):
        return None
    name = str(body.get("name") or os.path.splitext(os.path.basename(fname))[0]).strip()
    if not name or name.startswith("_"):
        return None
    def _words(v):
        return tuple(str(x).strip() for x in (v or []) if str(x).strip())
    def _pick(*keys):
        """按新→旧词汇取字段: 支持 一级/二级/三级触发词 与旧键 trigger_words/bubble_words/levels"""
        for k in keys:
            if k in body:
                return body.get(k)
        return None
    scenes = []
    for s in body.get("scenes") or []:
        if not isinstance(s, dict):
            continue
        cmd = str(s.get("cmd") or "").strip()
        obs = str(s.get("obs_scene") or cmd).strip()
        if cmd:
            scenes.append({"cmd": cmd, "name": obs})
    try:
        levels = sorted({int(x) for x in (_pick(*TIER3_KEYS) or [])})
    except (TypeError, ValueError):
        levels = []
    try:
        priority = int(body.get("priority") or 100)
    except (TypeError, ValueError):
        priority = 100
    try:
        default_level = int(_pick("三级缺省", "default_level") or (levels[0] if levels else DEFAULT_TIER3))
    except (TypeError, ValueError):
        default_level = levels[0] if levels else DEFAULT_TIER3
    if not levels:
        levels = [DEFAULT_TIER3]           # 三级触发词缺省 = 10米
    if not scenes:
        return None
    t1 = _words(_pick(*TIER1_KEYS))
    t2 = _words(_pick(*TIER2_KEYS))
    return {
        "name": name, "file": fname,
        "trigger_words": t1,
        "bubble_words": t2,
        # 新词汇镜像(前端/接口统一用三级触发词术语)
        "一级触发词": t1,
        "二级触发词": t2,
        "三级触发词": levels,
        "缺省二级": (None if t2 else DEFAULT_TIER2),   # 二级触发词缺省 = 白水
        "缺省三级": default_level,                     # 三级触发词缺省档
        # 别名: 值为正整数=自带档位(如{"珍珠":14}), true=仅品类标记
        "aliases": {str(k): (int(v) if isinstance(v, (int, float))
                             and not isinstance(v, bool) and v > 0 else True)
                    for k, v in (body.get("aliases") or {}).items()},
        "levels": levels, "priority": priority, "default_level": default_level,
        "scenes": scenes,
    }

def scenes_load_all():
    """全量加载目录 -> 内存表(SCENE_TABLE/SCENE_BY_CMD/CATEGORIES)。坏文件跳过不致命"""
    cats = {}
    global_cfg = {"default_scene": "默认", "hold_s": 180, "switch_delay_s": 3}
    for rel, _mt in sorted(scenes_scan().items()):
        p = os.path.join(SCENES_DIR, rel)
        try:
            with open(p, "r", encoding="utf-8") as f:
                body = json.load(f)
        except Exception as e:
            err("scenes: 加载 %s 失败(跳过): %s" % (rel, e))
            continue
        if rel.startswith("_"):                    # _global.json 等全局文件
            if isinstance(body, dict):
                global_cfg.update({k: body[k] for k in
                                   ("default_scene", "hold_s", "switch_delay_s") if k in body})
            continue
        cat = _load_category(body, rel)
        if cat:
            cats[cat["name"]] = cat
        else:
            err("scenes: %s 缺少有效scenes定义(跳过)" % rel)
    if not cats:                                    # 目录全坏 -> 回退内置默认
        log("scenes: 目录无有效品类, 回退内置默认")
        for fname, body in _DEFAULT_SCENE_FILES.items():
            if not fname.startswith("_"):
                cat = _load_category(body, fname)
                if cat:
                    cats[cat["name"]] = cat
    ordered = sorted(cats.values(), key=lambda c: (-c["priority"], c["name"]))
    table = [{"cmd": "默认", "name": str(global_cfg.get("default_scene") or "默认")}]
    for c in ordered:
        table.extend(c["scenes"])
    global SCENE_TABLE, SCENE_BY_CMD, SCENE_CATEGORIES, SCENE_GLOBAL
    SCENE_TABLE = table
    SCENE_BY_CMD = {s["cmd"]: s for s in table}
    SCENE_CATEGORIES = {c["name"]: c for c in ordered}
    SCENE_GLOBAL = global_cfg
    # 全局参数同步进 CONFIG(运行时统一从CONFIG读), scenes/_global.json为这两个键的权威源
    CONFIG["scene_hold_s"] = int(global_cfg.get("hold_s", 180))
    CONFIG["scene_switch_delay_s"] = int(global_cfg.get("switch_delay_s", 3))

_SCENES_MTIMES = {}    # {relpath: mtime} 上次扫描快照

def scenes_init():
    """启动时: 播种默认文件 -> 建立mtime基线 -> 全量加载"""
    _scene_seed_files()
    _SCENES_MTIMES.update(scenes_scan())
    scenes_load_all()
    log("scenes: %d品类 %d条切换指令 默认场景=[%s]"
        % (len(SCENE_CATEGORIES), len(SCENE_TABLE) - 1,
           SCENE_GLOBAL.get("default_scene", "默认")))

def scenes_sync_global_from_config():
    """/api/config 保存后把延迟/保持时长回写 _global.json, 保持单一权威源"""
    p = os.path.join(SCENES_DIR, "_global.json")
    try:
        body = {}
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                body = json.load(f)
        if not isinstance(body, dict):
            body = {}
        body["default_scene"] = SCENE_GLOBAL.get("default_scene", "默认")
        body["hold_s"] = int(CONFIG.get("scene_hold_s", 180))
        body["switch_delay_s"] = int(CONFIG.get("scene_switch_delay_s", 3))
        SCENE_GLOBAL.update({"default_scene": body["default_scene"],
                             "hold_s": body["hold_s"],
                             "switch_delay_s": body["switch_delay_s"]})
        with open(p, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
    except Exception as e:
        err("scenes: 回写_global.json失败: %s" % e)

def scenes_reload_if_changed():
    """mtime轮询(每秒, 主循环里调): 目录/文件有增删改 -> 热重载"""
    now = scenes_scan()
    if now != _SCENES_MTIMES:
        added = sorted(set(now) - set(_SCENES_MTIMES))
        removed = sorted(set(_SCENES_MTIMES) - set(now))
        changed = sorted(k for k in set(now) & set(_SCENES_MTIMES)
                         if now[k] != _SCENES_MTIMES[k])
        _SCENES_MTIMES.clear()
        _SCENES_MTIMES.update(now)
        try:
            scenes_load_all()
            log("scenes 热重载: 新增=%s 修改=%s 删除=%s -> %d品类/%d条切换指令"
                % (added, changed, removed, len(SCENE_CATEGORIES), len(SCENE_TABLE) - 1))
        except Exception as e:
            err("scenes 热重载失败: %s" % e)

def scenes_export():
    """给WebUI: 品类列表+全局参数"""
    return {"global": dict(SCENE_GLOBAL), "categories": [
        {k: (list(v) if isinstance(v, tuple) else dict(v) if isinstance(v, dict) else v)
         for k, v in c.items()} for c in
        sorted(SCENE_CATEGORIES.values(), key=lambda c: (-c["priority"], c["name"]))]}

SCENE_TABLE = []
SCENE_BY_CMD = {}
SCENE_CATEGORIES = {}
SCENE_GLOBAL = {"default_scene": "默认", "hold_s": 180, "switch_delay_s": 3}
CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}

SCENE_STATE = {
    "pending": None,        # {"at": ts, "cmd": str}
    "hold_until": None,     # 锁定期截止 ts
    "switch_count": 0,
}

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k in DEFAULT_CONFIG:
            if k in d:
                CONFIG[k] = d[k]
        log("config loaded: scene_enabled=%s delay=%ss hold=%ss" %
            (CONFIG.get("scene_enabled"), CONFIG.get("scene_switch_delay_s"), CONFIG.get("scene_hold_s")))
    except FileNotFoundError:
        log("no config.json, using defaults")
    except Exception as e:
        err("load config: %s" % e)

def save_config():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        err("save config: %s" % e)
        return False

STATE = {
    "enabled": True,
    "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "watched": "",
    "offset": 0,
    "seen": 0,
    "last_danmu": None,
    "obs_ok": False,
    "duck_active": False,
    "duck_muted": 0,
    "duck_repairs": 0,
    "current_scene": "默认",
    "last_scene_detect": "",
    "scene_switches": 0,
    "errors": deque(maxlen=5),
}
DANMU_LOG = deque(maxlen=100)      # 弹幕历史 [{time,nick,text,cat}]
DANMU_STATS = {"chat": 0, "enter": 0, "gift": 0}
CAT_RULES = [
    ("gift", ("礼物", "送出", "点亮", "小心心", "棒棒糖", "小星星", "荧光棒", "粉丝团灯牌")),
    ("enter", ("进入直播间", "来了", "欢迎", "大家好")),
]
def danmu_cat(nick, text):
    t = text or ""
    for cat, kws in CAT_RULES:
        for kw in kws:
            if kw in t:
                return cat
    if nick in ("系统", "系统消息"):
        return "enter"
    return "chat"

LOCK = threading.Lock()

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\[用户发言\](.*)$")
IGNORE_TEXTS = {"6", "66", "666", "6666", "哈哈", "哈哈哈", "来了", "签到"}
# 控制字符/零宽字符: 真实弹幕里混入会导致下游JSON解析422
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u2028\u2029\u2060\ufeff]")
# ---- 表情弹幕过滤: 剥离纯表情类弹幕, 只留有实际内容的发言 ----
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF"
    "\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF\uFE0F\u2b50\u3030\u303d\u3297\u3299\u2705\u274c]"
)
TAOBAO_FACE_RE = re.compile(r"\[-?[^][]*\]")      # 淘宝方括号表情码: [666] [-哈哈哈] [-鼓掌]
GIFT_COUNTER_RE = re.compile(r"^[\s\U0001F000-\U0001FAFF]*(?:减|点|送|赞|亮了|收藏|分享)\s*[0-9０-９]*[\s.。!！]*$")  # 礼物互动计数噪音

def effective_text(s):
    """剥离表情后剩余的有效文本(用于判断是否纯表情弹幕)"""
    s = TAOBAO_FACE_RE.sub("", s)
    s = EMOJI_RE.sub("", s)
    return CTRL_RE.sub("", s).strip()

def clean_text(s):
    return CTRL_RE.sub("", s).strip()

# 常驻日志句柄+大小轮转(单代), 性能与原版一致
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "scene_switcher.log")
_LOG_MAX = 5 * 1024 * 1024
_log_fh = None
_log_lock = threading.Lock()

def _log_write(line):
    global _log_fh
    with _log_lock:
        try:
            if _log_fh is None or _log_fh.tell() > _LOG_MAX:
                if _log_fh:
                    _log_fh.close()
                    _log_fh = None
                if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > _LOG_MAX:
                    os.replace(_LOG_PATH, _LOG_PATH + ".1")   # 单代轮转
                os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
                _log_fh = open(_LOG_PATH, "a", encoding="utf-8")
            _log_fh.write(line)
            _log_fh.flush()
        except Exception:
            try:
                if _log_fh: _log_fh.close()
            except Exception:
                pass
            _log_fh = None

def log(msg):
    line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    _log_write(line)
    try:
        print(line, end="", flush=True)
    except UnicodeEncodeError:                  # Windows GBK控制台遇emoji: 降级输出防线程中断
        try:
            print(line.encode("gbk", "replace").decode("gbk"), end="", flush=True)
        except Exception:
            pass

def err(msg):
    log("ERROR: " + msg)
    with LOCK:
        STATE["errors"].append("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg[:180]))

# ---------------- 弹幕文件监控 ----------------
OFFSETS = {}    # path -> 已读偏移
REMAIN = {}     # path -> 不完整行残留bytes

def danmu_log_dir():
    d = str(CONFIG.get("danmu_log_dir") or "").strip()
    return d if d else LOG_DIR

def find_newest_record():
    """返回最新的 用户发言记录*.txt 路径"""
    best, best_m = None, -1
    try:
        for name in os.listdir(danmu_log_dir()):
            if name.startswith("用户发言记录") and name.endswith(".txt"):
                p = os.path.join(danmu_log_dir(), name)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if m > best_m:
                    best, best_m = p, m
    except FileNotFoundError:
        pass
    return best

def parse_line(raw):
    """解析一行弹幕 -> (time_str, nick, text) 或 None"""
    try:
        s = raw.decode("utf-8", errors="replace").strip("\r\n")
    except Exception:
        return None
    m = LINE_RE.match(s)
    if not m:
        return None
    t, content = m.group(1), m.group(2).strip()
    content = clean_text(content.lstrip("「").rstrip("」"))
    if "：" in content:
        nick, text = content.split("：", 1)
    elif ":" in content:
        nick, text = content.split(":", 1)
    else:
        nick, text = "观众", content
    return t, clean_text(nick), clean_text(text)

def handle_danmu(t, nick, text):
    """独立版: 弹幕只做两件事 — 切场景检测 + 历史记录(无LLM/TTS链路)"""
    nick = clean_text(nick)
    text = clean_text(text)
    if not text:
        return
    scene_tag = scene_on_danmu(text) if CONFIG.get("scene_enabled", True) else None
    _cat = danmu_cat(nick, text)
    with LOCK:
        STATE["seen"] += 1
        STATE["last_danmu"] = {"time": t, "nick": nick, "text": text}
        DANMU_LOG.appendleft({"time": t, "nick": nick, "text": text, "cat": _cat})
        if _cat in DANMU_STATS:
            DANMU_STATS[_cat] += 1

def scan_once(mypath=None):
    """增量读取文件(默认最新用户发言记录, 可指定如淘宝桥接文件)，新弹幕进handle_danmu"""
    path = mypath or find_newest_record()
    if not path:
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    first_seen = path not in OFFSETS
    if first_seen:
        OFFSETS[path] = size            # 首次见到跳过历史
        REMAIN[path] = b""
        with LOCK:
            STATE["watched"] = os.path.basename(path)
            STATE["offset"] = size
        return
    off = OFFSETS[path]
    if size == off:
        return
    if size < off:                      # 文件被轮转/重写
        OFFSETS[path] = size
        REMAIN[path] = b""
        return
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except OSError as e:
        err("read %s: %s" % (path, e))
        return
    OFFSETS[path] = off + len(data)
    buf = REMAIN.pop(path, b"") + data
    lines = buf.split(b"\n")
    if buf.endswith(b"\n"):
        rem = b""
    else:
        rem = lines.pop()               # 最后一段可能不完整
    REMAIN[path] = rem
    for ln in lines:
        parsed = parse_line(ln)
        if parsed:
            handle_danmu(*parsed)

# ---------------- OBS websocket (纯标准库最小实现, obs-websocket 5.x) ----------------
class _MiniWS:
    """极简 websocket 客户端: 连接/发文本/收文本, 够用即可"""
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=8)
        self.sock.settimeout(15)
        self._buf = b""

    def handshake(self, host, port):
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET / HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (host, port, key))
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("ws握手失败")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("ws握手被拒: %s" % head[:80])
        self._buf = rest

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self):
        while True:
            h = self._recv_exact(2)
            fin_op = h[0]
            ln = h[1] & 0x7F
            masked = h[1] & 0x80
            if ln == 126:
                ln = int.from_bytes(self._recv_exact(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._recv_exact(8), "big")
            mask = self._recv_exact(4) if masked else None
            data = bytearray(self._recv_exact(ln))
            if mask:
                for i in range(len(data)):
                    data[i] ^= mask[i % 4]
            if (fin_op & 0x0F) == 1:      # 文本帧(不考虑分片)
                return bytes(data).decode("utf-8", errors="replace")
            # ping/其他帧忽略继续收

    def send_text(self, s):
        payload = s.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        header += mask
        enc = bytearray(payload)
        for i in range(n):
            enc[i] ^= mask[i % 4]
        self.sock.sendall(bytes(header) + bytes(enc))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

_OBS = {"ws": None, "lock": threading.Lock(), "rid": 0}

def _obs_connect():
    ws = _MiniWS(OBS_HOST, OBS_PORT)
    ws.handshake(OBS_HOST, OBS_PORT)
    hello = json.loads(ws.recv_text())
    ident = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
    auth = (hello.get("d") or {}).get("authentication")
    if auth:
        # obs-websocket 标准鉴权: secret=b64(sha256(password+salt)); auth=b64(sha256(secret+challenge))
        sec = base64.b64encode(hashlib.sha256(
            (OBS_PASSWORD + auth["salt"]).encode()).digest()).decode()
        authentication = base64.b64encode(hashlib.sha256(
            (sec + auth["challenge"]).encode()).digest()).decode()
        ident["d"]["authentication"] = authentication
    ws.send_text(json.dumps(ident))
    while True:
        r = json.loads(ws.recv_text())
        if r.get("op") == 2:
            log("obs-ws connected %s:%s" % (OBS_HOST, OBS_PORT))
            return ws
        if r.get("op") == 3:
            raise RuntimeError("obs鉴权失败")

def _obs_call(req_type, data=None):
    """带重连的 OBS 请求; OBS不在时抛异常由调用方兜底"""
    with _OBS["lock"]:
        last = None
        for attempt in range(2):
            try:
                if not _OBS["ws"]:
                    _OBS["ws"] = _obs_connect()
                _OBS["rid"] += 1
                rid = str(_OBS["rid"])
                _OBS["ws"].send_text(json.dumps({
                    "op": 6,
                    "d": {"requestType": req_type, "requestId": rid, "requestData": data or {}}}))
                while True:
                    r = json.loads(_OBS["ws"].recv_text())
                    if r.get("op") == 7 and r["d"].get("requestId") == rid:
                        st = r["d"].get("requestStatus", {})
                        if not st.get("result", st.get("code") == 100):
                            raise RuntimeError("%s: %s" % (req_type, st.get("comment") or st.get("code")))
                        with LOCK:
                            STATE["obs_ok"] = True
                        return r["d"].get("responseData", {})
            except Exception as e:
                last = e
                with LOCK:
                    STATE["obs_ok"] = False
                try:
                    if _OBS["ws"]:
                        _OBS["ws"].close()
                except Exception:
                    pass
                _OBS["ws"] = None
                if attempt:
                    break
                time.sleep(0.5)
        raise last

def _obs_video_inputs():
    """OBS全部视频类输入(不限当前场景)。返回(当前场景名, 视频输入名列表)"""
    scene = _obs_call("GetCurrentProgramScene").get("sceneName")
    inputs = (_obs_call("GetInputList") or {}).get("inputs", [])
    return scene, [i.get("inputName") for i in inputs
                   if i.get("inputKind") in DUCK_KINDS and i.get("inputName")]

def _ledger_load():
    try:
        with open(DUCK_LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _ledger_save(snap):
    try:
        with open(DUCK_LEDGER_PATH, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
    except Exception as e:
        err("duck ledger save: %s" % e)

def _all_duck_inputs_state():
    """全量视频源当前 (muted, vol_mul) 快照。OBS不在返回None"""
    try:
        inputs = (_obs_call("GetInputList") or {}).get("inputs", [])
        st = {}
        for i in inputs:
            if i.get("inputKind") not in DUCK_KINDS or not i.get("inputName"):
                continue
            n = i.get("inputName")
            m = bool((_obs_call("GetInputMute", {"inputName": n}) or {}).get("inputMuted"))
            v = float((_obs_call("GetInputVolume", {"inputName": n}) or {}).get("inputVolumeMul", 1.0))
            st[n] = {"muted": m, "vol": v}
        return st
    except Exception:
        return None

def duck_repair(only_current_scene=False):
    """救回被泄漏卡死的视频源: 按【账本】精确还原上次快照的静音/音量,
    只处理与账本不符的源——不无脑强推 mute=False/vol=1.0, 尊重用户手动静音/调音。
    无账本时(首次运行)不动任何源。返回修复名单。"""
    with LOCK:
        if STATE.get("duck_active"):
            return [], "ducking进行中, 跳过"
    try:
        scene = _obs_call("GetCurrentProgramScene").get("sceneName")
    except Exception as e:
        return [], "OBS不可达: %s" % e
    st = _all_duck_inputs_state()
    if st is None:
        return [], "OBS不可达"
    if only_current_scene:
        try:
            items = (_obs_call("GetSceneItemList", {"sceneName": scene}) or {}).get("sceneItems", [])
            keep = set(it.get("sourceName") for it in items)
            st = {k: v for k, v in st.items() if k in keep}
        except Exception:
            pass
    led = _ledger_load().get("state") or {}
    fixed = []
    for name, s in st.items():
        was = led.get(name)
        if not was:
            continue    # 账本没记过的源 = 非本系统接管, 手动状态一律不碰
        if s["muted"] != was["muted"] or abs(s["vol"] - was["vol"]) > 0.001:
            try:
                _obs_call("SetInputMute", {"inputName": name, "inputMuted": bool(was["muted"])})
                _obs_call("SetInputVolume", {"inputName": name, "inputVolumeMul": round(float(was["vol"]), 4)})
                fixed.append(name)
                log("duck repair: %s 还原至账本原状(mute=%s vol=%.3f)" % (name, was["muted"], was["vol"]))
            except Exception as e:
                err("duck repair %s: %s" % (name, e))
    if fixed:
        with LOCK:
            STATE["duck_repairs"] = STATE.get("duck_repairs", 0) + len(fixed)
    return fixed, scene

DUCK_STARTUP_WAIT = 20          # 启动自愈最多等OBS就绪秒数
def duck_startup_repair():
    """启动自愈: 若上次进程死在duck窗口(账本存在且记录了静音态), 按账本恢复。"""
    t0 = time.time()
    now_st = None
    while time.time() - t0 < DUCK_STARTUP_WAIT:
        now_st = _all_duck_inputs_state()
        if now_st is not None:
            break
        time.sleep(1)
    if now_st is None:
        err("startup repair: OBS %ds未就绪, 放弃本次" % DUCK_STARTUP_WAIT)
        return
    led = _ledger_load()
    st = led.get("state") or {}
    fixed = []
    for name, was in st.items():
        cur = now_st.get(name)
        if not cur:
            continue
        if was["muted"] != cur["muted"] or abs(was["vol"] - cur["vol"]) > 0.001:
            try:
                _obs_call("SetInputMute", {"inputName": name, "inputMuted": bool(was["muted"])})
                _obs_call("SetInputVolume", {"inputName": name, "inputVolumeMul": round(float(was["vol"]), 4)})
                fixed.append(name)
            except Exception as e:
                err("startup repair %s: %s" % (name, e))
    if fixed:
        log("duck startup-repair: %d 源按账本恢复 %s" % (len(fixed), fixed))
    try:
        f2, sc = duck_repair(only_current_scene=False)
        if f2:
            log("duck startup-repair(指纹): %d 源解锁 %s" % (len(f2), f2))
    except Exception as e:
        err("startup repair fingerprint: %s" % e)
    st2 = _all_duck_inputs_state()
    if st2 is not None:
        _ledger_save({"ts": time.time(), "state": st2})
    log("startup repair done: 账本=%d源 账本修复=%d 指纹修复=%d" % (len(st or {}), len(fixed), len(f2)))

# ---------------- 场景切换引擎(替代lua插件) ----------------
def _cn_number_runs(text):
    """提取中文数字串并求值: '十六'->16, '十二'->12。返回值列表(>1)"""
    vals = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in CN_DIGITS or ch == "十":
            j = i
            while j < n and (text[j] in CN_DIGITS or text[j] == "十"):
                j += 1
            run = text[i:j]
            v = None
            if run == "十":
                v = 10
            elif "十" in run:
                parts = run.split("十")
                left = CN_DIGITS.get(parts[0], 1) if parts[0] else 1
                right = CN_DIGITS.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
                v = left * 10 + right
            else:
                v = CN_DIGITS.get(run)
            if v and v > 1:
                vals.append(v)
            i = j
        else:
            i += 1
    return vals

def _snap_level(v, levels):
    """就近归档: 落到不大于v的最大档位; 低于最小档取最小档"""
    for lv in reversed(levels):
        if v >= lv:
            return lv
    return levels[0]

def _cat_match_score(content, cat):
    """品类命中强度: 品名/别名=3 > 泡泡系词=2；未提及=0"""
    aliases = cat.get("aliases") or {}
    if cat["name"] in content or any(a in content for a in aliases):
        return 3
    if any(w in content for w in cat["bubble_words"]):
        return 2
    return 0

def scene_extract_detail(content):
    """意图识别(三级触发词版):
    1) 二级触发词(品类词/品名/别名) 提及 -> 命中该品类(多品类按priority取最高), 不要求一级触发词
    2) 未提品类但含一级触发词 -> 落到缺省品类「白水」(无二级触发词者)
    数字就近归档到该品类三级触发词(档位); 无数字用缺省档(10米); 无品类可命中 -> None
    返回 dict(cmd/category/tier1/tier2/tier2_default/tier3/tier3_default/level) 或 None"""
    if not content:
        return None
    with LOCK:
        cats = list(SCENE_CATEGORIES.values())
    if not cats:
        return None
    by_pri = sorted(cats, key=lambda c: (-c["priority"], c["name"]))
    # 1) 二级触发词/品名/别名 指名命中
    cat = next((c for c in by_pri if _cat_match_score(content, c) > 0), None)
    hit_t2 = None
    if cat is not None:
        hit_t2 = next((w for w in cat["bubble_words"] if w in content), None) \
            or next((a for a in (cat.get("aliases") or {}) if a in content), None) \
            or (cat["name"] if cat["name"] in content else None)
    # 2) 仅一级触发词 -> 缺省品类「白水」(其次无二级触发词者, 再其次priority最高)
    if cat is None:
        hit_t1 = next((w for c in by_pri for w in c["trigger_words"] if w in content), None)
        if not hit_t1:
            return None
        base = [c for c in by_pri if c["name"] == DEFAULT_TIER2] \
            or [c for c in by_pri if not c["bubble_words"]] or by_pri
        cat = base[0]
    else:
        hit_t1 = next((w for w in cat["trigger_words"] if w in content), None)
    levels = cat["levels"]
    if not levels:
        levels = [DEFAULT_TIER3]
    def _fmt(level):
        return next((s["cmd"] for s in cat["scenes"]
                     if re.fullmatch(r"%d(泡|紫|黄|毫米|mm)?" % level, s["cmd"])), None) \
            or next((s["cmd"] for s in cat["scenes"]), str(level))

    def _ret(level, defaulted=False, t3=None):
        return {"cmd": _fmt(level), "category": cat["name"],
                "tier1": hit_t1, "tier2": hit_t2 or DEFAULT_TIER2,
                "tier2_default": (hit_t2 is None),
                "tier3": t3 or ("%d米" % level), "tier3_default": defaulted,
                "level": level}
    # 阿拉伯数字优先
    m = re.search(r"\d+", content)
    if m:
        return _ret(_snap_level(int(m.group()), levels))
    # 中文数字: 优先恰好等于档位的串
    runs = _cn_number_runs(content)
    for v in runs:
        if v in levels:
            return _ret(v)
    if runs:
        return _ret(_snap_level(runs[0], levels))
    # 无数字: 别名自带档位(如"珍珠":14)优先, 其次品类默认档
    for a, lv in sorted((cat.get("aliases") or {}).items(), key=lambda kv: -len(kv[0])):
        if a in content and isinstance(lv, int) and lv > 0:
            return _ret(_snap_level(lv, levels), t3="%d米(别名额)" % _snap_level(lv, levels))
    dl = cat.get("default_level") or levels[0]
    return _ret(dl, defaulted=True)


def scene_extract_cmd(content):
    """弹幕 → 切换指令(热路径, 只取cmd)"""
    d = scene_extract_detail(content)
    return d["cmd"] if d else None

def scene_on_danmu(content):
    """每条弹幕都进这里: 命中指令则安排延迟切换。返回tag描述"""
    cmd = scene_extract_cmd(content)
    if cmd is None:
        with LOCK:
            STATE["last_scene_detect"] = "- " + content[:40]
        return "静默"
    now = time.time()
    hold_until = SCENE_STATE["hold_until"]
    if CONFIG.get("scene_hold_s", 180) > 0 and hold_until and now < hold_until:
        with LOCK:
            STATE["last_scene_detect"] = "⏳ 锁定中剩余%ds，忽略 %s" % (int(hold_until - now), cmd)
        return "锁定·忽略 → " + cmd
    delay = max(0, int(CONFIG.get("scene_switch_delay_s", 3)))
    SCENE_STATE["pending"] = {"at": now + delay, "cmd": cmd}
    with LOCK:
        STATE["last_scene_detect"] = "▶ 检测 " + content[:30] + " -> " + cmd
    if delay > 0:
        log("scene: 检测到 [%s] -> %s，%d秒后切换" % (content[:40], cmd, delay))
    return "→ " + cmd

def scene_tick():
    """主循环调用: 执行到期的延迟切换 + 锁定期满回默认"""
    now = time.time()
    pend = SCENE_STATE["pending"]
    if pend and now >= pend["at"]:
        SCENE_STATE["pending"] = None
        cmd = pend["cmd"]
        entry = SCENE_BY_CMD.get(cmd)
        if not entry:
            with LOCK:
                STATE["last_scene_detect"] = "✗ %s -> 无对应场景" % cmd
            return
        try:
            _obs_call("SetCurrentProgramScene", {"sceneName": entry["name"]})
            SCENE_STATE["switch_count"] += 1
            hold = int(CONFIG.get("scene_hold_s", 180))
            SCENE_STATE["hold_until"] = time.time() + hold if hold > 0 else None
            with LOCK:
                STATE["current_scene"] = entry["name"]
                STATE["last_scene_detect"] = "✓ %s -> 场景[%s]" % (cmd, entry["name"])
            log("scene switched: cmd=%s scene=[%s] hold=%ds (累计%d次)"
                % (cmd, entry["name"], hold, SCENE_STATE["switch_count"]))
            # scene switched guard: 新场景就位后立即清哑源, 不等下一次播报
            try:
                fg, _ = duck_repair(only_current_scene=True)
                if fg:
                    log("scene-guard [%s]: 切景后修复 %s" % (entry["name"], fg))
            except Exception as e:
                err("scene switched guard: %s" % e)
        except Exception as e:
            err("scene switch %s: %s" % (entry["name"], e))
            with LOCK:
                STATE["last_scene_detect"] = "✗ 切换失败: %s" % e
    # 锁定期满回默认
    hu = SCENE_STATE["hold_until"]
    if hu and now >= hu:
        SCENE_STATE["hold_until"] = None
        default_scene = SCENE_GLOBAL.get("default_scene", "默认")
        try:
            _obs_call("SetCurrentProgramScene", {"sceneName": default_scene})
            with LOCK:
                STATE["current_scene"] = default_scene
            log("scene: 锁定期结束, 回默认场景[%s]" % default_scene)
            try:
                fg, _ = duck_repair(only_current_scene=True)
                if fg:
                    log("scene-guard [默认]: 回默认后修复 %s" % fg)
            except Exception as e:
                err("scene back guard: %s" % e)
        except Exception as e:
            err("scene back-to-default: %s" % e)

def scene_manual(cmd):
    """手动切换(WebUI/面板按钮用)。返回是否成功入队"""
    cmd = str(cmd).strip()
    if cmd not in SCENE_BY_CMD:
        return False
    delay = max(0, int(CONFIG.get("scene_switch_delay_s", 3)))
    SCENE_STATE["pending"] = {"at": time.time() + min(delay, 1), "cmd": cmd}
    return True

# ---------------- 主循环: 弹幕tail + 切景tick + scenes热重载 ----------------
def monitor_loop():
    log("monitor watching %s" % danmu_log_dir())
    while True:
        try:
            scan_once()                                   # FlyAiLive(抖音等)最新记录
            tbp = os.path.join(danmu_log_dir(), "用户发言记录_tb.txt")
            if os.path.exists(tbp):
                scan_once(tbp)                            # 淘宝弹幕桥接文件
        except Exception as e:
            err("scan: %s" % e)
        try:
            if CONFIG.get("scene_enabled", True):
                scene_tick()
                with LOCK:
                    STATE["scene_switches"] = SCENE_STATE["switch_count"]
        except Exception as e:
            err("scene tick: %s" % e)
        try:
            scenes_reload_if_changed()      # scenes/目录热重载(含WebUI/手动改文件)
        except Exception as e:
            err("scenes reload: %s" % e)
        time.sleep(POLL_INTERVAL)

SCENES_PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>全场景控制器 · 场景切换配置</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,"Microsoft YaHei";background:#0f172a;color:#e2e8f0;padding:24px}
.wrap{max-width:980px;margin:0 auto}h1{font-size:21px;margin:0 0 4px}.sub{color:#64748b;font-size:13px;margin-bottom:18px}
.card{background:#1e293b;border-radius:14px;padding:18px;margin-bottom:14px}
.card h2{margin:0 0 12px;font-size:15px;color:#38bdf8}
.row{display:flex;justify-content:space-between;align-items:center;margin:10px 0;gap:14px}
.lab{font-size:14px}.hint{font-size:12px;color:#64748b;margin-top:3px}
input[type=text],input[type=number],select{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:7px;font-size:13px}
textarea{background:#0f172a;color:#e2e8f0;border:1px solid #334155;border-radius:8px;padding:10px;font-size:13px;font-family:ui-monospace,Consolas,monospace;resize:vertical;width:100%}
button{border:none;border-radius:9px;padding:9px 20px;font-size:14px;font-weight:600;cursor:pointer;background:#38bdf8;color:#04283d}
button.ghost{background:#334155;color:#cbd5e1}
button.danger{background:#7f1d1d;color:#fecaca}
button:disabled{opacity:.5;cursor:not-allowed}
#tip{font-size:13px;color:#34d399;min-height:20px;margin-top:10px;white-space:pre-wrap}
.back{font-size:13px;color:#64748b;text-decoration:none}
.cattab{display:flex;align-items:center;gap:8px;background:#0f172a;border:1px solid #334155;border-radius:9px;padding:8px 14px;font-size:13px;cursor:pointer}
.cattab.on{border-color:#38bdf8;color:#38bdf8;font-weight:600}
.cats{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:4px}
.badge{font-size:11px;padding:2px 8px;border-radius:99px;background:#334155;color:#94a3b8}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
td,th{padding:6px 8px;border-bottom:1px solid #334155;text-align:left}
th{color:#64748b;font-weight:500}
.switch{position:relative;width:46px;height:26px;display:inline-block}.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;cursor:pointer;inset:0;background:#334155;border-radius:99px;transition:.2s}
.slider:before{content:"";position:absolute;height:20px;width:20px;left:3px;top:3px;background:#94a3b8;border-radius:50%;transition:.2s}
input:checked+.slider{background:#059669}input:checked+.slider:before{transform:translateX(20px);background:#fff}
input[type=range]{flex:1;accent-color:#38bdf8}
.val{width:74px;text-align:right;font-weight:600;color:#38bdf8;font-size:15px}
.tag{display:inline-flex;align-items:center;gap:4px;background:#334155;color:#e2e8f0;border-radius:99px;padding:3px 10px;margin:2px 4px 2px 0;font-size:13px;cursor:pointer}
.tag a{color:#94a3b8;text-decoration:none;font-weight:700}
.tag a:hover{color:#f87171}
.tag.add{background:#0f172a;border:1px dashed #475569;color:#38bdf8}
</style></head><body><div class="wrap">
<h1>🎬 场景切换配置 <a class="back" href="/">状态页</a></h1>
<div class="sub">品类规则保存在 scenes/ 目录（.json），保存即热生效，无需重启；手动改文件同样自动生效<br>
<b>三级触发词体系</b>：一级触发词 = 品类入口词 → 二级触发词 = 品类词（<b>缺省 白水</b>） → 三级触发词 = 档位（<b>缺省 10米</b>）</div>

<div class="card"><h2>📦 品类</h2>
<div class="cats" id="cats"></div>
<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
<input type="text" id="new_name" placeholder="新品类名, 如: 碧玺" style="width:180px">
<button class="ghost" onclick="newCat()">➕ 新建品类</button>
<button class="ghost" onclick="loadAll()">🔄 刷新</button>
<span style="flex:1"></span>
<button class="danger" onclick="delCat()">🗑 删除当前品类</button>
</div>
<div class="hint" style="margin-top:8px">提示：scenes/ 目录支持子文件夹归档（如 <code>饰品/碧玺.json</code>），程序递归扫描全部生效。obs_scene 必须和 OBS 里的场景名完全一致。</div>
</div>

<div class="card"><h2>⚙️ 全局参数（_global.json）<span id="obs_stat" class="badge"></span></h2>
<div class="row"><div><div class="lab">默认场景名</div><div class="hint">锁定期结束自动切回的场景</div></div>
<select id="g_default" style="width:200px"></select></div>
<div class="row"><div><div class="lab">场景保持时长</div><div class="hint">切换后保持秒数, 0=不回默认</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:300px">
<input type="range" id="g_hold" min="0" max="600" step="30" oninput="document.getElementById('v_hold').textContent=this.value+' s'"><span class="val" id="v_hold"></span></div></div>
<div class="row"><div><div class="lab">切换延迟</div><div class="hint">检测到指令后等几秒再切（窗口内新指令覆盖旧指令）</div></div>
<div style="display:flex;align-items:center;gap:10px;flex:1;max-width:300px">
<input type="range" id="g_delay" min="0" max="15" step="1" oninput="document.getElementById('v_delay').textContent=this.value+' s'"><span class="val" id="v_delay"></span></div></div>
<div class="row"><span></span><button onclick="saveGlobal()">💾 保存全局参数</button></div>
</div>

<div class="card"><h2>🧩 品类规则 <span class="badge" id="cur_file"></span></h2>
<div class="row"><div class="lab">一级触发词</div><div style="flex:1"><div id="w_trigger" class="tags"></div>
<div class="hint">弹幕含一级触发词且未指名品类 → 落到缺省品类（二级触发词缺省 = <b>白水</b>）</div></div></div>
<div class="row"><div class="lab">二级触发词</div><div style="flex:1"><div id="w_bubble" class="tags"></div>
<div class="hint">弹幕含二级触发词 → 命中本品类（如“泡泡珠”的 泡泡/泡子/珠珠）；<b>留空 = 缺省「白水」</b></div></div></div>
<div class="row"><div class="lab">品类别名</div><div style="flex:1"><div id="w_alias" class="tags"></div>
<div class="hint">别名=数字时自带档位（如“珍珠”=14 → “来个珍珠”切14泡）；别名=true 仅标记品类</div></div></div>
<div class="row"><div class="lab">三级触发词<br><span class="hint">（档位/毫米）</span></div><div style="flex:1"><div id="w_levels" class="tags"></div>
<div class="hint">数字就近归档到 ≤ 档位；<b>无数字 = 缺省「10米」</b></div></div></div>
<div class="row"><div><div class="lab">缺省档位</div><div class="hint">无数字时切换的三级触发词（缺省 10米）</div></div>
<select id="c_default_level" style="width:110px"></select></div>
<div class="row"><div><div class="lab">优先级</div><div class="hint">多品类同时命中时取高者</div></div>
<input type="number" id="c_priority" style="width:110px"></div>
<div class="row" style="align-items:flex-start"><div><div class="lab">切换映射</div><div class="hint">弹幕指令 → OBS 场景</div></div>
<div style="flex:1"><div id="scene_rows"></div>
<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center">
<input type="text" id="new_cmd" placeholder="指令, 如: 18" style="width:110px">
<select id="new_obs" style="width:200px"></select>
<button class="ghost" onclick="addSceneRow()">➕ 添加档位</button>
<button class="ghost" onclick="addSceneRowAuto()">➕ 从OBS场景生成</button>
</div></div></div>
<div class="row" style="margin-top:10px">
<span class="hint">保存 → 写入 json → 自动热重载生效</span>
<span style="display:flex;gap:8px">
<button onclick="saveCat()">💾 保存并生效</button></span></div>
</div>

<div class="card"><h2>🧪 弹幕意图模拟（不真切换）</h2>
<div class="row">
<input type="text" id="test_text" placeholder="输入弹幕, 如: 主播看看16泡泡 / 这个白水来个十二的" style="flex:1">
<button class="ghost" onclick="testExtract()">解析</button></div>
<div id="test_out" class="hint" style="margin-top:6px"></div>
<div id="cmd_table"></div>
</div>

<div id="tip"></div>
</div>
<script>
let DATA={global:{},categories:[]};
let CUR=null;   // 当前编辑品类name
let OBSS=[];    // OBS实际场景名
let EDIT=null;  // 当前表单态品类对象
function tip(t,ok){const e=document.getElementById('tip');e.textContent=t;e.style.color=ok===false?'#f87171':'#34d399'}
async function jget(u){const r=await fetch(u);if(!r.ok)throw new Error((await r.text())||r.status);return r.json()}
function fillObsSelect(sel,keep){
 const opts=OBSS.length?OBSS:(keep?[keep]:[]);
 sel.innerHTML=opts.map(s=>'<option'+(s===keep?' selected':'')+'>'+esc(s)+'</option>').join('');
 if(keep&&!opts.includes(keep)){const o=document.createElement('option');o.textContent=keep;o.selected=true;sel.appendChild(o)}
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function renderTags(elId,obj,numeric){
 const el=document.getElementById(elId);el.innerHTML='';
 const items=Array.isArray(obj)?obj.map(v=>[v,null]):Object.entries(obj||{});
 items.forEach(([k,v])=>{
  const t=document.createElement('span');t.className='tag';
  t.innerHTML=esc(k)+(numeric?' = '+esc(v):'')+' <a href="javascript:void(0)">×</a>';
  t.querySelector('a').onclick=()=>{items.splice(items.findIndex(x=>x[0]===k),1);saveTagsBack(elId,items)};
  if(!numeric)t.onclick=(ev)=>{if(ev.target.tagName!=='A')editTag(elId,items,k)};
  el.appendChild(t);
 });
 const add=document.createElement('span');add.className='tag add';add.textContent='＋ 添加';
 add.onclick=()=>addTag(elId,items,numeric);
 el.appendChild(add);
}
function addTag(elId,items,numeric){
 let k;
 if(numeric){k=prompt('别名（可用 中文/英文）：');if(!k)return;
  const v=prompt('档位数字（留空=仅标记品类）：','');if(v===null)return;
  if(v.trim()==='')items.push([k,true]);else if(parseInt(v))items.push([k,parseInt(v)]);else return tip('档位需为数字',false)}
 else{k=prompt('添加词语：');if(!k)return;
  if(items.some(x=>String(x[0])===String(k)))return tip('已存在: '+k,false);
  items.push([k,null])}
 saveTagsBack(elId,items);
}
function editTag(elId,items,k){
 const i=items.findIndex(x=>x[0]===k);if(i<0)return;
 const isAlias=elId==='w_alias';
 const nv=prompt('修改：',String(items[i][0])+(isAlias?' = '+items[i][1]:''));
 if(nv===null)return;
 if(isAlias){const m=nv.split('=');const nk=m[0].trim();const v=(m[1]||'').trim();
  items[i]=[nk,v===''?true:(parseInt(v)||true)]}
 else items[i]=[nv.trim(),null];
 saveTagsBack(elId,items);
}
function saveTagsBack(elId,items){
 if(elId==='w_trigger')EDIT.trigger_words=items.map(x=>x[0]);
 else if(elId==='w_bubble')EDIT.bubble_words=items.map(x=>x[0]);
 else if(elId==='w_alias'){const o={};items.forEach(([k,v])=>o[k]=v);EDIT.aliases=o}
 renderTags(elId,elId==='w_trigger'?EDIT.trigger_words:(elId==='w_bubble'?EDIT.bubble_words:EDIT.aliases),elId==='w_alias');
}
function renderLevels(){
 const el=document.getElementById('w_levels');el.innerHTML='';
 (EDIT.levels||[]).forEach(lv=>{
  const t=document.createElement('span');t.className='tag';
  t.innerHTML=lv+'米 <a href="javascript:void(0)">×</a>';
  t.querySelector('a').onclick=()=>{EDIT.levels=EDIT.levels.filter(x=>x!==lv);renderLevels();renderDefaultLevel()};
  el.appendChild(t);
 });
 const add=document.createElement('span');add.className='tag add';add.textContent='＋ 添加';
 add.onclick=async()=>{
  const v=prompt('添加三级触发词（档位/毫米数，如 18）：');
  if(v===null)return;
  const n=parseInt(v);if(!n)return tip('档位需为数字',false);
  if(EDIT.levels.includes(n))return tip('已存在: '+n,false);
  EDIT.levels.push(n);EDIT.levels.sort((a,b)=>a-b);
  // 联动生成同档位切换映射, OBS场景下拉直接选
  const obs=prompt('OBS场景名（默认与档位同名）：',String(n));
  if(obs){EDIT.scenes.push({cmd:String(n),obs_scene:obs});renderSceneRows()}
  renderLevels();renderDefaultLevel();
 };
 el.appendChild(add);
}
function renderDefaultLevel(){
 const sel=document.getElementById('c_default_level');
 const cur=EDIT.default_level;
 sel.innerHTML=EDIT.levels.map(l=>'<option value="'+l+'"'+(l===cur?' selected':'')+'>'+l+'米</option>').join('');
 if(!EDIT.levels.includes(cur)){const o=document.createElement('option');o.value=cur;o.textContent='缺省 '+cur+'米';o.selected=true;sel.appendChild(o)}
}
function renderSceneRows(){
 const el=document.getElementById('scene_rows');el.innerHTML='';
 EDIT.scenes.forEach((s,i)=>{
  const row=document.createElement('div');row.style.cssText='display:flex;gap:6px;margin-bottom:6px;align-items:center';
  row.innerHTML='<input data-i="'+i+'" data-k="cmd" value="'+esc(s.cmd)+'" style="width:110px" title="弹幕指令">'+
   '<select data-i="'+i+'" data-k="obs_scene" style="flex:1"></select>'+
   '<button class="danger" style="padding:2px 10px">✕</button>';
  row.querySelector('input').oninput=(e)=>{EDIT.scenes[i].cmd=e.target.value};
  fillObsSelect(row.querySelector('select'),s.name||s.obs_scene);
  row.querySelector('select').onchange=(e)=>{EDIT.scenes[i].obs_scene=e.target.value};
  row.querySelector('button').onclick=()=>{EDIT.scenes.splice(i,1);renderSceneRows()};
  el.appendChild(row);
 });
 if(!EDIT.scenes.length)el.innerHTML='<span class="hint">暂无映射，用下方按钮添加</span>';
}
function addSceneRow(){
 const cmd=document.getElementById('new_cmd').value.trim();
 if(!cmd)return tip('先填指令',false);
 if(EDIT.scenes.some(s=>s.cmd===cmd))return tip('指令已存在: '+cmd,false);
 const obs=document.getElementById('new_obs').value||cmd;
 EDIT.scenes.push({cmd:cmd,obs_scene:obs});
 document.getElementById('new_cmd').value='';
 renderSceneRows();
}
function addSceneRowAuto(){
 // 从OBS场景生成: 跳过已存在的obs_scene
 const used=new Set(EDIT.scenes.map(s=>s.obs_scene||s.name));
 let added=0;
 OBSS.forEach(s=>{if(!used.has(s)){EDIT.scenes.push({cmd:s,obs_scene:s});added++}});
 if(added)renderSceneRows();
 tip(added?('已补入 '+added+' 个OBS场景'):'所有OBS场景都已有映射');
}
async function loadAll(){
 try{
  DATA=await jget('/api/scenes');
  renderCats();
  try{const o=await jget('/api/obs_scenes');OBSS=o.ok?o.scenes:[];}
  catch(e){OBSS=[]}
  const gs=document.getElementById('g_default');
  const dv=DATA.global.default_scene||'默认';
  gs.innerHTML=OBSS.map(s=>'<option'+(s===dv?' selected':'')+'>'+esc(s)+'</option>').join('');
  if(OBSS.length&&!OBSS.includes(dv)){const op=document.createElement('option');op.textContent=dv;op.selected=true;gs.appendChild(op)}
  if(!OBSS.length){const op=document.createElement('option');op.textContent=dv;op.selected=true;gs.appendChild(op)}
  document.getElementById('g_hold').value=DATA.global.hold_s??180;
  document.getElementById('v_hold').textContent=(DATA.global.hold_s??180)+' s';
  document.getElementById('g_delay').value=DATA.global.switch_delay_s??3;
  document.getElementById('v_delay').textContent=(DATA.global.switch_delay_s??3)+' s';
  if(!CUR||!DATA.categories.find(c=>c.name===CUR))CUR=DATA.categories[0]?.name||null;
  if(CUR)await openCat(CUR);else{document.getElementById('cur_file').textContent='';EDIT=null}
  renderCmdTable();
  document.getElementById('obs_stat').textContent=OBSS.length?('OBS在线·'+OBSS.length+'场景'):'⚠ OBS离线/未连';
  document.getElementById('obs_stat').style.color=OBSS.length?'#34d399':'#fbbf24';
  const no=document.getElementById('new_obs');no.innerHTML=OBSS.map(s=>'<option>'+esc(s)+'</option>').join('');
 }catch(e){tip('加载失败: '+e.message,false)}
}
function renderCats(){
 const el=document.getElementById('cats');el.innerHTML='';
 DATA.categories.forEach(c=>{
  const d=document.createElement('div');d.className='cattab'+(c.name===CUR?' on':'');
  d.innerHTML='<span>'+c.name+'</span><span class="badge">'+c.scenes.length+'景</span>';
  d.onclick=()=>openCat(c.name);el.appendChild(d);
 });
}
async function openCat(name){
 CUR=name;renderCats();
 const c=DATA.categories.find(x=>x.name===name);
 if(!c)return;
 EDIT={name:c.name,trigger_words:[...c.trigger_words],bubble_words:[...c.bubble_words],
  aliases:Object.assign({},c.aliases),levels:[...c.levels],
  priority:c.priority,default_level:c.default_level,
  scenes:c.scenes.map(s=>({cmd:s.cmd,obs_scene:s.name||s.obs_scene}))};
 document.getElementById('cur_file').textContent='scenes/'+c.file;
 renderTags('w_trigger',EDIT.trigger_words);
 renderTags('w_bubble',EDIT.bubble_words);
 renderTags('w_alias',EDIT.aliases,true);
 renderLevels();renderDefaultLevel();
 document.getElementById('c_priority').value=EDIT.priority;
 renderSceneRows();
}
async function saveGlobal(){
 const body={default_scene:document.getElementById('g_default').value.trim()||'默认',
  hold_s:+document.getElementById('g_hold').value,
  switch_delay_s:+document.getElementById('g_delay').value};
 // 借道 /api/config: CONFIG-> _global.json 同步
 const cfg=await jget('/api/config');
 const payload=Object.assign(cfg,{scene_hold_s:body.hold_s,scene_switch_delay_s:body.switch_delay_s});
 const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
 if(!r.ok)return tip('保存失败',false);
 // 默认场景名只存 _global.json, 直接POST /api/scenes global
 const r2=await fetch('/api/scenes',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({action:'save_global',global:body})});
 const j=await r2.json();
 tip(j.ok?'✅ 全局参数已生效':('❌ '+j.error),j.ok);loadAll();
}
async function saveCat(){
 if(!CUR||!EDIT)return tip('请先选择品类',false);
 if(!EDIT.scenes.length)return tip('至少保留一条切换映射',false);
 if(!EDIT.scenes.some(s=>s.cmd.trim()))return tip('切换映射的指令不能为空',false);
 EDIT.name=CUR;
 EDIT.priority=parseInt(document.getElementById('c_priority').value)||100;
 EDIT.default_level=parseInt(document.getElementById('c_default_level').value)||EDIT.levels[0]||10;
 EDIT.scenes=EDIT.scenes.filter(s=>s.cmd.trim());
 const raw=JSON.stringify(EDIT,null,2);
 const r=await fetch('/api/scenes',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({action:'save',name:CUR,raw})});
 const j=await r.json();
 if(j.ok){tip('✅ 已保存('+j.file+')并即时生效');loadAll()}
 else tip('❌ '+j.error,false);
}
async function newCat(){
 const name=document.getElementById('new_name').value.trim();
 if(!name)return tip('先输入新品类名',false);
 const base=OBSS.length?OBSS:['6','8','10'];
 const raw=JSON.stringify({name:name,trigger_words:["看","卡","试","展示"],bubble_words:[],aliases:{},
  levels:[6,8,10,12,14,16],priority:100,default_level:10,
  scenes:base.slice(0,4).map(s=>({cmd:s,obs_scene:s}))},null,2);
 const r=await fetch('/api/scenes',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({action:'save',name:name,raw,new_file:true})});
 const j=await r.json();
 if(j.ok){document.getElementById('new_name').value='';CUR=name;tip('✅ 已创建品类 '+name);loadAll()}
 else tip('❌ '+j.error,false);
}
async function delCat(){
 if(!CUR)return;
 const c=DATA.categories.find(x=>x.name===CUR);
 if(!confirm('确认删除品类「'+CUR+'」? 文件 '+c.file+' 会被删除'))return;
 const r=await fetch('/api/scenes',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({action:'delete',name:CUR})});
 const j=await r.json();
 if(j.ok){tip('✅ 已删除');CUR=null;loadAll();}else tip('❌ '+j.error,false);
}
async function testExtract(){
 const t=document.getElementById('test_text').value.trim();
 if(!t)return;
 try{
  const j=await jget('/api/scenes_test?text='+encodeURIComponent(t));
  document.getElementById('test_out').innerHTML=j.cmd?
   ('命中品类「'+(j.category||'?')+'」 → 切换指令 ['+j.cmd+']'+(j.known?'':'  (⚠ 指令不在当前映射表)')+'<br>'+
    '一级触发词: <b>'+(j.tier1||'-')+'</b> · '+
    '二级触发词: <b>'+j.tier2+'</b>'+(j.tier2_default?' <span class="hint">(缺省 白水)</span>':'')+' · '+
    '三级触发词: <b>'+j.tier3+'</b>'+(j.tier3_default?' <span class="hint">(缺省档)</span>':'')):
   '未命中（静默，不切场景）';
 }catch(e){document.getElementById('test_out').textContent='失败: '+e.message}
}
function renderCmdTable(){
 const el=document.getElementById('cmd_table');
 if(!DATA.categories.length){el.innerHTML='';return}
 let h='<table><tr><th>品类</th><th>一级触发词</th><th>二级触发词</th><th>三级触发词</th><th>切换指令→OBS场景</th></tr>';
 DATA.categories.forEach(c=>{
  h+='<tr><td><b>'+c.name+'</b><br><span class="hint">'+c.file+'</span></td>'+
     '<td>'+(c.trigger_words.join(' ')||'-')+'</td>'+
     '<td>'+(c.bubble_words.join(' ')||'<span class="hint">缺省 白水</span>')+'</td>'+
     '<td>'+c.levels.map(l=>l+'米').join('/')+' <span class="hint">缺省 '+c.default_level+'米</span></td>'+
     '<td>'+c.scenes.map(s=>s.cmd+'→'+s.name).join(' · ')+'</td></tr>';
 });
 el.innerHTML=h+'</table>';
}
loadAll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    timeout = 30                  # 挂死连接30s自动断, 防线程堆积
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b)
        self.close_connection = True

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                self._send(200, SCENES_PAGE.replace("__PORT__", str(APP_PORT)), "text/html; charset=utf-8")
            elif u.path == "/scenes":
                self._send(200, SCENES_PAGE, "text/html; charset=utf-8")
            elif u.path == "/api/scenes":
                # 品类规则(WebUI配置页数据源); name参数=单文件JSON原文(编辑器)
                name = (qs.get("name") or [""])[0].strip()
                if name:
                    cat = SCENE_CATEGORIES.get(name)
                    if not cat:
                        return self._send(404, json.dumps({"error": "品类不存在"}, ensure_ascii=False))
                    p = os.path.join(SCENES_DIR, cat["file"])
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            raw = f.read()
                        return self._send(200, json.dumps({"name": name, "file": cat["file"],
                            "raw": raw}, ensure_ascii=False))
                    except Exception as e:
                        return self._send(500, json.dumps({"error": str(e)}, ensure_ascii=False))
                with LOCK:
                    snap = json.dumps(scenes_export(), ensure_ascii=False)
                self._send(200, snap)
            elif u.path == "/api/obs_scenes":
                # OBS实际场景清单(供WebUI下拉/校验)
                try:
                    data = _obs_call("GetSceneList")
                    names = [s.get("sceneName", "") for s in (data.get("scenes") or [])]
                except Exception as e:
                    names = []
                    self._obs_err = str(e)
                self._send(200, json.dumps({"scenes": names, "ok": bool(names)}, ensure_ascii=False))
            elif u.path == "/api/scenes_test":
                # 弹幕意图模拟: 返回(一级/二级/三级触发词命中 + 品类 + cmd) 不真切换
                text = (qs.get("text") or [""])[0].strip()
                if not text:
                    return self._send(400, "missing text")
                d = scene_extract_detail(text)
                cmd = d["cmd"] if d else None
                payload = {"text": text, "cmd": cmd,
                           "category": (d or {}).get("category"),
                           "known": bool(cmd and cmd in SCENE_BY_CMD)}
                if d:
                    payload.update({k: d.get(k) for k in
                                    ("tier1", "tier2", "tier2_default", "tier3", "tier3_default", "level")})
                self._send(200, json.dumps(payload, ensure_ascii=False))
            elif u.path == "/health":
                self._send(200, "ok")
            elif u.path == "/api/status":
                with LOCK:
                    snap = {k: (list(v) if isinstance(v, deque) else v) for k, v in STATE.items()}
                snap["now"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                snap["danmu"] = list(DANMU_LOG)[:50]
                snap["stats"] = dict(DANMU_STATS)
                snap["categories"] = len(SCENE_CATEGORIES)
                snap["cmds"] = len(SCENE_TABLE) - 1
                snap["port"] = APP_PORT
                self._send(200, json.dumps(snap, ensure_ascii=False))
            elif u.path == "/scene":
                cmd = (qs.get("cmd") or [""])[0].strip()
                if not cmd:
                    return self._send(400, "missing cmd")
                if cmd == "status":
                    return self._send(200, json.dumps({
                        "current": STATE.get("current_scene"),
                        "hold_until": SCENE_STATE["hold_until"],
                        "pending": SCENE_STATE["pending"],
                        "switch_count": SCENE_STATE["switch_count"],
                    }, ensure_ascii=False))
                if scene_manual(cmd):
                    return self._send(200, "已安排切换: " + cmd)
                return self._send(400, "未知命令: " + cmd)
            elif u.path == "/api/duck-repair":
                scope = (qs.get("scope") or ["all"])[0]
                fixed, scene = duck_repair(only_current_scene=(scope == "scene"))
                self._send(200, json.dumps({"ok": True, "scene": scene,
                    "repaired": fixed, "count": len(fixed)}, ensure_ascii=False))
            else:
                self._send(404, "not found")
        except Exception as e:
            self._send(500, "error: %s" % e)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            d = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            d = {}
        if u.path == "/api/config":
            try:
                for k in DEFAULT_CONFIG:
                    if k in d:
                        CONFIG[k] = d[k]
                save_config()
                try:
                    scenes_sync_global_from_config()   # 延迟/保持时长 -> _global.json(单一权威源)
                except Exception as se:
                    err("scenes sync global: %s" % se)
                return self._send(200, json.dumps(CONFIG, ensure_ascii=False))
            except Exception as e:
                return self._send(400, "bad config: %s" % e)
        if u.path == "/api/scenes":
            action = (d.get("action") or "").strip()
            if action == "save_global":
                # 全局参数直写 _global.json(默认场景名/hold/延迟)
                g = d.get("global") or {}
                p = os.path.join(SCENES_DIR, "_global.json")
                try:
                    body2 = {}
                    if os.path.exists(p):
                        with open(p, "r", encoding="utf-8") as f:
                            body2 = json.load(f)
                    if not isinstance(body2, dict):
                        body2 = {}
                    if g.get("default_scene"):
                        body2["default_scene"] = str(g["default_scene"]).strip()
                    if "hold_s" in g:
                        body2["hold_s"] = max(0, min(int(g["hold_s"]), 3600))
                    if "switch_delay_s" in g:
                        body2["switch_delay_s"] = max(0, min(int(g["switch_delay_s"]), 60))
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(body2, f, ensure_ascii=False, indent=2)
                    scenes_reload_if_changed()
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
                return self._send(200, json.dumps({"ok": True, "msg": "全局参数已生效"}, ensure_ascii=False))
            if action == "save":
                # 保存品类: 新建(写新文件)或覆盖原文件; 热重载器自动感知
                name = str(d.get("name") or "").strip()
                raw = d.get("raw")
                if not name or name.startswith("_") or "/" in name or "\\" in name:
                    return self._send(400, json.dumps({"ok": False, "error": "名称非法"}, ensure_ascii=False))
                try:
                    body2 = json.loads(raw if isinstance(raw, str) else json.dumps(raw))
                except Exception as e:
                    return self._send(400, json.dumps({"ok": False, "error": "JSON解析失败: %s" % e}, ensure_ascii=False))
                if not isinstance(body2, dict) or not (body2.get("scenes") or []):
                    return self._send(400, json.dumps({"ok": False,
                        "error": "缺少有效 scenes 定义(至少1条 {cmd, obs_scene})"}, ensure_ascii=False))
                body2["name"] = str(body2.get("name") or name)
                old = SCENE_CATEGORIES.get(name)
                if old and old["file"] != name + ".json" and not d.get("new_file"):
                    target = old["file"]               # 覆盖原文件(含子目录相对路径)
                else:
                    target = name + ".json"
                p = os.path.join(SCENES_DIR, target)
                try:
                    os.makedirs(os.path.dirname(p) or SCENES_DIR, exist_ok=True)
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(json.dumps(body2, ensure_ascii=False, indent=2))
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": "写入失败: %s" % e}, ensure_ascii=False))
                try:
                    scenes_reload_if_changed()
                except Exception as e:
                    err("scenes reload after save: %s" % e)
                return self._send(200, json.dumps({"ok": True, "file": target,
                    "msg": "已保存并即时生效"}, ensure_ascii=False))
            if action == "rename":
                old_name = str(d.get("from") or "").strip()
                new_name = str(d.get("to") or "").strip()
                old = SCENE_CATEGORIES.get(old_name)
                if not old:
                    return self._send(404, json.dumps({"ok": False, "error": "品类不存在"}, ensure_ascii=False))
                if not new_name or new_name.startswith("_") or "/" in new_name or "\\" in new_name:
                    return self._send(400, json.dumps({"ok": False, "error": "新名称非法"}, ensure_ascii=False))
                try:
                    with open(os.path.join(SCENES_DIR, old["file"]), "r", encoding="utf-8") as f:
                        body2 = json.load(f)
                    body2["name"] = new_name
                    with open(os.path.join(SCENES_DIR, new_name + ".json"), "w", encoding="utf-8") as f:
                        f.write(json.dumps(body2, ensure_ascii=False, indent=2))
                    os.remove(os.path.join(SCENES_DIR, old["file"]))
                    scenes_reload_if_changed()
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
                return self._send(200, json.dumps({"ok": True, "msg": "已重命名并生效"}, ensure_ascii=False))
            if action == "delete":
                name = str(d.get("name") or "").strip()
                cat = SCENE_CATEGORIES.get(name)
                if not cat:
                    return self._send(404, json.dumps({"ok": False, "error": "品类不存在"}, ensure_ascii=False))
                try:
                    os.remove(os.path.join(SCENES_DIR, cat["file"]))
                    scenes_reload_if_changed()
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
                return self._send(200, json.dumps({"ok": True, "msg": "已删除并生效"}, ensure_ascii=False))
            if action == "reload":
                try:
                    scenes_reload_if_changed()
                except Exception as e:
                    return self._send(500, json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
                return self._send(200, json.dumps({"ok": True,
                    "msg": "%d品类/%d条指令" % (len(SCENE_CATEGORIES), len(SCENE_TABLE) - 1)},
                    ensure_ascii=False))
            return self._send(400, json.dumps({"ok": False, "error": "unknown action"}, ensure_ascii=False))
        self._send(404, "not found")


def main():
    load_config()
    scenes_init()
    log("=" * 50)
    log("all-scene-controller v1.0 starting on port %d" % APP_PORT)
    log("scenes_dir=%s danmu_log_dir=%s" % (SCENES_DIR, danmu_log_dir()))
    def obs_probe():
        try:
            v = _obs_call("GetVersion").get("obsVersion", "?")
            log("obs probe ok, version %s" % v)
        except Exception as e:
            err("obs probe: %s" % e)
    threading.Thread(target=duck_startup_repair, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=obs_probe, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", APP_PORT), Handler)
    srv.daemon_threads = True          # HTTP线程随主进程退出, 不堆积
    srv.serve_forever()

if __name__ == "__main__":
    main()
