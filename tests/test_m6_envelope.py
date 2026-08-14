"""M6 tests: the strict control-envelope parser (app/tool_envelope.py).

Pins docs/TOOL_CALLING_PROTOCOL.md: envelope content is never partially
streamed, only a fully valid envelope becomes a tool call (rules 1–7),
anything invalid is flushed back as the honest plain text the model
produced, and at most ONE tool call is emitted per turn.
"""

from __future__ import annotations

import re

import pytest

from app.tool_envelope import (
    EmittedToolCall,
    EnvelopeParser,
    ToolCallEmitted,
    new_tool_call_id,
    parse_envelope_from_text,
)
from app.tools import (
    TOOL_CALL_END_SENTINEL,
    TOOL_CALL_START_SENTINEL,
    CanonicalTool,
)

READ_FILE = CanonicalTool(
    name="read_file",
    description="Read a file.",
    schema={
        "type": "object",
        "properties": {"file_path": {"type": "string"}},
        "required": ["file_path"],
    },
)
NO_SCHEMA = CanonicalTool(name="no_schema", description="", schema=None)
TOOLS = [READ_FILE, NO_SCHEMA]


def _envelope(name: str = "read_file", arguments: str = '{"file_path": "a.py"}') -> str:
    return (
        f'{TOOL_CALL_START_SENTINEL}\n{{"name":"{name}","arguments":{arguments}}}\n'
        f"{TOOL_CALL_END_SENTINEL}"
    )


def _run(parser: EnvelopeParser, text: str) -> list:
    return parser.feed(text) + parser.finalize()


class TestValidEnvelopes:
    def test_whole_envelope_in_one_chunk(self) -> None:
        parser = EnvelopeParser(TOOLS)
        outputs = _run(parser, _envelope())
        assert len(outputs) == 1
        (emitted,) = outputs
        assert isinstance(emitted, ToolCallEmitted)
        assert emitted.call.name == "read_file"
        assert emitted.call.arguments_json == '{"file_path":"a.py"}'
        assert re.fullmatch(r"call_dsqg_[0-9a-f]{32}", emitted.call.id)

    def test_text_before_the_envelope_is_rendered(self) -> None:
        parser = EnvelopeParser(TOOLS)
        outputs = _run(parser, "Let me check that file.\n" + _envelope())
        assert outputs[0] == "Let me check that file.\n"
        assert isinstance(outputs[1], ToolCallEmitted)

    def test_text_after_a_valid_envelope_is_discarded(self) -> None:
        parser = EnvelopeParser(TOOLS)
        outputs = _run(parser, _envelope() + "\nHope that helps!")
        assert len(outputs) == 1
        assert isinstance(outputs[0], ToolCallEmitted)

    def test_missing_arguments_falls_back_to_empty_object(self) -> None:
        parser = EnvelopeParser(TOOLS)
        text = (
            f'{TOOL_CALL_START_SENTINEL}{{"name":"no_schema"}}'
            f"{TOOL_CALL_END_SENTINEL}"
        )
        (emitted,) = _run(parser, text)
        assert isinstance(emitted, ToolCallEmitted)
        assert emitted.call.arguments_json == "{}"

    def test_whitespace_around_the_json_is_tolerated(self) -> None:
        parser = EnvelopeParser(TOOLS)
        text = f"{TOOL_CALL_START_SENTINEL}\n\n  {_envelope().splitlines()[1]}  \n{TOOL_CALL_END_SENTINEL}"
        outputs = _run(parser, text)
        assert any(isinstance(out, ToolCallEmitted) for out in outputs)

    def test_ids_are_unique_per_call(self) -> None:
        assert new_tool_call_id() != new_tool_call_id()


class TestStreamingBoundaries:
    @pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 13])
    def test_envelope_split_across_tiny_chunks(self, chunk_size: int) -> None:
        # Feed the same turn in fixed-size increments, split at arbitrary
        # points INSIDE the sentinels: the parser must hold back candidate
        # prefixes and never leak a partial sentinel as text.
        parser = EnvelopeParser(TOOLS)
        text = "Prefix text. " + _envelope()
        outputs: list = []
        for i in range(0, len(text), chunk_size):
            outputs.extend(parser.feed(text[i : i + chunk_size]))
        outputs.extend(parser.finalize())
        texts = [out for out in outputs if isinstance(out, str)]
        assert "".join(texts) == "Prefix text. "
        assert TOOL_CALL_START_SENTINEL not in "".join(texts)
        assert sum(isinstance(out, ToolCallEmitted) for out in outputs) == 1

    def test_sentinel_prefix_that_is_not_an_envelope_is_flushed(self) -> None:
        parser = EnvelopeParser(TOOLS)
        outputs = _run(parser, "a << b and <<<DSQG not a sentinel")
        assert "".join(out for out in outputs if isinstance(out, str)) == (
            "a << b and <<<DSQG not a sentinel"
        )
        assert not any(isinstance(out, ToolCallEmitted) for out in outputs)

    def test_truncated_envelope_is_flushed_raw_at_finalize(self) -> None:
        parser = EnvelopeParser(TOOLS)
        partial = "before " + TOOL_CALL_START_SENTINEL + '{"name":"read_file"'
        outputs = _run(parser, partial)
        assert "".join(out for out in outputs if isinstance(out, str)) == partial

    def test_no_output_while_an_envelope_is_undecided(self) -> None:
        parser = EnvelopeParser(TOOLS)
        assert parser.feed(TOOL_CALL_START_SENTINEL[:5]) == []
        assert parser.feed(TOOL_CALL_START_SENTINEL[5:]) == []
        assert (
            parser.feed('{"name":"read_file","arguments":{"file_path":"a"}}')
            == []
        )
        outputs = parser.feed(TOOL_CALL_END_SENTINEL)
        assert [type(out) for out in outputs] == [ToolCallEmitted]


