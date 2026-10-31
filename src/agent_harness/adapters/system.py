from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4


class SystemClock:
    def utc_now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return monotonic()


class UuidGenerator:
    def new_id(self) -> str:
        return str(uuid4())
