"""Attachment preparation, clipboard input, and vision-model routing helpers."""

from stellarcode.image.clipboard import grab_clipboard_image
from stellarcode.image.processor import (
    API_IMAGE_MAX_BASE64_SIZE,
    IMAGE_MAX_HEIGHT,
    IMAGE_MAX_WIDTH,
    MAX_SOURCE_IMAGE_BYTES,
    ImageDimensions,
    ImageInputError,
    ImageProcessor,
    ProcessedImage,
)
from stellarcode.image.references import (
    ImageReferenceParser,
    image_tool_message,
    prune_historical_images,
    strip_images_for_text_model,
)

__all__ = [
    "API_IMAGE_MAX_BASE64_SIZE",
    "IMAGE_MAX_HEIGHT",
    "IMAGE_MAX_WIDTH",
    "MAX_SOURCE_IMAGE_BYTES",
    "ImageDimensions",
    "ImageInputError",
    "ImageProcessor",
    "ImageReferenceParser",
    "ProcessedImage",
    "grab_clipboard_image",
    "image_tool_message",
    "prune_historical_images",
    "strip_images_for_text_model",
]
