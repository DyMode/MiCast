"""Process-wide serialized, rollback-safe runtime configuration changes.

State invariants:

- Persistence happens INSIDE ``mutate``: config setters call
  ``save_to_file()`` as part of the mutation, so a committed mutate is
  already on disk before any runtime work starts.
- The transaction only ever rolls back IN-MEMORY state
  (``settings.restore(snapshot)``); restore persists again, rewriting the
  disk file with the pre-mutate snapshot, so a failed apply never leaves
  the file describing state the runtime does not actually have.
- ``applied`` distinguishes where the failure happened: ``False`` means
  ``mutate`` itself raised (memory unchanged, nothing persisted, runtime
  never touched — no rollback call at all); ``True`` means the mutation
  committed and the runtime apply failed, so the runtime must be rolled
  back (``rollback_runtime``, falling back to re-running ``apply_runtime``).
- Staleness checks (e.g. the EQ ``expected_revision`` guard) must run as
  the FIRST step of ``mutate``, i.e. already inside this lock — checking
  before calling :func:`apply_config_transaction` leaves a TOCTOU window
  where another commit can land between the check and lock acquisition.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from micast.config import settings

logger = logging.getLogger(__name__)

_lock = asyncio.Lock()


async def run_config_runtime(work: Callable[[], Awaitable[object]]) -> object:
    """Serialize recovery with configuration without writing or rolling back settings."""
    async with _lock:
        return await work()


async def apply_config_transaction(
    mutate: Callable[[], object],
    apply_runtime: Callable[[], Awaitable[None]],
    rollback_runtime: Callable[[], Awaitable[None]] | None = None,
) -> object:
    """Persist and apply one mutation, restoring both layers on failure.

    This lock is process-wide: settings, receiver, group and tuning routes can
    no longer interleave independent read/modify/write cycles.
    """
    async with _lock:
        snapshot = settings.snapshot()
        applied = False
        try:
            result = mutate()
            applied = True
            await apply_runtime()
            return result
        except (Exception, asyncio.CancelledError):
            settings.restore(snapshot)
            if applied:
                # The mutation committed before runtime application failed
                # or was cancelled. Restore the previous runtime as well.
                try:
                    await (rollback_runtime or apply_runtime)()
                except (Exception, asyncio.CancelledError):
                    logger.exception("Runtime config rollback failed")
            raise
