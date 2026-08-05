from __future__ import annotations

from dataclasses import dataclass

import pytest

from mcp_servers import weather_server


@dataclass
class FakeWeather:
    city: str
    temperature: float

    def model_dump(self, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {"city": self.city, "temperature": self.temperature}


def test_fetch_weather_calls_uapi_with_selected_options(monkeypatch):
    captured: dict[str, object] = {}

    class FakeMisc:
        def get_misc_weather(self, **kwargs):
            captured.update(kwargs)
            return FakeWeather(city="北京", temperature=26.5)

    class FakeClient:
        def __init__(self, base_url: str, token: str, timeout: float):
            captured.update(base_url=base_url, token=token, timeout=timeout)
            self.misc = FakeMisc()

    monkeypatch.setenv("UAPI_TOKEN", "test-token")
    monkeypatch.setattr(weather_server, "UapiClient", FakeClient)

    result = weather_server.fetch_weather(city=" 北京 ", forecast=True)

    assert result == {"city": "北京", "temperature": 26.5}
    assert captured["base_url"] == "https://uapis.cn"
    assert captured["token"] == "test-token"
    assert captured["city"] == "北京"
    assert captured["forecast"] is True


def test_fetch_weather_requires_token(monkeypatch):
    monkeypatch.delenv("UAPI_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="UAPI_TOKEN"):
        weather_server.fetch_weather(city="北京")
