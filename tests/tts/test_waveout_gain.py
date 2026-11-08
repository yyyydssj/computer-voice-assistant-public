"""P10 R4：播放增益钩子（``WaveOutPCMStreamBackend(gain_provider=...)``）的离线契约测试。

只用假 WinMM 驱动真实后端；默认 ``gain_provider=None`` 必须**字节级不变**。
"""

from __future__ import annotations

import array
import ctypes
import unittest

from app.tts.waveout_pcm import PlaybackGain, WaveOutPCMStreamBackend, _scale_pcm16

FRAME = array.array("h", [1000, -1000, 32767, -32768, 0, 250]).tobytes()


def build_backend(*, gain_provider=None, submit_observer=None, write_result: int = 0):
    """按需装配一个用假 WinMM 驱动的真实后端；返回 ``(backend, 提交字节, observer 看到的字节)``。"""

    submitted: list[bytes] = []
    observed: list[bytes] = []

    class FakeWinMM:
        def waveOutOpen(self, handle, device, fmt, callback, instance, flags):
            return 0

        def waveOutPrepareHeader(self, handle, header, size):
            header._obj.dwFlags |= 0x00000002
            return 0

        def waveOutWrite(self, handle, header, size):
            header_obj = header._obj
            if header_obj.dwBufferLength:
                buffer = ctypes.cast(header_obj.lpData, ctypes.POINTER(ctypes.c_char))
                submitted.append(ctypes.string_at(buffer, header_obj.dwBufferLength))
            else:
                submitted.append(b"")
            return write_result

        def waveOutReset(self, handle):
            return 0

        def waveOutUnprepareHeader(self, handle, header, size):
            return 0

        def waveOutClose(self, handle):
            return 0

    observer = None
    if submit_observer is not None:

        def observer(pcm: bytes, at: float) -> None:
            observed.append(pcm)
            submit_observer(pcm, at)

    backend = WaveOutPCMStreamBackend(
        winmm=FakeWinMM(), gain_provider=gain_provider, submit_observer=observer
    )
    backend.open()
    return backend, submitted, observed


class PlaybackGainTests(unittest.TestCase):
    def test_default_value_is_full_volume(self) -> None:
        gain = PlaybackGain()
        self.assertEqual(gain.value, 1.0)
        self.assertEqual(gain.read(), 1.0)

    def test_set_clamps_to_unit_range(self) -> None:
        gain = PlaybackGain()
        self.assertEqual(gain.set(-1.0), 0.0)
        self.assertEqual(gain.set(2.0), 1.0)
        self.assertEqual(gain.set(float("nan")), 1.0)
        self.assertEqual(gain.set("loud"), 1.0)  # type: ignore[arg-type]

    def test_read_and_set_round_trip(self) -> None:
        gain = PlaybackGain()
        gain.set(0.25)
        self.assertEqual(gain.read(), 0.25)
        self.assertEqual(gain.value, 0.25)


class ScalePcmTests(unittest.TestCase):
    def test_full_gain_is_byte_identical(self) -> None:
        self.assertIs(_scale_pcm16(FRAME, 1.0), FRAME)
        self.assertIs(_scale_pcm16(FRAME, 2.0), FRAME)

    def test_half_gain_halves_the_samples(self) -> None:
        scaled = array.array("h")
        scaled.frombytes(_scale_pcm16(FRAME, 0.5))
        self.assertEqual(list(scaled), [500, -500, 16383, -16384, 0, 125])

    def test_zero_gain_is_silence_of_the_same_length(self) -> None:
        data = _scale_pcm16(FRAME, 0.0)
        self.assertEqual(len(data), len(FRAME))
        self.assertEqual(data, bytes(len(FRAME)))


class BackendGainTests(unittest.TestCase):
    def test_no_gain_provider_keeps_the_original_bytes(self) -> None:
        backend, submitted, _ = build_backend()
        backend.submit(FRAME)
        backend.close()
        self.assertEqual(submitted, [FRAME])
        self.assertEqual(backend.diagnostics.scaled_submissions, 0)
        self.assertEqual(backend.diagnostics.min_gain_applied, 1.0)

    def test_gain_provider_scales_the_submitted_audio(self) -> None:
        level = {"value": 1.0}
        backend, submitted, _ = build_backend(gain_provider=lambda: level["value"])
        backend.submit(FRAME)
        level["value"] = 0.5
        backend.submit(FRAME)
        backend.close()
        self.assertEqual(submitted[0], FRAME)
        scaled = array.array("h")
        scaled.frombytes(submitted[1])
        self.assertEqual(scaled[0], 500)
        self.assertEqual(backend.diagnostics.scaled_submissions, 1)
        self.assertEqual(backend.diagnostics.min_gain_applied, 0.5)

    def test_gain_provider_exception_falls_back_to_full_volume(self) -> None:
        def boom() -> float:
            raise RuntimeError("gain exploded")

        backend, submitted, _ = build_backend(gain_provider=boom)
        backend.submit(FRAME)
        backend.close()
        self.assertEqual(submitted, [FRAME])

    def test_provider_returning_nonsense_falls_back_to_full_volume(self) -> None:
        for value in ("loud", None, float("inf"), True):
            with self.subTest(value=value):
                backend, submitted, _ = build_backend(gain_provider=lambda v=value: v)
                backend.submit(FRAME)
                backend.close()
                self.assertEqual(submitted, [FRAME])

    def test_exception_from_write_does_not_notify_observer_or_apply_counters(self) -> None:
        seen: list[bytes] = []
        backend, submitted, observed = build_backend(
            gain_provider=lambda: 0.5,
            submit_observer=lambda pcm, at: seen.append(pcm),
            write_result=5,
        )
        with self.assertRaises(OSError):
            backend.submit(FRAME)
        # observer 与事实计数沿用既有语义：失败时不通知 observer（没有事实），
        # 但"这次提交尝试被缩放过"仍计数（与 submitted_count 同为尝试计数）。
        self.assertEqual(observed, [])
        self.assertEqual(seen, [])
        self.assertEqual(backend.diagnostics.scaled_submissions, 1)

    def test_submit_observer_sees_the_actual_device_signal(self) -> None:
        backend, submitted, observed = build_backend(
            gain_provider=lambda: 0.5,
            submit_observer=lambda pcm, at: None,
        )
        backend.submit(FRAME)
        backend.close()
        self.assertEqual(observed, submitted)
        self.assertNotEqual(observed[0], FRAME)

    def test_non_callable_gain_provider_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            WaveOutPCMStreamBackend(winmm=object(), gain_provider="loud")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
