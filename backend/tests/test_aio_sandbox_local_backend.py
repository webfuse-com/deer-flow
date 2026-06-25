import logging
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

from deerflow.community.aio_sandbox.local_backend import (
    LocalContainerBackend,
    _format_container_command_for_log,
    _format_container_mount,
    _redact_container_command_for_log,
    _resolve_docker_bind_host,
)


def test_format_container_mount_uses_mount_syntax_for_docker_windows_paths():
    args = _format_container_mount("docker", "D:/deer-flow/backend/.deer-flow/threads", "/mnt/threads", False)

    assert args == [
        "--mount",
        "type=bind,src=D:/deer-flow/backend/.deer-flow/threads,dst=/mnt/threads",
    ]


def test_format_container_mount_marks_docker_readonly_mounts():
    args = _format_container_mount("docker", "/host/path", "/mnt/path", True)

    assert args == [
        "--mount",
        "type=bind,src=/host/path,dst=/mnt/path,readonly",
    ]


def test_format_container_mount_keeps_volume_syntax_for_apple_container():
    args = _format_container_mount("container", "/host/path", "/mnt/path", True)

    assert args == [
        "-v",
        "/host/path:/mnt/path:ro",
    ]


def test_redact_container_command_for_log_redacts_env_values():
    redacted = _redact_container_command_for_log(
        [
            "docker",
            "run",
            "-e",
            "API_KEY=secret-value",
            "--env=TOKEN=token-value",
            "--name",
            "sandbox",
            "image",
        ]
    )

    assert "API_KEY=<redacted>" in redacted
    assert "--env=TOKEN=<redacted>" in redacted
    assert "secret-value" not in " ".join(redacted)
    assert "token-value" not in " ".join(redacted)


def test_redact_container_command_for_log_keeps_inherited_env_names():
    redacted = _redact_container_command_for_log(
        [
            "docker",
            "run",
            "-e",
            "API_KEY",
            "--env=TOKEN",
            "--name",
            "sandbox",
            "image",
        ]
    )

    assert redacted == [
        "docker",
        "run",
        "-e",
        "API_KEY",
        "--env=TOKEN",
        "--name",
        "sandbox",
        "image",
    ]


