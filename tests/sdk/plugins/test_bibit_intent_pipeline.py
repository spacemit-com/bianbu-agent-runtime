from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest


EXTERNAL_SRC = Path(os.environ.get("BIBIT_INTENT_SRC", "/opt/bibit-intent/src"))
RESOURCES = EXTERNAL_SRC / "bibit_intent" / "resources"
TOOLS_CONFIG = RESOURCES / "tools.yaml"
if str(EXTERNAL_SRC) not in sys.path:
    sys.path.insert(0, str(EXTERNAL_SRC))


def test_external_edge_modules_import_without_top_level_agent_or_tools_packages():
    from bibit_intent.execution.cancellation import run_command
    from bibit_intent.routing.embedding import EmbeddingClient
    from bibit_intent.execution.executor import Executor
    from bibit_intent.routing.intent import IntentAction
    from bibit_intent.knowledge.knowledge import BM25KnowledgeBase
    from bibit_intent.models.local_model import LocalModelClient
    from bibit_intent.core.session import SessionState
    from bibit_intent.execution.skill_loader import load_skills
    from bibit_intent.execution.system_tools import HANDLERS

    assert EmbeddingClient
    assert run_command
    assert Executor
    assert IntentAction
    assert BM25KnowledgeBase
    assert LocalModelClient
    assert SessionState
    assert load_skills
    assert "set_volume" in HANDLERS


def test_external_builtin_registry_and_deterministic_slot_maps_are_available():
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.rule_engine import RuleEngine

    registry = ToolRegistry(
        TOOLS_CONFIG
    )
    assert len(registry.all()) == 31
    assert registry.get("set_resolution") is not None
    assert registry.get("set_display_mode") is not None
    assert registry.get("window_list") is not None
    assert registry.get("window_activate") is not None
    assert registry.get("window_minimize") is not None
    assert registry.get("window_maximize") is not None

    rules = RuleEngine(registry)
    louder = rules.match("声音开大一点")
    unmute = rules.match("取消静音")
    assert louder is not None
    assert louder.tool_name == "set_volume"
    assert louder.slots == {"level": 80}
    assert unmute is not None
    assert unmute.tool_name == "mute"
    assert unmute.slots == {"on": False}


def test_external_chinese_numbers_and_new_deterministic_routes_are_available():
    from bibit_intent.core.cn_number import extract_cn_numbers, parse_cn_seq
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.rule_engine import RuleEngine

    assert parse_cn_seq("五十") == 50
    assert extract_cn_numbers("音量六成") == [60]

    registry = ToolRegistry(
        TOOLS_CONFIG
    )
    rules = RuleEngine(registry)
    volume = rules.match("音量调到百分之六十")
    cpu = rules.match("现在CPU占用高吗")

    assert volume is not None
    assert volume.tool_name == "set_volume"
    assert volume.slots == {"level": 60}
    assert cpu is not None
    assert cpu.tool_name == "get_system_info"


def test_question_detector_supports_deterministic_semantic_and_near_miss_layers():
    from bibit_intent.routing.question_detector import QuestionDetector

    assert QuestionDetector().detect("什么是黑洞").layer == "strong"
    assert QuestionDetector().detect("怎么打开终端呢").layer == "feature"
    assert not QuestionDetector().is_open_question("为什么没声音")

    class Retriever:
        def __init__(self, score):
            self.score = score
            self.calls = []

        def best_match(self, text):
            self.calls.append(text)
            return ("QUESTION", self.score)

    semantic = Retriever(0.91)
    assert QuestionDetector(semantic).detect("亮度如何调节").layer == "semantic"
    assert semantic.calls == ["亮度如何调节"]

    near_miss = Retriever(0.85)
    assert QuestionDetector(near_miss).is_near_miss("亮度如何调节")
    assert near_miss.calls == ["亮度如何调节"]

    gated = Retriever(0.99)
    assert not QuestionDetector(gated).is_open_question("把声音开大一点")
    assert gated.calls == []


