"""Rich terminal rendering for human-in-the-loop approval prompts."""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from stellarcode.hitl.model import ApprovalRequest


def build_approval_panel(request: ApprovalRequest, use_icons: bool = True) -> Panel:
    summary = Table.grid(padding=(0, 1))
    summary.add_column(style="bold", width=6, no_wrap=True)
    summary.add_column(ratio=1, overflow="fold")
    summary.add_row("工具:", Text(request.tool_name, style="bold cyan"))
    level = request.display_danger_level if use_icons else request.danger_level_label
    summary.add_row("等级:", Text(level, style=_level_style(request)))
    summary.add_row("风险:", Text(request.risk_description))
    if request.suggestion:
        summary.add_row("建议:", Text(request.suggestion))
    if request.caller_context:
        summary.add_row("来源:", Text(request.caller_context, style="dim"))

    arguments = Table.grid(padding=(0, 1))
    arguments.add_column(width=4)
    arguments.add_column(style="bold", no_wrap=True)
    arguments.add_column(ratio=1, overflow="fold")
    for key, value in request.argument_rows:
        arguments.add_row("", f"{key}:", Text(value, style="white"))

    return Panel(
        Group(summary, Rule(style="dim"), Text("参数:", style="bold"), arguments),
        title=(
            "[bold yellow]⚠️  需要审批[/bold yellow]"
            if use_icons
            else "[bold yellow]需要审批[/bold yellow]"
        ),
        title_align="left",
        border_style="yellow",
        box=box.SQUARE,
        padding=(0, 1),
    )


def _level_style(request: ApprovalRequest) -> str:
    return {
        "safe": "green",
        "medium": "yellow",
        "high": "bold red",
    }.get(request.danger_level, "white")
