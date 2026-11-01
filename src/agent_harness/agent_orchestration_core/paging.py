"""Boundary-aware, byte-bounded paging for task input frames."""

from dataclasses import dataclass

from agent_harness.domain.errors import require


@dataclass(frozen=True)
class PageFrame:
    page_id: str
    text: str
    byte_start: int
    byte_end: int

    def __post_init__(self) -> None:
        require(bool(self.page_id.strip()), "Page identifier is required")
        require(0 <= self.byte_start <= self.byte_end, "Invalid page byte range")
        require(len(self.text.encode("utf-8")) <= 131_072, "Page is too large")


class ContextPager:
    """Splits UTF-8 text without cutting inside a code point or preferred boundary."""

    def __init__(self, frame_bytes: int, overlap_bytes: int = 0) -> None:
        require(256 <= frame_bytes <= 131_072, "Frame size must be 256..131072 bytes")
        require(0 <= overlap_bytes < frame_bytes // 2, "Overlap must be less than half a frame")
        self._frame_bytes = frame_bytes
        self._overlap_bytes = overlap_bytes

    def page(self, payload: str) -> tuple[PageFrame, ...]:
        require(isinstance(payload, str), "Payload must be text")
        if not payload:
            return ()
        pages: list[PageFrame] = []
        start = 0
        byte_offset = 0
        while start < len(payload):
            end = self._fit(payload, start)
            if end <= start:
                end = min(len(payload), start + 1)
            text = payload[start:end]
            encoded = text.encode("utf-8")
            pages.append(
                PageFrame(f"page-{len(pages) + 1}", text, byte_offset, byte_offset + len(encoded))
            )
            if end == len(payload):
                break
            next_start = self._overlap_start(payload, start, end)
            # A very short boundary-selected page can be smaller than the overlap.
            # Preserve forward progress rather than repeatedly emitting that page.
            if next_start <= start:
                next_start = end
            byte_offset += len(payload[start:next_start].encode("utf-8"))
            start = next_start
        return tuple(pages)

    def _fit(self, payload: str, start: int) -> int:
        used = 0
        end = start
        while end < len(payload):
            width = len(payload[end].encode("utf-8"))
            if used + width > self._frame_bytes:
                break
            used += width
            end += 1
        if end == len(payload):
            return end
        candidates = ("\n\n", "\n", ". ", "? ", "! ", " ")
        prefix = payload[start:end]
        for delimiter in candidates:
            position = prefix.rfind(delimiter)
            if position > 0:
                return start + position + len(delimiter)
        return end

    def _overlap_start(self, payload: str, start: int, end: int) -> int:
        if not self._overlap_bytes:
            return end
        used = 0
        cursor = end
        while cursor > start:
            width = len(payload[cursor - 1].encode("utf-8"))
            if used + width > self._overlap_bytes:
                break
            used += width
            cursor -= 1
        return cursor
