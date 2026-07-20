"""Bounded async worker pool for the delta-driven crawl.

The crawl used to ``tg.start_soon`` one task per board: with a rotating window of tens of
thousands of boards that is O(window) coroutines (and O(window) live per-board state) held in
memory at once, and every task races onto the shared global :class:`~anyio.CapacityLimiter` and
the per-host buckets simultaneously. This module replaces that with a fixed pool of ``N``
persistent workers draining a rendezvous queue: memory is **O(workers)** regardless of how many
items are fed, throughput is still bounded by the same downstream limiters (the pool adds no
politeness of its own), and one item raising never sinks its sibling items or the pool.

``run_pool`` is the primitive the crawl controller wires ``_crawl_due`` to: hand it the
interleaved board sequence and an async ``handler`` (e.g. the existing ``grab`` closure). The
work queue is a zero-buffer stream, so the item source is consumed lazily -- only ``N`` items are
ever materialised beyond what the feeder holds, which is what keeps a huge window from being
pulled into memory up front.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Generic, TypeVar

import anyio

__all__ = ["PoolResult", "PoolStats", "run_pool", "stream_pool", "gather"]

Item = TypeVar("Item")
Result = TypeVar("Result")


@dataclass(frozen=True)
class PoolResult(Generic[Item, Result]):
    """Outcome of running ``handler`` against a single work item.

    Exactly one of ``result`` / ``error`` is meaningful: ``error is None`` means ``handler``
    returned ``result`` cleanly; otherwise ``error`` holds the (non-cancellation) exception the
    handler raised and ``result`` is ``None``. Cancellation is never captured here -- it
    propagates out so the pool tears down promptly.
    """

    item: Item
    result: Result | None
    error: Exception | None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class PoolStats:
    """Aggregate counts for one :func:`run_pool` invocation."""

    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[Exception] = field(default_factory=list)


async def run_pool(
    items: Iterable[Item],
    handler: Callable[[Item], Awaitable[Result]],
    *,
    concurrency: int,
    on_result: Callable[[PoolResult[Item, Result]], Awaitable[None]] | None = None,
) -> PoolStats:
    """Drain ``items`` through ``handler`` with at most ``concurrency`` in flight.

    ``N = concurrency`` persistent workers pull from a zero-buffer (rendezvous) queue, so at any
    instant at most ``N`` items are being handled and at most one extra sits queued -- memory is
    O(workers), not O(items), no matter how long ``items`` is.

    Crash isolation: a handler raising is caught per item, recorded on the returned
    :class:`PoolStats` (and surfaced via ``on_result``), and the worker moves to the next item.
    One failing item never cancels a sibling or the pool. Cancellation of the enclosing scope is
    *not* caught -- it tears the pool down as usual.

    ``on_result`` (optional) is awaited once per completed item, in completion order, from inside
    a worker -- keep it cheap and non-blocking (e.g. mutate a dict, push to a stream). Use it to
    stream results without buffering them all; the aggregate counts come back in ``PoolStats``.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")

    stats = PoolStats()
    # Zero-buffer: the feeder blocks until a worker is ready to take the next item, which is what
    # bounds resident memory to the in-flight set rather than the whole (possibly huge) window.
    work_send, work_recv = anyio.create_memory_object_stream[Item](0)

    async def worker() -> None:
        # Each worker owns a clone of the receive end; when the feeder closes the send end and the
        # queue drains, the ``async for`` ends cleanly and the worker exits.
        async with work_recv.clone() as recv:
            async for item in recv:
                try:
                    value = await handler(item)
                except Exception as exc:  # noqa: BLE001 -- per-item isolation is the whole point
                    outcome: PoolResult[Item, Result] = PoolResult(item, None, exc)
                    stats.processed += 1
                    stats.failed += 1
                    stats.errors.append(exc)
                else:
                    outcome = PoolResult(item, value, None)
                    stats.processed += 1
                    stats.succeeded += 1
                if on_result is not None:
                    await on_result(outcome)

    async def feeder() -> None:
        async with work_send:
            for item in items:
                await work_send.send(item)

    async with anyio.create_task_group() as tg:
        for _ in range(concurrency):
            tg.start_soon(worker)
        tg.start_soon(feeder)
    # Close the originals we no longer need; the tasks hold their own clones.
    await work_recv.aclose()
    return stats


@asynccontextmanager
async def stream_pool(
    items: Iterable[Item],
    handler: Callable[[Item], Awaitable[Result]],
    *,
    concurrency: int,
) -> AsyncIterator[AsyncIterator[PoolResult[Item, Result]]]:
    """Streaming form of :func:`run_pool`, as an async context manager over the results.

    Yields an async iterator that produces each :class:`PoolResult` as it completes (completion
    order -- unordered w.r.t. the input). Memory stays O(workers): the result stream is
    zero-buffer, so a worker blocks until the consumer takes the previous result. Both clean
    returns and caught handler errors are surfaced (inspect ``PoolResult.ok``)::

        async with stream_pool(boards, grab, concurrency=64) as results:
            async for res in results:
                ...  # may ``break`` early -- the pool is torn down on exit

    The pool runs in a task bound to this context manager, so exiting the ``async with`` (normally
    or via an early ``break``) always tears the workers down promptly and in the caller's task --
    unlike a bare async generator, which cannot own a task group safely across early close.
    """
    result_send, result_recv = anyio.create_memory_object_stream[PoolResult[Item, Result]](0)

    async def produce() -> None:
        async with result_send:
            await run_pool(items, handler, concurrency=concurrency, on_result=result_send.send)

    async with anyio.create_task_group() as tg:
        tg.start_soon(produce)
        try:
            yield result_recv
        finally:
            # Early break / normal end: cancel the producer FIRST so any worker blocked mid-send
            # gets a clean cancellation (cancelling before closing the receiver, which would
            # instead raise BrokenResourceError into the workers). Then close the receive end.
            tg.cancel_scope.cancel()
            await result_recv.aclose()


async def gather(
    items: Iterable[Item],
    handler: Callable[[Item], Awaitable[Result]],
    *,
    concurrency: int,
) -> list[PoolResult[Item, Result]]:
    """Convenience wrapper: run the pool and collect every :class:`PoolResult` into a list.

    Buffers all results (O(items)); prefer :func:`run_pool` with ``on_result`` or
    :func:`imap_unordered` when the item count is large.
    """
    collected: list[PoolResult[Item, Result]] = []

    async def _collect(outcome: PoolResult[Item, Result]) -> None:
        collected.append(outcome)

    await run_pool(items, handler, concurrency=concurrency, on_result=_collect)
    return collected