def test_intent_escalates_writing_web_reports_and_unsupported_audio_switching():
    from bibit_intent.routing.intent import IntentAction, decide_local_intent
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.rule_engine import RuleEngine

    registry = ToolRegistry(TOOLS_CONFIG)
    rules = RuleEngine(registry)

    writing = decide_local_intent("帮我写一首诗", rules, registry)
    web_report = decide_local_intent(
        "搜索AI资讯并生成可视化网页报告", rules, registry
    )
    audio = decide_local_intent("把声音输出切换到蓝牙耳机", rules, registry)

    assert writing.action is IntentAction.ESCALATE
    assert writing.reason == "writing_task"
    assert web_report.action is IntentAction.ESCALATE
    assert web_report.reason == "writing_task"
    assert audio.action is IntentAction.ESCALATE
    assert audio.reason == "unsupported_control"


def test_local_model_protocol_parses_call_and_escalate():
    from bibit_intent.models.local_model import (
        _GBNF_GRAMMAR,
        _build_system_prompt,
        _parse_output,
    )

    call = _parse_output("CALL vol level=60", 12.5)
    escalate = _parse_output("ESCALATE reason=complex", 8.0)

    assert call.is_call
    assert call.tool_name == "vol"
    assert call.params == {"level": 60}
    assert not escalate.is_call
    assert escalate.reason == "complex"
    assert 'kv-pair){0,2}' in _GBNF_GRAMMAR
    assert "禁止猜值" in _build_system_prompt("vol(level:0-100)")


def test_parameter_evidence_keeps_explicit_values_and_suspends_guesses():
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.router import _keep_evidenced_params

    registry = ToolRegistry(TOOLS_CONFIG)
    tool = registry.get("set_volume")

    assert _keep_evidenced_params("音量调到６０", {"level": 60}, tool).kept == {
        "level": 60
    }
    assert _keep_evidenced_params("音量六成", {"level": 60}, tool).kept == {
        "level": 60
    }
    guessed = _keep_evidenced_params(
        "帮我设置音量", {"level": 50, "bogus": "x"}, tool
    )
    assert guessed.kept == {}
    assert guessed.uncertain == {"level": 50}
    assert guessed.dropped == ["bogus"]


def test_router_requires_confirmation_for_guessed_values_and_handles_cancel_locally():
    from bibit_intent.execution.executor import Executor
    from bibit_intent.models.local_model import LocalModelResult
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.router import Router, Stage
    from bibit_intent.routing.rule_engine import RuleEngine
    from bibit_intent.core.session import SessionState
    from bibit_intent.execution.system_tools import ExecResult

    class Local:
        def is_available(self):
            return True

        def infer(self, _text, _tool_list, analysis=None):
            return LocalModelResult(
                action="CALL",
                tool_name="set_volume",
                params={"level": 50},
                raw="CALL set_volume level=50",
            )

    called = []

    def set_volume(level, **_):
        called.append(level)
        return ExecResult(True, f"volume={level}")

    registry = ToolRegistry(TOOLS_CONFIG)
    router = Router(
        registry,
        RuleEngine(registry),
        Local(),
        Executor(registry, handlers={"set_volume": set_volume}),
    )

    session = SessionState()
    first = router.handle("帮我设置音量", session=session)
    assert first.stage is Stage.CONFIRM
    assert called == []
    assert session.pending is not None
    confirmed = router.handle("确认", session=session)
    assert confirmed.stage is Stage.CONFIRM
    assert called == [50]

    session = SessionState()
    router.handle("帮我设置音量", session=session)
    replaced = router.handle("60", session=session)
    assert replaced.stage is Stage.CONFIRM
    assert called == [50, 60]

    session = SessionState()
    router.handle("帮我设置音量", session=session)
    cancelled = router.handle("取消", session=session)
    assert cancelled.stage is Stage.CONFIRM
    assert cancelled.escalate_reason == "cancelled"
    assert called == [50, 60]
    assert session.pending is None


def test_local_model_escalates_over_budget_input_before_completion_request():
    from bibit_intent.models.local_model import LocalModelClient

    class NoRequestClient(LocalModelClient):
        def _post(self, _path, _payload):
            raise AssertionError("overflow must not be sent to llama-server")

    client = NoRequestClient(context_limit=512)
    result = client.infer("很长的用户输入" * 500, "vol(level:0-100)")

    assert result.is_escalate
    assert result.reason == "context_overflow"


