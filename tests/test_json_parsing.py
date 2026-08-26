"""Tests for extract_json — the layer that salvages structured data from
model output that isn't clean JSON (a very common real-world failure)."""

import pytest

from app.services import ai_service as ai


@pytest.mark.failure
def test_plain_json():
    assert ai.extract_json('{"caller_name": "Sam"}') == {"caller_name": "Sam"}


@pytest.mark.failure
def test_json_wrapped_in_markdown_fence():
    raw = '```json\n{"caller_name": "Sam", "enough_to_complete": false}\n```'
    got = ai.extract_json(raw)
    assert got == {"caller_name": "Sam", "enough_to_complete": False}


@pytest.mark.failure
def test_json_with_leading_prose():
    raw = 'Sure! Here is the data: {"service_requested": "fade"} hope that helps'
    assert ai.extract_json(raw) == {"service_requested": "fade"}


@pytest.mark.failure
def test_unrecoverable_returns_none():
    assert ai.extract_json("I'm sorry, I can't do that.") is None


@pytest.mark.failure
def test_empty_returns_none():
    assert ai.extract_json("") is None
