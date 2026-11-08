from __future__ import annotations

import unittest

from app.agent.adapter import AgentTurnOutcome
from app.asr.push_to_talk import drive_turn_stream


def outcome(**overrides: object) -> AgentTurnOutcome:
    values: dict[str, object] = {
        "request_id": "req-1",
        "session_id": "sess-1",
        "completed": True,
        "text": "第一句。",
        "finish_reason": "stop",
    }
    values.update(overrides)
    return AgentTurnOutcome(**values)  # type: ignore[arg-type]


class FakeStream:
    def __init__(self, *, finish_error: Exception | None = None) -> None:
        self.chunks: list[str] = []
        self.finished = 0
        self.cancelled = 0
        self._finish_error = finish_error

    def push_text_chunk(self, text: str) -> None:
        self.chunks.append(text)

    def finish(self) -> None:
        if self._finish_error is not None:
            raise self._finish_error
        self.finished += 1

    def cancel(self) -> None:
        self.cancelled += 1


class MinimalStream:
    """A stream without ``push_text_chunk``; finish/cancel only."""

    def __init__(self) -> None:
        self.finished = 0
        self.cancelled = 0

    def finish(self) -> None:
        self.finished += 1

    def cancel(self) -> None:
        self.cancelled += 1


class FakeSpeaker:
    def __init__(self, stream: object | None = None, error: Exception | None = None) -> None:
        self._stream = stream
        self._error = error
        self.starts = 0

    def start_stream(self):
        self.starts += 1
        if self._error is not None:
            raise self._error
        return self._stream


class DriveTurnStreamTests(unittest.TestCase):
    def test_complete_outcome_finishes_the_stream(self) -> None:
        stream = FakeStream()
        received: list[object] = []
        result = drive_turn_stream(
            FakeSpeaker(stream),
            lambda on_chunk: received.append(on_chunk) or outcome(),
        )
        self.assertTrue(result.completed)
        self.assertEqual(stream.finished, 1)
        self.assertEqual(stream.cancelled, 0)
        self.assertEqual(received[0], stream.push_text_chunk)

    def test_incomplete_outcome_cancels_the_stream(self) -> None:
        stream = FakeStream()
        result = drive_turn_stream(
            FakeSpeaker(stream),
            lambda on_chunk: outcome(completed=False, text=None, finish_reason=None),
        )
        self.assertFalse(result.completed)
        self.assertEqual(stream.finished, 0)
        self.assertEqual(stream.cancelled, 1)

    def test_agent_failure_never_reaches_speech(self) -> None:
        stream = FakeStream()
        drive_turn_stream(FakeSpeaker(stream), lambda on_chunk: outcome(completed=False))
        self.assertEqual(stream.cancelled, 1)
        self.assertEqual(stream.finished, 0)

    def test_missing_speaker_submits_without_a_stream(self) -> None:
        received: list[object] = []
        result = drive_turn_stream(None, lambda on_chunk: received.append(on_chunk) or outcome())
        self.assertEqual(received, [None])
        self.assertTrue(result.completed)

    def test_start_stream_failure_never_blocks_the_turn(self) -> None:
        received: list[object] = []
        speaker = FakeSpeaker(error=RuntimeError("device unavailable"))
        result = drive_turn_stream(speaker, lambda on_chunk: received.append(on_chunk) or outcome())
        self.assertEqual(speaker.starts, 1)
        self.assertEqual(received, [None])
        self.assertTrue(result.completed)

    def test_submit_failure_cancels_the_stream_and_propagates(self) -> None:
        stream = FakeStream()

        def submit(on_chunk: object):
            raise RuntimeError("conversation failed")

        with self.assertRaises(RuntimeError):
            drive_turn_stream(FakeSpeaker(stream), submit)
        self.assertEqual(stream.cancelled, 1)
        self.assertEqual(stream.finished, 0)

    def test_stream_without_push_text_chunk_submits_none(self) -> None:
        stream = MinimalStream()
        received: list[object] = []
        drive_turn_stream(FakeSpeaker(stream), lambda on_chunk: received.append(on_chunk) or outcome())
        self.assertEqual(received, [None])
        self.assertEqual(stream.finished, 1)

    def test_finish_failure_cancels_the_stream_and_keeps_the_outcome(self) -> None:
        stream = FakeStream(finish_error=RuntimeError("provider failed"))
        result = drive_turn_stream(FakeSpeaker(stream), lambda on_chunk: outcome())
        self.assertTrue(result.completed)
        self.assertEqual(result.text, "第一句。")
        self.assertEqual(stream.cancelled, 1)

    def test_speaker_without_start_stream_is_tolerated(self) -> None:
        received: list[object] = []
        drive_turn_stream(object(), lambda on_chunk: received.append(on_chunk) or outcome())
        self.assertEqual(received, [None])


if __name__ == "__main__":
    unittest.main()
