"""Shared Pydantic base for user-authored run manifest sections."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
