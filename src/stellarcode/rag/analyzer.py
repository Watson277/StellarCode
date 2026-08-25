"""Static AST analysis for code symbols and lightweight project relationships."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from stellarcode.rag.model import CodeRelation


class CodeAnalyzer:
    def analyze_file(
        self,
        file_path: str | Path,
        display_path: str | None = None,
    ) -> list[CodeRelation]:
        path = Path(file_path)
        if path.suffix.lower() != ".py":
            return []
        content = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        source_path = display_path or str(path)
        relations: set[CodeRelation] = set()
        self._extract_imports(source_path, tree, relations)
        self._extract_classes(source_path, tree, relations)
        self._extract_top_level_functions(source_path, tree, relations)
        return sorted(
            relations,
            key=lambda item: (
                item.from_file,
                item.from_name,
                item.relation_type,
                item.to_name,
            ),
        )

    def _extract_imports(
        self,
        file_path: str,
        tree: ast.Module,
        relations: set[CodeRelation],
    ) -> None:
        for node in ast.walk(tree):
            module_names: list[str] = []
            if isinstance(node, ast.Import):
                module_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_names.append(node.module)
            for module_name in module_names:
                root_module = module_name.split(".", 1)[0]
                if root_module in sys.stdlib_module_names:
                    continue
                relations.add(
                    CodeRelation(file_path, "file", None, module_name, "imports")
                )

    def _extract_classes(
        self,
        file_path: str,
        tree: ast.Module,
        relations: set[CodeRelation],
    ) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for base in node.bases:
                base_name = _expression_name(base)
                if base_name:
                    relations.add(
                        CodeRelation(file_path, node.name, None, base_name, "extends")
                    )
            for member in node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                method_name = f"{node.name}.{member.name}"
                relations.add(
                    CodeRelation(file_path, node.name, file_path, method_name, "contains")
                )
                self._extract_calls(file_path, method_name, member, relations)

    def _extract_top_level_functions(
        self,
        file_path: str,
        tree: ast.Module,
        relations: set[CodeRelation],
    ) -> None:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._extract_calls(file_path, node.name, node, relations)

    def _extract_calls(
        self,
        file_path: str,
        caller: str,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        relations: set[CodeRelation],
    ) -> None:
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            callee = _expression_name(node.func)
            if callee:
                relations.add(CodeRelation(file_path, caller, None, callee, "calls"))


def _expression_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _expression_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        return _expression_name(node.value)
    try:
        return ast.unparse(node)
    except Exception:
        return ""
