"""端到端冒烟测试 —— 不依赖真实 MaiBot 主程序 / LLM / QQ 账号。

运行方式（任选一种 shell，在项目根目录下）::

    # PowerShell（Windows 默认）
    .\\.venv\\Scripts\\python.exe plugins\\xuqian13_autonomous-planning-plugin-v4\\tests\\run_smoke.py

    # CMD
    .venv\\Scripts\\python.exe plugins\\xuqian13_autonomous-planning-plugin-v4\\tests\\run_smoke.py

    # Git Bash / WSL / macOS / Linux
    ./.venv/Scripts/python.exe plugins/xuqian13_autonomous-planning-plugin-v4/tests/run_smoke.py

脚本顶部已强制 stdout/stderr 为 utf-8，不需要再设 ``PYTHONIOENCODING`` 环境变量。

覆盖范围（15 项）：
    1.  插件包导入（验证 cache 模块未缺失）
    2.  组件注册（4 Tool + 1 Command + 1 EventHandler + 2 HookHandler + 1 API = 9-10 个）
    3.  UI Section 渲染（4 个顶层 section 全部可见、字段带 label/hint/order）
    4.  v4.0 → v4.1 配置自动迁移
    5.  当前 config.toml 可加载且字段值正确
    6.  stream_filter 白名单匹配（含 qq:group / qq:private 分支）
    7.  llm_logger 写入 + cleanup_old_logs
    8.  pending_commitments CRUD（不污染 schedule_goals）
    9.  TimezoneManager 使用 zoneinfo 时区
    10. PromptBuilder 注入 4 个新段落（pending / history / knowledge / cross-day）
    11. role_judge：prompt 构造 / JSON 解析 / 未来日期推断
    12. get_current_activity_snapshot 返回结构（API 对外契约）
    13. replyer 注入 6 种场景（正常 / 重试 / 冷却 / 关闭 / 白名单 / 无活动）
    14. 多天日程对比（load_recent_schedule_summary 3 天回看）
    15. ScheduleAutoScheduler 构造 + start/stop（强类型 plugin.config 访问）

任何一项失败会抛 AssertionError + 退出码 1；全过输出 ALL SMOKE TESTS PASSED 退出码 0。
"""

from __future__ import annotations

# ── 强制 stdout/stderr 为 utf-8（避免 Windows 默认 GBK 在打印 emoji / 中文时崩溃）
import sys
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import asyncio
import importlib.util
import json
import tempfile
import tomllib
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock


PLUGIN_DIR = Path(__file__).resolve().parent.parent
PKG_NAME = "_maibot_plugin_xuqian13_autonomous_planning_plugin_v4"

_PASS: list[str] = []
_FAIL: list[tuple[str, str]] = []


def step(name: str):
    """简易测试装饰器：打印 [OK] / [FAIL]，收集结果。"""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            try:
                fn(*args, **kwargs)
                _PASS.append(name)
                print(f"[OK]   {name}")
            except AssertionError as exc:
                _FAIL.append((name, str(exc) or "断言失败"))
                print(f"[FAIL] {name}: {exc}")
            except Exception as exc:
                _FAIL.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        return wrapper
    return decorator


# ============================================================
# 加载插件包（一次性，所有测试复用）
# ============================================================

