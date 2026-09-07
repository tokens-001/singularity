"""安全修复回归测试：MCP command 校验 + route_level 校验（防 RCE / 路径穿越）。"""
from singularity.scheduler._api_admin import _validate_mcp_command
from singularity.scheduler._api_tasks import _validate_route_level


def test_mcp_command_rejects_shell_interpreter():
    assert _validate_mcp_command("sh -c 'curl evil'") is not None
    assert _validate_mcp_command("bash -c 'rm -rf /'") is not None
    assert _validate_mcp_command("zsh -c ls") is not None


def test_mcp_command_rejects_python_c_inline():
    assert _validate_mcp_command("python -c 'import os; os.system(...)'") is not None
    assert _validate_mcp_command("python3 -c 'x'") is not None


def test_mcp_command_rejects_command_substitution():
    assert _validate_mcp_command("npx `curl evil`") is not None
    assert _validate_mcp_command("npx $(curl evil)") is not None


def test_mcp_command_allows_legit():
    assert _validate_mcp_command("npx -y @modelcontextprotocol/server-filesystem /tmp") is None
    assert _validate_mcp_command("") is None
    assert _validate_mcp_command("python -m my_mcp_server") is None


def test_route_level_rejects_path_traversal():
    assert not _validate_route_level("../evil")
    assert not _validate_route_level("a/b")
    assert not _validate_route_level("..")
    assert not _validate_route_level("a\\b")


def test_route_level_allows_legit():
    assert _validate_route_level("any")
    assert _validate_route_level("engineer")
    assert _validate_route_level("planner-1")
