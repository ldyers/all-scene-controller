#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""双平台冒烟测试: 验证模块可导入、意图解析、scenes 加载(OBS不在线也通过)"""
import importlib.util
import os
import sys

os.environ["SCENE_PORT"] = "23499"
spec = importlib.util.spec_from_file_location(
    "scene_switcher", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scene_switcher.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

m.load_config()
m.scenes_init()

# 意图基线(与 23460 实测对齐)
cases = [
    ("看一下", "10"),
    ("看看16泡泡", "14泡"),
    ("这个白水来个十二的", "12"),
    ("来个紫水的", "10紫"),
    ("黄水看下8的", "8黄"),
    ("泡泡珠来个10的", "10泡"),
    ("随便聊聊", None),
    ("主播看看14的", "14"),
]
fails = 0
for text, want in cases:
    got = m.scene_extract_cmd(text)
    ok = got == want
    print(("PASS" if ok else "FAIL"), "[%s] cmd=%s (want %s)" % (text, got, want))
    if not ok:
        fails += 1

print("categories:", len(m.SCENE_CATEGORIES), "cmds:", len(m.SCENE_TABLE) - 1)
assert len(m.SCENE_CATEGORIES) == 4, "应加载4个品类"
assert fails == 0, "%d 条意图解析失败" % fails
print("\nALL TESTS PASSED")
