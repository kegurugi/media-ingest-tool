"""Propose a shoot date from clip creation times. The operator confirms or overrides."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, List, Optional


@dataclass
class ShootDateProposal:
    proposed: Optional[date]  # None when no clip dates are available; never guessed
    dates: List[date] = field(default_factory=list)  # every distinct clip date, sorted

    @property
    def spans_midnight(self) -> bool:
        """Clips fall on more than one calendar date: the operator must decide."""
        return len(self.dates) > 1


def propose_shoot_date(clip_times: Iterable[datetime]) -> ShootDateProposal:
    """Propose the earliest clip date, and list every date the clips fall on."""
    dates = sorted({t.date() for t in clip_times})
    return ShootDateProposal(proposed=dates[0] if dates else None, dates=dates)