def test_format_container_command_for_log_uses_windows_quoting(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    command = _format_container_command_for_log(["docker", "run", "--name", "sandbox one", "image"])

    assert command == 'docker run --name "sandbox one" image'


def test_start_container_logs_redacted_env_values(monkeypatch, caplog):
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="sandbox",
        config_mounts=[],
        environment={"API_KEY": "secret-value", "NORMAL": "visible-value"},
    )
    monkeypatch.setattr(backend, "_runtime", "docker")
    # The post-run Created-state check polls _is_container_running; mock it True
    # so the check passes immediately (the mock subprocess.run returns a fake ID).
    monkeypatch.setattr(backend, "_is_container_running", lambda name: True)

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return SimpleNamespace(stdout="container-id\n", stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    with caplog.at_level(logging.INFO, logger="deerflow.community.aio_sandbox.local_backend"):
        backend._start_container("sandbox-test", 18080)

    joined_cmd = " ".join(captured_cmd)
    assert "API_KEY=secret-value" in joined_cmd
    assert "NORMAL=visible-value" in joined_cmd

    log_output = "\n".join(record.getMessage() for record in caplog.records)
    assert "API_KEY=<redacted>" in log_output
    assert "NORMAL=<redacted>" in log_output
    assert "secret-value" not in log_output
    assert "visible-value" not in log_output


def _capture_start_container_command(monkeypatch, backend: LocalContainerBackend, runtime: str = "docker") -> list[str]:
    monkeypatch.setattr(backend, "_runtime", runtime)
    # The post-run Created-state check polls _is_container_running; mock it True
    # so the check passes immediately (the mock subprocess.run returns a fake ID).
    monkeypatch.setattr(backend, "_is_container_running", lambda name: True)
    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return SimpleNamespace(stdout="container-id\n", stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    backend._start_container("sandbox-test", 18080)
    return captured_cmd


def test_resolve_docker_bind_host_defaults_loopback_for_localhost(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_SANDBOX_BIND_HOST", raising=False)
    monkeypatch.delenv("DEER_FLOW_SANDBOX_HOST", raising=False)

    assert _resolve_docker_bind_host() == "127.0.0.1"


def test_resolve_docker_bind_host_keeps_dood_compatibility(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_SANDBOX_BIND_HOST", raising=False)
    monkeypatch.setenv("DEER_FLOW_SANDBOX_HOST", "host.docker.internal")

    assert _resolve_docker_bind_host() == "0.0.0.0"


def test_resolve_docker_bind_host_uses_ipv6_loopback_for_ipv6_sandbox_host(monkeypatch):
    monkeypatch.delenv("DEER_FLOW_SANDBOX_BIND_HOST", raising=False)
    monkeypatch.setenv("DEER_FLOW_SANDBOX_HOST", "[::1]")

    assert _resolve_docker_bind_host() == "[::1]"


def test_resolve_docker_bind_host_logs_selected_bind_reason(caplog):
    with caplog.at_level(logging.DEBUG, logger="deerflow.community.aio_sandbox.local_backend"):
        assert _resolve_docker_bind_host(sandbox_host="localhost", bind_host="") == "127.0.0.1"

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "Docker sandbox bind: 127.0.0.1 (loopback default)" in messages


def test_resolve_docker_bind_host_allows_explicit_override(monkeypatch):
    monkeypatch.setenv("DEER_FLOW_SANDBOX_HOST", "localhost")
    monkeypatch.setenv("DEER_FLOW_SANDBOX_BIND_HOST", "192.0.2.10")

    assert _resolve_docker_bind_host() == "192.0.2.10"


def test_start_container_binds_local_docker_port_to_loopback_by_default(monkeypatch):
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.delenv("DEER_FLOW_SANDBOX_HOST", raising=False)
    monkeypatch.delenv("DEER_FLOW_SANDBOX_BIND_HOST", raising=False)

    captured_cmd = _capture_start_container_command(monkeypatch, backend)

    assert captured_cmd[captured_cmd.index("-p") + 1] == "127.0.0.1:18080:8080"


def test_start_container_keeps_broad_bind_for_dood_sandbox_host(monkeypatch):
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.setenv("DEER_FLOW_SANDBOX_HOST", "host.docker.internal")
    monkeypatch.delenv("DEER_FLOW_SANDBOX_BIND_HOST", raising=False)

    captured_cmd = _capture_start_container_command(monkeypatch, backend)

    assert captured_cmd[captured_cmd.index("-p") + 1] == "0.0.0.0:18080:8080"


def test_start_container_binds_ipv6_sandbox_host_to_ipv6_loopback(monkeypatch):
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.setenv("DEER_FLOW_SANDBOX_HOST", "[::1]")
    monkeypatch.delenv("DEER_FLOW_SANDBOX_BIND_HOST", raising=False)

    captured_cmd = _capture_start_container_command(monkeypatch, backend)

    assert captured_cmd[captured_cmd.index("-p") + 1] == "[::1]:18080:8080"


def test_start_container_keeps_apple_container_port_format(monkeypatch):
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.setenv("DEER_FLOW_SANDBOX_BIND_HOST", "127.0.0.1")

    captured_cmd = _capture_start_container_command(monkeypatch, backend, runtime="container")

    assert captured_cmd[captured_cmd.index("-p") + 1] == "18080:8080"


# ── Network-DNS mode ─────────────────────────────────────────────────────────


def test_start_container_network_mode_adds_network_no_port_publish(monkeypatch):
    """In network mode, --network is emitted and -p (host port) is absent."""
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="sandbox",
        config_mounts=[],
        environment={},
        network="argus-net",
    )
    monkeypatch.setattr(backend, "_runtime", "docker")
    monkeypatch.setattr(backend, "_is_container_running", lambda name: True)

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return SimpleNamespace(stdout="container-id\n", stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    backend._start_container("sandbox-net", 8080)

    joined = " ".join(captured_cmd)
    assert "--network" in captured_cmd
    assert captured_cmd[captured_cmd.index("--network") + 1] == "argus-net"
    assert "-p" not in captured_cmd
    assert "--name" in captured_cmd
    assert captured_cmd[captured_cmd.index("--name") + 1] == "sandbox-net"


def test_start_container_legacy_mode_still_publishes_port(monkeypatch):
    """In legacy mode (network=None), -p is emitted and --network is absent."""
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.setattr(backend, "_runtime", "docker")
    monkeypatch.setattr(backend, "_is_container_running", lambda name: True)
    monkeypatch.delenv("DEER_FLOW_SANDBOX_HOST", raising=False)
    monkeypatch.delenv("DEER_FLOW_SANDBOX_BIND_HOST", raising=False)

    captured_cmd = _capture_start_container_command(monkeypatch, backend)

    assert "-p" in captured_cmd
    assert "--network" not in captured_cmd


def test_create_network_mode_builds_dns_url(monkeypatch):
    """create() in network mode returns sandbox_url with container DNS name, not host:port."""
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="argus-atlas-nicholas-sandbox",
        config_mounts=[],
        environment={},
        network="argus-net",
    )
    monkeypatch.setattr(backend, "_runtime", "docker")
    monkeypatch.setattr(backend, "_is_container_running", lambda name: True)

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(stdout="abc123\n", stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    info = backend.create("thread-1", "84484525")

    assert info.sandbox_url == "http://argus-atlas-nicholas-sandbox-84484525:8080"
    assert info.container_name == "argus-atlas-nicholas-sandbox-84484525"
    assert info.container_id == "abc123"


def test_create_network_mode_retries_on_name_conflict(monkeypatch):
    """create() in network mode force-removes a stale orphan on name conflict and retries."""
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="argus-sandbox",
        config_mounts=[],
        environment={},
        network="argus-net",
    )
    monkeypatch.setattr(backend, "_runtime", "docker")
    monkeypatch.setattr(backend, "_is_container_running", lambda name: True)

    call_count = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise subprocess.CalledProcessError(
                1, cmd,
                stderr='container create: the container name "argus-sandbox-dead" is already in use by abc123. You have to remove that container to be able to reuse that name: that name is already in use',
            )
        return SimpleNamespace(stdout="newid\n", stderr="", returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)

    removed: list[str] = []

    def fake_remove(name):
        removed.append(name)
        return True

    monkeypatch.setattr(backend, "_force_remove", fake_remove)

    info = backend.create("thread-1", "dead")

    assert info.sandbox_url == "http://argus-sandbox-dead:8080"
    assert removed == ["argus-sandbox-dead"]
    assert call_count["n"] == 2


# ── Orphan reaping ───────────────────────────────────────────────────────────


def test_list_orphaned_enumerates_non_running_containers(monkeypatch):
    """list_orphaned() returns Created/Exited container names matching the prefix."""
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="argus-sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.setattr(backend, "_runtime", "docker")

    import subprocess

    def mock_run(cmd, **kwargs):
        result = MagicMock()
        assert cmd[1] == "ps"
        assert "--filter" in cmd
        assert "status=created" in cmd
        result.returncode = 0
        result.stdout = "argus-sandbox-abc12345\nargus-sandbox-def67890\n"
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", mock_run)

    orphans = backend.list_orphaned()
    assert sorted(orphans) == ["argus-sandbox-abc12345", "argus-sandbox-def67890"]


def test_list_orphaned_skips_non_matching_names(monkeypatch):
    """list_orphaned() only returns containers matching the configured prefix."""
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="argus-sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.setattr(backend, "_runtime", "docker")

    import subprocess

    def mock_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "argus-sandbox-abc12345\nother-container\nargus-sandbox-def67890\n"
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", mock_run)

    orphans = backend.list_orphaned()
    assert sorted(orphans) == ["argus-sandbox-abc12345", "argus-sandbox-def67890"]


def test_list_orphaned_empty_when_none(monkeypatch):
    """list_orphaned() returns [] when docker ps finds nothing."""
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="argus-sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.setattr(backend, "_runtime", "docker")

    import subprocess

    def mock_run(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", mock_run)

    assert backend.list_orphaned() == []


def test_purge_force_removes_container(monkeypatch):
    """purge() delegates to _force_remove and returns its result."""
    backend = LocalContainerBackend(
        image="sandbox:latest",
        base_port=8080,
        container_prefix="argus-sandbox",
        config_mounts=[],
        environment={},
    )
    monkeypatch.setattr(backend, "_runtime", "docker")

    removed: list[str] = []

    def fake_remove(name):
        removed.append(name)
        return True

    monkeypatch.setattr(backend, "_force_remove", fake_remove)

    assert backend.purge("argus-sandbox-abc12345") is True
    assert removed == ["argus-sandbox-abc12345"]


def test_backend_list_orphaned_default_returns_empty():
    """Base SandboxBackend.list_orphaned() returns empty list (backward compat)."""
    from deerflow.community.aio_sandbox.backend import SandboxBackend

    class StubBackend(SandboxBackend):
        def create(self, thread_id, sandbox_id, extra_mounts=None):
            pass

        def destroy(self, info):
            pass

        def is_alive(self, info):
            return False

        def discover(self, sandbox_id):
            return None

    backend = StubBackend()
    assert backend.list_orphaned() == []
    assert backend.purge("any") is True
