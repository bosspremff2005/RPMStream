"""The upload queue: ordering, concurrency, cancellation."""

import asyncio

from app.queue.upload_queue import JobState, QueueSnapshot, UploadJob, UploadQueue
from app.telegram.streamer import MediaSource
from app.utils.errors import UploadCancelled


def source(name: str = "Movie.mp4", size: int = 1024) -> MediaSource:
    return MediaSource(file_id="id", file_unique_id=name, file_name=name, size=size)


def make_queue(runner, **kwargs) -> UploadQueue:
    queue = UploadQueue(runner, **kwargs)
    queue.start()
    return queue


async def test_jobs_run_in_arrival_order():
    order: list[str] = []

    async def runner(job: UploadJob) -> None:
        order.append(job.file_name)
        await asyncio.sleep(0.01)

    queue = make_queue(runner, max_workers=1)
    for index in range(5):
        queue.add(UploadJob(user_id=1, chat_id=1, source=source(f"video{index}.mp4")))
    await queue.join()
    await queue.stop()

    assert order == [f"video{index}.mp4" for index in range(5)]
    assert queue.snapshot().processed == 5


async def test_concurrency_is_capped():
    active = 0
    peak = 0
    release = asyncio.Event()

    async def runner(job: UploadJob) -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1

    queue = make_queue(runner, max_workers=2)
    jobs = [UploadJob(user_id=1, chat_id=1, source=source(f"v{i}.mp4")) for i in range(6)]
    for job in jobs:
        assert queue.add(job) is not None

    await asyncio.sleep(0.05)
    assert peak == 2, "exactly MAX_CONCURRENT_UPLOADS jobs may run at once"
    assert len(queue.snapshot().active) == 2
    assert len(queue.snapshot().waiting) == 4

    release.set()
    await queue.join()
    await queue.stop()
    assert peak == 2
    assert queue.snapshot().processed == 6


async def test_queue_positions_are_one_based():
    blocker = asyncio.Event()

    async def runner(job: UploadJob) -> None:
        await blocker.wait()

    queue = make_queue(runner, max_workers=1)
    first = UploadJob(user_id=1, chat_id=1, source=source("a.mp4"))
    second = UploadJob(user_id=1, chat_id=1, source=source("b.mp4"))
    queue.add(first)
    queue.add(second)

    assert queue.position(first.id) == 1
    assert queue.position(second.id) == 2
    blocker.set()
    await queue.join()
    await queue.stop()


async def test_queue_rejects_when_full():
    async def runner(job: UploadJob) -> None:
        await asyncio.sleep(5)

    queue = make_queue(runner, max_workers=1, max_items=2)
    assert queue.add(UploadJob(user_id=1, chat_id=1, source=source("1.mp4"))) == 1
    assert queue.add(UploadJob(user_id=1, chat_id=1, source=source("2.mp4"))) == 2
    assert queue.add(UploadJob(user_id=1, chat_id=1, source=source("3.mp4"))) is None
    await queue.stop()


async def test_cancelling_a_queued_job():
    blocker = asyncio.Event()

    async def runner(job: UploadJob) -> None:
        await blocker.wait()

    queue = make_queue(runner, max_workers=1)
    running = UploadJob(user_id=1, chat_id=1, source=source("running.mp4"))
    waiting = UploadJob(user_id=1, chat_id=1, source=source("waiting.mp4"))
    queue.add(running)
    queue.add(waiting)
    await asyncio.sleep(0.02)

    assert await queue.cancel(waiting.id) is True
    assert waiting.state is JobState.CANCELLED
    assert queue.waiting_count == 0

    blocker.set()
    await queue.join()
    await queue.stop()
    assert running.state is JobState.DONE


async def test_cancelling_a_running_job_keeps_the_worker_alive():
    """Regression: cancelling must not kill the worker task itself."""
    started = asyncio.Event()

    async def runner(job: UploadJob) -> None:
        started.set()
        await asyncio.wait_for(job.cancel_event.wait(), timeout=5)
        raise UploadCancelled("stopped")

    queue = make_queue(runner, max_workers=1)
    victim = UploadJob(user_id=1, chat_id=1, source=source("victim.mp4"))
    queue.add(victim)
    await asyncio.wait_for(started.wait(), timeout=5)

    assert await queue.cancel(victim.id) is True
    for _ in range(100):
        if victim.state is JobState.CANCELLED:
            break
        await asyncio.sleep(0.02)
    assert victim.state is JobState.CANCELLED

    # The same worker must still process the next job.
    processed = asyncio.Event()

    async def quick(job: UploadJob) -> None:
        processed.set()

    queue._runner = quick  # noqa: SLF001 - test hook
    survivor = UploadJob(user_id=1, chat_id=1, source=source("survivor.mp4"))
    queue.add(survivor)
    await asyncio.wait_for(processed.wait(), timeout=5)
    assert survivor.state is JobState.DONE
    await queue.stop()


async def test_failed_job_is_recorded_and_does_not_stop_the_queue():
    calls: list[str] = []

    async def runner(job: UploadJob) -> None:
        calls.append(job.file_name)
        if job.file_name == "boom.mp4":
            raise RuntimeError("kaboom")

    queue = make_queue(runner, max_workers=1)
    boom = UploadJob(user_id=1, chat_id=1, source=source("boom.mp4"))
    fine = UploadJob(user_id=1, chat_id=1, source=source("fine.mp4"))
    queue.add(boom)
    queue.add(fine)
    await queue.join()
    await queue.stop()

    assert boom.state is JobState.FAILED
    assert "kaboom" in boom.error
    assert fine.state is JobState.DONE
    snapshot = queue.snapshot()
    assert snapshot.failed == 1
    assert snapshot.processed == 1


async def test_cancel_all_only_touches_one_user():
    blocker = asyncio.Event()

    async def runner(job: UploadJob) -> None:
        await blocker.wait()

    queue = make_queue(runner, max_workers=1, max_items=10)
    mine = [UploadJob(user_id=7, chat_id=7, source=source(f"mine{i}.mp4")) for i in range(2)]
    theirs = [UploadJob(user_id=8, chat_id=8, source=source(f"theirs{i}.mp4")) for i in range(2)]
    for job in mine + theirs:
        queue.add(job)
    await asyncio.sleep(0.02)

    assert await queue.cancel_all(user_id=8) == 2
    assert theirs[0].state is JobState.CANCELLED
    assert mine[1].state is JobState.QUEUED

    blocker.set()
    await queue.join()
    await queue.stop()


async def test_snapshot_and_jobs_lookup():
    async def runner(job: UploadJob) -> None:
        return None

    queue = make_queue(runner, max_workers=1)
    job = UploadJob(user_id=42, chat_id=42, source=source("only.mp4", size=99))
    queue.add(job)
    await queue.join()
    await queue.stop()

    snapshot = queue.snapshot()
    assert isinstance(snapshot, QueueSnapshot)
    assert snapshot.processed == 1
    assert snapshot.is_empty
    assert queue.get(job.id) is job
    assert queue.jobs(42)[0] is job
    assert queue.jobs(99) == []
    assert job.size == 99
    assert job.elapsed >= 0


async def test_stop_cancels_everything_left():
    blocker = asyncio.Event()

    async def runner(job: UploadJob) -> None:
        await blocker.wait()

    queue = make_queue(runner, max_workers=1)
    queue.add(UploadJob(user_id=1, chat_id=1, source=source("a.mp4")))
    queue.add(UploadJob(user_id=1, chat_id=1, source=source("b.mp4")))
    await asyncio.sleep(0.02)
    await queue.stop()
    assert queue.waiting_count == 0
