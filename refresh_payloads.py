"""Immutable payloads used to move refresh results into UI state."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Mapping

import pandas as pd

from bot.picking_labels import PickingOrder


@dataclass(frozen=True)
class PendingPayload:
    dataframe: pd.DataFrame
    logs: tuple[str, ...]


@dataclass(frozen=True)
class PickingPayload:
    orders: tuple[PickingOrder, ...]
    warnings: tuple[str, ...]
    diagnostics: Mapping[str, object]


def copy_pending_payload(payload: PendingPayload) -> PendingPayload:
    return PendingPayload(
        dataframe=payload.dataframe.copy(deep=True),
        logs=tuple(payload.logs),
    )


def copy_picking_payload(payload: PickingPayload) -> PickingPayload:
    return PickingPayload(
        orders=deepcopy(payload.orders),
        warnings=tuple(payload.warnings),
        diagnostics=deepcopy(payload.diagnostics),
    )
