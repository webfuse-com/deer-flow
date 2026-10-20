"""[argus patch #154] A removed sandbox reads as absent behind a socket proxy that denies GET /info.

``docker inspect <name>`` without ``--type`` falls back through the other object
types when no container has that name, and the swarm probes ask the engine for
``GET /info``. Argus's per-stack socket proxy does not allow that route, so the
CLI prints its denial next to the missing-object line. ``_inspect_container_entries``
read the extra line as an inspect failure, so a thread whose sandbox container
had been removed could never get a new one: atlas-aphiwe thread a7ea1a15 failed
every file and shell tool from 2026-10-12 15:39 CEST with the stderr below.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend

PREFIX = "argus-atlas-aphiwe-sandbox"
SANDBOX_ID = "fddadb70f132161c"
NAME = f"{PREFIX}-{SANDBOX_ID}"
INFO_DENIAL = "Error response from daemon: sockproxy: GET /info is not on the allowlist"
# The exact stderr behind "Failed to inspect containers with docker: ..." in the
# thread's tool results (deerflow_atlas_aphiwe.run_events, seq 2207).
APHIWE_STDERR = f"{INFO_DENIAL}\nerror: no such object: {NAME}"


def _backend() -> LocalContainerBackend:
    backend = LocalContainerBackend(image="sandbox:latest", base_port=8080, container_prefix=PREFIX, config_mounts=[], environment={})
    backend._runtime = "docker"
    return backend


def _engine(monkeypatch, *, stderr: str, stdout: str = "[]", returncode: int = 1) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr("subprocess.run", run)
    return calls


@pytest.mark.parametrize("stdout", ["[]", ""])
def test_the_aphiwe_stderr_means_the_sandbox_is_absent(monkeypatch, stdout):
    calls = _engine(monkeypatch, stderr=APHIWE_STDERR, stdout=stdout)
    backend = _backend()
    assert backend.is_absent(SANDBOX_ID) is True
    assert backend.inspect_runtime(SANDBOX_ID) is None
    assert calls[0][:2] == ["docker", "inspect"] and calls[0][-1] == NAME


def test_the_aphiwe_stderr_means_the_container_is_not_running(monkeypatch):
    _engine(monkeypatch, stderr=APHIWE_STDERR, stdout="")
    assert _backend()._is_container_running(NAME) is False


def test_discovery_finds_nothing_to_adopt_without_a_warning(monkeypatch, caplog):
    _engine(monkeypatch, stderr=APHIWE_STDERR, stdout="")
    with caplog.at_level("WARNING"):
        assert _backend().discover(SANDBOX_ID) is None
    assert "Could not verify container" not in caplog.text


def test_strict_batch_inspect_reports_no_entries(monkeypatch):
    _engine(monkeypatch, stderr=APHIWE_STDERR)
    assert _backend()._batch_inspect([NAME], strict=True) == {}


def test_every_queried_name_missing_with_one_denial_each(monkeypatch):
    other = f"{PREFIX}-0123456789abcdef"
    stderr = f"{INFO_DENIAL}\nerror: no such object: {NAME}\n{INFO_DENIAL}\nerror: no such object: {other}"
    _engine(monkeypatch, stderr=stderr)
    assert _backend()._inspect_container_entries([NAME, other]) == {}


@pytest.mark.parametrize(
    "stderr",
    [
        # The denial alone proves nothing about the container.
        INFO_DENIAL,
        # The missing object is a different container.
        f"{INFO_DENIAL}\nerror: no such object: {PREFIX}-0123456789abcdef",
        # Any other refused route is a real failure.
        f"Error response from daemon: sockproxy: GET /containers/json is not on the allowlist\nerror: no such object: {NAME}",
        f"Error response from daemon: sockproxy: no policy is loaded for this stack\nerror: no such object: {NAME}",
        f"Error response from daemon: sockproxy: the container engine is unreachable\nerror: no such object: {NAME}",
        # Something other than the socket proxy denying /info.
        f"Error response from daemon: GET /info forbidden\nerror: no such object: {NAME}",
        f"{INFO_DENIAL} (retry)\nerror: no such object: {NAME}",
        f"{INFO_DENIAL}\nerror: no such object: {NAME}\nError response from daemon: permission denied",
        "Cannot connect to the Docker daemon at unix:///run/argus-sockproxy/aphiwe/docker.sock. Is the docker daemon running?",
    ],
)
def test_real_inspect_failures_keep_failing(monkeypatch, stderr):
    _engine(monkeypatch, stderr=stderr, stdout="")
    backend = _backend()
    with pytest.raises(RuntimeError):
        backend.is_absent(SANDBOX_ID)
    with pytest.raises(RuntimeError):
        backend._is_container_running(NAME)


def test_the_denial_does_not_excuse_a_partial_batch(monkeypatch):
    present = f"{PREFIX}-0123456789abcdef"
    entry = {"Id": "generation-1", "Name": f"/{present}", "Created": "2026-10-12T13:39:00Z", "State": {"Status": "running", "Running": True}}
    _engine(monkeypatch, stderr=APHIWE_STDERR, stdout=json.dumps([entry]))
    with pytest.raises(RuntimeError):
        _backend()._inspect_container_entries([present, NAME])
