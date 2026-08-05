from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from uapi import UapiClient
from uapi.errors import UapiError


mcp = FastMCP(
    "StellarCode Weather",
    instructions="Query current weather and optional forecasts through uapis.cn.",
)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    return value


def fetch_weather(
    city: str = "",
    adcode: str = "",
    extended: bool = False,
    forecast: bool = False,
    hourly: bool = False,
    minutely: bool = False,
    indices: bool = False,
    lang: Literal["zh", "en"] = "zh",
) -> dict[str, Any]:
    token = os.getenv("UAPI_TOKEN", "").strip()
    if not token:
        raise RuntimeError("UAPI_TOKEN is not configured")

    client = UapiClient("https://uapis.cn", token=token, timeout=15.0)
    try:
        result = client.misc.get_misc_weather(
            city=city.strip(),
            adcode=adcode.strip(),
            extended=extended,
            forecast=forecast,
            hourly=hourly,
            minutely=minutely,
            indices=indices,
            lang=lang,
        )
    except UapiError as exc:
        raise RuntimeError(f"UAPI weather request failed: {exc}") from exc

    converted = _jsonable(result)
    if not isinstance(converted, dict):
        raise RuntimeError("UAPI returned an unexpected weather response")
    return converted


@mcp.tool()
def query_weather(
    city: str = "",
    adcode: str = "",
    extended: bool = False,
    forecast: bool = False,
    hourly: bool = False,
    minutely: bool = False,
    indices: bool = False,
    lang: Literal["zh", "en"] = "zh",
) -> dict[str, Any]:
    """Query weather by city or adcode; omit both to use caller IP location.

    Enable extended for air quality and feels-like data, forecast for daily forecasts,
    hourly for 24-hour forecasts, minutely for near-term precipitation in China, and
    indices for lifestyle advice.
    """
    return fetch_weather(
        city=city,
        adcode=adcode,
        extended=extended,
        forecast=forecast,
        hourly=hourly,
        minutely=minutely,
        indices=indices,
        lang=lang,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
