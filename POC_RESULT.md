# V4 POC 静态契约推断结果

## TL;DR

**通过源码静态分析得出结论：旧版 `POST_LLM` EventHandler 在新版主程序下无法修改 LLM prompt**。
必须改用 **`HookHandler("maisaka.planner.before_request")`** 实现注入。

---

## 源码追踪：POST_LLM 事件的实际数据流

通过逐层阅读主程序源码，POST_LLM 事件从触发到到达插件 handler 的完整路径如下：

```
1. 业务层 event_bus.emit(POST_LLM, MaiMessages(llm_prompt="..."))
   src/core/event_bus.py:193

2. event_bus._bridge_to_ipc_runtime
   ↓ message.to_transport_dict()       ← MaiMessages → dict（包含 llm_prompt）
   src/core/event_bus.py:191

3. PluginRuntimeManager.bridge_event(event_value, message_dict)
   src/plugin_runtime/integration.py:979

4. PluginMessageUtils._build_session_message_from_dict(message_dict)
   src/plugin_runtime/host/message_utils.py:478
   ⚠️ 此函数只读取以下字段：
      message_id, timestamp, platform, message_info, raw_message,
      is_mentioned, is_at, is_emoji, is_picture, is_command, is_notify,
      session_id, reply_to, processed_plain_text
   ❌ 完全丢弃了 llm_prompt / llm_response_* / additional_data / action_usage

5. supervisor.dispatch_event(event_type, message=SessionMessage)
   src/plugin_runtime/host/supervisor.py:274

6. EventDispatcher._invoke_handler
   ↓ _session_message_to_dict(SessionMessage)    ← SessionMessage 本身无 llm_prompt
   src/plugin_runtime/host/event_dispatcher.py:120

7. RPC IPC → Runner 子进程 → 插件 @EventHandler 收到的 message dict
   ❌ 不含 llm_prompt
```

## 验证：MessageDict 字段清单

`PluginMessageUtils._session_message_to_dict()` 输出的 dict 包含：

| 字段 | 是否传给插件 |
|---|---|
| `message_id` | ✅ |
| `timestamp` | ✅ |
| `platform` | ✅ |
| `message_info` | ✅（含 user_info、group_info、additional_config） |
| `raw_message` | ✅（消息段列表） |
| `is_mentioned / is_at / is_emoji / is_picture / is_command / is_notify` | ✅ |
| `session_id` | ✅ |
| `reply_to` | ✅（可选） |
| `processed_plain_text` | ✅（可选） |
| **`llm_prompt`** | ❌ **被丢弃** |
| **`llm_response_content`** | ❌ **被丢弃** |
| **`llm_response_reasoning`** | ❌ **被丢弃** |
| **`additional_data`** | ❌ **被丢弃** |
| **`action_usage`** | ❌ **被丢弃** |

旧版 `MaiMessages.modify_llm_prompt()` 方法在新版子进程内根本不存在（plugin 收到的是 dict，不是 MaiMessages 对象），且即便修改也无法回写。

---

## 新版正确的注入入口：HookHandler

新版主程序在 `src/maisaka/chat_loop_service.py:97-139` 注册了内置 Hook **`maisaka.planner.before_request`**：

```python
HookSpec(
    name="maisaka.planner.before_request",
    description="在 Maisaka 向模型发起规划请求前触发，可改写消息窗口与工具定义。",
    parameters_schema={
        "messages": List[PromptMessage],         # 即将发给模型的消息列表
        "tool_definitions": List[Dict],
        "selected_history_count": int,
        "built_message_count": int,
        "selection_reason": str,
        "session_id": str,
    },
    allow_kwargs_mutation=True,                  # ← 关键：允许 hook 修改入参
    default_timeout_ms=6000,
)
```

**注入实现示意**：

```python
from maibot_sdk import HookHandler
from maibot_sdk.types import HookMode

class AutonomousPlanningPluginV4(MaiBotPlugin):

    @HookHandler(
        "maisaka.planner.before_request",
        name="schedule_inject_planner",
        mode=HookMode.BLOCKING,
    )
    async def inject_schedule_to_planner(
        self, messages: list, session_id: str, **kwargs
    ):
        """在 Maisaka 规划请求前注入当前日程信息。"""
        hint = await self._inject_svc.build_current_activity_hint(session_id)
        if not hint:
            return {"action": "continue"}
        modified_messages = self._inject_svc.append_system_hint(messages, hint)
        return {
            "action": "continue",
            "modified_kwargs": {"messages": modified_messages},
        }
```

## 其他可用 Hook（备查）

| Hook | 描述 | 是否允许修改 kwargs |
|---|---|---|
| `chat.receive.before_process` | 入站消息预处理前 | ✅ |
| `chat.receive.after_process` | 入站消息预处理后 | ✅ |
| `chat.command.before_execute` | 命令执行前 | ✅ |
| `maisaka.planner.before_request` | **向 LLM 发起规划请求前 ⭐** | ✅ |
| `maisaka.planner.after_response` | LLM 规划响应后 | ✅ |
| `maisaka.replyer.after_response` | replyer 响应后（可要求重生成） | ✅ |
| `emoji_system.*` | 表情包系统 | - |
| `expression_learner.*` | 表达学习 | - |

---

## 对 v4 实施计划的影响

### 阶段 4 改造方案变更

| 项 | 旧计划 | 新计划 |
|---|---|---|
| 注入事件 | `@EventHandler(POST_LLM, intercept_message=True)` | `@HookHandler("maisaka.planner.before_request", mode=BLOCKING)` |
| 修改对象 | `message.llm_prompt: str`（追加文本） | `messages: list[PromptMessage]`（插入/修改 system 消息） |
| 返回值 | 5 元组 `(True, True, None, None, modified_dict)` | `{"action": "continue", "modified_kwargs": {"messages": new_list}}` |
| 取消注入 | `return True, True, None, None, None` | `return {"action": "continue"}` |

### 业务模块连带改造

`handlers/inject/` 下原本的工具函数全部保留：
- `IntentClassifier` / `ActivityStateAnalyzer` / `InjectOptimizer` / `ContentTemplate` / `ContextCache` —— 输出的是「要注入的文本」
- 注入应用层从「修改 llm_prompt 字符串」改为「向 messages 列表追加/合并 system 角色消息」（约 30 行新代码）

### 保留 POST_LLM Handler 的价值

POST_LLM 仍可作为**只读**事件存在，用于：
- 记录 LLM 调用历史到 goals.db
- 触发后台 ML 任务
- 不需要修改 prompt 的统计/审计场景

如果业务上只关心注入功能，可不实现 POST_LLM EventHandler，节省一处代码。

---

## 关于 POC 探测器代码

POC 代码（`plugin.py` 中的 `probe_post_llm` / `probe_on_message`）仍然有保留价值：
- 在主程序实际运行时验证「dict 中确实没有 llm_prompt」（双重保险）
- 探测 ON_MESSAGE 拿到的 dict 真实结构，便于后续 Command/EventHandler 业务从 dict 中读取用户信息

但**不是阻塞项**，可在阶段 1 一起做。

---

## 决策

✅ **POC 阶段静态完成，无需用户运行主程序验证**。直接进入阶段 1。

阶段 4 的注入实现路线变更为 **HookHandler("maisaka.planner.before_request")**，已锁定。
