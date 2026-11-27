from pydantic import BaseModel, ConfigDict, Field

from src.constants.workflow import Decision
from src.domain.inputs import CODE


class StripWithdrawCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    strip_id: str = Field(min_length=1, max_length=48, pattern=CODE.pattern)
    reason: str = Field(min_length=4, max_length=1000)


class RevisionReviewCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    decision: Decision
    note: str = Field(min_length=4, max_length=1000)
