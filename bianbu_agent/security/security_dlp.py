"""Runtime DLP enforcement errors shared by data boundaries."""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Legacy Securityd/Runtime handles are still recognised so an in-flight
# Session that started before this change can still restore its tool
# arguments.  New masking never emits them: the model only sees natural
# stand-ins of the same PII shape (see ``securityd.dlp.pii_scanner.pseudonym``).
_LEGACY_REFERENCE = re.compile(r"\[PII-[A-Za-z0-9_.-]+-R[0-9a-f]{12}\]")
_PSEUDONYM_DOMAIN = b"bianbu-dlp-pseudonym-v1"
_PSEUDONYM_SURNAMES = (
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾"
    "肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏"
    "韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴"
    "莫孔向汤"
)
_PSEUDONYM_GIVEN_CHARS = (
    "伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉兰春燕"
    "晨宇涵轩然宁欣怡佳琪悦瑞博文思远嘉禾雨桐若彤知远书言清和"
)
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"
PII_TYPES = frozenset({
    "name", "phone", "id_card", "email",
    "bank_card", "address", "medical", "biometric",
})
# An inline data URL carries bytes, not text: the text scanner cannot inspect
# what it encodes, so it must never be forwarded as if it had been scanned.
_OPAQUE_DATA_URL = re.compile(r"^data:[^;,\s]*;base64,", re.IGNORECASE)
# Content blocks whose payload is not text.  The Runtime DLP service only
# scans text, so these blocks cannot cross a model boundary without an
# explicit local-model exception at the caller.
_MEDIA_BLOCK_TYPES = frozenset({
    'image', 'image_url', 'input_image', 'audio', 'input_audio',
    'file', 'document', 'video',
})


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


