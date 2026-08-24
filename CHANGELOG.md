# 更新日志 / Changelog

本文件记录麦麦自主规划插件 v4 的版本变更。格式遵循
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

> **Fork 溯源说明**：本分支的 v4.5.0 修改主要依据
> [xuqian13/autonomous_planning_plugin](https://github.com/xuqian13/autonomous_planning_plugin)
> 仓库的以下 issues（链接可点击回溯）：
>
> - [#14 请求更新 host_application.max_version 以兼容 MaiBot v1.1.0](https://github.com/xuqian13/autonomous_planning_plugin/issues/14)
> - [#13 MaiBot 1.1.0 兼容: host_application.max_version 需要更新](https://github.com/xuqian13/autonomous_planning_plugin/issues/13)
> - [#12 可否添加自定义日程？](https://github.com/xuqian13/autonomous_planning_plugin/issues/12)
> - [#11 custom_prompt 在推断链路中无效——日程演化缺乏叙事方向](https://github.com/xuqian13/autonomous_planning_plugin/issues/11)
> - [#9 模型请求失败（LLM 调用超时）](https://github.com/xuqian13/autonomous_planning_plugin/issues/9)
>
> 各版本条目中标注了对应 issue 编号，方便溯源。

## [4.5.0] - 2026-07-25

### 修复

- **兼容 MaiBot v1.1.0**（issue #13 / #14）：`_manifest.json` 的
  `host_application.max_version` 从 `1.0.0` 放宽为 `1.99.99`，
  `min_version` 调整为 **`1.0.0`**（与同仓库其他插件 maibot-team_napcat-adapter、
  maibot-deepseek-harness-connect、cateye_skland_sign 等一致，可在 MaiBot v1.0.x
  全系列加载）。此前 MaiBot 从 v1.0.12 升级到 v1.1.0 后，插件的 host 版本校验
  不通过，被标记为不兼容而拒绝加载。
- **LLM 调用超时可配置**（issue #9）：`generation_timeout` 配置此前只做了
  校验、从未真正生效，SDK/Host 层 RPC 默认 30 秒就超时，而用户在主程序
  模型配置中设置 60 秒超时也无济于事。现在日程生成、次日推断、角色裁判
  三条 LLM 调用链路都把 `generation_timeout`（秒）换算为 `timeout_ms`
  传给 SDK，超时真正可配置（默认 180 秒）。
- **custom_prompt 在推断链路中作为主信号**（issue #11）：
  - 次日推断 prompt 中把 `custom_prompt` 从末位提到首位，标注为
    「角色的长期状态」，日程历史降为「长期状态的具体表现」；
  - `_get_effective_custom_prompt` 从"推断结果直接覆盖配置值"改为**合并**：
    `推断结果 + 【底层的长期状态】配置值`，配置的长期状态不再被丢弃；
  - 生成 prompt 中「特殊要求」改名为「当前生活阶段与今日重点」；
  - 配置 UI 中 `custom_prompt` 的 label/hint 改为「当前生活阶段 / 长期状态」。

### 新增

- **自定义日程时间范围**（issue #12）：`schedule` 段新增 `day_start_time` /
  `day_end_time`（HH:MM，留空分别默认 00:00 / 24:00），生成日程时所有活动
  被硬约束在该范围内（支持跨夜写法，如 23:00-07:00 = 23:00 睡到次日 07:00）。
- **无睡眠模式**（`schedule.no_sleep_mode`，默认关闭）：开启后生成日程时
  不安排"睡觉 / 睡眠 / 安睡"类活动，原本属于睡眠的时段改为**无所事事**
  （自由活动 / 放空）。即使 LLM 偶尔漏网生成了睡眠类活动，也会被
  `_apply_no_sleep_postprocess` 后处理强制转换为"无所事事"。
- **/plan 命令返回改合并转发**：`/plan status`、`/plan help`、`/plan list`
  降级文本等长文本返回不再直接刷屏，改为构造**单条完整消息**（内容不切割）
  并通过 `ctx.send.forward` 合并转发（`ctx.send.forward`）发出；短消息
  （权限 / 删除 / 清理 / 错误提示）保持普通文本。转发失败时自动回退为
  普通文本，不影响功能。

### 重构

- `logo.jpg`（原 `bird.jpg`，已重命名）位于插件根目录作为图标
  （`_manifest.json` 的 `display.icon` 引用），
  删除 `assets/winter_char.jpg` 与空的 `assets/` 目录；日程图片生成器
  `ScheduleImageGenerator` 不再依赖冬季角色素材，背景改为纯渐变+雪花装饰。
- 重写 `.gitignore`，只保留必要项。
- 新增本 CHANGELOG.md。
- README 前部新增「快速上手 / 配置说明 / 常见问题」等章节，原文档完整保留在后部。

### 删除

- 移除 `tests/` 目录（冒烟测试依赖本机路径，且非插件运行必需）。

---

## [4.4.5] - 2026-05-25

### 修复

- 约定到点真发出：`_match_commitments_to_items` 把今日 `pending_commitments`
  一对一映射到日程项并注入 `is_commitment` / `commitment_*` 元数据，
  `ProactiveService` 对约定来源的活动改用强指令模板（不再陷入"自行决定是否回复"
  的工具调用循环）。

### 新增

- `/plan regenerate` 命令：立即重新生成今日日程（先删今天再重生，可附加临时要求）。
- `ScheduleGenerator` 配置单一来源：`plugin.build_schedule_config()` 统一所有
  构造入口的配置来源，修复 `auto_scheduler` 漏带 `bot_profile` 导致 prompt
  人设缺失的问题。

---

## [4.4.4] - 2026-05-20

### 修复

- `auto_scheduler` 改用 `plugin.build_schedule_config()` 构建配置，
  修复 schedule prompt 中人设（bot_profile）缺失的问题。

---

## [4.4.0] - 2026-05-15

### 新增

- 活动驱动的主动行为服务（`ProactiveService`）：活动切换瞬间主动开口
  （`maisaka.proactive.trigger`）+ 按活动类型调节聊天频率
  （`frequency.set_adjust`），需 `proactive_streams` 白名单显式开启。
- 恢复 `maisaka.replyer.before_request` 阶段注入（主程序已补上该 hook）。

---

## [4.3.x] - 2026-05

### 新增

- 活动状态分析（`ActivityStateAnalyzer`）：按活动进度注入情绪化短语。
- 精神状态模型（`utils/energy_model`）：按当前小时插入"精神满满 / 有点累"等。
- 主动碎碎念：闲聊场景概率性捎带当前活动（每会话每天 ≤3 次）。

---

## [4.2.x] - 2026-04

### 重构

- 合并 smart / rule 双注入模式为统一管道（意图分类 → 注入优化器 → 模板路由），
  `inject_mode` 字段保留仅为向后兼容。

---

## [4.1.x] - 2026-03

### 重构

- 配置结构扁平化：`[autonomous_planning.schedule.*]` 迁移到顶层
  `[schedule.*]` / `[inject.*]`，使 WebUI 能渲染所有配置 section。
- 移除 `inject_mode='traditional'`，自动降级为 `smart`。

---

## [4.0.0] - 2026-02

### 重构

- 基于 maibot-plugin-sdk v2.0 重写，从旧版 `src.plugin_system` API 迁移。
- 4 个 `@Tool` + 1 个 `@Command` + 1 个 `@EventHandler` + 2 个
  `@HookHandler` + 1 个 `@API` 组件化外壳，业务逻辑下沉到 `services/`。

---

[4.5.0]: https://github.com/xuqian13/autonomous_planning_plugin/releases/tag/v4.5.0
[4.4.5]: https://github.com/xuqian13/autonomous_planning_plugin/releases/tag/v4.4.5
[4.4.4]: https://github.com/xuqian13/autonomous_planning_plugin/releases/tag/v4.4.4
[4.4.0]: https://github.com/xuqian13/autonomous_planning_plugin/releases/tag/v4.4.0
[4.3.x]: https://github.com/xuqian13/autonomous_planning_plugin/releases
[4.2.x]: https://github.com/xuqian13/autonomous_planning_plugin/releases
[4.1.x]: https://github.com/xuqian13/autonomous_planning_plugin/releases
[4.0.0]: https://github.com/xuqian13/autonomous_planning_plugin/releases
