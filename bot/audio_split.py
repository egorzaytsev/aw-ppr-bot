"""Длинные записи — кусками по 10 минут для Groq (лимит на файл ~25 МБ).

ffmpeg берётся из пакета imageio-ffmpeg (статическая сборка), чтобы не зависеть от образа Railway.
Куски — моно mp3 16 кГц 32 кбит/с: 10 минут ≈ 2,4 МБ, распознаванию хватает.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

CHUNK_SECONDS = 600


def _ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


async def split_audio(content: bytes, suffix: str) -> list[tuple[str, bytes]]:
    """[(имя куска, байты)] по порядку; ошибка ffmpeg — исключение с его выводом."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in{suffix or '.m4a'}"
        src.write_bytes(content)
        pattern = str(Path(tmp) / "part%03d.mp3")
        proc = await asyncio.create_subprocess_exec(
            _ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "32k", "-f", "segment", "-segment_time", str(CHUNK_SECONDS), pattern,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg: {err.decode(errors='replace').strip()[:300]}")
        return [(p.name, p.read_bytes()) for p in sorted(Path(tmp).glob("part*.mp3"))]
