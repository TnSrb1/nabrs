"""The Viber delivery adapter — a STUB (ARCHITECTURE §15.1).

Viber is outside the Sprint-17 boundaries (Telegram only). The class exists for completeness of the
channel registry and forward compatibility; ``send`` raises
``NotImplementedError``. In the MVP the delivery worker does not pick viber rows from the
outbox (the filter ``enabled_channels``), so it does not get into the working cycle.
"""

from __future__ import annotations

from src.workers.adapters.base import (
    DeliveryAdapter,
    DeliveryJob,
    DeliveryResult,
    FormattedMessage,
)


class ViberAdapter(DeliveryAdapter):
    """A stub of the Viber channel (the implementation — a separate sprint)."""

    channel = "viber"

    def supports_buttons(self) -> bool:
        return True

    def format(self, job: DeliveryJob) -> FormattedMessage:
        raise NotImplementedError(
            "ViberAdapter.format is not implemented (a later sprint)"
        )

    async def send(self, job: DeliveryJob, message: FormattedMessage) -> DeliveryResult:
        raise NotImplementedError(
            "ViberAdapter.send is not implemented (a later sprint)"
        )
