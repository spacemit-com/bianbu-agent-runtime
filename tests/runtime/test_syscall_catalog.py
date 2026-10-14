from bianbu_agent.security.syscall_catalog import normalize_arch, syscall_context


def test_riscv64_273_is_finit_module_not_x86_renameat2():
    text = syscall_context("riscv64")
    assert "Worker kernel architecture: riscv64" in text
    assert "finit_module=273" in text
    assert "x86_64 name" in text
    assert "renameat2" not in text


def test_x86_64_uses_its_own_finit_module_number():
    text = syscall_context("x86_64")
    assert "finit_module=313" in text
    assert "finit_module=273" not in text


def test_arch_aliases_are_normalized():
    assert normalize_arch("risc-v64") == "riscv64"
    assert normalize_arch("amd64") == "x86_64"