class TestInvalidEnvelopesAreHonestText:
    def _assert_flushed_raw(self, text: str) -> None:
        parser = EnvelopeParser(TOOLS)
        outputs = _run(parser, text)
        assert "".join(out for out in outputs if isinstance(out, str)) == text
        assert not any(isinstance(out, ToolCallEmitted) for out in outputs)

    def test_unknown_tool_name(self) -> None:
        self._assert_flushed_raw(_envelope(name="does_not_exist"))

    def test_malformed_json(self) -> None:
        text = (
            f"{TOOL_CALL_START_SENTINEL}{{not json}}"
            f"{TOOL_CALL_END_SENTINEL}"
        )
        self._assert_flushed_raw(text)

    def test_non_object_payload(self) -> None:
        text = (
            f'{TOOL_CALL_START_SENTINEL}["read_file"]'
            f"{TOOL_CALL_END_SENTINEL}"
        )
        self._assert_flushed_raw(text)

    def test_non_string_name(self) -> None:
        text = (
            f'{TOOL_CALL_START_SENTINEL}{{"name":123,"arguments":{{}}}}'
            f"{TOOL_CALL_END_SENTINEL}"
        )
        self._assert_flushed_raw(text)

    def test_arguments_not_an_object(self) -> None:
        text = (
            f'{TOOL_CALL_START_SENTINEL}{{"name":"read_file","arguments":[1]}}'
            f"{TOOL_CALL_END_SENTINEL}"
        )
        self._assert_flushed_raw(text)

    def test_missing_required_argument(self) -> None:
        text = (
            f'{TOOL_CALL_START_SENTINEL}{{"name":"read_file","arguments":{{}}}}'
            f"{TOOL_CALL_END_SENTINEL}"
        )
        self._assert_flushed_raw(text)

    def test_argument_type_mismatch(self) -> None:
        text = (
            f'{TOOL_CALL_START_SENTINEL}'
            '{{"name":"read_file","arguments":{"file_path":42}}}'
            f"{TOOL_CALL_END_SENTINEL}"
        )
        self._assert_flushed_raw(text)

    def test_invalid_region_then_valid_envelope_still_works(self) -> None:
        # An invalid region is flushed raw and scanning continues (M6; the
        # bounded model repair turn arrives in M7).
        parser = EnvelopeParser(TOOLS)
        bad = f"{TOOL_CALL_START_SENTINEL}{{oops}}{TOOL_CALL_END_SENTINEL}"
        outputs = _run(parser, bad + "\n" + _envelope())
        texts = [out for out in outputs if isinstance(out, str)]
        assert "".join(texts) == bad + "\n"
        assert sum(isinstance(out, ToolCallEmitted) for out in outputs) == 1


class TestOneCallPerTurn:
    def test_second_valid_envelope_is_ignored(self) -> None:
        parser = EnvelopeParser(TOOLS)
        outputs = _run(parser, _envelope() + _envelope())
        emitted = [out for out in outputs if isinstance(out, ToolCallEmitted)]
        assert len(emitted) == 1

    def test_emitted_call_is_exposed_on_the_parser(self) -> None:
        parser = EnvelopeParser(TOOLS)
        assert parser.emitted_call is None
        _run(parser, _envelope())
        assert isinstance(parser.emitted_call, EmittedToolCall)

    def test_no_tools_means_no_envelope_is_ever_valid(self) -> None:
        parser = EnvelopeParser([])
        outputs = _run(parser, _envelope())
        assert "".join(out for out in outputs if isinstance(out, str)) == (
            _envelope()
        )


class TestParseEnvelopeFromText:
    def test_returns_text_parts_and_call(self) -> None:
        text_parts, call = parse_envelope_from_text(
            "thinking... " + _envelope(), TOOLS
        )
        assert text_parts == ["thinking... "]
        assert call is not None
        assert call.name == "read_file"

    def test_plain_text_returns_no_call(self) -> None:
        text_parts, call = parse_envelope_from_text("just words", TOOLS)
        assert text_parts == ["just words"]
        assert call is None
