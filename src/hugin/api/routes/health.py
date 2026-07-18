from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from hugin import __version__


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: Literal["hugin"] = "hugin"
    version: str = __version__


router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()
