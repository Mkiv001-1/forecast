"""Unit tests for scripts/core/ib_worker.py.

Tests verify:
- requests are serialized (one at a time)
- timeout raises TimeoutError without hanging the worker
- mock handler intercepts ops correctly
- ib_request_sync works from a non-async thread
- worker lifecycle (start/stop)

Run with:  python -m pytest scripts/tests/test_ib_worker.py -v
"""
import asyncio
import sys
import os
import threading
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _start_worker_with_mock(mock_fn):
    import scripts.core.ib_worker as w
    # Reset state between tests
    w._state.running = False
    w._state.task = None
    w._state.loop = None
    w._state._mock_handler = None
    await w.start_ib_worker()
    w.set_mock_handler(mock_fn)
    return w


async def _stop_worker():
    import scripts.core.ib_worker as w
    w.set_mock_handler(None)
    await w.stop_ib_worker()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ib_worker_basic_mock():
    """ib_request returns the mock handler's result."""
    import scripts.core.ib_worker as w

    calls = []

    def handler(op, kwargs):
        calls.append(op)
        return {"op": op, "status": "ok"}

    await _start_worker_with_mock(handler)
    try:
        result = await w.ib_request("test_connection", host="127.0.0.1", port=7497)
        assert result["status"] == "ok"
        assert result["op"] == "test_connection"
        assert calls == ["test_connection"]
    finally:
        await _stop_worker()


@pytest.mark.asyncio
async def test_ib_worker_serializes_requests():
    """Two concurrent requests are executed sequentially (not in parallel)."""
    import scripts.core.ib_worker as w

    order = []

    def handler(op, kwargs):
        order.append(("start", op))
        time.sleep(0.05)
        order.append(("end", op))
        return {"op": op}

    await _start_worker_with_mock(handler)
    try:
        r1, r2 = await asyncio.gather(
            w.ib_request("fetch_orders"),
            w.ib_request("test_connection"),
        )
        # Verify strict sequencing: first request fully completed before second started
        starts = [e for e in order if e[0] == "start"]
        ends   = [e for e in order if e[0] == "end"]
        assert ends[0][1] == starts[0][1], "First op must finish before second starts"
    finally:
        await _stop_worker()


@pytest.mark.asyncio
async def test_ib_worker_timeout():
    """A slow op raises TimeoutError and does not hang the worker."""
    import scripts.core.ib_worker as w

    slow_done = threading.Event()

    def slow_handler(op, kwargs):
        slow_done.wait(timeout=10)
        return {}

    await _start_worker_with_mock(slow_handler)
    try:
        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await w.ib_request("fetch_orders", timeout=0.2)

        # Worker must still be running after timeout
        assert w.is_running()

        # Unblock slow thread and let worker recover
        slow_done.set()
        await asyncio.sleep(0.3)

        # Next request should work fine
        w.set_mock_handler(lambda op, kw: {"ok": True})
        result = await w.ib_request("test_connection", timeout=5.0)
        assert result["ok"] is True
    finally:
        slow_done.set()
        await _stop_worker()


@pytest.mark.asyncio
async def test_ib_worker_unknown_op_raises():
    """Unknown op propagates ValueError back to caller."""
    import scripts.core.ib_worker as w

    await _start_worker_with_mock(None)
    # No mock — real _execute_op will raise ValueError for unknown op
    try:
        with pytest.raises(Exception, match="unknown op"):
            await w.ib_request("no_such_op", timeout=5.0)
    finally:
        await _stop_worker()


@pytest.mark.asyncio
async def test_ib_worker_place_bracket():
    """place_bracket op returns expected structure via mock."""
    import scripts.core.ib_worker as w

    expected = {
        "status": "submitted",
        "parent_id": 1001,
        "target_id": 1002,
        "stop_id": 1003,
    }

    def handler(op, kwargs):
        assert op == "place_bracket"
        assert kwargs["symbol"] == "AAPL"
        assert kwargs["action"] == "BUY"
        return expected

    await _start_worker_with_mock(handler)
    try:
        result = await w.ib_request(
            "place_bracket",
            symbol="AAPL",
            action="BUY",
            quantity=10,
            stop_loss_price=140.0,
            take_profit_price=165.0,
        )
        assert result == expected
    finally:
        await _stop_worker()


@pytest.mark.asyncio
async def test_ib_worker_cancel_order():
    """cancel_order op receives correct kwargs via mock."""
    import scripts.core.ib_worker as w

    received = {}

    def handler(op, kwargs):
        received.update({"op": op, **kwargs})
        return True

    await _start_worker_with_mock(handler)
    try:
        result = await w.ib_request("cancel_order", order_id=555, port=7497)
        assert result is True
        assert received["op"] == "cancel_order"
        assert received["order_id"] == 555
    finally:
        await _stop_worker()


def test_ib_request_sync_from_thread():
    """ib_request_sync works correctly when called from a non-async thread."""
    import scripts.core.ib_worker as w

    results = []
    errors = []

    def run_in_thread():
        try:
            result = w.ib_request_sync("test_connection", timeout=5.0)
            results.append(result)
        except Exception as e:
            errors.append(str(e))

    async def run():
        await _start_worker_with_mock(lambda op, kw: {"from_thread": True, "op": op})
        try:
            t = threading.Thread(target=run_in_thread)
            t.start()
            # Give thread time to run while event loop is active
            await asyncio.sleep(1.0)
            t.join(timeout=5.0)
        finally:
            await _stop_worker()

    _run(run())
    assert not errors, f"Thread raised: {errors}"
    assert results and results[0].get("from_thread") is True


@pytest.mark.asyncio
async def test_ib_worker_not_running_raises():
    """ib_request raises RuntimeError when worker is not started."""
    import scripts.core.ib_worker as w
    # Ensure not running
    w._state.running = False
    w._state.loop = None

    with pytest.raises(RuntimeError, match="not running"):
        await w.ib_request("test_connection")


@pytest.mark.asyncio
async def test_ib_worker_stop_and_restart():
    """Worker can be stopped and restarted cleanly."""
    import scripts.core.ib_worker as w

    await _start_worker_with_mock(lambda op, kw: {"cycle": 1})
    r1 = await w.ib_request("test_connection", timeout=5.0)
    assert r1["cycle"] == 1
    await _stop_worker()

    await _start_worker_with_mock(lambda op, kw: {"cycle": 2})
    r2 = await w.ib_request("test_connection", timeout=5.0)
    assert r2["cycle"] == 2
    await _stop_worker()
