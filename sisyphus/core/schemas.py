from pydantic import BaseModel
from typing import Optional, Literal


class BaseResult(BaseModel):
    status: Literal["completed", "failed", "timeout", "cancelled"]
    verdict: Optional[Literal["approved", "changes_requested"]] = None
    summary: str
    risk: Optional[Literal["low", "medium", "high"]] = None
    error: Optional[str] = None