def test_local_model_escalates_when_fixed_prompt_exceeds_context_limit():
    from bibit_intent.models.local_model import LocalModelClient

    class NoRequestClient(LocalModelClient):
        def _post(self, _path, _payload):
            raise AssertionError("overflow must not be sent to llama-server")

    result = NoRequestClient(context_limit=64).infer("调音量", "vol(level:0-100)")

    assert result.is_escalate
    assert result.reason == "context_overflow"


def test_external_skills_knowledge_and_route_anchors_are_available():
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.execution.skill_loader import load_skills
    from bibit_intent.execution.system_tools import HANDLERS

    registry = ToolRegistry(TOOLS_CONFIG)
    handlers = dict(HANDLERS)
    resources = RESOURCES
    loaded = load_skills(resources / "skills", registry, handlers)

    assert {skill.name for skill in loaded} == {
        "bluetooth",
        "ubuntu_admin",
        "ubuntu_cmd",
        "wifi",
    }
    assert len(registry.all()) == 131
    assert registry.get("usb_tree") is not None
    assert registry.get("usb_detail") is not None
    assert registry.get("usb_reset") is not None
    assert registry.get("wifi_scan") is not None
    assert "wifi_scan" in handlers
    assert (resources / "kb" / "wifi.md").is_file()
    assert (resources / "route-data" / "system_control.jsonl").is_file()
    assert (resources / "route-data" / "ubuntu_cmd.jsonl").is_file()
    assert (resources / "question_corpus.jsonl").is_file()


def test_embedding_index_warmup_never_blocks_request_routing(tmp_path):
    from bibit_intent.routing.embedding import EmbeddingRetriever

    started = threading.Event()
    release = threading.Event()

    class SlowUnavailableClient:
        def is_available(self):
            started.set()
            release.wait(timeout=1)
            return False

        def embed(self, _text):
            raise AssertionError("unavailable client must not embed")

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text("", encoding="utf-8")
    retriever = EmbeddingRetriever(SlowUnavailableClient(), corpus)
    retriever.start_background_build()
    assert started.wait(timeout=1)

    before = time.monotonic()
    try:
        result = retriever.route("打开无线网络")
    finally:
        release.set()

    assert time.monotonic() - before < 0.1
    assert result.ranked == []
    assert result.best is None


def test_cancellable_command_terminates_process_promptly():
    from bibit_intent.execution.cancellation import (
        EdgeExecutionCancelled,
        cancellation_scope,
        run_command,
    )

    cancel_event = threading.Event()
    timer = threading.Timer(0.05, cancel_event.set)
    timer.start()
    before = time.monotonic()
    try:
        with cancellation_scope(cancel_event):
            with pytest.raises(EdgeExecutionCancelled):
                run_command(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    timeout=10,
                )
    finally:
        timer.cancel()

    assert time.monotonic() - before < 1


def test_executor_refuses_to_start_without_full_execution_budget():
    from bibit_intent.execution.cancellation import (
        EdgeDeadlineExceeded,
        EdgeExecutionControl,
        cancellation_scope,
    )
    from bibit_intent.execution.executor import Executor
    from bibit_intent.core.registry import ToolRegistry

    called = []
    registry = ToolRegistry(TOOLS_CONFIG)
    executor = Executor(
        registry,
        handlers={"set_volume": lambda **_: called.append(True)},
    )
    control = EdgeExecutionControl(deadline=time.monotonic() + 1)

    with cancellation_scope(control):
        with pytest.raises(EdgeDeadlineExceeded):
            executor.execute("set_volume", {"level": 50})

    assert called == []
    assert not control.execution_started.is_set()


def test_read_only_long_running_tools_do_not_mark_device_side_effects():
    from bibit_intent.execution.cancellation import EdgeExecutionControl, cancellation_scope
    from bibit_intent.execution.executor import Executor
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.execution.skill_loader import load_skills

    resources = RESOURCES
    registry = ToolRegistry(TOOLS_CONFIG)
    handlers = {}
    load_skills(resources / "skills", registry, handlers)
    handlers["sec_antivirus"] = lambda **_: type(
        "Result", (), {"ok": True, "message": "clean", "detail": ""}
    )()
    executor = Executor(registry, handlers=handlers)
    control = EdgeExecutionControl(deadline=time.monotonic() + 1)

    with cancellation_scope(control):
        result = executor.execute("sec_antivirus", {"path": "/tmp"})

    assert result.ok
    assert not control.execution_started.is_set()


