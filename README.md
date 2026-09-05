# 麦麦自主规划插件 v4 · v4.6.0

> MaiBot 自主规划插件：让麦麦像真实生活着的人一样——有自己的作息、当下正在做的事，
> 回复时贴合生活节奏，还会主动兑现和你的约定。
>
> 🗄️ 旧版说明（v4.5.0 及更早）已归档至 [README_v4.5_归档.md](README_v4.5_归档.md)，
> 版本变更详见 [CHANGELOG.md](CHANGELOG.md)。

---

## 目录

- [这个插件做什么](#这个插件做什么)
- [工作原理速览](#工作原理速览)
- [安装](#安装)
- [用户命令](#用户命令)
- [和 bot 的自然语言交互](#和-bot-的自然语言交互)
- [作息与无睡眠模式](#作息与无睡眠模式)
- [角色裁判](#角色裁判)
- [配置说明](#配置说明)
- [对外 API（其他插件调用）](#对外-api其他插件调用)
- [常见问题](#常见问题)
- [开发与维护](#开发与维护)
- [License](#license)

---

## 这个插件做什么

- 🌅 **有作息**：每天由 LLM 按人设自动生成一份覆盖全天、无缝衔接的日程
  （起床 → 清醒活动 → 入睡 → 睡觉，绕时钟闭环）
- 🧠 **知道现在在干嘛**：planner 决策和 replyer 回复前都会被注入"当前活动"，
  语气随活动进度、时段精神状态自然变化
- 📅 **能记下未来的事**：你说"周六一起打游戏"，角色裁判会判断接不接；
  接了就记成约定，到那天自动排进日程、到点主动兑现
- 🌙 **懂跨天**：23:00 入睡的活动，凌晨注入也能识别"还在睡"
- 🤖 **可选无睡眠模式**：机器人 / AI 类角色可以完全不上床，睡眠时段改为"无所事事"
- 📊 **一条命令看全天**：`/plan list` 以"图片 + 详细文字"合并转发返回；
  今天还没生成日程时会自动先生成再展示
- 📣 **主动行为（可选）**：活动切换时主动开口、睡醒后道早安，学习时少说话、休息时多说话

## 工作原理速览

```
每日定时(默认 00:30) / /plan list / /plan regenerate / 角色裁判 today
        │
        ▼
  LLM 生成全天日程（多轮 + 质量分，按人设/近几天日程/群聊背景/知识库拼上下文）
        │
        ▼
  goals 表（SQLite）──► 定时清理：过期日程标记完成，超期旧目标删除
        │
        ├─► planner / replyer 注入：让 bot 每次说话都"知道自己在干嘛"
        ├─► 主动行为：活动切换开口 + 按活动类型调节聊天频率（需白名单开启）
        └─► 对外 API get_current_activity：其他插件读取"当前活动"
```

## 安装

1. 把整个目录放到 `MaiBot/plugins/xuqian13_autonomous-planning-plugin-v4/`
2. 确认主程序 `config/model_config.toml` 里插件使用的 LLM 任务组已配置
   （默认用 `replyer`，可在插件配置里改 `LLM 任务名` 指向其他任务），
   `model_list` 至少一个可用模型
3. 启动 MaiBot，首次启动自动创建 `data/goals.db`

> **版本要求**：MaiBot ≥ v1.0.0（v1.1.0 已测试），SDK 2.x。
> 从 v4.5 及更早版本升级：旧配置会**自动迁移**（`day_start_time/day_end_time`
> → `sleep_time/wake_time`，失效字段自动清理），无需手动处理。

启动日志里应看到：

```
[v4] 自主规划插件 v4 已加载, data_dir=...
🧹 麦麦目标清理循环已启动
📅 自动调度循环已启动
🌟 活动驱动主动行为循环已启动
```

## 用户命令

| 命令 | 作用 |
|---|---|
| `/plan` 或 `/规划` | 显示帮助 |
| `/plan list` | **查看今日日程**：合并转发"日程图片 + 详细文字"两条记录；若今天还没有日程，会先提示并自动生成，完成后发送 |
| `/plan regenerate [要求]` | 删掉今天已有日程并重新生成，可附加临时要求（如 `/plan regenerate 今天生日要庆祝`） |
| `/plan delete <id或序号>` | 删除指定目标 |
| `/plan clear [days]` | 清理旧日程，默认仅保留今天 |
| `/plan help` | 显示帮助 |

权限：`admin_users` 留空 = 所有人可用；`allowed_streams` 留空 = 所有会话可用。
`/plan list` 是否画图、画图超时分别由 `list_draw_image` 与 `image_timeout_seconds`
控制；绘制失败或超时会**静默**降级为纯文字，不影响使用。

## 和 bot 的自然语言交互

bot 的 LLM 可调用 4 个工具，多数场景下直接说话即可：

| 工具 | 触发说法示例 | 说明 |
|---|---|---|
| `get_planning_status_v4` | "今天有什么安排""你现在在干嘛" | 查询今日日程 |
| `update_schedule_v4` | "周六一起打游戏""下午别学了" | 走**角色裁判**（见下节），决定改今天 / 记约定 / 拒绝 |
| `manage_goal_v4` | "记一个目标：每周读完一本书" | 长期目标的增删改查 |
| `apply_schedule_v4` | （一般不主动触发） | 把 JSON 日程数据落库 |

## 作息与无睡眠模式

日程由两个**作息锚点**划定（v4.6.0 新语义）：

| 配置 | 默认 | 含义 |
|---|---|---|
| `wake_time` | 07:00 | **起床（睡醒）时间**——清醒活动从这一刻开始排 |
| `sleep_time` | 23:00 | **入睡时间**——这一刻之后进入睡眠时段，直到次日起床 |

- 清醒活动必须落在 `[起床, 入睡]` 区间内；睡眠时段正常模式下安排"睡觉"
  （跨午夜直接用大于 24h 的累计时长表达，如 23:00 入睡 → 8 小时 → 次日 07:00）
- 入睡时间早于起床时间（如入睡 03:00 / 起床 11:00）= 跨午夜夜猫子作息，同样支持
- 生成的提示词示例与时间框架会按你配置的锚点**动态生成**，所见即所得

**无睡眠模式**（`no_sleep_mode`，默认关）：开启后全天不出现任何睡眠类活动，
入睡到起床的时段改为**无所事事**（放空）。提示词与代码后处理双重保证——
即使 LLM 漏网生成了"睡觉/午休/打盹"，落库前也会被强制转换为"无所事事"。
适合机器人、AI、非人生物等不需要睡觉的角色设定。

## 角色裁判

当用户通过自然语言提出日程请求（"明天下午一起逛街""今晚别学了"）时，
插件把 **角色人设 + 今天日期 + 当前完整日程 + 请求原文** 交给 LLM，
让它**扮演 bot 这个角色**判断"我接不接"，输出三选一：

| 判定 | 含义 | 后续动作 |
|---|---|---|
| `today` | 接受，调整今天 | 自动重生成今日日程，把请求融入其中 |
| `future` | 接受未来预约 | 写入候选清单；到了那天生成日程时自动纳入，到点主动兑现 |
| `reject` | 角色拒绝 | 日程不变，附角色口吻的理由 |

裁判不是有求必应——不符合人设、时间冲突太强、语气像玩笑都会被拒。
判定失败时安全降级为"日程不变"。关闭 `role_judge_enabled` 则跳过裁判，
所有请求一律记为未来约定。温度由 `role_judge_temperature`（默认 0.3）控制，
越低越保守。

## 配置说明

插件配置模型为 `config_models.py`（`AutonomousPlanningV4Config`），WebUI 显示
4 个 section，全部字段都有中文 label + 提示：

| Section | 内容 |
|---|---|
| 插件 | 总开关、配置版本 |
| 后台清理 | 清理间隔（默认 1h）、旧目标保留天数（默认 30 天） |
| 日程管理 | 作息锚点、无睡眠模式、注入开关、生成参数、定时生成、跨群上下文、LLM 任务、角色裁判、缓存、`/plan list` 绘图选项 |
| 智能注入策略 | 意图分类、活动状态分析、注入冷却、闲聊注入概率、对话上下文 |
| 管理与日志 | 管理员 QQ、会话白名单（注入+命令）、LLM 调用归档与保留天数 |
| 主动行为 | 群聊/其他会话白名单、触发窗口、活动切换主动发起、早间问好、频率调控 |

常用项速查：

| 想做什么 | 改哪里 |
|---|---|
| 规定作息"23 点睡 7 点起" | `sleep_time="23:00"`、`wake_time="07:00"`（默认即是） |
| 让 bot 不睡觉 | `no_sleep_mode=true` |
| 换生成用的模型 | `llm_task_name`（对应主程序 model_config 任务名） |
| 慢模型总超时 | `generation_timeout`（默认 180 秒，v4.5.0 起真正生效） |
| 只让管理员用命令 | `admin_users` 填 QQ 号（「管理与日志」段；留空 = 所有人） |
| 只在部分群启用注入和命令 | `allowed_streams`（「管理与日志」段；留空 = 全部会话） |
| 关闭日程图片 | `list_draw_image=false`（「日程管理」段） |
| 睡醒后自动道早安 | `enable_morning_greeting=true`（「主动行为」段；可配 `morning_greeting_require_activation` 要求群里有人说话才发） |
| 圈定主动行为范围 | 群聊填 `proactive_group_ids`（**留空 = 所有群聊**）；私聊/指定会话填 `proactive_other_streams`（`qq:private:789` / `session:xxx`） |

每个字段在 WebUI 里都有说明；完整字段逐条解释见 CHANGELOG 与项目内文档。

## 对外 API（其他插件调用）

本插件向其他插件暴露一个读取接口（13 项 capability，含 `api.call` 供消费方声明）：

```python
snapshot = await self.ctx.api.call(
    "xuqian13.autonomous-planning-plugin-v4.get_current_activity",
    chat_id="global",
)
if snapshot["has_activity"]:
    print(snapshot["activity"]["name"])         # "晚餐"
    print(snapshot["activity"]["time_window"])  # "17:30-18:15"
    print(snapshot["next_activities"][:1])      # [{"time": "18:15", "name": "操场散步"}]
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
  "timezone": "Asia/Shanghai",
  "error": null
}
```

没有当前活动时 `has_activity=false`、`activity=null`。
接口兼容性分析（MaiTrace / 麦麦绘卷等消费方如何使用、如何用新插件替代本插件）
见项目配套文档。

## 常见问题

| 现象 | 看这里 |
|---|---|
| 日程定时生成"未启用" | 检查 `auto_schedule_enabled = true` |
| LLM 生成一直失败 / 报超时 | 看 `data/llm_logs/fail_schedule_generation_*.txt`（完整 prompt 与响应）；必要时调大 `generation_timeout` |
| `/plan list` 没有图片 | 图片绘制超时（`image_timeout_seconds`）或被关闭（`list_draw_image`）时会静默降级为文字，属预期行为 |
| 无睡眠模式下仍出现"午休" | v4.6.0 起关键词表已含午休/打盹/赖床；若仍有漏网活动名，属 LLM 偶发，后处理会在落库前转换 |
| 麦麦回复从不提当前活动 | 正常——注入文案写了"不要主动提及"，只在被问到 / 强相关时自然带出 |
| 升级后配置变了 | v4.5 → v4.6 自动迁移作息字段；`inject_mode`、`auto_generate` 等无效字段已移除 |

## 开发与维护

- **源码结构**：`plugin.py`（组件外壳：4 Tool + 1 Command + 2 Hook + 1 API）→
  `services/`（业务）→ `planner/`（目标 / 日程生成 / 定时 / 裁判）→
  `handlers/inject/`（注入子算法）→ `utils/` `cache/` `database/` `core/`
- **版本号同步**：修改版本时需同步 3 处——`_manifest.json` 的 `version`、
  `config_models.py` 的 `SUPPORTED_CONFIG_VERSION`、`__init__.py` 的 `__version__`
- **变更记录**：每个版本的变更写入 `CHANGELOG.md`
- **数据目录**：运行时 `data/`（goals.db / llm_logs/）已被
  `.gitignore` 排除，不进入版本库
- **历史文档**：v4.5 及更早的说明见 [README_v4.5_归档.md](README_v4.5_归档.md)

## License

AGPL-3.0（保留上游 LICENSE 全文；本项目未引入其他来源代码）
