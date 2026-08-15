"""M6 tests: incoming-tool normalization and tool prompt instructions.

Covers app/tools.py (ADR-023, docs/TOOL_CALLING_PROTOCOL.md): lenient
``tools[]`` normalization, canonical argument JSON, pragmatic schema
compatibility, and the deterministic control-instruction block.
"""

from __future__ import annotations

import json

import pytest

from app.tools import (
    TOOL_CALL_END_SENTINEL,
    TOOL_CALL_START_SENTINEL,
    CanonicalTool,
    arguments_compatible,
    build_tool_instructions,
    normalize_arguments_json,
    normalize_tools,
)


def _tool(name: str, description: str = "d", parameters=None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters
            if parameters is not None
            else {"type": "object", "properties": {}},
        },
    }


class TestNormalizeTools:
    def test_function_entries_become_canonical_tools(self) -> None:
        tools = normalize_tools(
            [
                _tool("read_file", "Read a file."),
                _tool("run_shell", "Run."),
            ]
        )
        assert tools == [
            CanonicalTool(
                name="read_file",
                description="Read a file.",
                schema={"type": "object", "properties": {}},
            ),
            CanonicalTool(name="run_shell", description="Run.", schema={
                "type": "object", "properties": {}
            }),
        ]

    def test_absent_or_empty_input_is_no_tools(self) -> None:
        assert normalize_tools(None) == []
        assert normalize_tools([]) == []

    def test_non_function_entries_are_skipped_not_rejected(self) -> None:
        # Lenient by design (ADR-023): one odd entry must not break chat.
        tools = normalize_tools(
            [
                {"type": "computer", "function": {"name": "x"}},
                {"function": {"name": "y"}},
                {"type": "function"},
                {"type": "function", "function": "nope"},
                {"type": "function", "function": {"name": 123}},
                {"type": "function", "function": {"name": "bad name!"}},
                "garbage",
                None,
                _tool("good"),
            ]
        )
        assert [tool.name for tool in tools] == ["good"]

    def test_duplicate_names_first_wins(self) -> None:
        tools = normalize_tools(
            [_tool("dup", "first"), _tool("dup", "second")]
        )
        assert len(tools) == 1
        assert tools[0].description == "first"

    def test_missing_description_and_parameters_are_normalized(self) -> None:
        tools = normalize_tools(
            [{"type": "function", "function": {"name": "bare"}}]
        )
        assert tools == [CanonicalTool(name="bare", description="", schema=None)]

    def test_qwen_code_style_tool_names_are_accepted(self) -> None:
        tools = normalize_tools(
            [
                _tool("read_file"),
                _tool("computer_use__click"),
                _tool("mcp__github__create_issue"),
                _tool("tool-2"),
            ]
        )
        assert [tool.name for tool in tools] == [
            "read_file",
            "computer_use__click",
            "mcp__github__create_issue",
            "tool-2",
        ]

    def test_deterministic_output(self) -> None:
        raw = [_tool("a"), _tool("b")]
        assert normalize_tools(raw) == normalize_tools(raw)


class TestNormalizeArgumentsJson:
    def test_dict_becomes_compact_json(self) -> None:
        assert (
            normalize_arguments_json({"file_path": "src/main.py", "n": 1})
            == '{"file_path":"src/main.py","n":1}'
        )

    def test_json_string_is_renormalized(self) -> None:
        assert (
            normalize_arguments_json('{ "file_path" : "src/main.py" }')
            == '{"file_path":"src/main.py"}'
        )

    def test_utf8_is_preserved_not_escaped(self) -> None:
        assert normalize_arguments_json({"path": "файл/文件.txt"}) == (
            '{"path":"файл/文件.txt"}'
        )

    def test_non_object_arguments_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON object"):
            normalize_arguments_json([1, 2])
        with pytest.raises(ValueError, match="JSON object"):
            normalize_arguments_json('"str"')
        with pytest.raises(ValueError, match="JSON object"):
            normalize_arguments_json(None)

    def test_invalid_json_string_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            normalize_arguments_json("{oops")


class TestArgumentsCompatible:
    SCHEMA = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "limit": {"type": "integer"},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "items": {"type": "array"},
            "meta": {"type": "object"},
        },
        "required": ["file_path"],
    }

    def test_valid_arguments_pass(self) -> None:
        assert arguments_compatible({"file_path": "a.py"}, self.SCHEMA)

    def test_missing_required_key_fails(self) -> None:
        assert not arguments_compatible({}, self.SCHEMA)

    def test_type_mismatches_fail(self) -> None:
        assert not arguments_compatible({"file_path": 7}, self.SCHEMA)
        assert not arguments_compatible(
            {"file_path": "a", "limit": "7"}, self.SCHEMA
        )
        assert not arguments_compatible(
            {"file_path": "a", "flag": "yes"}, self.SCHEMA
        )

    def test_bool_is_not_an_integer_or_number(self) -> None:
        assert not arguments_compatible(
            {"file_path": "a", "limit": True}, self.SCHEMA
        )
        assert not arguments_compatible(
            {"file_path": "a", "ratio": False}, self.SCHEMA
        )

    def test_int_satisfies_number(self) -> None:
        assert arguments_compatible(
            {"file_path": "a", "ratio": 2}, self.SCHEMA
        )

    def test_container_types_match_shallowly(self) -> None:
        assert arguments_compatible(
            {"file_path": "a", "items": [1], "meta": {"k": 1}}, self.SCHEMA
        )
        assert not arguments_compatible(
            {"file_path": "a", "items": {"not": "array"}}, self.SCHEMA
        )

    def test_unknown_properties_are_allowed(self) -> None:
        assert arguments_compatible(
            {"file_path": "a", "extra": "ok"}, self.SCHEMA
        )

    def test_no_schema_or_unrecognized_declarations_pass(self) -> None:
        assert arguments_compatible({"anything": 1}, None)
        assert arguments_compatible({"x": 1}, {"type": "object"})
        assert arguments_compatible(
            {"x": 1}, {"properties": {"x": {"type": ["string", "null"]}}}
        )

    def test_non_dict_arguments_fail(self) -> None:
        assert not arguments_compatible("nope", None)  # type: ignore[arg-type]


