# 麦麦自主规划插件 v4 · v4.5.0

> MaiBot 自主规划插件：让麦麦像真实生活着的人一样，拥有自己的日程、当下在做的事，
> 回复时贴合生活节奏。本页前半部分是 v4.5.0 新版说明；**后半部分为原版文档，
> 内容逐字保留**。

---

## Fork 溯源

本分支（v4.5.0）的修改主要依据
[xuqian13/autonomous_planning_plugin](https://github.com/xuqian13/autonomous_planning_plugin)
的以下 issues：

| Issue | 内容 | 对应改动 |
|---|---|---|
| [#14](https://github.com/xuqian13/autonomous_planning_plugin/issues/14) | 请求更新 host_application.max_version 以兼容 MaiBot v1.1.0 | `max_version → 1.99.99` |
| [#13](https://github.com/xuqian13/autonomous_planning_plugin/issues/13) | MaiBot 1.1.0 兼容：max_version 需要更新 | `max_version → 1.99.99`、`min_version → 1.0.0` |
| [#12](https://github.com/xuqian13/autonomous_planning_plugin/issues/12) | 可否添加自定义日程（时间范围） | `day_start_time` / `day_end_time` + 无睡眠模式 |
| [#11](https://github.com/xuqian13/autonomous_planning_plugin/issues/11) | custom_prompt 在推断链路中无效 | custom_prompt 提升为日程演化主信号 |
| [#9](https://github.com/xuqian13/autonomous_planning_plugin/issues/9) | 模型请求失败（LLM 调用超时） | `generation_timeout` 真正生效 |

各变更的详细说明见 [v4.5.0 新特性](#v450-新特性) 与 [CHANGELOG.md](CHANGELOG.md)。

---

## 目录

- [Fork 溯源](#fork-溯源)
- [快速上手](#快速上手)
- [v4.5.0 新特性](#v450-新特性)
  - [MaiBot v1.1.0 兼容](#maibot-v110-兼容)
  - [LLM 调用超时可配置](#llm-调用超时可配置)
  - [custom_prompt = 角色的长期状态](#custom_prompt--角色的长期状态)
  - [自定义日程时间范围](#自定义日程时间范围)
  - [无睡眠模式](#无睡眠模式)
  - [/plan 命令返回合并转发](#plan-命令返回合并转发)
- [配置说明](#配置说明)
- [对外 API（其他插件调用）](#对外-api其他插件调用)
- [开发与维护](#开发与维护)
- [License](#license)
- [附录：原版文档（保留）](#附录原版文档保留)

---

## 快速上手

1. 把整个目录放到 `MaiBot/plugins/xuqian13_autonomous-planning-plugin-v4/`
2. 确认主程序 `config/model_config.toml` 里 `[model_task_config.planner]` 段已配好
   （MaiBot 默认自带，`model_list` 至少一个可用模型）
3. 启动 MaiBot

首次启动后 `data/goals.db` 自动创建。更多细节见
[附录：原版文档](#附录原版文档保留) 的「安装」章节。

> **⚠️ 版本要求**：本版本要求 MaiBot ≥ **v1.0.0**（`_manifest.json` 的
> `host_application.min_version`，与社区其他插件一致），
> MaiBot v1.1.0 已测试可用（`max_version: 1.99.99`）。

---

## v4.5.0 新特性

### MaiBot v1.1.0 兼容

MaiBot v1.1.0 收紧了插件 Host 版本校验：`host_application.max_version` 的
major + minor 必须与主程序匹配，否则拒绝加载。本版本将：

- `max_version` 从 `1.0.0` 放宽为 **`1.99.99`**
- `min_version` 更新为实际测试过的最低版本 **`1.0.12`**

插件使用的全部 13 项 capability 与 2 个 Hook
（`maisaka.planner.before_request` / `maisaka.replyer.before_request`）
在 v1.0.12 → v1.1.0 之间均无破坏性变更，功能不受影响。

### LLM 调用超时可配置

此前 `generation_timeout` 配置（默认 180 秒）只做了参数校验、从未真正生效：
SDK/Host 层 RPC 默认 **30 秒**就超时，即使你在主程序模型配置里把超时设成
60 秒，插件侧的 LLM 调用依然在 30 秒报 `[E_TIMEOUT] 请求 cap.call 超时`。

v4.5.0 起，以下三条 LLM 调用链路都会把 `generation_timeout`（秒）换算为
`timeout_ms` 传给 SDK：

| 链路 | 位置 |
|---|---|
| 日程生成（多轮） | `ScheduleGenerator._call_llm` |
| 次日策略推断 | `ScheduleAutoScheduler._infer_next_day_prompt` |
| 角色裁判（update_schedule） | `judge_schedule_request` |

默认 180 秒；如需更长（如慢速模型），在插件配置里把
`[schedule] generation_timeout` 调大即可。

### custom_prompt = 角色的长期状态

`custom_prompt` 的语义从"一次性要求"升级为**角色的长期生活状态 / 持续阶段**。
如果你填了"我正在环游世界"，那么：

- **日程生成**：prompt 中的「特殊要求」改名为「当前生活阶段与今日重点」，
  生成的活动会延续这个方向；
- **次日推断**：`custom_prompt` 从推断 prompt 的末位提到**首位**，标注为
  「角色的长期状态」，日程历史降级为「长期状态的具体表现」；
- **合并而非覆盖**：推断系统生成 `next_day_prompt` 后，配置的
  `custom_prompt` 不再被丢弃，而是合并为
  `推断结果 + 【底层的长期状态】配置值`。

这样"备考研究生"的 bot 不会在推断链路里突然"考完了"——长期状态始终是
日程演化的主信号。

> 💡 兼容性说明：旧配置里"今天想多运动"这类一次性要求现在会被当作
> "最近想多运动"的持续阶段处理。如果你需要的是**一次性**要求，请使用
> `/plan regenerate 今天想多运动`（临时叠加，生成后自动还原）。

### 自定义日程时间范围

新增 `[schedule]` 配置项：

| 字段 | 默认 | 说明 |
|---|---|---|
| `day_start_time` | 留空（00:00） | 一天日程从几点开始（HH:MM） |
| `day_end_time` | 留空（24:00） | 一天日程到几点结束（HH:MM） |

生成日程时，所有活动的开始时间被**硬约束**在这个范围内。支持跨夜写法：
`day_start_time = "23:00"` + `day_end_time = "07:00"` 表示 23:00 睡到次日
07:00，一天的活动只安排在这个区间。

**示例**：想规定睡眠时间是"23:00 到次日 07:00"：

```toml
[schedule]
day_start_time = "23:00"
day_end_time = "07:00"
```

---

### 无睡眠模式

新增配置项 `[schedule] no_sleep_mode`（默认 `false`）。开启后：

- 生成日程时**不安排**"睡觉 / 睡眠 / 安睡 / 睡午觉 / 小憩"等睡眠类活动；
- 原本属于睡眠的时段改为**无所事事**（自由活动 / 放空），
  `goal_type` 用 `rest` / `free_time`；
- 即使 LLM 偶尔漏网生成了睡眠类活动，也会被
  `_apply_no_sleep_postprocess` 后处理**强制转换**为"无所事事"，
  保证开启后日程中绝不出现睡眠活动；
- 一天仍然无缝衔接，不会因为去掉睡眠出现大段空档。

**典型搭配**（issue #12）：规定一天的日程范围 + 无睡眠模式：

```toml
[schedule]
day_start_time = "23:00"
day_end_time = "07:00"
no_sleep_mode = true
```

这样 23:00 - 07:00 这个"本应是睡眠"的时段会被安排成无所事事 / 安静的
休闲活动，而不是睡觉。

> 💡 适合不需要睡眠的角色设定（机器人、AI、非人生物等）；需要正常作息的
> 角色请保持默认关闭。

### /plan 命令返回合并转发

`/plan status`、`/plan help`、`/plan list` 降级文本等**长文本返回**不再
直接刷屏，改为：

- **第 1 条**：头部固定输出（如 `📅 今日日程 2026-05-25 周一 / 共 N 项活动`）
- **第 2 条**：剩余正文（逐条活动明细 / 命令说明）

两条消息通过 `ctx.send.forward` **合并转发**为一条转发气泡。短消息
（权限提示 / 删除 / 清理 / 错误信息等）保持普通文本发送；若平台不支持
合并转发，自动回退为普通文本，功能不受影响。

---

## 配置说明

插件配置模型为 `config_models.py`（`AutonomousPlanningV4Config`），WebUI
显示 4 个 section：

| Section | 内容 |
|---|---|
| 插件 | 开关、配置版本（`config_version` 与插件版本同步） |
| 后台清理 | 清理间隔、旧目标保留天数 |
| 日程管理 | 注入开关、生成参数、自定义时间范围、定时、白名单、时区、LLM 日志（主战场） |
| 智能注入策略 | 注入模式（已弃用）、意图分类、状态分析、冷却 |

v4.5.0 新增/变更的配置字段：

- `schedule.custom_prompt` —— label 改为「当前生活阶段 / 长期状态」
- `schedule.day_start_time` / `schedule.day_end_time` —— 日程时间范围
- `schedule.no_sleep_mode` —— 无睡眠模式
- `schedule.generation_timeout` —— 现在真正生效（见上）

旧版 `[autonomous_planning.schedule.*]` 三级嵌套配置会在加载时自动迁移到
顶层 `[schedule.*]` / `[inject.*]`，无需手动处理。

---

## 对外 API（其他插件调用）

本插件向其他插件暴露的 API 严格保持兼容（v4.5.0 未做任何变更）：

```python
snapshot = await self.ctx.api.call(
    "xuqian13.autonomous-planning-plugin-v4.get_current_activity",
    chat_id="global",
)
if snapshot["has_activity"]:
    print(snapshot["activity"]["name"])
```

返回结构：

```json
{
  "has_activity": true,
  "activity": {
    "name": "晚餐",
    "description": "晚饭在食堂二楼吃了木桶饭…",
    "goal_type": "meal",
    "time_window": "17:30-18:15"
  },
  "next_activities": [{"time": "18:15", "name": "操场散步"}],
  "as_of": "2026-05-25T18:00:00+08:00",
  "timezone": "Asia/Shanghai"
}
```

完整说明见 [附录：原版文档](#附录原版文档保留) 的「给其他插件用」章节。

---

## 开发与维护

- **源码结构**：`plugin.py`（组件外壳）→ `services/`（业务）→
  `planner/`（目标/日程/推断）→ `handlers/inject/`（注入子模块）→
  `utils/` / `cache/` / `database/` / `core/`。
- **版本号同步**：修改版本时需同步 3 处——
  `_manifest.json` 的 `version`、`config_models.py` 的
  `SUPPORTED_CONFIG_VERSION`、`__init__.py` 的 `__version__`。
- **变更记录**：每个版本变更请写入 `CHANGELOG.md`。
- **图标**：`logo.jpg` 位于插件根目录，由 `_manifest.json` 的
  `display.icon` 引用（`type: "local"`）。
- **数据目录**：运行时 `data/`（goals.db / llm_logs/）已被 `.gitignore` 排除，
  不进入版本库。

---

## License

AGPL-3.0（保留上游 LICENSE 全文；本项目未引入其他来源代码）

---

## 附录：原版文档（保留）

> 以下为 v4.4.5 时代的原版 README，**内容逐字保留**，未做任何修改。
> 其中「项目结构」一节提到的 `tests/run_smoke.py` 在 v4.5.0 已移除。

---

# 麦麦自主规划插件 v4

> 让麦麦具备自主规划能力的 MaiBot 插件，通过 LLM 智能生成符合人设的日常生活日程，像真实生活着的人 —— 有自己的日程、有当下在做的事、回复时贴合生活节奏。

---

## 这个插件做什么

- 🌅 **有作息**：每天 LLM 自动生成 8-15 个活动，从睡觉、三餐到学习娱乐覆盖全天
- 🧠 **知道现在在干嘛**：planner 决策和 replyer 回复都能感知"当前活动"
- 🗣️ **回复贴合状态**：晚上 18 点你问"忙吗"，她会自然带出"刚吃完饭"
- 📅 **能记下未来的事**：你说"周六一起打游戏"，那天的日程会自动加上这件事
- ⚖️ **能拒绝离谱请求**：LLM 扮演角色判断 today / future / reject，不是无脑接受
- 🌙 **懂跨天活动**：23:00 开始的睡眠，凌晨 1 点注入也能识别"还在睡"

---

## 装上看到的第一件事

启动 MaiBot，日志里会冒出来：

```
插件 xuqian13.autonomous-planning-plugin-v4 v4.4.3 加载成功
[v4] bot_profile 已预拉取: {'personality': '...', 'reply_style': '...', 'bot_name': '...'}
✅ 智能注入组件已加载 (intent=True, optimizer=True, context=3/600s)
🧹 麦麦目标清理循环已启动
📅 自动调度循环已启动
🌟 活动驱动主动行为循环已启动（v4.4）
日程定时生成已启动 - 执行时间: 00:30
```

打开 WebUI 插件配置页，看到 4 个 section：

| Section | 干啥用 |
|---|---|
| 插件 | 开关、版本 |
| 后台清理 | 多久清一次过期目标和日志 |
| 日程管理 | 注入开关、生成参数、定时、白名单、时区、LLM 日志…（主战场） |
| 智能注入策略 | 注入模式、意图分类、冷却 |

每个字段都有中文 label + 提示。

---

## 三个典型对话

### 📌 让她生成今天的日程

```
你：帮我生成今天的日程
麦麦：[generate_schedule_v4 → LLM] 生成了 12 个活动，已经记下了
你：/plan status
麦麦：
  📅 今日日程 2026-05-25 周一  共 12 项活动
  1. ⏰ 00:00-07:00  🏠 睡觉
     📝 在温暖的被窝里睡个好觉…
  2. ⏰ 07:00-07:45  🏠 起床洗漱
     📝 闹钟一响就爬起来…
  ...
```

### 📌 自然贴合当前状态

```
（18:00 晚餐时段）
你：你忙吗？
麦麦：嗯，刚好吃完，要做点什么吗？
              ↑ 没说"我正在吃饭"，因为 prompt 写了"不要主动提及"
              只在你问起时自然带出来
```

### 📌 未来约定

```
（周三）
你：周六我们一起去吃章鱼烧
麦麦：嗯，周六记下了
       ↑ update_schedule_v4 → 角色裁判 → decision=future
       → 写入 goals 表 pending_commitment

（周六 00:30）
[自动调度] 拉到今日 pending → 生成时融入 →
  14:00 和朋友吃章鱼烧
[完成后 consume → goal status=completed]
```

---

## 用户命令

| 命令 | 作用 |
|---|---|
| `/plan` 或 `/规划` | 显示帮助 |
| `/plan status` | 今日日程（文字详细） |
| `/plan list` | 今日日程（图片） |
| `/plan regenerate [额外要求]` | 重新生成今日日程（先删今天再重生，可附加临时要求） |
| `/plan delete <id或序号>` | 删除指定目标 |
| `/plan clear [days]` | 清理旧日程，默认仅保留今天 |

`admin_users` 留空 = 所有人可用；
`allowed_streams` 留空 = 所有群可用。

---

## 给其他插件用

如果你也在写插件、想知道"麦麦现在在做什么"，调一下：

```python
snapshot = await self.ctx.api.call(
    "xuqian13.autonomous-planning-plugin-v4.get_current_activity",
)

if snapshot["has_activity"]:
    print(snapshot["activity"]["name"])         # "晚餐"
    print(snapshot["activity"]["time_window"])  # "17:30-18:15"
    print(snapshot["next_activities"][:1])      # [{"time": "18:15", "name": "操场散步"}]
```

完整返回：

```json
{
  "has_activity": true,
  "activity": {
    "name": "晚餐",
    "description": "晚饭在食堂二楼吃了木桶饭…",
    "goal_type": "meal",
    "time_window": "17:30-18:15"
  },
  "next_activities": [{"time": "18:15", "name": "操场散步"}],
  "as_of": "2026-05-25T18:00:00+08:00",
  "timezone": "Asia/Shanghai"
}
```

没有当前活动时 `has_activity=false`，`activity=null`。

---

## 安装

1. 把整个目录放到 `MaiBot/plugins/xuqian13_autonomous-planning-plugin-v4/`
2. 确认主程序 `config/model_config.toml` 里 `[model_task_config.planner]` 段已配好（MaiBot 默认就带这段，无需新建），`model_list` 至少一个能用的模型：

   ```toml
   [model_task_config.planner]   # 规划模型配置
   model_list = [
       "你的模型名",
   ]
   max_tokens = 2048
   temperature = 0.3
   ```

   想换别的任务名，把插件 `config.toml` 的 `[schedule] llm_task_name = "..."` 改成对应的任务名即可（例如 `replyer` / `utils` 等主程序已配置的任务）。
3. 启动 MaiBot

完事。`data/goals.db` 会在第一次启动时自动建。

---

## 出问题了？

| 现象 | 看这里 |
|---|---|
| 日程定时生成"未启用" | `auto_schedule_enabled = true` 改了没 |
| LLM 生成日程一直失败 | 看 `data/llm_logs/fail_schedule_generation_*.txt`，里面有完整 prompt 和响应 |
| 麦麦回复时不提当前活动 | 正常 —— prompt 写了"不要主动提及"；只在用户问起 / 强相关时才带出来 |
| 麦麦每天日程都差不多 | 提高 inject 的 temperature（0.85）+ 看 prompt 里的"原则" |

---

## 跑冒烟测试

不动主程序也能验证代码层面没回归：

```powershell
cd "F:\下载\Maibot 插件开发\MaiM-with-u\MaiBot"
.\.venv\Scripts\python.exe plugins\xuqian13_autonomous-planning-plugin-v4\tests\run_smoke.py
```

13 项全过 ✅。

---

## 项目结构（一眼看完）

```
plugin.py             ← 入口 + 7 个组件装饰器 + v4.0→4.1 配置迁移
config_models.py      ← 4 个顶层 PluginConfigBase（plugin / autonomous_planning / schedule / inject）
config.toml           ← 默认配置

services/             ← 4 个 service：tools / command / inject / cleanup
planner/              ← goal_manager + schedule_generator + auto_scheduler + role_judge + generator/
handlers/inject/      ← 智能注入子模块：意图分类、状态分析、注入优化器、上下文缓存
utils/                ← 时区 / 时间 / 流过滤 / LLM 日志 / 图片生成
cache/                ← 线程安全 LRU
database/             ← SQLite 数据访问
core/                 ← 数据模型 / 异常 / 常量
tests/run_smoke.py    ← 13 项端到端冒烟

data/                 ← 运行时（goals.db / llm_logs/）
```

---

## License

AGPL-3.0
