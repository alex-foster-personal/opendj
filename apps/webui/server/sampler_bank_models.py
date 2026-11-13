"""Classic sampler banks wire models (``GET`` and ``PUT /api/v1/sampler/banks``), snake_case.

The HTTP twin of ``apps.shared.state.sampler_banks``: the same modes, gain ranges and 4 x 16
shape, so a bad body is a 422 before anything is written. Every model forbids extra fields.
``master_gain_db`` (#9989, -60..0 dB) is optional in a PUT body and means 0 dB when left out.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from apps.shared.state import sampler_banks as store

#: Must equal ``sampler_banks.MODES`` (a test pins it).
BankMode = Literal["one_shot", "loop", "gate"]


class BankSlot(BaseModel):
    """One loaded slot: a library track (``stable_id``) or a file (``path``), its mode, gain and title."""

    model_config = ConfigDict(extra="forbid")

    stable_id: str | None = Field(default=None, min_length=1)
    path: str | None = Field(default=None, min_length=1)
    mode: BankMode
    gain_db: float = Field(ge=store.GAIN_DB_MIN, le=store.GAIN_DB_MAX, allow_inf_nan=False)
    title: str

    @model_validator(mode="after")
    def _one_source(self) -> BankSlot:
        if (self.stable_id is None) == (self.path is None):
            raise ValueError("a slot names exactly one of stable_id or path")
        return self


class BankSlotView(BankSlot):
    """A slot as GET serves it: ``missing`` is true when its ``stable_id`` no longer resolves."""

    missing: bool


Bank = Annotated[list[BankSlot | None], Field(min_length=store.SLOTS_PER_BANK, max_length=store.SLOTS_PER_BANK)]
BankView = Annotated[list[BankSlotView | None], Field(min_length=store.SLOTS_PER_BANK, max_length=store.SLOTS_PER_BANK)]


class BanksPut(BaseModel):
    """The whole banks document, replaced at once."""

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    active_bank: int = Field(ge=0, lt=store.BANK_COUNT)
    master_gain_db: float = Field(
        default=store.MASTER_GAIN_DB_DEFAULT,
        ge=store.MASTER_GAIN_DB_MIN,
        le=store.MASTER_GAIN_DB_MAX,
        allow_inf_nan=False,
    )
    banks: list[Bank] = Field(min_length=store.BANK_COUNT, max_length=store.BANK_COUNT)


class BanksView(BaseModel):
    """The banks document as GET and PUT answer it, each loaded slot marked ``missing`` or not."""

    v: Literal[1]
    active_bank: int
    master_gain_db: float
    banks: list[BankView]


class CaptureStored(BaseModel):
    """A stored capture (``POST /api/v1/sampler/captures``): ``path`` is what a bank slot's ``path`` stores."""

    path: str
    duration_ms: int
