"""[argus patch #170] An open-mode quarantined sandbox is retired behind a socket proxy.

After an uncertain shell outcome the provider recycles the sandbox and keeps a
quarantine record until a later create proves the container absent. That proof
ends in ``LocalContainerBackend.complete_absent_teardown``, which retried the
restricted-mode teardown strictly: ``docker stop deer-flow-netproxy-<digest>``.
Argus's per-stack socket proxy admits only the stack's own sandbox names, so the
stop was refused, retirement aborted, the record stayed, and every new sandbox
for that thread failed. atlas-daeun 2026-10-27 09:36-10:04 CET, thread 9a38e323,
sandbox 2d29ba9b54e63aeb (gateway journal):

    Failed to stop container deer-flow-netproxy-c1a44b2434411cc2: Error response
    from daemon: sockproxy: containers.stop: deer-flow-netproxy-c1a44b2434411cc2
    is not one of this stack's sandboxes

Atlas stacks run sandboxes in open mode and never had a sidecar. #142 fixed the
same refusal on the destroy path by reading the sandbox's labels; here the
sandbox is already gone, so an open-mode process lists the sidecar's fixed name
instead and runs the strict teardown only when one is there.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from test_aio_sandbox_provider import _make_provider

from deerflow.community.aio_sandbox import local_backend as local_backend_module
from deerflow.community.aio_sandbox.local_backend import LocalContainerBackend
from deerflow.community.aio_sandbox.sandbox_info import SandboxInfo

PREFIX = "argus-atlas-daeun-sandbox"
SANDBOX_ID = "2d29ba9b54e63aeb"
NAME = f"{PREFIX}-{SANDBOX_ID}"
GENERATION = "58e222436dbd866cbbe5f68ca05b938302d3c9bfd83e994c93de446fc845d14e"
PROXY = "deer-flow-netproxy-c1a44b2434411cc2"
INFO_DENIAL = "Error response from daemon: sockproxy: GET /info is not on the allowlist"


def _backend(mode: str = "open") -> LocalContainerBackend:
    backend = LocalContainerBackend(image="sandbox:latest", base_port=8080, container_prefix=PREFIX, config_mounts=[], environment={})
    backend._runtime = "docker"
    if mode != "open":
        backend._network_mode = mode
        backend._network_config = {"mode": mode, "allow_domains": ["pypi.org"], "approval": "prompt", "temporary_grant_ttl": 300, "proxy_image": "proxy:latest"}
    return backend


class _Engine:
    """The engine as a stack gateway sees it through the socket proxy.

    The sandbox container is gone. ``docker ps`` is allowed and its response is
    filtered to the stack's prefix, so it lists no sidecar unless ``listed``
    says so. Every other call on a ``deer-flow-*`` name is refused. With
    ``proxied=False`` the engine is a raw socket that removes what it is asked.
    """

    def __init__(self, *, listed: str = "", proxied: bool = True, listing_error: BaseException | None = None, listing_returncode: int = 0, present_mode: str | None = None):
        self.calls: list[list[str]] = []
        # A sandbox container still running under this mode label until it is stopped.
        self.present_mode = present_mode
        self.denied: list[list[str]] = []
        self.listed = listed
        self.proxied = proxied
        self.listing_error = listing_error
        self.listing_returncode = listing_returncode

    def _refuse(self, cmd: list[str], message: str):
        self.denied.append(cmd)
        return SimpleNamespace(stdout="", stderr=f"Error response from daemon: sockproxy: {message}", returncode=1)

    def run(self, cmd, **kwargs):
        self.calls.append(cmd)
        result = self._answer(cmd)
        if kwargs.get("check") and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
        return result

    def _answer(self, cmd):
        if cmd[:2] == ["docker", "inspect"] and self.present_mode is not None and NAME in cmd:
            labels = {"deerflow.role": "sandbox", "deerflow.sandbox_id": SANDBOX_ID, "deerflow.network_mode": self.present_mode}
            entry = {"Id": GENERATION, "Name": f"/{NAME}", "State": {"Running": True}, "Config": {"Image": "sandbox:latest", "Labels": labels}}
            return SimpleNamespace(stdout=json.dumps([entry]), stderr="", returncode=0)
        if cmd[:2] == ["docker", "stop"] and cmd[-1] in (GENERATION, NAME):
            self.present_mode = None
            return SimpleNamespace(stdout=cmd[-1], stderr="", returncode=0)
        if cmd[:2] == ["docker", "inspect"]:
            # The #154 shape: the CLI's type fallback asks GET /info, which is refused.
            return SimpleNamespace(stdout="[]", stderr="\n".join([INFO_DENIAL, *(f"error: no such object: {name}" for name in cmd[2:] if not name.startswith("-"))]), returncode=1)
        if cmd[:3] == ["docker", "ps", "-a"]:
            if self.listing_error is not None:
                raise self.listing_error
            if self.listing_returncode:
                return SimpleNamespace(stdout="", stderr="Cannot connect to the Docker daemon", returncode=self.listing_returncode)
            return SimpleNamespace(stdout=self.listed, stderr="", returncode=0)
        if not self.proxied:
            return SimpleNamespace(stdout=cmd[-1], stderr="", returncode=0)
        if cmd[:2] == ["docker", "stop"]:
            return self._refuse(cmd, f"containers.stop: {cmd[-1]} is not one of this stack's sandboxes")
        if cmd[:3] == ["docker", "rm", "-f"]:
            return self._refuse(cmd, f"containers.remove: {cmd[-1]} is not one of this stack's sandboxes")
        if cmd[:3] == ["docker", "network", "rm"]:
            return self._refuse(cmd, f"DELETE /networks/{cmd[-1]} is not on the allowlist")
        raise AssertionError(f"unexpected engine call: {cmd}")


@pytest.fixture
def released(monkeypatch) -> list[int]:
    ports: list[int] = []
    monkeypatch.setattr(local_backend_module, "release_port", ports.append)
    return ports


def _install(monkeypatch, engine: _Engine) -> _Engine:
    monkeypatch.setattr("subprocess.run", engine.run)
    return engine


def test_the_sidecar_name_is_the_one_daeun_was_refused():
    assert _backend()._resource_names(SANDBOX_ID)[0] == PROXY


def test_open_mode_absent_teardown_touches_no_sidecar_or_network(monkeypatch, released):
    engine = _install(monkeypatch, _Engine())
    backend = _backend()
    backend._pending_cleanup_ports["old-generation"] = (SANDBOX_ID, 18080)

    backend.complete_absent_teardown(SANDBOX_ID)

    assert engine.calls == [["docker", "ps", "-a", "--filter", f"name={PROXY}", "--format", "{{.Names}}"]]
    assert engine.denied == []
    assert released == [18080]
    assert backend._pending_cleanup_ports == {}


@pytest.mark.parametrize("listed", ["", f"{PROXY}-other\n", f"x{PROXY}\n"], ids=["nothing", "longer-name", "prefixed-name"])
def test_only_the_exact_sidecar_name_counts_as_listed(monkeypatch, released, listed):
    # ``--filter name=`` matches substrings, so a lookalike must not keep the fence.
    engine = _install(monkeypatch, _Engine(listed=listed))
    _backend().complete_absent_teardown(SANDBOX_ID)
    assert engine.denied == []


def test_restricted_mode_absent_teardown_stays_strict(monkeypatch, released):
    engine = _install(monkeypatch, _Engine())
    backend = _backend("allowlist")
    backend._pending_cleanup_ports["old-generation"] = (SANDBOX_ID, 18080)

    with pytest.raises(subprocess.CalledProcessError):
        backend.complete_absent_teardown(SANDBOX_ID)

    assert not any(cmd[:2] == ["docker", "ps"] for cmd in engine.calls)
    assert engine.denied == [["docker", "stop", PROXY]]
    assert released == []
    assert backend._pending_cleanup_ports == {"old-generation": (SANDBOX_ID, 18080)}


@pytest.mark.parametrize("listed", [f"{PROXY}\n", f"/{PROXY}\n", f"other\n{PROXY}\n"], ids=["bare", "slash", "among-others"])
def test_open_mode_with_a_listed_sidecar_stays_strict(monkeypatch, released, listed):
    # A sidecar left by an earlier restricted-mode process really exists: its
    # teardown keeps the upstream contract, so a refusal retains the fence.
    engine = _install(monkeypatch, _Engine(listed=listed))
    backend = _backend()
    backend._pending_cleanup_ports["old-generation"] = (SANDBOX_ID, 18080)

    with pytest.raises(subprocess.CalledProcessError):
        backend.complete_absent_teardown(SANDBOX_ID)

    assert engine.denied == [["docker", "stop", PROXY]]
    assert released == []


def test_open_mode_with_a_listed_sidecar_removes_it_on_a_raw_engine(monkeypatch, released):
    engine = _install(monkeypatch, _Engine(listed=f"{PROXY}\n", proxied=False))
    backend = _backend()
    _, network_name = backend._resource_names(SANDBOX_ID)

    backend.complete_absent_teardown(SANDBOX_ID)

    assert ["docker", "stop", PROXY] in engine.calls
    assert ["docker", "rm", "-f", PROXY] in engine.calls
    assert ["docker", "network", "rm", network_name] in engine.calls
    assert ["docker", "network", "rm", backend._egress_network_name(SANDBOX_ID)] in engine.calls


@pytest.mark.parametrize(
    "engine_kwargs",
    [
        {"listing_returncode": 1},
        {"listing_error": subprocess.TimeoutExpired(["docker", "ps"], 15)},
        {"listing_error": FileNotFoundError("docker")},
        {"listing_error": PermissionError("docker")},
    ],
    ids=["nonzero-exit", "timeout", "no-cli", "os-error"],
)
def test_open_mode_listing_failure_keeps_the_strict_cleanup(monkeypatch, released, engine_kwargs):
    engine = _install(monkeypatch, _Engine(**engine_kwargs))
    backend = _backend()
    backend._pending_cleanup_ports["old-generation"] = (SANDBOX_ID, 18080)

    with pytest.raises(subprocess.CalledProcessError):
        backend.complete_absent_teardown(SANDBOX_ID)

    assert engine.denied == [["docker", "stop", PROXY]]
    assert released == []


def _quarantined_provider(tmp_path, backend: LocalContainerBackend):
    provider = _make_provider(tmp_path)
    provider._warm_pool = {}
    provider._sandbox_infos = {}
    provider._thread_sandboxes = {}
    provider._last_activity = {}
    provider._backend = backend
    provider._quarantine.mark(SandboxInfo(SANDBOX_ID, "", container_name=NAME, container_id=GENERATION))
    assert provider._quarantine.contains(SandboxInfo(SANDBOX_ID, ""))
    return provider


def test_next_create_retires_an_open_mode_quarantine_behind_the_proxy(tmp_path, monkeypatch, released):
    # The daeun sequence: the recycle stopped the container and left the record;
    # the next create must prove absence, retire the record and go on.
    engine = _install(monkeypatch, _Engine())
    provider = _quarantined_provider(tmp_path, _backend())

    provider._retire_absent_quarantine(SANDBOX_ID)

    assert not provider._quarantine.contains(SandboxInfo(SANDBOX_ID, ""))
    assert engine.denied == []
    assert not any(PROXY in " ".join(cmd) for cmd in engine.calls if cmd[:2] != ["docker", "ps"])
    # A second create finds nothing to retire and asks the engine nothing.
    calls = len(engine.calls)
    provider._retire_absent_quarantine(SANDBOX_ID)
    assert len(engine.calls) == calls


def test_a_quarantined_open_sandbox_still_running_is_stopped_and_retired(tmp_path, monkeypatch, released):
    # The record outlived a recycle whose stop did not finish: the same call
    # stops the open-labelled container (#142 skips its sidecar) and then
    # retires the record once the container is gone (#170).
    engine = _install(monkeypatch, _Engine(present_mode="open"))
    provider = _quarantined_provider(tmp_path, _backend())

    provider._retire_absent_quarantine(SANDBOX_ID)

    assert ["docker", "stop", GENERATION] in engine.calls
    assert engine.denied == []
    assert not provider._quarantine.contains(SandboxInfo(SANDBOX_ID, ""))


def test_a_restricted_mode_quarantine_is_kept_when_its_sidecar_teardown_is_refused(tmp_path, monkeypatch, released):
    engine = _install(monkeypatch, _Engine())
    provider = _quarantined_provider(tmp_path, _backend("allowlist"))

    with pytest.raises(subprocess.CalledProcessError):
        provider._retire_absent_quarantine(SANDBOX_ID)

    assert provider._quarantine.contains(SandboxInfo(SANDBOX_ID, ""))
    assert engine.denied == [["docker", "stop", PROXY]]
