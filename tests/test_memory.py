from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from stellarcode.agent import Agent
from stellarcode.memory import (
    MemoryEntry,
    MemoryManager,
    MemoryType,
    ProjectMemoryService,
    estimate_tokens,
)
from stellarcode.memory.long_term import LongTermMemory
from stellarcode.tools import build_default_registry


class EchoSystemClient:
    def __init__(self) -> None:
        self.seen_system_prompts: list[str] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.seen_system_prompts.append(messages[0]["content"])
        return {"role": "assistant", "content": "ok"}


def test_estimate_tokens_handles_chinese_and_ascii():
    assert estimate_tokens("hello world") > 0
    assert estimate_tokens("项目使用 Python") > 0


def test_long_term_memory_persists_and_dedupes(tmp_path):
    memory = LongTermMemory(tmp_path)
    entry = MemoryEntry.create("项目默认使用 Python 3.10", MemoryType.FACT)

    assert memory.store(entry)
    assert not memory.store(MemoryEntry.create("项目默认使用 Python 3.10", MemoryType.FACT))

    reloaded = LongTermMemory(tmp_path)
    assert len(reloaded.all()) == 1
    assert reloaded.all()[0].content == "项目默认使用 Python 3.10"


def test_long_term_memory_quarantines_corrupt_json_without_blocking_startup(tmp_path):
    storage_file = tmp_path / "long_term_memory.json"
    corrupt_bytes = b'{"entries": [{"id": "truncated"}'
    storage_file.write_bytes(corrupt_bytes)

    memory = LongTermMemory(tmp_path)

    assert memory.all() == []
    assert not storage_file.exists()
    quarantined = list(tmp_path.glob("long_term_memory.corrupt-*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == corrupt_bytes
    assert str(quarantined[0]) in memory.warnings()[0]

    assert memory.store(MemoryEntry.create("recovered fact", MemoryType.FACT))
    assert storage_file.is_file()
    assert quarantined[0].read_bytes() == corrupt_bytes


def test_long_term_memory_quarantines_structurally_invalid_entries(tmp_path):
    storage_file = tmp_path / "long_term_memory.json"
    storage_file.write_text(
        '{"entries": [{"content": "missing fields"}]}', encoding="utf-8"
    )

    memory = LongTermMemory(tmp_path)

    assert memory.all() == []
    assert memory.warnings()
    assert len(list(tmp_path.glob("long_term_memory.corrupt-*.json"))) == 1


def test_memory_manager_retrieves_saved_fact(tmp_path):
    manager = MemoryManager(storage_dir=tmp_path)
    manager.save_fact("项目默认使用 Maven 构建")

    results = manager.search("Maven 构建")

    assert results
    assert results[0].content == "项目默认使用 Maven 构建"


def test_clear_short_term_keeps_long_term_memory(tmp_path):
    manager = MemoryManager(storage_dir=tmp_path)
    manager.add_user_message("临时对话")
    manager.save_fact("长期事实")

    manager.clear_short_term()

    assert manager.short_term.token_count() == 0
    assert manager.long_term.all()[0].content == "长期事实"


def test_memory_manager_and_token_budget_are_safe_for_parallel_writers(tmp_path):
    manager = MemoryManager(storage_dir=tmp_path, short_term_tokens=100_000)

    def add_entry(index: int) -> None:
        manager.add_tool_result("parallel", f"result-{index}")
        manager.token_budget.record_usage(10, 2)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(add_entry, range(100)))

    assert len(manager.short_term.entries) == 100
    assert manager.token_budget.llm_call_count == 100
    assert manager.token_budget.total_input_tokens == 1000
    assert manager.token_budget.total_output_tokens == 200


def test_project_memory_service_shares_long_term_but_isolates_conversations(tmp_path):
    service = ProjectMemoryService(tmp_path, context_window=100_000)
    first = service.create_conversation_manager(short_term_tokens=10_000)
    second = service.create_conversation_manager(short_term_tokens=10_000)

    assert first is not second
    assert first.short_term is not second.short_term
    assert first.token_budget is not second.token_budget
    assert first.long_term is second.long_term

    first.add_user_message("Only conversation one should contain this message")
    first.save_fact("The project uses Python 3.12")

    assert len(first.short_term.all()) == 1
    assert second.short_term.all() == []
    assert [entry.content for entry in second.search("Python 3.12")] == [
        "The project uses Python 3.12"
    ]


def test_project_memory_service_preserves_parallel_long_term_writes(tmp_path):
    service = ProjectMemoryService(tmp_path)
    managers = [service.create_conversation_manager() for _ in range(8)]

    def save_fact(index: int) -> None:
        managers[index % len(managers)].save_fact(f"shared-project-fact-{index}")

    with ThreadPoolExecutor(max_workers=len(managers)) as executor:
        list(executor.map(save_fact, range(100)))

    assert service.long_term.count() == 100
    assert len(LongTermMemory(tmp_path).all()) == 100


def test_compression_creates_summary_and_extracts_fact(tmp_path):
    manager = MemoryManager(storage_dir=tmp_path, short_term_tokens=20)
    manager.add_user_message("请记住 项目 使用 JDK 17 和 Maven 构建")
    manager.add_assistant_message("好的，我会记住")
    manager.add_user_message(
        "继续补充很多很多很多很多很多很多很多很多很多上下文 "
        "with extra details about build configuration and project defaults"
    )

    manager.compress_if_needed()

    assert manager.short_term.compressed_summaries or manager.long_term.all()


def test_agent_injects_relevant_memory_into_system_prompt(tmp_path):
    manager = MemoryManager(storage_dir=tmp_path)
    manager.save_fact("项目默认使用 Python 3.10")
    client = EchoSystemClient()
    agent = Agent(
        llm_client=client,
        tool_registry=build_default_registry(tmp_path),
        memory_manager=manager,
    )

    answer = agent.run("Python 版本是什么")

    assert answer == "ok"
    assert "Relevant memory" in client.seen_system_prompts[-1]
    assert "Python 3.10" in client.seen_system_prompts[-1]
