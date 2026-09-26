"""Request-local Team budgets; no mutation of the shared client across workers."""

import os

from stellarcode.llm.types import current_llm_operation


def team_request_options() -> dict[str, object]:
    role = current_llm_operation().removeprefix("team-")
    if current_llm_operation() not in {"team-planner", "team-worker", "team-reviewer"}:
        return {}
    prefix = f"TEAM_{role.upper()}_"
    effort = os.getenv(prefix + "REASONING_EFFORT", os.getenv("TEAM_REASONING_EFFORT", "low")).strip()
    # Empty effort explicitly disables this optional API parameter for other endpoints.
    if effort not in {"", "low", "medium", "high", "max", "minimal", "none"}:
        raise ValueError(f"Invalid {prefix}REASONING_EFFORT: {effort}")
    limit = int(os.getenv(prefix + "MAX_OUTPUT_TOKENS", "8192" if role == "worker" else "4096"))
    if limit < 0:
        raise ValueError(f"{prefix}MAX_OUTPUT_TOKENS must be nonnegative")
    field = os.getenv("TEAM_OUTPUT_TOKEN_PARAMETER", "max_tokens").strip()
    if field not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("TEAM_OUTPUT_TOKEN_PARAMETER must be max_tokens or max_completion_tokens")
    options: dict[str, object] = {}
    if effort:
        options["reasoning_effort"] = effort
    if limit:
        options[field] = limit
    return options
