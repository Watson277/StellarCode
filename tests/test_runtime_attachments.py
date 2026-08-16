import base64
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from stellarcode.runtime.attachments import prepare_attachments


def _attachment(path: Path, *, kind: str = "file", mime_type: str = "text/plain") -> dict:
    return {
        "id": f"attachment-{path.name}",
        "kind": kind,
        "mime_type": mime_type,
        "display_name": path.name,
        "local_path": str(path),
    }


def test_text_attachment_is_inlined_and_metadata_is_normalized(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nhello", encoding="utf-8")

    prepared = prepare_attachments("Summarize this", [_attachment(source)])

    assert prepared.agent_prompt.startswith("Summarize this")
    assert "BEGIN ATTACHMENT" in prepared.agent_prompt
    assert "# Notes" in prepared.agent_prompt
    assert prepared.metadata[0]["local_path"] == str(source.resolve())
    assert prepared.metadata[0]["size_bytes"] == source.stat().st_size


def test_image_attachment_uses_existing_image_reference_pipeline(tmp_path):
    image = tmp_path / "diagram.png"
    image.write_bytes(b"not-decoded-until-agent-input")

    prepared = prepare_attachments(
        "Explain the diagram",
        [_attachment(image, kind="image", mime_type="image/png")],
    )

    assert f'@image:"{image.resolve()}"' in prepared.agent_prompt
    assert prepared.metadata[0]["kind"] == "image"


def test_attachment_only_submission_receives_a_default_prompt(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text("print('ok')", encoding="utf-8")

    prepared = prepare_attachments("", [_attachment(source)])

    assert prepared.agent_prompt.startswith("请分析已附加的文件。")


def test_missing_or_excessive_attachments_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="not a readable file"):
        prepare_attachments("inspect", [_attachment(tmp_path / "missing.txt")])

    source = tmp_path / "one.txt"
    source.write_text("one", encoding="utf-8")
    with pytest.raises(ValueError, match="at most 10"):
        prepare_attachments("inspect", [_attachment(source)] * 11)


def test_pasted_base64_image_is_validated_cached_and_not_persisted(tmp_path):
    output = BytesIO()
    Image.new("RGB", (3, 2), "blue").save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    attachment = {
        "id": "attachment-paste",
        "kind": "image",
        "mime_type": "image/png",
        "display_name": "pasted-image.png",
        "data_base64": encoded,
    }

    prepared = prepare_attachments("describe", [attachment], cache_dir=tmp_path)

    cached = Path(prepared.metadata[0]["local_path"])
    assert cached.exists()
    assert cached.parent == tmp_path
    assert "data_base64" not in prepared.metadata[0]
    assert f'@image:"{cached}"' in prepared.agent_prompt


def test_invalid_pasted_image_is_rejected(tmp_path):
    attachment = {
        "kind": "image",
        "mime_type": "image/png",
        "display_name": "broken.png",
        "data_base64": "not-base64",
    }

    with pytest.raises(Exception):
        prepare_attachments("describe", [attachment], cache_dir=tmp_path)
