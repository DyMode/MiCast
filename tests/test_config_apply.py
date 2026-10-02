"""apply_config_transaction unit tests: snapshot/restore discipline and locking.

A FakeSettings stands in for micast.config.settings so snapshot/restore calls
are recorded without touching disk; apply/rollback runtimes are AsyncMocks.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

import micast.config_apply
from micast.config_apply import apply_config_transaction


class FakeSettings:
    def __init__(self):
        self.snapshots: list[str] = []
        self.restores: list[tuple[str, bool]] = []

    def snapshot(self) -> str:
        snap = f"snap-{len(self.snapshots)}"
        self.snapshots.append(snap)
        return snap

    def restore(self, snapshot: str, *, persist: bool = True) -> None:
        self.restores.append((snapshot, persist))


@pytest.fixture
def fake_settings(monkeypatch):
    fake = FakeSettings()
    monkeypatch.setattr(micast.config_apply, "settings", fake)
    return fake


@pytest.mark.asyncio
async def test_success_path_snapshots_applies_without_rollback(fake_settings):
    apply_runtime = AsyncMock()
    rollback_runtime = AsyncMock()

    result = await apply_config_transaction(lambda: "mutated", apply_runtime, rollback_runtime)

    assert result == "mutated"
    assert fake_settings.snapshots == ["snap-0"]
    assert fake_settings.restores == []
    apply_runtime.assert_awaited_once()
    rollback_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutate_failure_restores_memory_without_runtime_rollback(fake_settings):
    """mutate raised → nothing committed, runtime never ran, no rollback call."""
    apply_runtime = AsyncMock()
    rollback_runtime = AsyncMock()

    def mutate_boom():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await apply_config_transaction(mutate_boom, apply_runtime, rollback_runtime)

    # Memory rolled back with the pre-mutate snapshot, persisted to disk.
    assert fake_settings.restores == [("snap-0", True)]
    apply_runtime.assert_not_awaited()
    rollback_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_failure_rolls_back_with_prerecorded_rollback(fake_settings):
    apply_runtime = AsyncMock(side_effect=RuntimeError("apply failed"))
    rollback_runtime = AsyncMock()

    with pytest.raises(RuntimeError, match="apply failed"):
        await apply_config_transaction(lambda: "ok", apply_runtime, rollback_runtime)

    assert fake_settings.restores == [("snap-0", True)]
    apply_runtime.assert_awaited_once()
    rollback_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_failure_falls_back_to_apply_runtime_when_no_rollback(fake_settings):
    """rollback_runtime=None → the runtime rollback re-runs apply_runtime."""
    apply_runtime = AsyncMock(side_effect=RuntimeError("apply failed"))

    with pytest.raises(RuntimeError, match="apply failed"):
        await apply_config_transaction(lambda: "ok", apply_runtime)

    assert apply_runtime.await_count == 2  # failed apply + rollback re-apply
    assert fake_settings.restores == [("snap-0", True)]


@pytest.mark.asyncio
async def test_rollback_failure_is_swallowed_and_original_error_propagates(fake_settings):
    apply_runtime = AsyncMock(side_effect=RuntimeError("apply failed"))
    rollback_runtime = AsyncMock(side_effect=RuntimeError("rollback failed"))

    with pytest.raises(RuntimeError, match="apply failed"):
        await apply_config_transaction(lambda: "ok", apply_runtime, rollback_runtime)

    rollback_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_transactions_do_not_interleave(fake_settings):
    """Two transactions must run strictly serially: tx_two's mutate cannot
    start while tx_one still holds the lock inside its apply_runtime."""
    one_in_apply = asyncio.Event()
    release = asyncio.Event()
    order: list[str] = []

    async def one_apply():
        order.append("one-apply")
        one_in_apply.set()
        await release.wait()

    async def tx_one():
        def mutate():
            order.append("one-mutate")
            return None

        await apply_config_transaction(mutate, one_apply)

    async def tx_two():
        def mutate():
            # Serializability proof: tx_one already reached apply_runtime
            # before this mutate runs — with a broken lock tx_two would have
            # run first and this event would not be set.
            assert one_in_apply.is_set()
            order.append("two-mutate")
            return None

        await apply_config_transaction(mutate, AsyncMock())

    one = asyncio.create_task(tx_one())
    await one_in_apply.wait()
    two = asyncio.create_task(tx_two())
    await asyncio.sleep(0)
    assert not two.done()  # blocked on the lock, not interleaved
    release.set()
    await asyncio.gather(one, two)

    assert order == ["one-mutate", "one-apply", "two-mutate"]
    assert fake_settings.snapshots == ["snap-0", "snap-1"]


async def test_cancelled_apply_restores_persisted_and_runtime_state(fake_settings):
    entered = asyncio.Event()

    async def apply():
        entered.set()
        await asyncio.Future()

    rollback = AsyncMock()
    transaction = asyncio.create_task(
        apply_config_transaction(lambda: "committed", apply, rollback)
    )
    await entered.wait()
    transaction.cancel()
    with pytest.raises(asyncio.CancelledError):
        await transaction
    assert fake_settings.restores == [("snap-0", True)]
    rollback.assert_awaited_once()
