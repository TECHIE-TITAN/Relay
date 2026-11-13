from __future__ import annotations

import asyncio

import pytest
from helpers import ctx_for

from relay.admission import Admission, Shed
from relay.tracing import EventLog


def mk(n=1, cap=10, **kw):
    base = dict(newest_first_after_s=100.0, min_service_s=0.1, initial_service_s=0.1, estimate_check=False)
    base.update(kw)
    return Admission(n, cap, **base)


def test_free_slot_is_taken_immediately():
    async def go():
        a = mk(n=2)
        log = EventLog(None)
        w1 = await a.enter(ctx_for(log))
        w2 = await a.enter(ctx_for(log))
        return a.snapshot(), w1, w2
    snap, w1, w2 = asyncio.run(go())
    assert snap["inflight"] == 2 and w1 == 0.0 and w2 == 0.0


def test_fifo_order_when_not_stale():
    async def go():
        a = mk(n=1)
        log = EventLog(None)
        await a.enter(ctx_for(log, rid="first"))
        order = []

        async def waiter(rid):
            await a.enter(ctx_for(log, rid=rid))
            order.append(rid)
            a.release(0.01)

        tasks = [asyncio.ensure_future(waiter(r)) for r in ("a", "b", "c")]
        await asyncio.sleep(0.02)
        a.release(0.01)
        await asyncio.gather(*tasks)
        return order, a.snapshot()
    order, snap = asyncio.run(go())
    assert order == ["a", "b", "c"] and snap["inflight"] == 0 and snap["queue_len"] == 0


def test_newest_first_when_head_has_waited_too_long():
    async def go():
        a = mk(n=1, newest_first_after_s=0.0)
        log = EventLog(None)
        await a.enter(ctx_for(log, rid="busy"))
        order = []

        async def waiter(rid):
            await a.enter(ctx_for(log, rid=rid))
            order.append(rid)
            a.release(0.01)

        tasks = []
        for r in ("old", "mid", "new"):
            tasks.append(asyncio.ensure_future(waiter(r)))
            await asyncio.sleep(0.01)
        a.release(0.01)
        await asyncio.gather(*tasks)
        return order
    assert asyncio.run(go())[0] == "new"


def test_queue_full_sheds_immediately():
    async def go():
        a = mk(n=1, cap=1)
        log = EventLog(None)
        await a.enter(ctx_for(log))
        t = asyncio.ensure_future(a.enter(ctx_for(log, deadline_s=1.0)))
        await asyncio.sleep(0.01)
        with pytest.raises(Shed) as e:
            await a.enter(ctx_for(log))
        t.cancel()
        return e.value.reason, a.snapshot()["shed"]
    reason, shed = asyncio.run(go())
    assert reason == "queue_full" and shed == {"queue_full": 1}


def test_infeasible_wait_is_shed_by_estimate():
    async def go():
        a = mk(n=1, estimate_check=True, initial_service_s=10.0)
        log = EventLog(None)
        await a.enter(ctx_for(log))
        with pytest.raises(Shed) as e:
            await a.enter(ctx_for(log, deadline_s=5.0))
        return e.value.reason
    assert asyncio.run(go()) == "infeasible"


def test_request_that_waits_past_its_deadline_leaves_the_queue():
    async def go():
        a = mk(n=1)
        log = EventLog(None)
        await a.enter(ctx_for(log))
        with pytest.raises(Shed) as e:
            await a.enter(ctx_for(log, deadline_s=0.15))
        return e.value.reason, a.snapshot()
    reason, snap = asyncio.run(go())
    assert reason == "deadline_in_queue" and snap["queue_len"] == 0 and snap["inflight"] == 1


def test_expired_request_is_dropped_at_dequeue():
    async def go():
        a = mk(n=1, min_service_s=0.3)
        log = EventLog(None)
        await a.enter(ctx_for(log))
        t = asyncio.ensure_future(a.enter(ctx_for(log, deadline_s=0.5)))
        await asyncio.sleep(0.25)
        a.release(0.01)               # only 0.25 s left, below min_service_s
        with pytest.raises(Shed) as e:
            await t
        return e.value.reason, a.snapshot()
    reason, snap = asyncio.run(go())
    assert reason == "expired_in_queue" and snap["inflight"] == 0


def test_cancelled_waiter_does_not_leak_a_slot_or_queue_entry():
    async def go():
        a = mk(n=1)
        log = EventLog(None)
        await a.enter(ctx_for(log))
        t = asyncio.ensure_future(a.enter(ctx_for(log)))
        await asyncio.sleep(0.02)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        snap = a.snapshot()
        a.release(0.01)
        return snap, a.snapshot()
    snap, after = asyncio.run(go())
    assert snap["queue_len"] == 0 and snap["inflight"] == 1 and after["inflight"] == 0


def test_no_time_left_is_shed_and_service_estimate_follows_slot_hold_time():
    async def go():
        a = mk(n=1, initial_service_s=1.0)
        log = EventLog(None)
        with pytest.raises(Shed) as e:
            await a.enter(ctx_for(log, deadline_s=0.05))
        await a.enter(ctx_for(log))
        a.release(5.0)
        return e.value.reason, a.svc
    reason, svc = asyncio.run(go())
    assert reason == "no_time_left" and abs(svc - (0.8 * 1.0 + 0.2 * 5.0)) < 1e-9
