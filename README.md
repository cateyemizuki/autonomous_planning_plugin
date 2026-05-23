# 麦麦自主规划插件 v4

让麦麦具备自主规划能力的 MaiBot 插件，通过 LLM 智能生成符合人设的日常生活日程，
并在对话中自然提及当前活动。

**v4 基于 `maibot_sdk` v2.x 全面重写**，从旧版 `src.plugin_system` API 完整迁移到原生
新版 SDK，适配新版 Host ↔ Runner 子进程 IPC 架构。

---

## 主要特性

- 🤖 基于 Bot 人设智能生成每日日程（8-15 个活动）
- ⏰ 定时自动生成（默认每天 00:30）
- 💬 对话中自然提及当前活动（如「这会儿正吃午饭呢」）
- 📊 文字 + 图片两种日程展示
- 🔄 多轮生成机制，提升日程质量
- 💾 LRU 缓存优化，减少重复计算
- 🗑️ 自动清理过期目标（默认保留 30 天）
- 🌏 时区支持（默认 `Asia/Shanghai`）
- 🧠 智能注入：意图分类 + 状态分析 + 注入优化，仅在涉及时间的对话中注入
- 🎨 自定义提示词：可配置插件级日程风格偏好

---

## v4 关键变化（相对 v3）

| 维度 | v3 | v4 |
|------|----|----|
| 插件 API | `src.plugin_system` (旧) | `maibot_sdk` (新) |
| 主基类 | `BasePlugin` | `MaiBotPlugin` |
| 组件声明 | `get_plugin_components()` + 子类 | 装饰器 `@Tool/@Command/@EventHandler/@HookHandler` |
| 配置访问 | `self.get_config("a.b.c")` | `self.config.a.b.c`（强类型，pydantic） |
| 注入入口 | `EventHandler(POST_LLM)` 修改 `message.llm_prompt` | `HookHandler("maisaka.planner.before_request")` 修改 `messages` 列表 |
| 消息发送 | `self.send_text(t)` | `await self.ctx.send.text(t, stream_id)` |
| LLM 调用 | `llm_api.generate_with_model(prompt, model_config=…)` | `await self.ctx.llm.generate(prompt, model="<任务名>", …)` |
| 自定义模型 | 支持 `custom_model` 段，向 `src.config` 注册临时 provider | **已删除**，统一走主程序 `model_config.toml` 任务名 |
| 数据库 | 插件自管 SQLite (`data/goals.db`) | 不变 |
| 主进程依赖 | 直接 `from src.config / src.common.logger` 等 | **0 处**，全部通过 SDK 能力代理 |

---

## 升级指引（从 v3 → v4）

### 1. 主程序需要的预先配置

v4 不再支持 `custom_model` 内置 HTTP 直连，所有 LLM 调用都通过主程序的
`model_config.toml`。如果你之前依赖 `custom_model`，需要在主程序的 `model_config.toml`
中预先配置一个任务（任务名默认为 `replyer`，可在插件 `config.toml` 中改写）：

```toml
# model_config.toml（主程序）
[tasks.replyer]
model_list = ["你的模型"]
temperature = 0.7
```

或在插件 `config.toml` 中指定其它任务名：

```toml
[autonomous_planning.schedule]
llm_task_name = "schedule_generator"  # 主程序 model_config 中要对应配置此任务
```

### 2. 配置文件

v4 配置层级与 v3 完全一致（仍是 `[plugin]` / `[autonomous_planning]` /
`[autonomous_planning.schedule]` / `[autonomous_planning.schedule.inject]`），
**唯一差异**：

- 新增 `[plugin] config_version = "4.0.0"`（必填，否则 Runner 加载失败）
- 新增 `[autonomous_planning.schedule] llm_task_name = "replyer"`（默认 `replyer`）
- **删除** `[autonomous_planning.schedule.custom_model]` 整段（保留也不会被读取）

如果直接用 v3 的 `config.toml`，只需要给 `[plugin]` 段补一个 `config_version = "4.0.0"`
即可继续使用，原有字段都兼容。

### 3. 命令

v4 命令名加 `_v4` 后缀避免与并行运行的 v3 冲突（迁移期建议 v3/v4 并行测试）：

| v3 命令 | v4 命令 |
|---------|---------|
| `/plan status` | `/plan status` |
| `/plan list` | `/plan list` |
| `/plan delete <id>` | `/plan delete <id>` |
| `/plan clear` | `/plan clear` |
| `/plan help` | `/plan help` |

命令文本完全一致（v4 的正则与 v3 相同），用户无需调整使用习惯。命令组件**内部名**
带 `_v4` 后缀（`planning_v4`），LLM Tool **内部名**也带后缀（`manage_goal_v4` 等）。

### 4. 数据库

数据库文件路径不变：`plugins/autonomous_planning_plugin_v4/data/goals.db`。
**注意** v4 与 v3 各自维护独立 `data/` 目录，数据不会自动迁移；如需迁移，
手动复制 `plugins/autonomous_planning_plugin/data/goals.db` 到 v4 的 `data/` 目录即可。

