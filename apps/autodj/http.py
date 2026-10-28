"""The composition root for ``apps.autodj`` (ADR-NEW-module-contracts).

Delivery layers call :func:`build_service` and hold the result as the
contract's :class:`~apps.autodj.contract.service.AutoDjService` Protocol.
"""

from __future__ import annotations

from apps.autodj import styles
from apps.autodj.contract.models import StyleSummary
from apps.autodj.contract.service import AutoDjService


class _Service:
    def list_styles(self) -> list[StyleSummary]:
        return [StyleSummary.model_validate(doc) for doc in styles.list_styles()]


def build_service() -> AutoDjService:
    return _Service()
