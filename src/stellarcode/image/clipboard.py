"""Best-effort clipboard image capture used by local interactive clients."""

from __future__ import annotations

import time
from pathlib import Path

from stellarcode.image.processor import ImageInputError


def grab_clipboard_image(cache_dir: str | Path | None = None) -> Path:
    """Copy the current GUI clipboard image to StellarCode's local cache."""
    try:
        from PIL import Image, ImageGrab
    except ModuleNotFoundError as exc:
        raise RuntimeError("Missing dependency: install Pillow with `pip install -e .`.") from exc

    try:
        value = ImageGrab.grabclipboard()
    except Exception as exc:
        raise ImageInputError(f"读取系统剪贴板失败: {exc}") from exc

    image = value if isinstance(value, Image.Image) else None
    if image is None and isinstance(value, list):
        for candidate in value:
            path = Path(candidate)
            if not path.is_file():
                continue
            try:
                with Image.open(path) as opened:
                    opened.load()
                    image = opened.copy()
                break
            except OSError:
                continue
    if image is None:
        raise ImageInputError("剪贴板里没有图片，请先截图或复制一张图片")

    target_dir = Path(cache_dir) if cache_dir else Path.home() / ".stellarcode" / "cache"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"clip-{time.time_ns()}.png"
        image.save(target, format="PNG")
    except OSError as exc:
        raise ImageInputError(f"无法缓存剪贴板图片: {exc}") from exc
    return target.resolve()
