from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from crv_agent.domain.planning import Contract


class ChatRequest(Contract):
    thread_id: UUID
    request_id: UUID
    message: str = Field(min_length=1, max_length=8000)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class FeedbackRequest(Contract):
    thread_id: UUID
    request_id: UUID
    rating: Literal[-1, 1]
    comment: str = Field(default="", max_length=4000)
