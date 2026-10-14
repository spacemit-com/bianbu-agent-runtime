import struct
import pytest


def test_filter_logging_requests_kernel_log_flag(tmp_path):
    from bianbu_agent.security.seccomp_audit import enable_filter_logging
    path = tmp_path / 'filter'
    path.write_bytes(struct.pack('=HBBI', 6, 0, 0, 0x7fff0000))
    calls = []
    class Libc:
        def prctl(self, *args):
            calls.append(('prctl', args[:2])); return 0
        def syscall(self, number, operation, flags, program):
            calls.append(('seccomp', number, operation, flags)); return 0
    enable_filter_logging(path, libc=Libc(), arch='riscv64')
    assert calls == [('prctl', (38, 1)), ('seccomp', 277, 1, 2)]


def test_bad_filter_is_rejected_before_kernel_load(tmp_path):
    from bianbu_agent.security.seccomp_audit import enable_filter_logging
    path = tmp_path / 'filter'; path.write_bytes(b'bad')
    with pytest.raises(ValueError): enable_filter_logging(path)
