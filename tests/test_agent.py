"""firecrawl_api.agent 纯协议层测试。"""

from __future__ import annotations

import json

import pytest

from firecrawl_api.agent import (
    MAX_AGENT_CREDITS,
    MAX_AGENT_PROMPT_CHARS,
    MAX_AGENT_URLS,
    build_agent_payload,
    estimate_agent_credits,
    trim_agent_response,
)


def test_contract_limits():
    assert MAX_AGENT_PROMPT_CHARS == 10000
    assert MAX_AGENT_CREDITS == 2500
    assert MAX_AGENT_URLS == 100


@pytest.mark.parametrize("value", [1, 42, MAX_AGENT_CREDITS])
def test_estimate_agent_credits_passes_valid_range_through(value):
    assert estimate_agent_credits(value) == value
    assert type(estimate_agent_credits(value)) is int


@pytest.mark.parametrize("value", [0, -5, 2501, True, False, 1.5, "5", None])
def test_estimate_agent_credits_rejects_invalid(value):
    with pytest.raises(ValueError):
        estimate_agent_credits(value)


def test_build_agent_payload_minimal_exact():
    assert build_agent_payload(prompt="  帮我调研 X  ", max_credits=30) == {
        "prompt": "帮我调研 X",
        "maxCredits": 30,
    }


def test_prompt_length_boundary_after_strip():
    padded = "  " + "a" * MAX_AGENT_PROMPT_CHARS + "\n"
    assert build_agent_payload(prompt=padded, max_credits=1) == {
        "prompt": "a" * MAX_AGENT_PROMPT_CHARS,
        "maxCredits": 1,
    }
    with pytest.raises(ValueError):
        build_agent_payload(prompt="a" * (MAX_AGENT_PROMPT_CHARS + 1), max_credits=1)


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t ", None, 7])
def test_prompt_rejects_empty_or_non_string(prompt):
    with pytest.raises(ValueError):
        build_agent_payload(prompt=prompt, max_credits=1)


def test_urls_accept_tuple_dedupe_keep_order_and_trim():
    payload = build_agent_payload(
        prompt="p",
        max_credits=5,
        urls=(
            "  https://a.example.com/  ",
            "https://b.example.com/",
            "https://a.example.com/",
        ),
    )
    assert payload["urls"] == [
        "https://a.example.com/",
        "https://b.example.com/",
    ]


def test_urls_reject_single_string_and_bad_entries():
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, urls="https://example.com")
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, urls=["notaurl"])
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, urls=[7])


def test_urls_blocked_domain_rejected_without_echo():
    with pytest.raises(ValueError) as excinfo:
        build_agent_payload(
            prompt="p",
            max_credits=5,
            urls=["https://sub.evil.com/x"],
            blocked_domains=("evil.com",),
        )
    assert "sub.evil.com" not in str(excinfo.value)


def test_urls_count_limit_applies_after_dedupe():
    hundred = [f"https://host{i}.example.com/p" for i in range(MAX_AGENT_URLS)]
    payload = build_agent_payload(prompt="p", max_credits=5, urls=hundred)
    assert len(payload["urls"]) == MAX_AGENT_URLS

    duplicates = ["https://same.example.com/p"] * 150
    assert build_agent_payload(prompt="p", max_credits=5, urls=duplicates)["urls"] == [
        "https://same.example.com/p"
    ]

    with pytest.raises(ValueError):
        build_agent_payload(
            prompt="p",
            max_credits=5,
            urls=[f"https://host{i}.example.com/p" for i in range(MAX_AGENT_URLS + 1)],
        )


def test_strict_flag_projected_only_when_true_with_urls():
    urls = ["https://a.example.com/"]
    assert build_agent_payload(prompt="p", max_credits=5, urls=urls) == {
        "prompt": "p",
        "maxCredits": 5,
        "urls": urls,
    }
    payload = build_agent_payload(
        prompt="p", max_credits=5, urls=urls, strict_constrain_to_urls=True
    )
    assert payload["strictConstrainToURLs"] is True


@pytest.mark.parametrize("value", [1, 0, "yes", None])
def test_strict_flag_must_be_bool(value):
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, strict_constrain_to_urls=value)


def test_strict_true_requires_at_least_one_url():
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, strict_constrain_to_urls=True)
    with pytest.raises(ValueError):
        build_agent_payload(
            prompt="p", max_credits=5, urls=[], strict_constrain_to_urls=True
        )


