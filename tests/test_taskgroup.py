import asyncio
import sys

import pytest

from aiotools import (
    TaskGroup,
    TaskGroupError,
    VirtualClock,
)


@pytest.mark.asyncio
async def test_delayed_subtasks():
    with VirtualClock().patch_loop():
        async with TaskGroup() as tg:
            t1 = tg.create_task(asyncio.sleep(3, 'a'))
            t2 = tg.create_task(asyncio.sleep(2, 'b'))
            t3 = tg.create_task(asyncio.sleep(1, 'c'))
        assert t1.done()
        assert t2.done()
        assert t3.done()
        assert t1.result() == 'a'
        assert t2.result() == 'b'
        assert t3.result() == 'c'


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.version_info < (3, 7),
    reason='contextvars is available only in Python 3.7 or later',
)
async def test_contextual_taskgroup():

    from aiotools import current_taskgroup

    refs = []

    async def check_tg(delay):
        await asyncio.sleep(delay)
        refs.append(current_taskgroup.get())

    with VirtualClock().patch_loop():
        async with TaskGroup() as outer_tg:
            ot1 = outer_tg.create_task(check_tg(0.1))
            async with TaskGroup() as inner_tg:
                it1 = inner_tg.create_task(check_tg(0.2))
            ot2 = outer_tg.create_task(check_tg(0.3))
        assert ot1.done()
        assert ot2.done()
        assert it1.done()
        assert refs == [outer_tg, inner_tg, outer_tg]

        with pytest.raises(LookupError):
            # outside of any taskgroup, this is an error.
            current_taskgroup.get()


@pytest.mark.skipif(
    sys.version_info < (3, 7),
    reason='contextvars is available only in Python 3.7 or later',
)
@pytest.mark.filterwarnings('ignore::RuntimeWarning')
@pytest.mark.asyncio
async def test_contextual_taskgroup_spawning():

    from aiotools import current_taskgroup

    total_jobs = 0

    async def job():
        nonlocal total_jobs
        await asyncio.sleep(0)
        total_jobs += 1

    async def spawn_job():
        await asyncio.sleep(0)
        tg = current_taskgroup.get()
        tg.create_task(job())

    async def inner_tg_job():
        await asyncio.sleep(0)
        async with TaskGroup() as tg:
            tg.create_task(job())

    with VirtualClock().patch_loop():

        total_jobs = 0
        with pytest.raises(TaskGroupError), pytest.warns(RuntimeWarning):
            # When the taskgroup terminates immediately after spawning subtasks,
            # the spawned subtasks may not be allowed to proceed because the parent
            # taskgroup is already in the terminating procedure.
            async with TaskGroup() as tg:
                t = tg.create_task(spawn_job())
            assert not t.done()
        assert total_jobs == 0

        total_jobs = 0
        async with TaskGroup() as tg:
            tg.create_task(inner_tg_job())
            tg.create_task(spawn_job())
            tg.create_task(inner_tg_job())
            tg.create_task(spawn_job())
            # Give the subtasks chances to run.
            await asyncio.sleep(1)
        assert total_jobs == 4


@pytest.mark.asyncio
async def test_taskgroup_cancellation():
    with VirtualClock().patch_loop():

        async def do_job(delay, result):
            # NOTE: replacing do_job directly with asyncio.sleep
            #       results future-pending-after-loop-closed error,
            #       because asyncio.sleep() is not a task but a future.
            await asyncio.sleep(delay)
            return result

        with pytest.raises(asyncio.CancelledError):
            async with TaskGroup() as tg:
                t1 = tg.create_task(do_job(0.3, 'a'))
                t2 = tg.create_task(do_job(0.6, 'b'))
                await asyncio.sleep(0.5)
                raise asyncio.CancelledError

        assert t1.done()
        assert t2.cancelled()
        assert t1.result() == 'a'


@pytest.mark.asyncio
async def test_subtask_cancellation():

    results = []

    async def do_job():
        await asyncio.sleep(1)
        results.append('a')

    async def do_cancel():
        await asyncio.sleep(0.5)
        raise asyncio.CancelledError

    with VirtualClock().patch_loop():
        async with TaskGroup() as tg:
            t1 = tg.create_task(do_job())
            t2 = tg.create_task(do_cancel())
            t3 = tg.create_task(do_job())
        assert t1.done()
        assert t2.cancelled()
        assert t3.done()
        assert results == ['a', 'a']