"""M1 tests: the stable LLMBackend interface and its value types."""

from __future__ import annotations

import dataclasses

import pytest

from app.backends import BackendHealth, BackendSession, FakeBackend, LLMBackend
from app.backends.deepseek_web import DeepSeekWebBackend
from app.backends.events import MessageFinished, TextDelta


class TestInterfaceContract:
    def test_llmbackend_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            LLMBackend()  # type: ignore[abstract]

    def test_incomplete_subclass_is_rejected(self) -> None:
        class Partial(LLMBackend):
            backend_type = "partial"

            def health_check(self):
                raise NotImplementedError

            # create_session / stream_turn intentionally missing

        with pytest.raises(TypeError):
            Partial()  # type: ignore[abstract]

    def test_deepseek_backend_is_an_llmbackend(self) -> None:
        # Dummy token: construction is offline (no network in __init__).
        backend = DeepSeekWebBackend("dummy-offline-token")
        assert isinstance(backend, LLMBackend)
        assert backend.backend_type == "deepseek_web"

    def test_fake_backend_is_an_llmbackend(self) -> None:
        backend = FakeBackend(turns=[[TextDelta("x"), MessageFinished("stop")]])
        assert isinstance(backend, LLMBackend)
        assert backend.backend_type == "fake"

    def test_backend_type_satisfies_abstract_property_via_class_attr(self) -> None:
        # Both implementations use a plain class attribute; the abstract
        # property must accept it (documented in base.py).
        for cls, expected in (
            (DeepSeekWebBackend, "deepseek_web"),
            (FakeBackend, "fake"),
        ):
            assert cls.backend_type == expected  # type: ignore[abstract]


class TestValueTypes:
    def test_backend_session_is_frozen(self) -> None:
        session = BackendSession(session_id="s1")
        assert session.session_id == "s1"
        with pytest.raises(dataclasses.FrozenInstanceError):
            session.session_id = "other"  # type: ignore[misc]

    def test_backend_health_is_frozen_and_defaults_details(self) -> None:
        health = BackendHealth(backend_type="fake", ready=True)
        assert health.details == {}
        with pytest.raises(dataclasses.FrozenInstanceError):
            health.ready = False  # type: ignore[misc]
