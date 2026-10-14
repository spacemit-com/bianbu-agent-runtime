"""Runtime DLP enforcement errors shared by data boundaries."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class DLPDenied(PermissionError):
    """A data policy denial always terminates the run, regardless of tool risk."""

    should_terminate_run = True

    def __init__(self, reason: str, *, stage: str, tool: str = '',
                 degraded: bool = False) -> None:
        self.reason = reason
        self.stage = stage
        self.tool = tool
        self.degraded = degraded
        super().__init__(f'[bianbu-security] {reason}')

    def to_metadata(self) -> dict:
        return {'reason': self.reason, 'stage': self.stage, 'tool': self.tool,
                'decision': 'BLOCK', 'terminate_run': True,
                'policy_type': 'dlp', 'degraded': self.degraded}


def current_agent_id() -> str:
    import os
    from langgraph.config import get_config
    try:
        config = get_config().get('configurable', {})
    except RuntimeError:
        config = {}
    return str(config.get('agent_id') or os.environ.get('COM_BIANBU_AGENT_ID')
               or os.environ.get('BIANBU_WORKER_AGENT_ID')
               or os.environ.get('BIANBU_AGENT_ID') or 'com.bianbu.agent')


def dlp_checks_disabled(*, stage: str, agent_id: str | None = None) -> bool:
    """Read the current trusted mode; YOLO records a bypass without scanning."""
    from bianbu_agent.security.security_middleware import agent_security_mode_is_yolo
    aid = agent_id or current_agent_id()
    if not agent_security_mode_is_yolo(aid):
        return False
    from bianbu_agent.security.security_runtime import security_client_call
    try:
        security_client_call('audit_log', aid, 'dlp_bypass', stage,
                             'mode=yolo;scanned=false')
    except Exception as exc:
        # Audit availability cannot turn an explicitly disabled check into a denial.
        logger.warning('YOLO DLP bypass audit unavailable (%s): %s', stage, type(exc).__name__)
    return True


def scan_text(text: str, *, stage: str = 'pre_model_call') -> str:
    """Consult current policy for each request; never cache historical decisions."""
    if not text:
        return text
    if dlp_checks_disabled(stage=stage):
        return text
    from bianbu_agent.security.security_runtime import security_client_call
    from bianbu_agent.security.security_skill_dlp import _mask_findings
    try:
        result = security_client_call('dlp_scan', current_agent_id(), text)
        findings = result['findings']
        if not isinstance(findings, list):
            raise ValueError('invalid findings')
        if any(f.get('action') not in {'allow', 'ask', 'deny', 'mask'} for f in findings):
            raise ValueError('invalid DLP action')
        # 该边界没有交互确认通道：ask 与 deny 一律按拒绝处理（fail-closed）。
        if any(f['action'] in {'deny', 'ask'} for f in findings):
            raise DLPDenied('模型数据含受限敏感信息，已按 DLP 策略拦截', stage=stage)
        return _mask_findings(text, [f for f in findings if f['action'] == 'mask'])
    except DLPDenied:
        raise
    except Exception as exc:
        raise DLPDenied('模型数据 DLP 扫描失败，已阻止外发',
                        stage=stage, degraded=True) from exc


def _scan_value(value):
    if isinstance(value, str):
        return scan_text(value)
    if isinstance(value, list):
        return [_scan_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _scan_value(v) for k, v in value.items()}
    return value


def scan_message(message):
    """Copy textual payloads, preserving message/call IDs and media blocks."""
    import json
    content = message.content
    if isinstance(content, str):
        content = scan_text(content)
    elif isinstance(content, list):
        content = [scan_text(b) if isinstance(b, str) else
                   {**b, 'text': scan_text(b['text'])} if isinstance(b, dict) and isinstance(b.get('text'), str)
                   else b for b in content]
    updates = {'content': content}
    calls = getattr(message, 'tool_calls', None)
    if calls:
        updates['tool_calls'] = [{**c, 'args': _scan_value(c['args'])} for c in calls]
    extra = dict(message.additional_kwargs)
    if isinstance(extra.get('reasoning_content'), str):
        extra['reasoning_content'] = scan_text(extra['reasoning_content'])
    if extra.get('tool_calls'):
        extra['tool_calls'] = [{**c, 'function': {**c['function'],
            'arguments': json.dumps(_scan_value(json.loads(c['function']['arguments'])), ensure_ascii=False)}}
            for c in extra['tool_calls']]
    updates['additional_kwargs'] = extra
    return message.model_copy(update=updates)


def scan_request(request):
    return request.override(messages=[scan_message(m) for m in request.messages],
                            system_message=scan_message(request.system_message) if request.system_message else None)


def buffered_model_config():
    """Use task-local callback tags, never copy or mutate the model instance."""
    from langgraph.constants import TAG_NOSTREAM
    from langchain_core.runnables.config import ensure_config
    from langchain_core.callbacks import BaseCallbackHandler
    config = ensure_config()
    control = config.get('configurable', {}).get('run_control')

    class Progress(BaseCallbackHandler):
        def on_llm_new_token(self, token, **kwargs):
            if token and control is not None:
                control.model_progress()

    callbacks = config.get('callbacks')
    if callbacks is None:
        callbacks = [Progress()]
    elif isinstance(callbacks, list):
        callbacks = [*callbacks, Progress()]
    else:
        callbacks = callbacks.copy()
        callbacks.add_handler(Progress())
    return {**config, 'tags': [*config.get('tags', []), TAG_NOSTREAM], 'callbacks': callbacks}


def scan_response(response):
    from dataclasses import replace
    # The graph's model-node result publishes only these governed messages;
    # raw provider callbacks are suppressed by TAG_NOSTREAM.
    result = [scan_message(message) for message in response.result]
    return replace(response, result=result,
                   structured_response=_scan_value(response.structured_response))
