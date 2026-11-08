"""P7 批次 2：文字回复流到完整句段的纯内存契约。

本模块只把单个 ``reply_id`` 的有序文字块转换为不可变句段。它不调用 TTS
Provider、不生成音频、不接触播放设备，也不把“文字已生成”解释为“用户已听到”。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256

from app.contracts.base import ContractError, require_identifier, require_positive


class SegmentationError(ValueError):
    """分段配置或本地队列操作违反领域约束。"""


class SegmentBoundary(str, Enum):
    """形成完整句段的边界原因。"""

    PUNCTUATION = "punctuation"
    NEWLINE = "newline"
    LENGTH = "length"
    FINAL = "final"


class SegmentationDecision(str, Enum):
    """文字块提交结果；拒绝结果均不得改变 segmenter 状态。"""

    ACCEPTED = "accepted"
    EMPTY_TEXT = "empty_text"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    STALE_REPLY = "stale_reply"
    QUEUE_BUDGET_EXCEEDED = "queue_budget_exceeded"
    AFTER_CANCELLED = "after_cancelled"
    AFTER_TERMINAL = "after_terminal"


class ReplySegmentationState(str, Enum):
    OPEN = "open"
    TERMINAL = "terminal"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SegmentationConfig:
    """完整句切分与未消费句段队列的有界配置。"""

    min_segment_characters: int = 4
    max_segment_characters: int = 120
    max_queued_segments: int = 8
    # Optional real-time length policy. When omitted, the historical
    # max-length behaviour remains unchanged for every existing caller.
    soft_target_characters: int | None = None
    soft_search_min_characters: int | None = None
    soft_search_max_characters: int | None = None

    def __post_init__(self) -> None:
        try:
            require_positive("min_segment_characters", self.min_segment_characters)
            require_positive("max_segment_characters", self.max_segment_characters)
            require_positive("max_queued_segments", self.max_queued_segments)
        except ContractError as exc:
            raise SegmentationError(str(exc)) from exc
        if self.min_segment_characters > self.max_segment_characters:
            raise SegmentationError("min_segment_characters 不得大于 max_segment_characters")

        soft_values = (
            self.soft_target_characters,
            self.soft_search_min_characters,
            self.soft_search_max_characters,
        )
        if any(value is not None for value in soft_values):
            if any(type(value) is not int or value <= 0 for value in soft_values):
                raise SegmentationError("soft length policy must use positive integers")
            assert self.soft_target_characters is not None
            assert self.soft_search_min_characters is not None
            assert self.soft_search_max_characters is not None
            if not (
                self.min_segment_characters
                <= self.soft_search_min_characters
                <= self.soft_target_characters
                <= self.soft_search_max_characters
                <= self.max_segment_characters
            ):
                raise SegmentationError(
                    "soft length policy must satisfy min <= search_min <= target <= search_max <= max"
                )


@dataclass(frozen=True, slots=True)
class TextChunk:
    """来自同一模型回复的有序文字块。"""

    chunk_id: str
    reply_id: str
    sequence: int
    text: str
    is_final: bool = False

    def __post_init__(self) -> None:
        require_identifier("chunk_id", self.chunk_id)
        require_identifier("reply_id", self.reply_id)
        require_positive("sequence", self.sequence)
        if not isinstance(self.text, str):
            raise TypeError("text 必须是字符串")
        if not isinstance(self.is_final, bool):
            raise TypeError("is_final 必须是 bool")


@dataclass(frozen=True, slots=True)
class SentenceSegment:
    """可交给后续 Provider 调度层、但尚未合成或播放的完整句段。"""

    reply_id: str
    segment_id: str
    segment_index: int
    text: str
    boundary: SegmentBoundary
    source_sequence_start: int
    source_sequence_end: int
    character_count: int

    def __post_init__(self) -> None:
        require_identifier("reply_id", self.reply_id)
        require_identifier("segment_id", self.segment_id)
        require_positive("segment_index", self.segment_index)
        if not isinstance(self.text, str) or not self.text.strip():
            raise SegmentationError("句段 text 不得为空")
        if not isinstance(self.boundary, SegmentBoundary):
            raise SegmentationError("boundary 必须是 SegmentBoundary")
        require_positive("source_sequence_start", self.source_sequence_start)
        require_positive("source_sequence_end", self.source_sequence_end)
        if self.source_sequence_end < self.source_sequence_start:
            raise SegmentationError("source_sequence_end 不得早于 source_sequence_start")
        require_positive("character_count", self.character_count)
        if self.character_count != len(self.text):
            raise SegmentationError("character_count 必须等于 text 字符数")


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    decision: SegmentationDecision
    segments: tuple[SentenceSegment, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.decision is SegmentationDecision.ACCEPTED


@dataclass(frozen=True, slots=True)
class CancellationResult:
    dropped_segment_count: int
    dropped_buffered_characters: int


@dataclass(frozen=True, slots=True)
class _SegmentCandidate:
    text: str
    boundary: SegmentBoundary
    source_sequence_start: int
    source_sequence_end: int


_TERMINAL_PUNCTUATION = frozenset("。！？；.!?;")
_TRAILING_CLOSERS = frozenset('"\'”’）)]】》」』')
_SOFT_BOUNDARIES = frozenset("，,、：: ")


class ReplySegmenter:
    """为一个固定 ``reply_id`` 提供有序、原子且有界的句段缓冲。"""

    def __init__(
        self,
        *,
        reply_id: str,
        config: SegmentationConfig | None = None,
    ) -> None:
        require_identifier("reply_id", reply_id)
        if config is not None and not isinstance(config, SegmentationConfig):
            raise TypeError("config 必须是 SegmentationConfig")
        self._reply_id = reply_id
        self._config = config or SegmentationConfig()
        self._state = ReplySegmentationState.OPEN
        self._last_sequence = 0
        self._seen_chunk_ids: set[str] = set()
        self._buffer_text = ""
        self._buffer_sequences: list[int] = []
        self._queue: list[SentenceSegment] = []
        self._next_segment_index = 1

    @property
    def reply_id(self) -> str:
        return self._reply_id

    @property
    def state(self) -> ReplySegmentationState:
        return self._state

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    @property
    def buffered_text(self) -> str:
        return self._buffer_text

    def queued_segments(self) -> tuple[SentenceSegment, ...]:
        return tuple(self._queue)

    def submit(self, chunk: TextChunk) -> SegmentationResult:
        """原子提交文字块；任何拒绝都不消费序号、ID、缓冲或队列预算。"""

        if not isinstance(chunk, TextChunk):
            raise TypeError("chunk 必须是 TextChunk")
        if self._state is ReplySegmentationState.CANCELLED:
            return SegmentationResult(SegmentationDecision.AFTER_CANCELLED)
        if self._state is ReplySegmentationState.TERMINAL:
            return SegmentationResult(SegmentationDecision.AFTER_TERMINAL)
        if chunk.reply_id != self._reply_id:
            return SegmentationResult(SegmentationDecision.STALE_REPLY)
        if not chunk.text.strip():
            return SegmentationResult(SegmentationDecision.EMPTY_TEXT)
        if chunk.chunk_id in self._seen_chunk_ids:
            return SegmentationResult(SegmentationDecision.DUPLICATE)
        if chunk.sequence != self._last_sequence + 1:
            return SegmentationResult(SegmentationDecision.OUT_OF_ORDER)

        combined_text = self._buffer_text + chunk.text
        combined_sequences = self._buffer_sequences + [chunk.sequence] * len(chunk.text)
        candidates, remaining_text, remaining_sequences = self._extract_candidates(
            combined_text,
            combined_sequences,
            is_final=chunk.is_final,
        )
        if len(self._queue) + len(candidates) > self._config.max_queued_segments:
            return SegmentationResult(SegmentationDecision.QUEUE_BUDGET_EXCEEDED)

        segments = tuple(
            SentenceSegment(
                reply_id=self._reply_id,
                segment_id=self._segment_id(self._next_segment_index + offset),
                segment_index=self._next_segment_index + offset,
                text=candidate.text,
                boundary=candidate.boundary,
                source_sequence_start=candidate.source_sequence_start,
                source_sequence_end=candidate.source_sequence_end,
                character_count=len(candidate.text),
            )
            for offset, candidate in enumerate(candidates)
        )

        self._seen_chunk_ids.add(chunk.chunk_id)
        self._last_sequence = chunk.sequence
        self._buffer_text = remaining_text
        self._buffer_sequences = remaining_sequences
        self._queue.extend(segments)
        self._next_segment_index += len(segments)
        if chunk.is_final:
            self._state = ReplySegmentationState.TERMINAL
        return SegmentationResult(SegmentationDecision.ACCEPTED, segments)

    def finalize(self) -> SegmentationResult:
        """Flush the remaining non-punctuated text without adding characters."""
        if self._state is ReplySegmentationState.CANCELLED:
            return SegmentationResult(SegmentationDecision.AFTER_CANCELLED)
        if self._state is ReplySegmentationState.TERMINAL:
            return SegmentationResult(SegmentationDecision.AFTER_TERMINAL)
        if not self._buffer_text:
            self._state = ReplySegmentationState.TERMINAL
            return SegmentationResult(SegmentationDecision.ACCEPTED)
        candidates, remaining_text, remaining_sequences = self._extract_candidates(
            self._buffer_text, self._buffer_sequences, is_final=True
        )
        if len(self._queue) + len(candidates) > self._config.max_queued_segments:
            return SegmentationResult(SegmentationDecision.QUEUE_BUDGET_EXCEEDED)
        segments = tuple(
            SentenceSegment(
                reply_id=self._reply_id,
                segment_id=self._segment_id(self._next_segment_index + offset),
                segment_index=self._next_segment_index + offset,
                text=candidate.text,
                boundary=candidate.boundary,
                source_sequence_start=candidate.source_sequence_start,
                source_sequence_end=candidate.source_sequence_end,
                character_count=len(candidate.text),
            )
            for offset, candidate in enumerate(candidates)
        )
        self._buffer_text = remaining_text
        self._buffer_sequences = remaining_sequences
        self._queue.extend(segments)
        self._next_segment_index += len(segments)
        self._state = ReplySegmentationState.TERMINAL
        return SegmentationResult(SegmentationDecision.ACCEPTED, segments)

    def dequeue(self, count: int = 1) -> tuple[SentenceSegment, ...]:
        """从头取走未消费句段；这里只改变文本队列，不产生播放事实。"""

        try:
            require_positive("count", count)
        except ContractError as exc:
            raise SegmentationError(str(exc)) from exc
        actual = min(count, len(self._queue))
        items = tuple(self._queue[:actual])
        del self._queue[:actual]
        return items

    def cancel(self) -> CancellationResult:
        """作废本回复剩余文字；取消后的迟到块永不重新打开该 reply。"""

        if self._state is ReplySegmentationState.CANCELLED:
            return CancellationResult(0, 0)
        result = CancellationResult(
            dropped_segment_count=len(self._queue),
            dropped_buffered_characters=len(self._buffer_text),
        )
        self._queue.clear()
        self._buffer_text = ""
        self._buffer_sequences.clear()
        self._state = ReplySegmentationState.CANCELLED
        return result

    def _extract_candidates(
        self,
        text: str,
        sequences: list[int],
        *,
        is_final: bool,
    ) -> tuple[list[_SegmentCandidate], str, list[int]]:
        candidates: list[_SegmentCandidate] = []
        offset = 0
        while offset < len(text):
            remaining = text[offset:]
            hard_cut = self._find_hard_cut(remaining)
            if hard_cut is not None and hard_cut[0] <= self._config.max_segment_characters:
                cut, boundary = hard_cut
            elif self._should_try_soft_length_cut(remaining):
                cut = self._find_length_cut(remaining)
                if cut is None:
                    if len(remaining) < self._config.max_segment_characters:
                        break
                    cut = self._config.max_segment_characters
                boundary = SegmentBoundary.LENGTH
            elif len(remaining) >= self._config.max_segment_characters:
                cut = self._find_length_cut(remaining)
                if cut is None:
                    cut = self._config.max_segment_characters
                boundary = SegmentBoundary.LENGTH
            elif is_final:
                cut = len(remaining)
                boundary = SegmentBoundary.FINAL
            else:
                break

            raw_end = offset + cut
            raw_text = text[offset:raw_end]
            clean_text = raw_text.strip()
            if clean_text:
                raw_sequences = sequences[offset:raw_end]
                significant = [
                    sequence
                    for character, sequence in zip(raw_text, raw_sequences, strict=True)
                    if not character.isspace()
                ]
                relevant_sequences = significant or raw_sequences
                candidates.append(
                    _SegmentCandidate(
                        text=clean_text,
                        boundary=boundary,
                        source_sequence_start=relevant_sequences[0],
                        source_sequence_end=relevant_sequences[-1],
                    )
                )
            offset = raw_end

        return candidates, text[offset:], sequences[offset:]

    def _segment_id(self, segment_index: int) -> str:
        reply_digest = sha256(self._reply_id.encode("utf-8")).hexdigest()[:16]
        return f"segment-{reply_digest}-{segment_index}"

    @staticmethod
    def _find_hard_cut(text: str) -> tuple[int, SegmentBoundary] | None:
        newline_index = min(
            (index for index in (text.find("\r"), text.find("\n")) if index >= 0),
            default=-1,
        )
        punctuation_index = next(
            (index for index, character in enumerate(text) if character in _TERMINAL_PUNCTUATION),
            -1,
        )
        if newline_index < 0 and punctuation_index < 0:
            return None
        if newline_index >= 0 and (punctuation_index < 0 or newline_index < punctuation_index):
            cut = newline_index + 1
            if text[newline_index] == "\r" and cut < len(text) and text[cut] == "\n":
                cut += 1
            return cut, SegmentBoundary.NEWLINE

        cut = punctuation_index + 1
        while cut < len(text) and text[cut] in _TERMINAL_PUNCTUATION:
            cut += 1
        while cut < len(text) and text[cut] in _TRAILING_CLOSERS:
            cut += 1
        return cut, SegmentBoundary.PUNCTUATION

    def _should_try_soft_length_cut(self, text: str) -> bool:
        target = self._config.soft_target_characters
        return target is not None and len(text) >= target

    def _find_length_cut(self, text: str) -> int | None:
        maximum = self._config.max_segment_characters
        minimum = self._config.min_segment_characters
        search_min = self._config.soft_search_min_characters or minimum
        search_max = self._config.soft_search_max_characters or maximum
        upper = min(len(text), search_max)
        for index in range(upper - 1, search_min - 2, -1):
            if text[index] in _SOFT_BOUNDARIES:
                return index + 1
        if self._config.soft_target_characters is None:
            return maximum
        return None


__all__ = [
    "CancellationResult",
    "ReplySegmentationState",
    "ReplySegmenter",
    "SegmentBoundary",
    "SegmentationConfig",
    "SegmentationDecision",
    "SegmentationError",
    "SegmentationResult",
    "SentenceSegment",
    "TextChunk",
]