class _QueueRouter:
    def __init__(self, *results):
        self.results = list(results)

    def handle(self, _text, session=None):
        assert session is not None
        return self.results.pop(0)


def test_pipeline_handles_edge_results_and_falls_back_only_for_cloud_stage():
    from bibit_intent.pipeline import EmbeddedBibitPipeline
    from bibit_intent.routing.router import RouteResult, Stage
    from bibit_intent.execution.system_tools import ExecResult

    router = _QueueRouter(
        RouteResult(
            stage=Stage.RULE,
            tool_name="set_volume",
            params={"level": 60},
            exec_result=ExecResult(True, "音量已设置为 60%", "Master"),
        ),
        RouteResult(
            stage=Stage.LOCAL,
            tool_name="wifi_scan",
            params={},
            exec_result=ExecResult(False, "扫描失败", "nmcli unavailable"),
        ),
        RouteResult(
            stage=Stage.CLOUD,
            tool_name=None,
            params={},
            exec_result=None,
            escalate_reason="complex",
        ),
    )
    pipeline = EmbeddedBibitPipeline(router=router)

    rule = pipeline.handle("音量调到60", "session")
    local_failure = pipeline.handle("扫描无线网络", "session")
    cloud = pipeline.handle("写一首诗", "session")

    assert rule.handled
    assert rule.reply == "音量已设置为 60%\nMaster"
    assert rule.route == "rule"
    assert local_failure.handled
    assert local_failure.reply == "扫描失败\nnmcli unavailable"
    assert not cloud.handled
    assert cloud.reply == ""
    assert cloud.reason == "complex"


def test_pipeline_session_state_is_isolated_and_lru_bounded():
    from bibit_intent.pipeline import EmbeddedBibitPipeline
    from bibit_intent.routing.router import RouteResult, Stage
    from bibit_intent.execution.system_tools import ExecResult

    class PendingRouter:
        def __init__(self):
            self.sessions = []

        def handle(self, _text, session=None):
            self.sessions.append(session)
            session.set_pending("pkg_install", "pkg", "软件包名", {}, param_type="str")
            return RouteResult(
                stage=Stage.LOCAL,
                tool_name="pkg_install",
                params={},
                exec_result=ExecResult(False, "还差软件包名", need_param="pkg"),
            )

    router = PendingRouter()
    pipeline = EmbeddedBibitPipeline(router=router, max_sessions=2)
    pipeline.handle("first", "s1")
    pipeline.handle("second", "s2")
    first_s1 = router.sessions[0]

    assert router.sessions[0] is not router.sessions[1]
    pipeline.handle("third", "s3")
    assert pipeline.active_session_count == 2
    pipeline.handle("first-again", "s1")
    assert router.sessions[-1] is not first_s1
    assert pipeline.active_session_count == 2


def test_pipeline_serializes_concurrent_requests_for_the_same_session():
    from concurrent.futures import ThreadPoolExecutor
    from bibit_intent.pipeline import EmbeddedBibitPipeline
    from bibit_intent.routing.router import RouteResult, Stage
    from bibit_intent.execution.system_tools import ExecResult

    class ConcurrentRouter:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def handle(self, _text, session=None):
            assert session is not None
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.05)
            with self.lock:
                self.active -= 1
            return RouteResult(
                stage=Stage.LOCAL,
                tool_name="noop",
                params={},
                exec_result=ExecResult(True, "完成"),
            )

    router = ConcurrentRouter()
    pipeline = EmbeddedBibitPipeline(router=router)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(lambda text: pipeline.handle(text, "same-session"), ("a", "b"))
        )

    assert all(outcome.handled for outcome in outcomes)
    assert router.max_active == 1
    assert pipeline.active_session_count == 0


