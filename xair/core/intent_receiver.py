from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field

from xair.core.models import ActionIntent


@dataclass(order=True)
class _QueuedIntent:
    priority: int
    seq: int
    intent: ActionIntent = field(compare=False)


class IntentReceiver:
    """Priority queue for multi-source action intents."""

    def __init__(self) -> None:
        self._counter = itertools.count()
        self._heap: list[_QueuedIntent] = []

    def submit(self, intent: ActionIntent) -> None:
        heapq.heappush(
            self._heap,
            _QueuedIntent(priority=-intent.priority, seq=next(self._counter), intent=intent),
        )

    def pop(self) -> ActionIntent | None:
        if not self._heap:
            return None
        return heapq.heappop(self._heap).intent

    def __len__(self) -> int:
        return len(self._heap)
