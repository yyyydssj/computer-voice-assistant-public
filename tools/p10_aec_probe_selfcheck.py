"""P10 R2b-2 工具自检：用合成数据模拟整条真机链路，验证对齐与消除指标。

链路模型：24 kHz 参考 → 重采样 48 kHz → 播放时钟漂移 → 声学延迟+增益 → 麦克风噪声底；
回环 = 播放信号 + 极小底噪（回环是数字回灌，几乎无噪声）。

只有当本自检在"带漂移"情形下仍然给出高对齐分与合理 ERLE，真机结果才有解释力。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.audio.aec.nlms import AecProcessingChain, NlmsConfig  # noqa: E402
from app.audio.aec.reference import AecReferenceConfig  # noqa: E402

spec = importlib.util.spec_from_file_location("probe", REPO_ROOT / "tools" / "p10_aec_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

RATE = 48_000
REF_RATE = 24_000


def stretch(signal: np.ndarray, factor: float) -> np.ndarray:
    count = int(round(signal.size * factor))
    return np.interp(np.linspace(0, signal.size - 1, count), np.arange(signal.size), signal).astype(
        np.float32
    )


def build_case(
    *,
    loop_lead_ms: float,
    mic_lead_ms: float,
    acoustic_ms: float,
    gain: float,
    drift: float,
    mic_noise: float,
    loop_noise: float,
    seed: int,
):
    reference = probe.build_reference_signal(seconds=6.0, rate=REF_RATE)
    played = probe._resample(reference, src_rate=REF_RATE, dst_rate=RATE)
    if drift != 1.0:
        played = stretch(played, drift)
    rng = np.random.default_rng(seed)
    tail = int(0.6 * RATE)
    loop = np.concatenate(
        (
            (rng.standard_normal(int(loop_lead_ms * RATE / 1000)) * loop_noise).astype(np.float32),
            (played * 0.5).astype(np.float32),
            (rng.standard_normal(tail) * loop_noise).astype(np.float32),
        )
    )
    delay = int(acoustic_ms * RATE / 1000)
    echo = np.concatenate((np.zeros(delay, dtype=np.float32), (played * gain).astype(np.float32)))[
        : played.size
    ]
    mic = np.concatenate(
        (
            (rng.standard_normal(int(mic_lead_ms * RATE / 1000)) * mic_noise).astype(np.float32),
            echo,
            (rng.standard_normal(tail) * mic_noise).astype(np.float32),
        )
    )
    return {
        "reference": probe._resample(reference, src_rate=REF_RATE, dst_rate=RATE),
        "loopback": loop,
        "mic": mic,
    }


def run_case(label: str, **kwargs) -> dict:
    case = build_case(**kwargs)
    chain = AecProcessingChain(
        reference_config=AecReferenceConfig(max_delay_ms=200),
        nlms_config=NlmsConfig(filter_length_ms=160, step_size=0.5, erle_gate_db=1.0),
    )
    result = probe.analyze(
        reference=case["reference"],
        loopback=case["loopback"],
        mic=case["mic"],
        rate=RATE,
        chain=chain,
    )
    keys = (
        "status",
        "reason",
        "loopback_alignment",
        "mic_alignment",
        "acoustic_delay_ms",
        "acoustic_delay_score",
        "erl_db",
        "enr_db",
        "max_erle_db",
        "erle_tail_db",
        "applied_ratio",
    )
    print(f"[{label}] " + json.dumps({key: result.get(key) for key in keys}, ensure_ascii=False))
    return result


def main() -> int:
    base = dict(
        loop_lead_ms=1000,
        mic_lead_ms=1200,
        acoustic_ms=25,
        gain=0.25,
        mic_noise=0.003,
        loop_noise=0.0005,
        seed=101,
    )
    run_case("理想(无漂移)", drift=1.0, **base)
    run_case("漂移1.0005", drift=1.0005, **base)
    run_case("漂移1.002", drift=1.002, **base)
    run_case("白噪声底更大", drift=1.0005, **{**base, "mic_noise": 0.02})
    run_case("回声更弱", drift=1.0005, **{**base, "gain": 0.05})
    run_case("回环噪声更大", drift=1.0005, **{**base, "loop_noise": 0.05})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
