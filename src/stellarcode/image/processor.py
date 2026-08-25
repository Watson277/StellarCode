"""Normalize image bytes into bounded provider-safe content parts for multimodal chat."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any


API_IMAGE_MAX_BASE64_SIZE = 5 * 1024 * 1024
IMAGE_TARGET_RAW_SIZE = API_IMAGE_MAX_BASE64_SIZE * 3 // 4
MAX_SOURCE_IMAGE_BYTES = 50 * 1024 * 1024
IMAGE_MAX_WIDTH = 2000
IMAGE_MAX_HEIGHT = 2000
JPEG_QUALITIES = (85, 70, 55, 40, 25)


class ImageInputError(ValueError):
    """Raised when an image cannot be converted into a model attachment."""


@dataclass(frozen=True)
class ImageDimensions:
    original_width: int
    original_height: int
    display_width: int
    display_height: int


@dataclass(frozen=True)
class ProcessedImage:
    base64_data: str
    mime_type: str
    original_bytes: int
    output_bytes: int
    dimensions: ImageDimensions
    source_path: Path | None = None
    reencoded: bool = False

    def content_part(self, *, raw_base64: bool = False) -> dict[str, Any]:
        url = self.base64_data
        if not raw_base64:
            url = f"data:{self.mime_type};base64,{self.base64_data}"
        return {"type": "image_url", "image_url": {"url": url}}

    def metadata_text(self) -> str | None:
        dims = self.dimensions
        resized = (
            dims.original_width != dims.display_width
            or dims.original_height != dims.display_height
        )
        if self.source_path is None and not resized and not self.reencoded:
            return None
        details: list[str] = []
        if self.source_path is not None:
            details.append(f"source: {self.source_path}")
        if resized:
            scale = dims.original_width / max(1, dims.display_width)
            details.append(
                f"original {dims.original_width}x{dims.original_height}, displayed at "
                f"{dims.display_width}x{dims.display_height}; multiply coordinates by "
                f"{scale:.2f} to map to the original"
            )
        elif self.reencoded:
            details.append("re-encoded for the API size limit without changing dimensions")
        return f"[Image: {', '.join(details)}]"


class ImageProcessor:
    @classmethod
    def from_path(cls, path: str | Path) -> ProcessedImage:
        source = Path(path).resolve()
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise ImageInputError(f"无法读取图片: {exc}") from exc
        if size == 0:
            raise ImageInputError("图片文件为空")
        if size > MAX_SOURCE_IMAGE_BYTES:
            raise ImageInputError("图片超过 50MB 处理上限")
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise ImageInputError(f"无法读取图片: {exc}") from exc
        return cls.process(data, source_path=source)

    @classmethod
    def from_base64(
        cls,
        value: str,
        mime_type: str = "image/png",
    ) -> ProcessedImage:
        if not value or not value.strip():
            raise ImageInputError("图片数据为空")
        if len(value) > _estimated_base64_size(MAX_SOURCE_IMAGE_BYTES):
            raise ImageInputError("图片超过 50MB 处理上限")
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ImageInputError("图片 Base64 数据无效") from exc
        return cls.process(data, hinted_mime_type=mime_type)

    @classmethod
    def process(
        cls,
        data: bytes,
        *,
        source_path: Path | None = None,
        hinted_mime_type: str | None = None,
    ) -> ProcessedImage:
        if not data:
            raise ImageInputError("图片数据为空")
        if len(data) > MAX_SOURCE_IMAGE_BYTES:
            raise ImageInputError("图片超过 50MB 处理上限")

        try:
            from PIL import Image, UnidentifiedImageError
        except ModuleNotFoundError as exc:
            raise RuntimeError("Missing dependency: install Pillow with `pip install -e .`.") from exc

        try:
            with Image.open(BytesIO(data)) as opened:
                opened.load()
                image = opened.copy()
                image_format = (opened.format or "").upper()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageInputError("文件不是可解码的受支持图片") from exc

        mime_type = _mime_type(image_format, hinted_mime_type)
        original_width, original_height = image.size
        dimensions = ImageDimensions(
            original_width,
            original_height,
            original_width,
            original_height,
        )
        over_size = _estimated_base64_size(len(data)) > API_IMAGE_MAX_BASE64_SIZE
        has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info
        provider_native_format = image_format in {"JPEG", "PNG", "GIF", "WEBP"}

        if not over_size and not has_alpha and provider_native_format:
            return ProcessedImage(
                base64.b64encode(data).decode("ascii"),
                mime_type,
                len(data),
                len(data),
                dimensions,
                source_path,
                False,
            )

        if has_alpha or not provider_native_format:
            image = _flatten_alpha(image) if has_alpha else image.convert("RGB")
            flattened = _encode_png(image)
            if not over_size and _estimated_base64_size(len(flattened)) <= API_IMAGE_MAX_BASE64_SIZE:
                return ProcessedImage(
                    base64.b64encode(flattened).decode("ascii"),
                    "image/png",
                    len(data),
                    len(flattened),
                    dimensions,
                    source_path,
                    True,
                )

        target = _fit_within(image.size, (IMAGE_MAX_WIDTH, IMAGE_MAX_HEIGHT))
        image = _resize(image, target)
        resized_dimensions = ImageDimensions(
            original_width,
            original_height,
            target[0],
            target[1],
        )
        png = _encode_png(image)
        if _estimated_base64_size(len(png)) <= API_IMAGE_MAX_BASE64_SIZE:
            return ProcessedImage(
                base64.b64encode(png).decode("ascii"),
                "image/png",
                len(data),
                len(png),
                resized_dimensions,
                source_path,
                True,
            )

        encoded = _encode_jpeg_under_limit(image)
        if encoded is None and (target[0] > 512 or target[1] > 512):
            target = _fit_within((original_width, original_height), (1200, 1200))
            image = _resize(image, target)
            encoded = _encode_jpeg_under_limit(image)
            resized_dimensions = ImageDimensions(
                original_width,
                original_height,
                target[0],
                target[1],
            )
        if encoded is None:
            raise ImageInputError("图片压缩后仍超过 5MB API 上限")
        return ProcessedImage(
            base64.b64encode(encoded).decode("ascii"),
            "image/jpeg",
            len(data),
            len(encoded),
            resized_dimensions,
            source_path,
            True,
        )


def _estimated_base64_size(raw_bytes: int) -> int:
    return ((raw_bytes + 2) // 3) * 4


def _mime_type(image_format: str, hinted: str | None) -> str:
    formats = {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "GIF": "image/gif",
        "WEBP": "image/webp",
        "BMP": "image/bmp",
        "TIFF": "image/tiff",
    }
    return formats.get(image_format, (hinted or "image/png").lower())


def _flatten_alpha(image: Any) -> Any:
    from PIL import Image

    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _fit_within(size: tuple[int, int], bounds: tuple[int, int]) -> tuple[int, int]:
    width, height = size
    scale = min(1.0, bounds[0] / width, bounds[1] / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _resize(image: Any, target: tuple[int, int]) -> Any:
    from PIL import Image

    rgb = image.convert("RGB")
    if rgb.size == target:
        return rgb
    return rgb.resize(target, Image.Resampling.LANCZOS)


def _encode_png(image: Any) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _encode_jpeg_under_limit(image: Any) -> bytes | None:
    rgb = image.convert("RGB")
    for quality in JPEG_QUALITIES:
        output = BytesIO()
        rgb.save(output, format="JPEG", quality=quality, optimize=True)
        candidate = output.getvalue()
        if _estimated_base64_size(len(candidate)) <= API_IMAGE_MAX_BASE64_SIZE:
            return candidate
    return None