### 5. 完成迁移后

确认 v4 在生产环境稳定运行后，可禁用旧 v3 插件：

```toml
# plugins/autonomous_planning_plugin/config.toml
[plugin]
enabled = false
```

或直接删除 `plugins/autonomous_planning_plugin/` 目录。

---

## 组件清单

| 类型 | 名称 | 功能 |
|------|------|------|
| Tool | `manage_goal_v4` | 目标管理（创建/查看/更新/暂停/恢复/完成/取消/删除） |
| Tool | `get_planning_status_v4` | 查询今日日程（简洁文字格式） |
| Tool | `generate_schedule_v4` | 生成每日/每周/每月日程（LLM） |
| Tool | `apply_schedule_v4` | 应用之前生成的日程为目标 |
| Command | `planning_v4` (`/plan` 或 `/规划`) | 规划管理命令（status/list/delete/clear/help） |
| EventHandler | `autonomous_planner_v4` (ON_START) | 启动信号通知 |
| HookHandler | `schedule_inject_v4` (`maisaka.planner.before_request`) | **v4 注入入口**：向 LLM 规划请求的 messages 列表插入日程上下文 |

---

## 使用示例

### 通过对话

```
用户：帮我生成今天的日程
麦麦：[Tool 调用 generate_schedule_v4，LLM 生成 8-15 个活动并自动保存]

用户：在干嘛？
麦麦：[HookHandler 在 LLM 调用前注入当前日程上下文]
     这会儿正吃午饭呢~等下还要去图书馆学习
```

### 通过命令

```bash
/plan status     # 文字格式日程
/plan list       # 图片格式日程
/plan delete 1   # 删除第 1 个目标
/plan clear      # 清理昨天及更早的日程
/plan help       # 显示帮助
```

---

## 开发者文档

### 项目结构

```
autonomous_planning_plugin_v4/
├── _manifest.json              # manifest v2，SDK 2.0+
├── __init__.py
├── config.toml                 # 默认配置
├── config_models.py            # PluginConfigBase 嵌套模型（三级）
├── plugin.py                   # MaiBotPlugin 主类，7 个装饰器外壳
├── services/                   # 业务实现层
│   ├── tools_service.py        # 4 个 Tool 的业务
│   ├── command_service.py      # /plan 命令分发
│   ├── inject_service.py       # HookHandler 注入业务
│   └── cleanup_service.py      # 后台清理 + 自动调度
├── planner/                    # 核心规划逻辑
│   ├── goal_manager.py
│   ├── schedule_generator.py   # LLM 日程生成（通过 ctx.llm.generate）
│   ├── auto_scheduler.py       # 定时调度器
│   └── generator/              # 生成器子模块（prompt / schema / parser / validator）
├── handlers/inject/            # 智能注入子模块（意图分类等）
├── core/                       # 数据模型 / 常量 / 异常
├── cache/                      # LRU 缓存
├── utils/                      # 时区 / 时间工具 / 图片生成
├── database/goal_db.py         # 插件自管 SQLite
└── data/                       # 运行时数据目录（goals.db）
```

### 关键技术决策

1. **HookHandler 替代 POST_LLM 事件**：新版主程序 `bridge_event` 在 dict ↔ SessionMessage
   转换时丢失 `llm_prompt` 字段，POST_LLM EventHandler 无法修改 prompt；改用
   `maisaka.planner.before_request` Hook 修改 messages 列表实现等价注入。

2. **配置访问双轨**：业务模块（`planner/auto_scheduler.py` 等）保留 v3 风格的
   `self.plugin.get_config("a.b.c", default)` 调用，由 `plugin.py` 中的 `get_config()`
   适配器把点分割路径桥接到强类型 `self.config.a.b.c`，减少业务代码改动。

3. **bot_profile 预拉取**：`on_load` 时一次性通过 `ctx.config.get(...)` 拉取
   `personality / bot.nickname` 等全局配置缓存到 `_bot_profile`，PromptBuilder
   通过 config 字典中的 `bot_profile` 段读取，运行时零 IPC。

4. **数据库独立**：`ctx.db.*` 走 Host 数据库模型，不适合插件自有表；保留 v3 时代的
   独立 SQLite（`data/goals.db`），数据库路径由 `Path(__file__).parent / "data"`
   推导，子进程内可靠。

### 验证

```bash
cd /home/ubuntu/maimai/MaiBot
# 1. 静态检查：插件无 src.* 残留
grep -rn "from src\|maibot_sdk.compat" plugins/autonomous_planning_plugin_v4/ \
    --exclude-dir=__pycache__
# 应输出空（POC_RESULT.md 中提到的 compat 已删除）

# 2. 业务模块 import
uv run python -c "from plugins.autonomous_planning_plugin_v4 import create_plugin; \
    p = create_plugin(); print('OK', type(p).__name__)"
```

---

## License

AGPL-3.0
