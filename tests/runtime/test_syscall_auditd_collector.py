from pathlib import Path
import importlib.util


def _module():
    path = Path(__file__).parents[2] / "scripts" / "syscall-auditd-collector.py"
    spec = importlib.util.spec_from_file_location("syscall_auditd_collector", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_riscv_numeric_strace_name_maps_273_to_finit_module(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    module = _module()
    match = module.STRACE_NUMERIC.search(
        "syscall_0x111(3, \"\", 0) = -1 EPERM (不允许的操作)"
    )
    assert match is not None
    number = int(match.group("number"), 16)
    assert number == 273
    assert module.REVERSE_NUMBERS[number] == "finit_module"


def test_numeric_trace_takes_precedence_over_generic_name_match():
    module = _module()
    raw = "syscall_0x111(3, \"\", 0) = -1 EPERM (不允许的操作)"
    assert module.STRACE.search(raw) is not None
    assert module.STRACE_NUMERIC.search(raw) is not None


def test_kernel_seccomp_event_without_interpreted_suffix():
    module = _module()
    event = module.parse_kernel_event(
        'type=SECCOMP msg=audit(1789037299.039:112648): auid=1000 uid=1000 '
        'pid=1649541 comm="python" sig=0 arch=c00000f3 syscall=273 code=0x50000')
    assert event == {"pid": 1649541, "number": 273, "syscall": "finit_module",
                     "arch": "riscv64", "event_id": "audit-1789037299.039:112648",
                     "backend": "auditd-seccomp"}


def test_kernel_seccomp_parser_rejects_non_denial_and_unknown_arch():
    module = _module()
    template = 'type=SECCOMP msg=audit(1.0:2): pid=9 arch={} syscall=273 code={}'
    assert module.parse_kernel_event(template.format('c00000f3', '0x7fff0000')) is None
    assert module.parse_kernel_event(template.format('40000003', '0x50000')) is None
    assert module.parse_kernel_event('type=SYSCALL msg=audit(1.0:2): pid=9 arch=c00000f3 syscall=273 exit=0') is None


def test_kernel_seccomp_x86_mapping_is_not_overwritten_by_riscv():
    event = _module().parse_kernel_event(
        'type=SECCOMP msg=audit(1.0:3): pid=10 arch=c000003e syscall=313 code=0x50000')
    assert event['syscall'] == 'finit_module'
    assert event['arch'] == 'x86_64'
