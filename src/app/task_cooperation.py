"""Helpers for making background task loops yield to interactive requests.

Mirrors the deferral semantics proven in backfill_item_metadata_task: always
make some progress, check the shared interactive-request flag between items,
log the deferral, and let the caller re-enqueue the remainder.
"""

import logging

from app.interactive_requests import interactive_request_active

logger = logging.getLogger(__name__)


class CooperativeRun:
    """Iterate work items while yielding to active interactive browser requests.

    Usage:
        run = CooperativeRun("genre_backfill")
        for item in run.iter(items):
            process(item)
        if run.deferred:
            enqueue(run.remaining_ids)
    """

    def __init__(self, label, *, check_every=1, min_progress=1):
        """Configure the run.

        check_every: check the interactive flag every N items.
        min_progress: never defer before this many items were processed, so
        a busy instance still converges.
        """
        self.label = label
        self.check_every = max(1, check_every)
        self.min_progress = max(0, min_progress)
        self.deferred = False
        self.remaining = []

    def iter(self, items):
        """Yield items until exhausted or an interactive request is active."""
        items = list(items)
        for index, item in enumerate(items):
            if (
                index >= self.min_progress
                and index % self.check_every == 0
                and interactive_request_active()
            ):
                self.deferred = True
                self.remaining = items[index:]
                logger.info(
                    "%s_deferred reason=interactive_request_active "
                    "processed=%s remaining=%s",
                    self.label,
                    index,
                    len(self.remaining),
                )
                return
            yield item

    @property
    def remaining_ids(self):
        """Return the ids of items that were not processed."""
        return [item.id for item in self.remaining]

    def reenqueue_if_deferred(self, enqueue):
        """Hand unprocessed item ids back to the given enqueue callable."""
        if self.deferred and self.remaining:
            enqueue(self.remaining_ids)
