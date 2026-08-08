"""Phase 7 Step 5 — the control channel.

The spec asks for two things: that control messages round-trip, and that a
timeout on an unresponsive process is handled without deadlocking the API. The
second is the one with teeth.

A wedged worker holds its caller for the whole timeout. Under the current dev
server, which spawns a thread per request, that is merely wasteful. Under a
production WSGI server with a bounded pool — which Phase 10 introduces — a UI
polling a wedged run every second would tie workers up permanently and
eventually leave nothing to serve anything else with. So callers are
admission-controlled, and the tests below assert both halves: a blocked call
never stops other work, and enough blocked calls are refused rather than
allowed to accumulate.

The round-trip tests moved here from `test_process_isolation.py`, which is
about processes; this file is about the channel between them.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
import threading
import time
from pathlib import Path

import pytest

from app.services.simulation_ipc import (
    MAX_INFLIGHT_CALLS,
    SOCKET_NAME,
    ControlClient,
    ControlPlaneBusy,
    ControlServer,
    IPCError,
    WorkerUnreachable,
    socket_path,
)


def wait_for_a_quiet_gate(timeout: float = 20.0) -> None:
    """Block until no control call is in flight.

    The admission gate is process-global on purpose — it bounds the whole API,
    not one caller — which means it is also shared between tests. A call a test
    abandons keeps its slot until its own timeout expires, so without this the
    next test starts against a partly-full gate and fails for reasons that have
    nothing to do with it.
    """
    from app.services.simulation_ipc import _inflight

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        taken = []
        while _inflight.acquire(blocking=False):
            taken.append(True)
        for _ in taken:
            _inflight.release()
        if len(taken) == MAX_INFLIGHT_CALLS:
            return
        time.sleep(0.05)
    raise AssertionError("control-plane slots were never released")


@pytest.fixture(autouse=True)
def quiet_control_plane():
    """Every test in this file starts and ends with an empty gate."""
    wait_for_a_quiet_gate()
    yield
    wait_for_a_quiet_gate()


@pytest.fixture
def short_tmp():
    """The kernel limits a Unix socket path to 107 characters."""
    directory = Path(tempfile.mkdtemp(prefix="cs-", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


async def start_server(path, handlers):
    server = ControlServer(path)
    for name, handler in handlers.items():
        server.handle(name, handler)
    await server.start()
    return server


async def call(client, command, **kwargs):
    """Drive the blocking client off the loop the server is running on."""
    return await asyncio.to_thread(client.request, command, **kwargs)


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


async def test_a_command_and_its_reply_round_trip(short_tmp):
    async def on_echo(request):
        return {"heard": request.args.get("text")}

    server = await start_server(socket_path(short_tmp), {"echo": on_echo})
    client = ControlClient(socket_path(short_tmp), timeout=5.0)
    try:
        assert await call(client, "echo", text="hello") == {"heard": "hello"}
    finally:
        await server.close()


async def test_arguments_survive_their_types(short_tmp):
    """The protocol is JSON, so a caller gets back what it sent."""
    received = {}

    async def on_take(request):
        received.update(request.args)
        return {"ok": True}

    server = await start_server(socket_path(short_tmp), {"take": on_take})
    client = ControlClient(socket_path(short_tmp), timeout=5.0)
    try:
        await call(client, "take", number=42, flag=True, items=[1, 2],
                   nested={"a": "b"}, nothing=None)
    finally:
        await server.close()

    assert received == {"number": 42, "flag": True, "items": [1, 2],
                        "nested": {"a": "b"}, "nothing": None}


async def test_many_commands_over_one_channel(short_tmp):
    """Status is polled constantly; the server must not degrade across calls."""
    async def on_ping(_request):
        return {"pong": True}

    server = await start_server(socket_path(short_tmp), {"ping": on_ping})
    client = ControlClient(socket_path(short_tmp), timeout=5.0)
    try:
        for _ in range(30):
            assert await call(client, "ping") == {"pong": True}
    finally:
        await server.close()


async def test_an_unknown_command_is_an_error_not_a_crash(short_tmp):
    server = await start_server(socket_path(short_tmp), {})
    client = ControlClient(socket_path(short_tmp), timeout=5.0)
    try:
        with pytest.raises(IPCError, match="Unknown command"):
            await call(client, "nonsense")
    finally:
        await server.close()


async def test_A_FAILING_HANDLER_DOES_NOT_KILL_THE_WORKER(short_tmp):
    """A bad control request must never take down a run that is hours old."""
    async def on_boom(_request):
        raise ValueError("handler exploded")

    async def on_ping(_request):
        return {"pong": True}

    server = await start_server(socket_path(short_tmp),
                                {"boom": on_boom, "ping": on_ping})
    client = ControlClient(socket_path(short_tmp), timeout=5.0)
    try:
        with pytest.raises(IPCError, match="handler exploded"):
            await call(client, "boom")
        assert await call(client, "ping") == {"pong": True}
    finally:
        await server.close()


async def test_a_malformed_request_is_rejected_cleanly(short_tmp):
    import socket as socket_module

    server = await start_server(socket_path(short_tmp), {})
    try:
        def send_rubbish():
            connection = socket_module.socket(socket_module.AF_UNIX,
                                              socket_module.SOCK_STREAM)
            connection.settimeout(5)
            connection.connect(str(socket_path(short_tmp)))
            connection.sendall(b"this is not json\n")
            reply = connection.recv(65536)
            connection.close()
            return reply

        reply = await asyncio.to_thread(send_rubbish)
        assert b"Malformed" in reply
    finally:
        await server.close()


# --------------------------------------------------------------------------
# The socket itself
# --------------------------------------------------------------------------


def test_no_worker_is_unreachable_rather_than_a_hang(short_tmp):
    client = ControlClient(socket_path(short_tmp), timeout=1.0)
    started = time.monotonic()
    with pytest.raises(WorkerUnreachable):
        client.request("ping")
    assert time.monotonic() - started < 1.0


def test_ping_never_raises(short_tmp):
    """It is used to decide whether a worker is there at all."""
    assert ControlClient(socket_path(short_tmp), timeout=1.0).ping() is False


async def test_a_stale_socket_file_does_not_block_a_restart(short_tmp):
    """A killed worker never reaches its own cleanup."""
    path = socket_path(short_tmp)
    path.write_bytes(b"")

    async def on_ping(_request):
        return {"pong": True}

    server = await start_server(path, {"ping": on_ping})
    try:
        assert await asyncio.to_thread(ControlClient(path).ping)
    finally:
        await server.close()


async def test_a_live_socket_is_never_clobbered(short_tmp):
    """Otherwise starting a run twice would orphan the first worker."""
    async def on_ping(_request):
        return {"pong": True}

    first = await start_server(socket_path(short_tmp), {"ping": on_ping})
    try:
        with pytest.raises(IPCError, match="already listening"):
            await ControlServer(socket_path(short_tmp)).start()
    finally:
        await first.close()


async def test_closing_removes_the_socket(short_tmp):
    server = await start_server(socket_path(short_tmp), {})
    await server.close()
    assert not socket_path(short_tmp).exists()


def test_an_over_long_path_is_refused_with_the_reason(tmp_path):
    """bind() would fail complaining about the address, not the length."""
    with pytest.raises(IPCError, match="too long"):
        socket_path(tmp_path / ("x" * 90) / ("y" * 90))


def test_the_socket_lives_in_the_runs_own_directory(short_tmp):
    assert socket_path(short_tmp).name == SOCKET_NAME
    assert socket_path(short_tmp).parent == short_tmp


# --------------------------------------------------------------------------
# An unresponsive worker
# --------------------------------------------------------------------------


async def test_a_slow_handler_times_out_rather_than_waiting_forever(short_tmp):
    async def on_slow(_request):
        await asyncio.sleep(30)
        return {"eventually": True}

    server = await start_server(socket_path(short_tmp), {"slow": on_slow})
    client = ControlClient(socket_path(short_tmp), timeout=1.0)
    try:
        started = time.monotonic()
        with pytest.raises(WorkerUnreachable, match="did not answer"):
            await call(client, "slow")
        assert time.monotonic() - started < 3.0
    finally:
        await server.close()


async def test_A_BLOCKED_CALL_DOES_NOT_STOP_OTHER_WORK(short_tmp):
    """The spec's requirement: a timeout must not deadlock the API."""
    async def on_slow(_request):
        await asyncio.sleep(30)

    async def on_ping(_request):
        return {"pong": True}

    server = await start_server(socket_path(short_tmp),
                                {"slow": on_slow, "ping": on_ping})
    # A short timeout on the blocked caller so it returns its slot quickly;
    # the point being tested is that it does not hold up the other call.
    slow_client = ControlClient(socket_path(short_tmp), timeout=2.0)
    quick_client = ControlClient(socket_path(short_tmp), timeout=5.0)
    try:
        blocked = asyncio.ensure_future(call(slow_client, "slow"))
        await asyncio.sleep(0.3)

        started = time.monotonic()
        assert await call(quick_client, "ping") == {"pong": True}
        assert time.monotonic() - started < 2.0, "the second call waited on the first"

        blocked.cancel()
        try:
            await blocked
        except (asyncio.CancelledError, IPCError):
            pass
    finally:
        await server.close()


