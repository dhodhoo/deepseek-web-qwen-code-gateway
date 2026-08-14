"""M2 tests: deterministic OpenAI-messages → prompt compiler."""

from __future__ import annotations

import pytest

from app.openai_types import ChatMessage
from app.prompt_compiler import (
    UnsupportedMessageError,
    compile_messages_to_prompt,
    extract_text,
)


def _msg(role: str, content=None, **extra) -> ChatMessage:
    return ChatMessage(role=role, content=content, **extra)


class TestExtractText:
    def test_none_becomes_empty_string(self) -> None:
        assert extract_text(None) == ""

    def test_string_passes_through(self) -> None:
        assert extract_text("hello") == "hello"

    def test_text_parts_are_joined_with_newlines(self) -> None:
        content = [
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
        ]
        assert extract_text(content) == "one\ntwo"

    def test_non_text_parts_are_ignored(self) -> None:
        content = [
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
            {"type": "text", "text": "caption"},
            {"type": "input_audio"},
        ]
        assert extract_text(content) == "caption"

    def test_empty_list_becomes_empty_string(self) -> None:
        assert extract_text([]) == ""

    def test_malformed_text_part_is_ignored(self) -> None:
        content = [{"type": "text", "text": 123}, {"type": "text", "text": "ok"}]
        assert extract_text(content) == "ok"


class TestCompileMessages:
    def test_plain_history_compiles_to_labeled_blocks(self) -> None:
        messages = [
            _msg("system", "Be brief."),
            _msg("user", "Hello"),
            _msg("assistant", "Hi there"),
            _msg("user", "How are you?"),
        ]
        assert compile_messages_to_prompt(messages) == (
            "[system]\nBe brief.\n\n"
            "[user]\nHello\n\n"
            "[assistant]\nHi there\n\n"
            "[user]\nHow are you?"
        )

    def test_compilation_is_deterministic(self) -> None:
        messages = [_msg("user", "same"), _msg("assistant", "same")]
        first = compile_messages_to_prompt(messages)
        second = compile_messages_to_prompt(messages)
        assert first == second

    def test_content_list_is_reduced_to_text(self) -> None:
        messages = [
            _msg("user", [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
        ]
        assert compile_messages_to_prompt(messages) == "[user]\na\nb"

    def test_user_null_content_compiles_to_empty_block(self) -> None:
        assert compile_messages_to_prompt([_msg("user", None)]) == "[user]\n"

    def test_empty_history_is_rejected(self) -> None:
        with pytest.raises(UnsupportedMessageError, match="must not be empty"):
            compile_messages_to_prompt([])

    def test_tool_role_is_rejected_with_m6_pointer(self) -> None:
        with pytest.raises(UnsupportedMessageError, match="M6"):
            compile_messages_to_prompt(
                [_msg("user", "hi"), _msg("tool", "result", tool_call_id="call_1")]
            )

    def test_unknown_role_is_rejected(self) -> None:
        with pytest.raises(UnsupportedMessageError, match="unsupported role"):
            compile_messages_to_prompt([_msg("developer", "hi")])

    def test_assistant_tool_calls_are_rejected_with_m6_pointer(self) -> None:
        with pytest.raises(UnsupportedMessageError, match="M6"):
            compile_messages_to_prompt(
                [
                    _msg("user", "hi"),
                    _msg(
                        "assistant",
                        "thinking",
                        tool_calls=[
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "run", "arguments": "{}"},
                            }
                        ],
                    ),
                ]
            )

    def test_assistant_null_content_is_rejected_with_m6_pointer(self) -> None:
        with pytest.raises(UnsupportedMessageError, match="M6"):
            compile_messages_to_prompt([_msg("user", "hi"), _msg("assistant", None)])

    def test_error_message_identifies_the_offending_index(self) -> None:
        with pytest.raises(UnsupportedMessageError, match=r"messages\[1\]"):
            compile_messages_to_prompt([_msg("user", "hi"), _msg("tool", "x")])