def _digest_int(parts: tuple[str, ...], counter: int) -> int:
    import hashlib
    payload = b"\x00".join(
        [_PSEUDONYM_DOMAIN, *[part.encode("utf-8") for part in parts],
         str(counter).encode("ascii")])
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def _luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if len(digits) < 12:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def natural_stand_in(pii_type: str, value: str, counter: int = 0) -> str:
    """Deterministic, format-valid stand-in shared with securityd.

    The Runtime keeps its own copy of this tiny generator because a Worker may
    not have securityd's Python package installed.  Both sides hash the same
    domain, type, value and counter, so a value gets the same stand-in no
    matter which process masked it first.
    """
    kind = str(pii_type or "pii").strip().lower()
    if kind == "name":
        digest = _digest_int(("name", value), counter)
        surname = _PSEUDONYM_SURNAMES[digest % len(_PSEUDONYM_SURNAMES)]
        digest //= len(_PSEUDONYM_SURNAMES)
        length = 2 if len(value) <= 2 else 3
        given = "".join(
            _PSEUDONYM_GIVEN_CHARS[
                (digest // (len(_PSEUDONYM_GIVEN_CHARS) ** i))
                % len(_PSEUDONYM_GIVEN_CHARS)]
            for i in range(length - 1))
        return surname + given
    if kind == "phone":
        digest = _digest_int(("phone", value), counter)
        prefix = value[:3] if len(value) >= 3 and value[:3].isdigit() else "138"
        if prefix[0] != "1" or prefix[1] not in "3456789":
            prefix = "138"
        return prefix + str(digest % 100_000_000).zfill(8)
    if kind == "email":
        digest = _digest_int(("email", value), counter)
        return f"user{str(digest % 100_000_000).zfill(8)}@example.com"
    if kind == "id_card":
        digest = _digest_int(("id_card", value), counter)
        body = (
            "1101011990"
            + str(digest % 12 + 1).zfill(2)
            + str(digest // 12 % 28 + 1).zfill(2)
            + str(digest // 336 % 1000).zfill(3)
        )
        total = sum(int(c) * w for c, w in zip(body, _ID_WEIGHTS))
        return body + _ID_CHECK[total % 11]
    if kind == "bank_card":
        digits = "".join(c for c in value if c.isdigit()) or "6222020000000000"
        length = len(digits) if 13 <= len(digits) <= 19 else 16
        digest = _digest_int(("bank_card", value), counter)
        body = digits[0] + str(digest % 10 ** (length - 2)).zfill(length - 2)
        return next(
            (body + str(check) for check in range(10)
             if _luhn_valid(body + str(check))),
            body + "0",
        )
    if kind == "address":
        digest = _digest_int(("address", value), counter)
        return f"示范路{100 + digest % 900}号"
    digest = _digest_int((kind, value), counter)
    return f"{kind}-{digest % 1_000_000:06d}"


def replace_stand_ins(text: str, mapping: Mapping[str, str]) -> str:
    """Replace every stand-in in ``text`` exactly once, in a single pass.

    Sequential replacement would let a just-restored original be matched by
    the next key (a Session may legitimately map ``A -> X`` and ``S -> A``),
    so the alternation consumes each span once and never rescans its output.
    """

    if not text or not mapping:
        return text
    keys = sorted((key for key in mapping if key), key=len, reverse=True)
    if not keys:
        return text
    pattern = re.compile('|'.join(re.escape(key) for key in keys))
    return pattern.sub(lambda match: str(mapping[match.group(0)]), text)


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


def current_session_id() -> str:
    """Return the trusted session scope for reversible DLP references.

    ``RunControl.session_id`` is the key the Hook map and the Run itself use.
    The engine also publishes ``session_id``/``thread_id``, but a Session can
    carry a distinct thread (context clearing), so the Run's own identity is
    the only key that stays stable for the whole conversation.
    """
    import os
    from langgraph.config import get_config
    try:
        config = get_config().get('configurable', {})
    except RuntimeError:
        config = {}
    control = config.get('run_control')
    return str(getattr(control, 'session_id', '')
               or config.get('session_id') or config.get('product_session_id')
               or config.get('thread_id') or os.environ.get('BIANBU_SESSION_ID') or '')


def _adopt_hook_stand_ins(session_id: str) -> None:
    """Bring the Security Hook's Session map into this process.

    The Hook process owns the map that securityd created.  This middleware
    scans the model request again, so it must know those stand-ins are already
    assigned; otherwise it would replace them with a different value and the
    user could no longer see what the model actually answered.
    """
    if not session_id:
        return
    try:
        from langgraph.config import get_config

        control = get_config().get("configurable", {}).get("run_control")
    except RuntimeError:
        return
    mapping = getattr(control, "dlp_stand_ins", None)
    if not isinstance(mapping, Mapping) or not mapping:
        return
    # The engine configures both ``session_id`` and ``thread_id`` with the
    # product Session, but the Hook map is keyed by the Run's own
    # ``RunControl.session_id``.  Adopt under that same key so a later scan of
    # the real value finds the stand-in the model was already given.
    target = str(getattr(control, "session_id", "") or "") or session_id
    _REFERENCES.merge(target, mapping)


class _ReferenceStore:
    """In-memory, session-scoped handles that tools can resolve, never models.

    The map holds plaintext by design: it is the only way to hand the original
    value to a local tool after the model has been given an opaque handle.  It
    never leaves the Worker process, is bounded and expires with its session.
    """

    def __init__(self) -> None:
        import os
        self._lock = threading.Lock()
        # session -> (created, ref -> (value, label), (label, value) -> ref,
        #             real values seen in this session)
        self._sessions: dict[str, tuple[
            float,
            OrderedDict[str, tuple[str, str]],
            dict[tuple[str, str], str],
            set[str],
        ]] = {}
        self._max = max(1, int(os.environ.get('BIANBU_AGENT_DLP_REF_MAX', '2048')))
        self._ttl = max(0.0, float(os.environ.get('BIANBU_AGENT_DLP_REF_TTL', '3600')))

    def _session(self, session_id: str, *, create: bool = False):
        key = session_id or '-'
        now = time.monotonic()
        item = self._sessions.get(key)
        if item is not None and self._ttl and now - item[0] > self._ttl:
            self._sessions.pop(key, None)
            item = None
        if item is None and create:
            item = (now, OrderedDict(), {}, set())
            self._sessions[key] = item
        return item

    def get_or_create(self, session_id: str, pii_type: str, value: str) -> str:
        label = str(pii_type or 'pii')
        pair = (label, value)
        with self._lock:
            item = self._session(session_id, create=True)
            assert item is not None
            created, refs, by_value, real_values = item
            # The same text can cross this boundary twice: securityd masks the
            # message, then the model middleware scans the already-masked
            # request.  A stand-in this Session already owns must pass through
            # unchanged; replacing it again would show the model a different
            # value from the one the Hook reported.
            if value in refs:
                refs.move_to_end(value)
                return value
            existing = by_value.get(pair)
            if existing is None:
                # A stand-in adopted from the Hook process only carries
                # ``ref -> value``.  The detected label can also differ from the
                # one the Hook used, so the Session-wide value lookup is the
                # authority: the model has already seen that stand-in and a
                # second one would desynchronise its context from the map.
                existing = next(
                    (ref for ref, (known, _label) in refs.items()
                     if known == value),
                    None,
                )
            if existing is not None and existing in refs:
                by_value[pair] = existing
                refs.move_to_end(existing)
                return existing
            if label not in PII_TYPES:
                raise DLPDenied('DLP 引用生成失败', stage='pre_model_call')
            for counter in range(64):
                ref = natural_stand_in(label, value, counter)
                if ref not in refs and ref not in real_values:
                    break
            else:  # pragma: no cover - pool is far larger than 64 collisions
                raise DLPDenied('DLP 引用生成失败', stage='pre_model_call')
            refs[ref] = (value, label)
            by_value[pair] = ref
            real_values.add(value)
            while len(refs) > self._max:
                old_ref, (old_value, old_type) = refs.popitem(last=False)
                if not any(v == old_value for v, _t in refs.values()):
                    real_values.discard(old_value)
                if by_value.get((old_type, old_value)) == old_ref:
                    by_value.pop((old_type, old_value), None)
            return ref

    def mapping(self, session_id: str) -> dict[str, str]:
        with self._lock:
            item = self._session(session_id)
            return {ref: value for ref, (value, _label) in item[1].items()} if item else {}

    def merge(self, session_id: str, mapping: Mapping[str, str]) -> None:
        """Adopt stand-ins that were assigned in the trusted Hook process."""

        if not mapping:
            return
        with self._lock:
            item = self._session(session_id, create=True)
            assert item is not None
            created, refs, by_value, real_values = item
            known_values = {known for known, _label in refs.values()}
            for ref, value in mapping.items():
                if not isinstance(ref, str) or not isinstance(value, str):
                    continue
                if not ref or not value or ref in refs:
                    continue
                if value in known_values:
                    # The model has already been shown this value's stand-in;
                    # adopting a second one would fork its context.
                    continue
                refs[ref] = (value, "pii")
                known_values.add(value)
                real_values.add(value)

    def contains(self, session_id: str, text: str) -> bool:
        with self._lock:
            item = self._session(session_id)
            return bool(item) and any(ref in text for ref in item[1])

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._sessions.clear()
            else:
                self._sessions.pop(session_id or '-', None)


_REFERENCES = _ReferenceStore()


def _REFERENCE_STORE_CONTAINS(text: str) -> bool:
    return _REFERENCES.contains(current_session_id(), text)


def mask_text_with_references(text: str, findings: list[dict], *,
                              session_id: str | None = None) -> str:
    """Replace masked spans with session-stable handles and retain originals."""
    if not text or not findings:
        return text
    session = session_id if session_id is not None else current_session_id()
    known = _REFERENCES.mapping(session)
    if known:
        # The same value can reach this boundary twice: the security Hook may
        # already have replaced it, and this middleware then scans the masked
        # request.  A stand-in this Session already owns is not new PII and
        # must not receive a second stand-in.
        findings = [
            item for item in findings
            if text[int(item['start']):int(item['end'])] not in known
        ]
        if not findings:
            return text
    out = text
    for finding in sorted(findings, key=lambda item: int(item['start']), reverse=True):
        start, end = int(finding['start']), int(finding['end'])
        if start < 0 or end < start or end > len(out):
            raise ValueError('securityd DLP finding offset 越界')
        ref = _REFERENCES.get_or_create(
            session, str(finding.get('pii_type') or 'pii'), out[start:end])
        out = out[:start] + ref + out[end:]
    return out


def resolve_references(value, *, session_id: str | None = None, tool: str = ''):
    """Replace known handles with their originals inside a trusted caller.

    Unknown handle-shaped text fails closed: a forged or expired reference must
    never be forwarded to a tool as if it were ordinary data.
    """
    session = session_id if session_id is not None else current_session_id()
    mapping = _REFERENCES.mapping(session)
    if not mapping:
        if contains_reference(value):
            raise DLPDenied('DLP 引用已过期或不可用于本次会话',
                            stage='pre_tool_call', tool=tool)
        return value, 0

    def restore_string(text: str) -> tuple[str, int]:
        # Longest first, one pass: a natural stand-in may contain another
        # value as a substring (a name inside an address, say), and replacing
        # in sequence could rewrite text that was just restored.
        keys = sorted(mapping, key=len, reverse=True)
        pattern = re.compile(
            '|'.join(
                [re.escape(key) for key in keys]
                + [_LEGACY_REFERENCE.pattern]
            )
        )
        count = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal count
            ref = match.group(0)
            original = mapping.get(ref)
            if original is None:
                if _LEGACY_REFERENCE.fullmatch(ref):
                    raise DLPDenied('DLP 引用已过期或不可用于本次会话',
                                    stage='pre_tool_call', tool=tool)
                return ref
            count += 1
            return original

        return pattern.sub(replace, text), count

    if isinstance(value, str):
        return restore_string(value)
    if isinstance(value, list):
        restored, total = [], 0
        for item in value:
            item_value, item_count = resolve_references(
                item, session_id=session, tool=tool)
            restored.append(item_value)
            total += item_count
        return restored, total
    if isinstance(value, dict):
        restored, total = {}, 0
        for key, item in value.items():
            item_value, item_count = resolve_references(
                item, session_id=session, tool=tool)
            restored[key] = item_value
            total += item_count
        return restored, total
    return value, 0


def contains_reference(value) -> bool:
    """Fast check used to keep the common no-DLP tool path off the bus."""
    if isinstance(value, str):
        if _LEGACY_REFERENCE.search(value) is not None:
            return True
        # A natural stand-in is indistinguishable from ordinary text, so the
        # restored value is resolved by exact text lookup instead of a marker
        # regex.  This path only asks a private in-process map.
        return _REFERENCE_STORE_CONTAINS(value)
    if isinstance(value, list):
        return any(contains_reference(item) for item in value)
    if isinstance(value, dict):
        return any(contains_reference(item) for item in value.values())
    return False


def clear_references(session_id: str | None = None) -> None:
    """Drop reversible DLP state (tests and session teardown)."""
    _REFERENCES.clear(session_id)


def dlp_checks_disabled(*, stage: str, agent_id: str | None = None) -> bool:
    """Read the current trusted mode; YOLO records a bypass without scanning."""
    from bianbu_agent.security.enablement import security_agent_enabled
    from bianbu_agent.security.security_middleware import agent_security_mode_is_yolo

    if not security_agent_enabled():
        # No security Plugin means no DLP and no securityd audit either.
        return True
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


def scan_text(text: str, *, stage: str = 'pre_model_call',
              agent_id: str | None = None) -> str:
    """Consult current policy for each request; never cache historical decisions."""
    if not text:
        return text
    if dlp_checks_disabled(stage=stage, agent_id=agent_id):
        return text
    _adopt_hook_stand_ins(current_session_id())
    from bianbu_agent.security.security_runtime import security_client_call
    try:
        result = security_client_call(
            'dlp_scan', agent_id or current_agent_id(), text)
        findings = result['findings']
        if not isinstance(findings, list):
            raise ValueError('invalid findings')
        if any(f.get('action') not in {'allow', 'ask', 'deny', 'mask'} for f in findings):
            raise ValueError('invalid DLP action')
        # 该边界没有交互确认通道：ask 与 deny 一律按拒绝处理（fail-closed）。
        if any(f['action'] in {'deny', 'ask'} for f in findings):
            raise DLPDenied('模型数据含受限敏感信息，已按 DLP 策略拦截', stage=stage)
        return mask_text_with_references(
            text, [f for f in findings if f['action'] == 'mask'])
    except DLPDenied:
        raise
    except Exception as exc:
        raise DLPDenied('模型数据 DLP 扫描失败，已阻止外发',
                        stage=stage, degraded=True) from exc


def _scan_value(value, *, stage: str = 'pre_model_call'):
    if isinstance(value, str):
        if _OPAQUE_DATA_URL.match(value.strip()):
            raise DLPDenied('模型数据含无法扫描的内嵌二进制内容，已阻止外发',
                            stage=stage)
        return scan_text(value, stage=stage)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise DLPDenied('模型数据含无法扫描的二进制内容，已阻止外发',
                        stage=stage)
    if isinstance(value, list):
        return [_scan_value(v, stage=stage) for v in value]
    if isinstance(value, dict):
        return {k: _scan_value(v, stage=stage) for k, v in value.items()}
    return value


def _scan_content_block(block, *, stage: str):
    """Scan every text-bearing field of one content block.

    Non-text payloads (images, audio, documents, inline data) are refused
    instead of being forwarded unscanned.  Unknown block shapes are walked
    recursively so provider-specific reasoning/thinking blocks are covered
    too; only their text can leave this boundary.
    """
    if isinstance(block, str):
        return scan_text(block, stage=stage)
    if not isinstance(block, dict):
        raise DLPDenied('模型消息含无法进行 DLP 扫描的内容块，已阻止外发',
                        stage=stage)
    kind = str(block.get('type') or '').strip().lower()
    if kind in _MEDIA_BLOCK_TYPES or 'image_url' in block or 'input_image' in block:
        raise DLPDenied('模型消息含无法进行 DLP 扫描的媒体内容，已阻止外发',
                        stage=stage)
    return _scan_value(block, stage=stage)


def scan_message(message, *, stage: str = 'pre_model_call'):
    """Copy textual payloads, preserving message/call IDs and media blocks."""
    import json
    content = message.content
    if isinstance(content, str):
        content = scan_text(content, stage=stage)
    elif isinstance(content, list):
        content = [_scan_content_block(b, stage=stage) for b in content]
    updates = {'content': content}
    calls = getattr(message, 'tool_calls', None)
    if calls:
        updates['tool_calls'] = [
            {**c, 'args': _scan_value(c['args'], stage=stage)} for c in calls]
    extra = dict(message.additional_kwargs)
    if isinstance(extra.get('reasoning_content'), str):
        extra['reasoning_content'] = scan_text(
            extra['reasoning_content'], stage=stage)
    if extra.get('tool_calls'):
        extra['tool_calls'] = [{**c, 'function': {**c['function'],
            'arguments': json.dumps(_scan_value(
                json.loads(c['function']['arguments']), stage=stage),
                ensure_ascii=False)}}
            for c in extra['tool_calls']]
    function_call = extra.get('function_call')
    if isinstance(function_call, dict) and isinstance(
            function_call.get('arguments'), str):
        extra['function_call'] = {
            **function_call,
            'arguments': scan_text(function_call['arguments'], stage=stage),
        }
    updates['additional_kwargs'] = extra
    return message.model_copy(update=updates)


def scan_request(request):
    return request.override(
        messages=[scan_message(m, stage='pre_model_call')
                  for m in request.messages],
        system_message=(scan_message(request.system_message,
                                     stage='pre_model_call')
                        if request.system_message else None))


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
    result = [scan_message(message, stage='post_model_call')
              for message in response.result]
    return replace(response, result=result,
                   structured_response=_scan_value(
                       response.structured_response, stage='post_model_call'))
