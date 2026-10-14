#!/usr/bin/env python3
"""Prepared acceptance driver. Default plan is offline; mutations require --execute.
Use an SSH tunnel: ssh -N -L 19899:127.0.0.1:9899 bianbu@10.0.91.146
"""
import argparse
import json
from pathlib import Path
import time
import urllib.request
import uuid


def prompt(case, workspace):
    common = f'本次是完整的一次提交验收。所有项目操作仅在 {workspace} 下进行；自行创建目录。不要修改其他现有项目或服务，不使用 sudo，不删除已有资料。无需等待我说继续，持续完成并提供真实验证证据。遇到确实无法解决的阻塞如实报告，不伪造成功。'
    common += '另在项目根目录保存 acceptance-evidence.json，格式为 {"checks": [{"name": "检查名称", "passed": true, "evidence": "项目内相对日志或截图路径"}]}；每个检查必须有真实文件证据，失败用 passed:false，不得只声明成功。'
    if case == 'maskedvbyte':
        return common + '使用已安装的 riscv-porter skill，将 https://github.com/fast-pack/MaskedVByte 克隆到此目录并在当前 K3 riscv64 上完成原生移植验证。先读 skill，检查当前官方构建和测试入口；如基线已通过，不做无必要修改。保留原架构优化和公共格式/API。完成官方测试、确定性编码解码 smoke、ELF 架构确认及适用的 ASan/UBSan 检查，保存日志并生成中文 riscv-port-report.md。仅写计划或仅克隆不算完成。证据 JSON 的 checks 必须包含 build、official-tests、codec-smoke、riscv64-elf、sanitizers，sanitizers 不适用时在日志中说明依据。'
    return common + '制作可操作的中国象棋网页：有棋盘和完整初始棋子、红黑轮流走棋、合法走棋判断（含将帅照面）、吃子、重新开始。使用现有浏览器/桌面工具在 K3 上实际打开页面并操作验证至少一次红方走棋、一次黑方走棋及重新开始，保存截图和验证记录；不能只生成文件或给我链接。必要时仅启动此项目自己的无特权本地服务，记录 URL 和 PID，不占用 Runtime 的 9899 端口。证据 JSON 的 checks 必须包含 browser-open、red-move、black-move、reset，至少一个证据文件为实际浏览器截图。浏览器工具不可用时如实报告，不能将 curl 成功当作打开和操作成功。'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['plan', 'prepare', 'submit', 'watch'])
    p.add_argument('case', choices=['maskedvbyte', 'chess'])
    p.add_argument('--base', default='http://127.0.0.1:9899')
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--template', default='com.bianbu.278200e1617c')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--seconds', type=int, default=3600)
    a = p.parse_args()
    def api(path, body=None):
        req = urllib.request.Request(a.base + path, data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)
    def save(s):
        a.state.write_text(json.dumps(s, ensure_ascii=False, indent=2))
    def security(operation, aid, **kw):
        r = api('/security-api/invoke', {'protocolVersion': 1, 'requestId': uuid.uuid4().hex, 'serviceId': 'security', 'operationId': operation, 'params': {'agent_id': aid, **kw}})
        if not r.get('ok'): raise RuntimeError('Security operation failed: ' + operation)
        return r['result']
    if a.action == 'plan':
        print(prompt(a.case, f'/home/bianbu/.bianbu-agent/workspace/acceptance-UNIQUE/{a.case}'))
        return
    if a.action in ('prepare', 'submit') and not a.execute:
        p.error('Mutation disabled: explicit --execute required')
    if a.action == 'prepare':
        if a.state.exists(): p.error('State already exists; inspect it instead of recreating')
        tag = uuid.uuid4().hex[:12]
        s = {'case': a.case, 'tag': tag, 'workspace': f'/home/bianbu/.bianbu-agent/workspace/acceptance-{tag}/{a.case}'}
        a.state.parent.mkdir(parents=True, exist_ok=True)
        save(s)  # Crash leaves evidence; never auto-recreate agents.
        template = api('/api/v1/agents/' + a.template)['data']
        body = {k: template[k] for k in ('model', 'soul', 'tools', 'skills', 'mcps', 'hooks', 'permissions')}
        body.update(name=f'Acceptance {a.case} {tag}', metadata={'execution': {'requested_mode': 'host'}})
        agent = api('/api/v1/agents', body)['data']
        s['agent_id'] = agent['id']; save(s)
        security('agents.security-mode.update.v1', s['agent_id'], mode='yolo')
        session = api('/api/v1/sessions', {'agent_id': s['agent_id'], 'title': f'One submission {a.case} {tag}'})['data']
        s.update(session_id=session['id'], message_id='acceptance-' + tag, prompt=prompt(a.case, s['workspace']))
        save(s); print(json.dumps({k: s[k] for k in ('agent_id', 'session_id', 'workspace')})); return
    s = json.loads(a.state.read_text())
    if s['case'] != a.case: p.error('Case/state mismatch')
    if a.action == 'submit':
        if s.get('prompt_attempted'): p.error('Already attempted; inspect/watch. Never auto retry a possibly accepted POST.')
        mode = security('agents.security-mode.get.v1', s['agent_id'])
        if mode.get('mode') != 'yolo': p.error('Isolated acceptance expert is not YOLO')
        s['prompt_attempted'] = time.time(); save(s)
        s['submission_response'] = api('/api/v1/sessions/' + s['session_id'] + '/prompt', {'message_id': s['message_id'], 'content': s['prompt'], 'attachments': [], 'skill_ids': ['riscv-porter'] if a.case == 'maskedvbyte' else []})
        save(s); print('Single prompt accepted; use watch to observe.'); return
    seen = set(); deadline = time.monotonic() + a.seconds
    journal = a.state.with_suffix('.events.jsonl')
    if journal.exists(): seen = {json.loads(line)['seq'] for line in journal.read_text().splitlines() if line}
    while time.monotonic() < deadline:
        events = []; before = None
        while True:
            query = '?max_messages=50' + ('' if before is None else '&before_seq=' + str(before))
            page = api('/api/v1/sessions/' + s['session_id'] + '/events' + query)
            events.extend(page['data'])
            if not page.get('has_more') or not page['data']: break
            before = page['base_seq']
        terminal = False
        with journal.open('a') as f:
            for event in sorted(events, key=lambda e: e['seq']):
                kind = event.get('type', '')
                if event['seq'] not in seen:
                    f.write(json.dumps(event, ensure_ascii=False) + '\n'); seen.add(event['seq'])
                    if kind not in ('assistant/text.delta', 'assistant/reasoning.delta'): print(event['seq'], kind, flush=True)
                if kind in ('run/completed', 'run/failed', 'run/cancelled'): terminal = True
        if terminal:
            print('Run terminal; inspect artifacts/browser evidence. Terminal alone is not acceptance success.'); return
        time.sleep(2)
    print('Observation deadline reached; task left unchanged. Resume watch; never resubmit.')

if __name__ == '__main__': main()