class TestBuildToolInstructions:
    def test_contains_protocol_sentinels_and_rules(self) -> None:
        text = build_tool_instructions([CanonicalTool("t", "d", None)])
        assert TOOL_CALL_START_SENTINEL in text
        assert TOOL_CALL_END_SENTINEL in text
        assert "- t: d" in text
        assert '"name" must be one of the tools listed above.' in text
        assert "at most ONE envelope" in text

    def test_schema_is_rendered_as_compact_json(self) -> None:
        schema = {
            "type": "object",
            "properties": {"file_path": {"type": "string"}},
            "required": ["file_path"],
        }
        text = build_tool_instructions([CanonicalTool("read", "r", schema)])
        assert (
            "  parameters: "
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            in text
        )

    def test_tool_without_description_renders_name_only(self) -> None:
        text = build_tool_instructions([CanonicalTool("bare", "", None)])
        assert "\n- bare\n" in text

    def test_required_mode_demands_an_envelope(self) -> None:
        text = build_tool_instructions(
            [CanonicalTool("t", "d", None)], required=True
        )
        assert "You MUST request exactly one tool call now" in text

    def test_default_mode_allows_a_normal_answer(self) -> None:
        text = build_tool_instructions([CanonicalTool("t", "d", None)])
        assert "You may either answer normally" in text

    def test_default_mode_forbids_simulated_tool_use(self) -> None:
        # ADR-029: the optional wording must forbid prose-simulated tool
        # execution — a live acceptance turn answered by NARRATING a
        # tool loop with fabricated results instead of an envelope.
        text = build_tool_instructions([CanonicalTool("t", "d", None)])
        assert "NEVER simulate or narrate tool execution in prose" in text
        assert "you MUST request it with" in text
        assert (
            "- Never describe a tool call in prose; either output a real "
            "envelope or answer normally." in text
        )

    def test_deterministic(self) -> None:
        tools = [CanonicalTool("a", "x", None), CanonicalTool("b", "y", None)]
        assert build_tool_instructions(tools) == build_tool_instructions(tools)

    def test_multiline_description_compacts_to_first_nonempty_line(self) -> None:
        description = "\n  First line of the tool.\nSecond line.\nThird line."
        text = build_tool_instructions([CanonicalTool("t", description, None)])
        assert "- t: First line of the tool." in text
        assert "Second line." not in text
        assert "Third line." not in text

    def test_long_first_line_is_capped_with_ellipsis(self) -> None:
        text = build_tool_instructions(
            [CanonicalTool("t", "x" * 200, None)]
        )
        compacted = next(
            line[len("- t: ") :]
            for line in text.splitlines()
            if line.startswith("- t: ")
        )
        assert compacted == "x" * 149 + "\u2026"
        assert len(compacted) == 150

    def test_whitespace_only_description_renders_name_only(self) -> None:
        text = build_tool_instructions(
            [CanonicalTool("ws", "   \n  \n", None)]
        )
        assert "\n- ws\n" in text

    def test_schema_descriptions_are_stripped_at_every_depth(self) -> None:
        schema = {
            "type": "object",
            "description": "Top-level prose.",
            "properties": {
                "path": {"type": "string", "description": "Nested prose."},
                "mode": {
                    "type": "string",
                    "enum": ["a", "b"],
                    "description": "More prose.",
                },
            },
            "required": ["path"],
        }
        text = build_tool_instructions([CanonicalTool("t", "d", schema)])
        stripped = {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "mode": {"type": "string", "enum": ["a", "b"]},
            },
            "required": ["path"],
        }
        assert (
            "  parameters: "
            + json.dumps(stripped, ensure_ascii=False, separators=(",", ":"))
            in text
        )
        assert "Top-level prose." not in text
        assert "Nested prose." not in text

    def test_schema_compaction_does_not_mutate_the_input(self) -> None:
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "p"}},
        }
        build_tool_instructions([CanonicalTool("t", "d", schema)])
        assert schema["properties"]["path"]["description"] == "p"

    def test_validation_members_survive_compaction(self) -> None:
        # The rendered block loses only prose; every member the envelope
        # parser validates against (type/required/enum) must stay intact.
        schema = {
            "type": "object",
            "description": "prose",
            "properties": {
                "file_path": {"type": "string", "description": "p"},
            },
            "required": ["file_path"],
        }
        text = build_tool_instructions([CanonicalTool("read", "d", schema)])
        rendered = next(
            line[len("  parameters: ") :]
            for line in text.splitlines()
            if line.startswith("  parameters: ")
        )
        parsed = json.loads(rendered)
        assert parsed["required"] == ["file_path"]
        assert parsed["properties"]["file_path"] == {"type": "string"}
        assert "prose" not in rendered