def test_ENOUGH_BLOCKED_CALLS_ARE_REFUSED_RATHER_THAN_QUEUED(short_tmp):
    """A wedged worker must not be able to occupy every request thread.

    Today's dev server spawns a thread per request so this cannot bite; a
    production WSGI server has a bounded pool, and a UI polling a wedged run
    every second would starve it. The cap makes the property hold under both.
    """
    from app.services.simulation_ipc import _inflight

    held = []
    for _ in range(MAX_INFLIGHT_CALLS):
        assert _inflight.acquire(timeout=1), "could not fill the gate"
        held.append(True)
    try:
        client = ControlClient(socket_path(short_tmp), timeout=30.0)
        started = time.monotonic()
        with pytest.raises(ControlPlaneBusy, match="already in flight"):
            client.request("ping")
        assert time.monotonic() - started < 2.0, "a refused caller must not wait"
    finally:
        for _ in held:
            _inflight.release()


def test_the_gate_is_released_even_when_a_call_fails(short_tmp):
    """Otherwise one unreachable worker would leak the whole budget."""
    from app.services.simulation_ipc import _inflight

    client = ControlClient(socket_path(short_tmp), timeout=0.5)
    for _ in range(MAX_INFLIGHT_CALLS + 4):
        with pytest.raises(WorkerUnreachable):
            client.request("ping")

    acquired = [_inflight.acquire(timeout=0.5) for _ in range(MAX_INFLIGHT_CALLS)]
    try:
        assert all(acquired), "slots were leaked by failed calls"
    finally:
        for taken in acquired:
            if taken:
                _inflight.release()