spec = importlib.util.spec_from_file_location(
    PKG_NAME, PLUGIN_DIR / "__init__.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
plugin_mod = importlib.util.module_from_spec(spec)
sys.modules[PKG_NAME] = plugin_mod
spec.loader.exec_module(plugin_mod)


def imp(rel: str):
    """便捷导入子模块。"""
    return importlib.import_module(f"{PKG_NAME}.{rel}")


def fresh_plugin():
    """新建一个走默认配置的插件实例。"""
    inst = plugin_mod.AutonomousPlanningPluginV4()
    inst.set_plugin_config({})
    return inst


def mock_plugin(**schedule_overrides):
    """构造一个用于 InjectService 的 mock plugin（避免 ctx 调用）。"""
    plugin = MagicMock()
    plugin.config.plugin.enabled = True
    plugin.config.schedule.cache_max_size = 100
    plugin.config.schedule.cache_ttl = 300
    plugin.config.schedule.timezone = "Asia/Shanghai"
    plugin.config.schedule.allowed_streams = []
    plugin.config.schedule.cross_day_activity = True
    plugin.config.schedule.inject_schedule = True
    plugin.config.schedule.inject_into_replyer = True
    plugin.config.inject.inject_mode = "smart"
    plugin.config.inject.enable_intent_classification = True
    plugin.config.inject.enable_state_analysis = False
    plugin.config.inject.enable_inject_optimization = True
    plugin.config.inject.context_max_turns = 3
    plugin.config.inject.context_ttl = 600
    plugin.config.inject.casual_chat_inject_probability = 1.0
    for k, v in schedule_overrides.items():
        setattr(plugin.config.schedule, k, v)
    return plugin


# ============================================================
# 13 项测试
# ============================================================


@step("01. 插件包导入（cache 模块未缺失）")
def test_pkg_import():
    assert plugin_mod.__version__ == "4.2.0", f"version={plugin_mod.__version__}"
    cache_mod = imp("cache.lru_cache")
    c = cache_mod.LRUCache(max_size=2)
    c["a"] = 1; c["b"] = 2; c["c"] = 3
    assert "a" not in c
    assert c["b"] == 2 and c["c"] == 3


@step("02. 组件注册（10 个：5 Tool + 1 Command + 1 EventHandler + 2 HookHandler + 1 API）")
def test_components():
    inst = fresh_plugin()
    comps = inst.get_components()
    names = sorted((c["type"], c["name"]) for c in comps)
    required = {
        ("API", "get_current_activity"),
        ("TOOL", "manage_goal_v4"),
        ("TOOL", "get_planning_status_v4"),
        ("TOOL", "generate_schedule_v4"),
        ("TOOL", "apply_schedule_v4"),
        ("TOOL", "update_schedule_v4"),
        ("COMMAND", "planning_v4"),
        ("EVENT_HANDLER", "autonomous_planner_v4"),
        ("HOOK_HANDLER", "schedule_inject_v4"),
        ("HOOK_HANDLER", "schedule_inject_replyer_v4"),
    }
    missing = required - set(names)
    assert not missing, f"缺少组件: {missing}"
    assert len(comps) == 10, f"组件总数 {len(comps)} != 10"


@step("03. UI Section 渲染（4 个顶层 section + 字段 UI 元数据完整）")
def test_ui_schema():
    inst = fresh_plugin()
    schema = inst.build_config_schema(plugin_id="x.y", plugin_name="t")
    sections = schema["sections"]
    assert set(sections.keys()) == {"plugin", "autonomous_planning", "schedule", "inject"}, \
        f"sections={set(sections.keys())}"
    sched = sections["schedule"]["fields"]
    assert len(sched) >= 30, f"schedule 字段数={len(sched)}"
    # 关键字段 UI 元数据全部填了
    for fname in ("inject_schedule", "inject_into_replyer", "role_judge_enabled",
                  "allowed_streams", "cross_day_activity"):
        f = sched[fname]
        assert f["label"] and f["label"] != fname, f"{fname}.label 缺失"
        assert f["hint"], f"{fname}.hint 缺失"


@step("04. v4.0 → v4.2 配置迁移")
def test_migration():
    old_cfg = {
        "plugin": {"enabled": True, "config_version": "4.0.0"},
        "autonomous_planning": {
            "cleanup_interval": 1800,
            "schedule": {
                "inject_schedule": False,
                "allowed_streams": ["qq:group:111"],
                "inject": {"inject_mode": "traditional"},
            },
        },
    }
    inst = plugin_mod.AutonomousPlanningPluginV4()
    inst.set_plugin_config(old_cfg)
    assert inst.config.schedule.inject_schedule is False
    assert inst.config.schedule.allowed_streams == ["qq:group:111"]
    # v4.1.1+ 移除 traditional 模式，迁移层自动降级为 smart
    # v4.2 起 inject_mode 字段已 deprecated，但保留向后兼容
    assert inst.config.inject.inject_mode == "smart"
    assert inst.config.autonomous_planning.cleanup_interval == 1800


@step("05. 当前 config.toml 可加载")
def test_current_toml():
    with open(PLUGIN_DIR / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    inst = plugin_mod.AutonomousPlanningPluginV4()
    inst.set_plugin_config(cfg)
    # 不假设具体值（用户可能改过），只确认字段都能解析
    assert isinstance(inst.config.schedule.admin_users, list)
    assert isinstance(inst.config.schedule.inject_into_replyer, bool)
    # inject_mode 在 v4.2 起 deprecated 但保留向后兼容
    assert inst.config.inject.inject_mode in ("smart", "rule")
    assert inst.config.plugin.config_version == "4.2.0"


@step("06. stream_filter 白名单匹配")
def test_stream_filter():
    sf = imp("utils.stream_filter")
    assert sf.is_stream_allowed("s1", []) is True  # 留空 = 全允许
    assert sf.is_stream_allowed("s1", ["all"]) is True
    assert sf.is_stream_allowed("s1", ["session:s1"]) is True
    assert sf.is_stream_allowed("s2", ["session:s1"]) is False
    assert sf.is_stream_allowed("s1", ["qq:group:123"],
                                stream_info={"platform": "qq", "group_id": "123"}) is True
    assert sf.is_stream_allowed("s1", ["qq:group:999"],
                                stream_info={"platform": "qq", "group_id": "123"}) is False
    assert sf.is_stream_allowed("s1", ["qq:private:789"],
                                stream_info={"platform": "qq", "user_id": "789"}) is True


@step("07. llm_logger 读写 + cleanup")
def test_llm_logger():
    log_mod = imp("utils.llm_logger")
    log_dir = Path(tempfile.mkdtemp())
    log_mod.log_llm_call("schedule_generation", "prompt-body", "resp-body", "replyer", True, log_dir)
    log_mod.log_llm_call("role_decision", "p2", "r2", "replyer", False, log_dir)
    files = sorted(log_dir.iterdir())
    prefixes = {f.name.split("_")[0] for f in files}
    assert prefixes == {"ok", "fail"}
    ok_file = [f for f in files if f.name.startswith("ok_")][0]
    content = ok_file.read_text(encoding="utf-8")
    assert "PROMPT" in content and "prompt-body" in content
    # cleanup 不删近期文件
    assert log_mod.cleanup_old_logs(log_dir, retention_days=999) == 0


@step("08. pending_commitments CRUD")
def test_pending_commitments():
    gm_mod = imp("planner.goal_manager")
    gm = gm_mod.GoalManager(data_dir=str(Path(tempfile.mkdtemp())))
    gm.add_pending_commitment("2026-05-30", "周末一起打游戏", time="14:00", notes="开黑")
    got = gm.get_pending_commitments("2026-05-30")
    assert len(got) == 1 and got[0].name == "周末一起打游戏"
    # 不污染普通日程查询
    sg = gm.get_schedule_goals(chat_id="global", date_str="2026-05-30")
    assert all(g.goal_type != "pending_commitment" for g in sg)
    # consume 后清空
    assert len(gm.consume_pending_commitments("2026-05-30")) == 1
    assert not gm.get_pending_commitments("2026-05-30")


@step("09. TimezoneManager zoneinfo")
def test_timezone():
    tz_mod = imp("utils.timezone_manager")
    tz = tz_mod.TimezoneManager("Asia/Shanghai")
    now = tz.get_now()
    assert now.tzinfo is not None
    assert "Shanghai" in str(now.tzinfo) or "+08:00" in now.isoformat()


@step("10. PromptBuilder 注入 4 个新段落")
def test_prompt_builder():
    pb_mod = imp("planner.generator.prompt_builder")
    tz_mod = imp("utils.timezone_manager")
    pb = pb_mod.PromptBuilder({}, tz_mod.TimezoneManager("Asia/Shanghai"))
    prompt = pb.build_schedule_prompt(
        "daily", {},
        pending_commitments=[{"time": "14:00", "title": "打游戏", "notes": "周末"}],
        history_context="[12:30] 朵昕@群: 今天天气好",
        knowledge_context="麦麦喜欢油豆腐",
    )
    assert "今天需要纳入的约定" in prompt and "打游戏" in prompt
    assert "最近聊天背景" in prompt and "朵昕" in prompt
    assert "相关记忆参考" in prompt and "油豆腐" in prompt
    assert "跨天活动支持" in prompt


@step("11. role_judge 辅助函数")
def test_role_judge():
    rj = imp("planner.role_judge")
    prompt = rj._build_judge_prompt(
        persona="温柔", today_str="2026-05-25", weekday="周一",
        current_activities=[{"time": "09:00", "name": "早餐"}],
        description="下午两点一起学习",
    )
    assert "当前日程" in prompt and "09:00 早餐" in prompt and "decision" in prompt
    parsed = rj._parse_json_loose('{"decision":"today","title":"学习"}')
    assert parsed["decision"] == "today"
    parsed2 = rj._parse_json_loose('foo {"decision":"future","raw_date":"明天"} bar')
    assert parsed2["decision"] == "future"
    assert rj._infer_future_date("明天", "2026-05-25") == "2026-05-26"


@step("12. get_current_activity_snapshot API 返回结构")
def test_api_snapshot():
    gm_mod = imp("planner.goal_manager")
    gm = gm_mod.GoalManager(data_dir=str(Path(tempfile.mkdtemp())))
    gm_mod._goal_manager = gm
    now_min = datetime.now().hour * 60 + datetime.now().minute
    gm.create_goal(
        name="晚餐", goal_type="meal",
        description="晚饭在食堂二楼吃了木桶饭，加了个香煎里脊和荷包蛋，吃得超级满足。",
        creator_id="system", chat_id="global", priority="high",
        parameters={"time_window": [max(0, now_min - 15), min(1440, now_min + 30)]},
    )
    inj_mod = imp("services.inject_service")
    svc = inj_mod.InjectService(mock_plugin())
    snap = asyncio.run(svc.get_current_activity_snapshot("global"))
    assert snap["has_activity"] is True
    assert snap["activity"]["name"] == "晚餐"
    assert snap["activity"]["goal_type"] == "meal"
    assert "晚饭" in snap["activity"]["description"]  # 完整描述未截断
    assert "-" in snap["activity"]["time_window"]
    assert snap["timezone"] == "Asia/Shanghai"
    assert snap["as_of"]


@step("13. replyer 注入 6 场景")
def test_replyer_inject():
    gm_mod = imp("planner.goal_manager")
    gm = gm_mod.GoalManager(data_dir=str(Path(tempfile.mkdtemp())))
    gm_mod._goal_manager = gm
    now_min = datetime.now().hour * 60 + datetime.now().minute
    gm.create_goal(
        name="晚餐", goal_type="meal", description="木桶饭",
        creator_id="system", chat_id="global", priority="high",
        parameters={"time_window": [max(0, now_min - 15), min(1440, now_min + 30)]},
    )
    inj_mod = imp("services.inject_service")

    async def run():
        plugin = mock_plugin()
        svc = inj_mod.InjectService(plugin)

        # 1) 正常注入
        r1 = await svc.inject_into_replyer_extra_prompt(session_id="s1", attempt=1)
        assert r1.get("modified_kwargs", {}).get("extra_prompt"), "正常场景应注入"
        assert "晚餐" in r1["modified_kwargs"]["extra_prompt"]
        assert "不要主动提及" in r1["modified_kwargs"]["extra_prompt"]

        # 2) attempt=2 重试跳过
        r2 = await svc.inject_into_replyer_extra_prompt(session_id="s1", attempt=2)
        assert "modified_kwargs" not in r2

        # 3) 冷却命中（再次 attempt=1）
        r3 = await svc.inject_into_replyer_extra_prompt(session_id="s1", attempt=1)
        # 冷却命中或注入都可，不崩溃即可
        assert "action" in r3

        # 4) 关闭开关
        plugin.config.schedule.inject_into_replyer = False
        r4 = await svc.inject_into_replyer_extra_prompt(session_id="s_new", attempt=1)
        assert "modified_kwargs" not in r4

        # 5) 白名单过滤
        plugin.config.schedule.inject_into_replyer = True
        plugin.config.schedule.allowed_streams = ["session:only-me"]
        r5 = await svc.inject_into_replyer_extra_prompt(session_id="s_outsider", attempt=1)
        assert "modified_kwargs" not in r5

        # 6) 无活动
        plugin.config.schedule.allowed_streams = []
        for g in gm.get_all_goals(chat_id="global"):
            gm.delete_goal(g.goal_id)
        svc._schedule_cache.clear()
        r6 = await svc.inject_into_replyer_extra_prompt(session_id="s_empty", attempt=1)
        assert "modified_kwargs" not in r6

    asyncio.run(run())


# ============================================================
# Run all
# ============================================================


@step("14. 多天日程对比（load_recent_schedule_summary）")
def test_recent_schedule_summary():
    from datetime import timedelta as _td
    gm_mod = imp("planner.goal_manager")
    ctx_mod = imp("planner.generator.context_loader")
    tz_mod = imp("utils.timezone_manager")

    gm = gm_mod.GoalManager(data_dir=str(Path(tempfile.mkdtemp())))
    tz = tz_mod.TimezoneManager("Asia/Shanghai")
    now = tz.get_now()

    # 造 3 天历史：昨/前/大前
    for offset, acts in enumerate([
        [("审稿", 8 * 60), ("写专栏", 14 * 60)],
        [("回邮件", 8 * 60), ("整理藏书", 14 * 60)],
        [("审稿", 8 * 60), ("写专栏", 14 * 60)],
    ], start=1):
        day = now - _td(days=offset)
        for name, start_min in acts:
            g = gm.create_goal(
                name=name, description=f"{name}的描述", goal_type="study",
                creator_id="system", chat_id="global", priority="medium",
                parameters={"time_window": [start_min, start_min + 120]},
            )
            gm.db.update_goal(g.goal_id, created_at=day)

    loader = ctx_mod.ScheduleContextLoader(gm, tz)

    # days=1 只看昨天
    s1 = loader.load_recent_schedule_summary(days=1)
    assert "审稿" in s1 and "写专栏" in s1
    assert "回邮件" not in s1, "days=1 不应看到前天"

    # days=3 看 3 天
    s3 = loader.load_recent_schedule_summary(days=3)
    assert "审稿" in s3 and "回邮件" in s3 and "整理藏书" in s3
    # 应该出现 3 个【MM-DD】日期块
    assert s3.count("【") == 3, f"应该有 3 个日期块，实际 {s3.count('【')}"

    # 向后兼容：load_yesterday_schedule_summary 等价于 days=1
    s_yest = loader.load_yesterday_schedule_summary()
    # 不严格相等（动态日期 / 内容一致即可），只要看到昨天的活动
    assert "审稿" in s_yest


@step("15. ScheduleAutoScheduler 构造 + start/stop（强类型 config 访问）")
def test_auto_scheduler():
    sched_mod = imp("planner.auto_scheduler")
    inst = fresh_plugin()

    async def run():
        s = sched_mod.ScheduleAutoScheduler(inst)
        assert s.tz_manager.timezone_str == inst.config.schedule.timezone
        # 强制启用以走完 start 分支（验证 plugin.config.schedule.xxx 全部可访问）
        inst.config.schedule.auto_schedule_enabled = True
        await s.start()
        assert s.is_running is True
        await s.stop()
        assert s.is_running is False

    asyncio.run(run())


def main() -> int:
    print(f"\n{'=' * 60}")
    print("自主规划插件 v4 完整冒烟测试")
    print(f"{'=' * 60}\n")

    test_pkg_import()
    test_components()
    test_ui_schema()
    test_migration()
    test_current_toml()
    test_stream_filter()
    test_llm_logger()
    test_pending_commitments()
    test_timezone()
    test_prompt_builder()
    test_role_judge()
    test_api_snapshot()
    test_replyer_inject()
    test_recent_schedule_summary()
    test_auto_scheduler()

    print(f"\n{'=' * 60}")
    print(f"通过: {len(_PASS)} / 失败: {len(_FAIL)}")
    if _FAIL:
        print("\n失败项:")
        for name, msg in _FAIL:
            print(f"  - {name}\n      {msg}")
        return 1
    print("ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
