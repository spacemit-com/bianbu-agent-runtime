(function (root) {
    'use strict';

    function emptyState() {
        return { runs: new Map(), order: [], pending: new Map(), messages: [] };
    }

    // A folded history event stands for the stream chunks it covers.  The live
    // cursor has to use the end of that span, or the first chunk appended after
    // the window would look like a gap.
    function coveredEnd(event) {
        const span = event && event.data ? event.data.seq_range : null;
        if (Array.isArray(span) && Number.isInteger(Number(span[1]))) {
            return Number(span[1]);
        }
        return Number(event && event.seq);
    }

    function newRun(firstSeq) {
        return {
            firstSeq, hidden: false, segments: [], currentAssistant: null,
            userMessageIds: new Set(),
        };
    }

    function assistantSegment(run, createdAt, firstSeq) {
        if (run.currentAssistant) return run.currentAssistant;
        const segment = {
            kind: 'assistant', steps: [], toolSteps: new Map(),
            activeThinking: null,
            artifacts: [], approvals: new Map(), terminal: false, terminalContent: null,
            status: null, usage: null, error: null,
            createdAt: Number(createdAt || 0),
            firstSeq: Number(firstSeq),
        };
        run.segments.push(segment);
        run.currentAssistant = segment;
        return segment;
    }

    function appendAssistantText(segment, type, text) {
        const value = String(text || '');
        if (!value) return;
        const previous = segment.steps[segment.steps.length - 1];
        if (previous && previous.type === type
            && (type !== 'thinking' || segment.activeThinking === previous)) {
            previous.content += value;
            return;
        }
        const step = {type, content: value};
        segment.steps.push(step);
        if (type === 'thinking') segment.activeThinking = step;
    }

    function completeThinking(segment) {
        segment.activeThinking = null;
    }

    function assistantToolStep(segment, data) {
        completeThinking(segment);
        const callId = String(data.call_id || data.tool_call_id || '');
        let step = segment.toolSteps.get(callId);
        if (step) return step;
        step = {
            type: 'tool', tool_call_id: callId,
            name: String(data.name || data.tool || ''),
            arguments: {}, result: '', status: 'running',
        };
        segment.steps.push(step);
        segment.toolSteps.set(callId, step);
        return step;
    }

    function rebuild(state) {
        const messages = [];
        state.order.forEach(runId => {
            const run = state.runs.get(runId);
            if (!run || run.hidden) return;
            run.segments.forEach(segment => {
                if (segment.kind === 'context' || segment.kind === 'recovery') {
                    messages.push({
                        role: 'system', kind: segment.kind === 'recovery' ? 'model_recovery' : 'context_compression',
                        run_id: runId, created_at: segment.createdAt,
                        _seq: segment.firstSeq,
                        ...segment.data,
                    });
                    return;
                }
                if (segment.kind === 'user') {
                    messages.push({
                        role: 'user', content: segment.content, run_id: runId,
                        created_at: segment.createdAt,
                        _seq: segment.firstSeq,
                        ...(segment.messageId
                            ? {message_id: segment.messageId} : {}),
                        ...(segment.attachments.length
                            ? {attachments: segment.attachments.slice()} : {}),
                    });
                    return;
                }
                const contentSteps = segment.steps.filter(step => step.type === 'content');
                const reasoningSteps = segment.steps.filter(step => step.type === 'thinking');
                const toolSteps = segment.steps.filter(step => step.type === 'tool');
                const streamedContent = contentSteps.map(step => step.content).join('');
                const content = segment.terminalContent !== null
                    ? segment.terminalContent
                    : streamedContent;
                if (!content && !reasoningSteps.length
                    && !toolSteps.length && !segment.artifacts.length
                    && !segment.approvals.size && !segment.error) return;
                const message = {
                    role: 'assistant', content, run_id: runId,
                    created_at: segment.createdAt,
                    _seq: segment.firstSeq,
                    segment_id: `${runId}:${segment.firstSeq}`,
                    steps: segment.steps.map(step => ({...step})),
                    streaming: !segment.terminal,
                };
                const activeThinkingIndex = segment.steps.indexOf(segment.activeThinking);
                if (activeThinkingIndex >= 0) {
                    message.thinking_active_index = activeThinkingIndex;
                }
                if (reasoningSteps.length) {
                    message.reasoning = reasoningSteps.map(step => step.content).join('');
                }
                if (toolSteps.length) {
                    message.tool_calls = toolSteps.map(step => ({...step}));
                }
                if (segment.artifacts.length) {
                    message.artifacts = segment.artifacts.map(item => ({...item}));
                }
                if (segment.approvals.size) {
                    message.approvals = Array.from(segment.approvals.values())
                        .map(item => ({...item}));
                }
                if (segment.usage) message.usage = {...segment.usage};
                if (segment.error) message.error = {...segment.error};
                if (segment.status) message.status = segment.status;
                messages.push(message);
            });
        });
        state.pending.forEach(message => {
            messages.push({
                role: 'user', content: message.content, run_id: null,
                message_id: message.messageId, pending: true,
                created_at: message.createdAt,
                _seq: message.firstSeq,
                ...(message.attachments.length
                    ? {attachments: message.attachments.slice()} : {}),
            });
        });
        messages.sort((left, right) => Number(left._seq) - Number(right._seq));
        messages.forEach(message => delete message._seq);
        state.messages = messages;
        return state;
    }

    function reduceEvent(state, event) {
        const data = event.data || {};
        if (event.type === 'agent/inbox/spliced') {
            const message = data.message || {};
            const messageId = String(message.message_id || '');
            if (messageId) {
                state.pending.set(messageId, {
                    messageId,
                    content: String(message.content || ''),
                    createdAt: Number(message.created_at || event.time || 0),
                    firstSeq: Number(event.seq),
                    attachments: Array.isArray(message.attachments)
                        ? message.attachments.slice() : [],
                });
            }
            return rebuild(state);
        }
        if (event.type === 'agent/inbox/claimed') return state;
        const runId = event.run_id == null ? null : String(event.run_id);
        if (!runId) return state;
        if (!state.runs.has(runId)) {
            state.runs.set(runId, newRun(Number(event.seq)));
            state.order.push(runId);
        }
        const run = state.runs.get(runId);
        if (event.type === 'user/message') {
            run.hidden = !!data.console_operation;
            const messageId = String(data.message_id || `run:${runId}:initial`);
            const pending = state.pending.get(messageId);
            state.pending.delete(messageId);
            if (!run.userMessageIds.has(messageId)) {
                run.userMessageIds.add(messageId);
                if (run.currentAssistant) run.currentAssistant.terminal = true;
                run.segments.push({
                    kind: 'user', content: String(data.content || ''),
                    messageId: data.message_id ? messageId : null,
                    createdAt: Number(event.time || 0),
                    firstSeq: pending ? pending.firstSeq : Number(event.seq),
                    attachments: Array.isArray(data.attachments)
                        ? data.attachments.slice() : [],
                });
                run.currentAssistant = null;
            }
        } else if (event.type === 'assistant/text.delta') {
            const assistant = assistantSegment(run, event.time, event.seq);
            completeThinking(assistant);
            appendAssistantText(assistant, 'content', data.text);
        } else if (event.type === 'assistant/reasoning.delta') {
            appendAssistantText(
                assistantSegment(run, event.time, event.seq), 'thinking', data.text
            );
        } else if (event.type === 'assistant/message.end') {
            completeThinking(assistantSegment(run, event.time, event.seq));
        } else if (String(event.type).startsWith('tool/call.')) {
            const assistant = assistantSegment(run, event.time, event.seq);
            const call = assistantToolStep(assistant, data);
            if (event.type === 'tool/call.started') {
                call.name = String(data.name || data.tool || call.name || '');
                call.arguments = data.arguments || {};
            }
            if (event.type === 'tool/call.progress') {
                call.progress = String(data.content || data.text || '');
            }
            if (event.type === 'tool/call.completed') {
                call.result = String(data.result || '');
                call.status = String(data.status || 'success');
            }
            if (event.type === 'tool/call.rejected') {
                call.status = 'rejected';
                call.result = 'Tool is not available to this Agent.';
                call.reason = String(data.reason || 'tool_not_available');
                call.allowed_tools = Array.isArray(data.allowed_tools)
                    ? data.allowed_tools.slice() : [];
            }
        } else if (event.type === 'run/event' && data.kind === 'artifact') {
            assistantSegment(run, event.time, event.seq).artifacts.push({...data.payload});
        } else if (event.type === 'run/progress' && data.phase === 'recovering') {
            const discarded = String(data.discarded_text || '');
            const steps = run.segments.filter(segment => segment.kind === 'assistant')
                .flatMap(segment => segment.steps).filter(step => step.type === 'content');
            if (discarded && steps.map(step => step.content).join('').endsWith(discarded)) {
                let remaining = discarded.length;
                for (const step of steps.reverse()) {
                    const count = Math.min(remaining, step.content.length);
                    step.content = step.content.slice(0, step.content.length - count);
                    remaining -= count;
                    if (!remaining) break;
                }
            }
            if (run.currentAssistant) completeThinking(run.currentAssistant);
            run.currentAssistant = null;
            run.segments.push({kind: 'recovery', data: {...data},
                createdAt: Number(event.time || 0), firstSeq: Number(event.seq)});
        } else if (event.type === 'context/compressed') {
            if (data.compressed !== false) {
                run.segments.push({
                    kind: 'context', data: {...data},
                    createdAt: Number(event.time || 0),
                    firstSeq: Number(event.seq),
                });
            }
        } else if (event.type === 'approval/requested') {
            const assistant = assistantSegment(run, event.time, event.seq);
            completeThinking(assistant);
            const approvalId = String(data.approval_id || '');
            if (approvalId) assistant.approvals.set(approvalId, {...data});
        } else if (event.type === 'approval/resolved') {
            const approvalId = String(data.approval_id || '');
            run.segments.forEach(segment => segment.approvals?.delete(approvalId));
        } else if (['run/completed', 'run/failed', 'run/cancelled'].includes(event.type)) {
            const assistant = assistantSegment(run, event.time, event.seq);
            completeThinking(assistant);
            assistant.terminal = true;
            assistant.status = event.type === 'run/cancelled' ? 'cancelled' : null;
            run.segments.forEach(segment => {
                segment.approvals?.clear();
                if (segment.kind !== 'assistant') return;
                segment.terminal = true;
                completeThinking(segment);
                segment.toolSteps.forEach(tool => {
                    if (!['pending', 'running'].includes(tool.status)) return;
                    tool.status = 'error';
                    tool.result = String(data.error?.message || data.error?.details?.reason
                        || (event.type === 'run/cancelled' ? 'Run cancelled' : 'Run ended before tool completion'));
                });
            });
            if (Object.prototype.hasOwnProperty.call(data, 'content')) {
                let terminalContent = String(data.content || '');
                if (run.segments.some(segment => segment.kind === 'recovery')) {
                    const previous = run.segments.filter(segment =>
                        segment.kind === 'assistant' && segment !== assistant);
                    const prefix = previous.flatMap(segment => segment.steps)
                        .filter(step => step.type === 'content').map(step => step.content).join('');
                    if (terminalContent.startsWith(prefix)) {
                        terminalContent = terminalContent.slice(prefix.length);
                    } else if (terminalContent) {
                        previous.forEach(segment => { segment.terminalContent = ''; });
                    }
                }
                const hasStreamedContent = assistant.steps.some(
                    step => step.type === 'content' && step.content
                );
                if (terminalContent || !hasStreamedContent) {
                    assistant.terminalContent = terminalContent;
                }
            }
            assistant.usage = data.usage || null;
            assistant.error = data.error || null;
        }
        return rebuild(state);
    }

    function inputMessageId(event) {
        if (event.type === 'agent/inbox/spliced') {
            return String(event.data?.message?.message_id || '');
        }
        if (event.type === 'user/message') {
            return String(event.data?.message_id || '');
        }
        return '';
    }

    class SessionViewModel {
        constructor(sessionId, onGap) {
            this.sessionId = String(sessionId);
            this.onGap = typeof onGap === 'function' ? onGap : null;
            this.state = emptyState();
            this.events = new Map();
            this.hiddenRuns = new Set();
            this.baseSeq = 0;
            this.lastSeq = -1;
            this.hasMore = false;
            this.listeners = new Set();
            this.historyGeneration = 0;
            this.historyPending = false;
            this.liveBuffer = new Map();
            this.opened = false;
            this.stagedInputs = new Map();
            this.stagedInputSerial = 0;
        }

        beginHistory() {
            this.historyGeneration += 1;
            this.historyPending = true;
            return this.historyGeneration;
        }

        _rebuild() {
            this.state = emptyState();
            const ordered = Array.from(this.events.values()).sort((a, b) => a.seq - b.seq);
            const authoritativeInputIds = new Set(
                ordered.map(inputMessageId).filter(Boolean)
            );
            authoritativeInputIds.forEach(messageId => {
                this.stagedInputs.delete(messageId);
            });
            ordered.forEach(event => {
                if (event.type === 'user/message'
                    && event.data?.console_operation
                    && event.run_id != null) {
                    this.hiddenRuns.add(String(event.run_id));
                }
            });
            ordered.forEach(event => {
                if (event.run_id != null
                    && this.hiddenRuns.has(String(event.run_id))) return;
                reduceEvent(this.state, event);
            });
            this.stagedInputs.forEach((message, messageId) => {
                if (!this.state.pending.has(messageId)) {
                    this.state.pending.set(messageId, {...message});
                }
            });
            rebuild(this.state);
            this.baseSeq = ordered.length ? Number(ordered[0].seq) : 0;
            this.lastSeq = ordered.length
                ? coveredEnd(ordered[ordered.length - 1]) : -1;
        }

        stagePendingInput(message) {
            const messageId = String(message?.messageId || '');
            if (!messageId) return false;
            const alreadyAuthoritative = Array.from(this.events.values())
                .some(event => inputMessageId(event) === messageId);
            if (alreadyAuthoritative) return false;
            this.stagedInputSerial += 1;
            const staged = {
                messageId,
                content: String(message.content || ''),
                createdAt: Number(message.createdAt || 0),
                firstSeq: this.lastSeq + this.stagedInputSerial / 1000000,
                attachments: Array.isArray(message.attachments)
                    ? message.attachments.slice() : [],
            };
            this.stagedInputs.set(messageId, staged);
            this.state.pending.set(messageId, {...staged});
            rebuild(this.state);
            this.emit();
            return true;
        }

        discardPendingInput(messageId) {
            const key = String(messageId || '');
            if (!this.stagedInputs.delete(key)) return false;
            this.state.pending.delete(key);
            rebuild(this.state);
            this.emit();
            return true;
        }

        installHistory(events, options) {
            return this.installTail(events, options);
        }

        installTail(events, options) {
            const generation = options && options.generation;
            if (generation != null && generation !== this.historyGeneration) return false;
            if (!this.opened) this.events = new Map();
            (options?.hiddenRunIds || []).forEach(runId => {
                this.hiddenRuns.add(String(runId));
            });
            (events || []).slice().sort((a, b) => a.seq - b.seq).forEach(event => {
                const seq = Number(event.seq);
                if (!Number.isInteger(seq) || this.events.has(seq)) return;
                this.events.set(seq, event);
            });
            this._rebuild();
            this.hasMore = !!(options && options.hasMore);
            this.historyPending = false;
            this.opened = true;
            const buffered = Array.from(this.liveBuffer.values())
                .sort((a, b) => a.seq - b.seq);
            this.liveBuffer.clear();
            buffered.forEach(event => this.acceptLive(event));
            this.emit();
            return true;
        }

        prependHistory(events, options) {
            (options?.hiddenRunIds || []).forEach(runId => {
                this.hiddenRuns.add(String(runId));
            });
            (events || []).forEach(event => {
                const seq = Number(event.seq);
                if (Number.isInteger(seq) && seq >= 0 && !this.events.has(seq)) {
                    this.events.set(seq, event);
                }
            });
            this._rebuild();
            this.hasMore = !!(options && options.hasMore);
            this.emit();
            return true;
        }

        abortHistory(generation) {
            if (generation === this.historyGeneration) this.historyPending = false;
        }

        acceptLive(event) {
            const seq = Number(event.seq);
            if (!Number.isInteger(seq) || seq < 0) return 'invalid';
            const authoritativeInputId = inputMessageId(event);
            if (this.historyPending) {
                if (authoritativeInputId) {
                    this.stagedInputs.delete(authoritativeInputId);
                }
                this.liveBuffer.set(seq, event);
                return 'buffered';
            }
            if (this.events.has(seq) || seq <= this.lastSeq) return 'duplicate';
            if (this.lastSeq >= 0 && seq !== this.lastSeq + 1) {
                if (authoritativeInputId) {
                    this.stagedInputs.delete(authoritativeInputId);
                }
                this.liveBuffer.set(seq, event);
                if (this.onGap) this.onGap(this.sessionId, this.lastSeq + 1, seq);
                return 'gap';
            }
            if (event.type === 'user/message'
                && event.data?.console_operation
                && event.run_id != null) {
                this.hiddenRuns.add(String(event.run_id));
            }
            if (authoritativeInputId) this.stagedInputs.delete(authoritativeInputId);
            this.events.set(seq, event);
            if (event.run_id == null
                || !this.hiddenRuns.has(String(event.run_id))) {
                reduceEvent(this.state, event);
            }
            this.lastSeq = seq;
            this.emit();
            return 'applied';
        }

        subscribe(listener) {
            this.listeners.add(listener);
            return () => this.listeners.delete(listener);
        }

        emit() { this.listeners.forEach(listener => listener(this.snapshot())); }

        isRunTerminal(runId) {
            const run = this.state.runs.get(String(runId));
            return !!run && run.segments.some(
                segment => segment.kind === 'assistant' && segment.terminal
            );
        }

        isHiddenRun(runId) {
            return runId != null && this.hiddenRuns.has(String(runId));
        }

        snapshot() {
            const events = Array.from(this.events.values())
                .sort((left, right) => Number(left.seq) - Number(right.seq));
            return {
                session_id: this.sessionId,
                base_seq: this.baseSeq,
                last_seq: this.lastSeq,
                has_more: this.hasMore,
                messages: this.state.messages.map(message => ({...message})),
                events: events.filter(event => event.run_id == null
                    || !this.hiddenRuns.has(String(event.run_id))).map(event => ({
                    ...event,
                    data: {...(event.data || {})},
                })),
            };
        }
    }

    function normalizeSummary(summary) {
        return {
            ...summary,
            running: !!summary.running,
            waiting: !!summary.waiting,
            failed: !!summary.failed,
            completed_unread: !!summary.completed_unread,
            active_run_id: summary.active_run_id == null
                ? null : String(summary.active_run_id),
            pending_interaction: summary.pending_interaction || null,
        };
    }

    class SessionRuntime {
        constructor(onGap) {
            this.models = new Map();
            this.summaries = new Map();
            this.selected = null;
            this.onGap = onGap;
        }

        get(sessionId) {
            const sid = String(sessionId);
            if (!this.models.has(sid)) {
                this.models.set(sid, new SessionViewModel(sid, this.onGap));
            }
            return this.models.get(sid);
        }

        select(sessionId) {
            this.selected = String(sessionId);
            const summary = this.summary(this.selected);
            summary.completed_unread = false;
            this.summaries.set(this.selected, summary);
            return this.get(this.selected);
        }

        updateSummaries(items) {
            (items || []).forEach(item => {
                const sid = String(item.id || item.session_id || '');
                if (!sid) return;
                const previous = this.summaries.get(sid);
                const next = normalizeSummary(item);
                if (previous && (previous.running || previous.waiting)
                    && !next.running && !next.waiting && this.selected !== sid) {
                    next.completed_unread = true;
                } else if (previous && previous.completed_unread && this.selected !== sid) {
                    next.completed_unread = true;
                }
                this.summaries.set(sid, next);
            });
        }

        patchSummary(sessionId, patch) {
            const sid = String(sessionId);
            this.updateSummaries([{id: sid, ...this.summary(sid), ...patch}]);
            return this.summary(sid);
        }

        observeEvent(sessionId, event) {
            if (['run/completed', 'run/failed', 'run/cancelled'].includes(event.type)) {
                this.patchSummary(sessionId, {
                    running: false, waiting: false, active_run_id: null,
                    failed: event.type === 'run/failed', pending_interaction: null,
                });
            } else if (event.type === 'approval/requested') {
                this.patchSummary(sessionId, {
                    running: false, waiting: true, pending_interaction: 'approval',
                });
            } else if (event.type === 'approval/resolved') {
                this.patchSummary(sessionId, {
                    running: true, waiting: false, pending_interaction: null,
                });
            }
        }

        summary(sessionId) {
            return {...normalizeSummary(this.summaries.get(String(sessionId)) || {})};
        }
    }

    class SessionConnection {
        constructor(runtime, options) {
            this.runtime = runtime;
            this.options = options || {};
            this.WebSocketClass = this.options.WebSocketClass || root.WebSocket;
            this.basePath = this.options.basePath || '/api/v1';
            this.sockets = new Map();
            this.closed = false;
            this.retry = new Map();
        }

        connect() {
            this.closed = false;
            this._open('mux');
            this._open('host');
        }

        close() {
            this.closed = true;
            this.sockets.forEach(socket => socket.close());
            this.sockets.clear();
        }

        _url(channel) {
            const scheme = root.location.protocol === 'https:' ? 'wss:' : 'ws:';
            return `${scheme}//${root.location.host}${this.basePath}/events/${channel}`;
        }

        _open(channel) {
            if (this.closed) return;
            const Socket = this.WebSocketClass;
            const socket = new Socket(this._url(channel));
            this.sockets.set(channel, socket);
            socket.onopen = () => this.retry.set(channel, 0);
            socket.onmessage = event => this._message(channel, event.data);
            socket.onclose = () => {
                if (this.closed || this.sockets.get(channel) !== socket) return;
                const attempt = (this.retry.get(channel) || 0) + 1;
                this.retry.set(channel, attempt);
                root.setTimeout(
                    () => this._open(channel),
                    Math.min(5000, 100 * (2 ** attempt)),
                );
            };
        }

        _message(channel, raw) {
            let frame;
            try { frame = JSON.parse(raw); } catch (_) { return; }
            if (channel === 'host' && frame.type === 'host/sessions') {
                this.runtime.updateSummaries(frame.sessions || []);
                this.options.onHost?.(frame.sessions || []);
                return;
            }
            if (channel !== 'mux') return;
            if (frame.type === 'session/subscribed') {
                this.options.onSubscribed?.(
                    String(frame.session_id), Number(frame.last_seq),
                );
                return;
            }
            if (frame.type !== 'session/event' || !frame.event) return;
            const sid = String(frame.session_id);
            const runId = String(frame.event.run_id || '');
            const model = this.runtime.get(sid);
            const outcome = model.acceptLive(frame.event);
            if (model.isHiddenRun(runId)) return;
            this.runtime.observeEvent(sid, frame.event);
            this.options.onEvent?.(sid, frame.event, outcome);
        }
    }

    root.BianbuSessionRuntime = {
        coveredEnd, reduceEvent, SessionViewModel, SessionRuntime, SessionConnection,
    };
})(window);
