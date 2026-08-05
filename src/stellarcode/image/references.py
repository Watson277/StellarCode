from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from stellarcode.image.clipboard import grab_clipboard_image
from stellarcode.image.processor import ImageInputError, ImageProcessor


_IMAGE_REFERENCE = re.compile(
    r'@image:(?:<([^>]+)>|"([^"]+)"|\'([^\']+)\'|'
    r'([^\s<>\u2010-\u206f\u3000-\u303f\uff00-\uffef]+))'
    r"|@clipboard(?![\w])",
    flags=re.IGNORECASE,
)


class ImageReferenceParser:
    def __init__(
        self,
        base_dir: str | Path,
        clipboard_reader: Callable[[], Path] = grab_clipboard_image,
    ) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.clipboard_reader = clipboard_reader

    def user_message(self, value: str | None) -> dict[str, Any]:
        raw = value or ""
        matches = list(_IMAGE_REFERENCE.finditer(raw))
        if not matches:
            return {"role": "user", "content": raw}

        text = _IMAGE_REFERENCE.sub("", raw).strip() or "请分析以下图片。"
        notes: list[str] = []
        image_parts: list[dict[str, Any]] = []
        for match in matches:
            reference = next((group for group in match.groups() if group is not None), None)
            label = reference or "剪贴板"
            try:
                path = self.clipboard_reader() if reference is None else self.resolve(reference)
                if not path.exists():
                    raise ImageInputError("文件不存在")
                if not path.is_file():
                    raise ImageInputError("不是普通文件")
                processed = ImageProcessor.from_path(path)
                image_parts.append(processed.content_part())
                metadata = processed.metadata_text()
                if metadata:
                    notes.append(metadata)
            except Exception as exc:
                message = str(exc) or type(exc).__name__
                notes.append(f"[图片引用无效: {label}，原因: {message}]")

        if not image_parts:
            note_text = "\n".join(notes)
            suffix = f"\n\n{note_text}" if note_text else ""
            return {"role": "user", "content": f"{text}{suffix}"}

        attachment_note = (
            "[图片已作为视觉附件附加。请直接观察本轮图片，不要调用文件或浏览器工具"
            "重新读取 Image source；如果模型无法看图，请明确说明，不要根据路径猜测。]"
        )
        text_content = "\n\n".join(part for part in (text, attachment_note, *notes) if part)
        return {
            "role": "user",
            "content": [{"type": "text", "text": text_content}, *image_parts],
        }

    def resolve(self, reference: str) -> Path:
        value = reference.strip()
        if value.lower().startswith("file://"):
            parsed = urlparse(value)
            value = unquote(parsed.path)
            if parsed.netloc:
                value = f"//{parsed.netloc}{value}"
            if re.match(r"^/[A-Za-z]:/", value):
                value = value[1:]
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        return path.resolve()


def image_tool_message(
    tool_name: str,
    image_parts: tuple[dict[str, Any], ...] | list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not image_parts:
        return None
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    f"工具 {tool_name} 返回了图片内容，请结合上面的工具文字结果"
                    "直接观察并分析该图片。"
                ),
            },
            *image_parts,
        ],
    }


def prune_historical_images(messages: list[dict[str, Any]]) -> None:
    """Drop image payloads from completed user turns while retaining audit text."""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text_parts = [part for part in content if _is_text_part(part)]
        omitted = sum(1 for part in content if _is_image_part(part))
        if omitted == 0:
            continue
        text_parts.append(
            {
                "type": "text",
                "text": f"[历史图片附件已省略 {omitted} 张；需要时请重新附加。]",
            }
        )
        message["content"] = text_parts


def strip_images_for_text_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for message in messages:
        copy = dict(message)
        content = copy.get("content")
        if not isinstance(content, list):
            sanitized.append(copy)
            continue
        text_parts = [part for part in content if _is_text_part(part)]
        omitted = sum(1 for part in content if _is_image_part(part))
        if omitted:
            text_parts.append(
                {
                    "type": "text",
                    "text": (
                        f"[当前 provider/model 不支持图片附件，已省略 {omitted} 张；"
                        "请改用支持视觉输入的模型。]"
                    ),
                }
            )
        copy["content"] = text_parts or ""
        sanitized.append(copy)
    return sanitized


def _is_text_part(part: object) -> bool:
    return isinstance(part, dict) and part.get("type") == "text"


def _is_image_part(part: object) -> bool:
    return isinstance(part, dict) and part.get("type") == "image_url"
