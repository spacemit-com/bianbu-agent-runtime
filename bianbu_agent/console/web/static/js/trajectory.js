(function (root) {
    'use strict';

    function jsonText(value) {
        if (value == null || value === '') return '';
        if (typeof value === 'string') return value;
        try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
    }

    // Compact one-line rendering for ledger previews.  Objects must never
    // reach String(): an event whose detail is an object would show up as
    // "[object Object]" and tell the reader nothing.
    function previewText(value) {
        if (value == null) return '';
        if (typeof value === 'string') return value;
        if (typeof value === 'number' || typeof value === 'boolean') return String(value);
        if (Array.isArray(value) && value.length === 0) return '';
        if (typeof value === 'object' && Object.keys(value).length === 0) return '';
        try { return JSON.stringify(value); } catch (_) { return String(value); }
    }

    function oneLine(value, limit) {
        const text = previewText(value).replace(/\s+/g, ' ').trim();
        return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
    }

    // Scalar facts of a payload rendered as `key=value · …`.  The same shape is
    // used by Hook summaries and by generic Event rows, so every row that does
    // have data shows that data instead of repeating its own event name.
    // Objects and session-scoped identifiers (`*_id`) tell a reader nothing in
    // a one-line row; the raw payload stays in the detail pane.
    function scalarFacts(value, limit, skipKey) {
        const parts = [];
        if (!value || typeof value !== 'object') return parts;
        for (const [key, item] of Object.entries(value)) {
            if (parts.length >= limit) break;
            if (key.endsWith('_id') || (skipKey && skipKey(key, item))) continue;
            if (item && typeof item === 'object') continue;
            const text = oneLine(item, 40);
            if (!text) continue;
            parts.push(`${key}=${text}`);
        }
        return parts;
    }

    // Session events that live outside a Run still deserve a readable name.
    const SESSION_EVENT_LABELS = {
        'agent/inbox/spliced': 'Inbox queued',
        'agent/inbox/claimed': 'Inbox claimed',
        'agent/input.applied': 'Input applied',
        'run/event': 'Run event',
    };

    function sessionEventPreview(event, data) {
        if (event.type === 'agent/inbox/spliced') {
            const message = data.message;
            return oneLine(message && typeof message === 'object'
                ? message.content : message, 160);
        }
        if (event.type === 'agent/inbox/claimed') {
            const ids = Array.isArray(data.message_ids) ? data.message_ids : [];
            return ids.length > 0 ? `${ids.length} message(s)` : '';
        }
        if (event.type === 'run/event') {
            const payload = data.payload && typeof data.payload === 'object'
                ? scalarFacts(data.payload, 3).join(' · ') || oneLine(data.payload, 80)
                : oneLine(data.payload, 80);
            return [String(data.kind || ''), payload].filter(Boolean).join(' · ');
        }
        return oneLine(data.message ?? data.content ?? data.status ?? data.error, 160)
            || scalarFacts(data, 3).join(' · ');
    }

    // Hook payloads are structured, not prose.  One uniform summary line keeps
    // every Hook row readable: decision, a few scalar facts, then the effect.
    function hookSummary(data, sections, metadata, point = '') {
        const parts = [];
        const meta = metadata && typeof metadata === 'object' ? metadata : {};
        const inner = meta.securityd && typeof meta.securityd === 'object'
            ? meta.securityd : meta;
        const decision = inner.decision ?? meta.decision;
        if (decision != null && decision !== '') parts.push(String(decision).toUpperCase());
        if (data.error) parts.unshift(oneLine(data.error, 80));
        // "decision" is already the leading token, "_handler_trace" is debug
        // noise, and "stage" only adds value when it differs from the Hook
        // point the row already shows.
        parts.push(...scalarFacts(inner, 3, (key, value) => (
            key === 'decision' || key === '_handler_trace'
                || (key === 'stage' && String(value) === point)
        )));
        if (sections.length > 0) {
            parts.push(`context +${sections.length} · ${oneLine(sections[0], 60)}`);
        }
        if (data.direct_reply) parts.push('direct reply');
        return parts.join(' · ');
    }

    function hookName(hookId) {
        const text = String(hookId || '');
        return text.includes('/') ? text.slice(text.lastIndexOf('/') + 1) : text;
    }

    function numberOrNull(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function duration(start, end) {
        return start == null || end == null ? null : Math.max(0, end - start);
    }

    function recordBase(event, turn, kind, title) {
        const time = numberOrNull(event.time);
        return {
            key: `${kind}:${Number(event.seq)}`,
            seq: Number(event.seq),
            end_seq: Number(event.seq),
            run_id: event.run_id == null ? null : String(event.run_id),
            turn,
            kind,
            title,
            preview: '',
            input: '',
            output: '',
            status: null,
            started_at: time,
            ended_at: time,
            duration: null,
            data: {...(event.data || {})},
        };
    }

    // 轨迹要能对账：一轮的"耗时"是墙钟跨度，所以没有任何记录覆盖的时间必须自己
    // 成行，否则把行加起来只会得到一个比耗时小的数（长会话里能差出几十秒）。
    // 1ms 以下的空档并进相邻记录，既不刷碎行又能让口径严格闭合。
    const WAIT_MIN_SECONDS = 0.001;

    function waitReason(previousSpan, nextSpan) {
        if (previousSpan == null) return 'startup';
        if (nextSpan.kind === 'run') return 'finalize';
        if (nextSpan.kind === 'reasoning' || nextSpan.kind === 'assistant') {
            return 'model';
        }
        if (previousSpan.kind === 'tool' || nextSpan.kind === 'tool') return 'handoff';
        return 'idle';
    }

    function waitRecord(turn, start, end, reason, previousSpan, nextSpan) {
        const title = {
            startup: 'Startup',
            model: 'Model wait',
            handoff: 'Handoff',
            finalize: 'Finalize',
            idle: 'Idle',
        }[reason] || 'Idle';
        const seconds = end - start;
        const anchor = nextSpan || previousSpan;
        const record = {
            key: `wait:${turn.turn}:${start.toFixed(3)}:${end.toFixed(3)}`,
            seq: anchor == null ? 0 : Number(anchor.seq),
            end_seq: anchor == null ? 0 : Number(anchor.seq),
            run_id: turn.run_id,
            turn: turn.turn,
            kind: 'wait',
            title,
            preview: `No events for ${seconds.toFixed(2)}s`,
            input: '',
            output: '',
            status: null,
            started_at: start,
            ended_at: end,
            duration: seconds,
            data: {
                reason,
                waited_seconds: Number(seconds.toFixed(3)),
                after: previousSpan ? previousSpan.title : null,
                before: nextSpan ? nextSpan.title : null,
            },
        };
        record.wait = {reason};
        return record;
    }

    // 一条记录压进已覆盖区间时的两种重叠：完全落在里面的是嵌套（工具调用内部的
    // before_tool Hook），越过右边界的是并行（同一次模型轮里并发跑的两个工具）。
    // 它们的时间与别的行重叠，界面必须标出来，否则"行加起来大于耗时"看起来像 bug。
    function placeOverlappingSpan(span, cursor, owner) {
        span.overlap_seconds = Math.max(0, Math.min(span.ended_at, cursor) - span.started_at);
        span.owner_key = owner ? owner.key : null;
        if (span.ended_at <= cursor) {
            span.nested = true;
            return cursor;
        }
        span.parallel = true;
        return span.ended_at;
    }

    function accountForWallClock(turn) {
        if (turn.duration == null || turn.started_at == null || turn.ended_at == null) {
            return;
        }
        const spans = turn.records
            .filter(record => record.started_at != null && record.ended_at != null
                && record.ended_at > record.started_at)
            .sort((left, right) => left.started_at - right.started_at
                || left.seq - right.seq);
        const waits = [];
        let cursor = turn.started_at;
        let previousSpan = null;
        for (const span of spans) {
            if (span.started_at > cursor && span.started_at - cursor < WAIT_MIN_SECONDS) {
                span.started_at = cursor;
                span.duration = duration(span.started_at, span.ended_at);
            }
            if (span.started_at >= cursor) {
                if (span.started_at - cursor >= WAIT_MIN_SECONDS) {
                    waits.push({
                        before: span,
                        start: cursor,
                        end: span.started_at,
                        reason: waitReason(previousSpan, span),
                    });
                }
                cursor = span.ended_at;
                previousSpan = span;
                continue;
            }
            cursor = Math.max(cursor, placeOverlappingSpan(span, cursor, previousSpan));
        }
        if (turn.ended_at > cursor) {
            if (turn.ended_at - cursor < WAIT_MIN_SECONDS && previousSpan) {
                // 收尾的亚毫秒空档并进最后一条记录，让合计与耗时完全相等。
                previousSpan.ended_at = turn.ended_at;
                previousSpan.duration = duration(previousSpan.started_at, previousSpan.ended_at);
            } else {
                waits.push({
                    before: null,
                    start: cursor,
                    end: turn.ended_at,
                    reason: 'finalize',
                    previous: previousSpan,
                });
            }
            cursor = turn.ended_at;
        }
        turn.waiting = waits.reduce((total, wait) => total + (wait.end - wait.start), 0);
        turn.covered = turn.duration - turn.waiting;
        // 嵌套行与并行行的时间与别的行重叠：把它们单列出来，用户才知道为什么
        // 把每行显示的耗时加起来会比"耗时"大。
        turn.overlap = Number(turn.records.reduce((total, record) =>
            total + (record.overlap_seconds || 0), 0).toFixed(6));
        if (waits.length === 0) return;
        const beforeRow = new Map();
        let trailing = null;
        for (const wait of waits) {
            const record = waitRecord(
                turn, wait.start, wait.end, wait.reason,
                wait.previous == null ? previousSpanFor(spans, wait.start) : wait.previous,
                wait.before,
            );
            if (wait.before) beforeRow.set(wait.before, record);
            else trailing = record;
        }
        const ordered = [];
        for (const record of turn.records) {
            const wait = beforeRow.get(record);
            if (wait) ordered.push(wait);
            ordered.push(record);
        }
        if (trailing) {
            // 收尾的等待发生在最后一条记录之前，排在 Run 行前面才符合时间顺序。
            const last = ordered[ordered.length - 1];
            if (last && last.kind === 'run') ordered.splice(ordered.length - 1, 0, trailing);
            else ordered.push(trailing);
        }
        turn.records = ordered;
    }

    function previousSpanFor(spans, time) {
        let found = null;
        for (const span of spans) {
            if (span.ended_at <= time) found = span;
            else break;
        }
        return found;
    }

    function projectTrajectory(input) {
        const sorted = (input || []).slice().sort((left, right) =>
            Number(left.seq) - Number(right.seq));
        // Events without a run_id (for example agent/inbox/spliced) are session
        // bookkeeping, not a Turn of their own.  Fold each one into the Run it
        // precedes — or, when nothing follows, the Run before it — so a single
        // question never renders as two 轮次.
        const runIds = sorted.map(event =>
            event?.run_id == null ? null : String(event.run_id));
        let following = null;
        for (let index = runIds.length - 1; index >= 0; index -= 1) {
            if (runIds[index] === null) runIds[index] = following;
            else following = runIds[index];
        }
        let preceding = null;
        for (let index = 0; index < runIds.length; index += 1) {
            if (runIds[index] === null) runIds[index] = preceding;
            else preceding = runIds[index];
        }
        const events = sorted.map((event, index) => (
            event && typeof event === 'object' && event.run_id == null
                ? {...event, run_id: runIds[index]}
                : event
        ));
        const hiddenRuns = new Set(events.flatMap(event =>
            event?.type === 'user/message'
                && event.data?.console_operation
                && event.run_id != null
                ? [String(event.run_id)] : []));
        const turns = [];
        const turnsByRun = new Map();
        const records = [];

        function ensureTurn(event) {
            const runId = event.run_id == null ? null : String(event.run_id);
            const key = runId === null ? `event:${Number(event.seq)}` : `run:${runId}`;
            let turn = turnsByRun.get(key);
            if (turn) return turn;
            turn = {
                key,
                turn: turns.length + 1,
                run_id: runId,
                status: 'running',
                started_at: numberOrNull(event.time),
                ended_at: null,
                duration: null,
                records: [],
                openStream: null,
                tools: new Map(),
                hooks: new Map(),
            };
            turnsByRun.set(key, turn);
            turns.push(turn);
            return turn;
        }

        function append(turn, record) {
            turn.records.push(record);
            records.push(record);
            return record;
        }

        function closeStream(turn, event) {
            const record = turn.openStream;
            if (!record) return;
            record.ended_at = numberOrNull(event.time);
            record.duration = duration(record.started_at, record.ended_at);
            turn.openStream = null;
        }

        for (const event of events) {
            if (!event || !Number.isInteger(Number(event.seq))) continue;
            if (event.run_id != null && hiddenRuns.has(String(event.run_id))) continue;
            const turn = ensureTurn(event);
            const data = event.data || {};
            const eventTime = numberOrNull(event.time);
            if (eventTime != null && (turn.started_at == null || eventTime < turn.started_at)) {
                turn.started_at = eventTime;
            }

            const streamKind = event.type === 'assistant/reasoning.delta'
                ? 'reasoning'
                : event.type === 'assistant/text.delta' ? 'assistant' : null;
            if (streamKind !== null) {
                let record = turn.openStream;
                if (!record || record.kind !== streamKind) {
                    closeStream(turn, event);
                    record = recordBase(
                        event, turn.turn, streamKind,
                        streamKind === 'reasoning' ? 'Reasoning' : 'Assistant',
                    );
                    record.ended_at = null;
                    record.key = `${streamKind}:${Number(event.seq)}`;
                    append(turn, record);
                    turn.openStream = record;
                }
                const text = String(data.text || '');
                record.output += text;
                record.preview = record.output;
                record.end_seq = Number(event.seq);
                record.data = {};
                continue;
            }

            const isToolEvent = String(event.type).startsWith('tool/call.');
            if (!isToolEvent || event.type === 'tool/call.started') closeStream(turn, event);

            if (event.type === 'user/message') {
                const record = recordBase(event, turn.turn, 'user', 'User');
                record.input = String(data.content || '');
                record.preview = record.input;
                record.data = Object.fromEntries(Object.entries(data).filter(
                    ([key, value]) => key !== 'content' && key !== 'request'
                        && value != null && value !== ''
                        && (!Array.isArray(value) || value.length > 0),
                ));
                append(turn, record);
                continue;
            }
            if (event.type === 'assistant/message.end') continue;
            if (event.type === 'request/context') {
                const record = recordBase(event, turn.turn, 'context', 'System prompt');
                record.output = String(data.system_prompt || '');
                record.preview = record.output;
                record.status = String(data.reason || 'current');
                record.data = Object.fromEntries(Object.entries(data).filter(
                    ([key, value]) => key !== 'system_prompt' && key !== 'reason'
                        && value != null && value !== '',
                ));
                append(turn, record);
                continue;
            }
            if (event.type === 'hook/invoked') {
                const invocationId = String(data.invocation_id || '');
                const record = recordBase(
                    event,
                    turn.turn,
                    'hook',
                    String(data.hook_id || 'Hook'),
                );
                record.key = `hook:${invocationId || Number(event.seq)}`;
                record.status = 'running';
                record.ended_at = null;
                record.hook = {
                    id: String(data.hook_id || ''),
                    name: hookName(data.hook_id),
                    point: String(data.point || ''),
                };
                record.data = {point: record.hook.point};
                turn.hooks.set(invocationId, record);
                append(turn, record);
                continue;
            }
            if (event.type === 'hook/result') {
                const invocationId = String(data.invocation_id || '');
                let record = turn.hooks.get(invocationId);
                if (!record) {
                    record = recordBase(
                        event,
                        turn.turn,
                        'hook',
                        String(data.hook_id || 'Hook'),
                    );
                    record.key = `hook:${invocationId || Number(event.seq)}`;
                    turn.hooks.set(invocationId, record);
                    append(turn, record);
                }
                const sections = Array.isArray(data.context_sections)
                    ? data.context_sections.map(String).filter(Boolean) : [];
                const metadata = data.metadata && typeof data.metadata === 'object'
                    ? data.metadata : null;
                record.end_seq = Number(event.seq);
                record.title = String(data.hook_id || record.title);
                record.status = String(data.status || 'completed');
                record.ended_at = eventTime;
                const measured = numberOrNull(data.duration_ms);
                // 行在时间轴上占的是墙钟区间（invoked → result）。Hook 自报的
                // duration_ms 是它自己那一段，放进 data 里对照即可：拿它当行宽会
                // 出现"显示 0.9s、吃掉 3.1s 时间轴"的账，合计就对不上了。
                const wall = duration(record.started_at, record.ended_at);
                record.duration = wall == null
                    ? (measured == null ? null : Math.max(0, measured / 1000))
                    : wall;
                const point = String(data.point || record.data.point || '');
                record.output = sections.length > 0 ? sections.join('\n\n') : '';
                record.preview = hookSummary(data, sections, metadata, point) || record.status;
                record.hook = {
                    id: String(data.hook_id || record.hook?.id || ''),
                    name: hookName(data.hook_id) || record.hook?.name || '',
                    point,
                    sections,
                    directReply: Boolean(data.direct_reply),
                    metadata,
                };
                record.data = {
                    point,
                    ...(metadata ? {metadata} : {}),
                    ...(data.direct_reply ? {direct_reply: true} : {}),
                    ...(measured == null ? {} : {hook_elapsed_ms: Math.round(measured)}),
                };
                record.noise = record.status === 'completed'
                    && sections.length === 0
                    && metadata === null
                    && !data.direct_reply
                    && !data.error;
                continue;
            }
            if (isToolEvent) {
                const callId = String(data.call_id || data.tool_call_id || '');
                let record = turn.tools.get(callId);
                if (!record) {
                    record = recordBase(event, turn.turn, 'tool', String(data.name || data.tool || 'Tool'));
                    record.key = `tool:${callId || 'unknown'}:${Number(event.seq)}`;
                    record.started_at = event.type === 'tool/call.started' ? eventTime : null;
                    record.ended_at = null;
                    record.input = jsonText(data.arguments || {});
                    record.status = 'running';
                    record.data = {
                        call_id: callId,
                    };
                    turn.tools.set(callId, record);
                    append(turn, record);
                }
                record.end_seq = Number(event.seq);
                if (data.name || data.tool) {
                    record.title = String(data.name || data.tool);
                }
                if (data.arguments) {
                    record.input = jsonText(data.arguments);
                }
                if (event.type === 'tool/call.progress') {
                    record.data.progress = String(data.content || data.text || '');
                    record.preview = record.data.progress;
                }
                if (event.type === 'tool/call.completed') {
                    record.output = String(data.result || '');
                    record.preview = record.output;
                    record.status = String(data.status || 'success');
                    record.ended_at = eventTime;
                    record.duration = duration(record.started_at, record.ended_at);
                }
                if (event.type === 'tool/call.rejected') {
                    record.output = String(data.reason || 'tool_not_available');
                    record.preview = record.output;
                    record.status = 'rejected';
                    record.ended_at = eventTime;
                    record.duration = duration(record.started_at, record.ended_at);
                }
                continue;
            }

            if (['run/completed', 'run/failed', 'run/cancelled'].includes(event.type)) {
                const status = event.type.slice('run/'.length);
                turn.status = status;
                turn.ended_at = eventTime;
                turn.duration = duration(turn.started_at, turn.ended_at);
                const record = recordBase(event, turn.turn, 'run', `Run ${status}`);
                record.status = status;
                record.preview = status === 'failed'
                    ? String(data.error?.message || data.error || status)
                    : status;
                record.output = record.preview;
                record.data = data.usage ? {usage: data.usage} : {};
                append(turn, record);
                continue;
            }

            const known = {
                'approval/requested': ['approval', 'Approval requested'],
                'approval/resolved': ['approval', 'Approval resolved'],
                'context/compressed': ['context', 'Context compressed'],
            }[event.type];
            const label = SESSION_EVENT_LABELS[event.type];
            const record = recordBase(
                event, turn.turn, known?.[0] || 'event',
                known?.[1] || label || String(event.type),
            );
            const detail = sessionEventPreview(event, data);
            record.preview = label
                ? [label, detail].filter(Boolean).join(' · ')
                : detail || String(event.type);
            record.output = jsonText(data);
            record.data = {};
            append(turn, record);
        }

        turns.forEach(turn => {
            turn.records = turn.records.filter(record => !record.noise);
            turn.records.forEach(record => { delete record.noise; });
            if (turn.openStream) turn.openStream = null;
            if (turn.ended_at == null) {
                const last = turn.records[turn.records.length - 1];
                turn.ended_at = last?.ended_at ?? null;
                turn.duration = null;
            }
            accountForWallClock(turn);
            delete turn.openStream;
            delete turn.tools;
            delete turn.hooks;
        });

        const visibleRecords = turns.flatMap(turn => turn.records);

        return {
            turns,
            records: visibleRecords,
        };
    }

    root.BianbuTrajectory = {projectTrajectory};
})(window);