def test_schema_omitted_when_none_and_canonical_copy():
    assert "schema" not in build_agent_payload(prompt="p", max_credits=5)
    schema = {"z": 1, "a": {"nested": [1, 2]}}
    payload = build_agent_payload(prompt="p", max_credits=5, schema=schema)
    assert payload["schema"] == schema
    schema["a"]["nested"].append(999)
    schema["new"] = True
    assert payload["schema"] == {"z": 1, "a": {"nested": [1, 2]}}


@pytest.mark.parametrize("schema", [[1], "obj", 3])
def test_schema_must_be_dict(schema):
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, schema=schema)


def test_schema_rejects_nan_and_non_json_values():
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, schema={"x": float("nan")})
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, schema={"x": float("inf")})
    with pytest.raises(ValueError):
        build_agent_payload(prompt="p", max_credits=5, schema={"x": {1, 2}})


def test_full_payload_exact_shape():
    payload = build_agent_payload(
        prompt="调研",
        max_credits=MAX_AGENT_CREDITS,
        urls=["https://docs.example.com/", "https://blog.example.com/"],
        schema={"type": "object"},
        strict_constrain_to_urls=True,
    )
    assert payload == {
        "prompt": "调研",
        "maxCredits": 2500,
        "urls": ["https://docs.example.com/", "https://blog.example.com/"],
        "schema": {"type": "object"},
        "strictConstrainToURLs": True,
    }


@pytest.mark.parametrize(
    "response",
    ["nope", None, [], {"status": "unknown"}, {}, {"status": 5}],
)
def test_trim_rejects_bad_envelope_without_echo(response):
    with pytest.raises(ValueError):
        trim_agent_response(response, max_content_chars=10)
    secret = "TOPSECRET-RESPONSE"
    with pytest.raises(ValueError) as excinfo:
        trim_agent_response(
            {"status": "paused", "secret": secret}, max_content_chars=10
        )
    assert secret not in str(excinfo.value)


@pytest.mark.parametrize("value", [0, -3, True, 2.5])
def test_trim_requires_valid_max_content_chars(value):
    with pytest.raises(ValueError):
        trim_agent_response({"status": "processing"}, max_content_chars=value)


@pytest.mark.parametrize(
    "response",
    [
        {"status": "processing"},
        {"status": "completed", "data": {}},
        {"status": "failed"},
        {"status": "cancelled"},
    ],
)
def test_trim_accepts_all_statuses_with_minimal_fields(response):
    trimmed = trim_agent_response(response, max_content_chars=10)
    expected = {"status": response["status"]}
    if "data" in response:
        expected["content"] = json.dumps(
            response["data"], ensure_ascii=False, sort_keys=True
        )
    assert trimmed == expected


def test_trim_projects_credits_used_as_is():
    trimmed = trim_agent_response(
        {"status": "completed", "creditsUsed": 0, "data": {}}, max_content_chars=100
    )
    assert trimmed["creditsUsed"] == 0
    trimmed = trim_agent_response(
        {"status": "completed", "creditsUsed": 2.5, "data": {}}, max_content_chars=100
    )
    assert trimmed["creditsUsed"] == 2.5
    trimmed = trim_agent_response(
        {"status": "completed", "creditsUsed": None, "data": {}}, max_content_chars=100
    )
    assert "creditsUsed" not in trimmed


@pytest.mark.parametrize("value", [-1, -0.5, float("nan"), float("inf"), "3", True])
def test_trim_rejects_invalid_credits_used(value):
    with pytest.raises(ValueError):
        trim_agent_response(
            {"status": "completed", "creditsUsed": value, "data": {}},
            max_content_chars=10,
        )


def test_trim_projects_string_fields_and_skips_null():
    trimmed = trim_agent_response(
        {
            "status": "completed",
            "data": {},
            "expiresAt": "2026-01-01T00:00:00Z",
            "message": "done",
            "model": "agent-1",
            "stopReason": "completed",
            "error": None,
        },
        max_content_chars=100,
    )
    assert trimmed["expiresAt"] == "2026-01-01T00:00:00Z"
    assert trimmed["message"] == "done"
    assert trimmed["model"] == "agent-1"
    assert trimmed["stopReason"] == "completed"
    assert "error" not in trimmed


