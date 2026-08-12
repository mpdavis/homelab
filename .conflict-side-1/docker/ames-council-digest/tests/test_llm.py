"""Reading a model response back, and the retry loop around getting one."""

from __future__ import annotations

import json

import httpx
import pytest

from ames_digest.llm import LLMClient, LLMError, Usage, parse_json_object, text_field


class TestParseJsonObject:
    def test_plain_object(self):
        assert parse_json_object('{"a": 1}') == {"a": 1}

    def test_fenced(self):
        assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_language(self):
        assert parse_json_object('```\n{"a": 1}\n```') == {"a": 1}

    def test_fence_beats_braces_in_the_surrounding_prose(self):
        # The two strategies are not redundant. Brace-scanning alone would span
        # from the prose's "{" to the object's "}" and produce garbage, so this
        # is the case that proves the fence is load-bearing.
        raw = 'Note {see the packet}:\n```json\n{"a": 1}\n```\nHope that helps.'
        assert parse_json_object(raw) == {"a": 1}

    def test_fence_beats_trailing_prose_braces(self):
        raw = '```json\n{"a": 1}\n```\nLet me know if {anything} is unclear.'
        assert parse_json_object(raw) == {"a": 1}

    def test_chatty_preamble_and_tail(self):
        raw = 'Sure! Here you go:\n{"a": 1}\nLet me know if you need more.'
        assert parse_json_object(raw) == {"a": 1}

    def test_nested_braces_survive(self):
        assert parse_json_object('{"a": {"b": [1, 2]}}') == {"a": {"b": [1, 2]}}

    def test_no_object_at_all(self):
        with pytest.raises(ValueError, match="no JSON object"):
            parse_json_object("I could not do that.")

    def test_closing_brace_before_opening(self):
        with pytest.raises(ValueError, match="no JSON object"):
            parse_json_object("} nonsense {")

    def test_malformed_json_raises_decode_error(self):
        # Callers catch JSONDecodeError separately from ValueError's message,
        # so it must surface as itself rather than being swallowed.
        with pytest.raises(json.JSONDecodeError):
            parse_json_object('{"a": }')


class TestTextField:
    @pytest.mark.parametrize("value", [None, {"a": 1}, ["a"], True, False])
    def test_non_strings_become_empty(self, value):
        assert text_field(value) == ""

    @pytest.mark.parametrize("value", ["null", "None", "N/A", "n/a"])
    def test_stringified_absence_becomes_empty(self, value):
        assert text_field(value) == ""

    def test_whitespace_collapsed(self):
        assert text_field("  a   b\n\tc  ") == "a b c"

    def test_numbers_are_stringified(self):
        assert text_field(14) == "14"
        assert text_field(3.5) == "3.5"

    def test_limit_truncates(self):
        assert text_field("abcdefghij", 4) == "abcd"

    def test_limit_trims_whitespace_exposed_by_the_cut(self):
        assert text_field("ab cdefg", 3) == "ab"

    def test_no_limit_keeps_everything(self):
        assert text_field("a" * 500) == "a" * 500


class TestUsage:
    def test_add_accumulates(self):
        usage = Usage()
        usage.add({"usage": {"input_tokens": 10, "output_tokens": 3}})
        usage.add({"usage": {"input_tokens": 5, "output_tokens": 1}})
        assert (usage.input_tokens, usage.output_tokens, usage.calls) == (15, 4, 2)

    def test_cache_tokens_fold_into_input(self):
        usage = Usage()
        usage.add({
            "usage": {
                "input_tokens": 10,
                "output_tokens": 1,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
            }
        })
        # Cache reads still bill; reporting them apart from input would let the
        # footer understate what the page cost.
        assert usage.input_tokens == 130

    def test_missing_usage_block_still_counts_the_call(self):
        usage = Usage()
        usage.add({})
        assert (usage.input_tokens, usage.calls) == (0, 1)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeHTTP:
    """Stands in for httpx.Client, replaying a scripted sequence of responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def ok(text="hello"):
    return FakeResponse(payload={
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 7, "output_tokens": 2},
    })


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(LLMClient, "_sleep", staticmethod(lambda *a: None))


def client(*responses):
    http = FakeHTTP(*responses)
    return LLMClient(base_url="https://gw.test/v1/", api_key="k", _client=http), http


class TestComplete:
    def test_returns_concatenated_text_blocks(self):
        llm, _ = client(FakeResponse(payload={
            "content": [
                {"type": "text", "text": "one "},
                {"type": "thinking", "text": "ignored"},
                {"type": "text", "text": "two"},
            ],
            "usage": {},
        }))
        assert llm.complete(model="m", prompt="p", system="s") == "one two"

    def test_records_usage(self):
        llm, _ = client(ok())
        llm.complete(model="m", prompt="p", system="s")
        assert (llm.usage.input_tokens, llm.usage.calls) == (7, 1)

    def test_sends_both_auth_headers(self):
        # Gateways differ on which they read; each ignores the other.
        llm, http = client(ok())
        llm.complete(model="m", prompt="p", system="s")
        headers = http.requests[0]["headers"]
        assert headers["x-api-key"] == "k"
        assert headers["authorization"] == "Bearer k"

    def test_posts_to_the_messages_endpoint_with_a_normalized_base(self):
        llm, http = client(ok())
        llm.complete(model="m", prompt="p", system="s")
        assert http.requests[0]["url"] == "https://gw.test/v1/messages"

    def test_body_carries_the_prompt_and_knobs(self):
        llm, http = client(ok())
        llm.complete(model="m", prompt="p", system="s", max_tokens=99, temperature=0.7)
        body = http.requests[0]["json"]
        assert body["messages"] == [{"role": "user", "content": "p"}]
        assert (body["system"], body["max_tokens"], body["temperature"]) == ("s", 99, 0.7)

    def test_retries_a_retryable_status(self, no_sleep):
        llm, http = client(FakeResponse(429, text="slow down"), ok("after retry"))
        assert llm.complete(model="m", prompt="p", system="s") == "after retry"
        assert len(http.requests) == 2

    def test_retries_a_transport_error(self, no_sleep):
        llm, http = client(httpx.ConnectError("boom"), ok("recovered"))
        assert llm.complete(model="m", prompt="p", system="s") == "recovered"
        assert len(http.requests) == 2

    def test_non_retryable_status_fails_immediately(self, no_sleep):
        llm, http = client(FakeResponse(400, text="bad model"))
        with pytest.raises(LLMError, match="HTTP 400"):
            llm.complete(model="m", prompt="p", system="s")
        assert len(http.requests) == 1, "a 400 must not be retried"

    def test_gives_up_after_max_attempts(self, no_sleep):
        llm, http = client(*[FakeResponse(503, text="down")] * 3)
        llm.max_attempts = 3
        with pytest.raises(LLMError):
            llm.complete(model="m", prompt="p", system="s")
        assert len(http.requests) == 3

    def test_final_attempt_raises_rather_than_retrying(self, no_sleep):
        # On the last attempt a retryable status falls through to the raise, so
        # the error carries the gateway's own message instead of a generic one.
        llm, http = client(FakeResponse(429, text="rate limited"))
        llm.max_attempts = 1
        with pytest.raises(LLMError, match="rate limited"):
            llm.complete(model="m", prompt="p", system="s")
        assert len(http.requests) == 1
