from __future__ import annotations

import base64
import json
from io import BytesIO
from typing import Any

import httpx
import pytest
from PIL import Image

from stellarcode.agent import Agent
from stellarcode.image import ImageProcessor, ImageReferenceParser
from stellarcode.llm import GLMApiError, GLMClient
from stellarcode.tools import ToolDefinition, ToolOutput, ToolRegistry


def _write_image(path, mode: str = "RGB") -> None:
    color = (20, 120, 220, 100) if mode == "RGBA" else (20, 120, 220)
    Image.new(mode, (24, 16), color).save(path)


def _image_part() -> dict[str, Any]:
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    data = base64.b64encode(output.getvalue()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{data}"},
    }


def test_image_reference_parser_supports_spaces_multiple_images_and_clipboard(tmp_path):
    first = tmp_path / "first image.png"
    second = tmp_path / "second.png"
    clipboard = tmp_path / "clipboard.png"
    for path in (first, second, clipboard):
        _write_image(path)
    parser = ImageReferenceParser(tmp_path, clipboard_reader=lambda: clipboard)

    message = parser.user_message(
        "比较 @image:<first image.png>、@image:second.png 和 @clipboard"
    )

    assert message["role"] == "user"
    assert isinstance(message["content"], list)
    assert message["content"][0]["type"] == "text"
    assert "比较" in message["content"][0]["text"]
    image_parts = [part for part in message["content"] if part["type"] == "image_url"]
    assert len(image_parts) == 3
    assert all(
        part["image_url"]["url"].startswith("data:image/png;base64,")
        for part in image_parts
    )


def test_invalid_image_reference_becomes_visible_text_note(tmp_path):
    message = ImageReferenceParser(tmp_path).user_message("分析 @image:missing.png")

    assert isinstance(message["content"], str)
    assert "图片引用无效" in message["content"]
    assert "文件不存在" in message["content"]


def test_image_processor_flattens_alpha_without_changing_dimensions(tmp_path):
    source = tmp_path / "alpha.png"
    _write_image(source, "RGBA")

    processed = ImageProcessor.from_path(source)
    decoded = base64.b64decode(processed.base64_data)
    with Image.open(BytesIO(decoded)) as result:
        assert result.mode == "RGB"
        assert result.size == (24, 16)
    assert processed.reencoded is True
    assert processed.mime_type == "image/png"


def test_glm_automatically_routes_image_messages_to_vision_model():
    message = {"role": "user", "content": [{"type": "text", "text": "look"}, _image_part()]}
    text_client = GLMClient(api_key="test", model="glm-5.1")
    vision_client = GLMClient(api_key="test", model="glm-5v-turbo")

    text_content = text_client._prepare_messages(
        [message], text_client.model_for_messages([message])
    )[0]["content"]
    vision_content = vision_client._prepare_messages([message])[0]["content"]

    assert text_client.model_for_messages([{"role": "user", "content": "hello"}]) == (
        "glm-5.1"
    )
    assert text_client.model_for_messages([message]) == "glm-5v-turbo"
    assert text_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert vision_client.supports_image_input() is True
    assert vision_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert text_client.base_url == GLMClient.CODING_API_URL
    assert vision_client.base_url == GLMClient.MULTIMODAL_API_URL


def test_glm_can_disable_automatic_vision_routing():
    message = {"role": "user", "content": [{"type": "text", "text": "look"}, _image_part()]}
    client = GLMClient(api_key="test", model="glm-5.1", vision_model="disabled")

    prepared = client._prepare_messages([message])

    assert client.model_for_messages([message]) == "glm-5.1"
    assert all(part["type"] == "text" for part in prepared[0]["content"])
    assert "不支持图片附件" in prepared[0]["content"][-1]["text"]


def test_glm_error_includes_business_code_model_message_and_hint(monkeypatch):
    response = httpx.Response(
        429,
        json={"error": {"code": "1311", "message": "套餐未开放该模型"}},
    )

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: FakeHttpClient())
    client = GLMClient(api_key="test", model="glm-5.1", max_retries=0)

    with pytest.raises(GLMApiError) as caught:
        client.chat([{"role": "user", "content": "hello"}])

    error = str(caught.value)
    assert "HTTP 429" in error
    assert "model=glm-5.1" in error
    assert "code=1311" in error
    assert "套餐未开放该模型" in error
    assert "当前套餐未开放该视觉模型权限" in error


def test_glm_retries_transient_model_overload(monkeypatch):
    responses = [
        httpx.Response(
            429,
            json={"error": {"code": "1305", "message": "模型访问量过大"}},
        ),
        httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        ),
    ]

    class FakeHttpClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, *_args, **_kwargs):
            return responses.pop(0)

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: FakeHttpClient())
    client = GLMClient(
        api_key="test",
        model="glm-5.1",
        max_retries=2,
        retry_base_seconds=0,
    )

    result = client.chat([{"role": "user", "content": "hello"}])

    assert result["content"] == "ok"
    assert responses == []


def test_agent_sends_local_image_and_prunes_it_before_next_user_task(tmp_path):
    image_path = tmp_path / "screen.png"
    _write_image(image_path)

    class InspectingClient:
        def __init__(self) -> None:
            self.user_contents: list[Any] = []

        def chat(self, messages, tools=None, temperature=0.2):
            self.user_contents.append(messages[-1]["content"])
            return {"role": "assistant", "content": "看到了"}

    client = InspectingClient()
    agent = Agent(client, ToolRegistry(), workspace=tmp_path)

    assert agent.run("描述 @image:screen.png") == "看到了"
    assert isinstance(client.user_contents[0], list)
    assert agent.run("继续") == "看到了"
    old_image_message = agent.messages[1]
    assert isinstance(old_image_message["content"], list)
    assert all(part["type"] == "text" for part in old_image_message["content"])
    assert "历史图片附件已省略" in old_image_message["content"][-1]["text"]


def test_agent_attaches_tool_returned_image_after_tool_text(tmp_path):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="screenshot",
            description="Return a screenshot",
            parameters={"type": "object", "properties": {}},
            handler=lambda: ToolOutput("screenshot ready", (_image_part(),)),
        )
    )

    class ToolImageClient:
        def __init__(self) -> None:
            self.calls = 0

        def chat(self, messages, tools=None, temperature=0.2):
            self.calls += 1
            if self.calls == 1:
                return {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "shot_1",
                            "type": "function",
                            "function": {"name": "screenshot", "arguments": json.dumps({})},
                        }
                    ],
                }
            assert messages[-2]["role"] == "tool"
            assert messages[-2]["content"] == "screenshot ready"
            assert messages[-1]["role"] == "user"
            assert messages[-1]["content"][1]["type"] == "image_url"
            return {"role": "assistant", "content": "截图已分析"}

    answer = Agent(ToolImageClient(), registry, workspace=tmp_path).run("截屏并分析")

    assert answer == "截图已分析"
