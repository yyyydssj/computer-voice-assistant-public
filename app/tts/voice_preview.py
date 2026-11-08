"""Explicit opt-in text preview, not an Agent reply exit or automatic startup."""
from __future__ import annotations

import argparse

from app.tts.private_selection import read_selection, private_data_root
from app.tts.real_playback import RealVoicePreview


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit VoiceStudio preview on Windows default output")
    parser.add_argument("--text", required=True, help="Short non-sensitive text (maximum three sentences)")
    args = parser.parse_args(argv)
    try:
        selection = read_selection(private_data_root())
    except (ValueError, OSError):
        print("音色配置不可用；未发起语音播放。")
        print("原文：" + args.text)
        return 2
    result = RealVoicePreview(selection).speak(args.text)
    print(f"已收到音频句数: {result.received_audio}; 播放调用完成句数: {result.playback_calls_returned}; "
          f"用户是否听到: 待确认; 文字回退: {result.text_fallback}; "
          f"故障: {result.error_code or '无'}")
    if result.text_fallback:
        print("原文：" + result.text)
    return 1 if result.text_fallback else 0


if __name__ == "__main__":
    raise SystemExit(main())


