/* =====================================================================
   Bianbu Agent Console - Main Application Script
   ===================================================================== */

const PUBLIC_API_BASE = '/api/v1';
const _consoleNativeFetch = window.fetch.bind(window);
let runtimeMaintenanceBlocked = false;

async function pollUpgradeMaintenance() {
    let message = '';
    try {
        const response = await _consoleNativeFetch(`${PUBLIC_API_BASE}/runtime/maintenance`, {
            credentials: 'same-origin', signal: AbortSignal.timeout(5000),
        });
        if (!response.ok) throw new Error('Runtime unavailable');
        const {data} = await response.json();
        runtimeMaintenanceBlocked = data.maintenance === true;
        if (runtimeMaintenanceBlocked || data.state === 'unknown') message = data.message || '';
    } catch (_) {
        runtimeMaintenanceBlocked = true;
        message = 'Runtime 连接已断开，暂时无法操作，正在重新连接；升级结果待确认。';
    }
    let banner = document.getElementById('runtime-maintenance-banner');
    if (!banner) {
        banner = document.createElement('div');
        banner.id = 'runtime-maintenance-banner';
        banner.setAttribute('role', 'status');
        banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9999;padding:12px;background:#fef3c7;color:#78350f;text-align:center';
        document.body.appendChild(banner);
    }
    banner.textContent = message;
    banner.hidden = !message;
    updateSendBtnState();
    window.setTimeout(pollUpgradeMaintenance, 3000);
}
window.addEventListener('DOMContentLoaded', () => { void pollUpgradeMaintenance(); });

// Management resources are protected by the Runtime in production.  Keep the
// browser console usable in both development (no token) and production
// (Bearer token exchanged for an HttpOnly cookie) without duplicating auth
// handling at every form/list call site. Sessions remain public so
// the chat screen can still submit and receive a conversation before an
// operator opens the management pages.
let _adminSessionPromise = null;

function _consoleRequestPath(input) {
    try {
        const raw = typeof input === 'string' ? input : input && input.url;
        return new URL(raw || '', window.location.href).pathname;
    } catch (_) {
        return '';
    }
}

function _consoleManagementRequest(input) {
    const path = _consoleRequestPath(input);
    if (!path.startsWith(`${PUBLIC_API_BASE}/`)) return false;
    if (path === `${PUBLIC_API_BASE}/auth/session`) return false;
    if (path === `${PUBLIC_API_BASE}/runtime/status`) return false;
    for (const prefix of [`${PUBLIC_API_BASE}/sessions`]) {
        if (path === prefix || path.startsWith(`${prefix}/`)) return false;
    }
    return true;
}

async function _exchangeAdminToken() {
    const response = await _consoleNativeFetch(`${PUBLIC_API_BASE}/auth/session`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: window.prompt(
            '此 Runtime 的管理页面需要管理员令牌（Bearer Token）：',
        ) || ''}),
    });
    if (!response.ok) return false;
    const payload = await response.json().catch(() => ({}));
    return payload?.data?.authenticated === true;
}

async function _ensureAdminSession() {
    if (_adminSessionPromise) return _adminSessionPromise;
    _adminSessionPromise = (async () => {
        const current = await _consoleNativeFetch(`${PUBLIC_API_BASE}/auth/session`, {
            credentials: 'same-origin',
        });
        if (current.ok) {
            const payload = await current.json().catch(() => ({}));
            if (payload?.data?.required !== true || payload?.data?.authenticated === true) {
                return true;
            }
        }
        if (current.status !== 401) return false;
        return _exchangeAdminToken();
    })().catch(() => false);
    const result = await _adminSessionPromise;
    _adminSessionPromise = null;
    return result;
}

async function consoleFetch(input, init = {}) {
    const method = String(init.method || input?.method || 'GET').toUpperCase();
    if (runtimeMaintenanceBlocked && _consoleRequestPath(input).startsWith('/api/')
        && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
        throw new Error('Runtime 暂时不可操作，请等待重新连接及升级结束。');
    }
    if (!_consoleManagementRequest(input)) return _consoleNativeFetch(input, init);
    await _ensureAdminSession();
    let response = await _consoleNativeFetch(input, {...init, credentials: 'same-origin'});
    // A rotated/expired cookie gets one interactive re-authentication.  Do
    // not retry indefinitely and do not alter the request body on retry.
    if (response.status === 401 && !init.__adminAuthRetry) {
        _adminSessionPromise = null;
        await _ensureAdminSession();
        response = await _consoleNativeFetch(input, {
            ...init,
            __adminAuthRetry: true,
            credentials: 'same-origin',
        });
    }
    // The control plane intentionally starts before its isolated Worker.  A
    // page loaded during that bounded startup window receives 503
    // ``worker_unavailable`` for catalog/management resources.  Retry only
    // that explicit readiness response; auth failures and real API errors
    // must still reach the caller immediately.  Without this, the first
    // page load after a Runtime restart permanently leaves Tools/Skills/Hook
    // and Plugin panels empty until the user manually refreshes.
    for (let attempt = 0; attempt < 7 && response.status === 503; attempt += 1) {
        let code = '';
        try {
            const payload = await response.clone().json();
            code = String(payload?.error?.code || '');
        } catch (_) {
            code = '';
        }
        if (code !== 'worker_unavailable' && code !== 'worker_starting') break;
        await new Promise(resolve => setTimeout(resolve, Math.min(250 * (2 ** attempt), 2000)));
        response = await _consoleNativeFetch(input, {...init, credentials: 'same-origin'});
    }
    return response;
}

const fetch = consoleFetch;

async function responseJson(response) {
    const payload = await response.json();
    if (!response.ok) {
        const error = payload && payload.error ? payload.error : {};
        return {
            status: 'error',
            code: String(error.code || `http_${response.status}`),
            message: String(error.message || `HTTP ${response.status}`),
            details: error.details || {},
        };
    }
    return {
        status: 'success',
        data: payload.data,
        page: payload.page || null,
        context_start_seq: Number(payload.context_start_seq || 0),
        session_usage: payload.session_usage || null,
    };
}

// Every refused deletion has to reach the user: a dismiss-only dialog keeps the
// Runtime reason on screen even when the row that was clicked is scrolled away.
async function explainRefusedDelete(response, fallback) {
    let message = fallback;
    try {
        const data = await responseJson(response);
        if (data && data.message) message = data.message;
    } catch (_) {
        // Empty or non-JSON bodies keep the actionable fallback text.
    }
    showConfirmDialog({
        title: t('delete_failed_title'),
        message: String(message || fallback),
        okText: t('confirm_got_it'),
        hideCancel: true,
    });
}

function settingsView(payload) {
    const data = payload && payload.data ? payload.data : {};
    const deployment = data.deployment || {};
    const runtime = data.runtime || {};
    return {
        ...deployment,
        ...runtime,
        status: payload.status,
    };
}

// Operational logs keep their dedicated SSE feed. Conversation state uses the
// two shared Harness WebSockets in session-runtime.js.
function openConsoleEventSource(path) {
    return new window.EventSource(path);
}

// =====================================================================
// Version — fetched from backend (single source: /VERSION file)
// =====================================================================
let APP_VERSION = '';

// =====================================================================
// i18n
// =====================================================================
const I18N = {
    zh: {
        console: '控制台',
        nav_chat: '对话', nav_manage: '管理', nav_monitor: '监控',
        menu_chat: '对话', menu_agents: '专家', menu_config: '配置', menu_models: '服务商', menu_bibit_models: '本地模型', menu_tools: '工具', menu_plugins: '插件', menu_security: '安全', menu_mcp: 'MCP', menu_skills: '技能',
        bibit_models_title: '本地模型', bibit_models_desc: '设备上的本地模型由 AI Gateway 提供，本页只读显示状态。', bibit_models_refresh: '刷新',
        bibit_models_llm: '大语言模型', bibit_models_embed: '向量模型',
        bibit_models_running: '运行中', bibit_models_loaded: '已加载', bibit_models_stopped: '未加载', bibit_models_starting: '加载中', bibit_models_unknown: '状态未知',
        bibit_models_not_registered: '未在 AI Gateway 注册', bibit_models_invalid_response: '模型数据无效',
        bibit_models_service_unavailable: '本地模型状态不可用', bibit_models_service_unavailable_hint: '无法读取本地模型状态。请确认设备上的 AI Gateway（127.0.0.1:18790）或 bibit-intent 服务已启动，再点“刷新”。',
        menu_memory: '记忆',
        menu_logs: '日志', menu_agent_top: 'Agent Top', menu_runtime_status: 'Runtime 状态', menu_api_capture: 'API 捕获',
        agents_title: '专家', agents_desc: '创建具有专属指令、模型和能力的专家',
        agents_create: '创建专家', agents_loading: '正在加载专家...',
        agents_refresh_mcp: '刷新 MCP 状态', agents_refresh_mcp_running: '正在刷新 MCP 状态...',
        agents_refresh_mcp_updated: 'MCP 状态已更新：', agents_refresh_mcp_failed: '刷新 MCP 状态失败',
        agents_empty_title: '创建你的第一个专家', agents_empty_desc: '为不同任务定制身份、指令、模型、工具和技能。',
        agents_create_title: '创建专家', agents_edit_title: '编辑专家',
        agents_editor_desc: '定义它是谁、如何思考，以及可以使用哪些能力。',
        agents_avatar: '头像', agents_avatar_presets: '选择预设头像', agents_avatar_upload: '上传图片', agents_avatar_hint: 'PNG/JPEG/WebP/GIF · ≤ 2 MB',
        agents_avatar_uploading: '正在上传头像...', agents_avatar_upload_failed: '头像上传失败',
        agents_name: '调用名', agents_name_placeholder: '例如：ResearchAssistant',
        agents_name_zh: '中文名称', agents_name_zh_placeholder: '例如：研究助理',
        agents_name_en: '英文名称', agents_name_en_placeholder: '例如：Research Assistant',
        agents_description: '简介', agents_description_placeholder: '简要说明这个专家擅长什么',
        agents_description_zh: '中文简介', agents_description_zh_placeholder: '简要说明这个专家擅长什么',
        agents_soul: 'Soul', agents_soul_placeholder: '描述专家、目标、工作方式和输出要求...',
        agents_model: '模型', agents_default_model: '使用默认主模型',
        agents_tools: '工具', agents_skills: '技能', agents_plugins: '插件', agents_mcps: 'MCP', agents_select_all: '全选',
        agents_no_capabilities: '暂无可用项目', agents_name_required: '请填写专家名称',
        agents_capability_preset: '默认能力，所有专家固定启用',
        agents_saved: '专家已保存', agents_save_failed: '保存失败',
        agents_delete_title: '删除专家', agents_delete_confirm: '确定删除这个专家吗？此操作无法撤销。',
        agents_use: '使用并新建会话', agents_chatting_with: '当前主 Agent', agents_bound: '已绑定',
        agents_switch_mention: '输入 @ 切换专家',
        agents_new_session: '新建会话', agents_current_locked: '当前会话已绑定',
        agents_unavailable: 'Agent 不可用', agents_unavailable_hint: '该会话绑定的 Agent 已不可用，请新建会话',
        agents_choose_hint: '请先选择一个专家开始会话',
        model_setup_title: '当前 Agent 尚未配置模型',
        model_setup_steps: '请先添加服务商并填写 API Key，再为当前 Agent 选择模型。配置完成前不能聊天。',
        model_setup_provider: '配置服务商', model_setup_agent: '选择模型',
        model_setup_input_hint: '请先配置服务商和当前 Agent 模型',
        agents_stop_using: '停止使用此专家',
        agents_choose: '选择专家', agents_manage: '管理',
        agents_default: '默认助手', agents_default_desc: '使用全局默认配置',
        agents_switch_hint: '切换专家会开始一个新对话',
        models_title: '服务商',
        models_desc: '配置 Agent 共享的模型服务凭据',
        models_section_vendors: '厂商凭据',
        models_section_vendors_desc: '一处配置，多个模型能力共享',
        models_section_capabilities: '模型能力',
        models_add_vendor: '添加厂商',
        models_provider: '厂商',
        models_model: '模型',
        models_configured: '已配置',
        models_not_configured: '未配置',
        models_pick_to_configure: '选择以配置',
        models_clear_credential: '清除凭据',
        models_base_default_hint: '留空将使用官方默认地址',
        models_base_default: '默认',
        models_custom_vendor_label: '自定义',
        models_custom_name: '名称',
        models_custom_delete: '删除',
        models_custom_delete_confirm_title: '删除自定义厂商',
        models_custom_delete_confirm_msg: '确定删除该自定义厂商吗？此操作无法撤销。',
        models_custom_name_required: '请填写名称',
        models_custom_base_required: '请填写 API Base',
        models_custom_name_duplicate: '该名称已存在，请使用不同的名称',
        models_custom_edit_title: '编辑自定义厂商',
        models_custom_add_title: '添加自定义厂商',
        models_custom_key_placeholder: '输入 API 密钥',
        models_custom_key_configured: '已配置 — 输入新值可替换',
        models_auto_using: '当前优先使用',
        models_strategy_auto: '自动',
        models_pending_config: '待配置',
        models_unavailable: '不可用',
        models_set_via_project: '通过项目模型记录启用',
        models_dim_label: '维度',
        models_save_success: '已保存',
        models_save_failed: '保存失败',
        models_cleared: '已清除',
        models_clear_failed: '清除失败',
        models_pick_provider: '待选择',
        models_clear_confirm_title: '清除厂商凭据',
        models_clear_confirm_msg: '确认清除该厂商的 API Key 与 Base URL 吗？相关能力将不再可用。',
        cancel: '取消',
        save: '保存',
        ok: '确定',
        welcome_subtitle: '我可以帮你解答问题、管理计算机、创造和执行技能，并使用<br>长期记忆提供连续服务',
        example_sys_title: '系统管理', example_sys_text: '查看工作空间里有哪些文件',
        example_code_title: '编程助手', example_code_text: '搜索AI资讯并生成可视化网页报告',
        example_skill_title: '技能系统', example_skill_text: '查看所有支持的工具和技能',
        example_web_title: '指令中心', example_web_text: '查看全部命令',
        stop_active: '停止当前任务',
        skill_picker_title: '临时使用技能',
        skill_picker_search: '搜索全部可用技能',
        skill_picker_loading: '正在加载技能…',
        skill_picker_empty: '没有匹配的可用技能',
        skill_picker_error: '技能加载失败，请重试',
        skill_picker_selected: '已选择',
        skill_picker_close: '关闭技能选择器',
        skill_picker_remove: '移除临时技能',
        skill_picker_selected_skills: '下一条消息临时使用的技能',
        input_placeholder: '输入消息，@ 切换专家，/ 临时使用技能',
        conversation_chat: '对话', conversation_trajectory: '轨迹',
        trajectory_search: '搜索轨迹', trajectory_load_older: '加载更早轨迹',
        trajectory_empty: '当前会话暂无轨迹', trajectory_no_results: '没有匹配的轨迹记录',
        trajectory_turn: '轮次', trajectory_records: '条记录', trajectory_turns: '个轮次',
        trajectory_duration: '耗时', trajectory_input: '输入', trajectory_output: '输出',
        trajectory_metadata: '元数据', trajectory_select: '选择一条记录查看详情',
        trajectory_waiting: '等待', trajectory_nested: '时间已计入上一层记录',
        trajectory_parallel: '与其它记录并行，时间重叠',
        trajectory_covered: '记录覆盖', trajectory_overlap: '与其它记录重叠',
        config_title: '配置管理', config_desc: '管理联网搜索、Agent 和语言配置',
        config_agent: 'Agent 配置',
        config_language: '语言', config_language_hint: '界面展示、命令文案、系统提示词等使用的语言（与右上角切换同步）',
        config_agent_enabled: 'Agent 模式',
        config_max_tokens: '总上下文 Token 覆盖值', config_max_tokens_hint: '显式覆盖模型总窗口（1,000–2,000,000）；系统扣除模型输出预留后得到可用输入容量，未保存时从模型元数据推断',
        config_max_turns: '最大记忆轮次', config_max_turns_hint: '仅保留用于兼容旧配置，不会触发上下文压缩',
        config_context_compression_threshold: '上下文压缩触发阈值 (%)', config_context_compression_threshold_hint: '扣除输出预留后的可用输入容量小于 512K Token 时，有效触发阈值最低为 75%；否则按此比例自动压缩',
        config_max_tokens_auto_placeholder: '自动（从模型元数据推断）',
        config_context_compression_target: '上下文压缩目标比例 (%)', config_context_compression_target_hint: '压缩后上下文相对于有效触发阈值的保留比例',
        config_max_steps: '最大执行步数', config_max_steps_hint: '单次对话中 Agent 最多调用工具的次数',
        config_save: '保存', config_saved: '已保存',
        config_save_error: '保存失败',
        tools_title: '工具管理', tools_desc: '查看 Agent 可调用的内置工具及其状态',
        plugins_title: '插件管理', plugins_desc: '安装和管理本地插件', plugins_install_label: '本地插件目录', plugins_install_placeholder: '/absolute/path/to/plugin', plugins_install: '安装', plugins_loading: '正在加载插件...',
        mcp_title: 'MCP Server', mcp_desc: '用启动命令安装和管理 MCP Server', mcp_name_label: '名称', mcp_name_placeholder: 'Filesystem',
        mcp_system_managed: '系统安装', mcp_command_label: '启动命令', mcp_command_placeholder: 'npx -y @modelcontextprotocol/server-filesystem /data/projects', mcp_install: '安装', mcp_refresh: '刷新',
        mcp_loading: '正在加载 MCP Server...', mcp_empty: '暂无 MCP Server',
        mcp_plugin_owned_note: '插件随包提供的 MCP 在「插件」页随插件显示状态，这里只管理独立 MCP',
        mcp_empty_plugin_owned_only: '这里没有独立 MCP；插件自带的 MCP 在「插件」页显示',
        plugin_mcp_title: '插件自带的 MCP', plugin_mcp_tools: '个工具', plugin_mcp_tools_unknown: '工具数未知',
        plugin_members_mcp_moved: 'MCP 见下方', mcp_installing: '正在安装...', mcp_installed: '安装成功', mcp_install_preparing: '已登记，正在后台启动…', mcp_server_preparing: '后台启动中…', mcp_uninstall: '卸载',
        mcp_uninstall_confirm: '确认卸载这个 MCP Server？', mcp_no_tools: '暂未发现工具', mcp_not_connected: '未连接', mcp_status_unverified: '未验证', mcp_install_unhealthy: '已安装，但没能连上：', mcp_example: '示例', mcp_security_hint: '命令会直接启动程序，不经过 Shell；请只安装可信来源，并限制 Filesystem MCP 可访问的目录。',
        // MCP 生命周期：状态词和“它在干什么”分开说。只报 preparing 时用户
        // 盯着一动不动，也不知道是 116MB 的 npm 安装在跑，还是卡住了。
        mcp_state_not_loaded: '未加载', mcp_state_preparing: '准备中', mcp_state_ready: '就绪', mcp_state_degraded: '降级', mcp_state_failed: '失败', mcp_state_stopped: '已停止', mcp_state_unknown: '未知',
        mcp_phase_installing: '正在安装依赖（首次启动要下载 npm 包，可能需要 1-2 分钟）', mcp_phase_starting: '正在启动并握手…',
        mcp_state_not_loaded_hint: '还没有 Worker 启动它：Runtime 会在 Worker 就绪时自动准备，点「刷新」可立即重试。',
        mcp_state_unknown_hint: '暂时读不到 Worker 的加载状态，点「刷新」重试。',
        skills_title: '技能管理', skills_desc: '查看 Agent 技能；请在专家配置中选择启用项',
        skills_loading: '加载技能中...', skills_loading_desc: '技能加载后将显示在此处',
        tools_section_title: '内置工具', tools_loading: '加载工具中...',
        skills_section_title: '技能',
        memory_title: '记忆管理', memory_desc: '查看 Agent 记忆文件和内容',
        memory_tab_files: '记忆文件',
        memory_loading: '加载记忆文件中...', memory_loading_desc: '记忆文件将显示在此处',
        memory_back: '返回列表',
        memory_col_name: '文件名', memory_col_type: '类型', memory_col_size: '大小', memory_col_updated: '更新时间',
        agent_top_title: 'Agent Top', agent_top_desc: '实时查看 Agent、Session 与 Run 状态', agent_top_refresh: '刷新', agent_top_overview: '运行概览', agent_top_updated: '更新时间', agent_top_agents: 'Agents', agent_top_agents_desc: 'Agent 可用性与工作负载', agent_top_recent_runs: '最近 Runs', agent_top_runs_desc: '最新执行活动', runtime_status_title: 'Runtime 状态', runtime_status_desc: '查看 Control 进程与 Agent Worker 的运行关系', runtime_status_refresh: '刷新', runtime_status_overview: '运行概览', runtime_status_control: 'Control 进程', runtime_status_control_desc: '控制面进程负责 API、会话和 Worker 调度', runtime_status_workers: 'Worker Pool', runtime_status_workers_desc: '每个 Agent 对应一个受管 Worker 进程',
        runtime_status_user: '运行身份', runtime_status_data_dir: '数据目录',
        runtime_status_identity_desc: '系统级服务整机一份，Control、Worker 和控制台都读写同一个运行身份的数据目录',
        logs_title: '日志', logs_desc: '运行日志 (runtime.jsonl)',
        logs_live: '最近', logs_coming_msg: '正在加载最近的运行日志…',
        logs_empty: '暂无日志记录。',
        capture_title: 'API 捕获', capture_desc: '查看指定服务商的有界 LLM 请求与响应记录。',
        capture_warning: '即使凭据已脱敏，捕获的提示词与响应仍可能包含敏感内容。', capture_provider: '服务商',
        capture_start: '开始捕获', capture_switch: '切换捕获', capture_stop: '停止捕获', capture_clear: '清空记录',
        capture_active: '当前目标：', capture_inactive: '未捕获', capture_calls: '捕获记录', capture_empty: '暂无捕获调用',
        capture_open: '打开详情', capture_detail_title: '捕获的 API 调用', capture_close: '关闭', capture_input: '输入', capture_output: '输出', capture_http_request_body: '原始 HTTP 请求 Body', capture_http_response_body: '原始 HTTP 响应 Body', capture_copy: '复制', capture_copied: '已复制', capture_copy_failed: '复制失败',
        capture_request_friendly: '消息与工具', capture_response_assembled: '拼接后的响应', capture_content: '内容', capture_description: '描述', capture_tools: '工具定义', capture_tool_call: '工具调用', capture_tool_result: '工具结果', capture_empty_friendly: '没有可展示的内容', capture_choice: '响应分支', capture_call_id: '调用 ID', capture_arguments: '参数', capture_parameters: '参数 Schema',
        capture_loading: '正在加载捕获记录…', capture_saved: '捕获目标已更新。', capture_stopped: '已停止捕获。', capture_cleared: '记录已清空。',
        capture_preview_truncated: '预览已截断（省略 {count} 字节；显示头部和尾部）', capture_detail_truncated: '列表预览已截断；下方已按需加载完整的有界记录。',
        capture_agent: '专家', capture_run: '运行', capture_duration: '耗时', capture_characters: '字符', capture_tokens: 'Token',
        capture_input_short: '输入', capture_output_short: '输出', capture_status_success: '成功', capture_status_error: '错误',
        capture_status_failed: '失败', capture_status_running: '运行中', capture_status_in_progress: '进行中', capture_catalog_loading: '正在加载服务商…',
        new_chat: '新对话',
        session_history: '历史会话',
        today: '今天', yesterday: '昨天', earlier: '更早',
        delete_session_confirm: '确认删除该会话？所有消息将被清除。',
        delete_session_title: '删除会话',
        delete_session_failed: '删除会话失败，请稍后重试。',
        delete_failed_title: '删除失败',
        confirm_got_it: '知道了',
        mcp_uninstall_failed: '卸载 MCP Server 失败，请稍后重试。',
        plugin_delete_failed: '删除插件失败，请稍后重试。',
        rename_session: '重命名',
        delete_message_confirm: '确认删除这条消息？',
        delete_message_title: '删除消息',
        edit_disabled_reply_active: '正在生成回复，暂时无法编辑。',
        delete_disabled_reply_active: '正在生成回复，暂时无法删除。',
        untitled_session: '新对话',
        context_cleared: '— 以上内容已从上下文中移除 —',
        context_compressed: '上下文已从 {before} Token 压缩到 {after} Token',
        context_compression_detail: '保留 {messages} 条消息 · 第 {count} 次压缩',
        context_compression_fallback: '摘要模型不可用，已使用安全回退摘要',
        context_compression_accuracy_warning: '多次压缩准确率可能下降',
        token_input: '输入', token_output: '输出', token_total: '合计',
        tip_new_chat: '新建对话',
        tip_clear_context: '清除上下文',
        tip_attach: '添加附件',
        attach_menu_file: '上传文件',
        image_model_unsupported: '当前模型不支持图片,请切换支持图片的模型',
        mic_idle_title: '点击录音 / 再按一次结束',
        mic_recording_title: '录音中，再次点击结束',
        mic_busy_title: '识别中…',
        mic_permission_denied: '无法访问麦克风，请检查浏览器权限',
        mic_too_short: '录音太短，请重试',
        mic_error: '语音识别失败',
        speak_msg: '朗读这段回复',
        voice_reply_mode_label: '语音回复策略',
        voice_reply_off: '关闭',
        voice_reply_if_voice: '仅语音问/语音答',
        voice_reply_always: '总是语音回复',
        attach_menu_folder: '上传文件夹',
        confirm_yes: '确认',
        confirm_cancel: '取消',
        approval_title: '需要你的批准',
        approval_description: 'Agent 请求执行以下操作',
        approval_approve: '批准',
        approval_reject: '拒绝',
        approval_processing: '处理中…',
        approval_approved: '已批准',
        approval_rejected: '已拒绝',
        approval_failed: '审批失败，请重试',
        approval_input_blocked: '请先批准或拒绝待处理操作',
        error_send: '发送失败，请稍后再试。', error_timeout: '请求超时，请再试一次。',
        security_denied_title: '操作被安全策略拦截',
        security_denied_hint: '这不是临时故障，重试不会改变结果。如确需此操作，请在 bianbu-security 配置台为该能力授权。',
        dlp_unavailable_title: '数据安全检查不可用，已阻止外发',
        dlp_unavailable_hint: 'DLP 扫描未能完成。请检查 bianbu-securityd 服务、客户端连接和 Worker 身份校验日志，修复后重试。能力授权不能解决扫描故障。',
        dlp_denied_title: '数据被 DLP 策略拦截',
        dlp_denied_hint: '请移除受限敏感内容，或由管理员检查对应 Agent 的 DLP 数据策略。',
        security_unavailable_title: '安全服务不可用，高危操作已拦截',
        security_unavailable_hint: '判权服务（bianbu-securityd）暂时不可达，高危能力按 fail-closed 拦截。请检查服务状态后重试。',
        network_policy_blocked_title: '无法连接到模型服务',
        network_policy_blocked_hint: '请在安全控制中心的能力管理页更新该工具的策略或网络范围，保存后重启此 Agent。',
        provider_connection_failed_title: '模型服务暂时无法连接',
        provider_connection_failed_hint: '请检查网络、Provider 地址和服务状态后重试；若刚改过网络策略，需重启此 Agent 才会生效。',
        provider_timeout_title: '模型服务响应超时',
        provider_timeout_hint: 'Provider 收下了请求但在超时时间内没有返回内容。请检查网络与服务状态后重试；若该模型一贯较慢，请更换更快的模型或 Provider。',
        security_field_tool: '工具',
        security_field_policy: '策略',
        security_field_provider: '模型服务', security_field_network_mode: '网络策略',
        security_field_domains: '白名单', security_field_timeout: '超时时间(秒)',
        thinking_in_progress: '思考中...', thinking_done: '已深度思考', thinking_duration: '耗时',
        edit_message: '编辑消息',
        regenerate_response: '重新生成',
        edit_save: '保存并发送',
        edit_cancel: '取消',
    },
    en: {
        console: 'Console',
        nav_chat: 'Chat', nav_manage: 'Management', nav_monitor: 'Monitor',
        menu_chat: 'Chat', menu_agents: 'Experts', menu_config: 'Config', menu_models: 'Providers', menu_bibit_models: 'Local models', menu_tools: 'Tools', menu_plugins: 'Plugins', menu_security: 'Security', menu_mcp: 'MCP', menu_skills: 'Skills',
        bibit_models_title: 'Local models', bibit_models_desc: 'On-device models are served by the AI Gateway; this page shows their state read-only.', bibit_models_refresh: 'Refresh',
        bibit_models_llm: 'Large language model', bibit_models_embed: 'Embedding model',
        bibit_models_running: 'Running', bibit_models_loaded: 'Loaded', bibit_models_stopped: 'Not loaded', bibit_models_starting: 'Loading', bibit_models_unknown: 'Unknown state',
        bibit_models_not_registered: 'Not registered in the AI Gateway', bibit_models_invalid_response: 'Invalid model data',
        bibit_models_service_unavailable: 'Local model state unavailable', bibit_models_service_unavailable_hint: 'The local model state could not be read. Make sure the AI Gateway (127.0.0.1:18790) or bibit-intent service is running on the device, then press Refresh.',
        menu_memory: 'Memory',
        menu_logs: 'Logs', menu_agent_top: 'Agent Top', menu_runtime_status: 'Runtime Status', menu_api_capture: 'API Capture',
        agents_title: 'Experts', agents_desc: 'Create experts with their own instructions, model, and capabilities',
        agents_create: 'Create Expert', agents_loading: 'Loading experts...',
        agents_refresh_mcp: 'Refresh MCP status', agents_refresh_mcp_running: 'Refreshing MCP status...',
        agents_refresh_mcp_updated: 'MCP status updated:', agents_refresh_mcp_failed: 'Could not refresh MCP status',
        agents_empty_title: 'Create your first expert', agents_empty_desc: 'Customize an identity, instructions, model, tools, and skills for every job.',
        agents_create_title: 'Create Expert', agents_edit_title: 'Edit Expert',
        agents_editor_desc: 'Define who it is, how it works, and which capabilities it can use.',
        agents_avatar: 'Avatar', agents_avatar_presets: 'Choose a preset', agents_avatar_upload: 'Upload image', agents_avatar_hint: 'PNG/JPEG/WebP/GIF · ≤ 2 MB',
        agents_avatar_uploading: 'Uploading avatar...', agents_avatar_upload_failed: 'Avatar upload failed',
        agents_name: 'Call name', agents_name_placeholder: 'e.g. ResearchAssistant',
        agents_name_zh: 'Chinese name', agents_name_zh_placeholder: 'e.g. 研究助理',
        agents_name_en: 'English name', agents_name_en_placeholder: 'e.g. Research Assistant',
        agents_description: 'Description', agents_description_placeholder: 'What is this expert especially good at?',
        agents_description_zh: 'Chinese description', agents_description_zh_placeholder: 'What is this expert especially good at, in Chinese?',
        agents_soul: 'Soul', agents_soul_placeholder: "Describe the expert's goals, workflow, and output requirements...",
        agents_model: 'Model', agents_default_model: 'Use default main model',
        agents_tools: 'Tools', agents_skills: 'Skills', agents_plugins: 'Plugins', agents_mcps: 'MCP', agents_select_all: 'Select all',
        agents_no_capabilities: 'No items available', agents_name_required: 'Enter an expert name',
        agents_capability_preset: 'Default capability, always on for every expert',
        agents_saved: 'Expert saved', agents_save_failed: 'Could not save expert',
        agents_delete_title: 'Delete Expert', agents_delete_confirm: 'Delete this expert? This action cannot be undone.',
        agents_use: 'Use in new session', agents_chatting_with: 'Main Agent', agents_bound: 'Bound',
        agents_switch_mention: 'Type @ to switch Experts',
        agents_new_session: 'New session', agents_current_locked: 'Bound to current session',
        agents_unavailable: 'Agent unavailable', agents_unavailable_hint: 'The Agent bound to this session is unavailable. Start a new session.',
        agents_choose_hint: 'Choose an expert to start a session',
        model_setup_title: 'The selected Agent has no model',
        model_setup_steps: 'Add a provider and API Key, then select a model for this Agent. Chat is disabled until setup is complete.',
        model_setup_provider: 'Configure provider', model_setup_agent: 'Select model',
        model_setup_input_hint: 'Configure a provider and model for this Agent first',
        agents_stop_using: 'Stop using this Expert',
        agents_choose: 'Choose Expert', agents_manage: 'Manage',
        agents_default: 'Default assistant', agents_default_desc: 'Use the global default configuration',
        agents_switch_hint: 'Switching experts starts a new chat',
        models_title: 'Providers',
        models_desc: 'Configure model-service credentials shared by Agents',
        models_section_vendors: 'Provider Credentials',
        models_section_vendors_desc: 'Configured once, shared by multiple model capabilities',
        models_section_capabilities: 'Capabilities',
        models_add_vendor: 'Add Provider',
        models_provider: 'Provider',
        models_model: 'Model',
        models_configured: 'configured',
        models_not_configured: 'not configured',
        models_pick_to_configure: 'pick to configure',
        models_clear_credential: 'Clear credentials',
        models_base_default_hint: 'Leave blank to use the official default base URL',
        models_base_default: 'Default',
        models_custom_vendor_label: 'Custom',
        models_custom_name: 'Name',
        models_custom_delete: 'Delete',
        models_custom_delete_confirm_title: 'Delete custom provider',
        models_custom_delete_confirm_msg: 'Delete this custom provider? This cannot be undone.',
        models_custom_name_required: 'Name is required',
        models_custom_base_required: 'API Base is required',
        models_custom_name_duplicate: 'This name already exists, please use a different name',
        models_custom_edit_title: 'Edit custom provider',
        models_custom_add_title: 'Add custom provider',
        models_custom_key_placeholder: 'Enter API key',
        models_custom_key_configured: 'Configured — enter a new value to replace',
        models_auto_using: 'Preferred',
        models_strategy_auto: 'auto',
        models_pending_config: 'Pending setup',
        models_unavailable: 'unavailable',
        models_set_via_project: 'enable through project Model records',
        models_dim_label: 'dim',
        models_save_success: 'Saved',
        models_save_failed: 'Save failed',
        models_cleared: 'Cleared',
        models_clear_failed: 'Clear failed',
        models_pick_provider: 'Pick a provider',
        models_clear_confirm_title: 'Clear provider credentials',
        models_clear_confirm_msg: 'Remove this provider\'s API Key and Base URL? Capabilities relying on it will stop working.',
        cancel: 'Cancel',
        save: 'Save',
        ok: 'OK',
        welcome_subtitle: 'I can help answer questions, manage your computer, create and run skills, and provide continuity through <br> long-term memory.',
        example_sys_title: 'System', example_sys_text: 'Show me the files in the workspace',
        example_code_title: 'Coding', example_code_text: 'Search today\'s AI news and generate a visual report webpage',
        example_skill_title: 'Skills', example_skill_text: 'Show current tools and skills',
        example_web_title: 'Commands', example_web_text: 'Show all commands',
        stop_active: 'Stop active task',
        skill_picker_title: 'Use skills temporarily',
        skill_picker_search: 'Search all available skills',
        skill_picker_loading: 'Loading skills…',
        skill_picker_empty: 'No matching available skills',
        skill_picker_error: 'Could not load skills. Try again.',
        skill_picker_selected: 'Selected',
        skill_picker_close: 'Close skill picker',
        skill_picker_remove: 'Remove temporary skill',
        skill_picker_selected_skills: 'Skills used temporarily for the next message',
        input_placeholder: 'Type a message, @ to switch Experts, / to use a skill temporarily',
        conversation_chat: 'Chat', conversation_trajectory: 'Trajectory',
        trajectory_search: 'Search trajectory', trajectory_load_older: 'Load earlier trajectory',
        trajectory_empty: 'No trajectory for this session', trajectory_no_results: 'No matching trajectory records',
        trajectory_turn: 'Turn', trajectory_records: 'records', trajectory_turns: 'turns',
        trajectory_duration: 'Duration', trajectory_input: 'Input', trajectory_output: 'Output',
        trajectory_metadata: 'Metadata', trajectory_select: 'Select a record to inspect it',
        trajectory_waiting: 'Waiting', trajectory_nested: 'Time counted in the record above',
        trajectory_parallel: 'Runs in parallel with another record',
        trajectory_covered: 'Recorded', trajectory_overlap: 'overlapping other records',
        config_title: 'Configuration', config_desc: 'Manage web search, agent, and language settings',
        config_agent: 'Agent Configuration',
        config_language: 'Language', config_language_hint: 'Language for the UI, command text, system prompts and more (synced with the top-right switch)',
        config_agent_enabled: 'Agent Mode',
        config_max_tokens: 'Total Context Token Override', config_max_tokens_hint: 'Explicit model window override (1,000–2,000,000); output reserve is deducted for usable input, or model metadata is inferred when not stored',
        config_max_turns: 'Max Memory Turns', config_max_turns_hint: 'Retained for compatibility only; it does not trigger context compression',
        config_context_compression_threshold: 'Compression Threshold (%)', config_context_compression_threshold_hint: 'When usable input capacity after output reserve is below 512K tokens, the effective threshold is at least 75%; otherwise this percentage applies',
        config_max_tokens_auto_placeholder: 'Auto (infer from model metadata)',
        config_context_compression_target: 'Compression Target (%)', config_context_compression_target_hint: 'Percentage of the effective compression threshold retained after compression',
        config_max_steps: 'Max Steps', config_max_steps_hint: 'Max tool calls the Agent can make in a single conversation',
        config_save: 'Save', config_saved: 'Saved',
        config_save_error: 'Save failed',
        tools_title: 'Tools', tools_desc: 'View built-in tools available to agents and their status',
        plugins_title: 'Plugins', plugins_desc: 'Install and manage local plugins', plugins_install_label: 'Local plugin directory', plugins_install_placeholder: '/absolute/path/to/plugin', plugins_install: 'Install', plugins_loading: 'Loading plugins...',
        mcp_title: 'MCP Servers', mcp_desc: 'Install and manage MCP servers with launch commands', mcp_name_label: 'Name', mcp_name_placeholder: 'Filesystem',
        mcp_system_managed: 'System installed', mcp_command_label: 'Launch command', mcp_command_placeholder: 'npx -y @modelcontextprotocol/server-filesystem /data/projects', mcp_install: 'Install', mcp_refresh: 'Refresh',
        mcp_loading: 'Loading MCP servers...', mcp_empty: 'No MCP servers installed',
        mcp_plugin_owned_note: 'MCP servers shipped by a Plugin are listed with that Plugin on the Plugins page; only standalone servers are managed here',
        mcp_empty_plugin_owned_only: 'No standalone MCP servers here; Plugin-owned servers are shown on the Plugins page',
        plugin_mcp_title: 'MCP servers from this Plugin', plugin_mcp_tools: 'tools', plugin_mcp_tools_unknown: 'tool count unknown',
        plugin_members_mcp_moved: 'see MCP below', mcp_installing: 'Installing...', mcp_installed: 'Installed', mcp_install_preparing: 'Registered; starting in the background...', mcp_server_preparing: 'starting in the background...', mcp_uninstall: 'Uninstall',
        mcp_uninstall_confirm: 'Uninstall this MCP server?', mcp_no_tools: 'No tools discovered', mcp_not_connected: 'not connected', mcp_status_unverified: 'unverified', mcp_install_unhealthy: 'Installed, but it did not connect:', mcp_example: 'Example', mcp_security_hint: 'Commands start programs directly without a shell. Install only trusted sources and restrict directories exposed to Filesystem MCP.',
        // The lifecycle keeps "what state" and "what it is doing" apart: a bare
        // "preparing" never told an operator whether npm was downloading or
        // the server was stuck.
        mcp_state_not_loaded: 'Not loaded', mcp_state_preparing: 'Preparing', mcp_state_ready: 'Ready', mcp_state_degraded: 'Degraded', mcp_state_failed: 'Failed', mcp_state_stopped: 'Stopped', mcp_state_unknown: 'Unknown',
        mcp_phase_installing: 'Installing dependencies (the first start downloads the npm package and can take 1-2 minutes)', mcp_phase_starting: 'Starting and handshaking...',
        mcp_state_not_loaded_hint: 'No Worker has started it yet: the Runtime prepares it as soon as one is ready, or press Refresh to retry now.',
        mcp_state_unknown_hint: 'The Worker has not reported a status yet; refresh to retry.',
        skills_title: 'Skills', skills_desc: 'View Agent skills; choose enabled skills in Expert settings',
        skills_loading: 'Loading skills...', skills_loading_desc: 'Skills will be displayed here after loading',
        tools_section_title: 'Built-in Tools', tools_loading: 'Loading tools...',
        skills_section_title: 'Skills',
        memory_title: 'Memory', memory_desc: 'View agent memory files and contents',
        memory_tab_files: 'Memory Files',
        memory_loading: 'Loading memory files...', memory_loading_desc: 'Memory files will be displayed here',
        memory_back: 'Back to list',
        memory_col_name: 'Filename', memory_col_type: 'Type', memory_col_size: 'Size', memory_col_updated: 'Updated',
        agent_top_title: 'Agent Top', agent_top_desc: 'Live Agent, Session, and Run activity', agent_top_refresh: 'Refresh', agent_top_overview: 'Runtime overview', agent_top_updated: 'Updated', agent_top_agents: 'Agents', agent_top_agents_desc: 'Agent availability and workload', agent_top_recent_runs: 'Recent Runs', agent_top_runs_desc: 'Latest execution activity', runtime_status_title: 'Runtime Status', runtime_status_desc: 'See how the Control process relates to Agent Workers', runtime_status_refresh: 'Refresh', runtime_status_overview: 'Runtime overview', runtime_status_control: 'Control Process', runtime_status_control_desc: 'Owns the API, sessions, and Worker scheduling', runtime_status_workers: 'Worker Pool', runtime_status_workers_desc: 'Each Agent is served by one managed Worker process',
        runtime_status_user: 'Runtime user', runtime_status_data_dir: 'Data directory',
        runtime_status_identity_desc: 'One system-level service per device: the control plane, every Worker and this console read and write the same run identity and data directory',
        logs_title: 'Logs', logs_desc: 'Runtime log (runtime.jsonl)',
        logs_live: 'Recent', logs_coming_msg: 'Loading the recent runtime log…',
        logs_empty: 'No log records yet.',
        capture_title: 'API Capture', capture_desc: 'Inspect bounded LLM request and response records for one Provider.',
        capture_warning: 'Captured prompts and responses may contain sensitive content even after credentials are redacted.', capture_provider: 'Provider',
        capture_start: 'Start capture', capture_switch: 'Switch capture', capture_stop: 'Stop capture', capture_clear: 'Clear records',
        capture_active: 'Active target:', capture_inactive: 'Not capturing', capture_calls: 'Captured calls', capture_empty: 'No captured calls',
        capture_open: 'Open detail', capture_detail_title: 'Captured API call', capture_close: 'Close', capture_input: 'Input', capture_output: 'Output', capture_http_request_body: 'Raw HTTP request body', capture_http_response_body: 'Raw HTTP response body', capture_copy: 'Copy', capture_copied: 'Copied', capture_copy_failed: 'Copy failed',
        capture_request_friendly: 'Messages and tools', capture_response_assembled: 'Assembled response', capture_content: 'Content', capture_description: 'Description', capture_tools: 'Tool definitions', capture_tool_call: 'Tool call', capture_tool_result: 'Tool result', capture_empty_friendly: 'No displayable content', capture_choice: 'Choice', capture_call_id: 'Call ID', capture_arguments: 'Arguments', capture_parameters: 'Parameter schema',
        capture_loading: 'Loading capture records…', capture_saved: 'Capture target updated.', capture_stopped: 'Capture stopped.', capture_cleared: 'Records cleared.',
        capture_preview_truncated: 'Preview truncated ({count} bytes omitted; head and tail shown)', capture_detail_truncated: 'The list preview was truncated; the full bounded record is loaded below.',
        capture_agent: 'Agent', capture_run: 'Run', capture_duration: 'Duration', capture_characters: 'chars', capture_tokens: 'Tokens',
        capture_input_short: 'Input', capture_output_short: 'Output', capture_status_success: 'Success', capture_status_error: 'Error',
        capture_status_failed: 'Failed', capture_status_running: 'Running', capture_status_in_progress: 'In progress', capture_catalog_loading: 'Loading Providers…',
        new_chat: 'New Chat',
        session_history: 'History',
        today: 'Today', yesterday: 'Yesterday', earlier: 'Earlier',
        delete_session_confirm: 'Delete this session? All messages will be removed.',
        delete_session_title: 'Delete Session',
        delete_session_failed: 'Could not delete the session. Please retry.',
        delete_failed_title: 'Delete failed',
        confirm_got_it: 'OK',
        mcp_uninstall_failed: 'Could not uninstall the MCP Server. Please retry.',
        plugin_delete_failed: 'Could not delete the Plugin. Please retry.',
        rename_session: 'Rename',
        delete_message_confirm: 'Delete this message?',
        delete_message_title: 'Delete Message',
        edit_disabled_reply_active: 'Reply is being generated; editing is temporarily unavailable.',
        delete_disabled_reply_active: 'Reply is being generated; deletion is temporarily unavailable.',
        untitled_session: 'New Chat',
        context_cleared: '— Context above has been cleared —',
        context_compressed: 'Context compressed from {before} to {after} tokens',
        context_compression_detail: 'Retained {messages} messages · Compression #{count}',
        context_compression_fallback: 'Summary model unavailable; used the safe fallback summary',
        context_compression_accuracy_warning: 'Repeated compression may reduce accuracy',
        token_input: 'Input', token_output: 'Output', token_total: 'Total',
        tip_new_chat: 'New Chat',
        tip_clear_context: 'Clear Context',
        tip_attach: 'Add Attachment',
        attach_menu_file: 'Upload File',
        image_model_unsupported: 'The current model does not support images. Switch to a model that accepts images.',
        mic_idle_title: 'Click to record, click again to stop',
        mic_recording_title: 'Recording, click to stop',
        mic_busy_title: 'Transcribing…',
        mic_permission_denied: 'Cannot access microphone — check browser permissions',
        mic_too_short: 'Recording too short, please retry',
        mic_error: 'Speech recognition failed',
        speak_msg: 'Read this reply aloud',
        voice_reply_mode_label: 'Voice reply policy',
        voice_reply_off: 'Off',
        voice_reply_if_voice: 'Voice only if voice input',
        voice_reply_always: 'Always reply with voice',
        attach_menu_folder: 'Upload Folder',
        confirm_yes: 'Confirm',
        confirm_cancel: 'Cancel',
        approval_title: 'Approval required',
        approval_description: 'The agent wants to perform these actions',
        approval_approve: 'Approve',
        approval_reject: 'Reject',
        approval_processing: 'Processing…',
        approval_approved: 'Approved',
        approval_rejected: 'Rejected',
        approval_failed: 'Approval failed. Please retry.',
        approval_input_blocked: 'Approve or reject the pending action first',
        error_send: 'Failed to send. Please try again.', error_timeout: 'Request timeout. Please try again.',
        security_denied_title: 'Blocked by security policy',
        security_denied_hint: 'This is not a transient failure — retrying will not change the result. If this operation is needed, grant the capability in the bianbu-security console.',
        dlp_unavailable_title: 'Data security check unavailable; transfer blocked',
        dlp_unavailable_hint: 'The DLP scan could not complete. Check bianbu-securityd, the client connection, and Worker identity validation logs, then retry after resolving the failure. Capability grants do not resolve scanner failures.',
        dlp_denied_title: 'Data blocked by DLP policy',
        dlp_denied_hint: 'Remove restricted sensitive content or ask an administrator to review this Agent’s DLP policy.',
        security_unavailable_title: 'Security service unavailable; high-risk operation blocked',
        security_unavailable_hint: 'The authorization service (bianbu-securityd) is unreachable, so high-risk capabilities are blocked fail-closed. Check the service and retry.',
        network_policy_blocked_title: 'Unable to connect to the model service',
        network_policy_blocked_hint: 'Update the tool policy or network scope in Security Control Center > Capabilities, then restart this Agent.',
        provider_connection_failed_title: 'Model service is temporarily unreachable',
        provider_connection_failed_hint: 'Check the network, Provider URL, and service status, then retry. If you just changed the network policy, restart this Agent for it to take effect.',
        provider_timeout_title: 'Model service timed out',
        provider_timeout_hint: 'The Provider accepted the request but returned nothing before the timeout. Check the network and service status, then retry; if this model is consistently slow, switch to a faster model or Provider.',
        security_field_tool: 'Tool',
        security_field_policy: 'Policy',
        security_field_provider: 'Model service', security_field_network_mode: 'Network policy',
        security_field_domains: 'Allowlist', security_field_timeout: 'Timeout (s)',
        thinking_in_progress: 'Thinking...', thinking_done: 'Thought', thinking_duration: 'Duration',
        edit_message: 'Edit message',
        regenerate_response: 'Regenerate',
        edit_save: 'Save and send',
        edit_cancel: 'Cancel',
    }
};

// Resolve language by priority: browser-only choice (localStorage) -> the
// config.yaml product default -> browser language -> 'zh'. Shares
// __bianbuResolveLang__ defined in chat.html; falls back locally if standalone.
let currentLang = (typeof window.__bianbuResolveLang__ === 'function')
    ? window.__bianbuResolveLang__()
    : (function () {
        const norm = (raw) => {
            if (!raw) return '';
            const v = String(raw).trim().toLowerCase();
            if (v === 'auto') return '';
            if (v.indexOf('zh') === 0) return 'zh';
            if (v.indexOf('en') === 0) return 'en';
            return '';
        };
        return norm(localStorage.getItem('bianbu_lang'))
            || norm(window.__BIANBU_DEFAULT_LANG__)
            || norm(navigator.language)
            || 'zh';
    })();

function t(key) {
    return (I18N[currentLang] && I18N[currentLang][key]) || (I18N.en[key]) || key;
}

const sessionTokenUsageGeneration = new Map();

function beginSessionTokenUsageRefresh(ownerSession) {
    const key = String(ownerSession || '');
    const generation = (sessionTokenUsageGeneration.get(key) || 0) + 1;
    sessionTokenUsageGeneration.set(key, generation);
    return generation;
}

function isCurrentSessionTokenUsageRefresh(ownerSession, generation) {
    return sessionTokenUsageGeneration.get(String(ownerSession || '')) === generation;
}

function normalizeTokenUsage(value) {
    if (!value || typeof value !== 'object') return null;
    const fields = ['input_tokens', 'output_tokens', 'total_tokens'];
    const normalized = {};
    for (const field of fields) {
        if (value[field] === null || value[field] === undefined || value[field] === '') {
            return null;
        }
        const number = Number(value[field]);
        if (!Number.isSafeInteger(number) || number < 0) {
            return null;
        }
        normalized[field] = number;
    }
    return normalized;
}

function tokenNumber(value) {
    const locale = currentLang === 'zh' ? 'zh-CN' : 'en-US';
    return new Intl.NumberFormat(locale).format(value);
}

function renderTokenUsageElement(element) {
    if (!element) return;
    const usage = normalizeTokenUsage({
        input_tokens: element.dataset.inputTokens,
        output_tokens: element.dataset.outputTokens,
        total_tokens: element.dataset.totalTokens,
    });
    if (!usage) {
        element.hidden = true;
        element.textContent = '';
        return;
    }
    element.textContent = [
        `${t('token_input')} ${tokenNumber(usage.input_tokens)}`,
        `${t('token_output')} ${tokenNumber(usage.output_tokens)}`,
        `${t('token_total')} ${tokenNumber(usage.total_tokens)} tokens`,
    ].join(' · ');
    element.hidden = false;
}

function createTokenUsageElement(value, className) {
    const usage = normalizeTokenUsage(value);
    if (!usage) return null;
    const element = document.createElement('span');
    element.className = className;
    element.dataset.inputTokens = String(usage.input_tokens);
    element.dataset.outputTokens = String(usage.output_tokens);
    element.dataset.totalTokens = String(usage.total_tokens);
    renderTokenUsageElement(element);
    return element;
}

function attachTokenUsage(botEl, value) {
    const usage = normalizeTokenUsage(value);
    if (!botEl || !usage) return;
    const meta = botEl.querySelector('.bot-message-meta');
    if (!meta) return;
    meta.querySelector('.message-token-usage')?.remove();
    const element = createTokenUsageElement(
        usage,
        'message-token-usage text-xs text-slate-400 dark:text-slate-500',
    );
    const copyButton = meta.querySelector('.copy-msg-btn');
    if (copyButton) meta.insertBefore(element, copyButton);
    else meta.appendChild(element);
}

function setSessionTokenUsage(value, ownerSession = sessionId) {
    if (ownerSession !== sessionId) return;
    const element = document.getElementById('session-token-usage');
    if (!element) return;
    const usage = normalizeTokenUsage(value);
    if (!usage) {
        delete element.dataset.inputTokens;
        delete element.dataset.outputTokens;
        delete element.dataset.totalTokens;
        element.textContent = '';
        element.hidden = true;
        element.classList.add('hidden');
        return;
    }
    element.dataset.inputTokens = String(usage.input_tokens);
    element.dataset.outputTokens = String(usage.output_tokens);
    element.dataset.totalTokens = String(usage.total_tokens);
    element.classList.remove('hidden');
    renderTokenUsageElement(element);
}

function refreshRenderedTokenUsage() {
    document.querySelectorAll('.message-token-usage, #session-token-usage')
        .forEach(renderTokenUsageElement);
}

async function refreshCompletedRunUsage(requestId, botEl, ownerSession) {
    const usageGeneration = beginSessionTokenUsageRefresh(ownerSession);
    if (!ownerSession || ownerSession !== sessionId) return;
    if (isCurrentSessionTokenUsageRefresh(ownerSession, usageGeneration)) {
        const usages = sessionRuntime.get(ownerSession).snapshot().messages
            .map(message => normalizeTokenUsage(message.usage))
            .filter(Boolean);
        setSessionTokenUsage(usages.length ? {
            input_tokens: usages.reduce((sum, item) => sum + item.input_tokens, 0),
            output_tokens: usages.reduce((sum, item) => sum + item.output_tokens, 0),
            total_tokens: usages.reduce((sum, item) => sum + item.total_tokens, 0),
        } : null, ownerSession);
    }
}

// Resolve a localized label that may be either a plain string or
// a {zh, en} object returned by the backend.
function localizedLabel(label) {
    if (label && typeof label === 'object') {
        return label[currentLang] || label.en || label.zh || '';
    }
    return label || '';
}

function applyI18n() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
        el.innerHTML = t(el.dataset.i18nHtml);
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
        el.placeholder = t(el.dataset['i18nPlaceholder']);
    });
    document.querySelectorAll('[data-i18n-title]').forEach(el => {
        el.title = t(el.dataset['i18nTitle']);
    });
    document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
        el.setAttribute('aria-label', t(el.dataset['i18nAriaLabel']));
    });
    document.querySelectorAll('[data-tip-key]').forEach(el => {
        el.setAttribute('data-tooltip', t(el.dataset.tipKey));
    });
    applyProviderKeyHint();
    if (runtimeStatusPayload) renderRuntimeStatus(runtimeStatusPayload);
    installCfgTipPortal();
    
    // Clear any status messages when language changes
    document.querySelectorAll('[id$="-status"]').forEach(el => {
        el.classList.add('opacity-0');
    });
    
    const langLabel = document.getElementById('lang-label');
    if (langLabel) {
        if (currentLang === 'zh') langLabel.textContent = '简体';
        else langLabel.textContent = 'EN';
    }
    // Highlight the active option in the header language dropdown menu.
    document.querySelectorAll('#lang-menu .lang-menu-item').forEach(item => {
        const active = item.dataset.lang === currentLang;
        item.classList.toggle('text-blue-600', active);
        item.classList.toggle('dark:text-blue-400', active);
        item.classList.toggle('font-medium', active);
    });
    refreshRenderedTokenUsage();
    if (typeof renderTrajectorySnapshot === 'function') renderTrajectorySnapshot(sessionId);
}

// Single entry point for switching language. Updates the in-memory language,
// persists the browser preference locally, re-renders the UI, and updates the
// product-wide config.yaml language through the strict config endpoint.
function setLanguage(lang) {
    const next = (lang === 'en' || lang === 'zh') ? lang : 'zh';
    if (next === currentLang) {
        // Still persist + sync in case storage/backend drifted from the UI.
        localStorage.setItem('bianbu_lang', next);
        syncLanguageToBackend(next);
        return;
    }
    currentLang = next;
    localStorage.setItem('bianbu_lang', currentLang);
    applyI18n();
    updateComposerInteractionState();
    _applyInputTooltips();
    // Keep the language switch button and config selector visually in sync.
    try { updateLangControls(); } catch (e) {}
    
    // Sync language choice to backend first, then trigger dynamic views reload
    // to avoid race conditions on API endpoints.
    syncLanguageToBackend(currentLang, () => {
        try { rerenderDynamicViews(); } catch (e) {}
    });
}

// Persist the product default to config.yaml (best-effort; the browser-only
// preference has already switched locally, so a network failure is non-blocking).
function syncLanguageToBackend(lang, callback) {
    try {
        fetch(`${PUBLIC_API_BASE}/settings`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ deployment: { language: lang } })
        })
        .then(() => { if (callback) callback(); })
        .catch(() => { if (callback) callback(); });
    } catch (e) {
        if (callback) callback();
    }
}

// Reflect the current language on the header toggle.
function updateLangControls() {
    const langLabel = document.getElementById('lang-label');
    if (langLabel) {
        if (currentLang === 'zh') langLabel.textContent = '简体';
        else langLabel.textContent = 'EN';
    }
    // Highlight the active option in the header language dropdown menu.
    document.querySelectorAll('#lang-menu .lang-menu-item').forEach(item => {
        const active = item.dataset.lang === currentLang;
        item.classList.toggle('text-blue-600', active);
        item.classList.toggle('dark:text-blue-400', active);
        item.classList.toggle('font-medium', active);
    });

}

// Toggle the header language dropdown menu open/closed.
function toggleLangMenu(event) {
    if (event) event.stopPropagation();
    const menu = document.getElementById('lang-menu');
    if (menu) menu.classList.toggle('hidden');
}

// Pick a language from the dropdown, then close the menu.
function selectLanguage(lang) {
    const menu = document.getElementById('lang-menu');
    if (menu) menu.classList.add('hidden');
    setLanguage(lang);
}
window.toggleLangMenu = toggleLangMenu;
window.selectLanguage = selectLanguage;

// Close the language menu when clicking outside of it.
document.addEventListener('click', (e) => {
    const selector = document.getElementById('lang-selector');
    const menu = document.getElementById('lang-menu');
    if (menu && !menu.classList.contains('hidden') && selector && !selector.contains(e.target)) {
        menu.classList.add('hidden');
    }
});

// Refresh JS-rendered views after a language switch. Each branch uses the
// lightweight in-memory re-render path (no extra network round-trips).
function rerenderDynamicViews() {
    if (activeAgent) renderActiveAgent();
    renderChatAgentMenu();
    refreshSessionTitles();
    if (currentView === 'agents') {
        renderAgentsView();
    }
    if (currentView === 'models' && typeof renderModelsView === 'function'
            && modelsState && modelsState.providers) {
        renderModelsView();
    }
    // Reload skills and tools after language switch
    if (currentView === 'tools') {
        toolsLoaded = false;
        loadToolsView();
    } else if (currentView === 'skills') {
        loadSkillsView();
    }
    // An open Agent editor keeps localized Skill / Tool labels, so redraw its
    // capability panels from the cached catalog as well.
    const agentEditor = document.getElementById('agent-editor-overlay');
    if (agentEditor && !agentEditor.classList.contains('hidden')) {
        renderAgentEditorCapabilityPanels();
    }
    // The temporary-skill picker and its chips keep their own labels, so they
    // have to be redrawn for the new language as well.
    renderSelectedSkills();
    // The `/` list writes the same localized Skill names.
    if (isSlashMenuVisible() && chatInput.value.startsWith('/')) {
        slashLastFilter = null;
        showSlashMenu(chatInput.value);
    }
    if (!skillPicker.classList.contains('hidden')) renderSkillPickerItems();
    if (currentView === 'api-capture') {
        providerCaptureRecordFingerprint = '';
        renderProviderCapture();
    }
}

// Floating tooltip portal for [data-tip-key] elements. Tooltip nodes are
// appended to <body> so they aren't clipped by overflow:hidden ancestors
// (e.g. the config panel's scroll container).
let _cfgTipPortalEl = null;
let _cfgTipPortalInstalled = false;
function installCfgTipPortal() {
    if (_cfgTipPortalInstalled) return;
    _cfgTipPortalInstalled = true;

    const showTip = (target) => {
        const text = target.getAttribute('data-tooltip');
        if (!text) return;
        if (!_cfgTipPortalEl) {
            _cfgTipPortalEl = document.createElement('div');
            _cfgTipPortalEl.className = 'cfg-tip-floating';
            document.body.appendChild(_cfgTipPortalEl);
        }
        _cfgTipPortalEl.textContent = text;
        const rect = target.getBoundingClientRect();
        // Render once to measure, then position above the target, centered.
        _cfgTipPortalEl.style.left = '0px';
        _cfgTipPortalEl.style.top = '0px';
        _cfgTipPortalEl.classList.add('show');
        const tipRect = _cfgTipPortalEl.getBoundingClientRect();
        let left = rect.left + rect.width / 2 - tipRect.width / 2;
        // Clamp horizontally to the viewport with an 8px gutter.
        left = Math.max(8, Math.min(left, window.innerWidth - tipRect.width - 8));
        const top = rect.top - tipRect.height - 6;
        _cfgTipPortalEl.style.left = left + 'px';
        _cfgTipPortalEl.style.top = top + 'px';
    };
    const hideTip = () => {
        if (_cfgTipPortalEl) _cfgTipPortalEl.classList.remove('show');
    };

    document.addEventListener('mouseover', (e) => {
        const target = e.target.closest('[data-tip-key]');
        if (target) showTip(target);
    });
    document.addEventListener('mouseout', (e) => {
        const target = e.target.closest('[data-tip-key]');
        if (target) hideTip();
    });
    // Hide on scroll/resize so the tooltip doesn't drift away from its anchor.
    window.addEventListener('scroll', hideTip, true);
    window.addEventListener('resize', hideTip);
}

// =====================================================================
// Theme
// =====================================================================
let currentTheme = localStorage.getItem('bianbu_theme') || 'dark';

function applyTheme() {
    const root = document.documentElement;
    if (currentTheme === 'dark') {
        root.classList.add('dark');
        document.getElementById('theme-icon').className = 'fas fa-sun';
        document.getElementById('hljs-light').disabled = true;
        document.getElementById('hljs-dark').disabled = false;
    } else {
        root.classList.remove('dark');
        document.getElementById('theme-icon').className = 'fas fa-moon';
        document.getElementById('hljs-light').disabled = false;
        document.getElementById('hljs-dark').disabled = true;
    }
}

function toggleTheme() {
    currentTheme = currentTheme === 'dark' ? 'light' : 'dark';
    localStorage.setItem('bianbu_theme', currentTheme);
    applyTheme();
}

// =====================================================================
// Sidebar & Navigation
// =====================================================================
const VIEW_META = {
    chat:     { group: 'nav_chat',    page: 'menu_chat' },
    agents:   { group: 'nav_manage',  page: 'menu_agents' },
    models:   { group: 'nav_manage',  page: 'menu_models' },
    'bibit-models': { group: 'nav_manage', page: 'menu_bibit_models' },
    tools:    { group: 'nav_manage',  page: 'menu_tools' },
    plugins:  { group: 'nav_manage',  page: 'menu_plugins' },
    security: { group: 'nav_manage',  page: 'menu_security' },
    mcp:      { group: 'nav_manage',  page: 'menu_mcp' },
    skills:   { group: 'nav_manage',  page: 'menu_skills' },
    'agent-top': { group: 'nav_monitor', page: 'menu_agent_top' },
    'runtime-monitor': { group: 'nav_monitor', page: 'menu_runtime_status' },
    'api-capture': { group: 'nav_monitor', page: 'menu_api_capture' },
    logs:     { group: 'nav_monitor', page: 'menu_logs' },
};

let currentView = 'chat';

function navigateTo(viewId) {
    if (!VIEW_META[viewId]) return;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const target = document.getElementById('view-' + viewId);
    if (target) target.classList.add('active');
    document.querySelectorAll('.sidebar-item').forEach(item => {
        item.classList.toggle('active', item.dataset.view === viewId);
    });
    const meta = VIEW_META[viewId];
    document.getElementById('breadcrumb-group').textContent = t(meta.group);
    document.getElementById('breadcrumb-group').dataset.i18n = meta.group;
    document.getElementById('breadcrumb-page').textContent = t(meta.page);
    document.getElementById('breadcrumb-page').dataset.i18n = meta.page;
    currentView = viewId;
    
    // Clear status messages when navigating away
    document.querySelectorAll('[id$="-status"]').forEach(el => {
        el.classList.add('opacity-0');
    });
    
    if (window.innerWidth < 1024) closeSidebar();
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    const isOpen = !sidebar.classList.contains('-translate-x-full');
    if (isOpen) {
        closeSidebar();
    } else {
        sidebar.classList.remove('-translate-x-full');
        overlay.classList.remove('hidden');
    }
}

function closeSidebar() {
    document.getElementById('sidebar').classList.add('-translate-x-full');
    document.getElementById('sidebar-overlay').classList.add('hidden');
}

document.querySelectorAll('.menu-group > button').forEach(btn => {
    btn.addEventListener('click', () => {
        btn.parentElement.classList.toggle('open');
    });
});

document.querySelectorAll('.sidebar-item').forEach(item => {
    item.addEventListener('click', () => navigateTo(item.dataset.view));
});

window.addEventListener('resize', () => {
    if (window.innerWidth >= 1024) {
        document.getElementById('sidebar').classList.remove('-translate-x-full');
        document.getElementById('sidebar-overlay').classList.add('hidden');
    } else {
        if (!document.getElementById('sidebar').classList.contains('-translate-x-full')) {
            closeSidebar();
        }
    }
});

// =====================================================================
// Markdown Renderer
// =====================================================================
const FALLBACK_HLJS = {
    getLanguage() { return false; },
    highlight(str) { return { value: escapeHtml(str) }; },
    highlightAuto(str) { return { value: escapeHtml(str) }; },
    highlightElement() {},
};

function getHljs() {
    return window.hljs || FALLBACK_HLJS;
}

function workspaceHtmlDownloadUrl(value) {
    let path;
    try { path = decodeURIComponent(String(value || '')); } catch (_) { return null; }
    if (/[\\?#\x00-\x1f\x7f]/.test(path) || /^[a-z][a-z0-9+.-]*:/i.test(path)) return null;
    const workerRoot = '/home/agent/workspace/';
    if (path.startsWith(workerRoot)) path = path.slice(workerRoot.length);
    else if (path.startsWith('/')) return null;
    else if (path.startsWith('workspace/')) path = path.slice('workspace/'.length);
    else if (path.startsWith('./')) path = path.slice(2);
    const parts = path.split('/');
    if (!/\.html?$/i.test(path) || parts.some(part => !part || part === '.' || part === '..')) return null;
    return `${PUBLIC_API_BASE}/workspace/download?path=${encodeURIComponent(path)}`;
}

function createMd() {
    const hljsLib = getHljs();
    const mdFactory = window.markdownit;
    if (typeof mdFactory !== 'function') {
        return {
            render(text) {
                return `<p>${escapeHtml(text || '')}</p>`;
            }
        };
    }
    const md = mdFactory({
        html: false, breaks: true, linkify: true, typographer: true,
        highlight: function(str, lang) {
            if (lang && hljsLib.getLanguage(lang)) {
                try { return hljsLib.highlight(str, { language: lang }).value; } catch (_) {}
            }
            return hljsLib.highlightAuto(str).value;
        }
    });
    // Fix greedy linkify: markdown-it's linkify swallows markdown emphasis (*)
    // and CJK full-width punctuation glued to a URL (common in LLM output like
    // "**https://x**，中文"), turning the whole tail into one broken link. Cut
    // the URL at the first such char and spill the remainder back as text.
    var GREEDY_LINK_CUT = /[*\u3000-\u303F\uFF00-\uFFEF]/;
    md.core.ruler.after('linkify', 'fix_greedy_linkify', function(state) {
        for (var b = 0; b < state.tokens.length; b++) {
            var blk = state.tokens[b];
            if (blk.type !== 'inline' || !blk.children) continue;
            var ch = blk.children;
            for (var i = 0; i < ch.length; i++) {
                var open = ch[i];
                if (open.type !== 'link_open' || open.markup !== 'linkify') continue;
                var textTok = ch[i + 1], close = ch[i + 2];
                if (!textTok || textTok.type !== 'text' || !close || close.type !== 'link_close') continue;
                var idx = textTok.content.search(GREEDY_LINK_CUT);
                if (idx < 0) continue;
                var keep = textTok.content.slice(0, idx);
                var spill = textTok.content.slice(idx);
                textTok.content = keep;
                open.attrSet('href', keep);
                var spillTok = new state.Token('text', '', 0);
                spillTok.content = spill;
                ch.splice(i + 3, 0, spillTok);
            }
        }
    });
    const defaultLinkOpen = md.renderer.rules.link_open || function(tokens, idx, options, env, self) {
        return self.renderToken(tokens, idx, options);
    };
    md.renderer.rules.link_open = function(tokens, idx, options, env, self) {
        const originalHref = tokens[idx].attrGet('href') || '';
        const downloadUrl = workspaceHtmlDownloadUrl(originalHref);
        if (downloadUrl) {
            tokens[idx].attrSet('href', downloadUrl);
            tokens[idx].attrSet('download', decodeURIComponent(originalHref).split('/').pop());
        }
        tokens[idx].attrPush(['target', '_blank']);
        tokens[idx].attrPush(['rel', 'noopener noreferrer']);
        return defaultLinkOpen(tokens, idx, options, env, self);
    };
    const defaultCodeInline = md.renderer.rules.code_inline;
    md.renderer.rules.code_inline = function(tokens, idx, options, env, self) {
        const path = tokens[idx].content;
        const insideLink = tokens.slice(0, idx).reduce((depth, token) =>
            depth + (token.type === 'link_open' ? 1 : token.type === 'link_close' ? -1 : 0), 0) > 0;
        const explicitWorkspace = !insideLink && (path.startsWith('/home/agent/workspace/') || path.startsWith('workspace/'));
        const downloadUrl = explicitWorkspace ? workspaceHtmlDownloadUrl(path) : null;
        const code = defaultCodeInline(tokens, idx, options, env, self);
        if (!downloadUrl) return code;
        const filename = decodeURIComponent(path).split('/').pop();
        return `<a href="${md.utils.escapeHtml(downloadUrl)}" download="${md.utils.escapeHtml(filename)}">${code}</a>`;
    };
    return md;
}

const md = createMd();

const VIDEO_EXT_RE = /\.(?:mp4|webm|mov|avi|mkv)$/i;  // tested against URL without query string
const IMAGE_EXT_RE = /\.(?:jpg|jpeg|png|gif|webp|bmp|svg)$/i;  // tested against URL without query string

function _toWebUrl(url) {
    if (/^\/[A-Za-z]/.test(url) && !url.startsWith(PUBLIC_API_BASE)) return '';
    if (/^file:\/\/\//i.test(url)) return '';
    return url;
}

function safeMediaUrl(value) {
    try {
        const parsed = new URL(String(value || ''), window.location.origin);
        if (parsed.protocol === 'blob:' && parsed.origin === window.location.origin) {
            return parsed.href;
        }
        if (
            (parsed.protocol === 'http:' || parsed.protocol === 'https:')
            && parsed.origin === window.location.origin
        ) {
            return parsed.href;
        }
    } catch (_) {}
    return null;
}

function createImagePreview(url, alt = 'image') {
    const safeUrl = safeMediaUrl(_toWebUrl(String(url || '')));
    if (!safeUrl) return null;
    const wrapper = document.createElement('div');
    wrapper.style.margin = '10px 0';
    const image = document.createElement('img');
    image.src = safeUrl;
    image.alt = String(alt || 'image');
    image.loading = 'lazy';
    image.className = 'bianbu-lightbox-trigger';
    image.style.cssText = 'max-width:520px;width:100%;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.15);display:block;cursor:zoom-in;';
    image.addEventListener('click', () => _openImageLightbox(image.src));
    wrapper.appendChild(image);
    return wrapper;
}

function createVideoPreview(url) {
    const rawUrl = String(url || '');
    const safeUrl = safeMediaUrl(_toWebUrl(rawUrl));
    if (!safeUrl) return null;
    const wrapper = document.createElement('div');
    wrapper.style.margin = '10px 0';
    const video = document.createElement('video');
    video.controls = true;
    video.preload = 'metadata';
    video.style.cssText = 'max-width:100%;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.15);display:block;';
    const source = document.createElement('source');
    source.src = safeUrl;
    video.appendChild(source);
    const link = document.createElement('a');
    link.href = safeUrl;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.style.cssText = 'display:inline-flex;align-items:center;gap:4px;margin-top:4px;font-size:12px;color:#8b8fa8;text-decoration:none;';
    const icon = document.createElement('i');
    icon.className = 'fas fa-download';
    link.append(icon, document.createTextNode(` ${rawUrl.split('/').pop().split('?')[0]}`));
    wrapper.append(video, link);
    return wrapper;
}

function _openImageLightbox(src) {
    let overlay = document.getElementById('bianbu-lightbox');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'bianbu-lightbox';
        overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.85);display:flex;align-items:center;justify-content:center;cursor:zoom-out;opacity:0;transition:opacity .2s';
        overlay.onclick = () => { overlay.style.opacity = '0'; setTimeout(() => overlay.style.display = 'none', 200); };
        const img = document.createElement('img');
        img.id = 'bianbu-lightbox-img';
        img.style.cssText = 'max-width:92vw;max-height:92vh;border-radius:8px;box-shadow:0 4px 24px rgba(0,0,0,0.5);object-fit:contain;';
        img.onclick = (e) => e.stopPropagation();
        overlay.appendChild(img);
        document.body.appendChild(overlay);
    }
    overlay.querySelector('#bianbu-lightbox-img').src = src;
    overlay.style.display = 'flex';
    requestAnimationFrame(() => overlay.style.opacity = '1');
}

function normalizeMarkdownMedia(html) {
    const template = document.createElement('template');
    template.innerHTML = html;
    template.content.querySelectorAll('img').forEach(image => {
        const safeUrl = safeMediaUrl(_toWebUrl(image.getAttribute('src') || ''));
        if (!safeUrl) {
            image.remove();
            return;
        }
        image.src = safeUrl;
        image.classList.add('bianbu-lightbox-trigger');
        image.style.cursor = 'zoom-in';
    });
    template.content.querySelectorAll('a[href]').forEach(link => {
        const rawUrl = link.getAttribute('href') || '';
        const cleanPath = rawUrl.split('?')[0];
        const preview = VIDEO_EXT_RE.test(cleanPath)
            ? createVideoPreview(rawUrl)
            : (IMAGE_EXT_RE.test(cleanPath) ? createImagePreview(rawUrl) : null);
        if (preview) link.replaceWith(preview);
    });
    return template.innerHTML;
}

document.addEventListener('click', event => {
    const image = event.target.closest?.('.bianbu-lightbox-trigger');
    if (image && image.src) _openImageLightbox(image.src);
});

function renderMarkdown(text) {
    try {
        const html = normalizeMarkdownMedia(md.render(text));
        // Note: Code block headers are added via DOM manipulation after insertion
        // See addCodeBlockHeadersToElement()
        return html;
    }
    catch (e) { return text.replace(/\n/g, '<br>'); }
}

function assistantDisplayText(text) {
    return stripAssistantThinkBlocks(text);
}

function renderAssistantMarkdown(text) {
    return renderMarkdown(assistantDisplayText(text));
}

function _addCodeBlockHeaders(container) {
    // Add header with language label and copy button to each <pre> block using DOM manipulation
    const preBlocks = container.querySelectorAll('pre');
    preBlocks.forEach(pre => {
        if (pre.parentElement && pre.parentElement.classList.contains('code-block-wrapper')) return;
        
        const codeEl = pre.querySelector('code');
        if (!codeEl) return;
        
        const langClass = Array.from(codeEl.classList).find(c => c.startsWith('language-'));
        const language = langClass ? langClass.replace('language-', '') : '';
        // Hide label for unknown/empty languages (e.g. language-undefined)
        const showLang = language && language !== 'undefined' && language !== 'code';
        const langLabel = showLang ? language.charAt(0).toUpperCase() + language.slice(1) : '';
        
        const wrapper = document.createElement('div');
        wrapper.className = 'code-block-wrapper';
        
        const header = document.createElement('div');
        header.className = 'code-block-header';
        header.innerHTML = `
            <span class="code-block-lang">${langLabel}</span>
            <button class="code-copy-btn" title="Copy code">
                <i class="fas fa-copy"></i>
            </button>
        `;
        
        pre.parentNode.insertBefore(wrapper, pre);
        wrapper.appendChild(header);
        wrapper.appendChild(pre);
    });
}

// =====================================================================
// Chat Module
// =====================================================================
let loadingContainers = {};
const runRestorePromises = new Map(); // session_id:request_id -> snapshot single flight
const sessionPendingApprovals = new Map(); // session_id -> {requestId, ids, revision}
const ACTIVE_RUNS_KEY = 'bianbu_active_runs';
const ACTIVE_RUN_STARTED_AT_KEY = 'bianbu_active_run_started_at';
let sessionActiveRequest = (() => {
    try {
        const value = JSON.parse(localStorage.getItem(ACTIVE_RUNS_KEY) || '{}');
        return value && typeof value === 'object' ? value : {};
    } catch (_) {
        return {};
    }
})();   // session_id -> request_id (active conversational Run per Session)
let activeRunStartedAt = (() => {
    try {
        const value = JSON.parse(localStorage.getItem(ACTIVE_RUN_STARTED_AT_KEY) || '{}');
        return value && typeof value === 'object' ? value : {};
    } catch (_) {
        return {};
    }
})();   // session_id:request_id -> browser timestamp in milliseconds
let runProgressTimer = null;

function persistActiveRuns() {
    localStorage.setItem(ACTIVE_RUNS_KEY, JSON.stringify(sessionActiveRequest));
}

function activeRunKey(ownerSession, requestId) {
    return `${ownerSession}:${requestId}`;
}

function persistActiveRunStartedAt() {
    localStorage.setItem(
        ACTIVE_RUN_STARTED_AT_KEY, JSON.stringify(activeRunStartedAt)
    );
}

function pruneActiveRunStartedAt() {
    const activeKeys = new Set(Object.entries(sessionActiveRequest)
        .map(([ownerSession, requestId]) => activeRunKey(ownerSession, requestId)));
    let changed = false;
    Object.keys(activeRunStartedAt).forEach(key => {
        if (activeKeys.has(key)) return;
        delete activeRunStartedAt[key];
        changed = true;
    });
    if (changed) persistActiveRunStartedAt();
}

function updateRunProgressIndicator(ownerSession = sessionId) {
    const requestId = String(sessionActiveRequest[ownerSession] || '');
    const indicators = messagesDiv.querySelectorAll('.run-progress-indicator');
    const renderedApproval = requestId && Array.from(
        messagesDiv.querySelectorAll('.approval-card')
    ).some(card => card.dataset.requestId === requestId);
    if (!requestId || String(ownerSession) !== sessionId
        || hasPendingApproval(ownerSession) || renderedApproval) {
        indicators.forEach(element => element.remove());
        return;
    }
    const replies = Array.from(messagesDiv.querySelectorAll('.bot-message-group'))
        .filter(element => element.dataset.requestId === requestId);
    if (!replies.length) {
        indicators.forEach(element => element.remove());
        return;
    }
    const target = replies[replies.length - 1].querySelector('.msg-content');
    if (!target) return;
    indicators.forEach(element => {
        if (!target.contains(element)) element.remove();
    });
    let indicator = target.querySelector('.run-progress-indicator');
    if (!indicator) {
        indicator = document.createElement('div');
        indicator.className = 'run-progress-indicator mt-2 text-xs text-slate-400 dark:text-slate-500';
        indicator.setAttribute('role', 'timer');
        indicator.setAttribute('aria-live', 'off');
        target.appendChild(indicator);
    } else if (target.lastElementChild !== indicator) {
        target.appendChild(indicator);
    }
    const key = activeRunKey(ownerSession, requestId);
    const startedAt = Number(activeRunStartedAt[key] || Date.now());
    if (!activeRunStartedAt[key]) {
        activeRunStartedAt[key] = startedAt;
        persistActiveRunStartedAt();
    }
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    const text = `running.... ${seconds}s`;
    if (indicator.textContent !== text) indicator.textContent = text;
}

function stopRunProgressUpdates() {
    if (runProgressTimer === null) return;
    window.clearTimeout(runProgressTimer);
    runProgressTimer = null;
}

function scheduleRunProgressUpdates() {
    if (runProgressTimer !== null) return;
    const requestId = String(sessionActiveRequest[sessionId] || '');
    if (!requestId || hasPendingApproval(sessionId)) return;
    const startedAt = Number(
        activeRunStartedAt[activeRunKey(sessionId, requestId)] || Date.now()
    );
    const elapsed = Math.max(0, Date.now() - startedAt);
    const delay = Math.max(50, 1000 - (elapsed % 1000) + 10);
    runProgressTimer = window.setTimeout(() => {
        runProgressTimer = null;
        updateRunProgressIndicator();
        scheduleRunProgressUpdates();
    }, delay);
}

function syncRunProgressIndicator(ownerSession = sessionId) {
    if (String(ownerSession) !== sessionId) return;
    stopRunProgressUpdates();
    updateRunProgressIndicator(ownerSession);
    scheduleRunProgressUpdates();
}

function trackAcceptedRun(ownerSession, requestId, loadingEl = null) {
    const previousRequestId = sessionActiveRequest[ownerSession];
    if (previousRequestId && previousRequestId !== requestId) {
        delete activeRunStartedAt[activeRunKey(ownerSession, previousRequestId)];
    }
    sessionActiveRequest[ownerSession] = requestId;
    persistActiveRuns();
    if (loadingEl) {
        loadingEl.classList.add('chat-loading-indicator');
        loadingEl.dataset.requestId = String(requestId);
    }
    const key = activeRunKey(ownerSession, requestId);
    activeRunStartedAt[key] = Number(loadingEl?.dataset.startedAt || Date.now());
    pruneActiveRunStartedAt();
    persistActiveRunStartedAt();
    syncRunProgressIndicator(ownerSession);
    if (sessionRuntime.get(ownerSession).isRunTerminal(requestId)) {
        _markRunTerminal(ownerSession, requestId);
        loadingEl?.remove();
        if (ownerSession === sessionId) resetActiveRunControls(requestId);
        return;
    }
    sessionRuntime.patchSummary(ownerSession, {
        running: true, waiting: false, failed: false,
        active_run_id: requestId, pending_interaction: null,
    });
    renderSessionStatus(ownerSession);
}

function isCurrentSessionConversationActive() {
    return !!sessionActiveRequest[sessionId];
}

let isComposing = false;
let appConfig = { use_agent: false, title: 'Bianbu Agent', subtitle: '', providers: {}, api_bases: {} };

const SESSION_ID_KEY = 'bianbu_session_id';

function generateSessionId() {
    return 'session_' + ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
        (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16)
    );
}

function generateIdempotencyKey(prefix = 'draft') {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    const random = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return `${prefix}:${random}`;
}

// Restore session_id from localStorage so conversation history survives page refresh.
// A new id is only generated when the user explicitly starts a new chat.
function loadOrCreateSessionId() {
    const stored = localStorage.getItem(SESSION_ID_KEY);
    return stored || null;
}

let sessionId = loadOrCreateSessionId();
let newChatDraft = null;
let agentChooserMode = 'navigate';
let agentMentionRange = null;
let agentMentionActiveIndex = 0;
const chatInput = document.getElementById('chat-input');
const sendBtn = document.getElementById('send-btn');
const stopBtn = document.getElementById('stop-btn');
const attachBtn = document.getElementById('attach-btn');
const attachMenu = document.getElementById('attach-menu');
const attachFolderOption = document.getElementById('attach-folder-option');
const attachmentInput = document.getElementById('composer-file-input');
const attachmentFolderInput = document.getElementById('composer-folder-input');
const composerNotice = document.getElementById('composer-notice');
const messagesDiv = document.getElementById('chat-messages');
const trajectoryView = document.getElementById('trajectory-view');
const trajectoryLedger = document.getElementById('trajectory-ledger');
const trajectoryInspector = document.getElementById('trajectory-inspector');
const trajectorySearch = document.getElementById('trajectory-search');
const trajectoryLoadOlder = document.getElementById('trajectory-load-older');
let activeConversationView = 'chat';
let trajectorySearchQuery = '';
let selectedTrajectoryKey = null;

function trajectoryText(value) {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    if (Array.isArray(value) && value.length === 0) return '';
    if (typeof value === 'object' && Object.keys(value).length === 0) return '';
    try { return JSON.stringify(value, null, 2); } catch (_) { return String(value); }
}

function trajectorySeconds(value) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    const seconds = Number(value);
    return seconds < 1 ? `${Math.round(seconds * 1000)}ms` : `${seconds.toFixed(1)}s`;
}

function trajectoryClock(value) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    return new Date(Number(value) * 1000).toLocaleTimeString(
        currentLang === 'en' ? 'en-US' : 'zh-CN',
        {hour: '2-digit', minute: '2-digit', second: '2-digit'},
    );
}

function trajectoryKindLabel(record) {
    if (record.kind === 'tool') return record.title || 'Tool';
    // Which Hook ran matters more than the fact that it was a Hook: the ledger
    // shows "security.before_run" instead of a wall of identical "Hook" rows.
    if (record.kind === 'hook' && record.hook && record.hook.name) {
        return record.hook.name;
    }
    // 等待行说的是"这段时间没有记录"：按它卡在哪个阶段给出中英标签，用户才知道
    // 时间是被启动、模型首字还是收尾吃掉的。
    if (record.kind === 'wait') {
        const reason = record.wait && record.wait.reason;
        const waits = currentLang === 'en'
            ? {startup: 'Startup', model: 'Model wait', handoff: 'Handoff', finalize: 'Finalize', idle: 'Idle'}
            : {startup: '启动', model: '等待模型', handoff: '交接', finalize: '收尾', idle: '空档'};
        return waits[reason] || (currentLang === 'en' ? 'Waiting' : '等待');
    }
    const labels = currentLang === 'en'
        ? {user: 'User', reasoning: 'Reasoning', assistant: 'Assistant', run: 'Run', approval: 'Approval', context: 'Context', hook: 'Hook', event: 'Event'}
        : {user: '用户', reasoning: '推理', assistant: '助手', run: '运行', approval: '审批', context: '上下文', hook: 'Hook', event: '事件'};
    return labels[record.kind] || record.title;
}

function trajectorySearchText(record) {
    return [record.kind, trajectoryKindLabel(record), record.title, record.preview,
        record.input, record.output,
        record.status, record.run_id, record.seq, trajectoryText(record.data)]
        .join('\n').toLocaleLowerCase();
}

function appendTrajectoryDetail(parent, label, value) {
    if (!value) return;
    const section = document.createElement('section');
    section.className = 'trajectory-inspector-section';
    const heading = document.createElement('h4');
    heading.textContent = label;
    const content = document.createElement('pre');
    content.textContent = value;
    section.append(heading, content);
    parent.appendChild(section);
}

function renderTrajectoryInspector(record) {
    trajectoryInspector.replaceChildren();
    if (!record) {
        trajectoryInspector.classList.add('hidden');
        return;
    }
    trajectoryInspector.classList.remove('hidden');
    const header = document.createElement('header');
    const title = document.createElement('h3');
    title.textContent = record.kind === 'hook' && record.hook && record.hook.id
        ? `${record.hook.id} · #${record.seq}`
        : `${trajectoryKindLabel(record)} · #${record.seq}`;
    const meta = document.createElement('span');
    meta.textContent = `${trajectoryClock(record.started_at)} · ${t('trajectory_duration')} ${trajectorySeconds(record.duration)}`;
    header.append(title, meta);
    trajectoryInspector.appendChild(header);
    appendTrajectoryDetail(trajectoryInspector, t('trajectory_input'), record.input);
    appendTrajectoryDetail(trajectoryInspector, t('trajectory_output'), record.output);
    appendTrajectoryDetail(
        trajectoryInspector, t('trajectory_metadata'), trajectoryText(record.data),
    );
}

function createTrajectoryRecord(record) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `trajectory-record kind-${record.kind}`;
    button.dataset.trajectoryKey = record.key;
    // 嵌套行的时间算在外层记录里，并行行与别的记录同时进行；两者都要看得见，
    // 否则"行加起来大于耗时"看着像账错了。
    if (record.nested) button.classList.add('trajectory-record-nested');
    if (record.parallel) button.classList.add('trajectory-record-parallel');
    button.title = record.nested
        ? `${t('trajectory_nested')} · ${trajectorySeconds(record.overlap_seconds)}`
        : record.parallel
            ? `${t('trajectory_parallel')} · ${trajectorySeconds(record.overlap_seconds)}`
            : '';
    button.classList.toggle('selected', record.key === selectedTrajectoryKey);
    button.setAttribute('aria-pressed', String(record.key === selectedTrajectoryKey));
    const index = document.createElement('span');
    index.className = 'trajectory-record-index';
    index.textContent = record.kind === 'wait' ? '—' : `#${record.seq}`;
    const time = document.createElement('time');
    time.className = 'trajectory-record-time';
    time.textContent = trajectoryClock(record.started_at);
    const kind = document.createElement('strong');
    kind.className = `trajectory-record-kind kind-${record.kind}`;
    kind.textContent = `${record.nested ? '↳ ' : record.parallel ? '∥ ' : ''}${trajectoryKindLabel(record)}`;
    const preview = document.createElement('span');
    preview.className = 'trajectory-record-preview';
    preview.textContent = record.preview || record.title;
    const metric = document.createElement('span');
    metric.className = 'trajectory-record-metric';
    metric.textContent = [
        record.status,
        record.duration == null ? null : trajectorySeconds(record.duration),
    ].filter(Boolean).join(' · ') || '—';
    button.append(index, time, kind, preview, metric);
    button.addEventListener('click', () => {
        selectedTrajectoryKey = record.key;
        trajectoryLedger.querySelectorAll('.trajectory-record').forEach(row => {
            const selected = row.dataset.trajectoryKey === selectedTrajectoryKey;
            row.classList.toggle('selected', selected);
            row.setAttribute('aria-pressed', String(selected));
        });
        renderTrajectoryInspector(record);
    });
    return button;
}

function renderTrajectorySnapshot(sid = sessionId, preserveScroll = false) {
    if (!trajectoryLedger || String(sid) !== String(sessionId)) return false;
    const snapshot = sessionRuntime.get(sid).snapshot();
    const projection = window.BianbuTrajectory.projectTrajectory(snapshot.events || []);
    const query = trajectorySearchQuery.trim().toLocaleLowerCase();
    const previousDistance = trajectoryLedger.scrollHeight
        - trajectoryLedger.scrollTop - trajectoryLedger.clientHeight;
    const followTail = preserveScroll && previousDistance < 32;
    trajectoryLedger.replaceChildren();
    const visibleRecords = [];
    projection.turns.forEach(turn => {
        const records = query
            ? turn.records.filter(record => trajectorySearchText(record).includes(query))
            : turn.records;
        if (!records.length) return;
        visibleRecords.push(...records);
        const section = document.createElement('section');
        section.className = 'trajectory-turn';
        const header = document.createElement('header');
        const heading = document.createElement('h3');
        heading.textContent = `${t('trajectory_turn')} ${turn.turn}`;
        const details = document.createElement('span');
        const waiting = turn.waiting
            ? ` · ${t('trajectory_waiting')} ${trajectorySeconds(turn.waiting)}` : '';
        details.textContent = `${records.length} ${t('trajectory_records')} · ${t('trajectory_duration')} ${trajectorySeconds(turn.duration)}${waiting} · ${turn.status}`;
        header.append(heading, details);
        const list = document.createElement('div');
        list.className = 'trajectory-records';
        records.forEach(record => list.appendChild(createTrajectoryRecord(record)));
        section.append(header, list);
        // 一轮的账要能当场验算：耗时 = 记录覆盖 + 等待；行里显示的耗时之和比它大，
        // 多出来的就是与其它记录重叠（嵌套/并行）的那部分。
        if (!query && turn.duration != null) {
            const total = document.createElement('p');
            total.className = 'trajectory-turn-total';
            total.textContent = [
                `${t('trajectory_covered')} ${trajectorySeconds(turn.covered)}`,
                `+ ${t('trajectory_waiting')} ${trajectorySeconds(turn.waiting)}`,
                `= ${trajectorySeconds(turn.duration)}`,
                turn.overlap
                    ? `(${trajectorySeconds(turn.overlap)} ${t('trajectory_overlap')})`
                    : null,
            ].filter(Boolean).join(' ');
            section.appendChild(total);
        }
        trajectoryLedger.appendChild(section);
    });
    if (!visibleRecords.length) {
        const empty = document.createElement('p');
        empty.className = 'trajectory-empty';
        empty.textContent = projection.records.length
            ? t('trajectory_no_results') : t('trajectory_empty');
        trajectoryLedger.appendChild(empty);
    }
    trajectoryLoadOlder.classList.toggle('hidden', !snapshot.has_more);
    const selected = projection.records.find(record => record.key === selectedTrajectoryKey);
    if (!selected) selectedTrajectoryKey = null;
    renderTrajectoryInspector(selected);
    if (followTail) trajectoryLedger.scrollTop = trajectoryLedger.scrollHeight;
    return projection.records.length > 0;
}

function setConversationView(view) {
    activeConversationView = view === 'trajectory' ? 'trajectory' : 'chat';
    const trajectory = activeConversationView === 'trajectory';
    messagesDiv.classList.toggle('hidden', trajectory);
    trajectoryView.classList.toggle('hidden', !trajectory);
    document.getElementById('scroll-to-bottom-btn').classList.toggle('hidden', trajectory);
    [['chat', !trajectory], ['trajectory', trajectory]].forEach(([name, active]) => {
        const tab = document.getElementById(`conversation-tab-${name}`);
        tab.classList.toggle('active', active);
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
    });
    if (trajectory) renderTrajectorySnapshot(sessionId);
}

function resetTrajectoryState() {
    trajectorySearchQuery = '';
    selectedTrajectoryKey = null;
    trajectorySearch.value = '';
    renderTrajectoryInspector(null);
}

document.getElementById('conversation-tab-chat').addEventListener(
    'click', () => setConversationView('chat'),
);
document.getElementById('conversation-tab-trajectory').addEventListener(
    'click', () => setConversationView('trajectory'),
);
trajectorySearch.addEventListener('input', () => {
    trajectorySearchQuery = trajectorySearch.value;
    renderTrajectorySnapshot(sessionId);
});
trajectoryLoadOlder.addEventListener('click', () => loadOlderSessionHistory(sessionId));
const sessionRuntime = new window.BianbuSessionRuntime.SessionRuntime(
    sid => openSessionHistory(sid, true),
);
sessionRuntime.select(sessionId);
const sessionConnection = new window.BianbuSessionRuntime.SessionConnection(
    sessionRuntime,
    {
        onSubscribed: (sid, serverLastSeq) => {
            const model = sessionRuntime.get(sid);
            if (
                (!model.opened && sid === sessionId)
                || (model.opened && serverLastSeq > model.lastSeq)
            ) {
                void openSessionHistory(sid, model.opened);
            }
        },
        onEvent: (sid, event) => {
            renderSessionStatus(sid);
            if (event.type === 'approval/requested') {
                trackApprovalEvent(sid, String(event.run_id || ''), {
                    type: 'approval_required',
                    approval_id: event.data?.approval_id,
                });
            } else if (event.type === 'approval/resolved') {
                trackApprovalEvent(sid, String(event.run_id || ''), {
                    type: 'approval_resolved',
                    approval_id: event.data?.approval_id,
                });
            }
            const terminal = ['run/completed', 'run/failed', 'run/cancelled'].includes(event.type);
            if (terminal) {
                _markRunTerminal(sid, String(event.run_id || ''));
            }
            if (sid !== sessionId) return;
            if (terminal) {
                document.querySelectorAll('.chat-loading-indicator').forEach(el => el.remove());
                resetActiveRunControls(String(event.run_id || ''));
            }
            scheduleSessionModelSnapshot(sid, terminal);
        },
        onHost: sessions => {
            applyHostSessionSnapshot(sessions);
            sessions.forEach(item => renderSessionStatus(item.id));
            const summary = sessionRuntime.summary(sessionId);
            if (summary.active_run_id) setActiveRunControls(summary.active_run_id);
            else resetActiveRunControls();
        },
    },
);
sessionConnection.connect();

let pendingSessionRenderFrame = null;
let pendingSessionRenderId = null;

function scheduleSessionModelSnapshot(sid, immediate = false) {
    pendingSessionRenderId = String(sid);
    if (immediate) {
        if (pendingSessionRenderFrame !== null) {
            cancelAnimationFrame(pendingSessionRenderFrame);
            pendingSessionRenderFrame = null;
        }
        const owner = pendingSessionRenderId;
        pendingSessionRenderId = null;
        if (owner === sessionId) {
            renderLiveSessionModelSnapshot(owner);
            if (activeConversationView === 'trajectory') {
                renderTrajectorySnapshot(owner, true);
            }
            scrollChatToBottom();
        }
        return;
    }
    if (pendingSessionRenderFrame !== null) return;
    pendingSessionRenderFrame = requestAnimationFrame(() => {
        pendingSessionRenderFrame = null;
        const owner = pendingSessionRenderId;
        pendingSessionRenderId = null;
        if (owner === sessionId) {
            renderLiveSessionModelSnapshot(owner);
            if (activeConversationView === 'trajectory') {
                renderTrajectorySnapshot(owner, true);
            }
            scrollChatToBottom();
        }
    });
}

function sessionStateLabel(summary) {
    if (summary.waiting) return ['waiting', '等待操作'];
    if (summary.running) return ['running', '生成中'];
    if (summary.failed) return ['failed', '失败'];
    if (summary.completed_unread) return ['completed', '已完成'];
    return ['', ''];
}

function isSafeSessionResourceId(value) {
    return /^[A-Za-z0-9._:-]+$/.test(String(value || ''));
}

function renderSessionStatus(sid) {
    const item = document.querySelector(`.session-item[data-session-id="${CSS.escape(String(sid))}"]`);
    if (!item) return;
    const summary = sessionRuntime.summary(sid);
    const [state, label] = sessionStateLabel(summary);
    // 主机快照会反复调用这里；状态没变就不要碰 DOM，否则整行跟着重画。
    if (item.dataset.runState !== state) item.dataset.runState = state;
    let badge = item.querySelector('.session-run-state');
    if (!state) {
        if (badge) badge.remove();
        return;
    }
    if (!badge) {
        badge = document.createElement('span');
        badge.className = 'session-run-state';
        item.querySelector('.session-title-wrap')?.appendChild(badge);
    }
    const badgeClass = `session-run-state ${state}`;
    const badgeTitle = summary.pending_interaction === 'approval' ? '需要批准工具调用' : label;
    if (badge.className !== badgeClass) badge.className = badgeClass;
    if (badge.textContent !== label) badge.textContent = label;
    if (badge.title !== badgeTitle) badge.title = badgeTitle;
}

async function openSessionHistory(sid, force = false) {
    const owner = String(sid || '');
    if (!owner || !isSafeSessionResourceId(owner)) return;
    const model = sessionRuntime.get(owner);
    const incrementalRepair = force && model.opened;
    if (model.opened && !force) {
        if (owner === sessionId) renderSessionModelSnapshot(owner);
        return;
    }
    const generation = model.beginHistory();
    try {
        const response = await fetch(
            `${PUBLIC_API_BASE}/sessions/${encodeURIComponent(owner)}/history?max_messages=50`,
            {cache: 'no-store'},
        );
        if (!response.ok) {
            model.abortHistory(generation);
            return;
        }
        const payload = await response.json();
        if (!model.installTail(payload.data || [], {
            generation,
            hasMore: !!payload.has_more,
            hiddenRunIds: payload.hidden_run_ids || [],
        })) return;
        reconcileInstalledSession(owner, model);
        if (owner === sessionId) {
            if (incrementalRepair) {
                renderLiveSessionModelSnapshot(owner);
                if (activeConversationView === 'trajectory') {
                    renderTrajectorySnapshot(owner, true);
                }
                scrollChatToBottom();
            } else {
                renderSessionModelSnapshot(owner);
                scrollChatToBottom(true);
            }
        }
    } catch (_) {
        // The shared mux remains connected; its buffered events are retained.
        model.abortHistory(generation);
    }
}

async function loadOlderSessionHistory(sid) {
    const owner = String(sid || '');
    const model = sessionRuntime.get(owner);
    if (!owner || !model.opened || !model.hasMore || model.historyPending) return;
    model.historyPending = true;
    const beforeSeq = model.baseSeq;
    const oldHeight = messagesDiv.scrollHeight;
    const oldTop = messagesDiv.scrollTop;
    try {
        const response = await fetch(
            `${PUBLIC_API_BASE}/sessions/${encodeURIComponent(owner)}/history`
                + `?before_seq=${beforeSeq}&max_messages=50`,
            {cache: 'no-store'},
        );
        if (!response.ok) return;
        const payload = await response.json();
        model.prependHistory(payload.data || [], {
            hasMore: !!payload.has_more,
            hiddenRunIds: payload.hidden_run_ids || [],
        });
        if (owner === sessionId) {
            prependSessionModelSnapshot(owner);
            if (activeConversationView === 'trajectory') renderTrajectorySnapshot(owner);
            messagesDiv.scrollTop = oldTop + (messagesDiv.scrollHeight - oldHeight);
        }
    } finally {
        model.historyPending = false;
    }
}

function reconcileInstalledSession(owner, model) {
    const activeRunId = sessionActiveRequest[owner];
    if (!activeRunId || !model.isRunTerminal(activeRunId)) return;
    _markRunTerminal(owner, activeRunId);
    if (owner === sessionId) {
        document.querySelectorAll('.chat-loading-indicator').forEach(el => el.remove());
        resetActiveRunControls(activeRunId);
    }
}

localStorage.removeItem('bianbu_session_agents');
let activeAgent = null;
const sessionsById = new Map();
queueMicrotask(() => renderActiveAgent());

function renderSessionModelSnapshot(sid) {
    const snapshot = sessionRuntime.get(sid).snapshot();
    const waiting = messagesDiv.querySelector('.chat-loading-indicator');
    messagesDiv.innerHTML = '';
    ensureOlderHistoryControl(sid, snapshot.has_more);
    snapshot.messages.forEach(message => {
        const timestamp = message.created_at
            ? new Date(Number(message.created_at) * 1000) : new Date();
        if (message.role === 'system' && message.kind === 'context_compression') {
            appendContextCompressionDivider(messagesDiv, message);
            return;
        }
        if (message.role === 'system' && message.kind === 'model_recovery') {
            const notice = document.createElement('div');
            notice.className = 'context-compression-divider';
            notice.setAttribute('role', 'status');
            notice.textContent = message.message || '正在保留已有进度并分步继续。';
            notice.dataset.sessionMessageKey = sessionMessageKey(message);
            messagesDiv.appendChild(notice);
            return;
        }
        let element;
        if (message.role === 'user') {
            element = createUserMessageEl(
                message.content, timestamp, message.attachments || [],
            );
        } else {
            element = createBotMessageEl(
                message.content || message.error?.message || '',
                timestamp, message.run_id || null, message,
            );
        }
        element.dataset.sessionSnapshot = 'true';
        if (message.run_id) element.dataset.requestId = message.run_id;
        if (message.segment_id) element.dataset.sessionSegmentId = message.segment_id;
        if (message.message_id) element.dataset.messageId = message.message_id;
        element.dataset.sessionMessageKey = sessionMessageKey(message);
        messagesDiv.appendChild(element);
        (message.approvals || []).forEach(approval => {
            messagesDiv.appendChild(createApprovalCard(message.run_id, approval));
        });
    });
    if (waiting) {
        messagesDiv.appendChild(waiting);
        removeResolvedLoadingIndicators();
    }
    if (sid === sessionId) {
        const usages = snapshot.messages
            .map(message => normalizeTokenUsage(message.usage))
            .filter(Boolean);
        setSessionTokenUsage(usages.length ? {
            input_tokens: usages.reduce((sum, item) => sum + item.input_tokens, 0),
            output_tokens: usages.reduce((sum, item) => sum + item.output_tokens, 0),
            total_tokens: usages.reduce((sum, item) => sum + item.total_tokens, 0),
        } : null, sid);
        syncRunProgressIndicator(sid);
        if (activeConversationView === 'trajectory') renderTrajectorySnapshot(sid);
    }
    return snapshot.messages.length > 0;
}

function sessionMessageKey(message) {
    if (message.role === 'system') return `${message.run_id || ''}:system:${message.kind || ''}:${message._seq ?? ''}`;
    if (message.role === 'user') {
        return `${message.run_id || ''}:user:${message.message_id || 'initial'}`;
    }
    return `${message.run_id || ''}:assistant:${message.segment_id || ''}`;
}

function ensureOlderHistoryControl(sid, visible) {
    let button = messagesDiv.querySelector(':scope > .load-older-history');
    if (!visible) {
        button?.remove();
        return null;
    }
    if (!button) {
        button = document.createElement('button');
        button.className = 'load-older-history mx-auto my-3 block text-xs text-slate-500 hover:text-primary-500';
        button.textContent = currentLang === 'en' ? 'Load earlier messages' : '加载更早消息';
        button.onclick = () => loadOlderSessionHistory(sid);
        messagesDiv.prepend(button);
    }
    return button;
}

function prependSessionModelSnapshot(sid) {
    const snapshot = sessionRuntime.get(sid).snapshot();
    const existing = new Set(Array.from(
        messagesDiv.querySelectorAll('[data-session-message-key]')
    ).map(element => element.dataset.sessionMessageKey));
    const fragment = document.createDocumentFragment();
    snapshot.messages.forEach(message => {
        const key = sessionMessageKey(message);
        if (existing.has(key) || message.role === 'system') return;
        const timestamp = message.created_at
            ? new Date(Number(message.created_at) * 1000) : new Date();
        const element = message.role === 'user'
            ? createUserMessageEl(message.content, timestamp, message.attachments || [])
            : createBotMessageEl(
                message.content || message.error?.message || '', timestamp,
                message.run_id || null, message,
            );
        element.dataset.sessionMessageKey = key;
        if (message.run_id) element.dataset.requestId = message.run_id;
        if (message.segment_id) element.dataset.sessionSegmentId = message.segment_id;
        if (message.message_id) element.dataset.messageId = message.message_id;
        fragment.appendChild(element);
    });
    const button = ensureOlderHistoryControl(sid, snapshot.has_more);
    messagesDiv.insertBefore(fragment, button ? button.nextSibling : messagesDiv.firstChild);
}

function sameAssistantStepKind(left, right) {
    const kinds = ['agent-thinking-step', 'agent-content-step', 'agent-tool-step'];
    return kinds.some(kind => left.classList.contains(kind)
        && right.classList.contains(kind));
}

function patchAssistantStep(current, next) {
    const userToggled = current.dataset.userToggled === 'true';
    const expanded = current.classList.contains('expanded');
    if (current.classList.contains('agent-tool-step')) {
        const currentHeader = current.querySelector('.tool-header');
        const nextHeader = next.querySelector('.tool-header');
        if (currentHeader && nextHeader && currentHeader.innerHTML !== nextHeader.innerHTML) {
            currentHeader.innerHTML = nextHeader.innerHTML;
        }
        const currentDetail = current.querySelector('.tool-detail');
        const nextDetail = next.querySelector('.tool-detail');
        if (currentDetail && nextDetail && currentDetail.innerHTML !== nextDetail.innerHTML) {
            currentDetail.innerHTML = nextDetail.innerHTML;
        }
    } else if (current.classList.contains('agent-thinking-step')) {
        const currentSummary = current.querySelector('.thinking-summary');
        const nextSummary = next.querySelector('.thinking-summary');
        if (currentSummary && nextSummary) currentSummary.textContent = nextSummary.textContent;
        const currentFull = current.querySelector('.thinking-full');
        const nextFull = next.querySelector('.thinking-full');
        if (currentFull && nextFull && currentFull.innerHTML !== nextFull.innerHTML) {
            currentFull.innerHTML = nextFull.innerHTML;
        }
    } else {
        const currentBody = current.querySelector('.agent-content-body');
        const nextBody = next.querySelector('.agent-content-body');
        if (currentBody && nextBody && currentBody.innerHTML !== nextBody.innerHTML) {
            currentBody.innerHTML = nextBody.innerHTML;
        }
    }
    current.className = next.className;
    Object.entries(next.dataset).forEach(([key, value]) => {
        current.dataset[key] = value;
    });
    if (current.classList.contains('agent-tool-step') || userToggled) {
        // Tool input/output is often inspected while the run keeps streaming.
        // Updating its leaves must never close the disclosure the user opened.
        current.dataset.userToggled = 'true';
        current.classList.toggle('expanded', expanded);
    }
    return current;
}

function reconcileAssistantSteps(currentHost, nextHost) {
    const current = Array.from(currentHost.children);
    const next = Array.from(nextHost.children);
    next.forEach((candidate, index) => {
        const existing = current[index];
        const node = existing && sameAssistantStepKind(existing, candidate)
            ? patchAssistantStep(existing, candidate)
            : candidate;
        currentHost.appendChild(node);
    });
    current.slice(next.length).forEach(node => node.remove());
}

function reconcileOptionalMessageContent(currentHost, nextHost, selector) {
    const current = currentHost?.querySelector(selector);
    const next = nextHost?.querySelector(selector);
    if (!next) {
        current?.remove();
        return;
    }
    if (!current) {
        currentHost?.appendChild(next);
        return;
    }
    if (current.outerHTML !== next.outerHTML) current.replaceWith(next);
}

function patchBotMessageElement(current, message, timestamp) {
    const next = createBotMessageEl(
        message.content || message.error?.message || '',
        timestamp, message.run_id || null, message,
    );
    const currentContent = current.querySelector('.msg-content');
    const nextContent = next.querySelector('.msg-content');
    let currentSteps = currentContent?.querySelector('.agent-steps');
    const nextSteps = nextContent?.querySelector('.agent-steps');
    if (nextSteps) {
        if (!currentSteps) {
            currentSteps = nextSteps.cloneNode(false);
            currentContent?.querySelector('.answer-content')?.before(currentSteps);
        }
        reconcileAssistantSteps(currentSteps, nextSteps);
    } else if (currentSteps) {
        currentSteps.remove();
    }

    const currentAnswer = currentContent?.querySelector('.answer-content');
    const nextAnswer = nextContent?.querySelector('.answer-content');
    if (currentAnswer && nextAnswer
        && currentAnswer.dataset.rawMd !== nextAnswer.dataset.rawMd) {
        currentAnswer.innerHTML = nextAnswer.innerHTML;
        currentAnswer.dataset.rawMd = nextAnswer.dataset.rawMd || '';
        applyHighlighting(currentAnswer);
    }
    reconcileOptionalMessageContent(
        currentContent, nextContent, ':scope > .agent-terminal-error'
    );
    attachTokenUsage(current, message.usage);
    if (message.status === 'cancelled'
        && !currentContent?.querySelector('.agent-cancelled-tag')) {
        const tag = nextContent?.querySelector('.agent-cancelled-tag');
        if (tag) currentContent.appendChild(tag.cloneNode(true));
    }
    reconcileOptionalMessageContent(
        currentContent, nextContent, ':scope > .stored-tool-media'
    );
    reconcileOptionalMessageContent(
        currentContent, nextContent, ':scope > .stored-artifacts'
    );
    const currentAudio = currentContent?.querySelector(':scope > .bot-audio-slot');
    const nextAudio = nextContent?.querySelector(':scope > .bot-audio-slot');
    if (currentAudio && nextAudio
        && currentAudio.dataset.audioUrl !== nextAudio.dataset.audioUrl) {
        currentAudio.replaceWith(nextAudio);
    }
    return current;
}

function findRenderedBotMessage(runId, segmentId) {
    return Array.from(messagesDiv.querySelectorAll('.bot-message-group'))
        .find(element => String(element.dataset.requestId || '') === String(runId || '')
            && element.dataset.sessionSegmentId === String(segmentId || '')) || null;
}

function findRenderedUserMessage(message) {
    const runId = String(message.run_id || '');
    const byRun = Array.from(messagesDiv.querySelectorAll('.user-message-group'))
        .find(element => element.dataset.requestId === runId);
    if (byRun) return byRun;
    return Array.from(messagesDiv.querySelectorAll('.user-message-group'))
        .find(element => !element.dataset.requestId
            && element.querySelector('.user-bubble')?.textContent.trim()
                === String(message.content || '').trim()) || null;
}

function reconcileLiveMessageOrder(snapshot) {
    const elements = snapshot.messages
        .filter(message => message.role !== 'system')
        .map(message => {
            const key = sessionMessageKey(message);
            return Array.from(
                messagesDiv.querySelectorAll('[data-session-message-key]')
            ).find(element => element.dataset.sessionMessageKey === key) || null;
        })
        .filter(Boolean);
    // Work backwards and move only pairs that are inverted. This preserves
    // node identity, streaming state, scroll behavior, and unrelated controls.
    let anchor = null;
    for (let index = elements.length - 1; index >= 0; index -= 1) {
        const element = elements[index];
        if (anchor) {
            const children = Array.from(messagesDiv.children);
            if (children.indexOf(element) > children.indexOf(anchor)) {
                messagesDiv.insertBefore(element, anchor);
            }
        }
        anchor = element;
    }
}

function renderLiveSessionModelSnapshot(sid) {
    if (String(sid) !== sessionId) return false;
    const snapshot = sessionRuntime.get(sid).snapshot();
    const approvalIds = new Set();
    snapshot.messages.forEach(message => {
        if (message.role === 'system' && message.kind === 'context_compression') {
            appendContextCompressionDivider(messagesDiv, message);
            return;
        }
        if (message.role === 'user') {
            let user;
            if (message.message_id) {
                user = appendPendingInputMessage(
                    message.message_id, message.content,
                    message.attachments || [],
                    message.created_at
                        ? new Date(Number(message.created_at) * 1000) : new Date(),
                );
            } else {
                user = findRenderedUserMessage(message);
                if (!user) {
                    user = createUserMessageEl(
                        message.content,
                        message.created_at
                            ? new Date(Number(message.created_at) * 1000) : new Date(),
                        message.attachments || [],
                    );
                    messagesDiv.appendChild(user);
                }
            }
            if (!user) return;
            user.dataset.sessionSnapshot = 'true';
            user.dataset.sessionMessageKey = sessionMessageKey(message);
            if (message.run_id) user.dataset.requestId = message.run_id;
            if (message.message_id) user.dataset.messageId = message.message_id;
            return;
        }
        const timestamp = message.created_at
            ? new Date(Number(message.created_at) * 1000) : new Date();
        let element = findRenderedBotMessage(message.run_id, message.segment_id);
        if (element && !element.classList.contains('security-denied-group')) {
            patchBotMessageElement(element, message, timestamp);
        } else if (!element) {
            element = createBotMessageEl(
                message.content || message.error?.message || '',
                timestamp, message.run_id || null, message,
            );
            element.dataset.sessionSnapshot = 'true';
            element.dataset.sessionMessageKey = sessionMessageKey(message);
            if (message.run_id) element.dataset.requestId = message.run_id;
            if (message.segment_id) {
                element.dataset.sessionSegmentId = message.segment_id;
            }
            const loading = messagesDiv.querySelector('.chat-loading-indicator');
            messagesDiv.insertBefore(element, loading);
        }
        (message.approvals || []).forEach(approval => {
            const approvalId = String(approval.approval_id || '');
            approvalIds.add(approvalId);
            const exists = Array.from(messagesDiv.querySelectorAll('.approval-card'))
                .some(card => card.dataset.approvalId === approvalId);
            if (!exists) messagesDiv.appendChild(createApprovalCard(message.run_id, approval));
        });
    });
    messagesDiv.querySelectorAll('.approval-card').forEach(card => {
        if (!approvalIds.has(String(card.dataset.approvalId || ''))) {
            (card.closest('.approval-message-group') || card).remove();
        }
    });
    reconcileLiveMessageOrder(snapshot);
    removeResolvedLoadingIndicators();
    return snapshot.messages.length > 0;
}

function restoreInitialConversation() {
    return restorePersistedSession(sessionId).then(record => {
        if (!record) return null;
        return openSessionHistory(sessionId);
    }).finally(() => {
        restorePendingApproval();
    });
}

fetch(`${PUBLIC_API_BASE}/settings`).then(responseJson).then(settingsView).then(data => {
    if (data.status === 'success') {
        appConfig = data;
        const title = data.title || 'Bianbu Agent';
        document.getElementById('welcome-title').textContent = title;
    }
    restoreInitialConversation();
}).catch(() => {
    restoreInitialConversation();
});

// Smart auto-scroll: pause when user scrolls up, resume when near bottom
let _autoScrollEnabled = true;
const _SCROLL_THRESHOLD = 80; // px from bottom to re-enable auto-scroll

messagesDiv.addEventListener('scroll', () => {
    const distFromBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight;
    _autoScrollEnabled = distFromBottom <= _SCROLL_THRESHOLD;
    _updateScrollToBottomBtn();
});

// Intercept internal navigation links in chat messages
messagesDiv.addEventListener('click', (e) => {
    // Code block copy button
    const codeCopyBtn = e.target.closest('.code-copy-btn');
    if (codeCopyBtn) {
        e.preventDefault();
        const wrapper = codeCopyBtn.closest('.code-block-wrapper');
        const codeEl = wrapper && wrapper.querySelector('pre code');
        if (codeEl) {
            const codeText = codeEl.textContent;
            copyToClipboard(codeText).then(() => {
                const icon = codeCopyBtn.querySelector('i');
                if (icon) { icon.className = 'fas fa-check'; setTimeout(() => { icon.className = 'fas fa-copy'; }, 1500); }
            });
        }
        return;
    }

    const copyBtn = e.target.closest('.copy-msg-btn');
    if (copyBtn) {
        e.preventDefault();
        const msgRoot = copyBtn.closest('.flex.gap-3');
        const answerEl = msgRoot && msgRoot.querySelector('.answer-content');
        const rawMd = answerEl && answerEl.dataset.rawMd;
        if (rawMd) {
            copyToClipboard(rawMd).then(() => {
                const icon = copyBtn.querySelector('i');
                if (icon) { icon.className = 'fas fa-check'; setTimeout(() => { icon.className = 'fas fa-copy'; }, 1500); }
            });
        }
        return;
    }

});
const attachmentPreview = document.getElementById('attachment-preview');
const selectedSkillsEl = document.getElementById('selected-skills');
const skillPicker = document.getElementById('skill-picker');
const skillPickerSearch = document.getElementById('skill-picker-search');
const skillPickerStatus = document.getElementById('skill-picker-status');
const skillPickerList = document.getElementById('skill-picker-list');
const selectedSkills = new Map();
let availableSkills = [];
let filteredSkills = [];
let skillPickerActiveIdx = 0;

// Pending attachments: [{file_path, file_name, file_type, preview_url}]
// Items with _uploading=true are still in flight.
let pendingAttachments = [];
let uploadingCount = 0;

// Skills written in Power format v2 carry a display name per language
// (`name_zh` / `name_en`) next to the call name `name`. The picker follows the
// console language and falls back to the old `display_name`/`name` chain, so a
// Skill written before v2, or one from a vendor package, stays readable.
function localizedCapabilityName(item) {
    if (!item) return '';
    const fallback = String(item.display_name || item.name || item.id || '');
    const preferred = currentLang === 'zh' ? item.name_zh : item.name_en;
    return String(preferred || '').trim() || fallback;
}

// Skill descriptions follow the same rule: the Chinese text is only shown in
// Chinese, and any other language keeps the original (model-facing) text.
function localizedCapabilityDescription(item) {
    if (!item) return '';
    const fallback = String(item.description || '');
    if (currentLang !== 'zh') return fallback;
    return String(item.description_zh || '').trim() || fallback;
}

// Plugin Skills are addressed as `plugin-id:skill-name` everywhere the user
// searches, so the picker does not hide which Plugin owns a Skill.
function skillDisplayName(skill) {
    const base = localizedCapabilityName(skill);
    const pluginId = String(skill.plugin_id || '').trim();
    if (!pluginId || base.startsWith(`${pluginId}:`) || base.startsWith(`${pluginId}/`)) {
        return base;
    }
    return `${pluginId}:${base}`;
}

function selectedSkillIds() {
    return Array.from(selectedSkills.keys());
}

function renderSelectedSkills() {
    selectedSkillsEl.replaceChildren();
    selectedSkillsEl.classList.toggle('hidden', selectedSkills.size === 0);
    selectedSkills.forEach((skill, skillId) => {
        const chip = document.createElement('div');
        chip.className = 'selected-skill-chip';
        const label = document.createElement('span');
        label.textContent = skillDisplayName(skill);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.textContent = '×';
        remove.setAttribute('aria-label', `${t('skill_picker_remove')}: ${skillDisplayName(skill)}`);
        remove.addEventListener('click', () => {
            selectedSkills.delete(skillId);
            renderSelectedSkills();
            renderSkillPickerItems();
        });
        chip.append(label, remove);
        selectedSkillsEl.appendChild(chip);
    });
}

function consumeSelectedSkills(skillIds) {
    skillIds.forEach(skillId => selectedSkills.delete(skillId));
    renderSelectedSkills();
}

function clearSelectedSkills() {
    selectedSkills.clear();
    renderSelectedSkills();
}

// 专家已经在跑的 Skill 有两类：它自己点名的，以及它选的 Plugin 附带的 —— Plugin
// 一旦进会话，名下 Skill 就跟着一起跑（Tool / MCP 也会把它们的 Plugin 拉进来，所以
// 声明里带 `插件id/成员名` 的条目同样算这个 Plugin 在场）。两类都不该再出现在临时
// 技能里：用户以为"临时加一次"，其实它本来就在跑。预设 Skill 固定启用，同理排除。
function expertOwnedSelectors() {
    const skills = new Set((activeAgent?.skills || []).map(String));
    const plugins = new Set((activeAgent?.plugins || []).map(String));
    for (const selector of [...skills, ...(activeAgent?.mcps || []).map(String)]) {
        const separator = selector.indexOf('/');
        if (separator > 0) plugins.add(selector.slice(0, separator));
    }
    return { skills, plugins };
}

function skillCandidate(skill) {
    if (!skill || typeof skill.id !== 'string' || !skill.id) return false;
    if (skill.enabled === false || skill.assignable === false) return false;
    if (skill.preset === true) return false;
    const owned = expertOwnedSelectors();
    if (owned.skills.has('*') || owned.plugins.has('*')) return false;
    if (owned.skills.has(String(skill.id))) return false;
    const pluginId = String(skill.plugin_id || '').trim();
    return !pluginId || !owned.plugins.has(pluginId);
}

// 名字完全相同 → 前缀 → 包含 → 只命中描述；同层保持目录顺序。
function matchSkills(query) {
    const q = String(query || '').trim().toLowerCase();
    return availableSkills.filter(skillCandidate).map(skill => {
        const names = [skill.id, skill.name, skill.display_name, skill.name_zh, skill.name_en,
            skill.plugin_id, skillDisplayName(skill)]
            .filter(Boolean).map(value => value.toLowerCase());
        let rank = 4;
        if (!q || names.some(name => name === q)) rank = 0;
        else if (names.some(name => name.startsWith(q))) rank = 1;
        else if (names.some(name => name.includes(q))) rank = 2;
        else if (localizedCapabilityDescription(skill).toLowerCase().includes(q)) rank = 3;
        return {skill, rank};
    }).filter(item => item.rank < 4)
        .sort((a, b) => a.rank - b.rank)
        .map(item => item.skill);
}

async function loadAvailableSkills() {
    const result = await fetch(`${PUBLIC_API_BASE}/capabilities/skill`).then(responseJson);
    if (result.status !== 'success' || !Array.isArray(result.data)) throw new Error('skill catalog unavailable');
    availableSkills = result.data;
}

function renderSkillPickerItems() {
    filteredSkills = matchSkills(skillPickerSearch.value);
    skillPickerActiveIdx = Math.min(
        Math.max(skillPickerActiveIdx, 0),
        Math.max(filteredSkills.length - 1, 0),
    );
    skillPickerList.replaceChildren();
    skillPickerStatus.textContent = filteredSkills.length ? '' : t('skill_picker_empty');
    if (filteredSkills.length) {
        skillPickerSearch.setAttribute(
            'aria-activedescendant',
            `skill-picker-option-${skillPickerActiveIdx}`,
        );
    } else {
        skillPickerSearch.removeAttribute('aria-activedescendant');
    }
    filteredSkills.forEach((skill, index) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.id = `skill-picker-option-${index}`;
        item.className = `skill-picker-item${index === skillPickerActiveIdx ? ' active' : ''}`;
        item.dataset.index = String(index);
        item.setAttribute('role', 'option');
        item.setAttribute('aria-selected', selectedSkills.has(skill.id) ? 'true' : 'false');

        const name = document.createElement('div');
        name.className = 'skill-picker-item-name';
        const label = document.createElement('span');
        label.textContent = skillDisplayName(skill);
        name.appendChild(label);
        if (selectedSkills.has(skill.id)) {
            const marker = document.createElement('span');
            marker.textContent = `✓ ${t('skill_picker_selected')}`;
            name.appendChild(marker);
        }
        item.appendChild(name);
        item.addEventListener('click', () => toggleSkillSelection(index));
        item.addEventListener('mousemove', () => {
            skillPickerActiveIdx = index;
            skillPickerSearch.setAttribute('aria-activedescendant', item.id);
            skillPickerList.querySelectorAll('.skill-picker-item').forEach((node, itemIndex) => {
                node.classList.toggle('active', itemIndex === index);
            });
        });
        skillPickerList.appendChild(item);
    });
}

function toggleSkillById(skill) {
    if (!skill) return;
    if (selectedSkills.has(skill.id)) selectedSkills.delete(skill.id);
    else selectedSkills.set(skill.id, skill);
    renderSelectedSkills();
    renderSkillPickerItems();
}

function toggleSkillSelection(index) {
    toggleSkillById(filteredSkills[index]);
}

function closeSkillPicker() {
    // Only hand focus back to the composer when the picker actually owned it.
    // Closing an already-hidden picker must never steal focus from the page.
    const wasOpen = !skillPicker.classList.contains('hidden');
    skillPicker.classList.add('hidden');
    if (wasOpen) chatInput.focus();
}

async function openSkillPicker() {
    hideSlashMenu();
    clearComposerText();
    skillPicker.classList.remove('hidden');
    skillPickerSearch.value = '';
    skillPickerList.replaceChildren();
    skillPickerStatus.textContent = t('skill_picker_loading');
    skillPickerSearch.focus();
    try {
        await loadAvailableSkills();
        skillPickerActiveIdx = 0;
        renderSkillPickerItems();
    } catch (error) {
        console.warn('[skills.picker]', error);
        availableSkills = [];
        filteredSkills = [];
        skillPickerList.replaceChildren();
        skillPickerStatus.textContent = t('skill_picker_error');
    }
}

document.getElementById('skill-picker-close').addEventListener('click', closeSkillPicker);
document.addEventListener('mousedown', event => {
    if (skillPicker.classList.contains('hidden')) return;
    if (skillPicker.contains(event.target) || event.target.closest('#slash-menu')) return;
    closeSkillPicker();
});
skillPickerSearch.addEventListener('input', () => {
    skillPickerActiveIdx = 0;
    renderSkillPickerItems();
});
skillPickerSearch.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
        event.preventDefault();
        closeSkillPicker();
        return;
    }
    if (event.key === 'ArrowDown') {
        event.preventDefault();
        skillPickerActiveIdx = Math.min(skillPickerActiveIdx + 1, filteredSkills.length - 1);
        renderSkillPickerItems();
        return;
    }
    if (event.key === 'ArrowUp') {
        event.preventDefault();
        skillPickerActiveIdx = Math.max(skillPickerActiveIdx - 1, 0);
        renderSkillPickerItems();
        return;
    }
    if (event.key === 'Enter') {
        event.preventDefault();
        toggleSkillSelection(skillPickerActiveIdx);
    }
});

// Input history (like terminal arrow-key recall)
const inputHistory = [];
let historyIdx = -1;
let historySavedDraft = '';

// The composer always sends. Stop is a separate control for the active Run.
let activeRequestId = null;

function hasPendingApproval(ownerSession = sessionId) {
    return !!ownerSession
        && (sessionPendingApprovals.get(ownerSession)?.ids?.size || 0) > 0;
}

function approvalRevision(ownerSession) {
    return sessionPendingApprovals.get(ownerSession)?.revision || 0;
}

function setSessionPendingApprovals(
    ownerSession,
    requestId,
    actions,
    expectedRevision = null,
) {
    if (!ownerSession || !requestId) return false;
    const current = sessionPendingApprovals.get(ownerSession);
    const currentRevision = current?.revision || 0;
    if (expectedRevision !== null && currentRevision !== expectedRevision) {
        return false;
    }
    const ids = new Set(
        (Array.isArray(actions) ? actions : [])
            .filter(action => action?.type === 'approval_required' && action.approval_id)
            .map(action => String(action.approval_id))
    );
    sessionPendingApprovals.set(ownerSession, {
        requestId,
        ids,
        revision: currentRevision + 1,
    });
    if (ownerSession === sessionId) {
        updateComposerInteractionState();
        syncRunProgressIndicator(ownerSession);
    }
    return true;
}

function trackApprovalEvent(ownerSession, requestId, item) {
    if (!ownerSession || !requestId || !item) return;
    const approvalId = String(item.approval_id || '');
    if (!approvalId) return;
    const current = sessionPendingApprovals.get(ownerSession);
    const ids = new Set(
        current?.requestId === requestId ? current.ids : []
    );
    ids.delete('__session_pending_approval__');
    if (item.type === 'approval_required') ids.add(approvalId);
    else if (item.type === 'approval_resolved') ids.delete(approvalId);
    else return;
    sessionPendingApprovals.set(ownerSession, {
        requestId,
        ids,
        revision: (current?.revision || 0) + 1,
    });
    if (ownerSession === sessionId) {
        updateComposerInteractionState();
        syncRunProgressIndicator(ownerSession);
    }
}

function clearSessionPendingApprovals(ownerSession, requestId) {
    if (!ownerSession || !requestId) return;
    const current = sessionPendingApprovals.get(ownerSession);
    if (!current || current.requestId !== requestId) return;
    sessionPendingApprovals.set(ownerSession, {
        requestId,
        ids: new Set(),
        revision: current.revision + 1,
    });
    if (ownerSession === sessionId) {
        updateComposerInteractionState();
        syncRunProgressIndicator(ownerSession);
    }
}

function setActiveRunControls(requestId) {
    activeRequestId = requestId;
    stopBtn.classList.remove('hidden');
    stopBtn.classList.add('flex');
    stopBtn.disabled = false;
    updateComposerInteractionState();
}

function resetActiveRunControls(completedRequestId = null) {
    if (
        completedRequestId
        && activeRequestId
        && String(activeRequestId) !== String(completedRequestId)
    ) return;
    activeRequestId = null;
    stopBtn.classList.add('hidden');
    stopBtn.classList.remove('flex');
    stopBtn.disabled = true;
    updateSendBtnState();
    updateComposerInteractionState();
}

function updateActiveRunControls() {
    const active = !!activeRequestId;
    stopBtn.classList.toggle('hidden', !active);
    stopBtn.classList.toggle('flex', active);
    if (!active) stopBtn.disabled = true;
}

function updateComposerInteractionState() {
    const missingAgent = !activeAgent;
    const unavailable = activeAgent?.available === false;
    const needsModelSetup = modelSetupRequired();
    const approvalBlocked = hasPendingApproval();
    chatInput.disabled = missingAgent || unavailable || approvalBlocked;
    if (attachBtn) attachBtn.disabled = missingAgent || unavailable || approvalBlocked;
    chatInput.placeholder = t(
        approvalBlocked
            ? 'approval_input_blocked'
            : missingAgent
                ? 'agents_choose_hint'
            : needsModelSetup
                ? 'model_setup_input_hint'
                : unavailable
                ? 'agents_unavailable_hint'
                : 'input_placeholder'
    );
    if (missingAgent || unavailable || approvalBlocked) {
        hideSlashMenu();
    }
    updateSendBtnState();
    updateActiveRunControls();
}

async function requestActiveStop() {
    const reqId = activeRequestId;
    if (!reqId) return;
    stopBtn.disabled = true;
    fetch(`${PUBLIC_API_BASE}/sessions/${encodeURIComponent(sessionId)}/cancel`, {
        method: 'POST',
    }).catch(err => console.warn('[run control] request failed', err));
    // Optimistic UI lock until the Session terminal event arrives.
    stopBtn.disabled = true;
}

stopBtn.addEventListener('click', requestActiveStop);
sendBtn.addEventListener('click', sendMessage);

document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && agentMentionRange) {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeChatAgentMenu();
        chatInput.focus();
        return;
    }
    if (event.key !== 'Escape' || !activeRequestId) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    void requestActiveStop();
}, true);

function updateSendBtnState() {
    sendBtn.disabled = runtimeMaintenanceBlocked || !activeAgent
        || activeAgent.available === false
        || hasPendingApproval()
        || uploadingCount > 0
        || (!chatInput.value.trim() && pendingAttachments.length === 0);
    updateActiveRunControls();
}

function renderAttachmentPreview() {
    if (pendingAttachments.length === 0) {
        attachmentPreview.classList.add('hidden');
        attachmentPreview.innerHTML = '';
        updateSendBtnState();
        return;
    }
    attachmentPreview.classList.remove('hidden');
    attachmentPreview.replaceChildren();
    pendingAttachments.forEach((att, idx) => {
        const suffix = att.file_type === 'directory' && att.file_count
            ? ` (${att.file_count})`
            : '';
        let item;
        if (att._uploading) {
            item = document.createElement('div');
            item.className = 'att-chip att-uploading';
            const spinner = document.createElement('i');
            spinner.className = 'fas fa-spinner fa-spin';
            item.appendChild(spinner);
        } else if (att.file_type === 'image') {
            item = document.createElement('div');
            item.className = 'att-thumb';
            const safeUrl = safeMediaUrl(att.preview_url);
            if (safeUrl) {
                const image = document.createElement('img');
                image.src = safeUrl;
                image.alt = String(att.file_name || '');
                item.appendChild(image);
            }
        } else {
            item = document.createElement('div');
            item.className = 'att-chip';
            const icon = document.createElement('i');
            icon.className = `fas ${att.file_type === 'video'
                ? 'fa-film'
                : (att.file_type === 'directory' ? 'fa-folder-tree' : 'fa-file-alt')}`;
            item.appendChild(icon);
        }
        item.dataset.idx = String(idx);
        if (att.file_type !== 'image' || att._uploading) {
            const name = document.createElement('span');
            name.className = 'att-name';
            name.textContent = `${String(att.file_name || '')}${suffix}`;
            item.appendChild(name);
        }
        if (!att._uploading) {
            const remove = document.createElement('button');
            remove.type = 'button';
            remove.className = 'att-remove';
            remove.textContent = '×';
            remove.addEventListener('click', () => removeAttachment(idx));
            item.appendChild(remove);
        }
        attachmentPreview.appendChild(item);
    });
    updateSendBtnState();
}

function removeAttachment(idx) {
    if (pendingAttachments[idx]?._uploading) return;
    pendingAttachments.splice(idx, 1);
    renderAttachmentPreview();
}

// ── Attachments ──────────────────────────────────────────────
// Attaching never blocks. A model without vision cannot describe an image, so
// the console warns at pick time, but the bytes still land in the Agent
// workspace: the model opens an image with `read_image`, any other file with
// `read_file`/`search_files`, and a directory with `search_files`/`terminal`.
const IMAGE_MEDIA_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
let composerNoticeTimer = null;
const supportsDirectoryUpload = !!attachmentFolderInput && 'webkitdirectory' in attachmentFolderInput;

if (!supportsDirectoryUpload && attachFolderOption) {
    attachFolderOption.classList.add('hidden');
}

function isImageFile(file) {
    if (!file) return false;
    if (IMAGE_MEDIA_TYPES.has(String(file.type || '').toLowerCase())) return true;
    return /\.(png|jpe?g|webp|gif)$/i.test(String(file.name || ''));
}

function showComposerNotice(message) {
    if (!composerNotice || !message) return;
    composerNotice.textContent = message;
    composerNotice.classList.remove('hidden');
    if (composerNoticeTimer) clearTimeout(composerNoticeTimer);
    composerNoticeTimer = setTimeout(() => composerNotice.classList.add('hidden'), 8000);
}

// A probe failure reads as "supported" on purpose: it only drives the warning,
// so a hiccup must not scare the user off a model that actually works.
async function visionSupport() {
    try {
        const data = await fetch(
            `${PUBLIC_API_BASE}/sessions/${encodeURIComponent(sessionId)}/vision`,
        ).then(responseJson);
        return {
            available: data.status === 'success' && Boolean(data.data?.available),
            message: String(data.data?.message || '') || t('image_model_unsupported'),
        };
    } catch (error) {
        console.warn('[vision support]', error);
        return {available: true, message: ''};
    }
}

function dropPlaceholder(placeholder) {
    const index = pendingAttachments.indexOf(placeholder);
    if (index !== -1) pendingAttachments.splice(index, 1);
}

async function uploadAttachment(file) {
    const placeholder = {file_name: file.name, file_type: 'file', _uploading: true};
    pendingAttachments.push(placeholder);
    uploadingCount += 1;
    renderAttachmentPreview();
    try {
        const form = new FormData();
        form.append('file', file);
        const response = await fetch(
            `${PUBLIC_API_BASE}/sessions/${encodeURIComponent(sessionId)}/attachments`,
            {method: 'POST', body: form},
        );
        const data = await responseJson(response);
        if (data.status !== 'success' || !data.data?.file_path) {
            dropPlaceholder(placeholder);
            showComposerNotice(data.message || t('error_send'));
            return;
        }
        placeholder.file_path = data.data.file_path;
        placeholder.file_name = data.data.file_name || file.name;
        placeholder.file_type = data.data.file_type || 'file';
        if (data.data.preview_url) placeholder.preview_url = data.data.preview_url;
        if (data.data.file_count != null) placeholder.file_count = data.data.file_count;
        delete placeholder._uploading;
    } catch (error) {
        console.warn('[attachment upload]', error);
        dropPlaceholder(placeholder);
        showComposerNotice(t('error_send'));
    } finally {
        uploadingCount -= 1;
        renderAttachmentPreview();
    }
}

function _makeUploadId() {
    return `dir_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

function _groupDirectoryFiles(files) {
    const groups = new Map();
    for (const file of Array.from(files || [])) {
        const relPath = String(file.webkitRelativePath || file.name || '');
        const parts = relPath.split('/').filter(Boolean);
        const rootName = parts[0] || file.name || 'upload';
        if (!groups.has(rootName)) groups.set(rootName, []);
        groups.get(rootName).push({file, relPath: relPath || file.name});
    }
    return groups;
}

async function uploadDirectory(rootName, entries) {
    const placeholder = {
        file_name: rootName,
        file_type: 'directory',
        file_count: entries.length,
        _uploading: true,
    };
    pendingAttachments.push(placeholder);
    uploadingCount += 1;
    renderAttachmentPreview();
    try {
        const form = new FormData();
        form.append('upload_id', _makeUploadId());
        for (const {file, relPath} of entries) {
            form.append('files', file);
            form.append('relative_paths', relPath);
        }
        const response = await fetch(
            `${PUBLIC_API_BASE}/sessions/${encodeURIComponent(sessionId)}/attachments`,
            {method: 'POST', body: form},
        );
        const data = await responseJson(response);
        if (data.status !== 'success' || !data.data?.file_path) {
            dropPlaceholder(placeholder);
            showComposerNotice(data.message || t('error_send'));
            return;
        }
        placeholder.file_path = data.data.file_path;
        placeholder.file_name = data.data.file_name || rootName;
        placeholder.file_type = data.data.file_type || 'directory';
        placeholder.file_count = data.data.file_count ?? entries.length;
        delete placeholder._uploading;
    } catch (error) {
        console.warn('[directory upload]', error);
        dropPlaceholder(placeholder);
        showComposerNotice(t('error_send'));
    } finally {
        uploadingCount -= 1;
        renderAttachmentPreview();
    }
}

async function addAttachments(files) {
    const list = Array.from(files || []);
    if (list.length === 0) return;
    if (list.some(isImageFile)) {
        const support = await visionSupport();
        if (!support.available) showComposerNotice(support.message);
    }
    await Promise.all(list.map(uploadAttachment));
}

async function addFolderAttachments(files) {
    const groups = _groupDirectoryFiles(files);
    if (groups.size === 0) return;
    await Promise.all(
        Array.from(groups.entries()).map(([root, entries]) => uploadDirectory(root, entries)),
    );
}

function isAttachMenuVisible() {
    return !!attachMenu && !attachMenu.classList.contains('hidden');
}

function hideAttachMenu() {
    if (attachMenu) attachMenu.classList.add('hidden');
}

function toggleAttachMenu(event) {
    if (!attachMenu) return;
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    closeChatAgentMenu();
    attachMenu.classList.toggle('hidden');
}

function triggerFileUpload() {
    hideAttachMenu();
    attachmentInput?.click();
}

function triggerFolderUpload() {
    hideAttachMenu();
    if (supportsDirectoryUpload) attachmentFolderInput?.click();
}

attachBtn?.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    toggleAttachMenu(event);
});

document.getElementById('attach-file-option')?.addEventListener('click', event => {
    event.preventDefault();
    triggerFileUpload();
});

document.getElementById('attach-folder-option')?.addEventListener('click', event => {
    event.preventDefault();
    triggerFolderUpload();
});

attachmentInput?.addEventListener('change', function () {
    void addAttachments(this.files);
    this.value = '';
});

attachmentFolderInput?.addEventListener('change', function () {
    void addFolderAttachments(this.files);
    this.value = '';
});

document.addEventListener('click', event => {
    if (!isAttachMenuVisible()) return;
    if (attachMenu.contains(event.target) || attachBtn.contains(event.target)) return;
    hideAttachMenu();
});

// Paste and drag & drop reach the composer the same way CowAgent accepts an
// attachment, so the picker is never the only entry point.
chatInput.addEventListener('paste', event => {
    const items = event.clipboardData?.items;
    if (!items) return;
    const files = [];
    for (const item of items) {
        if (item.kind !== 'file') continue;
        const file = item.getAsFile();
        if (file) files.push(file);
    }
    if (files.length === 0) return;
    event.preventDefault();
    void addAttachments(files);
});

['dragover', 'drop'].forEach(type => {
    chatInput.addEventListener(type, event => {
        if (!event.dataTransfer?.types?.includes('Files')) return;
        event.preventDefault();
        if (type === 'drop') void addAttachments(event.dataTransfer.files);
    });
});

chatInput.addEventListener('compositionstart', () => { isComposing = true; });
chatInput.addEventListener('compositionend', () => {
    setTimeout(() => {
        isComposing = false;
        chatInput.dispatchEvent(new Event('input'));
    }, 100);
});

// ── Slash Menu ───────────────────────────────────────────────
// 「/」只做一件事：为下一条消息临时选技能。候选取自同一个技能目录，并且只保留
// 这个专家还没有的技能（见 skillCandidate）；命令列表不再出现在菜单里。
const slashMenu = document.getElementById('slash-menu');
let slashActiveIdx = 0;
let slashFiltered = [];
let slashJustSelected = false;
let slashLastFilter = null;
let slashLastMouseX = -1;
let slashLastMouseY = -1;
let slashSkillsLoading = false;

function slashItems(filter) {
    return matchSkills(String(filter || '').replace(/^\/+/, '')).map(skill => ({
        cmd: skillDisplayName(skill),
        skill,
    }));
}

function showSlashMenu(filter) {
    const q = String(filter || '');
    if (q === slashLastFilter && !slashMenu.classList.contains('hidden')) return;
    slashLastFilter = q;
    slashFiltered = slashItems(q);
    if (!slashFiltered.length) {
        hideSlashMenu();
        if (!availableSkills.length && !slashSkillsLoading) void loadSlashSkills(q);
        return;
    }
    slashActiveIdx = Math.min(Math.max(slashActiveIdx, 0), slashFiltered.length - 1);
    slashNavByKeyboard = true;
    renderSlashItems();
    slashMenu.classList.remove('hidden');
    if (!availableSkills.length && !slashSkillsLoading) void loadSlashSkills(q);
}

// 技能目录按需拉取一次；拉回来后按当前输入重新渲染，收起菜单会顺手作废这次结果。
async function loadSlashSkills(filter) {
    slashSkillsLoading = true;
    try {
        await loadAvailableSkills();
    } catch (error) {
        console.warn('[skills.slash]', error);
        return;
    } finally {
        slashSkillsLoading = false;
    }
    // The menu is hidden exactly when the catalog had no candidates yet, which
    // is the very first `/` after a page load, so `hidden` cannot be the gate
    // for showing the result: wait for the composer instead, and re-read it
    // because the user may have typed past the filter we loaded for.
    if (!chatInput.value.startsWith('/')) return;
    slashLastFilter = null;
    showSlashMenu(chatInput.value);
}

function hideSlashMenu() {
    slashMenu.classList.add('hidden');
    slashMenu.innerHTML = '';
    slashFiltered = [];
    slashActiveIdx = -1;
    slashLastFilter = '';
    slashNavByKeyboard = false;
    slashLastMouseX = -1;
    slashLastMouseY = -1;
}

function isSlashMenuVisible() {
    return !slashMenu.classList.contains('hidden') && slashFiltered.length > 0;
}

function renderSlashItems() {
    slashMenu.innerHTML =
        `<div class="slash-menu-header">${escapeHtml(t('skill_picker_title'))}</div>` +
        slashFiltered.map((c, i) =>
            `<div class="slash-menu-item${i === slashActiveIdx ? ' active' : ''}" data-idx="${i}">` +
            `<span class="cmd">${escapeHtml(c.cmd)}</span>` +
            `<span class="desc">${escapeHtml(selectedSkills.has(c.skill.id) ? t('skill_picker_selected') : '')}</span></div>`
        ).join('');

    const activeEl = slashMenu.querySelector('.slash-menu-item.active');
    if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
}

function selectSlashCommand(idx) {
    if (idx < 0 || idx >= slashFiltered.length) return;
    const item = slashFiltered[idx];
    if (!item || !item.skill) return;
    slashJustSelected = true;
    toggleSkillById(item.skill);
    clearComposerText();
    chatInput.focus();
    slashLastFilter = null;
    showSlashMenu('');
}

chatInput.addEventListener('input', function() {
    this.style.height = '42px';
    const scrollH = this.scrollHeight;
    const newH = Math.min(scrollH, 180);
    this.style.height = newH + 'px';
    this.style.overflowY = scrollH > 180 ? 'auto' : 'hidden';
    updateSendBtnState();

    const prefix = this.value.slice(0, this.selectionStart);
    const mention = /(?:^|\s)@([^\s@]*)$/.exec(prefix);
    if (mention && !isComposing) {
        if (!agentMentionRange || agentMentionRange.query !== mention[1]) agentMentionActiveIndex = 0;
        agentMentionRange = {start: prefix.lastIndexOf('@'), end: this.selectionStart, query: mention[1]};
        agentChooserMode = 'mention';
        const menu = document.getElementById('chat-agent-menu');
        if (menu.classList.contains('hidden')) void toggleChatAgentMenu();
        else renderChatAgentMenu();
    } else if (agentMentionRange) {
        closeChatAgentMenu();
    }

    const val = this.value;
    if (slashJustSelected) {
        slashJustSelected = false;
    } else if (val.startsWith('/')) {
        showSlashMenu(val);
    } else {
        hideSlashMenu();
    }
});

chatInput.addEventListener('keydown', function(e) {
    if (e.keyCode === 229 || e.isComposing || isComposing) return;

    if (agentMentionRange) {
        if (e.key === 'Escape') {
            e.preventDefault();
            closeChatAgentMenu();
            return;
        }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            agentMentionActiveIndex += e.key === 'ArrowDown' ? 1 : -1;
            highlightMentionedAgent(true);
            return;
        }
        if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') {
            e.preventDefault();
            highlightMentionedAgent()?.click();
            return;
        }
    }

    if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && isSkillPickerAlias()) {
        e.preventDefault();
        sendMessage();
        return;
    }
    if (isSlashMenuVisible()) {
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            slashNavByKeyboard = true;
            slashActiveIdx = Math.min(slashActiveIdx + 1, slashFiltered.length - 1);
            renderSlashItems();
            return;
        }
        if (e.key === 'ArrowUp') {
            e.preventDefault();
            slashNavByKeyboard = true;
            slashActiveIdx = Math.max(slashActiveIdx - 1, 0);
            renderSlashItems();
            return;
        }
        if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) {
            e.preventDefault();
            selectSlashCommand(slashActiveIdx);
            return;
        }
        if (e.key === 'Escape') {
            e.preventDefault();
            hideSlashMenu();
            return;
        }
        if (e.key === 'Tab') {
            e.preventDefault();
            selectSlashCommand(slashActiveIdx);
            return;
        }
    }

    // Arrow-key history recall (only when input is empty or already browsing history)
    if (e.key === 'ArrowUp' && inputHistory.length > 0 && !isSlashMenuVisible()) {
        const curVal = this.value.trim();
        const isSingleLine = !this.value.includes('\n');
        if (isSingleLine && (curVal === '' || historyIdx >= 0)) {
            e.preventDefault();
            if (historyIdx < 0) {
                historySavedDraft = this.value;
                historyIdx = inputHistory.length - 1;
            } else if (historyIdx > 0) {
                historyIdx--;
            }
            this.value = inputHistory[historyIdx];
            slashJustSelected = true;
            this.dispatchEvent(new Event('input'));
            hideSlashMenu();
            this.selectionStart = this.selectionEnd = this.value.length;
            return;
        }
    }
    if (e.key === 'ArrowDown' && historyIdx >= 0 && !isSlashMenuVisible()) {
        const isSingleLine = !this.value.includes('\n');
        if (isSingleLine) {
            e.preventDefault();
            if (historyIdx < inputHistory.length - 1) {
                historyIdx++;
                this.value = inputHistory[historyIdx];
            } else {
                historyIdx = -1;
                this.value = historySavedDraft;
                historySavedDraft = '';
            }
            slashJustSelected = true;
            this.dispatchEvent(new Event('input'));
            hideSlashMenu();
            this.selectionStart = this.selectionEnd = this.value.length;
            return;
        }
    }

    if ((e.ctrlKey || e.shiftKey) && e.key === 'Enter') {
        const start = this.selectionStart;
        const end = this.selectionEnd;
        this.value = this.value.substring(0, start) + '\n' + this.value.substring(end);
        this.selectionStart = this.selectionEnd = start + 1;
        this.dispatchEvent(new Event('input'));
        e.preventDefault();
    } else if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey) {
        sendMessage();
        e.preventDefault();
    }
});

chatInput.addEventListener('blur', () => {
    setTimeout(hideSlashMenu, 150);
});

document.querySelectorAll('.example-card').forEach(card => {
    card.addEventListener('click', () => {
        // data-send overrides the visible text (e.g. show "查看全部命令" but send "/help")
        const sendText = card.dataset.send;
        if (sendText) {
            chatInput.value = sendText;
            chatInput.dispatchEvent(new Event('input'));
            chatInput.focus();
            return;
        }
        const textEl = card.querySelector('[data-i18n*="text"]');
        if (textEl) {
            chatInput.value = textEl.textContent;
            chatInput.dispatchEvent(new Event('input'));
            chatInput.focus();
        }
    });
});

// Voice-message variant of sendMessage(): renders a playable audio bubble
// and submits the recognised text through the same Session prompt endpoint.
async function sendVoiceMessage(text, audioUrl) {
    const ownerSession = sessionId;
    if (hasPendingApproval(ownerSession)) return;
    text = (text || '').trim();
    if (!text || !activeAgent || activeAgent.available === false) return;
    if (hasPendingApproval(ownerSession)) return;

    inputHistory.push(text);
    historyIdx = -1;
    historySavedDraft = '';

    const conversation = prepareConversationSend();
    if (conversation.draft) {
        const created = await fetch(`${PUBLIC_API_BASE}/sessions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                id: conversation.sessionId,
                agent_id: conversation.agentId,
            }),
        }).then(responseJson);
        if (created.status !== 'success') {
            restoreRejectedSendInput(text, pendingAttachments);
            addBotMessage(t('error_send'), new Date());
            return;
        }
    }
    const ws = document.getElementById('welcome-screen');
    const isFirstMessage = !!ws;
    if (ws) ws.remove();

    const titleInfo = isFirstMessage
        ? { sid: conversation.sessionId, userMsg: text }
        : null;
    const timestamp = new Date();
    addUserVoiceMessage(audioUrl, text, timestamp);
    const loadingEl = addLoadingIndicator();

    const body = {
        message_id: generateSessionMessageId(),
        content: text,
        attachments: [],
    };

    const MAX_RETRIES = 2;
    const RETRY_DELAY_MS = 1000;
    function postWithRetry(attempt) {
        if (hasPendingApproval(conversation.sessionId)) {
            loadingEl.remove();
            return;
        }
        fetch(`${PUBLIC_API_BASE}/sessions/${encodeURIComponent(conversation.sessionId)}/prompt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
        .then(async response => ({
            httpStatus: response.status,
            data: await responseJson(response),
        }))
        .then(async ({ httpStatus, data }) => {
            if (data.status === 'success') {
                if (conversation.draft) {
                    promoteNewChatDraft(conversation.sessionId);
                }
                const requestId = data.data.run_id;
                if (requestId) {
                    setActiveRunControls(requestId);
                    trackAcceptedRun(conversation.sessionId, requestId, loadingEl, titleInfo);
                }
            } else {
                loadingEl.remove();
                restoreRejectedSendInput(text, []);
                resetActiveRunControls();
            }
        })
        .catch(err => {
            if (attempt < MAX_RETRIES) {
                setTimeout(() => postWithRetry(attempt + 1), RETRY_DELAY_MS * (attempt + 1));
                return;
            }
            loadingEl.remove();
            restoreRejectedSendInput(text, []);
            resetActiveRunControls();
        });
    }
    postWithRetry(0);
}

function addUserVoiceMessage(audioUrl, caption, timestamp) {
    const el = document.createElement('div');
    el.className = 'flex justify-end px-4 sm:px-6 py-3';
    // Voice-message bubble: compact voice pill on top, ASR caption beneath.
    // The bubble keeps the same primary tint as a normal user message so
    // it visually slots into the conversation flow.
    el.innerHTML = `
        <div class="max-w-[75%] sm:max-w-[60%]">
            <div class="bg-slate-100 dark:bg-white/10 text-slate-700 dark:text-slate-200 rounded-2xl px-3 py-2 msg-content user-bubble">
                <div class="user-voice-slot"></div>
                ${caption ? `<div class="text-xs mt-1.5 leading-snug text-slate-500 dark:text-slate-400 whitespace-pre-wrap break-words">${escapeHtml(caption)}</div>` : ''}
            </div>
            <div class="text-xs text-slate-400 dark:text-slate-500 mt-1.5 text-right">${formatTime(timestamp)}</div>
        </div>
    `;
    el.querySelector('.user-voice-slot').appendChild(renderVoicePill(audioUrl));
    messagesDiv.appendChild(el);
    _autoScrollEnabled = true;
    scrollChatToBottom(true);
}

// Clipboard helper with fallback for non-HTTPS environments
function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        return navigator.clipboard.writeText(text);
    }
    // Fallback for HTTP environments
    return new Promise((resolve, reject) => {
        const textArea = document.createElement('textarea');
        textArea.value = text;
        textArea.style.position = 'fixed';
        textArea.style.left = '-999999px';
        textArea.style.top = '-999999px';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        try {
            document.execCommand('copy') ? resolve() : reject(new Error('Copy failed'));
        } catch (err) {
            reject(err);
        } finally {
            textArea.remove();
        }
    });
}

let composerSubmissionPromise = null;
let composerSubmissionQueued = false;

function clearComposerText() {
    chatInput.value = '';
    chatInput.style.height = '42px';
    chatInput.style.overflowY = 'hidden';
    chatInput.dispatchEvent(new Event('input'));
}

function restoreComposerText(text, optimisticEl) {
    optimisticEl?.remove();
    chatInput.value = text;
    chatInput.dispatchEvent(new Event('input'));
    chatInput.focus();
}

function generateSessionMessageId() {
    if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
        return globalThis.crypto.randomUUID();
    }
    return `message-${Date.now()}-${Math.random().toString(36).slice(2, 14)}`;
}

async function postComposerControl(path, payload, retries = 0, ownerSession = sessionId) {
    let lastError = null;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
        if (hasPendingApproval(ownerSession)) {
            return { approvalBlocked: true };
        }
        try {
            const response = await fetch(path, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            return {
                httpStatus: response.status,
                data: await responseJson(response),
            };
        } catch (error) {
            lastError = error;
        }
    }
    throw lastError || new Error('Run control request failed');
}

async function submitActiveRunText(runId, text) {
    const ownerSession = sessionId;
    const skillIds = selectedSkillIds();
    const attachments = pendingAttachments.map(item => ({
        file_path: item.file_path,
        file_name: item.file_name,
        file_type: item.file_type,
        ...(item.file_count == null ? {} : {file_count: item.file_count}),
    }));
    if (!text && attachments.length === 0) return;
    const content = text || (
        currentLang === 'zh'
            ? '请处理已附加的文件。'
            : 'Please process the attached files.'
    );
    if (hasPendingApproval(ownerSession)) return;
    inputHistory.push(text);
    historyIdx = -1;
    historySavedDraft = '';
    const messageId = generateSessionMessageId();
    clearComposerText();
    pendingAttachments = [];
    renderAttachmentPreview();
    const path = `${PUBLIC_API_BASE}/sessions/${encodeURIComponent(ownerSession)}/prompt`;
    const payload = {
        message_id: messageId,
        content,
        attachments,
        skill_ids: skillIds,
    };
    try {
        const result = await postComposerControl(
            path,
            payload,
            1,
            ownerSession,
        );
        if (result.approvalBlocked) {
            restoreRejectedSendInput(text, attachments);
            return;
        }
        const {data} = result;
        if (data.status !== 'success') {
            restoreRejectedSendInput(text, attachments);
            addBotMessage(t('error_send'), new Date());
            return;
        }
        if (ownerSession === sessionId) consumeSelectedSkills(skillIds);
        const alreadyRendered = Array.from(
            messagesDiv.querySelectorAll('[data-message-id]')
        ).some(element => element.dataset.messageId === messageId);
        if (!alreadyRendered) {
            appendPendingInputMessage(messageId, content, attachments);
        }
    } catch (error) {
        console.warn('[session.prompt]', error);
        const rendered = Array.from(
            messagesDiv.querySelectorAll('[data-message-id]')
        ).some(element => element.dataset.messageId === messageId);
        if (rendered) {
            if (ownerSession === sessionId) consumeSelectedSkills(skillIds);
            return;
        }
        restoreRejectedSendInput(text, attachments);
        addBotMessage(t('error_send'), new Date());
    }
}

async function dispatchComposerSubmission() {
    if (!activeAgent || activeAgent.available === false || hasPendingApproval()) return;
    const text = chatInput.value.trim();
    if (!text && pendingAttachments.length === 0) return;
    if (activeRequestId) {
        return submitActiveRunText(activeRequestId, text);
    }
    return sendNewRun();
}

// 手打 `/skills` 是打开临时技能选择器的入口（输入区没有别的按钮），所以它优先于
// 建议列表：菜单里恰好命中 "skills" 的技能（本机是 `superpowers:writing-skills`）
// 过去会把 Enter 吃掉，别名等于形同虚设。方向键加 Tab 一样能选中那一行。
const SKILL_PICKER_ALIASES = ['/skills', '/skill'];

function isSkillPickerAlias() {
    return SKILL_PICKER_ALIASES.includes(chatInput.value.trim().toLowerCase());
}

function sendMessage() {
    if (runtimeMaintenanceBlocked) return;
    // Local commands must not depend on the suggestion popup still being
    // visible (it can close on blur, Escape, or an asynchronous UI refresh).
    if (isSkillPickerAlias()) {
        return openSkillPicker();
    }
    if (composerSubmissionPromise) {
        composerSubmissionQueued = true;
        return composerSubmissionPromise;
    }
    composerSubmissionPromise = Promise.resolve(dispatchComposerSubmission())
        .finally(() => {
            composerSubmissionPromise = null;
            updateSendBtnState();
            if (composerSubmissionQueued) {
                composerSubmissionQueued = false;
                void sendMessage();
            }
        });
    return composerSubmissionPromise;
}

async function sendNewRun() {
    const ownerSession = sessionId;
    const skillIds = selectedSkillIds();
    const text = chatInput.value.trim();
    if (hasPendingApproval(ownerSession)) return;
    if (!text && pendingAttachments.length === 0) return;

    if (text) {
        inputHistory.push(text);
        historyIdx = -1;
        historySavedDraft = '';
    }

    const conversation = prepareConversationSend();
    if (conversation.draft) {
        try {
            const created = await fetch(`${PUBLIC_API_BASE}/sessions`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    id: conversation.sessionId,
                    agent_id: conversation.agentId,
                }),
            }).then(responseJson);
            if (created.status !== 'success') {
                addBotMessage(t('error_send'), new Date());
                return;
            }
        } catch (error) {
            console.warn('[session.create]', error);
            addBotMessage(t('error_send'), new Date());
            return;
        }
    }
    const foreground = conversation.sessionId === sessionId;
    const ws = foreground ? document.getElementById('welcome-screen') : null;
    const isFirstMessage = !!ws;
    if (ws) ws.remove();

    const titleInfo = (isFirstMessage && text)
        ? { sid: conversation.sessionId, userMsg: text }
        : null;

    const timestamp = new Date();
    const attachments = [...pendingAttachments];
    const messageId = generateSessionMessageId();
    const sessionModel = sessionRuntime.get(conversation.sessionId);
    sessionModel.stagePendingInput({
        messageId,
        content: text,
        createdAt: timestamp.getTime() / 1000,
        attachments,
    });
    if (foreground) {
        const optimistic = addUserMessage(text, timestamp, attachments);
        optimistic.dataset.messageId = messageId;
    }

    const loadingEl = foreground
        ? addLoadingIndicator()
        : document.createElement('div');

    function preserveAuthoritativeInput() {
        if (sessionModel.discardPendingInput(messageId)) return false;
        if (conversation.draft) promoteNewChatDraft(conversation.sessionId);
        if (foreground) renderLiveSessionModelSnapshot(conversation.sessionId);
        if (conversation.sessionId === sessionId) consumeSelectedSkills(skillIds);
        return true;
    }

    clearComposerText();
    pendingAttachments = [];
    renderAttachmentPreview();
    sendBtn.disabled = true;

    const runPrompt = text || (
        currentLang === 'zh'
            ? '请处理已附加的文件。'
            : 'Please process the attached files.'
    );
    const body = {
        message_id: messageId,
        content: runPrompt,
        skill_ids: skillIds,
    };
    if (attachments.length > 0) {
        body.attachments = attachments.map(a => ({
            file_path: a.file_path,
            file_name: a.file_name,
            file_type: a.file_type,
            file_count: a.file_count,
        }));
    }

    const MAX_RETRIES = 2;
    const RETRY_DELAY_MS = 1000;

    function postWithRetry(attempt) {
        if (hasPendingApproval(conversation.sessionId)) {
            if (preserveAuthoritativeInput()) return;
            loadingEl.remove();
            restoreRejectedSendInput(text, attachments);
            return;
        }
        fetch(`${PUBLIC_API_BASE}/sessions/${encodeURIComponent(conversation.sessionId)}/prompt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
        .then(async response => ({
            httpStatus: response.status,
            data: await responseJson(response),
        }))
        .then(async ({ httpStatus, data }) => {
            if (data.status === 'success') {
                if (conversation.draft) {
                    promoteNewChatDraft(conversation.sessionId);
                }
                if (conversation.sessionId === sessionId) consumeSelectedSkills(skillIds);
                const requestId = data.data.run_id;
                if (requestId) {
                    if (foreground) setActiveRunControls(requestId);
                    trackAcceptedRun(
                        conversation.sessionId, requestId, loadingEl,
                    );
                    if (titleInfo) {
                        void generateSessionTitle(titleInfo.sid, titleInfo.userMsg);
                    }
                } else {
                    loadingEl.remove();
                    if (foreground) void reloadAuthoritativeHistory();
                }
            } else {
                if (preserveAuthoritativeInput()) return;
                restoreRejectedSendInput(text, attachments);
                resetActiveRunControls();
                loadingEl.remove();
            }
        })
        .catch(err => {
            if (err.name === 'AbortError') {
                if (preserveAuthoritativeInput()) return;
                loadingEl.remove();
                restoreRejectedSendInput(text, attachments);
                resetActiveRunControls();
                return;
            }
            if (attempt < MAX_RETRIES) {
                console.warn(`[sendMessage] attempt ${attempt + 1} failed, retrying...`, err);
                setTimeout(() => postWithRetry(attempt + 1), RETRY_DELAY_MS * (attempt + 1));
                return;
            }
            if (preserveAuthoritativeInput()) return;
            loadingEl.remove();
            restoreRejectedSendInput(text, attachments);
            resetActiveRunControls();
        });
    }

    postWithRetry(0);
}

function createUserMessageEl(content, timestamp, attachments) {
    const el = document.createElement('div');
    el.className = 'flex justify-end px-4 sm:px-6 py-3 user-message-group';
    const column = document.createElement('div');
    column.className = 'max-w-[75%] sm:max-w-[60%]';
    const bubble = document.createElement('div');
    bubble.className = 'bg-primary-400 text-white rounded-2xl px-4 py-2.5 text-sm leading-relaxed msg-content user-bubble';
    if (attachments && attachments.length > 0) {
        const attachmentContainer = document.createElement('div');
        attachmentContainer.className = 'user-msg-attachments';
        attachments.forEach(a => {
            if (a.file_type === 'image') {
                const safeUrl = safeMediaUrl(a.preview_url);
                if (safeUrl) {
                    const image = document.createElement('img');
                    image.src = safeUrl;
                    image.alt = String(a.file_name || '');
                    image.className = 'user-msg-image';
                    attachmentContainer.appendChild(image);
                }
                return;
            }
            const file = document.createElement('div');
            file.className = 'user-msg-file';
            const icon = document.createElement('i');
            icon.className = `fas ${a.file_type === 'video'
                ? 'fa-film'
                : (a.file_type === 'directory' ? 'fa-folder-tree' : 'fa-file-alt')}`;
            const suffix = a.file_type === 'directory' && a.file_count
                ? ` (${a.file_count})`
                : '';
            file.append(icon, document.createTextNode(` ${String(a.file_name || '')}${suffix}`));
            attachmentContainer.appendChild(file);
        });
        bubble.appendChild(attachmentContainer);
    }
    if (content) {
        const text = document.createElement('div');
        text.innerHTML = renderMarkdown(content);
        while (text.firstChild) bubble.appendChild(text.firstChild);
    }
    const controls = document.createElement('div');
    controls.className = 'flex items-center justify-end gap-2 mt-1.5';
    const time = document.createElement('span');
    time.className = 'text-xs text-slate-400 dark:text-slate-500';
    time.textContent = formatTime(timestamp);
    controls.append(time);
    column.append(bubble, controls);
    el.appendChild(column);
    // Store raw content for editing
    el.dataset.rawContent = content || '';
    return el;
}

function rejectedToolMessage(item) {
    const base = currentLang === 'zh'
        ? '此工具未授权给当前 Agent。'
        : 'This tool is not available to the current Agent.';
    const allowed = Array.isArray(item.allowed_tools)
        ? item.allowed_tools.filter(Boolean).join(', ')
        : '';
    if (!allowed) return base;
    const prefix = currentLang === 'zh' ? '可用工具：' : 'Available tools: ';
    return `${base} ${prefix}${allowed}`;
}

function renderToolCallsHtml(toolCalls) {
    if (!toolCalls || toolCalls.length === 0) return '';
    return toolCalls.map(tc => {
        const argsStr = formatToolArgs(tc.arguments || {});
        const isRejected = tc.status === 'rejected';
        const displayResult = isRejected
            ? rejectedToolMessage(tc)
            : (tc.result || '');
        const resultStr = displayResult ? escapeHtml(String(displayResult)) : '';
        const hasResult = !!resultStr;
        const isErr = isRejected || (tc.status && tc.status !== 'success');
        const iconClass = isRejected
            ? 'fas fa-ban text-red-400 flex-shrink-0 tool-icon'
            : (isErr
                ? 'fas fa-times text-red-400 flex-shrink-0 tool-icon'
                : 'fas fa-check text-primary-400 flex-shrink-0 tool-icon');
        return `
<div class="agent-step agent-tool-step${isErr ? ' tool-failed' : ''}${isRejected ? ' tool-rejected' : ''}" data-tool-call-id="${escapeHtml(tc.tool_call_id || '')}">
    <div class="tool-header" onclick="this.parentElement.classList.toggle('expanded')">
        <i class="${iconClass}"></i>
        <span class="tool-name">${escapeHtml(tc.name || '')}</span>
        <i class="fas fa-chevron-right tool-chevron"></i>
    </div>
    <div class="tool-detail">
        <div class="tool-detail-section">
            <div class="tool-detail-label">Input</div>
            <pre class="tool-detail-content">${argsStr}</pre>
        </div>
        ${hasResult ? `
        <div class="tool-detail-section tool-output-section">
            <div class="tool-detail-label">${isRejected ? (currentLang === 'zh' ? '已拒绝' : 'Rejected') : (isErr ? 'Error' : 'Output')}</div>
            <pre class="tool-detail-content${isErr ? ' tool-error-text' : ''}">${resultStr}</pre>
        </div>` : ''}
    </div>
</div>`;
    }).join('');
}

// Long reasoning remains fully available.  We only skip repeated Markdown
// parsing past this threshold so a fast stream does not stall the browser.
const REASONING_RENDER_CAP = 4 * 1024; // 4 KB

function _renderReasoningBody(text) {
    const shown = String(text || '');
    if (shown.length > REASONING_RENDER_CAP) {
        return '<pre class="thinking-stream-pre">' + escapeHtml(shown) + '</pre>';
    }
    return renderMarkdown(shown);
}

function finalizeThinking(el, startTime, text) {
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
    el.querySelector('.thinking-summary').textContent = t('thinking_done');
    const fullDiv = el.querySelector('.thinking-full');
    fullDiv.innerHTML = `<div class="thinking-duration">${t('thinking_duration')} ${elapsed}s</div>` + _renderReasoningBody(text);
    el.classList.remove('expanded');
}

function toggleAgentStep(element) {
    element.classList.toggle('expanded');
    element.dataset.userToggled = 'true';
}

function renderThinkingHtml(text, streaming = false) {
    if (!text || !text.trim()) return '';
    const full = text.trim();
    return `
<div class="agent-step agent-thinking-step${streaming ? ' expanded' : ''}">
    <div class="thinking-header" onclick="toggleAgentStep(this.parentElement)">
        <i class="fas fa-lightbulb text-amber-400 flex-shrink-0"></i>
        <span class="thinking-summary">${t(streaming ? 'thinking_in_progress' : 'thinking_done')}</span>
        <i class="fas fa-chevron-right thinking-chevron"></i>
    </div>
    <div class="thinking-full">${_renderReasoningBody(full)}</div>
</div>`;
}

function renderStepsHtml(steps, streaming = false, activeThinkingIndex = -1) {
    if (!steps || steps.length === 0) return { stepsHtml: '', finalContent: '' };

    // Only a trailing content block belongs in the main answer area. Hoisting an
    // earlier content block across a later thinking/tool block would reorder the
    // durable Session event stream.
    const lastContentIdx = steps[steps.length - 1]?.type === 'content'
        ? steps.length - 1 : -1;

    let html = '';
    let lastContentText = '';
    for (let i = 0; i < steps.length; i++) {
        const step = steps[i];
        if (step.type === 'thinking') {
            html += renderThinkingHtml(
                step.content,
                streaming && i === activeThinkingIndex,
            );
        } else if (step.type === 'content') {
            if (i === lastContentIdx) {
                lastContentText = step.content;
            } else {
                html += `<div class="agent-step agent-content-step"><div class="agent-content-body">${renderAssistantMarkdown(step.content)}</div></div>`;
            }
        } else if (step.type === 'tool') {
            const argsStr = formatToolArgs(step.arguments || {});
            const isRejected = step.status === 'rejected';
            const displayResult = isRejected
                ? rejectedToolMessage(step)
                : (step.result || step.progress || '');
            const resultStr = displayResult ? escapeHtml(String(displayResult)) : '';
            const isStreaming = step.status === 'running';
            const isErr = isRejected || step.is_error === true
                || (!!step.status && !['running', 'success'].includes(step.status));
            if (isTodoTool(step.name) && !isErr) {
                html += `
<div class="agent-step agent-tool-step agent-todo-step" data-tool-call-id="${escapeHtml(step.tool_call_id || '')}">
    ${renderTodoToolHtml(step.arguments && step.arguments.todos)}
</div>`;
                continue;
            }
            const iconClass = isRejected
                ? 'fas fa-ban text-red-400 flex-shrink-0 tool-icon'
                : (isErr
                    ? 'fas fa-times text-red-400 flex-shrink-0 tool-icon'
                    : (isStreaming
                        ? 'fas fa-spinner fa-spin text-primary-400 flex-shrink-0 tool-icon'
                        : 'fas fa-check text-primary-400 flex-shrink-0 tool-icon'));
            html += `
<div class="agent-step agent-tool-step${isErr ? ' tool-failed' : ''}${isRejected ? ' tool-rejected' : ''}${isStreaming ? ' tool-streaming' : ''}" data-tool-call-id="${escapeHtml(step.tool_call_id || '')}">
    <div class="tool-header" onclick="toggleAgentStep(this.parentElement)">
        <i class="${iconClass}"></i>
        <span class="tool-name">${escapeHtml(step.name || '')}</span>
        <i class="fas fa-chevron-right tool-chevron"></i>
    </div>
    <div class="tool-detail">
        <div class="tool-detail-section">
            <div class="tool-detail-label">Input</div>
            <pre class="tool-detail-content">${argsStr}</pre>
        </div>
        ${resultStr ? `
        <div class="tool-detail-section tool-output-section">
            <div class="tool-detail-label">${isRejected ? (currentLang === 'zh' ? '已拒绝' : 'Rejected') : (isErr ? 'Error' : 'Output')}</div>
            <pre class="tool-detail-content${isErr ? ' tool-error-text' : ''}">${resultStr}</pre>
        </div>` : ''}
    </div>
</div>`;
        }
    }
    return { stepsHtml: html, lastContentText };
}

function appendStoredToolMedia(messageElement, message) {
    const candidates = [
        ...(Array.isArray(message?.steps)
            ? message.steps.filter(step => step.type === 'tool')
            : []),
        ...(Array.isArray(message?.tool_calls) ? message.tool_calls : []),
    ];
    const host = messageElement.querySelector('.msg-content');
    if (!host) return;
    const media = document.createElement('div');
    media.className = 'stored-tool-media';
    candidates.forEach(step => {
        if (!step?.result) return;
        let payload;
        try {
            payload = typeof step.result === 'string'
                ? JSON.parse(step.result)
                : step.result;
        } catch (_) {
            return;
        }
        if (payload?.type !== 'file_to_send' || !payload.path) return;
        const preview = payload.file_type === 'image'
            ? createImagePreview(payload.path)
            : (payload.file_type === 'video' ? createVideoPreview(payload.path) : null);
        if (!preview) return;
        const wrapper = document.createElement('div');
        wrapper.className = 'agent-step';
        wrapper.appendChild(preview);
        media.appendChild(wrapper);
    });
    if (media.childElementCount) host.appendChild(media);
}

function appendStoredArtifacts(messageElement, message) {
    const artifacts = Array.isArray(message?.artifacts) ? message.artifacts : [];
    if (!artifacts.length) return;
    const host = messageElement.querySelector('.msg-content');
    if (!host) return;
    const media = document.createElement('div');
    media.className = 'media-content stored-artifacts';
    artifacts.forEach(item => {
        const path = String(item.url || item.path || '');
        const mediaType = String(item.media_type || '');
        const preview = mediaType.startsWith('image/')
            ? createImagePreview(path, String(item.name || 'artifact'))
            : mediaType.startsWith('video/') ? createVideoPreview(path) : null;
        if (preview) {
            media.appendChild(preview);
            return;
        }
        const safeUrl = path ? safeMediaUrl(_toWebUrl(path)) : null;
        const artifact = document.createElement(safeUrl ? 'a' : 'div');
        artifact.className = 'agent-file-artifact';
        if (safeUrl) {
            artifact.href = safeUrl;
            artifact.target = '_blank';
            artifact.rel = 'noopener noreferrer';
        }
        const icon = document.createElement('i');
        icon.className = 'fas fa-file-arrow-down';
        const label = document.createElement('span');
        label.textContent = String(item.name || path.split('/').pop() || 'artifact');
        artifact.append(icon, label);
        media.appendChild(artifact);
    });
    if (media.childElementCount) host.appendChild(media);
}

// Cosmetic translator for cancel markers persisted in history.
// History keeps the English canonical form for the LLM; only display is localized.
function localizeCancelMarker(text) {
    if (!text) return text;
    if (currentLang !== 'zh') return text;
    return text
        .replace(/_\(Cancelled by user\)_/g, '_(用户已中止)_')
        .replace(/_\(Cancelled\)_/g, '_(已中止)_');
}

function createBotMessageEl(content, timestamp, requestId, msg) {
    const el = document.createElement('div');
    el.className = 'flex gap-3 px-4 sm:px-6 py-3 bot-message-group';
    if (requestId) el.dataset.requestId = requestId;

    let stepsHtml = '';
    let displayContent = localizeCancelMarker(
        assistantDisplayText(content)
    );

    if (msg && msg.steps && msg.steps.length > 0) {
        // New format: ordered steps with interleaved content
        const result = renderStepsHtml(
            msg.steps,
            msg.streaming === true,
            Number.isInteger(msg.thinking_active_index)
                ? msg.thinking_active_index : -1,
        );
        stepsHtml = result.stepsHtml;
        // The final content (last text after all steps) is the main answer
        displayContent = assistantDisplayText(result.lastContentText);
    } else {
        // Legacy format: separate tool_calls + optional reasoning
        const toolCalls = msg && msg.tool_calls;
        const reasoning = msg && msg.reasoning;
        stepsHtml = renderThinkingHtml(reasoning) + renderToolCallsHtml(toolCalls);
    }

    el.innerHTML = `
        <img src="assets/logo.jpg" alt="Bianbu Agent" class="w-8 h-8 rounded-lg flex-shrink-0">
        <div class="min-w-0 flex-1 max-w-[85%]">
            <div class="bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-2xl px-4 py-3 text-sm leading-relaxed msg-content text-slate-700 dark:text-slate-200">
                ${stepsHtml ? `<div class="agent-steps">${stepsHtml}</div>` : ''}
                <div class="answer-content">${renderMarkdown(displayContent)}</div>
                <div class="bot-audio-slot"></div>
            </div>
            <div class="bot-message-meta flex items-center gap-2 mt-1.5">
                <span class="text-xs text-slate-400 dark:text-slate-500">${formatTime(timestamp)}</span>
                <button class="copy-msg-btn text-xs text-slate-300 dark:text-slate-600 hover:text-slate-500 dark:hover:text-slate-400 transition-colors cursor-pointer" title="${currentLang === 'zh' ? '复制' : 'Copy'}">
                    <i class="fas fa-copy"></i>
                </button>
            </div>
        </div>
    `;
    if (isSecurityErrorPayload(msg?.error)) {
        const error = buildSecurityErrorEl(msg.error);
        error.classList.remove('bot-message-group');
        error.classList.add('agent-terminal-error');
        el.querySelector('.msg-content')?.appendChild(error);
    }
    attachTokenUsage(el, msg && msg.usage);
    if (msg?.status === 'cancelled') {
        const tag = document.createElement('div');
        tag.className = 'agent-cancelled-tag text-xs text-amber-600 dark:text-amber-400 mt-1';
        tag.textContent = currentLang === 'zh' ? '已中止' : 'Cancelled';
        el.querySelector('.msg-content')?.appendChild(tag);
    }
    el.querySelector('.answer-content').dataset.rawMd = displayContent;
    appendStoredToolMedia(el, msg);
    appendStoredArtifacts(el, msg);
    // Existing TTS attachment (history replay): mount the player up-front.
    const existingAudio = msg && msg.extras && msg.extras.audio && msg.extras.audio.url;
    if (existingAudio) {
        attachAudioToBotBubble(el, existingAudio, { autoplay: false });
    }
    applyHighlighting(el);
    return el;
}

// Append (or replace) a small audio player inside a bot bubble's
// dedicated `.bot-audio-slot`. Used by both live TTS pushes and history
// replay. Silent failures: never throws.
function attachAudioToBotBubble(botEl, audioUrl, opts) {
    try {
        if (!botEl || !audioUrl) return;
        const slot = botEl.querySelector('.bot-audio-slot');
        if (!slot) return;
        slot.dataset.audioUrl = String(audioUrl);
        slot.innerHTML = '';
        slot.style.marginTop = '6px';
        const pill = renderVoicePill(audioUrl, { autoplay: !!(opts && opts.autoplay) });
        slot.appendChild(pill);
    } catch (_) { /* silent */ }
}

// Build a compact play/pause + progress + duration pill that wraps a
// hidden <audio>. Returns the root element; safe to embed anywhere.
function renderVoicePill(audioUrl, opts) {
    opts = opts || {};
    const wrap = document.createElement('div');
    wrap.className = 'voice-pill';
    const safeUrl = safeMediaUrl(_toWebUrl(audioUrl));
    if (!safeUrl) {
        wrap.hidden = true;
        return wrap;
    }
    wrap.innerHTML = `
        <button type="button" class="voice-pill-btn" data-state="play" aria-label="play">
            <i class="fas fa-play"></i>
        </button>
        <div class="voice-pill-track"><div class="voice-pill-fill"></div></div>
        <span class="voice-pill-time">0:00</span>
    `;
    const audio = document.createElement('audio');
    audio.preload = 'metadata';
    audio.src = safeUrl;
    wrap.appendChild(audio);
    const btn = wrap.querySelector('.voice-pill-btn');
    const fill = wrap.querySelector('.voice-pill-fill');
    const timeEl = wrap.querySelector('.voice-pill-time');

    const fmt = (s) => {
        if (!isFinite(s) || s < 0) s = 0;
        const m = Math.floor(s / 60);
        const r = Math.floor(s % 60);
        return `${m}:${r < 10 ? '0' : ''}${r}`;
    };
    const setIcon = (state) => {
        btn.dataset.state = state;
        btn.querySelector('i').className = state === 'pause' ? 'fas fa-pause' : 'fas fa-play';
        btn.setAttribute('aria-label', state === 'pause' ? 'pause' : 'play');
    };

    audio.addEventListener('loadedmetadata', () => {
        if (audio.duration && isFinite(audio.duration)) timeEl.textContent = fmt(audio.duration);
    });
    audio.addEventListener('timeupdate', () => {
        const dur = audio.duration || 0;
        if (dur > 0) {
            fill.style.width = `${Math.min(100, (audio.currentTime / dur) * 100)}%`;
            timeEl.textContent = fmt(dur - audio.currentTime);
        }
    });
    audio.addEventListener('ended', () => {
        setIcon('play');
        fill.style.width = '0%';
        timeEl.textContent = fmt(audio.duration || 0);
    });
    audio.addEventListener('play',  () => setIcon('pause'));
    audio.addEventListener('pause', () => setIcon('play'));

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (audio.paused) {
            audio.play().catch(() => {});
        } else {
            audio.pause();
        }
    });

    if (opts.autoplay) {
        // Autoplay may be blocked by the browser; fall back silently and
        // let the user tap the play button.
        const tryPlay = () => audio.play().catch(() => {});
        if (audio.readyState >= 2) tryPlay();
        else audio.addEventListener('canplay', tryPlay, { once: true });
    }
    return wrap;
}

function addUserMessage(content, timestamp, attachments) {
    const el = createUserMessageEl(content, timestamp, attachments);
    messagesDiv.appendChild(el);
    _autoScrollEnabled = true;
    scrollChatToBottom(true);
    return el;
}

// ── 安全拦截的错误呈现 ─────────────────────────────────────────
// 后端把 SecurityDenied 投影成 code=security_denied / security_unavailable
// 的 error 事件，并在 details 里带 tool / policy / reason。
//
// 此前这里无条件显示 t('error_send')「发送失败，请稍后再试」—— 用户看到的是
// "系统故障，重试可能会好"，实际是"这个操作没有授权，重试一万次也一样"，
// 而且不知道缺哪条能力、该去哪里授权。
const SECURITY_ERROR_CODES = [
    'security_denied', 'security_unavailable',
    'network_policy_blocked', 'provider_connection_failed',
    'provider_timeout',
];

function isSecurityErrorPayload(item) {
    return !!item && SECURITY_ERROR_CODES.includes(String(item.code || ''));
}

// details 里的 tool / scope 可能源自模型生成的参数（属外部输入），
// 必须转义后再进 innerHTML —— 这里走 escapeHtml 而非 renderMarkdown。
function buildSecurityErrorEl(item) {
    const code = String(item.code);
    const degraded = code === 'security_unavailable';
    const policyBlocked = code === 'network_policy_blocked';
    const providerConnectionFailed = code === 'provider_connection_failed';
    // 超时和连不上都是「模型服务没能给出回答」，共用网络故障的卡片版式，
    // 但标题/建议不同：超时要考虑服务过载与模型本身太慢。
    const providerTimeout = code === 'provider_timeout';
    const networkError = policyBlocked || providerConnectionFailed || providerTimeout;
    const details = (item && typeof item.details === 'object' && item.details) || {};
    const dlp = details.policy_type === 'dlp';
    const title = t(dlp ? (degraded ? 'dlp_unavailable_title' : 'dlp_denied_title')
        : degraded ? 'security_unavailable_title'
        : policyBlocked ? 'network_policy_blocked_title'
        : providerConnectionFailed ? 'provider_connection_failed_title'
        : providerTimeout ? 'provider_timeout_title'
        : 'security_denied_title');
    const hint = t(dlp ? (degraded ? 'dlp_unavailable_hint' : 'dlp_denied_hint')
        : degraded ? 'security_unavailable_hint'
        : policyBlocked ? 'network_policy_blocked_hint'
        : providerConnectionFailed ? 'provider_connection_failed_hint'
        : providerTimeout ? 'provider_timeout_hint'
        : 'security_denied_hint');
    // 后端消息带 [bianbu-security] 前缀，标题已表达来源，去掉更好读
    const reason = String(details.reason || item.message || '')
        .replace(/^\[bianbu-security\]\s*/, '');

    const rows = (networkError ? [
        ['security_field_provider', details.provider_host],
        ['security_field_network_mode', details.network_mode],
        ['security_field_domains', Array.isArray(details.domains) ? details.domains.join(', ') : details.domains],
        ['security_field_timeout', providerTimeout ? details.timeout_seconds : undefined],
    ] : [
        ['security_field_tool', details.tool],
        ['security_field_policy', details.policy],
    ])
    .filter(([, v]) => v !== undefined && v !== null && String(v) !== '')
    .map(([k, v]) =>
        `<div class="flex gap-2">
             <span class="text-slate-400 dark:text-slate-500 shrink-0">${escapeHtml(t(k))}</span>
             <span class="font-mono break-all">${escapeHtml(String(v))}</span>
         </div>`).join('');
    const panelClass = degraded
        ? 'bg-amber-50 dark:bg-amber-500/10 border-amber-200 dark:border-amber-500/30'
        : providerConnectionFailed
            ? 'bg-sky-50 dark:bg-sky-500/10 border-sky-200 dark:border-sky-500/30'
            : providerTimeout
                ? 'bg-amber-50 dark:bg-amber-500/10 border-amber-200 dark:border-amber-500/30'
                : 'bg-rose-50 dark:bg-rose-500/10 border-rose-200 dark:border-rose-500/30';
    const titleClass = degraded
        ? 'text-amber-700 dark:text-amber-300'
        : providerConnectionFailed
            ? 'text-sky-700 dark:text-sky-300'
            : providerTimeout
                ? 'text-amber-700 dark:text-amber-300'
                : 'text-rose-700 dark:text-rose-300';
    const iconClass = degraded ? 'fa-triangle-exclamation'
        : providerConnectionFailed ? 'fa-plug-circle-xmark'
        : providerTimeout ? 'fa-clock' : 'fa-shield-halved';

    const el = document.createElement('div');
    el.className = 'flex gap-3 px-4 sm:px-6 py-3 bot-message-group security-denied-group';
    el.innerHTML = `
        <img src="assets/logo.jpg" alt="Bianbu Agent" class="w-8 h-8 rounded-lg flex-shrink-0">
        <div class="min-w-0 flex-1 max-w-[85%]">
            <div class="border rounded-2xl px-4 py-3 text-sm leading-relaxed
                        ${panelClass}">
                <div class="flex items-center gap-2 font-medium ${titleClass}">
                    <i class="fas ${iconClass}"></i>
                    <span>${escapeHtml(title)}</span>
                </div>
                ${reason ? `<div class="mt-2 text-slate-700 dark:text-slate-200">${escapeHtml(reason)}</div>` : ''}
                ${rows ? `<div class="mt-2 space-y-1 text-xs">${rows}</div>` : ''}
                <div class="mt-2 text-xs text-slate-500 dark:text-slate-400">${escapeHtml(hint)}</div>
            </div>
            <div class="flex items-center gap-2 mt-1.5">
                <span class="text-xs text-slate-400 dark:text-slate-500">${formatTime(new Date())}</span>
            </div>
        </div>
    `;
    return el;
}

// 安全拦截 → 展示真实原因；其余错误 → 保持原有通用文案
function addRunErrorMessage(item) {
    if (isSecurityErrorPayload(item)) {
        messagesDiv.appendChild(buildSecurityErrorEl(item));
        scrollChatToBottom();
        return;
    }
    addBotMessage(t('error_send'), new Date());
}

function createApprovalCard(runId, approval) {
    const group = document.createElement('div');
    group.className = 'flex gap-3 px-4 sm:px-6 py-3 approval-message-group';

    const avatar = document.createElement('img');
    avatar.src = 'assets/logo.jpg';
    avatar.alt = 'Bianbu Agent';
    avatar.className = 'w-8 h-8 rounded-lg flex-shrink-0';

    const content = document.createElement('div');
    content.className = 'min-w-0 flex-1 max-w-[85%] approval-message-content';

    const card = document.createElement('div');
    card.className = 'approval-card';
    card.dataset.requestId = String(runId || '');
    card.dataset.approvalId = String(approval.approval_id || '');

    const title = document.createElement('strong');
    title.className = 'approval-card-header';
    title.textContent = t('approval_title');
    const description = document.createElement('div');
    description.className = 'approval-card-description';
    description.textContent = t('approval_description');
    const actions = document.createElement('div');
    actions.className = 'approval-actions';
    (Array.isArray(approval.actions) ? approval.actions : []).forEach(action => {
        const row = document.createElement('pre');
        row.className = 'approval-action';
        row.textContent = `${String(action.name || '')} ${JSON.stringify(
            action.arguments || {}, null, 2,
        )}`;
        actions.appendChild(row);
    });

    const status = document.createElement('span');
    status.className = 'approval-status';
    const controls = document.createElement('div');
    controls.className = 'approval-card-footer';
    const approve = document.createElement('button');
    approve.className = 'approval-button approval-approve';
    approve.textContent = t('approval_approve');
    const reject = document.createElement('button');
    reject.className = 'approval-button approval-reject';
    reject.textContent = t('approval_reject');

    const decide = async decision => {
        approve.disabled = true;
        reject.disabled = true;
        status.textContent = t('approval_processing');
        try {
            const response = await fetch(
                `${PUBLIC_API_BASE}/sessions/${encodeURIComponent(sessionId)}`
                + `/approvals/${encodeURIComponent(card.dataset.approvalId)}`,
                {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({decision}),
                },
            );
            const payload = await responseJson(response);
            if (!response.ok || payload.status !== 'success') throw new Error('approval failed');
            status.textContent = t(
                decision === 'approve' ? 'approval_approved' : 'approval_rejected'
            );
        } catch (_) {
            status.textContent = t('approval_failed');
            approve.disabled = false;
            reject.disabled = false;
        }
    };
    approve.addEventListener('click', () => void decide('approve'));
    reject.addEventListener('click', () => void decide('reject'));
    controls.append(approve, reject, status);
    card.append(title, description, actions, controls);
    content.appendChild(card);
    group.append(avatar, content);
    return group;
}

function appendPendingInputMessage(
    messageId,
    content,
    attachments = [],
    timestamp = new Date(),
) {
    const selected = String(messageId || '');
    if (!selected || !content) return null;
    const existing = Array.from(
        messagesDiv.querySelectorAll('[data-message-id]')
    ).find(element => element.dataset.messageId === selected);
    if (existing) return existing;
    const element = addUserMessage(String(content), timestamp, attachments);
    element.dataset.messageId = selected;
    return element;
}

function addBotMessage(content, timestamp, requestId) {
    const el = createBotMessageEl(content, timestamp, requestId);
    messagesDiv.appendChild(el);
    scrollChatToBottom();
    return el;
}

function createContextCompressionDivider(payload) {
    const normalizeCount = (value, fallback = 0) => {
        const number = Number(value);
        return Number.isFinite(number) && number >= 0
            ? Math.floor(number)
            : fallback;
    };
    const beforeTokens = normalizeCount(payload.before_tokens);
    const afterTokens = normalizeCount(payload.after_tokens);
    const retainedMessages = normalizeCount(payload.retained_messages);
    const compressionCount = Math.max(
        1,
        normalizeCount(payload.compression_count, 1)
    );
    const requestIdentity = String(payload.request_id || '').trim();
    const compressionIdentity = String(payload.compression_id || '').trim();
    const threadIdentity = String(
        payload.new_thread_id || payload.runtime_thread_id || ''
    ).trim();
    const strongAliases = [
        compressionIdentity && `compression:${compressionIdentity}`,
        requestIdentity && `request:${requestIdentity}`,
        threadIdentity && `thread:${threadIdentity}:${compressionCount}`
    ].filter(Boolean);
    const weakIdentity =
        `tokens:${beforeTokens}:${afterTokens}:${compressionCount}`;
    const identity = strongAliases[0] || weakIdentity;

    const divider = document.createElement('div');
    divider.className = 'context-divider context-compression-divider';
    divider.dataset.compressionIdentity = identity;
    divider.dataset.compressionStrongAliases = JSON.stringify(strongAliases);
    divider.dataset.compressionWeakIdentity = weakIdentity;

    const summary = document.createElement('span');
    summary.textContent = t('context_compressed')
        .replace('{before}', beforeTokens.toLocaleString())
        .replace('{after}', afterTokens.toLocaleString());
    divider.appendChild(summary);

    const detail = document.createElement('small');
    detail.textContent = t('context_compression_detail')
        .replace('{messages}', retainedMessages)
        .replace('{count}', compressionCount);
    divider.appendChild(detail);

    if (compressionCount >= 2) {
        const accuracyWarning = document.createElement('span');
        accuracyWarning.className = 'context-compression-warning context-compression-accuracy-warning';
        accuracyWarning.textContent = t('context_compression_accuracy_warning');
        divider.appendChild(accuracyWarning);
    }
    if (payload.used_fallback) {
        const fallbackWarning = document.createElement('span');
        fallbackWarning.className = 'context-compression-warning context-compression-fallback-warning';
        fallbackWarning.textContent = t('context_compression_fallback');
        divider.appendChild(fallbackWarning);
    }
    return divider;
}

function appendContextCompressionDivider(container, payload) {
    if (payload.compressed === false) return null;
    const divider = createContextCompressionDivider(payload);
    const strongAliases = new Set(JSON.parse(
        divider.dataset.compressionStrongAliases || '[]'
    ));
    const weakIdentity = divider.dataset.compressionWeakIdentity || '';
    const isSameBoundary = element => {
        let existingStrong = [];
        try {
            existingStrong = JSON.parse(
                element.dataset.compressionStrongAliases || '[]'
            );
        } catch (_error) {
            existingStrong = [];
        }
        if (strongAliases.size > 0 || existingStrong.length > 0) {
            return strongAliases.size > 0
                && existingStrong.length > 0
                && existingStrong.some(alias => strongAliases.has(alias));
        }
        return element.dataset.compressionWeakIdentity === weakIdentity;
    };
    const alreadyRendered = Array.from(
        document.querySelectorAll('.context-compression-divider')
    ).some(isSameBoundary);
    const alreadyQueued = typeof container.querySelectorAll === 'function'
        && Array.from(
            container.querySelectorAll('.context-compression-divider')
        ).some(isSameBoundary);
    if (alreadyRendered || alreadyQueued) return null;
    container.appendChild(divider);
    return divider;
}

function _markRunTerminal(ownerSession, requestId) {
    const matchedActiveRun = sessionActiveRequest[ownerSession] === requestId;
    if (matchedActiveRun) {
        delete sessionActiveRequest[ownerSession];
        persistActiveRuns();
    }
    delete activeRunStartedAt[activeRunKey(ownerSession, requestId)];
    persistActiveRunStartedAt();
    if (matchedActiveRun) syncRunProgressIndicator(ownerSession);
    clearSessionPendingApprovals(ownerSession, requestId);
}

function restorePendingRun(
    ownerSession = sessionId,
    requestId = sessionActiveRequest[ownerSession],
) {
    if (isNewChatDraft() || !ownerSession || !requestId) return Promise.resolve(null);
    const key = `${ownerSession}:${requestId}`;
    const existing = runRestorePromises.get(key);
    if (existing) return existing;
    const snapshotApprovalRevision = approvalRevision(ownerSession);
    const promise = (async () => {
        try {
            const response = await fetch(`${PUBLIC_API_BASE}/sessions/${encodeURIComponent(ownerSession)}`);
            if (sessionActiveRequest[ownerSession] !== requestId) return null;
            if (response.status === 404) {
                _markRunTerminal(ownerSession, requestId);
                if (ownerSession === sessionId) {
                    resetActiveRunControls(requestId);
                    await reloadAuthoritativeHistory();
                }
                return null;
            }
            const payload = await responseJson(response);
            if (!response.ok || payload.status !== 'success') {
                throw new Error(payload.message || `snapshot failed: ${response.status}`);
            }
            if (sessionActiveRequest[ownerSession] !== requestId) return null;
            const snapshot = payload.data;
            if (!snapshot.running && !snapshot.waiting) {
                _markRunTerminal(ownerSession, requestId);
                if (ownerSession === sessionId) {
                    resetActiveRunControls(requestId);
                    await reloadAuthoritativeHistory();
                }
                return snapshot;
            }
            if (snapshot.active_run_id && snapshot.active_run_id !== requestId) {
                trackAcceptedRun(ownerSession, snapshot.active_run_id);
                return snapshot;
            }
            if (
                snapshot.pending_interaction === 'approval'
                && !hasPendingApproval(ownerSession)
            ) {
                setSessionPendingApprovals(ownerSession, requestId, [{
                    type: 'approval_required',
                    approval_id: '__session_pending_approval__',
                }], snapshotApprovalRevision);
            }
            if (ownerSession === sessionId) setActiveRunControls(requestId);
            return snapshot;
        } catch (_) {
            if (sessionActiveRequest[ownerSession] !== requestId) return null;
            if (ownerSession === sessionId) setActiveRunControls(requestId);
            return null;
        } finally {
            if (runRestorePromises.get(key) === promise) {
                runRestorePromises.delete(key);
            }
        }
    })();
    runRestorePromises.set(key, promise);
    return promise;
}

function restorePendingApproval() {
    return restorePendingRun();
}

function addLoadingIndicator() {
    const el = document.createElement('div');
    el.className = 'flex gap-3 px-4 sm:px-6 py-3 chat-loading-indicator';
    el.dataset.startedAt = String(Date.now());
    el.dataset.assistantCount = String(
        messagesDiv.querySelectorAll('.bot-message-group').length
    );
    el.innerHTML = `
        <img src="assets/logo.jpg" alt="Bianbu Agent" class="w-8 h-8 rounded-lg flex-shrink-0">
        <div class="bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-2xl px-4 py-3">
            <div class="flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-primary-400 animate-pulse-dot" style="animation-delay: 0s"></span>
                <span class="w-2 h-2 rounded-full bg-primary-400 animate-pulse-dot" style="animation-delay: 0.2s"></span>
                <span class="w-2 h-2 rounded-full bg-primary-400 animate-pulse-dot" style="animation-delay: 0.4s"></span>
            </div>
        </div>
    `;
    messagesDiv.appendChild(el);
    scrollChatToBottom();
    return el;
}

function removeResolvedLoadingIndicators() {
    const assistants = Array.from(
        messagesDiv.querySelectorAll('.bot-message-group')
    );
    messagesDiv.querySelectorAll('.chat-loading-indicator').forEach(indicator => {
        const requestId = String(indicator.dataset.requestId || '');
        const baseline = Number(indicator.dataset.assistantCount || 0);
        const matchingReply = requestId && assistants.some(
            element => element.dataset.requestId === requestId
        );
        if (matchingReply || assistants.length > baseline) indicator.remove();
    });
    syncRunProgressIndicator();
}

function isNewChatDraft() {
    return newChatDraft !== null;
}

function prepareConversationSend() {
    if (!newChatDraft) {
        return {
            sessionId,
            agentId: activeAgent?.id || null,
            draft: false,
        };
    }
    if (!newChatDraft.sessionId) {
        newChatDraft.sessionId = generateSessionId();
    }
    sessionId = newChatDraft.sessionId;
    return {
        sessionId,
        agentId: newChatDraft.agent.id,
        draft: true,
    };
}

function promoteNewChatDraft(acceptedSessionId) {
    if (!newChatDraft || newChatDraft.sessionId !== acceptedSessionId) return;
    const agent = newChatDraft.agent;
    newChatDraft = null;
    sessionId = acceptedSessionId;
    activeAgent = agent;
    localStorage.setItem(SESSION_ID_KEY, sessionId);
    loadSessionList();
    updateComposerInteractionState();
}

function reloadAuthoritativeHistory() {
    if (isNewChatDraft() || !sessionId) return Promise.resolve();
    messagesDiv.replaceChildren();
    return openSessionHistory(sessionId, true);
}

function restoreRejectedSendInput(text, attachments) {
    if (newChatDraft) {
        renderEmptyConversation();
    } else {
        reloadAuthoritativeHistory();
    }
    chatInput.value = text;
    pendingAttachments = [...(attachments || [])];
    renderAttachmentPreview();
    chatInput.dispatchEvent(new Event('input'));
    chatInput.focus();
}

function enterNewChatDraft(agent, { activate = true } = {}) {
    resetTrajectoryState();
    // A background draft (activate=false) must not pull the user back to the
    // conversation tab while they are reading another view.
    if (activate) setConversationView('chat');
    sessionId = null;
    newChatDraft = { agent, sessionId: null };
    activeAgent = agent;
    setSessionTokenUsage(null, null);
    renderActiveAgent();
    resetActiveRunControls();
    closeSkillPicker();
    clearSelectedSkills();
    pendingAttachments = [];
    renderAttachmentPreview();
    renderEmptyConversation();
    if (activate) {
        if (currentView !== 'chat') navigateTo('chat');
        if (_isMobileView()) closeSessionPanel();
        chatInput.focus();
    }
}

function renderEmptyConversation() {
    messagesDiv.innerHTML = '';
    const ws = document.createElement('div');
    ws.id = 'welcome-screen';
    ws.className = 'flex flex-col items-center justify-center h-full px-6 pb-16';
    ws.style.paddingTop = '6vh';
    ws.innerHTML = `
        <img src="assets/logo.jpg" alt="Bianbu Agent" class="w-16 h-16 rounded-2xl mb-6 shadow-lg shadow-primary-500/20">
        <h1 class="text-2xl font-bold text-slate-800 dark:text-slate-100 mb-3">${appConfig.title || 'Bianbu Agent'}</h1>
        <p class="text-slate-500 dark:text-slate-400 text-center max-w-lg mb-10 leading-relaxed" data-i18n="welcome_subtitle">${t('welcome_subtitle')}</p>
        <div class="grid grid-cols-2 sm:grid-cols-3 gap-3 w-full max-w-2xl">
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-blue-50 dark:bg-blue-900/30 flex items-center justify-center">
                        <i class="fas fa-folder-open text-blue-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_sys_title">${t('example_sys_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_sys_text">${t('example_sys_text')}</p>
            </div>
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-emerald-50 dark:bg-emerald-900/30 flex items-center justify-center">
                        <i class="fas fa-code text-emerald-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_code_title">${t('example_code_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_code_text">${t('example_code_text')}</p>
            </div>
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-rose-50 dark:bg-rose-900/30 flex items-center justify-center">
                        <i class="fas fa-puzzle-piece text-rose-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_skill_title">${t('example_skill_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_skill_text">${t('example_skill_text')}</p>
            </div>
            <div class="example-card group bg-white dark:bg-[#1A1A1A] border border-slate-200 dark:border-white/10 rounded-xl p-4 cursor-pointer hover:border-primary-300 dark:hover:border-primary-600 hover:shadow-md transition-all duration-200" data-send="/help">
                <div class="flex items-center gap-2 mb-2">
                    <div class="w-7 h-7 rounded-lg bg-slate-100 dark:bg-slate-800 flex items-center justify-center">
                        <i class="fas fa-terminal text-slate-500 text-xs"></i>
                    </div>
                    <span class="font-medium text-sm text-slate-700 dark:text-slate-200" data-i18n="example_web_title">${t('example_web_title')}</span>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 leading-relaxed" data-i18n="example_web_text">${t('example_web_text')}</p>
            </div>
        </div>
    `;
    messagesDiv.appendChild(ws);
    ws.querySelectorAll('.example-card').forEach(card => {
        card.addEventListener('click', () => {
            const sendText = card.dataset.send;
            if (sendText) {
                chatInput.value = sendText;
                chatInput.dispatchEvent(new Event('input'));
                chatInput.focus();
                return;
            }
            const textEl = card.querySelector('[data-i18n*="text"]');
            if (textEl) {
                chatInput.value = textEl.textContent;
                chatInput.dispatchEvent(new Event('input'));
                chatInput.focus();
            }
        });
    });
}

// =====================================================================
// Session Panel
// =====================================================================

const SESSION_PANEL_KEY = 'bianbu_session_panel_open';
let sessionPanelOpen = localStorage.getItem(SESSION_PANEL_KEY) === '1';

function _persistPanelState() {
    localStorage.setItem(SESSION_PANEL_KEY, sessionPanelOpen ? '1' : '0');
}

function _isMobileView() {
    return window.innerWidth <= 768;
}

function _showSessionOverlay() {
    if (!_isMobileView()) return;
    const overlay = document.getElementById('session-panel-overlay');
    if (overlay) overlay.classList.remove('hidden');
}

function _hideSessionOverlay() {
    const overlay = document.getElementById('session-panel-overlay');
    if (overlay) overlay.classList.add('hidden');
}

function closeSessionPanel() {
    const panel = document.getElementById('session-panel');
    if (!panel || !sessionPanelOpen) return;
    sessionPanelOpen = false;
    panel.classList.add('hidden');
    _hideSessionOverlay();
    _persistPanelState();
}

function toggleSessionPanel() {
    const panel = document.getElementById('session-panel');
    if (!panel) return;
    sessionPanelOpen = !sessionPanelOpen;
    panel.classList.toggle('hidden', !sessionPanelOpen);
    if (sessionPanelOpen) {
        _showSessionOverlay();
    } else {
        _hideSessionOverlay();
    }
    _persistPanelState();
    if (sessionPanelOpen) loadSessionList();
}

function openSessionPanel() {
    const panel = document.getElementById('session-panel');
    if (!panel || sessionPanelOpen) return;
    sessionPanelOpen = true;
    panel.classList.remove('hidden');
    _showSessionOverlay();
    _persistPanelState();
    loadSessionList();
}

function _restoreSessionPanel() {
    const panel = document.getElementById('session-panel');
    if (!panel) return;
    if (_isMobileView()) {
        sessionPanelOpen = false;
        panel.classList.add('hidden');
        _hideSessionOverlay();
        _persistPanelState();
    } else if (sessionPanelOpen) {
        panel.classList.remove('hidden');
        _showSessionOverlay();
        loadSessionList();
    } else {
        panel.classList.add('hidden');
        _hideSessionOverlay();
    }
}

function appendAgentAvatar(container, avatar) {
    const value = String(avatar || '🤖');
    if (isUploadedAgentAvatar(value)) {
        const image = document.createElement('img');
        image.src = value;
        image.alt = '';
        container.appendChild(image);
        return;
    }
    const text = document.createElement('span');
    text.textContent = value;
    container.appendChild(text);
}

function agentDisplayName(agent) {
    if (!agent) return '';
    const localized = currentLang === 'zh' ? agent.name_zh : agent.name_en;
    if (String(localized || '').trim()) return String(localized).trim();
    return String(agent.name || agent.id || '');
}

function agentDescription(agent) {
    if (!agent) return '';
    const localized = currentLang === 'zh' ? agent.description_zh : agent.description;
    return String(localized || agent.description || agent.description_zh || '');
}

function sortSessionItems() {
    const container = document.getElementById('session-list');
    if (!container) return;
    const items = Array.from(container.querySelectorAll('.session-item'));
    items.sort((a, b) => {
        const left = sessionsById.get(a.dataset.sessionId) || {};
        const right = sessionsById.get(b.dataset.sessionId) || {};
        return Number(right.updated_at || right.created_at || 0)
            - Number(left.updated_at || left.created_at || 0)
            || a.dataset.sessionId.localeCompare(b.dataset.sessionId);
    });
    // 只移动位置真的变了的行。把已经排好的行重新插一遍会摘掉它的 :hover，
    // 行底色和悬停才出现的重命名/删除按钮会重新淡入——指针停在上面就是一直闪。
    let expected = null;
    for (let index = items.length - 1; index >= 0; index -= 1) {
        const item = items[index];
        if (item.nextElementSibling !== expected) {
            container.insertBefore(item, expected);
        }
        expected = item;
    }
}

// 标题只写真的不一样的值：重复写 textContent 会换掉文本节点，界面没有任何
// 变化却在重画。
function applySessionTitle(element, record) {
    if (!element) return;
    const title = element.querySelector('.session-title');
    if (!title || element.querySelector('.session-title-input')) return;
    const value = String(record?.title || t('untitled_session'));
    if (title.textContent !== value) title.textContent = value;
    if (title.title !== value) title.title = value;
}

// 兜底标题跟着界面语言走；主机快照不再无条件重写标题，语言切换时在这里补一次。
function refreshSessionTitles() {
    document.querySelectorAll('#session-list .session-item').forEach(item => {
        applySessionTitle(item, sessionsById.get(item.dataset.sessionId));
    });
}

function createSessionItem(record, isActive = false) {
    const sid = String(record.session_id || record.id || '');
    const titleText = String(record.title || t('untitled_session'));
    const item = document.createElement('div');
    item.className = `session-item${isActive ? ' active' : ''}`;
    item.dataset.sessionId = sid;
    const icon = document.createElement('i');
    icon.className = 'fas fa-message session-icon';
    const titleWrap = document.createElement('span');
    titleWrap.className = 'session-title-wrap';
    const title = document.createElement('span');
    title.className = 'session-title';
    title.title = titleText;
    title.textContent = titleText;
    titleWrap.appendChild(title);
    const rename = document.createElement('button');
    rename.type = 'button';
    rename.className = 'session-rename';
    rename.title = t('rename_session');
    const renameIcon = document.createElement('i');
    renameIcon.className = 'fas fa-pen';
    rename.appendChild(renameIcon);
    rename.addEventListener('click', event => {
        event.stopPropagation();
        renameSession(sid);
    });
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'session-delete';
    remove.title = 'Delete';
    const removeIcon = document.createElement('i');
    removeIcon.className = 'fas fa-trash-can';
    remove.appendChild(removeIcon);
    remove.addEventListener('click', event => {
        event.stopPropagation();
        deleteSession(sid);
    });
    item.append(icon, titleWrap, rename, remove);
    item.addEventListener('click', () => switchSession(sid));
    queueMicrotask(() => renderSessionStatus(sid));
    return item;
}

function applyHostSessionSnapshot(items) {
    const sessions = (items || []).map(item => ({
        ...item, session_id: String(item.id || item.session_id || ''),
    })).filter(item => item.session_id);
    const present = new Set(sessions.map(item => item.session_id));
    sessions.forEach(record => {
        sessionsById.set(record.session_id, record);
        let element = document.querySelector(
            `.session-item[data-session-id="${CSS.escape(record.session_id)}"]`
        );
        if (!element) {
            const container = document.getElementById('session-list');
            if (container) {
                container.querySelector('.session-empty')?.remove();
                element = createSessionItem(record, record.session_id === sessionId);
                container.prepend(element);
            }
        }
        applySessionTitle(element, record);
    });
    Array.from(sessionsById.keys()).forEach(sid => {
        if (present.has(sid)) return;
        sessionsById.delete(sid);
        document.querySelector(
            `.session-item[data-session-id="${CSS.escape(sid)}"]`
        )?.remove();
    });
    sortSessionItems();
}

function _applyInputTooltips() {
    const set = (id, key, pos) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.setAttribute('data-tooltip', t(key));
        el.removeAttribute('title');
        if (pos) el.setAttribute('data-tooltip-pos', pos);
    };
    set('history-new-chat-btn', 'tip_new_chat');
    set('session-toggle-btn', 'session_history', 'bottom');
}

function _addOptimisticSessionItem(sid) {
    const container = document.getElementById('session-list');
    if (!container) return;

    const emptyEl = container.querySelector('.session-empty');
    if (emptyEl) emptyEl.remove();

    document.querySelectorAll('.session-item.active').forEach(el => el.classList.remove('active'));

    const item = createSessionItem({ session_id: sid, title: t('new_chat') }, true);
    container.prepend(item);
}

let _sessionPage = 1;
let _sessionHasMore = false;
let _sessionLoading = false;
const _SESSION_PAGE_SIZE = 50;

function loadSessionList(onDone) {
    const container = document.getElementById('session-list');
    if (!container) return;

    _sessionPage = 1;
    _sessionHasMore = false;

    _fetchSessionPage(1, true, onDone);
}

function _fetchSessionPage(page, clear, onDone) {
    if (_sessionLoading) return;
    _sessionLoading = true;

    const container = document.getElementById('session-list');
    if (!container) { _sessionLoading = false; return; }

    // Remove existing "load more" sentinel before fetching
    const oldSentinel = container.querySelector('.session-load-more');
    if (oldSentinel) oldSentinel.remove();

    Promise.all([
        fetch(`${PUBLIC_API_BASE}/sessions?page=${page}&page_size=${_SESSION_PAGE_SIZE}`).then(responseJson),
        refreshAgentList(),
    ]).then(([data]) => {
            _sessionLoading = false;
            if (data.status !== 'success') return;

            if (clear) {
                container.innerHTML = '';
                sessionsById.clear();
            }

            const sessions = (data.data || []).map(item => ({
                ...item,
                session_id: item.id,
            }));
            _sessionPage = page;
            _sessionHasMore = !!data.page?.has_more;
            sessionRuntime.updateSummaries(sessions);

            if (sessions.length === 0 && page === 1) {
                container.innerHTML = '<div class="session-empty">' + t('untitled_session') + '</div>';
                if (typeof onDone === 'function') onDone();
                return;
            }

            sessions.forEach(s => {
                sessionsById.set(s.session_id, s);
                const agent = agentForId(s.agent_id);
                if (s.session_id === sessionId) {
                    activeAgent = agent;
                    renderActiveAgent();
                }
                container.querySelector(`.session-item[data-session-id="${CSS.escape(s.session_id)}"]`)?.remove();
                const isActive = s.session_id === sessionId;
                const item = createSessionItem(s, isActive);
                container.appendChild(item);
            });
            sortSessionItems();

            if (typeof onDone === 'function') onDone();
        })
        .catch(() => { _sessionLoading = false; });
}

function _onSessionListScroll() {
    if (!_sessionHasMore || _sessionLoading) return;
    const container = document.getElementById('session-list');
    if (!container) return;
    // Trigger when scrolled near the bottom (within 60px)
    if (container.scrollHeight - container.scrollTop - container.clientHeight < 60) {
        _fetchSessionPage(_sessionPage + 1, false);
    }
}

// Attach scroll listener once DOM is ready
(function _initSessionScroll() {
    const el = document.getElementById('session-list');
    if (el) {
        el.addEventListener('scroll', _onSessionListScroll);
    } else {
        document.addEventListener('DOMContentLoaded', () => {
            const el2 = document.getElementById('session-list');
            if (el2) el2.addEventListener('scroll', _onSessionListScroll);
        });
    }
})();

function switchSession(newSessionId) {
    if (newSessionId === sessionId) {
        if (currentView !== 'chat') navigateTo('chat');
        return;
    }

    newChatDraft = null;
    closeSkillPicker();
    clearSelectedSkills();
    resetTrajectoryState();
    const targetSessionId = newSessionId;
    sessionId = targetSessionId;
    sessionRuntime.select(targetSessionId);
    renderSessionStatus(targetSessionId);
    const record = sessionsById.get(sessionId);
    activeAgent = record ? agentForId(record.agent_id) : null;
    renderActiveAgent();
    restoreSessionBinding(sessionId);
    localStorage.setItem(SESSION_ID_KEY, sessionId);

    renderSessionModelSnapshot(targetSessionId);
    setSessionTokenUsage(null, sessionId);
    const summary = sessionRuntime.summary(targetSessionId);
    if (summary.active_run_id) setActiveRunControls(summary.active_run_id);
    else resetActiveRunControls();
    void openSessionHistory(targetSessionId).finally(() => {
        if (sessionId === targetSessionId) restorePendingApproval();
    });

    document.querySelectorAll('.session-item').forEach(el => {
        el.classList.toggle('active', el.dataset.sessionId === sessionId);
    });

    if (_isMobileView()) closeSessionPanel();
    if (currentView !== 'chat') navigateTo('chat');
}

// In-place rename a session title: replace the title <span> with an <input>,
// commit on Enter/blur, cancel on Escape. Persists through the Session resource.
function renameSession(sid) {
    const item = Array.from(document.querySelectorAll('.session-item[data-session-id]'))
        .find(element => element.dataset.sessionId === sid);
    if (!item) return;
    const titleEl = item.querySelector('.session-title');
    if (!titleEl || item.querySelector('.session-title-input')) return;

    const oldTitle = titleEl.textContent;

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-title-input';
    input.value = oldTitle;
    input.maxLength = 100;

    // Avoid switching session while interacting with the input
    const stop = e => e.stopPropagation();
    input.addEventListener('click', stop);
    input.addEventListener('mousedown', stop);

    titleEl.replaceWith(input);
    input.focus();
    input.select();

    let done = false;

    const restore = (title) => {
        if (done) return;
        done = true;
        const span = document.createElement('span');
        span.className = 'session-title';
        span.title = title;
        span.textContent = title;
        input.replaceWith(span);
    };

    const commit = () => {
        if (done) return;
        const newTitle = normalizeSessionTitle(input.value);
        if (!newTitle || newTitle === oldTitle || !isSafeSessionResourceId(sid)) {
            restore(oldTitle);
            return;
        }
        // Optimistically show the new title, then persist.
        restore(newTitle);
        fetch(`${PUBLIC_API_BASE}/sessions/${encodeURIComponent(sid)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle })
        })
            .then(responseJson)
            .then(data => {
                if (data.status !== 'success') {
                    // Revert UI on failure
                    const span = item.querySelector('.session-title');
                    if (span) {
                        span.title = oldTitle;
                        span.textContent = oldTitle;
                    }
                }
            })
            .catch(() => {
                const span = item.querySelector('.session-title');
                if (span) {
                    span.title = oldTitle;
                    span.textContent = oldTitle;
                }
            });
    };

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(); }
        else if (e.key === 'Escape') { e.preventDefault(); restore(oldTitle); }
    });
    input.addEventListener('blur', commit);
}

function deleteSession(sid) {
    showConfirmModal(t('delete_session_title'), t('delete_session_confirm'), () => {
        // Before deleting, find the next real session to fall back to when the
        // current one is removed (the sibling item in the list, which is sorted
        // newest-first). Falls back to the welcome screen if none remain.
        const nextSid = sid === sessionId ? _findNextSessionId(sid) : null;

        fetch(`${PUBLIC_API_BASE}/sessions/${encodeURIComponent(sid)}`, { method: 'DELETE' })
            .then(async response => {
                if (!response.ok) {
                    await explainRefusedDelete(response, t('delete_session_failed'));
                    return;
                }
                if (sid !== sessionId) {
                    loadSessionList();
                    return;
                }
                if (nextSid) {
                    // Switch to an existing session; refresh the list afterwards
                    // so the deleted item disappears.
                    switchSession(nextSid);
                    loadSessionList();
                } else {
                    // No other sessions: reset to a fresh empty session without
                    // inserting an optimistic placeholder (it has no backend
                    // record and would be an empty, undeletable item).
                    void startNewChat({
                        discardStoredSession: true,
                        discardedSessionId: sid,
                    });
                }
            })
            .catch(() => {});
    });
}

function preferredBibitAgent() {
    const available = agentsState.filter(agent => agentCanStartSession(agent));
    return available.find(agent => agent.origin === 'system'
        && agent.name === 'BibitEdgeAssistant')
        || available.find(agent => agent.name === 'BibitEdgeAssistant');
}

// Every new chat starts on the built-in Expert. Users switch Experts from the
// composer with "@<name>", so no picker is needed to begin a conversation.
// The lookup only walks further when the built-in Expert cannot start yet, and
// returns null when the catalog is empty.
function resolveNewChatAgent() {
    const startable = agentsState.filter(agent => agentCanStartSession(agent));
    return preferredBibitAgent()
        || startable.find(agent => agent.id === activeAgent?.id)
        || startable[0]
        || agentsState.find(agent => agent.name === 'BibitEdgeAssistant')
        || agentsState[0]
        || null;
}

// Starting a new chat keeps the user moving instead of reopening the Expert
// picker. Switching Experts happens through "@" in the composer, and the picker
// stays reachable from the history panel for browsing.
//
// `discardStoredSession` drops the persisted pointer as well: callers use it
// when the previous session is gone (deleted or never existed), so a reload
// cannot resurrect it. A plain new chat keeps the pointer and therefore still
// discards the unsent draft when the page reloads.
//
// The pointer is only dropped while it still names the session being
// discarded. Startup bookkeeping is asynchronous, so an unconditional
// removeItem() can land after a newer value was written (the user opened
// another conversation, or a second tab started one) and silently discard it.
function discardStoredSessionPointer(expectedSessionId) {
    const stored = localStorage.getItem(SESSION_ID_KEY);
    if (stored === null || stored !== String(expectedSessionId || '')) return;
    localStorage.removeItem(SESSION_ID_KEY);
}

async function startNewChat({
    activate = true,
    discardStoredSession = false,
    discardedSessionId = null,
} = {}) {
    if (discardStoredSession) discardStoredSessionPointer(discardedSessionId);
    sessionId = null;
    newChatDraft = null;
    closeChatAgentMenu();
    try {
        await refreshAgentList();
    } catch (_) {
        // Keep going with whatever catalog is already loaded.
    }
    const agent = resolveNewChatAgent();
    if (!agent) {
        activeAgent = null;
        renderActiveAgent();
        renderEmptyConversation();
        return;
    }
    enterNewChatDraft(agent, { activate });
}

function enterAgentChooserDraft({ activate = true } = {}) {
    localStorage.removeItem(SESSION_ID_KEY);
    sessionId = null;
    newChatDraft = null;
    activeAgent = null;
    const bibit = preferredBibitAgent();
    if (bibit) {
        closeChatAgentMenu();
        enterNewChatDraft(bibit, { activate });
        return;
    }
    renderActiveAgent();
    renderEmptyConversation();
    agentChooserMode = 'new-chat';
    openSessionPanel();
    const menu = document.getElementById('chat-agent-menu');
    if (menu?.classList.contains('hidden')) void toggleChatAgentMenu();
}

// Pick the session to show after deleting `sid` (the current session): prefer
// the next item below it in the list, otherwise the previous one. Returns null
// if no other session exists.
function _findNextSessionId(sid) {
    const items = Array.from(document.querySelectorAll('.session-item[data-session-id]'));
    const idx = items.findIndex(el => el.dataset.sessionId === sid);
    if (idx === -1) {
        const other = items.find(el => el.dataset.sessionId !== sid);
        return other ? other.dataset.sessionId : null;
    }
    const next = items[idx + 1] || items[idx - 1];
    return next ? next.dataset.sessionId : null;
}

function showConfirmModal(title, message, onConfirm) {
    let overlay = document.getElementById('confirm-modal-overlay');
    if (overlay) overlay.remove();

    overlay = document.createElement('div');
    overlay.id = 'confirm-modal-overlay';
    overlay.className = 'confirm-overlay';

    const modal = document.createElement('div');
    modal.className = 'confirm-modal';
    modal.innerHTML = `
        <div class="confirm-title">${escapeHtml(title)}</div>
        <div class="confirm-message">${escapeHtml(message)}</div>
        <div class="confirm-actions">
            <button class="confirm-btn confirm-btn-cancel">${t('confirm_cancel')}</button>
            <button class="confirm-btn confirm-btn-ok">${t('confirm_yes')}</button>
        </div>
    `;
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    requestAnimationFrame(() => overlay.classList.add('visible'));

    const close = () => {
        overlay.classList.remove('visible');
        setTimeout(() => overlay.remove(), 200);
    };

    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    modal.querySelector('.confirm-btn-cancel').addEventListener('click', close);
    modal.querySelector('.confirm-btn-ok').addEventListener('click', () => {
        close();
        onConfirm();
    });
}

function clearContext() {
    if (isNewChatDraft() || !sessionId) return;
    fetch(`${PUBLIC_API_BASE}/sessions/${encodeURIComponent(sessionId)}/clear-context`, { method: 'POST' })
        .then(responseJson)
        .then(data => {
            if (data.status !== 'success') return;
            // Insert a visual divider in the chat
            const divider = document.createElement('div');
            divider.className = 'context-divider';
            divider.innerHTML = `<span>${t('context_cleared')}</span>`;
            messagesDiv.appendChild(divider);
            scrollChatToBottom();
        })
        .catch(() => {});
}

async function generateSessionTitle(sid, userMsg) {
    const owner = String(sid || '');
    const content = String(userMsg || '').trim();
    if (!owner || !content) return false;
    try {
        const response = await fetch(
            `${PUBLIC_API_BASE}/sessions/${encodeURIComponent(owner)}/generate-title`,
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    message_id: generateSessionMessageId(),
                    content,
                }),
            },
        );
        if (!response.ok) {
            console.warn('[session.generate-title]', response.status);
            return false;
        }
        if (sessionPanelOpen) loadSessionList();
        return true;
    } catch (error) {
        console.warn('[session.generate-title]', error);
        return false;
    }
}

// =====================================================================
// Utilities
// =====================================================================
function formatTime(date) {
    const now = new Date();
    const sameDay = date.getFullYear() === now.getFullYear()
        && date.getMonth() === now.getMonth()
        && date.getDate() === now.getDate();
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (sameDay) return time;
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    if (date.getFullYear() === now.getFullYear()) return `${m}-${d} ${time}`;
    return `${date.getFullYear()}-${m}-${d} ${time}`;
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

function escapeAttribute(str) {
    return escapeHtml(String(str ?? ''))
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatToolArgs(args) {
    if (!args || Object.keys(args).length === 0) return '(none)';
    try {
        return escapeHtml(JSON.stringify(args, null, 2));
    } catch (_) {
        return escapeHtml(String(args));
    }
}

function isTodoTool(name) {
    return name === 'todo';
}

function renderTodoToolHtml(todos) {
    const items = Array.isArray(todos) ? todos : [];
    const completed = items.filter(item => item && item.status === 'completed').length;
    const rows = items.map(item => {
        const status = ['pending', 'in_progress', 'completed'].includes(item && item.status)
            ? item.status
            : 'pending';
        const icon = status === 'completed'
            ? 'fa-check'
            : (status === 'in_progress' ? 'fa-circle-dot' : 'fa-circle');
        return `
            <div class="todo-item todo-item-${status}" data-status="${status}">
                <i class="fas ${icon}"></i>
                <span>${escapeHtml(String((item && item.content) || ''))}</span>
            </div>`;
    }).join('');
    return `
        <div class="todo-header">
            <i class="fas fa-list-check tool-icon"></i>
            <span class="tool-name">Todo</span>
            <span class="todo-summary">${completed}/${items.length}</span>
        </div>
        <div class="todo-list">${rows}</div>`;
}

function scrollChatToBottom(force) {
    if (force || _autoScrollEnabled) {
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }
}

function _updateScrollToBottomBtn() {
    const btn = document.getElementById('scroll-to-bottom-btn');
    if (!btn) return;
    const distFromBottom = messagesDiv.scrollHeight - messagesDiv.scrollTop - messagesDiv.clientHeight;
    btn.classList.toggle('hidden', distFromBottom <= _SCROLL_THRESHOLD);
}

function applyHighlighting(container) {
    const root = container || document;
    setTimeout(() => {
        const hljsLib = getHljs();
        root.querySelectorAll('pre code').forEach(block => {
            if (!block.classList.contains('hljs')) {
                hljsLib.highlightElement(block);
            }
        });
        // Add language labels and copy buttons to code blocks
        _addCodeBlockHeaders(root);
    }, 0);
}

// =====================================================================
// Config View
// =====================================================================
// --- Custom dropdown helper ---
function initDropdown(el, options, selectedValue, onChange, opts) {
    // opts.placeholder: when set AND selectedValue is empty, render that text
    // in a dim style instead of auto-selecting options[0]. Useful for
    // "pick or empty" capabilities (asr / embedding) where we want the
    // user to make an explicit choice.
    opts = opts || {};
    const textEl = el.querySelector('.cfg-dropdown-text');
    const menuEl = el.querySelector('.cfg-dropdown-menu');
    const selEl = el.querySelector('.cfg-dropdown-selected');

    el._ddValue = selectedValue || '';
    el._ddOnChange = onChange;

    function render() {
        menuEl.innerHTML = '';
        options.forEach(opt => {
            const item = document.createElement('div');
            item.className = 'cfg-dropdown-item' + (opt.value === el._ddValue ? ' active' : '');
            item.dataset.value = opt.value;
            // Hint is an optional dim secondary label rendered on the right
            // side of the row (e.g. friendly brand name next to a technical
            // model id). When absent the row degrades to the original
            // single-string layout.
            if (opt.hint) {
                const labelEl = document.createElement('span');
                labelEl.className = 'cfg-dropdown-label';
                labelEl.textContent = opt.label;
                const hintEl = document.createElement('span');
                hintEl.className = 'cfg-dropdown-hint';
                hintEl.textContent = opt.hint;
                item.appendChild(labelEl);
                item.appendChild(hintEl);
            } else {
                item.textContent = opt.label;
            }
            item.addEventListener('click', (e) => {
                e.stopPropagation();
                el._ddValue = opt.value;
                textEl.textContent = opt.label;
                menuEl.querySelectorAll('.cfg-dropdown-item').forEach(i => i.classList.remove('active'));
                item.classList.add('active');
                el.classList.remove('open');
                if (el._ddOnChange) el._ddOnChange(opt.value);
            });
            menuEl.appendChild(item);
        });
        const sel = options.find(o => o.value === el._ddValue);
        if (sel) {
            textEl.textContent = sel.label;
            textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
        } else if (opts.placeholder && !el._ddValue) {
            // No selection yet — show the placeholder in muted style.
            // Do NOT write a fallback value, so the dropdown stays
            // "unsaved" until the user explicitly picks.
            textEl.textContent = opts.placeholder;
            textEl.classList.add('text-slate-400', 'dark:text-slate-500');
        } else {
            textEl.textContent = options[0] ? options[0].label : '--';
            textEl.classList.remove('text-slate-400', 'dark:text-slate-500');
            if (options[0]) el._ddValue = options[0].value;
        }
    }

    render();

    if (!el._ddBound) {
        selEl.addEventListener('click', (e) => {
            e.stopPropagation();
            document.querySelectorAll('.cfg-dropdown.open').forEach(d => {
                if (d !== el) {
                    d.classList.remove('open');
                    d.querySelector('.agent-model-dropdown-toggle')?.setAttribute('aria-expanded', 'false');
                }
            });
            el.classList.toggle('open');
        });
        el._ddBound = true;
    }
}

document.addEventListener('click', () => {
    document.querySelectorAll('.cfg-dropdown.open').forEach(d => {
        d.classList.remove('open');
        d.querySelector('.agent-model-dropdown-toggle')?.setAttribute('aria-expanded', 'false');
    });
});

function getDropdownValue(el) { return el._ddValue || ''; }

function initAgentModelDropdown() {
    const dropdown = document.getElementById('agent-form-model-dropdown');
    const toggle = dropdown?.querySelector('.agent-model-dropdown-toggle');
    if (!dropdown || !toggle || dropdown._modelDropdownBound) return;
    toggle.addEventListener('click', event => {
        event.stopPropagation();
        document.querySelectorAll('.cfg-dropdown.open').forEach(item => {
            if (item !== dropdown) {
                item.classList.remove('open');
                item.querySelector('.agent-model-dropdown-toggle')?.setAttribute('aria-expanded', 'false');
            }
        });
        dropdown.classList.toggle('open');
        toggle.setAttribute('aria-expanded', dropdown.classList.contains('open') ? 'true' : 'false');
    });
    dropdown._modelDropdownBound = true;
}

function renderAgentModelOptions(models) {
    const dropdown = document.getElementById('agent-form-model-dropdown');
    const menu = document.getElementById('agent-model-options');
    const input = document.getElementById('agent-form-model-name');
    if (!dropdown || !menu || !input) return;
    menu.replaceChildren();
    (models || []).forEach(model => {
        const modelId = String(model.id || '').trim();
        if (!modelId) return;
        const item = document.createElement('div');
        item.className = 'cfg-dropdown-item';
        item.dataset.value = modelId;
        item.textContent = String(model.name || modelId);
        item.addEventListener('click', event => {
            event.stopPropagation();
            input.value = modelId;
            menu.querySelectorAll('.cfg-dropdown-item').forEach(option => {
                option.classList.toggle('active', option === item);
            });
            dropdown.classList.remove('open');
            dropdown.querySelector('.agent-model-dropdown-toggle')?.setAttribute('aria-expanded', 'false');
        });
        menu.appendChild(item);
    });
}

// =====================================================================
// Skills View
// =====================================================================
let toolsLoaded = false;

const TOOL_ICONS = {
    todo: 'fa-list-check',
    terminal: 'fa-terminal',
    process: 'fa-gears',
    read_file: 'fa-file-lines',
    write_file: 'fa-file-pen',
    patch: 'fa-pen-to-square',
    search_files: 'fa-folder-tree',
    web_search: 'fa-magnifying-glass',
    web_extract: 'fa-globe',
    memory: 'fa-brain',
    session_search: 'fa-book-open',
    skills_list: 'fa-puzzle-piece',
    skill_view: 'fa-book',
    skill_manage: 'fa-screwdriver-wrench',
};

function getToolIcon(name) {
    if (name.startsWith('kanban_')) return 'fa-list-check';
    return TOOL_ICONS[name] || 'fa-wrench';
}

function loadSkillsView() {
    loadSkillsSection();
}

function loadToolsView() {
    loadToolsSection();
}

let pluginsLoaded = false;

function loadPluginsView(force = false) {
    if (pluginsLoaded && !force) return Promise.resolve();
    return loadPluginsAdminView().then(() => { pluginsLoaded = true; });
}

async function installPlugin() {
    await installPluginAdmin();
    pluginsLoaded = true;
    toolsLoaded = false;
    invalidateAgentCatalog();
}

async function uninstallPlugin(pluginId) {
    if (!await deletePluginAdmin(pluginId)) return;
    pluginsLoaded = true;
    toolsLoaded = false;
    invalidateAgentCatalog();
}

let mcpLoaded = false;

function renderMcpServers(servers) {
    const empty = document.getElementById('mcp-empty');
    const list = document.getElementById('mcp-list');
    const pluginNote = document.getElementById('mcp-plugin-owned-note');
    list.replaceChildren();
    // 插件随包提供的 MCP 属于插件的成员：状态在「插件」页随插件显示，这里只
    // 管理独立 MCP，免得同一台 server 在两个页面各说一遍、还可能说得不一致。
    const pluginOwned = servers.filter(server => server.owner_plugin_id);
    servers = servers.filter(server => !server.owner_plugin_id);
    if (pluginNote) {
        pluginNote.textContent = pluginOwned.length
            ? `${t('mcp_plugin_owned_note')}（${pluginOwned.length}）`
            : '';
        pluginNote.classList.toggle('hidden', !pluginOwned.length);
    }
    if (!servers.length) {
        empty.textContent = pluginOwned.length ? t('mcp_empty_plugin_owned_only') : t('mcp_empty');
        empty.classList.remove('hidden');
        list.classList.add('hidden');
        return;
    }
    servers.forEach(server => {
        const card = document.createElement('article');
        card.dataset.mcpId = server.id || '';
        card.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-4';
        const tools = server.tools || [];
        const detail = mcpStateDetail(server);
        card.innerHTML = `
            <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                    <div class="flex items-center gap-2 flex-wrap">
                        <strong class="text-sm text-slate-800 dark:text-slate-100">${escapeHtml(server.display_name || server.id || '')}</strong>
                        <span class="text-[10px] px-1.5 py-0.5 rounded ${mcpStatusBadgeClass(server)}">${escapeHtml(mcpStateLabel(server))}</span>
                        <span class="text-[10px] px-1.5 py-0.5 rounded bg-violet-50 text-violet-600 dark:bg-violet-900/20 dark:text-violet-300">${escapeHtml(server.transport || 'stdio')}</span>
                        ${tools.length ? `<span class="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 dark:bg-white/5 dark:text-slate-400">${tools.length} ${escapeHtml(t('plugin_mcp_tools'))}</span>` : ''}
                    </div>
                    <p class="font-mono text-xs text-slate-400 mt-1 break-all">${escapeHtml(server.id || '')}</p>
                </div>
                ${server.removable === false
                    ? `<span class="text-xs text-slate-400">${server.owner_plugin_id ? `Plugin · ${escapeHtml(server.owner_plugin_id)}` : t('mcp_system_managed')}</span>`
                    : `<button type="button" data-action="uninstall" class="text-xs text-red-500 hover:text-red-600">${escapeHtml(t('mcp_uninstall'))}</button>`}
            </div>
            ${server.command ? `<code class="block mt-3 px-2 py-1.5 rounded bg-slate-50 dark:bg-black/20 text-xs text-slate-600 dark:text-slate-300 break-all">${escapeHtml(server.command)}</code>` : ''}
            ${detail ? `<p class="mt-3 text-xs ${mcpStateDetailTone(server)} break-all">${escapeHtml(detail)}</p>` : ''}
            <div class="mt-3 flex flex-wrap gap-1.5">
                ${tools.length ? tools.map(tool => `<span class="text-xs px-2 py-1 rounded bg-slate-100 dark:bg-white/5 text-slate-600 dark:text-slate-300">${escapeHtml(tool.display_name || tool.name || tool.id || '')}</span>`).join('') : `<span class="text-xs text-slate-400">${escapeHtml(mcpToolsPlaceholder(server))}</span>`}
            </div>`;
        card.querySelector('[data-action="uninstall"]')?.addEventListener('click', () => uninstallMcp(server.id || ''));
        list.appendChild(card);
    });
    empty.classList.add('hidden');
    list.classList.remove('hidden');
}

// 这一段与它调用的几个小函数必须自包含：Console 契约测试只加载
// renderMcpServers 到 loadMcpView 之间的代码。
//
// == MCP 生命周期共用块：MCP 页和插件页共用同一套状态词/解释/配色 ==
// Console 契约测试按这两个标记截取这一块，所以它必须自包含（只依赖 t/escapeHtml）。
// 状态词和「它在干什么」分开：Runtime 现在同时给 state（生命周期）和 phase
// （准备到哪一步），只报 preparing 时用户盯着一动不动的那个问题就出在这里。
const MCP_STATE_LABELS = {
    not_loaded: 'mcp_state_not_loaded', preparing: 'mcp_state_preparing', ready: 'mcp_state_ready',
    degraded: 'mcp_state_degraded', failed: 'mcp_state_failed', stopped: 'mcp_state_stopped',
    unknown: 'mcp_state_unknown',
};

// 老 Runtime 只给 status（healthy/unhealthy/preparing/…），先归一化再取词，
// 免得升级 Console 之后徽章变成英文原文。
function mcpServerState(server) {
    const raw = String((server && server.state) || '').toLowerCase();
    if (MCP_STATE_LABELS[raw]) return raw;
    const status = String((server && server.status) || '').toLowerCase();
    if (status === 'healthy' || status === 'ready' || status === 'running') return 'ready';
    if (status === 'preparing' || status === 'starting') return 'preparing';
    if (status === 'degraded') return 'degraded';
    if (status === 'failed' || status === 'unhealthy' || status === 'error') return 'failed';
    if (status === 'stopped') return 'stopped';
    return status === 'not_loaded' ? 'not_loaded' : 'unknown';
}

function mcpStateLabel(server) {
    const state = mcpServerState(server);
    const key = MCP_STATE_LABELS[state];
    return key ? t(key) : String((server && server.status) || 'unknown');
}

// 准备中的 phase 是唯一能解释「为什么还没好」的东西：装依赖和冷启动是两种
// 不同的等待，前者以分钟计。
function mcpStateDetail(server) {
    const detail = String((server && server.detail) || '').trim();
    if (detail) return detail;
    const state = mcpServerState(server);
    if (state === 'preparing') {
        return String((server && server.phase) || '').toLowerCase() === 'installing'
            ? t('mcp_phase_installing')
            : t('mcp_phase_starting');
    }
    if (state === 'not_loaded') return t('mcp_state_not_loaded_hint');
    if (state === 'unknown') return t('mcp_state_unknown_hint');
    return '';
}

// 真正出错的原因用琥珀色，纯进度用中性灰：同一行里既可能写「npm 404」，
// 也可能写「正在下载」，颜色不能都当错误。
function mcpStateDetailTone(server) {
    const state = mcpServerState(server);
    if (state === 'failed' || state === 'degraded') {
        return 'text-amber-600 dark:text-amber-300';
    }
    return 'text-slate-400 dark:text-slate-500';
}

// 色表：准备中是中性蓝（npm 固化/冷启动本来就要几十秒，不是故障），
// 失败才用琥珀色。
function mcpStatusBadgeClass(server) {
    switch (mcpServerState(server)) {
        case 'ready':
            return 'bg-emerald-50 text-emerald-600 dark:bg-emerald-900/20 dark:text-emerald-300';
        case 'preparing':
            return 'bg-sky-50 text-sky-600 dark:bg-sky-900/20 dark:text-sky-300';
        case 'failed':
        case 'degraded':
            return 'bg-amber-50 text-amber-600 dark:bg-amber-900/20 dark:text-amber-300';
        default:
            return 'bg-slate-100 text-slate-500 dark:bg-white/5 dark:text-slate-400';
    }
}

// 只有「就绪」却一个工具都没有才是这台 Server 真没工具；其余状态说清是
// 还没连上（或在准备），而不是让用户以为它没有工具。
function mcpToolsPlaceholder(server) {
    switch (mcpServerState(server)) {
        case 'preparing':
            return t('mcp_server_preparing');
        case 'ready':
            return t('mcp_no_tools');
        default:
            return t('mcp_not_connected');
    }
}
// == MCP 生命周期共用块结束 ==


function loadMcpView(force = false) {
    if (mcpLoaded && !force) return Promise.resolve();
    const empty = document.getElementById('mcp-empty');
    empty.textContent = t('mcp_loading');
    empty.classList.remove('hidden');
    return fetch(`${PUBLIC_API_BASE}/mcp-servers`).then(async response => {
        const data = await responseJson(response);
        if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Failed to load MCP servers');
        renderMcpServers(data.data || []);
        mcpLoaded = true;
        watchMcpPreparation(data.data || []);
    }).catch(error => {
        empty.textContent = String(error.message || error);
        empty.classList.remove('hidden');
    });
}

let mcpPreparingTimer;
// Worker 发布一次增删要几秒（它得重建 Generation），这期间新 Server 还是
// not_loaded：刚动过目录的那一小段时间要接着看，否则装完立刻刷新只会看到
// 「未加载」，用户又得自己反复点。
let mcpSettlingUntil = 0;

// 「准备中」是要看的：首次启动要下载 npm 包并握手，状态、phase 和工具清单都会
// 变，用户不该为了看到变化反复点刷新。离开 MCP 页就停，等出结果也停。
function watchMcpPreparation(servers) {
    clearTimeout(mcpPreparingTimer);
    mcpPreparingTimer = undefined;
    if (currentView !== 'mcp') return;
    const waitingForWorker = Date.now() < mcpSettlingUntil;
    const watching = (servers || []).some(server => {
        const state = mcpServerState(server);
        if (state === 'preparing') return true;
        return waitingForWorker && (state === 'not_loaded' || state === 'unknown');
    });
    if (!watching) return;
    mcpPreparingTimer = setTimeout(() => { loadMcpView(true); }, 3000);
}

function stopMcpPreparationWatch() {
    clearTimeout(mcpPreparingTimer);
    mcpPreparingTimer = undefined;
}

function refreshMcp() {
    const button = document.getElementById('mcp-refresh-button');
    const status = document.getElementById('mcp-health-status');
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    if (status) status.textContent = t('agents_refresh_mcp_running');
    mcpLoaded = false;
    return fetch(`${PUBLIC_API_BASE}/mcp-health-checks`, {method: 'POST'})
        .then(async response => {
            const data = await responseJson(response);
            if (!response.ok || data.status !== 'success') {
                throw new Error(data.message || t('agents_refresh_mcp_failed'));
            }
            if (status) {
                const checkedAt = (data.data || []).find(item => item.health_checked_at)?.health_checked_at;
                status.textContent = checkedAt
                    ? `${t('agents_refresh_mcp_updated')} ${checkedAt}`
                    : t('agents_refresh_mcp_updated');
            }
        })
        .then(() => loadMcpView(true))
        .catch(error => {
            const empty = document.getElementById('mcp-empty');
            empty.textContent = String(error.message || error);
            empty.classList.remove('hidden');
            if (status) status.textContent = `${t('agents_refresh_mcp_failed')}: ${String(error.message || error)}`;
        })
        .finally(() => {
            if (button) {
                button.disabled = false;
                button.setAttribute('aria-busy', 'false');
            }
        });
}

async function installMcp() {
    const nameInput = document.getElementById('mcp-install-name');
    const commandInput = document.getElementById('mcp-install-command');
    const button = document.getElementById('mcp-install-button');
    const status = document.getElementById('mcp-status');
    const name = String(nameInput.value || '').trim();
    const command = String(commandInput.value || '').trim();
    if (!name || !command) return;
    status.textContent = t('mcp_installing');
    status.classList.remove('opacity-0');
    button.disabled = true;
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/mcp-servers`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({name, command}),
        });
        const data = await responseJson(response);
        if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Install failed');
        nameInput.value = '';
        commandInput.value = '';
        const installed = data.data || {};
        if (String(installed.status || '').toLowerCase() === 'preparing') {
            status.textContent = t('mcp_install_preparing');
        } else if (installed.status && installed.status !== 'healthy') {
            status.textContent = `${t('mcp_install_unhealthy')} ${String(installed.detail || installed.status)}`;
        } else {
            status.textContent = t('mcp_installed');
        }
        mcpLoaded = false; pluginsLoaded = false; toolsLoaded = false; invalidateAgentCatalog();
        mcpSettlingUntil = Date.now() + 90000;
        await loadMcpView(true);
    } catch (error) {
        status.textContent = String(error.message || error);
    } finally {
        button.disabled = false;
    }
}

async function uninstallMcp(serverId) {
    if (!window.confirm(t('mcp_uninstall_confirm'))) return;
    const response = await fetch(
        `${PUBLIC_API_BASE}/mcp-servers?id=${encodeURIComponent(serverId)}`,
        {method: 'DELETE'},
    );
    if (response.status !== 204) {
        await explainRefusedDelete(response, t('mcp_uninstall_failed'));
        return;
    }
    mcpLoaded = false; pluginsLoaded = false; toolsLoaded = false; invalidateAgentCatalog();
    mcpSettlingUntil = Date.now() + 90000;
    await loadMcpView(true);
}

function loadToolsSection() {
    if (toolsLoaded) return;
    const emptyEl = document.getElementById('tools-empty');
    const listEl = document.getElementById('tools-list');
    const badge = document.getElementById('tools-count-badge');

    fetch(`${PUBLIC_API_BASE}/capabilities/tool`).then(responseJson).then(data => {
        if (data.status !== 'success') throw new Error(data.message || (currentLang === 'zh' ? '工具 Worker 不可用' : 'Tool Worker unavailable'));
        const tools = data.data || [];
        const toolStatuses = tools.map(tool => ({
            ...tool,
            available: tool.enabled !== false,
            reason: tool.unavailable_reason || '',
        }));
        emptyEl.classList.add('hidden');
        if (toolStatuses.length === 0) {
            emptyEl.classList.remove('hidden');
            emptyEl.innerHTML = `<span class="text-sm text-slate-400 dark:text-slate-500">${currentLang === 'zh' ? '暂无内置工具' : 'No built-in tools'}</span>`;
            return;
        }
        const availableCount = toolStatuses.filter(item => item.available).length;
        badge.textContent = `${availableCount}/${toolStatuses.length}`;
        badge.classList.remove('hidden');
        listEl.innerHTML = '';
        toolStatuses.forEach(tool => {
            const card = document.createElement('div');
            const toolId = tool.id || tool.name;
            const record = tools.find(item => (item.id || item.name) === toolId || item.name === tool.name) || tool;
            card.dataset.toolId = toolId;
            card.className = `bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-4 flex items-start gap-3 ${tool.available ? '' : 'opacity-60'}`;
            const statusLabel = tool.available
                ? (currentLang === 'zh' ? '可用' : 'Available')
                : (currentLang === 'zh' ? '未启用' : 'Unavailable');
            const statusClass = tool.available
                ? 'text-emerald-600 bg-emerald-50 dark:text-emerald-400 dark:bg-emerald-900/20'
                : 'text-amber-600 bg-amber-50 dark:text-amber-400 dark:bg-amber-900/20';
            // Runtime-owned tools ship a short intro per language; everything
            // else keeps its own description whatever language it is written
            // in. The card clamps it, the title attribute is the hover detail.
            const summaries = tool.summaries && typeof tool.summaries === 'object' ? tool.summaries : {};
            const summary = typeof summaries[currentLang] === 'string' ? summaries[currentLang].trim() : '';
            const detail = summary
                || record.description
                || tool.reason
                || (currentLang === 'zh' ? '依赖或运行环境未满足' : 'Dependency or runtime requirement is not met');
            card.innerHTML = `
                <div class="w-9 h-9 rounded-lg bg-blue-50 dark:bg-blue-900/20 flex items-center justify-center flex-shrink-0">
                    <i class="fas ${getToolIcon(tool.name)} text-blue-500 dark:text-blue-400 text-sm"></i>
                </div>
                <div class="flex-1 min-w-0">
                    <div class="flex items-center gap-2">
                        <span class="font-medium text-sm text-slate-700 dark:text-slate-200">${escapeHtml(record.display_name || tool.name)}</span>
                        <span class="text-[10px] px-1.5 py-0.5 rounded ${statusClass}">${statusLabel}</span>
                        ${record.origin === 'mcp' ? `<span class="tool-source-badge text-[10px] px-1.5 py-0.5 rounded bg-violet-50 text-violet-600 dark:bg-violet-900/20 dark:text-violet-300">MCP · ${escapeHtml(record.plugin_id || '')}</span>` : ''}
                    </div>
                    <p class="text-xs text-slate-400 dark:text-slate-500 mt-1 line-clamp-2" title="${escapeAttribute(detail)}">${escapeHtml(detail)}</p>
                </div>`;
            listEl.appendChild(card);
        });
        listEl.classList.remove('hidden');
        toolsLoaded = true;
    }).catch(error => {
        emptyEl.classList.remove('hidden');
        emptyEl.innerHTML = `<span class="text-sm text-amber-600 dark:text-amber-400">${escapeHtml(String(error.message || error || (currentLang === 'zh' ? '加载失败' : 'Failed to load')))}</span>`;
        listEl.classList.add('hidden');
    });
}

let pluginAdminState = [];
// /mcp-servers 的快照：插件页用它给插件自带的 MCP 打实时状态。
let pluginMcpState = [];

function renderPluginAdminView() {
    const list = document.getElementById('plugin-admin-list');
    const empty = document.getElementById('plugins-empty');
    if (!list) return;
    list.replaceChildren();
    if (empty) {
        empty.textContent = currentLang === 'zh' ? '暂无插件' : 'No plugins installed';
        empty.classList.toggle('hidden', pluginAdminState.length > 0);
    }
    pluginAdminState.forEach(plugin => {
        const pluginId = String(plugin.id || '');
        const card = document.createElement('article');
        card.className = 'plugin-admin-card rounded-xl border border-slate-200 dark:border-white/10 bg-white dark:bg-[#1A1A1A] p-4';
        const name = document.createElement('h3');
        name.className = 'font-semibold text-slate-800 dark:text-slate-100';
        name.textContent = pluginId;
        const members = document.createElement('p');
        members.className = 'text-xs text-slate-500 dark:text-slate-400 mt-1';
        const memberRows = Array.isArray(plugin.members) ? plugin.members : [];
        // MCP 成员单独一段：它们的「状态」来自 /mcp-servers 的实时快照，而其它
        // 成员只是一行清单，混在一起会让人以为 MCP 那几行也是静态描述。
        const mcpRows = pluginMcpRows(pluginId, memberRows);
        members.textContent = memberRows
            .filter(member => typeof member === 'string'
                || String(member.kind || '').toLowerCase() !== 'mcp')
            .map(member => {
                if (typeof member === 'string') return member;
                const kind = String(member.kind || '').toLowerCase();
                const labels = {hook: 'Hook', skill: 'Skill', tool: 'Tool'};
                const name = String(member.name || member.id || '');
                return `${labels[kind] || kind || 'Member'} · ${name}`;
            }).filter(Boolean).join(', ');
        if (mcpRows.length) members.textContent = [members.textContent, t('plugin_members_mcp_moved')].filter(Boolean).join(' · ');
        const actions = document.createElement('div');
        actions.className = 'flex gap-2 mt-3';
        const inspect = document.createElement('button');
        inspect.type = 'button';
        inspect.className = 'plugin-admin-inspect text-xs text-primary-500';
        inspect.textContent = 'Inspect';
        inspect.addEventListener('click', () => inspectPluginAdmin(pluginId));
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'plugin-admin-delete text-xs text-red-500';
        remove.textContent = 'Delete';
        remove.addEventListener('click', () => deletePluginAdmin(pluginId));
        actions.append(inspect, remove);
        card.append(name, members);
        if (mcpRows.length) {
            const block = document.createElement('div');
            block.className = 'plugin-admin-mcp mt-2';
            block.innerHTML = `<p class="text-xs font-medium text-slate-500 dark:text-slate-400">${escapeHtml(t('plugin_mcp_title'))}</p>`
                + `<div class="mt-1 flex flex-col gap-1">${mcpRows.map(row => `
                    <div class="flex items-center gap-2 flex-wrap">
                        <span class="font-mono text-xs text-slate-500 dark:text-slate-400 break-all">${escapeHtml(row.id)}</span>
                        <span class="text-[10px] px-1.5 py-0.5 rounded ${mcpStatusBadgeClass(row.server)}" title="${escapeAttribute(mcpStateDetail(row.server))}">${escapeHtml(row.server ? mcpStateLabel(row.server) : t('mcp_state_unknown'))}</span>
                        <span class="text-[10px] text-slate-400">${escapeHtml(row.tools === null ? t('plugin_mcp_tools_unknown') : `${row.tools} · ${t('plugin_mcp_tools')}`)}</span>
                    </div>`).join('')}</div>`;
            card.append(block);
        }
        card.append(actions);
        list.appendChild(card);
    });
}

// 插件的 MCP 成员在 /mcp-servers 里有实时状态；拿不到那一份快照时退回成员清单，
// 状态标 unknown 而不是假装健康。
function pluginMcpRows(pluginId, memberRows) {
    const live = pluginMcpState.filter(server => String(server.owner_plugin_id || '') === pluginId);
    if (live.length) {
        // 整条 server 一起带走：状态词、准备阶段和原因都在同一套生命周期函数里，
        // 插件页和 MCP 页才不会对同一台 Server 各说一句话。
        return live.map(server => ({
            id: String(server.id || ''),
            server,
            state: mcpServerState(server),
            tools: Array.isArray(server.tools) ? server.tools.length : 0,
        })).sort((left, right) => left.id.localeCompare(right.id));
    }
    return memberRows
        .filter(member => typeof member !== 'string' && String(member.kind || '').toLowerCase() === 'mcp')
        .map(member => ({ id: String(member.id || member.name || ''), server: null, state: 'unknown', tools: null }));
}

async function loadPluginsAdminView() {
    const status = document.getElementById('plugin-admin-status');
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/plugins`);
        const payload = await responseJson(response);
        if (!response.ok || payload.status !== 'success') {
            throw new Error(payload.message || 'Failed to load Plugins');
        }
        pluginAdminState = Array.isArray(payload.data) ? payload.data : [];
        // 插件自带的 MCP 的状态要在这里显示，所以顺带取一份 MCP 快照。取不到
        // 只是状态退化成 unknown，不该让整个插件页失败。
        try {
            const mcpResponse = await fetch(`${PUBLIC_API_BASE}/mcp-servers`);
            const mcpPayload = await responseJson(mcpResponse);
            pluginMcpState = mcpResponse.ok && mcpPayload.status === 'success' && Array.isArray(mcpPayload.data)
                ? mcpPayload.data
                : [];
        } catch (_error) {
            pluginMcpState = [];
        }
        if (status) status.textContent = '';
        renderPluginAdminView();
    } catch (error) {
        if (status) status.textContent = String(error.message || error);
    }
}

async function inspectPluginAdmin(pluginId) {
    const details = document.getElementById('plugin-admin-details');
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/plugins/${encodeURIComponent(pluginId)}`);
        const payload = await responseJson(response);
        if (!response.ok || payload.status !== 'success') throw new Error(payload.message);
        details.textContent = JSON.stringify(payload.data, null, 2);
        details.classList.remove('hidden');
    } catch (error) {
        details.textContent = String(error.message || error);
        details.classList.remove('hidden');
    }
}

async function installPluginAdmin() {
    const sourceInput = document.getElementById('plugin-install-source');
    const source = String(sourceInput?.value || '').trim();
    if (!source) return;
    const status = document.getElementById('plugin-admin-status');
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/plugins`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source }),
        });
        const payload = await responseJson(response);
        if (!response.ok || payload.status !== 'success') throw new Error(payload.message || (currentLang === 'zh' ? '插件安装失败' : 'Plugin install failed'));
        sourceInput.value = '';
        if (status) status.textContent = '';
        await loadPluginsAdminView();
    } catch (error) {
        if (status) status.textContent = String(error.message || error);
    }
}

async function deletePluginAdmin(pluginId) {
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/plugins/${encodeURIComponent(pluginId)}`, {
            method: 'DELETE',
        });
        if (response.status !== 204) {
            await explainRefusedDelete(response, t('plugin_delete_failed'));
            return false;
        }
        pluginAdminState = pluginAdminState.filter(plugin => plugin.id !== pluginId);
        renderPluginAdminView();
        return true;
    } catch (error) {
        showConfirmDialog({
            title: t('delete_failed_title'),
            message: String(error.message || error),
            okText: t('confirm_got_it'),
            hideCancel: true,
        });
        return false;
    }
}

document.getElementById('plugin-install-btn')?.addEventListener('click', installPluginAdmin);

function loadSkillsSection() {
    const emptyEl = document.getElementById('skills-empty');
    const listEl = document.getElementById('skills-list');
    const badge = document.getElementById('skills-count-badge');

    fetch(`${PUBLIC_API_BASE}/capabilities/skill`).then(responseJson).then(data => {
        if (data.status !== 'success') throw new Error(data.message || (currentLang === 'zh' ? '技能 Worker 不可用' : 'Skill Worker unavailable'));
        const skills = (data.data || []).filter(skill => !skill.plugin_id);
        if (skills.length === 0) {
            const p = emptyEl.querySelector('p');
            if (p) p.textContent = currentLang === 'zh' ? '暂无技能' : 'No skills found';
            return;
        }
        badge.textContent = skills.length;
        badge.classList.remove('hidden');
        emptyEl.classList.add('hidden');
        listEl.innerHTML = '';

        skills.forEach(sk => {
            const card = document.createElement('div');
            card.className = 'bg-white dark:bg-[#1A1A1A] rounded-xl border border-slate-200 dark:border-white/10 p-4 flex items-start gap-3 transition-opacity';
            card.dataset.skillName = sk.name;
            card.dataset.skillDisplayName = localizedCapabilityName(sk);
            card.dataset.skillDesc = localizedCapabilityDescription(sk);
            renderSkillCard(card, sk);
            listEl.appendChild(card);
        });
    }).catch(error => {
        const message = String(error.message || error);
        const p = emptyEl.querySelector('p');
        if (p) p.textContent = message;
        else emptyEl.textContent = message;
        emptyEl.classList.remove('hidden');
        listEl.classList.add('hidden');
    });
}

function renderSkillCard(card, sk) {
    card.innerHTML = `
        <div class="w-9 h-9 rounded-lg bg-amber-50 dark:bg-amber-900/20 flex items-center justify-center flex-shrink-0">
            <i class="fas fa-bolt text-amber-400 text-sm"></i>
        </div>
        <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
                <span class="skill-card-name font-medium text-sm text-slate-700 dark:text-slate-200 truncate flex-1"></span>
            </div>
            <p class="skill-card-description text-xs text-slate-400 dark:text-slate-500 line-clamp-2"></p>
        </div>`;
    card.querySelector('.skill-card-name').textContent = localizedCapabilityName(sk);
    card.querySelector('.skill-card-description').textContent =
        localizedCapabilityDescription(sk) || '--';
}

// =====================================================================
// Custom Confirm Dialog
// =====================================================================
function showConfirmDialog({ title, message, okText, cancelText, onConfirm, hideCancel }) {
    const overlay = document.getElementById('confirm-dialog-overlay');
    document.getElementById('confirm-dialog-title').textContent = title || '';
    document.getElementById('confirm-dialog-message').textContent = message || '';
    document.getElementById('confirm-dialog-ok').textContent = okText || 'OK';
    const cancelBtn = document.getElementById('confirm-dialog-cancel');
    cancelBtn.textContent = cancelText || t('cancel');
    cancelBtn.classList.toggle('hidden', !!hideCancel);

    function cleanup() {
        overlay.classList.add('hidden');
        okBtn.removeEventListener('click', onOk);
        cancelBtn.removeEventListener('click', onCancel);
        overlay.removeEventListener('click', onOverlayClick);
    }
    function onOk() { cleanup(); if (onConfirm) onConfirm(); }
    function onCancel() { cleanup(); }
    function onOverlayClick(e) { if (e.target === overlay) cleanup(); }

    const okBtn = document.getElementById('confirm-dialog-ok');
    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    overlay.addEventListener('click', onOverlayClick);
    overlay.classList.remove('hidden');
}

// =====================================================================
// Agents View
// =====================================================================
let agentsLoaded = false;
let agentsState = [];
let agentsStateRevision = 0;
let agentsListRequest = 0;
let agentCatalog = null;
let agentCatalogPromise = null;
let agentCatalogEpoch = 0;
let editingAgentId = '';
let agentEditorEpoch = 0;
let agentEditorSnapshot = null;
let agentEditorProvidersReady = false;
let agentEditorSaving = false;
let pendingAgentAvatarFile = null;
let pendingAgentAvatarUrl = '';
let removeAgentAvatar = false;
const AGENT_AVATAR_PRESETS = [
    '🤖', '✨', '🧠', '🔎', '🧑‍💻', '🎨',
    '📚', '🧭', '🛠️', '📊', '🎯', '🔮',
];
function isUploadedAgentAvatar(avatar) {
    return /^\/api\/v1\/agents\/[^/]+\/avatar$/i.test(String(avatar || ''));
}

function agentAvatarMarkup(avatar) {
    const value = String(avatar || '🤖');
    if (isUploadedAgentAvatar(value)) {
        return `<img src="${escapeAttribute(value)}" alt="">`;
    }
    return `<span>${escapeHtml(value)}</span>`;
}

function clearPendingAgentAvatar() {
    if (pendingAgentAvatarUrl) URL.revokeObjectURL(pendingAgentAvatarUrl);
    pendingAgentAvatarFile = null;
    pendingAgentAvatarUrl = '';
}

function selectAgentAvatar(avatar, userSelected = true) {
    const value = String(avatar || '🤖');
    if (userSelected) {
        clearPendingAgentAvatar();
        removeAgentAvatar = !!editingAgentId && isUploadedAgentAvatar(
            agentsState.find(item => item.id === editingAgentId)?.avatar
        );
    }
    document.getElementById('agent-form-avatar').value = value;
    document.getElementById('agent-avatar-preview').innerHTML = agentAvatarMarkup(value);
    document.querySelectorAll('#agent-avatar-presets button').forEach(button => {
        button.classList.toggle('active', button.dataset.avatar === value);
    });
}

function renderAgentAvatarPicker(avatar) {
    const presets = document.getElementById('agent-avatar-presets');
    presets.innerHTML = AGENT_AVATAR_PRESETS.map(value => `
        <button type="button" data-avatar="${escapeAttribute(value)}"
                aria-label="${escapeAttribute(value)}"
                onclick="selectAgentAvatar(this.dataset.avatar)">
            ${escapeHtml(value)}
        </button>
    `).join('');
    selectAgentAvatar(avatar || '🤖', false);
}

async function uploadAgentAvatar(input) {
    const file = input.files?.[0];
    if (!file) return;
    const status = document.getElementById('agent-editor-status');
    if (file.size > 2 * 1024 * 1024) {
        status.textContent = t('agents_avatar_hint');
        status.className = 'agent-editor-status error';
        input.value = '';
        return;
    }
    if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type)) {
        status.textContent = t('agents_avatar_hint');
        status.className = 'agent-editor-status error';
        input.value = '';
        return;
    }
    clearPendingAgentAvatar();
    pendingAgentAvatarFile = file;
    pendingAgentAvatarUrl = URL.createObjectURL(file);
    removeAgentAvatar = false;
    const preview = document.getElementById('agent-avatar-preview');
    const image = document.createElement('img');
    image.src = pendingAgentAvatarUrl;
    image.alt = '';
    preview.replaceChildren(image);
    document.querySelectorAll('#agent-avatar-presets button').forEach(button => {
        button.classList.remove('active');
    });
    status.textContent = '';
    input.value = '';
}

async function refreshAgentList(force = false) {
    if (agentsLoaded && !force) return agentsState;
    const revision = agentsStateRevision;
    const requestId = ++agentsListRequest;
    const response = await fetch(`${PUBLIC_API_BASE}/agents`);
    const data = await responseJson(response);
    if (data.status !== 'success') {
        throw new Error(data.message || 'Failed to load agents');
    }
    if (revision !== agentsStateRevision || requestId !== agentsListRequest) {
        // A newer refresh owns the final state. It renders the view below, so
        // the superseded caller must not clear the grid with its stale copy.
        return agentsState;
    }
    agentsState = data.data || [];
    agentsLoaded = true;
    // A concurrent refresh can supersede the call that a view load awaits, so
    // whoever ends up owning the state is the one that has to paint it.
    if (currentView === 'agents') renderAgentsView();
    return agentsState;
}

async function loadAgentsView(force = false) {
    if (agentsLoaded && !force) {
        renderAgentsView();
        if (!agentCatalog) ensureAgentCatalog().catch(() => {});
        return;
    }
    const loading = document.getElementById('agents-loading');
    const empty = document.getElementById('agents-empty');
    const grid = document.getElementById('agents-grid');
    loading.classList.remove('hidden');
    empty.classList.add('hidden');
    grid.classList.add('hidden');
    try {
        await refreshAgentList(force);
        renderAgentsView();
        ensureAgentCatalog().catch(() => {});
    } catch (error) {
        loading.innerHTML = `<i class="fas fa-circle-exclamation text-red-400"></i><span>${escapeHtml(error.message)}</span>`;
    }
}

function positionChatAgentMenu() {
    const menu = document.getElementById('chat-agent-menu');
    if (!menu) return;
    const isMention = agentChooserMode === 'mention';
    const anchor = isMention ? chatInput.parentElement : document.querySelector('.session-new-chat-control');
    menu.classList.toggle('chat-agent-menu-mention', isMention);
    if (anchor && menu.parentElement !== anchor) anchor.appendChild(menu);
}

async function toggleChatAgentMenu(event) {
    if (event) event.stopPropagation();
    const menu = document.getElementById('chat-agent-menu');
    if (!menu) return;
    const willOpen = menu.classList.contains('hidden');
    if (!willOpen) {
        closeChatAgentMenu();
        return;
    }
    positionChatAgentMenu();
    menu.classList.remove('hidden');
    document.getElementById('chat-agent-menu-list').innerHTML =
        '<div class="chat-agent-menu-loading"><i class="fas fa-spinner fa-spin"></i></div>';
    try {
        await refreshAgentList();
        renderChatAgentMenu();
    } catch (error) {
        document.getElementById('chat-agent-menu-list').innerHTML =
            `<div class="chat-agent-menu-error">${escapeHtml(error.message)}</div>`;
    }
}

function closeChatAgentMenu() {
    document.getElementById('chat-agent-menu')?.classList.add('hidden');
    agentMentionRange = null;
    agentMentionActiveIndex = 0;
}

function highlightMentionedAgent(scroll = false) {
    const options = Array.from(document.querySelectorAll('#chat-agent-menu .chat-agent-option:not(:disabled)'));
    agentMentionActiveIndex = Math.max(0, Math.min(agentMentionActiveIndex, options.length - 1));
    options.forEach((option, index) => {
        option.classList.toggle('keyboard-active', index === agentMentionActiveIndex);
    });
    const selected = options[agentMentionActiveIndex];
    if (scroll) selected?.scrollIntoView({block: 'nearest'});
    return selected;
}

function renderChatAgentMenu() {
    positionChatAgentMenu();
    const list = document.getElementById('chat-agent-menu-list');
    if (!list || !agentsLoaded) return;
    const isMention = agentChooserMode === 'mention';
    const isNewChat = agentChooserMode === 'new-chat' || isMention;
    const options = activeAgent?.available === false
        ? [activeAgent, ...agentsState.filter(agent => agent.id !== activeAgent.id)]
        : agentsState;
    list.replaceChildren();
    options.forEach(agent => {
        if (isMention && agentMentionRange?.query && ![agent.name, agent.name_zh, agent.name_en, agent.id]
            .some(value => String(value || '').toLowerCase().includes(agentMentionRange.query.toLowerCase()))) return;
        const selected = activeAgent?.id === agent.id;
        const disabled = !agentCanStartSession(agent) || (!isNewChat && selected);
        const sessions = Array.from(sessionsById.values())
            .filter(item => item.agent_id === agent.id)
            .slice(0, 3);
        const group = document.createElement('div');
        group.className = 'chat-agent-option-group';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `chat-agent-option${selected ? ' active' : ''}`;
        button.disabled = !!disabled;
        if (!disabled) {
            button.addEventListener('click', () => isMention ? selectMentionedAgent(agent.id) : createSessionForAgent(agent.id));
        }
        const avatar = document.createElement('span');
        avatar.className = 'chat-agent-option-avatar';
        appendAgentAvatar(avatar, agent.avatar);
        const copy = document.createElement('span');
        copy.className = 'chat-agent-option-copy';
        const name = document.createElement('strong');
        name.textContent = agentDisplayName(agent);
        const detail = document.createElement('small');
        detail.textContent = agent.available === false
            ? t('agents_unavailable')
            : (isNewChat
                ? t('agents_new_session')
                : (selected ? t('agents_current_locked') : t('agents_new_session')));
        copy.append(name, detail);
        const action = document.createElement('i');
        action.className = `fas ${!isNewChat && selected ? 'fa-lock' : 'fa-plus'} chat-agent-option-action`;
        button.append(avatar, copy, action);
        group.appendChild(button);
        if (!isNewChat) {
            sessions.forEach(session => {
                const link = document.createElement('button');
                link.type = 'button';
                link.className = 'chat-agent-session-link';
                const icon = document.createElement('i');
                icon.className = 'fas fa-message';
                link.append(icon, document.createTextNode(String(session.title || t('untitled_session'))));
                link.addEventListener('click', () => {
                    closeChatAgentMenu();
                    switchSession(session.session_id);
                });
                group.appendChild(link);
            });
        }
        list.appendChild(group);
    });
    if (isMention) highlightMentionedAgent();
    if (!isNewChat) {
        const hint = document.createElement('p');
        hint.className = 'chat-agent-switch-hint';
        hint.textContent = t('agents_switch_hint');
        list.appendChild(hint);
    }
}

function selectChatAgent(agentId) {
    closeChatAgentMenu();
    if (activeAgent?.id === agentId) return;
    const agent = agentsState.find(item => item.id === agentId);
    if (!agent) return;
    createSessionForAgent(agent.id);
}

function selectMentionedAgent(agentId) {
    const agent = agentForId(agentId);
    if (!agentCanStartSession(agent) || !agentMentionRange) return;
    const {start, end} = agentMentionRange;
    const draft = chatInput.value.slice(0, start) + chatInput.value.slice(end);
    const attachments = [...pendingAttachments];
    const skills = new Map(selectedSkills);
    closeChatAgentMenu();
    if (activeAgent?.id !== agent.id) {
        enterNewChatDraft(agent);
        pendingAttachments = attachments;
        skills.forEach((value, key) => selectedSkills.set(key, value));
        renderAttachmentPreview();
        renderSelectedSkills();
    }
    chatInput.value = draft;
    chatInput.setSelectionRange(start, start);
    chatInput.dispatchEvent(new Event('input'));
    chatInput.focus();
}

function agentForId(agentId) {
    return agentsState.find(item => item.id === agentId) || {
        id: String(agentId || ''), name: String(agentId || t('agents_default')),
        avatar: '🤖', available: false, unavailable_reason: 'agent_not_found'
    };
}

async function restoreSessionBinding(targetSessionId) {
    const ownerSession = String(targetSessionId || sessionId);
    try {
        const [response] = await Promise.all([
            fetch(`${PUBLIC_API_BASE}/sessions/${encodeURIComponent(ownerSession)}`),
            refreshAgentList(),
        ]);
        if (response.status === 404) {
            if (ownerSession === sessionId) {
                activeAgent = null;
                newChatDraft = null;
                renderActiveAgent();
                await startNewChat({
                    activate: false,
                    discardStoredSession: true,
                    discardedSessionId: ownerSession,
                });
            }
            return null;
        }
        const data = await responseJson(response);
        if (!response.ok || data.status !== 'success') return null;
        const record = { ...data.data, session_id: data.data.id };
        sessionsById.set(record.session_id, record);
        if (ownerSession === sessionId) {
            activeAgent = agentForId(record.agent_id);
            renderActiveAgent();
        }
        return record;
    } catch (_) {
        return null;
    }
}

async function findSessionInCatalog(targetSessionId) {
    const ownerSession = String(targetSessionId || '');
    if (!ownerSession) return null;
    let page = 1;
    while (true) {
        try {
            const response = await fetch(
                `${PUBLIC_API_BASE}/sessions?page=${page}&page_size=100`,
                {cache: 'no-store'},
            );
            if (!response.ok) return undefined;
            const data = await responseJson(response);
            if (data.status !== 'success') return undefined;
            const record = (data.data || []).find(item => item.id === ownerSession);
            if (record) return {...record, session_id: record.id};
            if (!data.page?.has_more) return null;
            page += 1;
        } catch (_) {
            return undefined;
        }
    }
}

async function restorePersistedSession(targetSessionId) {
    const [record] = await Promise.all([
        findSessionInCatalog(targetSessionId),
        refreshAgentList(),
    ]);
    // Startup restoration may finish after the user has selected an expert or
    // opened another conversation. Never replace that newer foreground state.
    if (isNewChatDraft() || sessionId !== targetSessionId) return null;
    // If the catalog itself is unavailable, retain the previous direct lookup
    // behavior instead of treating a transient failure as a deleted session.
    if (record === undefined) return restoreSessionBinding(targetSessionId);
    if (record === null) {
        // Restoring a session that no longer exists installs a default new
        // chat in the background: never yank the user away from where they are.
        await startNewChat({
            activate: false,
            discardStoredSession: true,
            discardedSessionId: targetSessionId,
        });
        return null;
    }
    sessionsById.set(record.session_id, record);
    if (record.session_id === sessionId) {
        activeAgent = agentForId(record.agent_id);
        renderActiveAgent();
    }
    return record;
}

async function recoverSessionAgentMismatch(loadingEl) {
    if (loadingEl?.remove) loadingEl.remove();
    messagesDiv.innerHTML = '';
    await restoreSessionBinding(sessionId);
    await openSessionHistory(sessionId, true);
    resetActiveRunControls();
}

async function startNewChatFromButton(event) {
    event?.stopPropagation();
    await startNewChat();
}

function openAgentSessionNavigator(event) {
    agentChooserMode = 'navigate';
    toggleChatAgentMenu(event);
}

// Inserting "@" is the Expert switch: it opens the mention picker inline and
// replaces the Expert of the current draft once the user picks a name.
function startExpertMention() {
    if (!agentsLoaded || !agentsState.length) return;
    const value = chatInput.value;
    const start = chatInput.selectionStart ?? value.length;
    const end = chatInput.selectionEnd ?? start;
    const before = value.slice(0, start);
    const inserted = before.length > 0 && !/\s$/.test(before) ? ' @' : '@';
    chatInput.value = before + inserted + value.slice(end);
    const caret = start + inserted.length;
    chatInput.setSelectionRange(caret, caret);
    chatInput.dispatchEvent(new Event('input'));
    chatInput.focus();
}

function createSessionForAgent(agentId) {
    closeChatAgentMenu();
    const agent = agentForId(agentId);
    if (!agentCanStartSession(agent)) return;
    enterNewChatDraft(agent);
}

document.addEventListener('click', event => {
    const menu = document.getElementById('chat-agent-menu');
    const toggle = document.getElementById('chat-agent-toggle');
    if (
        menu
        && !menu.classList.contains('hidden')
        && !menu.contains(event.target)
        && !toggle?.contains(event.target)
        && !event.target.closest?.('#active-agent-bar')
        && event.target !== chatInput
    ) {
        closeChatAgentMenu();
    }
});

function renderAgentsView() {
    const loading = document.getElementById('agents-loading');
    const empty = document.getElementById('agents-empty');
    const grid = document.getElementById('agents-grid');
    renderChatAgentMenu();
    loading.classList.add('hidden');
    if (!agentsState.length) {
        empty.classList.remove('hidden');
        grid.classList.add('hidden');
        grid.innerHTML = '';
        return;
    }
    empty.classList.add('hidden');
    grid.classList.remove('hidden');
    grid.replaceChildren();
    agentsState.forEach(agent => {
        const capabilityCount = (agent.tools || []).length + (agent.skills || []).length
            + (agent.plugins || []).length + (agent.mcps || []).length;
        const description = agentDescription(agent);
        const card = document.createElement('article');
        card.className = 'agent-card';
        card.dataset.agentId = agent.id;
        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        card.addEventListener('click', () => editAgent(agent.id));
        card.addEventListener('keydown', event => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                editAgent(agent.id);
            }
        });
        const top = document.createElement('div');
        top.className = 'agent-card-top';
        const avatar = document.createElement('div');
        avatar.className = 'agent-card-avatar';
        appendAgentAvatar(avatar, agent.avatar);
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'agent-card-delete';
        remove.title = t('agents_delete_title');
        const removeIcon = document.createElement('i');
        removeIcon.className = 'fas fa-trash';
        remove.appendChild(removeIcon);
        remove.addEventListener('keydown', event => event.stopPropagation());
        remove.addEventListener('click', event => {
            event.stopPropagation();
            deleteAgent(agent.id);
        });
        top.append(avatar, remove);
        const name = document.createElement('h3');
        name.textContent = agentDisplayName(agent);
        const descriptionEl = document.createElement('p');
        descriptionEl.textContent = String(description || '');
        const meta = document.createElement('div');
        meta.className = 'agent-card-meta';
        if (agent.origin === 'system') {
            const source = document.createElement('span');
            source.textContent = currentLang === 'en' ? 'System' : '系统专家';
            meta.appendChild(source);
        }
        const model = document.createElement('span');
        const modelIcon = document.createElement('i');
        modelIcon.className = 'fas fa-microchip';
        model.append(modelIcon, document.createTextNode(agentModelLabel(agent)));
        const capabilities = document.createElement('span');
        const capabilitiesIcon = document.createElement('i');
        capabilitiesIcon.className = 'fas fa-wand-magic-sparkles';
        capabilities.append(capabilitiesIcon, document.createTextNode(String(capabilityCount)));
        meta.append(model, capabilities);
        const use = document.createElement('button');
        use.type = 'button';
        use.className = 'agent-card-use';
        use.disabled = !agentCanStartSession(agent);
        const useIcon = document.createElement('i');
        useIcon.className = 'fas fa-message';
        use.append(useIcon, document.createTextNode(t('agents_use')));
        use.addEventListener('click', event => useAgent(event, agent.id));
        card.append(top, name, descriptionEl, meta, use);
        grid.appendChild(card);
    });
}

function agentModelLabel(agent) {
    if (!agent.model?.provider || !agent.model?.name) return t('agents_unavailable');
    const provider = agentCatalog?.providers?.find(item => item.id === agent.model.provider);
    return `${provider?.name || agent.model.provider} · ${agent.model.name}`;
}

async function ensureAgentCatalog() {
    while (!agentCatalog) {
        if (!agentCatalogPromise) {
            const epoch = agentCatalogEpoch;
            let catalogPromise;
            catalogPromise = Promise.allSettled([
                fetch(`${PUBLIC_API_BASE}/capabilities/tool`).then(responseJson),
                fetch(`${PUBLIC_API_BASE}/capabilities/skill`).then(responseJson),
                fetch(`${PUBLIC_API_BASE}/mcp-servers`).then(responseJson),
                fetch(`${PUBLIC_API_BASE}/providers`).then(responseJson),
            ]).then(([toolsResult, skillsResult, mcpResult, providersResult]) => {
                if (agentCatalogEpoch !== epoch) return null;
                const providerResponse = providersResult.status === 'fulfilled'
                    ? providersResult.value : null;
                if (!providerResponse || providerResponse.status !== 'success') {
                    throw new Error(providerResponse?.message || (currentLang === 'zh'
                        ? '服务商目录不可用' : 'Provider catalog unavailable'));
                }
                const catalogData = result => result.status === 'fulfilled'
                    && result.value?.status === 'success' ? result.value.data || [] : [];
                const catalog = {
                    tools: catalogData(toolsResult),
                    skills: catalogData(skillsResult),
                    mcps: catalogData(mcpResult),
                    providers: (providerResponse.data || []).filter(provider => provider.configured).map(
                        provider => ({ ...provider, name: provider.label })
                    ),
                };
                agentCatalog = catalog;
                return catalog;
            }).finally(() => {
                if (agentCatalogPromise === catalogPromise) {
                    agentCatalogPromise = null;
                }
            });
            agentCatalogPromise = catalogPromise;
        }
        const catalog = await agentCatalogPromise;
        if (catalog && catalog === agentCatalog) return catalog;
    }
    return agentCatalog;
}

function invalidateAgentCatalog() {
    agentCatalogEpoch += 1;
    agentCatalog = null;
    agentCatalogPromise = null;
}

function selectedAgentCapabilityIds(type) {
    const target = document.getElementById(`agent-form-${type}`);
    if (target.dataset.pending === 'true') return JSON.parse(target.dataset.selection || '[]');
    const inputs = [...target.querySelectorAll('input[type="checkbox"]')];
    if (target.dataset.wildcard === 'true' && inputs.every(input => input.checked)) {
        return ['*'];
    }
    return inputs.filter(input => input.checked).map(input => input.value);
}

function newAgentCapabilitySelection(type, items) {
    // Preset capabilities come from the Runtime configuration and are forced
    // onto every Agent; the editor only mirrors them as locked entries.
    return items
        .filter(item => item.preset === true && item.enabled !== false)
        .map(item => String(item.name || item.id || ''));
}

// The editor shows one panel per capability type. The fetched catalog is kept
// here so a language switch can redraw the panels with localized Skill / Tool
// names from the same data instead of re-fetching every catalog, and so the
// boxes the user has already ticked survive the redraw.
const agentCapabilityCatalog = {tools: null, skills: null, plugins: null, mcps: null};

// Redraw every capability panel from the cached catalog. Called on a language
// switch while the editor is open: names and descriptions follow the console
// language, and the current tick state is read back from the DOM first.
function renderAgentEditorCapabilityPanels() {
    ['tools', 'skills', 'plugins', 'mcps'].forEach(type => {
        const cached = agentCapabilityCatalog[type];
        if (!cached) return;
        const selected = cached.pending ? cached.selected : selectedAgentCapabilityIds(type);
        cached.selected = selected;
        renderAgentCapabilityOptions(type, cached.items, selected);
    });
}

function renderAgentCapabilityOptions(type, items, selected) {
    const target = document.getElementById(`agent-form-${type}`);
    target.onchange = () => {
        target.dataset.touched = 'true';
    };
    items = items.filter(item => type === 'plugins' || !(item.plugin_id || item.owner_plugin_id));
    target.dataset.pending = 'false';
    target.dataset.selection = JSON.stringify(selected || []);
    const known = new Set(items.map(item => String(item.name || item.id || '')));
    items = [...items, ...(selected || []).filter(id => id !== '*' && !known.has(id)).map(id => ({ name: id }))];
    const selectedSet = new Set(selected || []);
    const selectsAll = selectedSet.has('*');
    target.dataset.wildcard = selectsAll ? 'true' : 'false';
    if (!items.length) {
        target.innerHTML = `<p class="agent-capability-empty">${escapeHtml(t('agents_no_capabilities'))}</p>`;
        return;
    }
    target.innerHTML = items.map(item => {
        const name = String(item.name || item.id || '');
        const label = localizedCapabilityName(item) || name;
        const description = localizedCapabilityDescription(item);
        const unavailableReason = item.enabled === false ? (item.unavailable_reason || '') : '';
        const presetNote = item.preset === true ? t('agents_capability_preset') : '';
        const details = [description, unavailableReason, presetNote].filter(Boolean).join(' · ');
        const locked = item.enabled === false || item.preset === true;
        return `<label class="agent-capability-item${item.enabled === false ? ' opacity-60' : ''}" title="${escapeAttribute(details)}">
            <input type="checkbox" value="${escapeAttribute(name)}" data-preset="${item.preset === true ? 'true' : 'false'}" ${item.preset === true || selectsAll || selectedSet.has(name) ? 'checked' : ''} ${locked ? 'disabled' : ''}>
            <span><strong>${escapeHtml(label)}</strong>${details ? `<small>${escapeHtml(details)}</small>` : ''}</span>
        </label>`;
    }).join('');
}

async function openAgentEditor(agent = null) {
    if (agentEditorSaving) return;
    const epoch = ++agentEditorEpoch;
    agentEditorSnapshot = agent ? JSON.parse(JSON.stringify(agent)) : null;
    agentEditorProvidersReady = false;
    document.getElementById('agent-editor-save').disabled = true;
    editingAgentId = agent ? agent.id : '';
    clearPendingAgentAvatar();
    removeAgentAvatar = false;
    const overlay = document.getElementById('agent-editor-overlay');
    const status = document.getElementById('agent-editor-status');
    document.getElementById('agent-editor-title').textContent = t(agent ? 'agents_edit_title' : 'agents_create_title');
    renderAgentAvatarPicker(agent?.avatar || '🤖');
    document.getElementById('agent-form-name').value = agent?.name || '';
    document.getElementById('agent-form-name-zh').value = agent?.name_zh || '';
    document.getElementById('agent-form-name-en').value = agent?.name_en || '';
    document.getElementById('agent-form-description').value = agent?.description || '';
    document.getElementById('agent-form-description-zh').value = agent?.description_zh || '';
    document.getElementById('agent-form-soul').value = agent?.soul || '';
    status.textContent = '';
    status.className = 'agent-editor-status';
    overlay.classList.remove('hidden');
    overlay.classList.add('flex');
    document.body.classList.add('agent-editor-open');
    document.getElementById('agent-form-name').focus();
    document.getElementById('agent-form-model-name').value = agent?.model?.name || '';
    ['tools', 'skills', 'plugins', 'mcps'].forEach(type => {
        const target = document.getElementById(`agent-form-${type}`);
        target.dataset.selection = JSON.stringify(agent?.[type] || []);
        target.dataset.pending = 'true';
        target.dataset.touched = 'false';
        target.innerHTML = `<p>${escapeHtml(currentLang === 'zh' ? '正在加载…' : 'Loading…')}</p>`;
        const button = document.querySelector(`[onclick="toggleAllAgentCapabilities('${type}')"]`);
        if (button) button.disabled = true;
        const path = type === 'mcps' ? 'mcp-servers' : type === 'plugins' ? 'plugins' : `capabilities/${{ tools: 'tool', skills: 'skill' }[type]}`;
        void fetch(`${PUBLIC_API_BASE}/${path}`).then(responseJson).then(result => {
            if (epoch !== agentEditorEpoch) return;
            if (result.status !== 'success') throw Error(result.message || 'Catalog unavailable');
            const items = (result.data || []).filter(item => type === 'plugins' || !(item.plugin_id || item.owner_plugin_id)).map(item => type === 'plugins' ? { ...item, name: item.id, display_name: item.name || item.id } : item);
            const selected = agent ? agent[type] : newAgentCapabilitySelection(type, items);
            agentCapabilityCatalog[type] = {items, selected, pending: false};
            renderAgentCapabilityOptions(type, items, selected);
            if (button) button.disabled = false;
        }).catch(error => {
            if (epoch !== agentEditorEpoch) return;
            target.textContent = error.message;
        });
    });
    try {
        const result = await fetch(`${PUBLIC_API_BASE}/providers`).then(responseJson);
        if (epoch !== agentEditorEpoch) return;
        if (result.status !== 'success') throw Error(result.message || 'Provider catalog unavailable');
        const catalog = { providers: (result.data || []).filter(item => item.configured).map(item => ({ ...item, name: item.label })) };
        const providerDropdown = document.getElementById('agent-form-provider');
        // Preselect the only configured provider for convenience. Keep the
        // empty placeholder when none exists so the draft can be saved and
        // configured later.
        const selectedProvider = agent?.model?.provider
            || (catalog.providers.length === 1 ? catalog.providers[0]?.id : '')
            || '';
        initDropdown(
            providerDropdown,
            catalog.providers.map(provider => ({
                value: provider.id,
                label: provider.name,
            })),
            selectedProvider,
            () => onAgentProviderChanged(),
            {placeholder: t('models_pick_provider')},
        );
        initAgentModelDropdown();
        renderAgentModelOptions([]);
        document.getElementById('agent-model-discovery-status').textContent = '';
        if (selectedProvider) discoverAgentModels(selectedProvider);
        agentEditorProvidersReady = true;
        document.getElementById('agent-editor-save').disabled = false;
    } catch (error) {
        if (epoch !== agentEditorEpoch) return;
        status.textContent = error.message;
        status.classList.add('error');
    }
}

function editAgent(agentId) {
    const agent = agentsState.find(item => item.id === agentId);
    if (agent) openAgentEditor(agent);
}

function closeAgentEditor() {
    if (agentEditorSaving) return;
    agentEditorEpoch += 1;
    for (const type of Object.keys(agentCapabilityCatalog)) agentCapabilityCatalog[type] = null;
    const overlay = document.getElementById('agent-editor-overlay');
    overlay.classList.add('hidden');
    overlay.classList.remove('flex');
    document.body.classList.remove('agent-editor-open');
    clearPendingAgentAvatar();
    removeAgentAvatar = false;
    editingAgentId = '';
}

function toggleAllAgentCapabilities(type) {
    document.getElementById(`agent-form-${type}`).dataset.touched = 'true';
    const target = document.getElementById(`agent-form-${type}`);
    if (target.dataset.pending === 'true') return;
    const checkboxes = [...target.querySelectorAll('input[type="checkbox"]')];
    const selectable = checkboxes.filter(checkbox => checkbox.dataset.preset !== 'true');
    const shouldCheck = selectable.some(checkbox => !checkbox.checked);
    selectable.forEach(checkbox => { checkbox.checked = shouldCheck; });
    target.dataset.wildcard = shouldCheck ? 'true' : 'false';
}

async function saveAgent() {
    if (!agentEditorProvidersReady || document.getElementById('agent-editor-save').disabled) return;
    const name = document.getElementById('agent-form-name').value.trim();
    const status = document.getElementById('agent-editor-status');
    if (!name) {
        status.textContent = t('agents_name_required');
        status.className = 'agent-editor-status error';
        document.getElementById('agent-form-name').focus();
        return;
    }
    const provider = getDropdownValue(document.getElementById('agent-form-provider'));
    const modelName = document.getElementById('agent-form-model-name').value.trim();
    if (!provider && modelName) {
        status.textContent = currentLang === 'zh'
            ? '填写模型名称前请先选择服务商，也可以留空后稍后配置'
            : 'Select a provider before entering a model name, or leave it empty to configure later';
        status.className = 'agent-editor-status error';
        return;
    }
    const selected = type => selectedAgentCapabilityIds(type);
    const currentAgent = agentEditorSnapshot || {};
    const selectedAvatar = document.getElementById('agent-form-avatar').value.trim() || '🤖';
    const metadataAvatar = isUploadedAgentAvatar(selectedAvatar)
        ? (isUploadedAgentAvatar(currentAgent.metadata?.avatar)
            ? '🤖'
            : (currentAgent.metadata?.avatar || '🤖'))
        : selectedAvatar;
    const payload = {
        name,
        name_zh: document.getElementById('agent-form-name-zh').value.trim(),
        name_en: document.getElementById('agent-form-name-en').value.trim(),
        description: document.getElementById('agent-form-description').value.trim(),
        description_zh: document.getElementById('agent-form-description-zh').value.trim(),
        soul: document.getElementById('agent-form-soul').value.trim(),
        model: modelName ? { provider, name: modelName } : null,
        tools: selected('tools'),
        skills: selected('skills'),
        plugins: selected('plugins'),
        mcps: selected('mcps'),
        metadata: {
            ...(currentAgent.metadata || {}),
            avatar: metadataAvatar,
        },
    };
    const equal = (left, right) => {
        if (left === right) return true;
        if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
        if (Array.isArray(left) || Array.isArray(right)) return Array.isArray(left) && Array.isArray(right) && left.length === right.length && left.every((value, index) => equal(value, right[index]));
        return Object.keys(left).length === Object.keys(right).length && Object.keys(left).every(key => Object.hasOwn(right, key) && equal(left[key], right[key]));
    };
    if (editingAgentId) {
        payload.metadata = { ...(currentAgent.metadata || {}) };
        if (selectedAvatar !== (currentAgent.avatar || '🤖')) payload.metadata.avatar = metadataAvatar;
        for (const key of Object.keys(payload)) {
            const previous = ['name', 'name_zh', 'name_en', 'description', 'description_zh', 'soul'].includes(key) ? (currentAgent[key] || '').trim() : currentAgent[key];
            const sameSelection = ['tools', 'skills', 'plugins', 'mcps'].includes(key) && Array.isArray(previous)
                && payload[key].length === previous.length && payload[key].every(value => previous.includes(value));
            if ((sameSelection || equal(payload[key], previous)) && !(key === 'plugins' && document.getElementById('agent-form-plugins').dataset.touched === 'true')) delete payload[key];
        }
    }
    // 安全权限设置与执行模式都在安全控制台维护，专家编辑器不再改动它们。
    const processChange = false;
    const nextRunChange = Boolean(editingAgentId && ['model', 'soul', 'tools', 'skills', 'plugins', 'mcps'].some(key => key in payload));
    const saveButton = document.getElementById('agent-editor-save');
    saveButton.disabled = true;
    agentEditorSaving = true;
    const closeButtons = document.querySelectorAll('[onclick="closeAgentEditor()"]');
    closeButtons.forEach(button => { button.disabled = true; });
    status.className = 'agent-editor-status';
    status.textContent = processChange
        ? (currentLang === 'zh' ? '正在应用进程与安全配置…' : 'Applying process and security configuration…')
        : (currentLang === 'zh' ? '正在保存…' : 'Saving…');
    try {
        let data = { status: 'success', data: currentAgent };
        if (!editingAgentId || Object.keys(payload).length) {
            const response = await fetch(editingAgentId ? `${PUBLIC_API_BASE}/agents/${encodeURIComponent(editingAgentId)}` : `${PUBLIC_API_BASE}/agents`, {
                method: editingAgentId ? 'PATCH' : 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            data = await responseJson(response);
            if (data.status !== 'success') throw new Error(data.message || t('agents_save_failed'));
        }
        // Reject lists begun before the mutation, including a partial avatar save.
        agentsStateRevision += 1;
        const agentId = data.data.id;
        editingAgentId = agentId;
        agentEditorSnapshot = data.data;
        if (pendingAgentAvatarFile) {
            const form = new FormData();
            form.append('file', pendingAgentAvatarFile);
            const avatarResponse = await fetch(
                `${PUBLIC_API_BASE}/agents/${encodeURIComponent(agentId)}/avatar`,
                { method: 'POST', body: form },
            );
            data = await responseJson(avatarResponse);
            if (data.status !== 'success') {
                throw new Error(data.message || t('agents_avatar_upload_failed'));
            }
        } else if (removeAgentAvatar) {
            const avatarResponse = await fetch(
                `${PUBLIC_API_BASE}/agents/${encodeURIComponent(agentId)}/avatar`,
                { method: 'DELETE' },
            );
            if (!avatarResponse.ok && avatarResponse.status !== 404) {
                throw new Error(t('agents_avatar_upload_failed'));
            }
            data.data.avatar = metadataAvatar;
        }
        // Avatar operations can finish after another list request has started.
        agentsStateRevision += 1;
        if (activeAgent?.id === data.data.id) setActiveAgent(data.data);
        agentsState = agentsState.some(item => item.id === data.data.id)
            ? agentsState.map(item => item.id === data.data.id ? data.data : item)
            : [...agentsState, data.data];
        agentEditorSaving = false;
        closeAgentEditor();
        renderAgentsView();
        const feedback = document.getElementById('agents-mcp-health-status');
        feedback.textContent = processChange
            ? (currentLang === 'zh' ? '进程与安全配置已应用。' : 'Process and security configuration applied.')
            : nextRunChange ? (currentLang === 'zh' ? '配置已保存，下次运行生效；当前运行不受影响。' : 'Configuration saved for the next run; the current run is unaffected.') : t('agents_saved');
        void refreshAgentList(true).then(() => renderAgentsView()).catch(error => {
            feedback.textContent += ` ${currentLang === 'zh' ? '列表刷新失败：' : 'List refresh failed: '}${error.message}`;
        });
    } catch (error) {
        status.textContent = error.message || t('agents_save_failed');
        status.className = 'agent-editor-status error';
    } finally {
        agentEditorSaving = false;
        closeButtons.forEach(button => { button.disabled = false; });
        saveButton.disabled = false;
    }
}

function onAgentProviderChanged() {
    const providerId = getDropdownValue(document.getElementById('agent-form-provider'));
    document.getElementById('agent-form-model-name').value = '';
    renderAgentModelOptions([]);
    discoverAgentModels(providerId);
}

async function discoverAgentModels(providerId) {
    const status = document.getElementById('agent-model-discovery-status');
    const options = document.getElementById('agent-model-options');
    if (!providerId || !status || !options) return;
    status.textContent = currentLang === 'zh' ? '正在获取模型列表…' : 'Loading models…';
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/providers/${encodeURIComponent(providerId)}/models`);
        const data = await responseJson(response);
        if (!response.ok || data.status !== 'success') throw new Error(data.message || 'Discovery failed');
        renderAgentModelOptions(data.data || []);
        status.textContent = currentLang === 'zh'
            ? `已获取 ${(data.data || []).length} 个模型，也可手动输入`
            : `${(data.data || []).length} models found; manual entry is also supported`;
    } catch (error) {
        // 后端已把「超时 / 连不上 / 凭据被拒」翻译成具体原因（error envelope 的
        // message）。以前这里无条件显示「无法获取模型列表」，用户看不出是网络、
        // 地址写错还是 Key 过期 —— 保留原因，只在拿不到原因时回退到通用文案。
        const reason = String((error && error.message) || '').trim();
        const detail = reason && reason !== 'Discovery failed' ? reason : '';
        status.textContent = detail
            || (currentLang === 'zh'
                ? '无法获取模型列表，请手动输入模型名称'
                : 'Could not load models; enter the model name manually');
    }
}

function deleteAgent(agentId) {
    showConfirmDialog({
        title: t('agents_delete_title'),
        message: t('agents_delete_confirm'),
        onConfirm: async () => {
            const response = await fetch(`${PUBLIC_API_BASE}/agents/${encodeURIComponent(agentId)}`, { method: 'DELETE' });
            if (response.ok) {
                agentsStateRevision += 1;
                agentsState = agentsState.filter(agent => agent.id !== agentId);
                if (activeAgent?.id === agentId) {
                    activeAgent = {
                        ...activeAgent,
                        available: false,
                        unavailable_reason: 'agent_not_found',
                    };
                    renderActiveAgent();
                }
                renderAgentsView();
            } else {
                await explainRefusedDelete(
                    response,
                    currentLang === 'zh'
                        ? '删除失败：安全服务暂不可用，请恢复 securityd 后重试。'
                        : 'Delete failed: the security service is unavailable. Restore securityd and retry.',
                );
            }
        },
    });
}

function useAgent(event, agentId) {
    if (event) event.stopPropagation();
    const agent = agentsState.find(item => item.id === agentId);
    if (!agent || !agentCanStartSession(agent)) return;
    createSessionForAgent(agent.id);
}

function setActiveAgent(agent) {
    activeAgent = agent && agent.id ? agent : null;
    renderActiveAgent();
}

const MODEL_SETUP_REASONS = new Set([
    'agent_model_required',
]);

function agentCanStartSession(agent) {
    return agent?.available !== false
        || MODEL_SETUP_REASONS.has(String(agent?.unavailable_reason || ''));
}

function modelSetupRequired() {
    return activeAgent?.available === false
        && MODEL_SETUP_REASONS.has(String(activeAgent.unavailable_reason || ''));
}

function renderModelSetupReminder() {
    const reminder = document.getElementById('model-setup-required');
    if (reminder) reminder.classList.toggle('hidden', !modelSetupRequired());
}

function openModelProviderSetup() {
    navigateTo('models');
}

function openActiveAgentModelSetup() {
    if (activeAgent) void openAgentEditor(activeAgent);
}

function renderActiveAgent() {
    const bar = document.getElementById('active-agent-bar');
    if (!bar) return;
    const dot = document.getElementById('chat-agent-active-dot');
    const toggle = document.getElementById('chat-agent-toggle');
    if (!activeAgent) {
        bar.classList.add('hidden');
        if (dot) dot.classList.add('hidden');
        if (toggle) toggle.classList.remove('active');
        renderModelSetupReminder();
        renderChatAgentMenu();
        updateComposerInteractionState();
        return;
    }
    document.getElementById('active-agent-avatar').innerHTML = agentAvatarMarkup(activeAgent.avatar);
    document.getElementById('active-agent-name').textContent = agentDisplayName(activeAgent);
    const label = bar.querySelector('small');
    if (label) label.textContent = t(activeAgent.available === false ? 'agents_unavailable' : 'agents_chatting_with');
    bar.classList.toggle('agent-unavailable', activeAgent.available === false);
    bar.classList.remove('hidden');
    if (dot) dot.classList.remove('hidden');
    if (toggle) toggle.classList.add('active');
    renderModelSetupReminder();
    renderChatAgentMenu();
    updateComposerInteractionState();
    updateSendBtnState();
}

function clearActiveAgent() {
    setActiveAgent(null);
}

function stopUsingAgent() {
    if (!activeAgent) return;
    enterAgentChooserDraft();
}

document.getElementById('agent-editor-overlay').addEventListener('click', event => {
    if (event.target === event.currentTarget) closeAgentEditor();
});

// =====================================================================
// Model Providers
// =====================================================================
let modelsState = { templates: [], providers: [] };
let editingProviderId = '';
// The Provider record the modal is editing, kept for the API key hint.
let editingProviderRecord = null;
let selectedProviderTemplateId = 'custom';
const PROVIDER_TEMPLATES = [
    {id: 'openai', name: 'OpenAI', protocol: 'openai-compatible', default_base_url: 'https://api.openai.com/v1'},
    {id: 'anthropic', name: 'Anthropic', protocol: 'openai-compatible', default_base_url: 'https://api.anthropic.com/v1'},
    {id: 'gemini', name: 'Gemini', protocol: 'openai-compatible', default_base_url: 'https://generativelanguage.googleapis.com/v1beta/openai'},
    {id: 'openrouter', name: 'OpenRouter', protocol: 'openai-compatible', default_base_url: 'https://openrouter.ai/api/v1'},
    {id: 'deepseek', name: 'DeepSeek', protocol: 'openai-compatible', default_base_url: 'https://api.deepseek.com/v1'},
    {id: 'minimax', name: 'MiniMax', protocol: 'openai-compatible', default_base_url: 'https://api.minimaxi.com/v1'},
    {id: 'siliconflow', name: 'SiliconFlow', protocol: 'openai-compatible', default_base_url: 'https://api.siliconflow.cn/v1'},
    {id: 'mistral', name: 'Mistral', protocol: 'openai-compatible', default_base_url: 'https://api.mistral.ai/v1'},
    {id: 'groq', name: 'Groq', protocol: 'openai-compatible', default_base_url: 'https://api.groq.com/openai/v1'},
    {id: 'xai', name: 'xAI', protocol: 'openai-compatible', default_base_url: 'https://api.x.ai/v1'},
    {id: 'nvidia', name: 'NVIDIA', protocol: 'openai-compatible', default_base_url: 'https://integrate.api.nvidia.com/v1'},
    {id: 'huggingface', name: 'Hugging Face', protocol: 'openai-compatible', default_base_url: 'https://router.huggingface.co/v1'},
    {id: 'azure', name: 'Azure OpenAI', protocol: 'openai-compatible', default_base_url: ''},
    {id: 'ollama', name: 'Ollama', protocol: 'openai-compatible', default_base_url: 'http://127.0.0.1:11434/v1'},
    {id: 'lm-studio', name: 'LM Studio', protocol: 'openai-compatible', default_base_url: 'http://127.0.0.1:1234/v1'},
    {id: 'custom', name: 'Custom Provider', protocol: 'openai-compatible', default_base_url: ''},
];

function loadModelsView() {
    const loading = document.getElementById('models-loading');
    const content = document.getElementById('models-content');
    if (!loading || !content) return;
    loading.classList.remove('hidden');
    content.classList.add('hidden');
    setModelsStatus('');
    fetch(`${PUBLIC_API_BASE}/providers`).then(responseJson).then(data => {
        if (data.status !== 'success') throw new Error(data.message || 'Failed to load providers');
        modelsState = {
            templates: PROVIDER_TEMPLATES,
            providers: (data.data || []).map(provider => ({
                ...provider,
                name: provider.label,
                protocol: provider.parameters?.protocol || 'openai-compatible',
            })),
        };
        invalidateAgentCatalog();
        renderProvidersSection();
        loading.classList.add('hidden');
        content.classList.remove('hidden');
    }).catch(error => {
        loading.innerHTML = `<span class="text-red-500">${escapeHtml(String(error))}</span>`;
    });
}

function setModelsStatus(message) {
    const status = document.getElementById('models-status');
    if (!status) return;
    status.textContent = message || '';
    status.classList.toggle('hidden', !message);
}

function renderProvidersSection() {
    const target = document.getElementById('models-providers-list');
    if (!target) return;
    if (!modelsState.providers.length) {
        target.innerHTML = '<p class="text-sm text-slate-400">尚未配置服务商。添加服务商后即可为 Agent 选择模型。</p>';
        return;
    }
    target.innerHTML = modelsState.providers.map(provider => `
        <button type="button" onclick="openProviderEditor('${escapeAttribute(provider.id)}')"
                class="w-full flex items-center justify-between gap-3 p-3 rounded-lg border border-slate-200 dark:border-white/10 text-left">
            <span><strong class="block">${escapeHtml(provider.name || provider.provider)}</strong>
                <small class="text-slate-400">${escapeHtml(provider.protocol)} · ${escapeHtml(provider.base_url || '')}</small></span>
            <span class="text-xs ${provider.configured ? 'text-emerald-500' : 'text-amber-500'}">
                ${provider.configured ? 'Configured' : 'Incomplete'}
            </span>
        </button>`).join('');
}

function openProviderPicker() {
    const picker = document.getElementById('provider-picker');
    picker.innerHTML = modelsState.templates.map(template => `
        <button type="button" class="provider-picker-item" onclick="selectProviderTemplate('${escapeAttribute(template.id)}')">
            <strong>${escapeHtml(template.name)}</strong>
            <small>${escapeHtml(template.protocol)}</small>
        </button>`).join('');
    document.getElementById('provider-picker-overlay').classList.remove('hidden');
}

function closeProviderPicker() {
    document.getElementById('provider-picker-overlay').classList.add('hidden');
}

function selectProviderTemplate(templateId) {
    closeProviderPicker();
    editingProviderId = '';
    selectedProviderTemplateId = templateId;
    const template = modelsState.templates.find(item => item.id === templateId);
    openProviderModal(null, template);
}

function openProviderEditor(providerId) {
    editingProviderId = providerId;
    const provider = modelsState.providers.find(item => item.id === providerId);
    selectedProviderTemplateId = modelsState.templates.some(item => item.id === providerId)
        ? providerId : 'custom';
    openProviderModal(provider, null);
}

function openCustomProviderModal(providerId = '') {
    if (providerId) openProviderEditor(providerId);
    else selectProviderTemplate('custom');
}

function setCustomProviderModalStatus(message, error = false) {
    const element = document.getElementById('custom-provider-modal-status');
    if (!element) return;
    element.textContent = String(message || '');
    element.classList.toggle('opacity-0', !message);
    element.classList.toggle('text-red-500', Boolean(error));
    element.classList.toggle('text-primary-500', !error);
}

// A stored API key is never sent back to the browser, so the field opens empty
// and the placeholder is the only thing that can tell the user a credential
// already exists and that typing a value replaces it.  Same promise Harness
// makes ("Configured — enter a new value to replace"); a language switch
// re-applies data-i18n-placeholder, so this stays the last word on the field.
function applyProviderKeyHint() {
    const input = document.getElementById('custom-provider-key');
    if (!input) return;
    const configured = Boolean(editingProviderRecord &&
        (editingProviderRecord.api_key_masked || editingProviderRecord.configured));
    input.placeholder = t(configured ? 'models_custom_key_configured' : 'models_custom_key_placeholder');
}

function openProviderModal(provider, template) {
    const overlay = document.getElementById('custom-provider-modal-overlay');
    document.getElementById('custom-provider-modal-title').textContent = provider ? 'Edit Provider' : 'Add Provider';
    document.getElementById('custom-provider-name').value = provider?.name || template?.name || '';
    document.getElementById('custom-provider-protocol').value = provider?.protocol || template?.protocol || 'openai-compatible';
    document.getElementById('custom-provider-base').value = provider?.base_url || template?.default_base_url || '';
    document.getElementById('custom-provider-key').value = '';
    editingProviderRecord = provider || null;
    applyProviderKeyHint();
    setCustomProviderModalStatus('');
    const deleteButton = document.getElementById('custom-provider-modal-delete');
    deleteButton.classList.toggle('hidden', !provider);
    deleteButton.onclick = provider ? () => deleteProviderRecord(provider.id) : null;
    document.getElementById('custom-provider-modal-cancel').onclick = closeCustomProviderModal;
    document.getElementById('custom-provider-modal-save').onclick = saveCustomProviderModal;
    overlay.classList.remove('hidden');
}

function closeCustomProviderModal() {
    document.getElementById('custom-provider-modal-overlay').classList.add('hidden');
    editingProviderRecord = null;
    editingProviderId = '';
}

async function saveCustomProviderModal() {
    const name = document.getElementById('custom-provider-name').value.trim();
    const protocol = document.getElementById('custom-provider-protocol').value;
    const base = document.getElementById('custom-provider-base').value.trim();
    const key = document.getElementById('custom-provider-key').value.trim();
    if (!name || !base) {
        setCustomProviderModalStatus(t('models_custom_name_required') + ' / ' + t('models_custom_base_required'), true);
        return;
    }
    const payload = {
        label: name,
        base_url: base,
        parameters: { protocol },
    };
    if (key) payload.api_key = key;
    if (!editingProviderId) {
        const customId = name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '') || 'provider';
        payload.id = selectedProviderTemplateId === 'custom'
            ? `custom:${customId}`
            : selectedProviderTemplateId;
        // Check if provider ID already exists
        const existingProvider = modelsState.providers.find(p => p.id === payload.id);
        if (existingProvider) {
            setCustomProviderModalStatus(t('models_custom_name_duplicate'), true);
            return;
        }
    }
    const endpoint = editingProviderId
        ? `${PUBLIC_API_BASE}/providers/${encodeURIComponent(editingProviderId)}`
        : `${PUBLIC_API_BASE}/providers`;
    const response = await fetch(endpoint, {
        method: editingProviderId ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    const data = await responseJson(response);
    if (!response.ok || data.status !== 'success') {
        setCustomProviderModalStatus(data.message || 'Failed to save provider.', true);
        return;
    }
    closeCustomProviderModal();
    loadModelsView();
}

async function deleteProviderRecord(recordId) {
    const response = await fetch(`${PUBLIC_API_BASE}/providers/${encodeURIComponent(recordId)}`, { method: 'DELETE' });
    if (response.status !== 204) {
        const data = await responseJson(response);
        setCustomProviderModalStatus(data.message || 'Failed to delete provider.', true);
        return;
    }
    closeCustomProviderModal();
    loadModelsView();
}

// =====================================================================
// Provider API Capture
// =====================================================================
const PROVIDER_CAPTURE_POLL_MS = 1000;
const PROVIDER_CAPTURE_CATALOG_RETRY_MS = 1000;
let providerCaptureProviders = [];
let providerCaptureState = { provider_id: null, limits: {}, records: [] };
let providerCaptureGeneration = 0;
let providerCaptureTimer = null;
let providerCaptureCatalogTimer = null;
let providerCaptureRequest = null;
let providerCaptureCatalogRequest = null;
let providerCaptureMutationRequest = null;
let providerCaptureDetailController = null;
let providerCaptureModalOpener = null;
let providerCaptureBusy = false;
let providerCaptureSelectionTouched = false;
let providerCaptureProviderFingerprint = '';
let providerCaptureRecordFingerprint = '';

function providerCaptureJson(value) {
    if (value === undefined) return '';
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value, null, 2); }
    catch (_) { return String(value); }
}

function providerCaptureContentText(content) {
    if (content === undefined || content === null) return '';
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return providerCaptureJson(content);
    return content.map(part => {
        if (typeof part === 'string') return part;
        if (!part || typeof part !== 'object') return providerCaptureJson(part);
        for (const key of ['text', 'input_text', 'output_text']) {
            if (typeof part[key] === 'string') return part[key];
        }
        return providerCaptureJson(part);
    }).filter(Boolean).join('\n');
}

function providerCaptureFriendlyEmpty(container) {
    const empty = document.createElement('p');
    empty.className = 'rounded-lg border border-dashed border-slate-200 dark:border-white/10 px-3 py-4 text-center text-xs text-slate-400 dark:text-slate-500';
    empty.textContent = t('capture_empty_friendly');
    container.appendChild(empty);
}

function providerCaptureFriendlyCard(label) {
    const card = document.createElement('article');
    card.className = 'rounded-lg border border-slate-200 dark:border-white/10 bg-slate-50 dark:bg-white/[0.03] p-3';
    const badge = document.createElement('strong');
    badge.className = 'inline-flex rounded bg-slate-200 dark:bg-white/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-600 dark:text-slate-300';
    badge.textContent = String(label || '--').toUpperCase();
    card.appendChild(badge);
    return card;
}

function providerCaptureFriendlyField(card, label, value, code = false) {
    if (value === undefined || value === null || value === '') return;
    const wrapper = document.createElement('div');
    wrapper.className = 'mt-2';
    const heading = document.createElement('div');
    heading.className = 'mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400 dark:text-slate-500';
    heading.textContent = label;
    const content = document.createElement(code ? 'pre' : 'div');
    content.className = code
        ? 'overflow-auto whitespace-pre-wrap break-words rounded bg-slate-950 px-3 py-2 text-xs text-slate-200'
        : 'max-h-64 overflow-auto whitespace-pre-wrap break-words text-sm text-slate-700 dark:text-slate-200';
    content.textContent = String(value);
    wrapper.append(heading, content);
    card.appendChild(wrapper);
}

function providerCaptureToolCallCard(call, fallbackLabel = null) {
    const source = call && typeof call === 'object' ? call : {};
    const fn = source.function && typeof source.function === 'object'
        ? source.function
        : source;
    const card = providerCaptureFriendlyCard(fallbackLabel || t('capture_tool_call'));
    const name = fn.name || source.name || '--';
    const title = document.createElement('span');
    title.className = 'ml-2 text-sm font-semibold text-slate-700 dark:text-slate-200';
    title.textContent = String(name);
    card.firstElementChild.after(title);
    providerCaptureFriendlyField(card, t('capture_call_id'), source.id || source.call_id);
    const argumentsValue = Object.prototype.hasOwnProperty.call(fn, 'arguments')
        ? fn.arguments
        : fn.args;
    providerCaptureFriendlyField(
        card,
        t('capture_arguments'),
        typeof argumentsValue === 'string'
            ? argumentsValue
            : providerCaptureJson(argumentsValue),
        true,
    );
    return card;
}

function renderProviderCaptureRequestFriendly(body) {
    const container = document.getElementById('provider-capture-detail-request-friendly');
    container.replaceChildren();
    if (!body || typeof body !== 'object') {
        providerCaptureFriendlyEmpty(container);
        return;
    }
    const messages = Array.isArray(body.messages) ? body.messages : [];
    messages.forEach(message => {
        if (!message || typeof message !== 'object') return;
        const role = String(message.role || '--');
        const card = providerCaptureFriendlyCard(role);
        const content = providerCaptureContentText(message.content);
        if (content) {
            providerCaptureFriendlyField(
                card,
                role === 'tool' ? t('capture_tool_result') : t('capture_content'),
                content,
            );
        }
        if (role === 'tool') {
            providerCaptureFriendlyField(
                card,
                t('capture_call_id'),
                message.tool_call_id || message.call_id,
            );
        }
        const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
        calls.forEach(call => card.appendChild(providerCaptureToolCallCard(call)));
        container.appendChild(card);
    });
    const tools = Array.isArray(body.tools) ? body.tools : [];
    tools.forEach(tool => {
        if (!tool || typeof tool !== 'object') return;
        const fn = tool.function && typeof tool.function === 'object'
            ? tool.function
            : tool;
        const card = providerCaptureFriendlyCard(t('capture_tools'));
        const title = document.createElement('span');
        title.className = 'ml-2 text-sm font-semibold text-slate-700 dark:text-slate-200';
        title.textContent = String(fn.name || tool.name || '--');
        card.firstElementChild.after(title);
        providerCaptureFriendlyField(card, t('capture_description'), fn.description);
        providerCaptureFriendlyField(
            card,
            t('capture_parameters'),
            providerCaptureJson(fn.parameters || fn.input_schema),
            true,
        );
        container.appendChild(card);
    });
    if (!container.children.length) providerCaptureFriendlyEmpty(container);
}

function providerCaptureAssembledChoices(body) {
    if (!body || typeof body !== 'object') return [];
    const frames = Array.isArray(body.stream) ? body.stream : null;
    if (!frames) {
        const choices = Array.isArray(body.choices) ? body.choices : [];
        return choices.map((choice, position) => {
            const source = choice?.message || choice?.delta || {};
            return {
                index: choice?.index ?? position,
                role: source.role || 'assistant',
                content: providerCaptureContentText(source.content),
                toolCalls: Array.isArray(source.tool_calls) ? source.tool_calls : [],
            };
        });
    }
    const assembled = new Map();
    frames.forEach(frame => {
        if (!frame || typeof frame !== 'object') return;
        const choices = Array.isArray(frame.choices) ? frame.choices : [];
        choices.forEach((choice, position) => {
            if (!choice || typeof choice !== 'object') return;
            const index = choice.index ?? position;
            if (!assembled.has(index)) {
                assembled.set(index, {
                    index,
                    role: 'assistant',
                    content: '',
                    toolCalls: new Map(),
                });
            }
            const state = assembled.get(index);
            const delta = choice.delta || choice.message || {};
            if (delta.role) state.role = String(delta.role);
            state.content += providerCaptureContentText(delta.content);
            const calls = Array.isArray(delta.tool_calls) ? delta.tool_calls : [];
            calls.forEach((call, callPosition) => {
                if (!call || typeof call !== 'object') return;
                const callIndex = call.index ?? callPosition;
                if (!state.toolCalls.has(callIndex)) {
                    state.toolCalls.set(callIndex, {
                        id: '',
                        type: call.type || 'function',
                        function: { name: '', arguments: '' },
                    });
                }
                const target = state.toolCalls.get(callIndex);
                if (call.id) target.id = String(call.id);
                if (call.type) target.type = String(call.type);
                const fn = call.function && typeof call.function === 'object'
                    ? call.function
                    : call;
                if (fn.name) target.function.name += String(fn.name);
                const argumentFragment = Object.prototype.hasOwnProperty.call(fn, 'arguments')
                    ? fn.arguments
                    : fn.args;
                if (argumentFragment !== undefined && argumentFragment !== null) {
                    target.function.arguments += typeof argumentFragment === 'string'
                        ? argumentFragment
                        : providerCaptureJson(argumentFragment);
                }
            });
        });
    });
    return Array.from(assembled.values()).map(choice => ({
        ...choice,
        toolCalls: Array.from(choice.toolCalls.values()),
    }));
}

function renderProviderCaptureResponseFriendly(body) {
    const container = document.getElementById('provider-capture-detail-response-friendly');
    container.replaceChildren();
    const choices = providerCaptureAssembledChoices(body);
    choices.forEach(choice => {
        const card = providerCaptureFriendlyCard(choice.role || 'assistant');
        if (choices.length > 1) {
            providerCaptureFriendlyField(card, t('capture_choice'), choice.index);
        }
        if (choice.content) {
            providerCaptureFriendlyField(card, t('capture_content'), choice.content);
        }
        choice.toolCalls.forEach(call => card.appendChild(providerCaptureToolCallCard(call)));
        container.appendChild(card);
    });
    if (!container.children.length) providerCaptureFriendlyEmpty(container);
}

function providerCaptureLabel(providerId) {
    const provider = providerCaptureProviders.find(item => item.id === providerId);
    return provider ? String(provider.label || provider.id) : String(providerId || '');
}

function providerCaptureSetStatus(message, error = false) {
    const element = document.getElementById('provider-capture-status');
    if (!element) return;
    element.textContent = String(message || '');
    element.classList.toggle('opacity-0', !message);
    element.classList.toggle('text-red-500', Boolean(error));
    element.classList.toggle('text-slate-500', !error);
    element.classList.toggle('dark:text-slate-400', !error);
}

function providerCaptureSetCatalogStatus(message, error = false) {
    const element = document.getElementById('provider-capture-provider-status');
    if (!element) return;
    element.textContent = String(message || '');
    element.classList.toggle('opacity-0', !message);
    element.classList.toggle('text-red-500', Boolean(error));
    element.classList.toggle('text-slate-500', !error);
    element.classList.toggle('dark:text-slate-400', !error);
}

function providerCaptureSetDetailStatus(message, error = false) {
    const element = document.getElementById('provider-capture-detail-status');
    element.textContent = String(message || '');
    element.classList.toggle('opacity-0', !message);
    element.classList.toggle('text-red-500', Boolean(error));
    element.classList.toggle('text-slate-500', !error);
    element.classList.toggle('dark:text-slate-400', !error);
}

function providerCaptureLimitsText(limits) {
    const retentionDays = Number(limits.retention_seconds || 0) / 86400;
    const storageMiB = Number(limits.max_storage_bytes || 0) / (1024 * 1024);
    const retainedRecords = Number(limits.max_retained_records || 0);
    const recordMiB = Number(limits.max_record_bytes || 0) / (1024 * 1024);
    const previewKiB = Number(limits.preview_bytes || 0) / 1024;
    const sidecarKiB = Number(limits.max_preview_index_bytes || 0) / 1024;
    if (!retentionDays || !storageMiB || !retainedRecords || !recordMiB || !previewKiB || !sidecarKiB) return '';
    const formattedRecords = retainedRecords.toLocaleString(currentLang === 'en' ? 'en-US' : undefined);
    if (currentLang === 'zh') {
        return `保留限制：${retentionDays} 天、${storageMiB} MiB 总存储或 ${formattedRecords} 条记录，以先到者为准；每条完整记录最多 ${recordMiB} MiB；列表最多分别显示 ${previewKiB} KiB 头部和尾部，sidecar 最多 ${sidecarKiB} KiB。`;
    }
    return `Retention: ${retentionDays} days, ${storageMiB} MiB total storage, or ${formattedRecords} records; full records are capped at ${recordMiB} MiB; previews show up to ${previewKiB} KiB each from the head and tail; ${sidecarKiB} KiB sidecars keep collection reads bounded.`;
}

function providerCapturePreviewNode(label, preview) {
    const wrapper = document.createElement('div');
    const heading = document.createElement('div');
    heading.className = 'flex items-center justify-between gap-2 mb-1';
    const title = document.createElement('strong');
    title.className = 'text-[11px] uppercase tracking-wide text-slate-500 dark:text-slate-400';
    title.textContent = label;
    const count = document.createElement('span');
    count.className = 'text-[11px] text-slate-400 dark:text-slate-500';
    count.textContent = `${Number(preview?.characters || 0)} ${t('capture_characters')}`;
    heading.append(title, count);
    const content = document.createElement('pre');
    content.className = 'max-h-44 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-slate-950 p-3 text-xs text-slate-200';
    const head = String(preview?.head || '');
    const tail = String(preview?.tail || '');
    content.textContent = preview?.truncated ? `${head}\n\n…\n\n${tail}` : `${head}${tail}`;
    wrapper.append(heading, content);
    if (preview?.truncated) {
        const marker = document.createElement('p');
        marker.className = 'mt-1 text-[11px] text-amber-600 dark:text-amber-400';
        const omittedBytes = Math.max(0, Number(preview?.omitted_bytes || 0));
        marker.textContent = t('capture_preview_truncated').replace(
            '{count}', String(omittedBytes)
        );
        wrapper.appendChild(marker);
    }
    return wrapper;
}

function providerCaptureLocale() {
    if (currentLang === 'zh') return 'zh-CN';
    return 'en-US';
}

function providerCaptureStatusLabel(value) {
    const normalized = String(value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_');
    const key = `capture_status_${normalized}`;
    return normalized && I18N[currentLang]?.[key] ? t(key) : String(value || '--');
}

function providerCaptureUsageTotal(usage) {
    const valid = value => {
        if (value === null || value === undefined || value === '' || typeof value === 'boolean') {
            return null;
        }
        const number = Number(value);
        return Number.isSafeInteger(number) && number >= 0 ? number : null;
    };
    const explicit = valid(usage?.total_tokens);
    if (explicit !== null) return explicit;
    for (const [inputKey, outputKey] of [
        ['input_tokens', 'output_tokens'],
        ['prompt_tokens', 'completion_tokens'],
    ]) {
        const input = valid(usage?.[inputKey]);
        const output = valid(usage?.[outputKey]);
        if (input !== null && output !== null) return input + output;
    }
    return null;
}

function providerCaptureRecordNode(record) {
    const article = document.createElement('article');
    article.className = 'p-5 space-y-4';
    article.dataset.captureRecordId = String(record.id || '');
    const header = document.createElement('div');
    header.className = 'flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between';
    const identity = document.createElement('div');
    identity.className = 'min-w-0';
    const title = document.createElement('strong');
    title.className = 'block text-sm text-slate-800 dark:text-slate-100';
    title.textContent = `${providerCaptureLabel(record.provider_id)} · ${String(record.model || '--')}`;
    const meta = document.createElement('p');
    meta.className = 'mt-1 text-xs text-slate-500 dark:text-slate-400 break-all';
    const when = record.started_at || record.captured_at;
    const timestamp = when
        ? new Date(Number(when) * 1000).toLocaleString(providerCaptureLocale())
        : '--';
    meta.textContent = `${timestamp} · ${t('capture_agent')} ${String(record.agent_id || '--')} · ${t('capture_run')} ${String(record.run_id || '--')}`;
    const metrics = document.createElement('p');
    metrics.className = 'mt-1 text-xs text-slate-400 dark:text-slate-500';
    const usage = record.usage || {};
    const metricParts = [providerCaptureStatusLabel(record.status)];
    if (
        record.duration_ms !== undefined
        && record.duration_ms !== null
        && Number.isFinite(Number(record.duration_ms))
    ) {
        metricParts.push(`${t('capture_duration')} ${Number(record.duration_ms)} ms`);
    }
    metricParts.push(
        `${t('capture_input_short')} ${Number(record.input_characters || 0)} ${t('capture_characters')}`,
        `${t('capture_output_short')} ${Number(record.output_characters || 0)} ${t('capture_characters')}`,
    );
    const totalTokens = providerCaptureUsageTotal(usage);
    if (totalTokens !== null) {
        metricParts.push(`${t('capture_tokens')} ${tokenNumber(totalTokens)}`);
    }
    metrics.textContent = metricParts.join(' · ');
    identity.append(title, meta, metrics);
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'provider-capture-open shrink-0 px-3 py-1.5 rounded-lg border border-slate-200 dark:border-white/10 text-xs font-medium text-primary-500 hover:bg-primary-50 dark:hover:bg-primary-900/20';
    open.textContent = t('capture_open');
    open.addEventListener('click', () => openProviderCaptureRecord(
        String(record.id || ''), record, open
    ));
    header.append(identity, open);
    const previews = document.createElement('div');
    previews.className = 'grid gap-4 lg:grid-cols-2';
    previews.append(
        providerCapturePreviewNode(t('capture_http_request_body'), record.input_preview || {}),
        providerCapturePreviewNode(t('capture_http_response_body'), record.output_preview || {}),
    );
    article.append(header, previews);
    return article;
}

function renderProviderCapture() {
    const selector = document.getElementById('provider-capture-provider');
    const previousSelection = selector.value;
    const nextProviderFingerprint = JSON.stringify(
        providerCaptureProviders.map(provider => [provider.id, provider.label])
    );
    if (nextProviderFingerprint !== providerCaptureProviderFingerprint) {
        selector.replaceChildren();
        providerCaptureProviders.forEach(provider => {
            const option = document.createElement('option');
            option.value = String(provider.id || '');
            option.textContent = String(provider.label || provider.id || '');
            selector.appendChild(option);
        });
        providerCaptureProviderFingerprint = nextProviderFingerprint;
    }
    const desired = providerCaptureSelectionTouched
        ? previousSelection
        : (providerCaptureState.provider_id || previousSelection);
    if (desired && providerCaptureProviders.some(provider => provider.id === desired)) {
        selector.value = desired;
    }
    selector.disabled = providerCaptureBusy || providerCaptureProviders.length === 0;

    const active = document.getElementById('provider-capture-active-target');
    active.textContent = providerCaptureState.provider_id
        ? providerCaptureLabel(providerCaptureState.provider_id)
        : t('capture_inactive');
    document.getElementById('provider-capture-retention').textContent = providerCaptureLimitsText(providerCaptureState.limits || {});

    const start = document.getElementById('provider-capture-start');
    start.textContent = providerCaptureState.provider_id ? t('capture_switch') : t('capture_start');
    start.disabled = providerCaptureBusy || providerCaptureProviders.length === 0;
    document.getElementById('provider-capture-stop').disabled = providerCaptureBusy || !providerCaptureState.provider_id;
    document.getElementById('provider-capture-clear').disabled = providerCaptureBusy || !(providerCaptureState.records || []).length;

    const records = document.getElementById('provider-capture-records');
    const nextRecords = Array.isArray(providerCaptureState.records)
        ? providerCaptureState.records
        : [];
    const recordProviderLabels = nextRecords.map(record => [
        String(record.provider_id || ''),
        providerCaptureLabel(record.provider_id),
    ]);
    const nextRecordFingerprint = JSON.stringify([nextRecords, recordProviderLabels]);
    if (nextRecordFingerprint !== providerCaptureRecordFingerprint) {
        records.replaceChildren();
        nextRecords.forEach(record => records.appendChild(providerCaptureRecordNode(record)));
        if (!records.children.length) {
            const empty = document.createElement('p');
            empty.className = 'p-8 text-center text-sm text-slate-400 dark:text-slate-500';
            empty.textContent = t('capture_empty');
            records.appendChild(empty);
        }
        providerCaptureRecordFingerprint = nextRecordFingerprint;
    }
}

function stopProviderCapturePolling() {
    providerCaptureGeneration += 1;
    if (providerCaptureTimer) clearTimeout(providerCaptureTimer);
    if (providerCaptureCatalogTimer) clearTimeout(providerCaptureCatalogTimer);
    providerCaptureTimer = null;
    providerCaptureCatalogTimer = null;
    const pollRequest = providerCaptureRequest;
    const catalogRequest = providerCaptureCatalogRequest;
    const mutationRequest = providerCaptureMutationRequest;
    providerCaptureRequest = null;
    providerCaptureCatalogRequest = null;
    providerCaptureMutationRequest = null;
    providerCaptureBusy = false;
    if (pollRequest) pollRequest.controller.abort();
    if (catalogRequest) catalogRequest.controller.abort();
    if (mutationRequest) mutationRequest.controller.abort();
    if (providerCaptureDetailController) providerCaptureDetailController.abort();
    providerCaptureDetailController = null;
}

function scheduleProviderCapturePoll(generation) {
    if (generation !== providerCaptureGeneration || currentView !== 'api-capture') return;
    if (providerCaptureTimer) clearTimeout(providerCaptureTimer);
    providerCaptureTimer = setTimeout(() => pollProviderCapture(generation), PROVIDER_CAPTURE_POLL_MS);
}

function scheduleProviderCaptureCatalogRetry(generation) {
    if (generation !== providerCaptureGeneration || currentView !== 'api-capture') return;
    if (providerCaptureCatalogTimer) clearTimeout(providerCaptureCatalogTimer);
    providerCaptureCatalogTimer = setTimeout(
        () => providerCaptureLoadProviders(generation),
        PROVIDER_CAPTURE_CATALOG_RETRY_MS,
    );
}

async function providerCaptureLoadProviders(generation = providerCaptureGeneration) {
    if (generation !== providerCaptureGeneration || currentView !== 'api-capture') return;
    if (providerCaptureCatalogRequest?.generation === generation) return;
    const owner = { generation, controller: new AbortController() };
    providerCaptureCatalogRequest = owner;
    providerCaptureSetCatalogStatus(t('capture_catalog_loading'));
    let retry = false;
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/providers`, {
            signal: owner.controller.signal,
        });
        const payload = await responseJson(response);
        if (payload.status !== 'success') {
            throw new Error(payload.message || 'Failed to load Providers');
        }
        const providers = (Array.isArray(payload.data) ? payload.data : [])
            .filter(provider => provider.configured === true);
        if (
            providerCaptureCatalogRequest !== owner
            || generation !== providerCaptureGeneration
            || currentView !== 'api-capture'
        ) return;
        providerCaptureProviders = providers;
        renderProviderCapture();
        providerCaptureSetCatalogStatus('');
    } catch (error) {
        if (
            error.name !== 'AbortError'
            && providerCaptureCatalogRequest === owner
            && generation === providerCaptureGeneration
        ) {
            retry = true;
            providerCaptureSetCatalogStatus(error.message || error, true);
        }
    } finally {
        if (providerCaptureCatalogRequest === owner) {
            providerCaptureCatalogRequest = null;
            if (retry) scheduleProviderCaptureCatalogRetry(generation);
        }
    }
}

async function pollProviderCapture(generation = providerCaptureGeneration) {
    if (generation !== providerCaptureGeneration || currentView !== 'api-capture') return;
    if (providerCaptureRequest?.generation === generation) return;
    const owner = { generation, controller: new AbortController() };
    providerCaptureRequest = owner;
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/provider-capture`, {
            signal: owner.controller.signal,
        });
        const payload = await responseJson(response);
        if (payload.status !== 'success') throw new Error(payload.message || 'Failed to load capture records');
        if (
            providerCaptureRequest !== owner
            || generation !== providerCaptureGeneration
            || currentView !== 'api-capture'
        ) return;
        providerCaptureState = payload.data || { provider_id: null, limits: {}, records: [] };
        renderProviderCapture();
        providerCaptureSetStatus('');
    } catch (error) {
        if (
            error.name !== 'AbortError'
            && providerCaptureRequest === owner
            && generation === providerCaptureGeneration
        ) {
            providerCaptureSetStatus(error.message || error, true);
        }
    } finally {
        if (providerCaptureRequest === owner) {
            providerCaptureRequest = null;
            scheduleProviderCapturePoll(generation);
        }
    }
}

function loadProviderCaptureView() {
    stopProviderCapturePolling();
    const generation = providerCaptureGeneration;
    providerCaptureSelectionTouched = false;
    providerCaptureSetStatus(t('capture_loading'));
    providerCaptureSetCatalogStatus(t('capture_catalog_loading'));
    renderProviderCapture();
    providerCaptureLoadProviders(generation);
    pollProviderCapture(generation);
}

async function mutateProviderCapture(path, options, successMessage) {
    if (providerCaptureMutationRequest) return;
    stopProviderCapturePolling();
    const generation = providerCaptureGeneration;
    if (currentView !== 'api-capture') return;
    const owner = { generation, controller: new AbortController() };
    providerCaptureMutationRequest = owner;
    providerCaptureBusy = true;
    renderProviderCapture();
    providerCaptureSetStatus('');
    try {
        const response = await fetch(`${PUBLIC_API_BASE}${path}`, {
            ...options,
            signal: owner.controller.signal,
        });
        const payload = await responseJson(response);
        if (payload.status !== 'success') throw new Error(payload.message || 'Capture update failed');
        if (
            providerCaptureMutationRequest !== owner
            || generation !== providerCaptureGeneration
            || currentView !== 'api-capture'
        ) return;
        providerCaptureState = payload.data || providerCaptureState;
        renderProviderCapture();
        providerCaptureSetStatus(successMessage);
        await pollProviderCapture(generation);
    } catch (error) {
        if (
            error.name !== 'AbortError'
            && providerCaptureMutationRequest === owner
            && generation === providerCaptureGeneration
            && currentView === 'api-capture'
        ) {
            providerCaptureSetStatus(error.message || error, true);
        }
    } finally {
        if (providerCaptureMutationRequest === owner) {
            providerCaptureMutationRequest = null;
            providerCaptureBusy = false;
            if (
                generation === providerCaptureGeneration
                && currentView === 'api-capture'
            ) {
                renderProviderCapture();
                scheduleProviderCapturePoll(generation);
                providerCaptureLoadProviders(generation);
            }
        }
    }
}

function startProviderCapture() {
    const providerId = document.getElementById('provider-capture-provider').value;
    if (!providerId) return;
    return mutateProviderCapture('/provider-capture', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider_id: providerId }),
    }, t('capture_saved'));
}

function stopProviderCapture() {
    return mutateProviderCapture('/provider-capture/target', { method: 'DELETE' }, t('capture_stopped'));
}

function clearProviderCaptureRecords() {
    return mutateProviderCapture('/provider-capture/records', { method: 'DELETE' }, t('capture_cleared'));
}

function providerCaptureModalIsOpen() {
    return !document.getElementById('provider-capture-modal').classList.contains('hidden');
}

function closeProviderCaptureModal(restoreFocus = true) {
    if (providerCaptureDetailController) providerCaptureDetailController.abort();
    providerCaptureDetailController = null;
    const modal = document.getElementById('provider-capture-modal');
    modal.classList.add('hidden');
    modal.classList.remove('flex');
    const app = document.getElementById('app');
    app.inert = false;
    app.removeAttribute('aria-hidden');
    const opener = providerCaptureModalOpener;
    providerCaptureModalOpener = null;
    if (restoreFocus) {
        const target = opener?.isConnected
            ? opener
            : document.querySelector('[data-view="api-capture"]');
        target?.focus();
    }
}

async function openProviderCaptureRecord(recordId, preview, opener) {
    if (!recordId) return;
    if (providerCaptureDetailController) providerCaptureDetailController.abort();
    const controller = new AbortController();
    providerCaptureDetailController = controller;
    providerCaptureModalOpener = opener?.isConnected ? opener : null;
    const modal = document.getElementById('provider-capture-modal');
    modal.classList.remove('hidden');
    modal.classList.add('flex');
    const app = document.getElementById('app');
    app.inert = true;
    app.setAttribute('aria-hidden', 'true');
    document.getElementById('provider-capture-modal-close').focus();
    document.getElementById('provider-capture-detail-meta').textContent = recordId;
    document.getElementById('provider-capture-detail-input').textContent = '';
    document.getElementById('provider-capture-detail-output').textContent = '';
    document.getElementById('provider-capture-detail-request-friendly').replaceChildren();
    document.getElementById('provider-capture-detail-response-friendly').replaceChildren();
    providerCaptureSetDetailStatus(t('capture_loading'));
    const truncation = document.getElementById('provider-capture-detail-truncation');
    const previewWasTruncated = Boolean(preview?.input_preview?.truncated || preview?.output_preview?.truncated || preview?.truncation);
    truncation.textContent = previewWasTruncated ? t('capture_detail_truncated') : '';
    truncation.classList.toggle('hidden', !previewWasTruncated);
    try {
        const response = await fetch(`${PUBLIC_API_BASE}/provider-capture/${encodeURIComponent(recordId)}`, { signal: controller.signal });
        const payload = await responseJson(response);
        if (payload.status !== 'success') throw new Error(payload.message || 'Failed to load capture detail');
        if (providerCaptureDetailController !== controller) return;
        const record = payload.data || {};
        document.getElementById('provider-capture-detail-meta').textContent = `${providerCaptureLabel(record.provider_id)} · ${String(record.model || '--')} · ${recordId}`;
        const requestBody = record.http_request && Object.prototype.hasOwnProperty.call(record.http_request, 'body')
            ? record.http_request.body
            : undefined;
        const responseBody = record.http_response && Object.prototype.hasOwnProperty.call(record.http_response, 'body')
            ? record.http_response.body
            : undefined;
        const inputSectionTitle = document.querySelector('#provider-capture-detail-input')?.previousElementSibling?.querySelector('h4');
        const outputSectionTitle = document.querySelector('#provider-capture-detail-output')?.previousElementSibling?.querySelector('h4');
        if (inputSectionTitle) inputSectionTitle.textContent = requestBody !== undefined ? t('capture_http_request_body') : t('capture_input');
        if (outputSectionTitle) outputSectionTitle.textContent = responseBody !== undefined ? t('capture_http_response_body') : t('capture_output');
        document.getElementById('provider-capture-detail-input').textContent = providerCaptureJson(
            requestBody !== undefined ? requestBody : null
        );
        document.getElementById('provider-capture-detail-output').textContent = providerCaptureJson(
            responseBody !== undefined ? responseBody : null
        );
        renderProviderCaptureRequestFriendly(requestBody);
        renderProviderCaptureResponseFriendly(responseBody);
        if (record.truncation) {
            truncation.textContent = `${t('capture_detail_truncated')} ${providerCaptureJson(record.truncation)}`;
            truncation.classList.remove('hidden');
        }
        providerCaptureSetDetailStatus('');
    } catch (error) {
        if (error.name !== 'AbortError') {
            providerCaptureSetDetailStatus(error.message || error, true);
        }
    } finally {
        if (providerCaptureDetailController === controller) providerCaptureDetailController = null;
    }
}

function trapProviderCaptureModalFocus(event) {
    if (!providerCaptureModalIsOpen()) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        closeProviderCaptureModal();
        return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(
        document.querySelectorAll('#provider-capture-modal button:not([disabled])')
    ).filter(element => element.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
    }
}

async function copyProviderCapturePane(elementId, button) {
    const value = document.getElementById(elementId)?.textContent || '';
    if (!value) return;
    try {
        await copyToClipboard(value);
        if (button) {
            const original = button.textContent;
            button.textContent = t('capture_copied');
            setTimeout(() => { button.textContent = original; }, 1200);
        }
    } catch (error) {
        providerCaptureSetDetailStatus(error.message || t('capture_copy_failed'), true);
    }
}

document.getElementById('provider-capture-start').addEventListener('click', startProviderCapture);
document.getElementById('provider-capture-stop').addEventListener('click', stopProviderCapture);
document.getElementById('provider-capture-clear').addEventListener('click', clearProviderCaptureRecords);
document.getElementById('provider-capture-provider').addEventListener('change', () => {
    providerCaptureSelectionTouched = true;
});
document.getElementById('provider-capture-modal-close').addEventListener('click', closeProviderCaptureModal);
document.getElementById('provider-capture-modal').addEventListener('keydown', trapProviderCaptureModalFocus);
document.getElementById('provider-capture-copy-input').addEventListener('click', event => copyProviderCapturePane('provider-capture-detail-input', event.currentTarget));
document.getElementById('provider-capture-copy-output').addEventListener('click', event => copyProviderCapturePane('provider-capture-detail-output', event.currentTarget));



// =====================================================================
// Logs View
// =====================================================================
let logEventSource = null;

function logLevelClass(line) {
    if (/\[CRITICAL\]/.test(line)) return 'log-line-critical';
    if (/\[ERROR\]/.test(line))    return 'log-line-error';
    if (/\[WARNING\]/.test(line))  return 'log-line-warning';
    if (/\[INFO\]/.test(line))     return 'log-line-info';
    if (/\[DEBUG\]/.test(line))    return 'log-line-debug';
    return '';
}

function getHiddenLevels() {
    const hidden = new Set();
    document.querySelectorAll('.log-filter-cb').forEach(function(cb) {
        if (!cb.checked) hidden.add('log-line-' + cb.dataset.level);
    });
    return hidden;
}

function applyLogFilter() {
    const hidden = getHiddenLevels();
    document.querySelectorAll('#log-output .log-line').forEach(function(span) {
        const level = span.classList[1] || '';
        span.style.display = hidden.has(level) ? 'none' : '';
    });
}

function appendLogLines(output, text) {
    const hidden = getHiddenLevels();
    let lastLevelClass = '';
    const lines = text.split('\n');
    lines.forEach(function(line, i) {
        if (i === lines.length - 1 && line === '') return;
        const span = document.createElement('span');
        const levelClass = logLevelClass(line) || lastLevelClass;
        if (logLevelClass(line)) lastLevelClass = levelClass;
        span.className = 'log-line ' + levelClass;
        span.textContent = line + '\n';
        if (hidden.has(levelClass)) span.style.display = 'none';
        output.appendChild(span);
    });
}

document.addEventListener('change', function(e) {
    if (e.target.classList.contains('log-filter-cb')) applyLogFilter();
});

let agentTopTimer = null;
let agentTopRequest = null;
let runtimeStatusTimer = null;
let runtimeStatusRequest = null;
let runtimeStatusPayload = null;

function stopRuntimeStatus() { clearTimeout(runtimeStatusTimer); runtimeStatusTimer = null; }

function renderRuntimeStatus(payload) {
    // Keep the last payload so a language switch can re-render this panel
    // without another request.
    runtimeStatusPayload = payload || null;
    const data = payload || {}, control = data.control || {}, runtime = data.runtime || {};
    const workers = Array.isArray(data.workers) ? data.workers : [];
    const state = document.getElementById('runtime-status-state');
    if (!state || !document.getElementById('runtime-status-workers')) return;
    state.textContent = String(runtime.status || control.state || '--');
    state.className = `agent-top-status ${String(runtime.status || control.state || 'idle').toLowerCase()}`;
    document.getElementById('runtime-status-clock').textContent = new Date().toLocaleTimeString();
    document.getElementById('runtime-status-summary').innerHTML = [
        ['CONTROL PID', control.pid ?? '--'], ['WORKERS', data.worker_count ?? 0], ['RUNTIME', runtime.status || '--'], ['CONTROL', control.control_ready ? 'READY' : String(control.state || '--')],
    ].map(([label, value]) => `<div class="agent-top-stat"><small>${label}</small><strong>${escapeHtml(String(value))}</strong></div>`).join('');
    document.getElementById('runtime-status-control').innerHTML = `<div class="runtime-status-control-grid"><div><small>PID</small><strong>${control.pid ?? '--'}</strong></div><div><small>STATE</small><strong>${escapeHtml(control.state || '--')}</strong></div><div><small>MODE</small><strong>${escapeHtml(control.effective_mode || '--')}</strong></div><div><small>BACKEND</small><strong>${escapeHtml(control.backend || '--')}</strong></div><div><small>SANDBOX</small><strong>${control.sandboxed ? 'yes' : 'no'}</strong></div><div><small>${t('runtime_status_user')}</small><strong>${escapeHtml(runtime.service_user || '--')}</strong></div><div class="runtime-status-wide"><small>${t('runtime_status_data_dir')}</small><strong title="${escapeHtml(t('runtime_status_identity_desc'))}">${escapeHtml(runtime.data_dir || '--')}</strong></div></div>`;
    const rows = workers.map(worker => `<tr><td>${agentTopStatus(worker.state).outerHTML}</td><td>${escapeHtml(String(worker.agent_id || '--'))}</td><td>${worker.pid ?? '--'}</td><td>${escapeHtml(worker.effective_mode || '--')}</td><td>${escapeHtml(worker.backend || '--')}</td><td>${worker.sandboxed ? 'yes' : 'no'}</td><td>${worker.generation_id ?? '--'}</td><td>${escapeHtml(worker.error || '--')}</td></tr>`);
    document.getElementById('runtime-status-workers').innerHTML = rows.join('') || '<tr><td colspan="8">No workers</td></tr>';
}

async function loadRuntimeStatusView(force = false) {
    if (runtimeStatusRequest && !force) return runtimeStatusRequest;
    stopRuntimeStatus();
    const error = document.getElementById('runtime-status-error');
    runtimeStatusRequest = Promise.all([
        window.fetch(`${PUBLIC_API_BASE}/runtime/status`, {credentials: 'same-origin'}).then(responseJson),
        window.fetch(`${PUBLIC_API_BASE}/runtime/workers`, {credentials: 'same-origin'}).then(responseJson),
    ]).then(([status, workers]) => {
        if (workers.status !== 'success') throw new Error(workers.message || 'Runtime workers unavailable');
        renderRuntimeStatus({...workers.data, runtime: status.status === 'success' ? status.data : workers.data?.runtime});
        error.classList.add('hidden');
    }).catch(reason => { error.textContent = String(reason.message || reason); error.classList.remove('hidden'); }).finally(() => { runtimeStatusRequest = null; if (currentView === 'runtime-monitor') runtimeStatusTimer = setTimeout(() => loadRuntimeStatusView(), 3000); });
    return runtimeStatusRequest;
}
window.loadRuntimeStatusView = loadRuntimeStatusView;
document.addEventListener('click', (event) => {
    const item = event.target.closest?.('[data-view="runtime-monitor"]');
    if (!item) return;
    event.preventDefault();
    navigateTo('runtime-monitor');
    loadRuntimeStatusView(true);
}, true);

function stopAgentTop() {
    if (agentTopTimer) clearTimeout(agentTopTimer);
    agentTopTimer = null;
}

function agentTopAge(timestamp, serverTime) {
    if (!timestamp) return '--';
    const seconds = Math.max(0, Math.floor(serverTime - timestamp));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
}

function agentTopStatus(value) {
    const status = document.createElement('span');
    const normalized = String(value || 'idle');
    status.className = `agent-top-status ${normalized}`;
    status.textContent = normalized;
    return status;
}

function renderAgentTop(data) {
    const summary = data.summary || {};
    const labels = [
        ['AGENTS', summary.agents], ['AVAILABLE', summary.available_agents],
        ['SESSIONS', summary.sessions], ['RUNS', summary.runs],
        ['ACTIVE', summary.active_runs], ['WAITING', summary.waiting_runs],
        ['FAILED', summary.failed_runs],
    ];
    const summaryRoot = document.getElementById('agent-top-summary');
    summaryRoot.replaceChildren(...labels.map(([label, value]) => {
        const card = document.createElement('div');
        card.className = 'agent-top-stat';
        const caption = document.createElement('small');
        caption.textContent = label;
        const count = document.createElement('strong');
        count.textContent = String(value ?? 0);
        card.append(caption, count);
        return card;
    }));
    document.getElementById('agent-top-clock').textContent = new Date(
        Number(data.server_time || Date.now() / 1000) * 1000
    ).toLocaleTimeString();

    const agentBody = document.getElementById('agent-top-agents');
    agentBody.replaceChildren();
    (data.agents || []).forEach(agent => {
        const row = document.createElement('tr');
        const status = document.createElement('td');
        status.appendChild(agentTopStatus(agent.status));
        const identity = document.createElement('td');
        const avatar = document.createElement('span');
        avatar.className = 'agent-top-avatar';
        appendAgentAvatar(avatar, agent.avatar);
        identity.append(avatar, document.createTextNode(agentDisplayName(agent)));
        const values = [agent.session_count, agent.run_count, agent.active_run_count, agent.failed_run_count];
        row.append(status, identity, ...values.map(value => {
            const cell = document.createElement('td');
            cell.textContent = String(value || 0);
            return cell;
        }));
        const last = document.createElement('td');
        last.textContent = agentTopAge(Number(agent.last_active_at || 0), Number(data.server_time || 0));
        row.appendChild(last);
        agentBody.appendChild(row);
    });
    if (!agentBody.children.length) {
        const row = agentBody.insertRow();
        const cell = row.insertCell();
        cell.colSpan = 7;
        cell.textContent = '--';
    }

    const runBody = document.getElementById('agent-top-runs');
    runBody.replaceChildren();
    (data.recent_runs || []).forEach(run => {
        const row = document.createElement('tr');
        const status = document.createElement('td');
        status.appendChild(agentTopStatus(run.status));
        const runId = document.createElement('td');
        runId.textContent = String(run.id || '').slice(0, 12);
        runId.title = String(run.id || '');
        const agent = document.createElement('td');
        agent.textContent = String(run.agent_id || '--');
        const session = document.createElement('td');
        session.textContent = String(run.session_title || run.session_id || '--');
        const updated = document.createElement('td');
        updated.textContent = agentTopAge(Number(run.updated_at || 0), Number(data.server_time || 0));
        row.append(status, runId, agent, session, updated);
        runBody.appendChild(row);
    });
    if (!runBody.children.length) {
        const row = runBody.insertRow();
        const cell = row.insertCell();
        cell.colSpan = 5;
        cell.textContent = '--';
    }
}

async function loadAgentTopView(force = false) {
    if (agentTopRequest && !force) return agentTopRequest;
    stopAgentTop();
    const error = document.getElementById('agent-top-error');
    agentTopRequest = fetch(`${PUBLIC_API_BASE}/agent-top`)
        .then(responseJson)
        .then(payload => {
            if (payload.status !== 'success') throw new Error(payload.message || 'Agent Top unavailable');
            renderAgentTop(payload.data || {});
            error.classList.add('hidden');
        })
        .catch(reason => {
            error.textContent = String(reason.message || reason);
            error.classList.remove('hidden');
        })
        .finally(() => {
            agentTopRequest = null;
            if (currentView === 'agent-top') {
                agentTopTimer = setTimeout(() => loadAgentTopView(), 3000);
            }
        });
    return agentTopRequest;
}

function startLogStream() {
    if (logEventSource) return;
    const output = document.getElementById('log-output');
    output.innerHTML = '';
    let received = false;

    logEventSource = openConsoleEventSource(`${PUBLIC_API_BASE}/logs`);
    logEventSource.onmessage = function(e) {
        let item;
        try { item = JSON.parse(e.data); } catch (_) { return; }

        if (item.type === 'log') {
            received = true;
            appendLogLines(output, item.content || '');
            output.scrollTop = output.scrollHeight;
        } else if (item.type === 'error') {
            output.textContent = item.message || 'Error loading logs';
        }
    };
    logEventSource.onerror = function() {
        logEventSource.close();
        logEventSource = null;
        // The snapshot ends the stream; an empty page must say so instead of
        // looking like a terminal that never connected.
        if (!received) output.textContent = t('logs_empty');
    };
}

function stopLogStream() {
    if (logEventSource) {
        logEventSource.close();
        logEventSource = null;
    }
}

// =====================================================================
// Local models — read-only status reported by the AI Gateway
// =====================================================================
const LOCAL_MODEL_TYPES = ['llm', 'embed'];
const localModelState = {
    generation: 0,
    requestSequence: 0,
    timer: null,
    types: {
        llm: { models: [], server: null, download: null },
        embed: { models: [], server: null, download: null },
    },
};

async function localModelRequest(url) {
    const response = await _consoleNativeFetch(url, {
        headers: { 'Content-Type': 'application/json' },
    });
    let payload;
    try {
        payload = await response.json();
    } catch (_) {
        if (response.ok) throw new Error(t('bibit_models_invalid_response'));
        payload = null;
    }
    if (!response.ok) {
        const envelope = payload && (payload.error || payload.detail);
        const detail = (typeof envelope === 'string' ? envelope : envelope && (envelope.message || envelope.detail)) || (payload && payload.message);
        throw new Error(typeof detail === 'string' ? detail : `HTTP ${response.status}`);
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
        throw new Error(t('bibit_models_invalid_response'));
    }
    if (!payload.data || typeof payload.data !== 'object' || Array.isArray(payload.data)) {
        throw new Error(t('bibit_models_invalid_response'));
    }
    return payload.data;
}

function validateLocalModels(payload) {
    if (!Array.isArray(payload.models) || !payload.models.every(model => {
        return model && typeof model === 'object' && !Array.isArray(model)
            && LOCAL_MODEL_TYPES.includes(model.model_type)
            && typeof model.name === 'string' && model.name.trim().length > 0
            && typeof model.backend === 'string'
            && (model.artifact === undefined || model.artifact === null || typeof model.artifact === 'string')
            && (model.path === undefined || model.path === null || typeof model.path === 'string')
            && (model.port === undefined || model.port === null
                || (Number.isInteger(model.port) && model.port > 0 && model.port <= 65535))
            && model.download && typeof model.download.status === 'string'
            && Number.isFinite(Number(model.download.progress))
            && model.server && typeof model.server.status === 'string';
    })) {
        throw new Error(t('bibit_models_invalid_response'));
    }
    if (!LOCAL_MODEL_TYPES.every(type =>
        payload.models.filter(model => model.model_type === type).length <= 1)) {
        throw new Error(t('bibit_models_invalid_response'));
    }
    return payload;
}

function applyLocalModels(payload) {
    const descriptor = validateLocalModels(payload);
    LOCAL_MODEL_TYPES.forEach(modelType => {
        const source = descriptor.models.find(model => model.model_type === modelType);
        const state = localModelState.types[modelType];
        state.models = source ? [{
            id: modelType,
            name: source.name,
            artifact: source.artifact || null,
            path: source.path || 'AI Gateway',
            backend: source.backend,
            registered: source.registered !== false,
            download: source.download,
            server: source.server,
        }] : [];
        state.server = source ? { status: source.server.status, model_id: modelType } : null;
        state.download = source ? source.download : null;
        renderLocalModelType(modelType);
    });
}

function localModelServerLabel(status) {
    if (status === 'running') return t('bibit_models_running');
    if (status === 'starting') return t('bibit_models_starting');
    if (status === 'loaded') return t('bibit_models_loaded');
    if (status === 'stopped') return t('bibit_models_stopped');
    return t('bibit_models_unknown');
}

function renderLocalModelType(modelType) {
    const state = localModelState.types[modelType];
    const list = document.getElementById(`bibit-model-${modelType}-list`);
    const status = document.getElementById(`bibit-model-${modelType}-status`);
    if (!list || !status) return;

    const serverStatus = state.server && state.server.status ? state.server.status : '';
    status.textContent = serverStatus ? localModelServerLabel(serverStatus) : '';
    status.className = `bibit-model-service-status ${serverStatus}`;

    const models = state.models.length ? state.models : [{
        id: null,
        name: t('bibit_models_not_registered'),
        path: '',
        download: null,
        server: null,
        unresolved: true,
    }];

    list.innerHTML = models.map(model => {
        const download = model.download || {};
        const progress = Math.max(0, Math.min(100, Math.round(Number(download.progress) || 0)));
        const progressMarkup = ['pending', 'downloading'].includes(download.status)
            ? `<div class="bibit-model-progress"><span style="width:${progress}%"></span></div><span class="bibit-model-progress-label">${progress}%</span>`
            : '';
        const serverLabel = model.unresolved
            ? t('bibit_models_stopped')
            : localModelServerLabel(String((model.server && model.server.status) || ''));
        const modelIdAttribute = model.id === null ? '' : ` data-model-id="${escapeAttribute(model.id)}"`;
        return `<article class="bibit-model-card"${modelIdAttribute} data-model-type="${modelType}">
            <div class="bibit-model-card-main">
                <div class="bibit-model-card-title-row">
                    <h4>${escapeHtml(String(model.artifact || model.name || model.id || ''))}</h4>
                </div>
                <p title="${escapeAttribute(model.path || '')}">${escapeHtml(String(model.path || ''))}</p>
                <div class="bibit-model-card-meta"><span>${escapeHtml(serverLabel)}</span>${progressMarkup}</div>
            </div>
        </article>`;
    }).join('');
}

function renderLocalModelAlert(message) {
    const alert = document.getElementById('bibit-models-alert');
    if (!alert) return;
    alert.hidden = !message;
    alert.innerHTML = message
        ? `<strong>${escapeHtml(t('bibit_models_service_unavailable'))}</strong><span class="bibit-model-alert-detail">${escapeHtml(message)}</span><small>${escapeHtml(t('bibit_models_service_unavailable_hint'))}</small>`
        : '';
}

function scheduleLocalModelRefresh(generation) {
    clearTimeout(localModelState.timer);
    if (generation !== localModelState.generation || currentView !== 'bibit-models') return;
    const active = LOCAL_MODEL_TYPES.some(type => {
        const state = localModelState.types[type];
        const download = state.download;
        return Boolean(download && ['pending', 'downloading'].includes(download.status))
            || (state.server && state.server.status === 'starting');
    });
    localModelState.timer = setTimeout(() => refreshLocalModels(generation), active ? 1000 : 3000);
}

async function refreshLocalModels(generation = localModelState.generation) {
    if (generation !== localModelState.generation || currentView !== 'bibit-models') return;
    const requestSequence = ++localModelState.requestSequence;
    try {
        const descriptor = await localModelRequest(`${PUBLIC_API_BASE}/local-models`);
        if (generation !== localModelState.generation || currentView !== 'bibit-models'
            || requestSequence !== localModelState.requestSequence) return;
        applyLocalModels(descriptor);
        renderLocalModelAlert('');
        scheduleLocalModelRefresh(generation);
    } catch (error) {
        if (generation === localModelState.generation && currentView === 'bibit-models') {
            renderLocalModelAlert(String((error && error.message) || error));
            scheduleLocalModelRefresh(generation);
        }
    }
}

async function loadLocalModelsView(_force = false) {
    if (currentView !== 'bibit-models') return;
    clearTimeout(localModelState.timer);
    const generation = ++localModelState.generation;
    renderLocalModelAlert('');
    LOCAL_MODEL_TYPES.forEach(renderLocalModelType);
    try {
        await refreshLocalModels(generation);
    } catch (_) {
        if (generation !== localModelState.generation) return;
        scheduleLocalModelRefresh(generation);
    }
}

function stopLocalModelPolling() {
    clearTimeout(localModelState.timer);
    localModelState.timer = null;
    localModelState.generation += 1;
    localModelState.requestSequence += 1;
}

// =====================================================================
// View Navigation Hook
// =====================================================================
const _origNavigateTo = navigateTo;
navigateTo = function(viewId) {
    let restoreModalFocus = false;
    // Stop log stream when leaving logs view
    if (currentView === 'logs' && viewId !== 'logs') stopLogStream();
    if (currentView === 'agent-top' && viewId !== 'agent-top') stopAgentTop();
    if (currentView === 'runtime-monitor' && viewId !== 'runtime-monitor') stopRuntimeStatus();
    if (currentView === 'bibit-models' && viewId !== 'bibit-models') stopLocalModelPolling();
    if (currentView === 'mcp' && viewId !== 'mcp') stopMcpPreparationWatch();
    if (currentView === 'api-capture' && viewId !== 'api-capture') {
        stopProviderCapturePolling();
        if (providerCaptureModalIsOpen()) {
            closeProviderCaptureModal(false);
            restoreModalFocus = true;
        }
    }

    _origNavigateTo(viewId);

    // Lazy-load view data
    if (viewId === 'agents') loadAgentsView();
    else if (viewId === 'models') loadModelsView();
    else if (viewId === 'bibit-models') loadLocalModelsView();
    else if (viewId === 'tools') loadToolsView();
    else if (viewId === 'plugins') loadPluginsView();
    else if (viewId === 'mcp') loadMcpView();
    else if (viewId === 'skills') loadSkillsView();
    else if (viewId === 'agent-top') loadAgentTopView();
    else if (viewId === 'runtime-monitor') loadRuntimeStatusView();
    else if (viewId === 'api-capture') loadProviderCaptureView();
    else if (viewId === 'logs') startLogStream();
    else if (viewId === 'security') { syncSecuritySyscallPanel('agents'); loadSecurityOtherView('agents'); }

    if (restoreModalFocus) {
        const destination = document.querySelector(`[data-view="${viewId}"]`);
        if (destination) {
            if (!destination.matches('button, a[href], [tabindex]')) {
                destination.tabIndex = -1;
            }
            destination.focus();
        }
    }
};

// Runtime-native security control center. All mutations are proxied through
// /security-api/invoke and ultimately executed by bianbu_security_client.
let _securityCatalog = [], _securityPolicy = null, _securityAuditFilter = 'all';
let _securityAuditPage = { cursor: 0, history: [] };
let _securityOtherRequest = 0;
let securitySelectedAgent = localStorage.getItem('bianbu.security.selected_agent') || '';
let securityAgents = [];
function securityAgentId(agent) { return String(agent?.id || agent?.agent_id || ''); }
function securityAgentName(agent) { return String(agent?.name || agent?.display_name || securityAgentId(agent)); }
function setSecurityAgentContext(agentId, reload = true) {
    securitySelectedAgent = String(agentId || '');
    if (securitySelectedAgent) localStorage.setItem('bianbu.security.selected_agent', securitySelectedAgent);
    else localStorage.removeItem('bianbu.security.selected_agent');
    const selects = [document.getElementById('security-agent-select'), document.getElementById('security-syscall-agent')];
    selects.forEach(select => { if (select && select.value !== securitySelectedAgent) select.value = securitySelectedAgent; });
    if (reload) {
        const tab = activeSecurityTab();
        syncSecuritySyscallPanel(tab);
        if (tab === 'syscalls') loadSecuritySyscallView();
        else if (tab === 'permissions') loadSecurityPermissionView(securitySelectedAgent);
        else loadSecurityOtherView(tab);
    }
}
function renderSecurityAgentOptions(agents) {
    securityAgents = agents || [];
    const preferred = securityAgents.some(a => securityAgentId(a) === securitySelectedAgent) ? securitySelectedAgent : securityAgentId(securityAgents[0]);
    securitySelectedAgent = preferred;
    [document.getElementById('security-agent-select'), document.getElementById('security-syscall-agent')].forEach(select => {
        if (!select) return;
        select.innerHTML = securityAgents.map(a => `<option value="${escapeHtml(securityAgentId(a))}">${escapeHtml(securityAgentName(a))}</option>`).join('') || '<option value="">暂无可用 Agent</option>';
        select.value = preferred;
    });
    if (preferred) localStorage.setItem('bianbu.security.selected_agent', preferred);
}
async function securityInvoke(operationId, params = {}) {
    // Older WebViews (and non-secure HTTP origins) may not expose
    // crypto.randomUUID.  The request id is only a correlation key, so use
    // the Runtime's existing compatibility generator in that case.
    const requestId = typeof generateSessionMessageId === 'function'
        ? generateSessionMessageId()
        : `security-${Date.now()}-${Math.random().toString(36).slice(2, 14)}`;
    const response = await fetch('/security-api/invoke', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({protocolVersion:1, serviceId:'security', requestId, operationId, params})});
    const data = await response.json();
    if (!data.ok) throw new Error(data.error?.message || 'securityd unavailable');
    return data.result || {};
}
async function loadSecuritySyscallView() {
    const status = document.getElementById('security-runtime-status'), agentSelect = document.getElementById('security-agent-select');
    if (!status || !agentSelect) return;
    let serviceOnline = false;
    try {
        const agents = await securityInvoke('agents.list.v1', {});
        serviceOnline = true;
        const list = agents.agents || agents.items || agents || [];
        renderSecurityAgentOptions(list);
        status.textContent = 'securityd 已连接'; status.className = 'text-xs px-3 py-1.5 rounded-full bg-emerald-100 dark:bg-emerald-900/30 text-emerald-600';
    } catch (e) {
        // Always resolve the initial loading placeholder, even when securityd
        // is offline.  Leaving it in place makes the control center look
        // stuck and hides the actionable service error shown below.
        renderSecurityAgentOptions([]);
        status.textContent = 'securityd 不可用'; status.className = 'text-xs px-3 py-1.5 rounded-full bg-red-100 dark:bg-red-900/30 text-red-600';
        const target = document.getElementById('security-other-content');
        if (target && !target.textContent.trim()) target.innerHTML = `<div class="rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-900/20 p-4 text-sm text-red-700 dark:text-red-300">安全服务暂不可用：${escapeHtml(e.message)}<br>请确认 securityd 已启动后点击刷新。</div>`;
    }
    try {
        _securityCatalog = (await securityInvoke('syscall-catalog.v1', {arch: document.getElementById('security-syscall-arch').value})).syscalls || [];
        await loadSecuritySyscallPolicy();
        await loadSecuritySyscallAudit();
    } catch (e) {
        if (!serviceOnline) { status.textContent = 'securityd 不可用'; status.className = 'text-xs px-3 py-1.5 rounded-full bg-red-100 dark:bg-red-900/30 text-red-600'; }
        const message = document.getElementById('security-syscall-message');
        if (message) { message.textContent = `安全服务暂不可用：${e.message}（点击“刷新”重试）`; message.className = 'text-sm text-red-600 dark:text-red-400'; }
        const list = document.getElementById('security-syscall-list');
        if (list) list.innerHTML = '<div class="col-span-full rounded-lg border border-red-200 dark:border-red-900/50 bg-red-50 dark:bg-red-900/20 p-4 text-sm text-red-700 dark:text-red-300">无法读取系统调用策略。securityd 恢复后点击“刷新”重试。</div>';
    }
}
async function loadSecuritySyscallPolicy() {
    const aid = securitySelectedAgent || document.getElementById('security-agent-select')?.value || document.getElementById('security-syscall-agent')?.value, arch = document.getElementById('security-syscall-arch').value;
    if (!aid) return; _securityPolicy = await securityInvoke('syscall-policy.get.v1', {agent_id: aid, arch});
    const additions = new Set(_securityPolicy.agent_additions || []), selected = new Set(additions);
    document.getElementById('security-syscall-list').innerHTML = `<div class="security-table-wrap"><table class="security-table"><thead><tr><th>启用</th><th>系统调用</th><th>编号</th><th>说明</th><th>类型</th></tr></thead><tbody>${_securityCatalog.map(s => { const name = s.name || s.syscall || s; return `<tr><td><input type="checkbox" value="${escapeHtml(String(name))}" ${selected.has(name)?'checked':''} class="security-syscall-check"></td><td><strong>${escapeHtml(String(name))}</strong></td><td>${escapeHtml(String(s.number ?? '-'))}</td><td>${escapeHtml(String(s.description || s.reason || '危险系统调用'))}</td><td><span class="security-pill">${selected.has(name)?'已拦截':'可拦截'}</span></td></tr>`; }).join('')}</tbody></table></div><div class="flex flex-wrap gap-3 text-xs text-slate-500 dark:text-slate-400 mt-3"><span>策略版本：${escapeHtml(String(_securityPolicy.policy_version ?? '-'))}</span><span>已应用版本：${escapeHtml(String(_securityPolicy.applied_policy_version ?? '-'))}</span><span class="${_securityPolicy.pending_restart?'text-amber-600 dark:text-amber-400':''}">${_securityPolicy.pending_restart?'待重启 Worker 生效':'已应用'}</span></div>`;
}
async function saveSecuritySyscallPolicy() { const aid = securitySelectedAgent, arch = document.getElementById('security-syscall-arch').value; const additions = [...document.querySelectorAll('.security-syscall-check:checked')].map(x=>x.value); const msg = document.getElementById('security-syscall-message'); try { const result = await securityInvoke('syscall-policy.agent.update.v1',{agent_id:aid,arch,additions}); msg.textContent = `已保存，策略版本 ${result.version || result.policy_version || '-'}；重启 Worker 后生效`; await loadSecuritySyscallPolicy(); } catch(e) { msg.textContent = e.message; } }
async function loadSecuritySyscallAudit() { const target=document.getElementById('security-syscall-audit-list'); if(!target) return; try { const result=await securityInvoke('syscall-audit.events.v1',{agent_id:securitySelectedAgent||'',limit:100,cursor:0}); const rows=Array.isArray(result)?result:(result.events||result.records||result.value||[]); target.innerHTML=rows.length?`<table class="security-table"><thead><tr><th>时间</th><th>Agent</th><th>系统调用</th><th>PID</th><th>后端</th><th>结果</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${escapeHtml(securityClockTime(r.ts) || r.ts || '-')}</td><td>${escapeHtml(r.worker_agent_id||r.agent_id||'-')}</td><td><strong>${escapeHtml(r.syscall||'-')}</strong> (${escapeHtml(String(r.number??'-'))})</td><td>${escapeHtml(String(r.pid??'-'))}</td><td>${escapeHtml(r.backend||'-')}</td><td>${escapeHtml(r.action||'deny')}</td></tr>`).join('')}</tbody></table>`:'<div class="text-sm text-slate-500 p-3">暂无系统调用拦截事件</div>'; } catch(e) { target.innerHTML=`<div class="text-sm text-red-600 dark:text-red-400 p-3">无法读取独立审计：${escapeHtml(e.message)}</div>`; } }
function securityTimeValue(value) {
    // securityd 的 last_seen/installed_at/审计时间都是 ISO-8601 UTC。老数据里的裸
    // 时间 "YYYY-MM-DD HH:MM:SS" 同样是 UTC，必须补上 Z 再解析，否则浏览器按本地
    // 时间解析，展示出来的时间会比真实时间早一个时区（CST 差 8 小时）。
    const text = String(value ?? '').trim();
    if (!text) return null;
    const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text);
    const parsed = Date.parse(hasZone ? text : text.replace(' ', 'T') + 'Z');
    return Number.isFinite(parsed) ? parsed : null;
}
function securityClockTime(value) {
    const timestamp = securityTimeValue(value);
    if (timestamp === null) return '';
    const date = new Date(timestamp), pad = item => String(item).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
        + `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function securityActivityTime(value, now = Date.now()) {
    // 展示规则和「最近活动」的语义对齐：刚发生说「刚刚」，一小时内说几分钟前，
    // 更早给绝对时间（当天省略日期），全部按浏览器本地时区显示。
    const timestamp = securityTimeValue(value);
    if (timestamp === null) return '';
    const elapsed = now - timestamp;
    if (elapsed >= 0 && elapsed < 60000) return currentLang === 'zh' ? '刚刚' : 'just now';
    if (elapsed >= 60000 && elapsed < 3600000) {
        const minutes = Math.floor(elapsed / 60000);
        return currentLang === 'zh'
            ? `${minutes} 分钟前`
            : `${minutes} ${minutes === 1 ? 'minute' : 'minutes'} ago`;
    }
    const clock = securityClockTime(value);
    const sameDay = new Date(now).toDateString() === new Date(timestamp).toDateString();
    return sameDay ? clock.slice(11) : clock;
}
function securityActivityCell(agent) {
    // 没有活动证据时明确写「暂无活动」，并把安装时间放进悬浮提示：安装时间不是
    // 活动时间，直接顶替会让用户拿它和实际使用时间一对就对不上。
    const lastSeen = securityActivityTime(agent?.last_seen);
    if (lastSeen) return escapeHtml(lastSeen);
    const installed = securityActivityTime(agent?.installed_at);
    const label = currentLang === 'zh' ? '暂无活动' : 'No activity yet';
    const title = installed
        ? ` title="${escapeHtml((currentLang === 'zh' ? '安装于 ' : 'Installed ') + installed)}"`
        : '';
    return `<span class="text-slate-400"${title}>${escapeHtml(label)}</span>`;
}
async function loadSecurityAuditPage(aid, target, request, reset) {
    if (reset) _securityAuditPage = { cursor: 0, history: [] };
    const page = _securityAuditPage;
    target.textContent = '正在读取审计日志…';
    try {
        const result = await securityInvoke('audit.query.v1', {
            agent_id: aid, filter: _securityAuditFilter, limit: 100,
            cursor: page.cursor,
            ...(page.snapshot === undefined ? {} : { snapshot_seq: page.snapshot }),
        });
        if (request !== _securityOtherRequest) return;
        if (!Array.isArray(result.records) || result.records.length > 100 ||
            !Number.isSafeInteger(result.snapshot_seq) || result.snapshot_seq < 0 ||
            typeof result.has_more !== 'boolean' ||
            (result.has_more && (!Number.isSafeInteger(result.next_cursor) || result.next_cursor <= 0))) {
            throw new Error('审计分页接口不可用，请更新 Security 和 Runtime 服务');
        }
        page.snapshot = result.snapshot_seq;
        page.next = result.has_more ? result.next_cursor : null;
        target.innerHTML = `<div class="security-section-heading"><h3>审计日志</h3><span class="security-pill">第 ${page.history.length + 1} 页 · ${result.records.length} 条记录</span></div>
          <div class="security-toolbar"><select id="security-audit-filter" class="security-config-control">
          ${[['all','全部事件'],['hook','安全 Hook'],['deny','仅拒绝']].map(([value,label]) => `<option value="${value}" ${_securityAuditFilter === value ? 'selected' : ''}>${label}</option>`).join('')}</select>
          <div><button id="security-audit-refresh" class="security-button secondary">刷新</button>
          <button id="security-audit-verify" class="security-button secondary">校验哈希链</button><span id="security-audit-verify-status" class="security-pill">未校验</span></div></div>
          <div class="security-table-wrap"><table class="security-table"><thead><tr><th>序号</th><th>时间</th><th>操作</th><th>目标</th><th>结果</th></tr></thead><tbody>
          ${result.records.map(r => `<tr>${[r.seq,securityClockTime(r.ts) || r.ts, r.op_type, r.target, r.result].map(v => `<td>${escapeHtml(v ?? '-')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>
          <div class="security-toolbar"><button id="security-audit-prev" class="security-button secondary" ${page.history.length ? '' : 'disabled'}>上一页</button>
          <button id="security-audit-next" class="security-button secondary" ${result.has_more ? '' : 'disabled'}>下一页</button></div>`;
    } catch (error) {
        if (request !== _securityOtherRequest) return;
        target.innerHTML = `<p>${escapeHtml(error.message)}</p><button id="security-audit-refresh" class="security-button secondary">重试</button>`;
    }
}
// 能力管理只有工具一个维度：每个工具一条 allow / ask / deny 策略；工具目录来自
// securityd 的权威目录（按 Agent 对接的 framework 取 runtime 或 hermes 的工具）。
const SECURITY_TOOL_POLICIES = ['allow', 'ask', 'deny'];
// DLP 动作在三态之外多一个 mask（脱敏后放行）。
const SECURITY_DLP_ACTIONS = ['allow', 'ask', 'deny', 'mask'];
const SECURITY_PERMISSION_GROUP_LABELS = {
    read: '可读范围', write: '可写范围', network: '网络访问范围',
    syscall: '系统调用黑名单', custom: '自定义补充项',
};
function securityToolPolicy(value) {
    const policy = String(value || '').toLowerCase();
    return SECURITY_TOOL_POLICIES.includes(policy) ? policy : 'allow';
}
function securityDlpAction(value) {
    const action = String(value || '').toLowerCase();
    return SECURITY_DLP_ACTIONS.includes(action) ? action : 'mask';
}
function securityPolicyOptions(policies, selected) {
    return policies.map(value => `<option value="${value}" ${value === selected ? 'selected' : ''}>${value}</option>`).join('');
}
function activeSecurityTab() {
    return document.querySelector('.security-tab.border-b-2')?.dataset.securityTab || 'agents';
}
// 系统调用黑名单与安全权限设置各自是独立页面：前者是内核 seccomp 拒绝表，
// 后者决定「调用之后能碰什么」并把执行边界推导出来，混在能力管理页里反而不好找。
const SECURITY_PANELS = {syscalls: 'security-panel-syscalls', permissions: 'security-panel-permissions'};
function syncSecuritySyscallPanel(tab) {
    const other = document.getElementById('security-panel-other');
    for (const [name, id] of Object.entries(SECURITY_PANELS)) {
        const panel = document.getElementById(id);
        if (panel) panel.classList.toggle('hidden', tab !== name);
    }
    if (other) other.classList.toggle('hidden', tab === 'syscalls' || tab === 'permissions');
}
function securityRangePaths(value) {
    if (Array.isArray(value)) return value.join('\n');
    return ((value || {}).include || []).join('\n');
}
function securityRangeExcludes(value) {
    if (Array.isArray(value) || !value) return '';
    return (value.exclude || []).join('\n');
}
function securityPermissionPanel(settings, locked, error) {
    if (!settings) {
        // 只有真正拿到接口却读失败时才值得说「未提供」；否则把那句误导性
        // 文案连同真实原因一起给出，排查时不必再去猜是哪一环断了。
        const detail = error ? `<p class="security-permission-hint">读取失败：${escapeHtml(error)}</p>` : '';
        return `<section class="security-subpanel security-permissions"><h4>安全权限设置</h4><p>当前 securityd 未提供安全权限设置接口，请升级 securityd 后重试。</p>${detail}</section>`;
    }
    const network = settings.network || {};
    const modeLabel = network.mode === 'whitelist' ? '白名单' : network.mode === 'deny_all' ? '全部拒绝' : network.mode === 'learning' ? '学习模式' : '允许全部';
    const boundary = settings.execution_mode || {};
    const boundaryMode = boundary.mode || 'host';
    const reasons = (boundary.reasons || []).join('、');
    const boundaryText = boundaryMode === 'sandbox'
        ? `已声明 ${escapeHtml(reasons || '安全设置')}，需要沙箱命名空间/seccomp 落实；重启 Worker 后生效。`
        : '未声明额外安全设置，在宿主上以受约束的 systemd 单元运行。';
    return `<section class="security-subpanel security-permissions">
      <div class="security-form-heading"><div><h4>安全权限设置</h4><p>能力管理决定「这个 tool 能不能调」；这里决定「调用之后能碰什么」。可读/可写范围都是「包含 + 排除」：包含项列出允许触碰的目录，排除项从其中挖掉子目录——可读排除＝完全不可见，可写排除＝仍可读但不可写。声明了任何一项就走 sandbox，否则走 host。</p></div></div>
      <div class="security-mode-control security-capability-mode ${boundaryMode === 'sandbox' ? 'is-yolo' : ''}"><div class="security-mode-card__content"><h4>🧱 执行模式 <span class="security-mode-card__status">${escapeHtml(boundaryMode)}</span></h4><p>${boundaryText}</p></div></div>
      <div class="security-permission-grid">
        <label>可读范围 · 包含<textarea id="security-permission-read" rows="3" class="security-config-control" placeholder="/opt/data">${escapeHtml(securityRangePaths(settings.read))}</textarea></label>
        <label>可读范围 · 排除<textarea id="security-permission-read-exclude" rows="3" class="security-config-control" placeholder="/opt/data/secret">${escapeHtml(securityRangeExcludes(settings.read))}</textarea></label>
        <label>可写范围 · 包含<textarea id="security-permission-write" rows="3" class="security-config-control" placeholder="/var/lib/app">${escapeHtml(securityRangePaths(settings.write))}</textarea></label>
        <label>可写范围 · 排除<textarea id="security-permission-write-exclude" rows="3" class="security-config-control" placeholder="/var/lib/app/cache">${escapeHtml(securityRangeExcludes(settings.write))}</textarea></label>
        <label class="security-permission-wide">网络访问范围<span class="security-permission-hint">当前模式：${escapeHtml(modeLabel)}。填写域名后按白名单模式保存。</span><textarea id="security-permission-network" rows="3" class="security-config-control" placeholder="api.example.com">${escapeHtml((network.domains || []).join('\n'))}</textarea></label>
      </div>
      <div class="security-save-row"><span id="security-permission-status" class="security-form-status"></span><button id="security-permission-save" class="security-button" ${locked}>保存安全权限设置</button></div>
    </section>`;
}
function securityPermissionGroups(aid) {
    const paths = id => (document.getElementById(id)?.value || '')
        .split('\n').map(item => item.trim()).filter(Boolean);
    const domains = paths('security-permission-network');
    return [
        {group: 'read', value: {include: paths('security-permission-read'),
                                exclude: paths('security-permission-read-exclude')}},
        {group: 'write', value: {include: paths('security-permission-write'),
                                 exclude: paths('security-permission-write-exclude')}},
        {group: 'network', mode: domains.length ? 'whitelist' : 'allow_all', domains},
    ];
}
async function saveSecurityPermissionSettings(aid) {
    const status = document.getElementById('security-permission-status');
    const groups = securityPermissionGroups(aid);
    if (status) status.textContent = '正在保存…';
    try {
        for (const item of groups) {
            if (item.group === 'network') {
                await securityInvoke('permissions.set-group.v1', {agent_id: aid, group: 'network', mode: item.mode, domains: item.domains});
            } else {
                await securityInvoke('permissions.set-group.v1', {agent_id: aid, group: item.group, value: item.value});
            }
        }
        if (status) status.textContent = '已保存';
        await loadSecurityPermissionView(aid);
    } catch (error) {
        if (status) status.textContent = error.message;
    }
}
function securityToolPolicyPanel(rows, mode) {
    const yolo = mode === 'yolo';
    const counts = SECURITY_TOOL_POLICIES.map(policy => `${rows.filter(row => row.policy === policy).length} ${policy}`).join(' · ');
    const locked = yolo ? 'disabled' : '';
    return `<div class="security-section-heading"><div><span class="security-eyebrow">POLICY CONTROL</span><h3>能力管理</h3><p>每个工具一条策略：allow 放行 / ask 确认 / deny 拒绝。</p></div><div class="security-heading-actions"><span class="security-pill">${counts}</span><button id="security-other-retry" class="security-button secondary">刷新</button></div></div>
    <section id="security-capability-mode" class="security-mode-control security-capability-mode ${yolo ? 'is-yolo' : ''}"><div class="security-mode-card__content"><h4>🛡 Agent 安全模式 <span class="security-mode-card__status ${yolo ? 'security-mode-card__status--risk' : ''}">${yolo ? 'YOLO · 高风险' : '标准模式'}</span></h4><p>${yolo ? '工具默认放行；跳过 DLP 扫描、网络限制与审批，仅保留审计记录。' : '按每个工具的 allow / ask / deny 策略执行。'}</p>${yolo ? '<span class="security-mode-card__risk">高风险模式已启用</span>' : ''}</div><div class="security-mode-card__action"><button id="security-capability-mode-toggle" class="security-button ${yolo ? 'security-button--secondary' : 'security-button--danger'}">${yolo ? '关闭 YOLO 模式' : '⚠ 启用 YOLO 模式'}</button>${yolo ? '' : '<span class="security-mode-card__action-hint">点击后将跳过工具确认</span>'}</div></section>
    <div class="security-table-wrap"><table class="security-table"><thead><tr><th>工具</th><th>策略</th><th>框架</th><th>说明</th></tr></thead><tbody>${rows.map(row => `<tr><td><strong>${escapeHtml(row.tool)}</strong></td><td><select class="security-tool-policy security-config-control" data-tool="${escapeAttribute(row.tool)}" ${locked}>${securityPolicyOptions(SECURITY_TOOL_POLICIES, row.policy)}</select></td><td>${escapeHtml(row.framework || '-')}</td><td><small>${escapeHtml(row.summary || '')}</small></td></tr>`).join('')}</tbody></table></div>
    `;
}
async function loadSecurityPermissionView(aid) {
    const target = document.getElementById('security-permission-content');
    if (!target) return;
    if (!aid) { target.textContent = '请选择 Agent'; return; }
    let settings = null, settingsError = '';
    try { settings = await securityInvoke('permissions.get.v1', {agent_id: aid}); }
    catch (error) { settingsError = error && error.message ? error.message : String(error); }
    target.innerHTML = securityPermissionPanel(settings, '', settingsError);
}
async function loadSecurityOtherView(tab, resetAudit = true) {
    if (tab === 'syscalls' || tab === 'permissions') return;
    const request = ++_securityOtherRequest;
    const aid = securitySelectedAgent || document.getElementById('security-agent-select')?.value || document.getElementById('security-syscall-agent')?.value;
    const target = document.getElementById('security-other-content'); if (!target) return;
    if (tab === 'audit' && aid) return loadSecurityAuditPage(aid, target, request, resetAudit);
    if (tab === 'agents') {
      try { const result = await securityInvoke('agents.list.v1', {}); target.innerHTML=`<div class="security-section-heading"><div><span class="security-eyebrow">RUNTIME INVENTORY</span><h3>已注册 Agent</h3><p>查看身份、版本、状态和能力数量。暂停请求仅记录审计并发送告警，不会停止 Agent 执行。</p></div><button id="security-other-retry" class="security-button secondary">刷新</button></div><div class="security-table-wrap"><table class="security-table"><thead><tr><th>Agent</th><th>版本</th><th>状态</th><th>能力数</th><th>最近活动</th><th>操作</th></tr></thead><tbody>${(result.agents||[]).map(a=>`<tr><td><strong>${escapeHtml(a.name||a.agent_id)}</strong><small>${escapeHtml(a.agent_id||'')}</small></td><td>${escapeHtml(a.version||'-')}</td><td><span class="security-pill">${escapeHtml(a.status||'active')}</span></td><td>${escapeHtml(a.capability_count||0)}</td><td>${securityActivityCell(a)}</td><td><button class="security-agent-inspect security-link-button" data-agent="${escapeHtml(a.agent_id)}">查看</button> <button class="security-agent-suspend security-link-button security-link-button--danger" data-agent="${escapeHtml(a.agent_id)}">发送暂停请求</button></td></tr>`).join('')}</tbody></table></div>`; } catch(e) { target.innerHTML=`<div class="security-error-state">安全服务暂不可用：${escapeHtml(e.message)}<br><button id="security-other-retry" class="security-button secondary">重试</button></div>`; } return;
    }
    if (!aid) { target.textContent = '请选择 Agent'; return; }
    try {
      if (tab === 'capabilities') {
        let mode = 'normal';
        try { mode = String((await securityInvoke('agents.security-mode.get.v1', {agent_id: aid})).mode || 'normal'); } catch (_) { mode = 'normal'; }
        const catalog = await securityInvoke('capabilities.catalog.v1', {agent_id: aid});
        const detail = await securityInvoke('agents.get.v1', {agent_id: aid});
        if (request !== _securityOtherRequest) return;
        const declared = new Map((detail.capabilities || []).map(item => [String(item.tool || ''), securityToolPolicy(item.policy)]));
        const tools = new Map();
        (catalog.capabilities || []).forEach(item => {
          const tool = String(item.tool || ''); if (!tool) return;
          tools.set(tool, {tool, policy: securityToolPolicy(item.default_policy), framework: String(item.framework || ''), summary: String(item.summary || '')});
        });
        declared.forEach((policy, tool) => { if (tool) tools.set(tool, {...(tools.get(tool) || {}), tool, policy}); });
        const rows = [...tools.values()].sort((left, right) => left.tool.localeCompare(right.tool));
        target.innerHTML = securityToolPolicyPanel(rows, mode);
      } else if (tab === 'dlp') {
        let mode = 'normal';
        try { mode = String((await securityInvoke('agents.security-mode.get.v1', {agent_id: aid})).mode || 'normal'); } catch (_) { mode = 'normal'; }
        const result = await securityInvoke('dlp.matrix.v1', {agent_id: aid});
        let stats = {total: 0};
        try { stats = await securityInvoke('dlp.stats.v1', {agent_id: aid}); } catch (_) {}
        if (request !== _securityOtherRequest) return;
        const yolo = mode === 'yolo';
        const rows = result.rows || result.policies || [];
        target.innerHTML = `<div class="security-section-heading"><div><span class="security-eyebrow">DATA PROTECTION</span><h3>DLP / 个人信息策略</h3><p>${yolo ? 'YOLO 不执行 DLP 扫描；保留 normal 模式规则和审计记录。' : '控制敏感数据的放行、确认、脱敏或拦截行为。'}</p></div><span class="security-pill">${stats.total || 0} 次干预</span></div><div class="security-table-wrap"><table class="security-table"><thead><tr><th>PII 类型</th><th>${yolo ? '已保存动作（当前不执行）' : '生效动作'}</th><th>操作</th></tr></thead><tbody>${rows.map(r => `<tr><td><strong>${escapeHtml(r.pii_type)}</strong></td><td><select ${yolo ? 'disabled' : ''} class="security-dlp-action security-config-control" data-pii="${escapeAttribute(r.pii_type)}">${securityPolicyOptions(SECURITY_DLP_ACTIONS, securityDlpAction(r.action))}</select></td><td><button ${yolo ? 'disabled' : ''} class="security-dlp-reset security-link-button" data-pii="${escapeAttribute(r.pii_type)}">恢复默认</button></td></tr>`).join('')}</tbody></table></div><div class="security-subpanel"><h4>DLP 文本试扫</h4><textarea id="security-dlp-text" ${yolo ? 'disabled' : ''} class="security-dlp-textarea" rows="3" placeholder="输入包含手机号、身份证号的文本"></textarea><button id="security-dlp-scan" ${yolo ? 'disabled' : ''} class="mt-2 security-button">扫描文本</button><pre id="security-dlp-result" class="text-xs mt-2"></pre></div>`;
      } else {
        target.textContent = '';
      }
    }
    catch (e) { if (request === _securityOtherRequest) target.textContent = e.message; }
}
document.addEventListener('change', e => { if(e.target.id==='security-agent-select') setSecurityAgentContext(e.target.value); if(e.target.id==='security-syscall-agent') setSecurityAgentContext(e.target.value); if(e.target.id==='security-syscall-arch') loadSecuritySyscallPolicy(); });
document.addEventListener('change', e => {
 if(e.target.disabled) return;
 if(e.target.classList.contains('security-tool-policy')) { const tool=e.target.dataset.tool; securityInvoke('capabilities.update.v1',{agent_id:securitySelectedAgent,tool,policy:e.target.value}).then(()=>loadSecurityOtherView('capabilities')).catch(x=>{ alert(x.message); loadSecurityOtherView('capabilities'); }); }
 if(e.target.classList.contains('security-dlp-action')) { const aid=securitySelectedAgent; securityInvoke('dlp.policy.update.v1',{agent_id:aid,pii_type:e.target.dataset.pii,action:e.target.value}).catch(x=>alert(x.message)); }
 if(e.target.id==='security-audit-filter') { _securityAuditFilter=e.target.value; loadSecurityOtherView('audit'); }
});
document.addEventListener('click', e => {
 const tab = e.target.closest('.security-tab');
 if (tab && !e.target.disabled) {
  document.querySelectorAll('.security-tab').forEach(node => node.classList.remove('border-b-2','border-primary-500','text-primary-600'));
  tab.classList.add('border-b-2','border-primary-500','text-primary-600');
  const name = tab.dataset.securityTab;
  syncSecuritySyscallPanel(name);
  if (name === 'syscalls') loadSecuritySyscallView();
  else if (name === 'permissions') loadSecurityPermissionView(securitySelectedAgent || document.getElementById('security-agent-select')?.value);
  else loadSecurityOtherView(name);
  return;
 }
 if(e.target.disabled) return;
 const aid=securitySelectedAgent;
 if(e.target.id==='security-other-retry' || e.target.id==='security-global-refresh') { const tab=activeSecurityTab(); if(tab==='syscalls') loadSecuritySyscallView(); else if(tab==='permissions') loadSecurityPermissionView(aid); else loadSecurityOtherView(tab); }
 if(e.target.id==='security-syscall-reset') { const aid=securitySelectedAgent; const arch=document.getElementById('security-syscall-arch')?.value||'x86_64'; securityInvoke('syscall-policy.agent.reset.v1',{agent_id:aid,arch}).then(()=>loadSecuritySyscallPolicy()).catch(x=>alert(x.message)); }
 if(e.target.id==='security-capability-mode-toggle') { const enabling=!document.getElementById('security-capability-mode')?.classList.contains('is-yolo'); if(enabling && !window.confirm('启用 YOLO 模式后，所有工具默认放行，跳过 DLP 扫描与审批，仅保留审计。确认启用？')) return; securityInvoke('agents.security-mode.update.v1',{agent_id:aid,mode:enabling?'yolo':'normal'}).then(()=>loadSecurityOtherView('capabilities')).catch(x=>alert(x.message)); }
 if(e.target.id==='security-syscall-refresh') loadSecuritySyscallView();
 if(e.target.id==='security-syscall-audit-refresh') loadSecuritySyscallAudit();
 if(e.target.id==='security-syscall-save') saveSecuritySyscallPolicy();
 if(e.target.id==='security-permission-save') saveSecurityPermissionSettings(aid);
 if(e.target.classList.contains('security-agent-suspend')) {
   const button = e.target; button.disabled = true;
   securityInvoke('agents.suspend.v1',{agent_id:button.dataset.agent})
     .then(() => { alert('暂停请求已发送。Agent 未停止，仍可能继续执行。'); return loadSecurityOtherView('agents'); })
     .catch(error => alert(error.message)).finally(() => { button.disabled = false; });
 }
 if(e.target.classList.contains('security-agent-inspect')) { setSecurityAgentContext(e.target.dataset.agent, false); document.querySelector('[data-security-tab="capabilities"]')?.click(); }
 if(e.target.classList.contains('security-dlp-reset')) securityInvoke('dlp.policy.delete.v1',{agent_id:aid,pii_type:e.target.dataset.pii}).then(()=>loadSecurityOtherView('dlp'));
 if(e.target.id==='security-dlp-scan') securityInvoke('dlp.scan.v1',{agent_id:aid,text:document.getElementById('security-dlp-text').value}).then(r=>{ const el=document.getElementById('security-dlp-result'); if(el) el.innerHTML=`发现 ${r.findings?.length||0} 项敏感信息${r.masked_text?`；脱敏结果：${escapeHtml(r.masked_text)}`:''}`; }).catch(x=>{ const el=document.getElementById('security-dlp-result'); if(el) el.textContent=x.message; });
 if(e.target.id==='security-audit-verify') securityInvoke('audit.verify.v1',{agent_id:aid}).then(r=>{ const el=document.getElementById('security-audit-verify-status'); if(el){ el.textContent=r.intact?'哈希链完整':'校验失败'; el.className=`security-pill ${r.intact?'security-pill--ok':'security-pill--error'}`; } }).catch(x=>{ const el=document.getElementById('security-audit-verify-status'); if(el){ el.textContent=x.message; el.className='security-pill security-pill--error'; } });
 if(e.target.id==='security-audit-refresh') loadSecurityOtherView('audit');
 if(e.target.id==='security-audit-next' && _securityAuditPage.next) {
   _securityAuditPage.history.push(_securityAuditPage.cursor);
   _securityAuditPage.cursor = _securityAuditPage.next;
   loadSecurityOtherView('audit', false);
 }
 if(e.target.id==='security-audit-prev' && _securityAuditPage.history.length) {
   _securityAuditPage.cursor = _securityAuditPage.history.pop();
   loadSecurityOtherView('audit', false);
 }
});

function initApp() {
    applyI18n();
    _applyInputTooltips();
    _restoreSessionPanel();

    document.getElementById('sidebar-version').textContent = 'Bianbu Agent';
    chatInput.focus();
}

// =====================================================================
// Initialization
// =====================================================================
applyTheme();
applyI18n();

initApp();

requestAnimationFrame(() => {
    document.body.classList.add('transition-colors', 'duration-200');
});
