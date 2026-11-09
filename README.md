# all-scene-controller v1.0

弹幕关键词 → OBS 场景切换 独立服务（从 live-brain :23460 完整剥离的切场景子系统）。

**单文件 · 零第三方依赖 · Ubuntu / Windows 双平台**（Python 3.8+ 标准库，无需 pip install）。

## 功能（与 23460 原版一致）

- **三级触发词体系**：一级触发词（品类入口词）→ 二级触发词（品类词，缺省 白水）→ 三级触发词（档位·毫米，缺省 10米，就近归档）
- **品类规则**：`scenes/*.json` 递归扫描（支持子文件夹），mtime 轮询热重载——WebUI 改/手动改文件免重启即时生效
- **WebUI `/scenes` 配置页**：品类标签页、触发词 tag 编辑、指令↔OBS场景下拉映射（数据源=OBS实况）、全局参数（默认场景/保持时长/切换延迟）、弹幕意图模拟器
- **OBS websocket 5.x**：纯标准库自研客户端（含密码鉴权），断线自愈自动重连
- **切换调度**：延迟切换（窗口内新指令覆盖旧定时）、锁定保持、到期自动回默认场景、切景后哑源修复（scene guard）
- **弹幕文件 tail**：`用户发言记录*.txt` 增量读取（抖音 FlyAiLive 格式）+ `用户发言记录_tb.txt`（淘宝桥），文件轮转自动处理
- **duck 音量账本**：视频源静音快照落盘，崩溃/泄漏后按账本精确还原（尊重 OBS 手动静音，不无脑强推）

## 快速开始

### Ubuntu / Linux

```bash
./start.sh           # 默认 23480 端口
```

### Windows

```
双击 start.bat       （需 Python 3.8+ 已在 PATH）
```

开机自启（可选）：
- Linux: `systemd` 单元或 `crontab @reboot`
- Windows: 计划任务 `schtasks /create /tn SceneSwitcher /tr "C:\path\start.bat" /sc onstart`（配 `runHidden.vbs` 隐藏窗口）

## 配置

| 方式 | 内容 |
|---|---|
| `config.json` | 端口外（环境变量），`danmu_log_dir` 弹幕文件目录（空=./logs）、`scene_enabled`、`scene_switch_delay_s`、`scene_hold_s`、`duck_*` |
| 环境变量 | `SCENE_PORT`(23480)、`OBS_WS_HOST`(127.0.0.1)、`OBS_WS_PORT`(4455)、`OBS_PASSWORD`(OBS ws 密码，无鉴权留空)、`SCENE_LOG_DIR` |
| `scenes/*.json` | 品类规则（一级/二级/三级触发词、别名、优先级、指令→场景映射），`_global.json` 全局参数 |

## HTTP 接口

| 接口 | 语义 |
|---|---|
| `GET /` 或 `/scenes` | WebUI 配置页（控件化表单） |
| `GET /api/scenes` | 品类规则全集（`?name=品类` 返回该文件 JSON 原文） |
| `POST /api/scenes` | `action=save / save_global / rename / delete / reload` |
| `GET /api/obs_scenes` | OBS 实际场景清单（WebUI 下拉数据源） |
| `GET /api/scenes_test?text=` | 意图模拟器（返回一/二/三级命中详情，不真切换） |
| `GET /scene?cmd=10` | 手动安排切换（`/scene?cmd=status` 看状态） |
| `GET /api/status` | 运行状态（obs_ok/当前场景/最近检测/弹幕历史/计数） |
| `GET /api/duck-repair?scope=scene` | 手动触发哑源修复 |
| `GET /health` | 存活探针 |

## 弹幕文件格式

行格式（日期**无方括号**）：

```
2026-09-21 10:04:01[用户发言]「梨花溅雨意笙歌a：兰花苔」
```

测试：

```bash
echo "$(date '+%Y-%m-%d %H:%M:%S')[用户发言]「测试观众：主播看看8米的」" >> logs/用户发言记录.txt
```

注意：程序首次见到文件会跳过历史（从当前文件尾开始），追加的新行才会被消费。

## 上游与差异

- 来源：[live-brain-sys](https://github.com/ldyers/live-brain-sys) `live_brain.py`（:23460，2026-11-08 剥离）
- 不含：LLM 回复、RAG、TTS 播报、话术循环、手机拉流——本服务专注"弹幕→场景"
- 保留：scenes 规则语义、意图解析、OBS 控制、duck 账本、WebUI `/scenes` 页与原版逐字节一致（仅标题与返回链接微调）

## 许可

MIT