async def test_the_cap_still_allows_normal_concurrent_use(short_tmp):
    """It bounds pathological callers, not ordinary polling."""
    async def on_ping(_request):
        await asyncio.sleep(0.05)
        return {"pong": True}

    server = await start_server(socket_path(short_tmp), {"ping": on_ping})
    client = ControlClient(socket_path(short_tmp), timeout=5.0)
    try:
        results = await asyncio.gather(
            *(call(client, "ping") for _ in range(MAX_INFLIGHT_CALLS)))
        assert all(r == {"pong": True} for r in results)
    finally:
        await server.close()


def test_a_busy_control_plane_is_a_503_over_http(tmp_path, config, monkeypatch):
    """A caller can retry; it is not a server fault."""
    from app.main import create_app
    from app.services.simulation_config_generator import SimulationConfig
    from app.services.simulation_store import SimulationStore
    from app.services.tasks import TaskStore

    store = SimulationStore(tmp_path / "sims")
    meta = store.create(SimulationConfig.model_validate({
        "graph_id": "g-1", "event": "e", "rounds": 2,
        "broadcaster": {"name": "Wire"}, "seed_posts": [{"content": "c"}]}))

    class BusyManager:
        def env_status(self, sim_id, **kwargs):
            raise ControlPlaneBusy("8 control calls are already in flight")

        def is_running(self, sim_id):
            return False

    class Runtime:
        config = None
        sims = store
        manager = BusyManager()
        tasks = TaskStore(tmp_path / "tasks.db")
        runner = None

    runtime = Runtime()
    runtime.config = config
    monkeypatch.setattr("app.api.simulation.get_runtime", lambda **_: runtime)

    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as client:
        response = client.post("/api/simulation/env-status",
                               json={"sim_id": meta.sim_id})
    assert response.status_code == 503
    assert response.get_json()["retry_after"] == 1


