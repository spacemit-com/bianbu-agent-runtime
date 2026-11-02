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
        'session/title': 'Session title',
        'session/title/request': 'Title request',
        'session/title/route': 'Title route',
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
        if (event.type === 'session/title/request') {
            const route = data.route && typeof data.route === 'object'
                ? [data.route.provider, data.route.model].filter(Boolean).join('/')
                : '';
            const system = oneLine(data.system, 60);
            const input = oneLine(data.input, 80);
            return [route, system, input].filter(Boolean).join(' · ');
        }
        if (event.type === 'session/title/route') {
            const route = data.route && typeof data.route === 'object'
                ? [data.route.provider, data.route.model].filter(Boolean).join('/')
                : '';
            return [route, data.request_seq == null ? '' : `request #${data.request_seq}`]
                .filter(Boolean).join(' · ');
        }
        if (event.type === 'session/title') {
            // The title event is log-only state: show the accepted revision and
            // which source produced it, since there is no Run to attribute it to.
            const source = data.source && typeof data.source === 'object'
                ? String(data.source.kind || '') : String(data.source || '');
            return [oneLine(data.title, 120), source].filter(Boolean).join(' · ');
        }
        if (event.type === 'assistant/message.end') {
            return data.has_tool_calls ? 'Tool calls follow' : 'Assistant reply complete';
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

    // Session-level title work.  It owns no Run, so the ledger treats it as a
    // second input kind next to Runs instead of folding it into a question.
    const TITLE_EVENT_TYPES = new Set([
        'session/title',
        'session/title/request',
        'session/title/route',
    ]);

    function isTitleEvent(event) {
        return Boolean(event) && TITLE_EVENT_TYPES.has(String(event.type || ''));
    }

    function sessionTitleOf(event) {
        const data = event && event.data ? event.data : {};
        return String(data.title || '');
    }

    function numberOrNull(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function duration(start, end) {
        return start == null || end == null ? null : Math.max(0, end - start);
    }

    // A folded history event stands for the chunk span it covers.  Showing only
    // the first seq makes every folded chunk look like a missing event, so the
    // ledger prints the whole span.
    function foldedSeqRange(event) {
        const span = event && event.data ? event.data.seq_range : null;
        if (Array.isArray(span) && Number.isInteger(Number(span[0]))
            && Number.isInteger(Number(span[1]))) {
            return [Number(span[0]), Number(span[1])];
        }
        return null;
    }

    function recordBase(event, turn, kind, title) {
        const time = numberOrNull(event.time);
        const record = {
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
        const folded = foldedSeqRange(event);
        if (folded) record.seq_range = folded;
        return record;
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
        // A title unit is a log-only exchange (fallback -> request ->
        // provider answer).  Gaps between those steps are dispatch bookkeeping,
        // not model waiting, so absorb them into the neighbouring row instead
        // of drawing a fake "Startup"/"Idle" wait.
        const titleUnit = turn.kind === 'title';
        const spans = turn.records
            .filter(record => record.started_at != null && record.ended_at != null
                && (record.ended_at > record.started_at
                    || (titleUnit && record.ended_at === record.started_at)))
            .sort((left, right) => left.started_at - right.started_at
                || left.seq - right.seq);
        const waits = [];
        let cursor = turn.started_at;
        let previousSpan = null;
        for (const span of spans) {
            if (span.started_at > cursor) {
                const gap = span.started_at - cursor;
                if (gap < WAIT_MIN_SECONDS) {
                    span.started_at = cursor;
                    span.duration = duration(span.started_at, span.ended_at);
                } else if (titleUnit) {
                    if (previousSpan) {
                        previousSpan.ended_at = span.started_at;
                        previousSpan.duration = duration(
                            previousSpan.started_at, previousSpan.ended_at,
                        );
                        cursor = span.started_at;
                    } else {
                        span.started_at = cursor;
                        span.duration = duration(span.started_at, span.ended_at);
                    }
                }
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
            if (previousSpan && (titleUnit || turn.ended_at - cursor < WAIT_MIN_SECONDS)) {
                // 收尾空档并进最后一条记录，让合计与耗时完全相等；标题单元的
                // 收尾空档同样不值得单独成行。
                previousSpan.ended_at = turn.ended_at;
                previousSpan.duration = duration(
                    previousSpan.started_at, previousSpan.ended_at,
                );
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
        // Two kinds of input share this stream: Run events (`run_id` set) and
        // Session-level work that owns no Run (`run_id` null).  A null event
        // joins the Run before/after it only when it is plain bookkeeping;
        // `session/title*` is its own unit and must not be absorbed into a
        // question, otherwise the title looks like part of the conversation.
        const runIds = sorted.map(event =>
            event?.run_id == null ? null : String(event.run_id));
        const ownUnit = event => isTitleEvent(event);
        let following = null;
        for (let index = runIds.length - 1; index >= 0; index -= 1) {
            if (runIds[index] === null) {
                runIds[index] = ownUnit(sorted[index]) ? null : following;
            } else {
                following = runIds[index];
            }
        }
        let preceding = null;
        for (let index = 0; index < runIds.length; index += 1) {
            if (runIds[index] === null) {
                if (!ownUnit(sorted[index])) runIds[index] = preceding;
            } else {
                preceding = runIds[index];
            }
        }
        const events = sorted.map((event, index) => (
            event && typeof event === 'object' && event.run_id == null
                && runIds[index] != null
                ? {...event, run_id: runIds[index]}
                : event
        ));
        // One title request is one Turn.  Every event of a request carries the
        // same ``request_id``, minted before the fallback is written.  That id
        // is authoritative: a late provider answer stays in its own unit even
        // when a newer request is already in flight.  Events written before
        // ids existed fall back to seq-order grouping.
        const titleUnitKeys = new Map();
        let currentTitleKey = null;
        const titleEvents = sorted.filter(isTitleEvent);
        const forRequest = (id) => {
            const text = id == null ? '' : String(id);
            return text ? `title:request:${text}` : null;
        };
        for (let index = 0; index < titleEvents.length; index += 1) {
            const event = titleEvents[index];
            const data = event.data || {};
            const seq = Number(event.seq);
            const stamped = forRequest(data.request_id);
            if (stamped) {
                titleUnitKeys.set(seq, stamped);
                if (event.type === 'session/title/request') {
                    currentTitleKey = stamped;
                } else if (event.type === 'session/title') {
                    const source = data.source && typeof data.source === 'object'
                        ? String(data.source.kind || '') : String(data.source || '');
                    if (source === 'provider' && currentTitleKey === stamped) {
                        currentTitleKey = null;
                    }
                }
                continue;
            }
            // Legacy events have no id.  A request names itself by seq, a
            // route names the request seq it resolved, and title revisions
            // bind to the request before/after them positionally.
            if (event.type === 'session/title/request') {
                currentTitleKey = `title:seq:${seq}`;
                titleUnitKeys.set(seq, currentTitleKey);
                continue;
            }
            if (event.type === 'session/title/route') {
                const requestSeq = Number(data.request_seq);
                const key = Number.isInteger(requestSeq)
                    ? `title:seq:${requestSeq}` : currentTitleKey;
                if (key) titleUnitKeys.set(seq, key);
                continue;
            }
            if (!currentTitleKey) {
                let following = null;
                for (let lookahead = index + 1;
                    lookahead < titleEvents.length; lookahead += 1) {
                    if (titleEvents[lookahead].type === 'session/title/request') {
                        following = `title:seq:${Number(titleEvents[lookahead].seq)}`;
                        break;
                    }
                }
                currentTitleKey = following || `title:orphan:${seq}`;
            }
            titleUnitKeys.set(seq, currentTitleKey);
            const source = data.source && typeof data.source === 'object'
                ? String(data.source.kind || '') : String(data.source || '');
            if (source === 'provider') currentTitleKey = null;
        }

        const turns = [];
        const turnsByRun = new Map();
        const records = [];

        function ensureTurn(event) {
            const title = isTitleEvent(event);
            const runId = event.run_id == null ? null : String(event.run_id);
            // Title work is one Turn of its own so the ledger shows a
            // "Title" section instead of pretending the request was a Run.
            const key = title
                ? (titleUnitKeys.get(Number(event.seq)) || `title:${Number(event.seq)}`)
                : runId === null ? `event:${Number(event.seq)}` : `run:${runId}`;
            let turn = turnsByRun.get(key);
            if (turn) return turn;
            turn = {
                key,
                turn: turns.length + 1,
                kind: title ? 'title' : 'run',
                title: title ? sessionTitleOf(event) : '',
                run_id: runId,
                status: 'running',
                started_at: numberOrNull(event.time),
                ended_at: null,
                duration: null,
                records: [],
                openStream: null,
                tools: new Map(),
                hooks: new Map(),
                requestRecord: null,
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
                const folded = foldedSeqRange(event);
                record.output += text;
                record.preview = record.output;
                record.end_seq = Number(event.seq);
                if (folded) {
                    const current = record.seq_range || [
                        Number(event.seq), Number(event.seq),
                    ];
                    record.seq_range = [
                        Math.min(current[0], folded[0]),
                        Math.max(current[1], folded[1]),
                    ];
                }
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
            if (event.type === 'assistant/message.end') {
                // Chat closes its reasoning phase on this event; Trajectory
                // must still account for it.  Skipping a persisted event here
                // leaves a hole in the seq column that no row explains.
                const record = recordBase(
                    event, turn.turn, 'event', 'Assistant message ended',
                );
                record.preview = sessionEventPreview(event, data);
                record.data = {};
                append(turn, record);
                continue;
            }
            if (event.type === 'session/title/request') {
                const record = recordBase(
                    event, turn.turn, 'context', 'Title request',
                );
                record.input = String(data.input || '');
                record.output = String(data.system || '');
                record.preview = sessionEventPreview(event, data);
                record.status = 'sent';
                record.ended_at = null;
                record.request_seq = Number(event.seq);
                turn.requestRecord = record;
                append(turn, record);
                continue;
            }
            if (event.type === 'session/title/route') {
                // The route is resolved only after the Worker picks the
                // Agent's model.  It annotates the request row that was
                // already persisted, so the context and its destination stay
                // one ledger entry while remaining separately auditable.
                const route = data.route && typeof data.route === 'object'
                    ? data.route : {};
                const request = turn.requestRecord;
                if (request) {
                    request.data = {...request.data, route};
                    request.preview = sessionEventPreview(
                        {type: 'session/title/request'}, request.data,
                    );
                } else {
                    const record = recordBase(
                        event, turn.turn, 'event', 'Title route',
                    );
                    record.preview = sessionEventPreview(event, data);
                    record.data = {route, request_seq: data.request_seq};
                    append(turn, record);
                }
                continue;
            }
            if (event.type === 'request/context') {
                // 这一行就是本轮真正发给模型的请求：系统提示词、工具声明与折叠后的
                // 全部消息。引擎按 provider 看到的顺序渲染成纯文本，控制台只负责
                // 原样显示，不再拿会话日志二次拼一份会漂移的视图。
                const record = recordBase(event, turn.turn, 'context', 'Request');
                record.output = String(data.text || '');
                record.preview = record.output;
                record.status = String(data.reason || 'current');
                record.data = Object.fromEntries(Object.entries(data).filter(
                    ([key, value]) => key !== 'text' && key !== 'reason'
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

            if (event.type === 'session/title') {
                // The accepted revision is the unit's outcome; the request row
                // above it already carries the exact model-visible context.
                const source = data.source && typeof data.source === 'object'
                    ? String(data.source.kind || '') : String(data.source || '');
                turn.status = 'completed';
                turn.ended_at = eventTime;
                turn.duration = duration(turn.started_at, turn.ended_at);
                turn.title = String(data.title || turn.title || '');
                // The blocking auxiliary request spans from dispatch to this
                // answer, so its row shows the model time instead of leaving a
                // gap that would render as a fake "wait".
                const request = turn.requestRecord;
                if (request && source === 'provider') {
                    request.end_seq = Number(event.seq);
                    request.ended_at = eventTime;
                    request.duration = duration(request.started_at, request.ended_at);
                    request.status = 'completed';
                }
                const record = recordBase(event, turn.turn, 'title',
                    String(data.title || 'Session title'));
                record.output = String(data.title || '');
                record.preview = [record.output, source].filter(Boolean).join(' · ');
                record.status = source || 'completed';
                record.ended_at = eventTime;
                record.duration = duration(record.started_at, record.ended_at);
                record.data = source ? {source} : {};
                append(turn, record);
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
            if (turn.openStream) turn.openStream = null;
            if (turn.ended_at == null) {
                const last = turn.records[turn.records.length - 1];
                turn.ended_at = last?.ended_at ?? null;
                turn.duration = turn.kind === 'title'
                    ? duration(turn.started_at, turn.ended_at) : null;
            }
            if (turn.kind === 'title') {
                if (!turn.title) turn.title = 'Session title';
                if (turn.status === 'running') turn.status = 'pending';
                if (turn.requestRecord && turn.requestRecord.ended_at == null) {
                    turn.requestRecord.ended_at = turn.ended_at;
                    turn.requestRecord.duration = duration(
                        turn.requestRecord.started_at, turn.requestRecord.ended_at,
                    );
                }
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