def test_pipeline_cancels_while_waiting_for_same_session_lock():
    from concurrent.futures import ThreadPoolExecutor
    from bibit_intent.execution.cancellation import EdgeExecutionCancelled
    from bibit_intent.pipeline import EmbeddedBibitPipeline
    from bibit_intent.routing.router import RouteResult, Stage
    from bibit_intent.execution.system_tools import ExecResult

    started = threading.Event()
    release = threading.Event()

    class BlockingRouter:
        def handle(self, text, session=None):
            if text == "first":
                started.set()
                assert release.wait(timeout=2)
            return RouteResult(
                stage=Stage.LOCAL,
                tool_name="noop",
                params={},
                exec_result=ExecResult(True, "完成"),
            )

    pipeline = EmbeddedBibitPipeline(router=BlockingRouter())
    cancel_event = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pipeline.handle, "first", "same")
        assert started.wait(timeout=1)
        second = pool.submit(
            pipeline.handle,
            "second",
            "same",
            cancel_event,
        )
        time.sleep(0.06)
        before = time.monotonic()
        cancel_event.set()
        with pytest.raises(EdgeExecutionCancelled):
            second.result(timeout=0.5)
        assert time.monotonic() - before < 0.3
        release.set()
        assert first.result(timeout=1).handled


def test_pipeline_never_exceeds_max_sessions_under_distinct_session_concurrency():
    from concurrent.futures import ThreadPoolExecutor
    from bibit_intent.pipeline import EmbeddedBibitPipeline
    from bibit_intent.routing.router import RouteResult, Stage
    from bibit_intent.execution.system_tools import ExecResult

    class CountingRouter:
        def __init__(self):
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def handle(self, _text, session=None):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.04)
            session.set_pending("noop", "value", "值", {})
            with self.lock:
                self.active -= 1
            return RouteResult(
                stage=Stage.LOCAL,
                tool_name="noop",
                params={},
                exec_result=ExecResult(False, "缺少值", need_param="value"),
            )

    router = CountingRouter()
    pipeline = EmbeddedBibitPipeline(router=router, max_sessions=2)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda index: pipeline.handle("x", f"s{index}"), range(6)))

    assert router.max_active <= 2
    assert pipeline.active_session_count == 2


def test_pipeline_rejects_empty_session_id():
    from bibit_intent.pipeline import EmbeddedBibitPipeline

    with pytest.raises(ValueError, match="session_id"):
        EmbeddedBibitPipeline(router=_QueueRouter()).handle("hello", "")


def test_production_pipeline_builds_from_plugin_resources_and_environment(tmp_path):
    from bibit_intent.pipeline import EmbeddedBibitPipeline, PipelineSettings

    settings = PipelineSettings.from_environment(
        {
            "BIBIT_INTENT_LLAMA_URL": "http://edge.test:18081",
            "BIBIT_INTENT_EMBED_URL": "http://edge.test:18082",
            "BIBIT_INTENT_CONTEXT_LIMIT": "640",
            "BIBIT_INTENT_LOCAL_TIMEOUT_SECONDS": "17",
            "BIBIT_INTENT_MAX_SESSIONS": "3",
        }
    )
    assert settings.llama_url == "http://edge.test:18081"
    assert settings.embed_url == "http://edge.test:18082"
    assert settings.context_limit == 640
    assert settings.local_timeout_seconds == 17
    assert settings.max_sessions == 3

    pipeline = EmbeddedBibitPipeline.from_package(
        data_root=tmp_path / "plugin-data",
        settings=settings,
    )
    assert pipeline.registered_tool_count == 131
    assert pipeline.embedding_cache_path == tmp_path / "plugin-data" / "embedding-index.json"
    assert pipeline.question_embedding_cache_path == (
        tmp_path / "plugin-data" / "question-embedding-index.json"
    )
    outcome = pipeline.handle("什么是黑洞", "session")
    assert not outcome.handled
    assert outcome.route == "cloud"
    assert outcome.reason == "open_question"

    web_report = pipeline.handle(
        "搜索AI资讯并生成可视化网页报告", "report-session"
    )
    assert not web_report.handled
    assert web_report.reply == ""
    assert web_report.route == "cloud"
    assert web_report.reason == "writing_task"


def test_pipeline_settings_have_deployment_service_defaults():
    from bibit_intent.pipeline import PipelineSettings

    settings = PipelineSettings.from_environment({})
    assert settings == PipelineSettings(
        llama_url="http://127.0.0.1:8081",
        embed_url="http://127.0.0.1:8082",
        context_limit=512,
        local_timeout_seconds=20,
        max_sessions=256,
    )