def _async_reply(payload):
    """Handlers are awaited, so a plain lambda will not do."""
    async def handler(request):
        return payload
    return handler


# --------------------------------------------------------------------------
# The post-run interview window
# --------------------------------------------------------------------------
#
# An agent answers an interview from memory held in the worker process, so when
# the worker exits the population becomes unreachable. Holding it open for a
# while after the run ends gives an operator time to actually type a question.
#
# The window is measured from the *last command*, not from the moment the run
# ended. A fixed window does not solve the problem it exists for: someone
# mid-question at the deadline loses the worker just the same.


async def test_the_window_closes_when_nobody_is_asking(tmp_path):
    server = ControlServer(tmp_path / "c.sock")
    server.handle("ping", _async_reply({"pong": True}))
    await server.start()
    try:
        held = await server.linger(seconds=0.3, poll=0.05)
        assert 0.25 <= held < 2.0, held
    finally:
        await server.close()


async def test_a_zero_window_does_not_linger_at_all(tmp_path):
    server = ControlServer(tmp_path / "c.sock")
    await server.start()
    try:
        assert await server.linger(seconds=0) == 0.0
    finally:
        await server.close()


async def test_USING_THE_WINDOW_KEEPS_IT_OPEN(tmp_path):
    """The whole point: a slow typist must not lose the worker mid-question."""
    import asyncio

    server = ControlServer(tmp_path / "c.sock")
    server.handle("ping", _async_reply({"pong": True}))
    await server.start()

    async def keep_asking():
        client = ControlClient(server.path)
        for _ in range(4):
            await asyncio.sleep(0.15)
            await asyncio.to_thread(client.request, "ping")

    try:
        asking = asyncio.create_task(keep_asking())
        held = await server.linger(seconds=0.3, poll=0.05)
        await asking
        # Four pings 0.15s apart hold a 0.3s idle window well past 0.3s.
        assert held > 0.5, f"the window closed while it was being used ({held})"
    finally:
        await server.close()


async def test_a_command_resets_the_idle_clock(tmp_path):
    import asyncio

    server = ControlServer(tmp_path / "c.sock")
    server.handle("ping", _async_reply({"pong": True}))
    await server.start()
    try:
        await asyncio.sleep(0.1)
        before = server.idle_for()
        client = ControlClient(server.path)
        await asyncio.to_thread(client.request, "ping")
        assert server.idle_for() < before
    finally:
        await server.close()


async def test_an_unknown_command_still_counts_as_use(tmp_path):
    """Somebody is talking to this worker either way."""
    import asyncio

    server = ControlServer(tmp_path / "c.sock")
    await server.start()
    try:
        await asyncio.sleep(0.1)
        before = server.idle_for()
        client = ControlClient(server.path)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(client.request, "no-such-command")
        assert server.idle_for() < before
    finally:
        await server.close()


async def test_THE_WINDOW_ENDS_EARLY_WHEN_ASKED_TO_STOP(tmp_path):
    """A stop must not wait out an interview window nobody is using."""
    server = ControlServer(tmp_path / "c.sock")
    await server.start()
    try:
        held = await server.linger(seconds=30, poll=0.05, should_stop=lambda: True)
        assert held < 1.0, f"a stop waited {held}s for the window"
    finally:
        await server.close()


async def test_the_socket_still_answers_during_the_window(tmp_path):
    import asyncio

    server = ControlServer(tmp_path / "c.sock")
    server.handle("interview", _async_reply({"answer": "still here"}))
    await server.start()
    try:
        window = asyncio.create_task(server.linger(seconds=0.4, poll=0.05))
        client = ControlClient(server.path)
        reply = await asyncio.to_thread(client.request, "interview")
        assert reply["answer"] == "still here"
        await window
    finally:
        await server.close()