@pytest.mark.parametrize(
    "name", ["expiresAt", "message", "model", "stopReason", "error"]
)
def test_trim_rejects_non_string_fields(name):
    secret = "TOPSECRET-VALUE"
    with pytest.raises(ValueError) as excinfo:
        trim_agent_response(
            {"status": "completed", name: 12345, "secret": secret, "data": {}},
            max_content_chars=10,
        )
    assert secret not in str(excinfo.value)


def test_trim_projects_partial_schema_valid_bool():
    trimmed = trim_agent_response(
        {"status": "failed", "partial": {}, "partialSchemaValid": True},
        max_content_chars=10,
    )
    assert trimmed["partialSchemaValid"] is True
    trimmed = trim_agent_response(
        {"status": "failed", "partial": {}, "partialSchemaValid": False},
        max_content_chars=10,
    )
    assert trimmed["partialSchemaValid"] is False
    with pytest.raises(ValueError):
        trim_agent_response(
            {"status": "failed", "partial": {}, "partialSchemaValid": 1},
            max_content_chars=10,
        )


def test_completed_requires_data_object():
    with pytest.raises(ValueError):
        trim_agent_response({"status": "completed"}, max_content_chars=10)
    with pytest.raises(ValueError):
        trim_agent_response(
            {"status": "completed", "data": "nope"}, max_content_chars=10
        )
    with pytest.raises(ValueError):
        trim_agent_response({"status": "completed", "data": [1]}, max_content_chars=10)


def test_processing_must_not_carry_data_or_partial():
    with pytest.raises(ValueError):
        trim_agent_response({"status": "processing", "data": {}}, max_content_chars=10)
    with pytest.raises(ValueError):
        trim_agent_response(
            {"status": "processing", "partial": {}}, max_content_chars=10
        )


def test_failed_and_cancelled_must_not_carry_data():
    with pytest.raises(ValueError):
        trim_agent_response({"status": "failed", "data": {}}, max_content_chars=10)
    with pytest.raises(ValueError):
        trim_agent_response({"status": "cancelled", "data": {}}, max_content_chars=10)


def test_data_and_partial_are_mutually_exclusive():
    with pytest.raises(ValueError):
        trim_agent_response(
            {"status": "completed", "data": {}, "partial": {}}, max_content_chars=10
        )


def test_failed_partial_must_be_object_and_cancelled_partial_rejected():
    with pytest.raises(ValueError):
        trim_agent_response({"status": "failed", "partial": "x"}, max_content_chars=10)
    with pytest.raises(ValueError):
        trim_agent_response(
            {"status": "cancelled", "partial": {}}, max_content_chars=10
        )


def test_completed_data_becomes_canonical_json_content():
    data = {"名": "值", "n": 1}
    trimmed = trim_agent_response(
        {"status": "completed", "data": data, "creditsUsed": 3},
        max_content_chars=1000,
    )
    assert trimmed["content"] == json.dumps(data, ensure_ascii=False, sort_keys=True)
    assert trimmed["creditsUsed"] == 3
    assert "truncated" not in trimmed
    assert "partial" not in trimmed
    assert "data" not in trimmed


def test_failed_partial_becomes_content_with_partial_marker():
    trimmed = trim_agent_response(
        {"status": "failed", "partial": {"b": 2, "a": 1}},
        max_content_chars=1000,
    )
    assert trimmed["content"] == '{"a": 1, "b": 2}'
    assert trimmed["partial"] is True
    assert "data" not in trimmed
    assert "truncated" not in trimmed


def test_content_truncation_boundary():
    data = {"k": "x" * 50}
    full = json.dumps(data, ensure_ascii=False, sort_keys=True)
    trimmed = trim_agent_response(
        {"status": "completed", "data": data}, max_content_chars=len(full)
    )
    assert trimmed["content"] == full
    assert "truncated" not in trimmed

    trimmed = trim_agent_response(
        {"status": "completed", "data": data}, max_content_chars=len(full) - 1
    )
    assert trimmed["content"] == full[: len(full) - 1]
    assert trimmed["truncated"] is True


def test_completed_data_with_nan_rejected():
    with pytest.raises(ValueError):
        trim_agent_response(
            {"status": "completed", "data": {"x": float("nan")}},
            max_content_chars=100,
        )


def test_trim_ignores_unsupported_extensions():
    trimmed = trim_agent_response(
        {
            "status": "processing",
            "threadId": "t",
            "exchange": [],
            "pendingApproval": {"a": 1},
            "suggestions": ["s"],
        },
        max_content_chars=10,
    )
    assert trimmed == {"status": "processing"}
