"""Evidence gates for real task acceptance; no network or model calls here."""
import json
import re
import struct
from pathlib import Path


def _riscv64_executable(path: Path) -> bool:
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01':
            return False
        kind, machine, version, _, phoff, _, _, ehsize, phsize, phnum, *_ = struct.unpack('<HHIQQQIHHHHHH', header[16:])
        size = path.stat().st_size
        if (kind not in {2, 3} or machine != 243 or version != 1 or ehsize != 64
                or phsize != 56 or not phnum or phoff < 64 or phoff + phsize * phnum > size):
            return False
        stream.seek(phoff)
        segments = [struct.unpack('<IIQQQQQQ', stream.read(56)) for _ in range(phnum)]
        loads = [segment for segment in segments if segment[0] == 1]
        return bool(loads) and all(segment[2] + segment[5] <= size and segment[5] <= segment[6]
                                   for segment in loads)


def _decoded_screenshot(path: Path) -> bool:
    if path.stat().st_size < 32:
        return False
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(path) as picture:
            if picture.format not in {'PNG', 'JPEG'}:
                return False
            picture.verify()
        with Image.open(path) as picture:
            picture.load()
            return picture.width > 0 and picture.height > 0
    except (OSError, ValueError, UnidentifiedImageError):
        return False


def _terminal_browser_controller(workspace: Path, events: list[dict], successful: list[dict], run_id: str) -> bool:
    completed_ids = {item.get('call_id') for item in successful if item.get('name') == 'terminal'}
    commands = [str(event.get('data', {}).get('arguments', {}).get('command', ''))
                for event in events if event['type'] == 'tool/call.started'
                and event.get('run_id') == run_id
                and event.get('data', {}).get('call_id') in completed_ids]
    for path in workspace.rglob('*'):
        if (path.suffix not in {'.js', '.mjs', '.py'} or not path.is_file()
                or not path.resolve().is_relative_to(workspace.resolve())
                or path.stat().st_size > 1_000_000):
            continue
        invocation = r'(?:^|[;&|\n])\s*(?:node|python(?:3(?:\.\d+)?)?)\s+[\"\']?(?:[^\s\"\';]*/)?' + re.escape(path.name) + r'[\"\']?(?=\s|$|[;&|])'
        if not any(re.search(invocation, command) for command in commands):
            continue
        source = path.read_text(errors='replace')
        cdp = all(re.search(r'\.(?:send|call)\(\s*[\"\']' + re.escape(method) + r'[\"\']', source)
                  for method in ('Page.navigate', 'Input.dispatchMouseEvent', 'Page.captureScreenshot'))
        playwright = all(re.search(r'\.\s*' + method + r'\s*\(', source)
                         for method in ('goto', 'click', 'screenshot'))
        if cdp or playwright:
            return True
    return False


def validate_evidence(case: str, workspace: Path, events: list[dict]) -> dict:
    """Validate durable evidence, not the model's final success assertion.

    This is an evidence completeness gate. It does not independently prove every
    chess rule or audit every compiler/test result; reviewers inspect the files.
    """
    assert workspace.is_dir(), f'Missing project directory: {workspace}'
    users = [e for e in events if e['type'] == 'user/message']
    assert len(users) == 1, 'Expected exactly one submitted user message'
    run_id = users[0]['run_id']
    assert run_id, 'Missing real run identity'
    assert not any(e['type'] == 'goal/command' for e in events), 'Goal/continue is outside this acceptance'
    terminal = [e for e in events if e['type'] in ('run/completed', 'run/failed', 'run/cancelled')]
    assert len(terminal) == 1 and terminal[0]['type'] == 'run/completed', 'Run did not complete successfully'
    assert terminal[0]['run_id'] == run_id, 'Unexpected extra run'
    assert not terminal[0].get('data', {}).get('error'), 'Completed run returned an error'
    assert not any(e['type'] == 'approval/requested' for e in events), 'Acceptance required intervention'
    completed = [e['data'] for e in events if e['type'] == 'tool/call.completed' and e['run_id'] == run_id]
    successful = [d for d in completed if d.get('status') == 'success']
    assert successful, 'No successful real tool execution recorded'
    manifest = json.loads((workspace / 'acceptance-evidence.json').read_text())
    checks = manifest.get('checks', [])
    required = {'build', 'official-tests', 'codec-smoke', 'riscv64-elf', 'sanitizers'} if case == 'maskedvbyte' else {'browser-open', 'red-move', 'black-move', 'reset'}
    by_name = {c['name']: c for c in checks}
    assert required <= by_name.keys(), f'Missing evidence checks: {required - by_name.keys()}'
    files = []
    for name in sorted(required):
        check = by_name[name]
        assert check.get('passed') is True, f'{name} failed'
        evidence = check['evidence']
        references = [evidence] if isinstance(evidence, str) else evidence
        assert isinstance(references, list) and references, f'{name}: missing evidence paths'
        check_files = []
        for reference in references:
            assert isinstance(reference, str) and reference, f'{name}: invalid evidence path'
            relative = Path(reference)
            assert not relative.is_absolute(), f'{name}: evidence must be project-relative'
            path = (workspace / relative).resolve()
            assert path.is_relative_to(workspace.resolve()), f'{name}: evidence escapes project'
            assert path.is_file(), f'{name}: missing evidence'
            check_files.append(path)
        # Successful compilers may have no diagnostics. Each check still needs
        # at least one nonempty result, and every declared file must exist.
        assert any(path.stat().st_size > 0 for path in check_files), f'{name}: empty evidence'
        files.extend(check_files)
    if case == 'maskedvbyte':
        assert list(workspace.rglob('riscv-port-report.md')), 'Missing port report'
        # ELF e_machine is authoritative even if a report falsely claims riscv64.
        native = []
        for path in workspace.rglob('*'):
            if path.is_symlink() or not path.is_file():
                continue
            if _riscv64_executable(path):
                native.append(str(path.relative_to(workspace)))
        assert native, 'No real RISC-V ELF artifact'
        assert any(d.get('name') == 'terminal' for d in successful), 'No successful build terminal tool'
    else:
        assert list(workspace.rglob('*.html')), 'Missing webpage'
        names = [d.get('name') for d in successful]
        browser_tools = 'browser_navigate' in names and names.count('browser_click') >= 3
        assert browser_tools or _terminal_browser_controller(workspace, events, successful, run_id), 'Missing browser tools or executed browser controller'
        screenshots = []
        for path in files:
            if _decoded_screenshot(path):
                screenshots.append(str(path.relative_to(workspace)))
        assert screenshots, 'No real screenshot file referenced by checks'
    return {'case': case, 'run_id': run_id, 'checked_files': [str(p.relative_to(workspace)) for p in files], 'evidence_complete': True, 'semantic_review_required': True}