def test_real_router_uses_local_model_for_ambiguous_control_and_executes_call():
    from bibit_intent.execution.executor import Executor
    from bibit_intent.models.local_model import LocalModelResult
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.router import Router, Stage
    from bibit_intent.routing.rule_engine import RuleEngine
    from bibit_intent.execution.system_tools import ExecResult

    class Local:
        def __init__(self):
            self.tool_list = ""

        def is_available(self):
            return True

        def infer(self, _text, tool_list, analysis=None):
            self.tool_list = tool_list
            return LocalModelResult(
                action="CALL",
                tool_name="vol",
                params={"level": 65},
                raw="CALL vol level=65",
            )

    class UnavailableEmbedding:
        def route(self, _text):
            raise OSError("embedding service unavailable")

    called = []

    def set_volume(level, **_):
        called.append(level)
        return ExecResult(True, f"音量已设置为 {level}%")

    registry = ToolRegistry(TOOLS_CONFIG)
    local = Local()
    router = Router(
        registry,
        RuleEngine(registry),
        local,
        Executor(registry, handlers={"set_volume": set_volume}),
        embed=UnavailableEmbedding(),
    )

    result = router.handle("声音帮我调整一下，目标数值是 65")
    assert result.stage is Stage.LOCAL
    assert result.tool_name == "set_volume"
    assert called == [65]
    assert "vol(level:0-100)" in local.tool_list


def test_real_router_answers_query_knowledge_from_external_local_kb():
    from bibit_intent.routing.embedding import RouteResult as EmbeddingRouteResult
    from bibit_intent.execution.executor import Executor
    from bibit_intent.knowledge.knowledge import BM25KnowledgeBase, Doc
    from bibit_intent.models.local_model import LocalModelResult
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.router import Router, Stage
    from bibit_intent.routing.rule_engine import RuleEngine

    class Local:
        def is_available(self):
            return True

        def infer(self, _text, _tool_list, analysis=None):
            return LocalModelResult(
                action="CALL",
                tool_name="query_knowledge",
                params={"query": "wifi 连接故障"},
                raw="CALL query_knowledge query=wifi连接故障",
            )

    class Embed:
        def route(self, _text):
            return EmbeddingRouteResult(
                ranked=["query_knowledge"],
                best=("query_knowledge", 0.95),
            )

    registry = ToolRegistry(TOOLS_CONFIG)
    kb = BM25KnowledgeBase()
    kb.build([Doc("wifi", "wifi", "连接失败时先检查 NetworkManager 服务。")])
    router = Router(
        registry,
        RuleEngine(registry),
        Local(),
        Executor(registry),
        kb=kb,
        embed=Embed(),
    )

    # Keep this fixture single-intent: the upgraded canonical router treats
    # multiple action-like verbs (e.g. “查询” + “连接”) as multi_intent.
    result = router.handle("查询知识库 wifi")

    assert result.stage is Stage.LOCAL
    assert result.exec_result is not None
    assert result.exec_result.ok
    assert "NetworkManager" in result.exec_result.message


@pytest.mark.parametrize("reason", ["parse_failed", "out_of_scope"])
def test_real_router_falls_back_when_local_model_escalates(reason):
    from bibit_intent.execution.executor import Executor
    from bibit_intent.models.local_model import LocalModelResult
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.router import Router, Stage
    from bibit_intent.routing.rule_engine import RuleEngine

    class Local:
        def is_available(self):
            return True

        def infer(self, _text, _tool_list, analysis=None):
            return LocalModelResult(action="ESCALATE", reason=reason, raw="invalid")

    registry = ToolRegistry(TOOLS_CONFIG)
    router = Router(
        registry,
        RuleEngine(registry),
        Local(),
        Executor(registry, handlers={}),
    )
    result = router.handle("给我讲个笑话")
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == reason


def test_real_router_falls_back_when_local_server_is_unavailable():
    from bibit_intent.execution.executor import Executor
    from bibit_intent.core.registry import ToolRegistry
    from bibit_intent.routing.router import Router, Stage
    from bibit_intent.routing.rule_engine import RuleEngine

    class UnavailableLocal:
        def is_available(self):
            return False

    registry = ToolRegistry(TOOLS_CONFIG)
    router = Router(
        registry,
        RuleEngine(registry),
        UnavailableLocal(),
        Executor(registry, handlers={}),
    )
    result = router.handle("给我讲个笑话")
    assert result.stage is Stage.CLOUD
    assert result.escalate_reason == "local_unavailable"
