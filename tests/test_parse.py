"""firecrawl_api.parse 纯协议层测试。"""

from __future__ import annotations

import pytest

from firecrawl_api.parse import (
    MAX_PARSE_FILE_BYTES,
    SUPPORTED_PARSE_EXTENSIONS,
    build_parse_options,
    estimate_parse_credits,
    normalize_parse_file,
    trim_parse_response,
)


def test_supported_extensions_full_set_and_case_insensitive():
    expected = frozenset(
        {
            ".html",
            ".htm",
            ".xhtml",
            ".pdf",
            ".docx",
            ".doc",
            ".docm",
            ".odt",
            ".ods",
            ".odp",
            ".rtf",
            ".xlsx",
            ".xls",
            ".xlsm",
            ".xlsb",
            ".pptx",
            ".ppt",
            ".pptm",
            ".epub",
            ".csv",
        }
    )
    assert SUPPORTED_PARSE_EXTENSIONS == expected
    for extension in sorted(expected):
        info = normalize_parse_file(f"report{extension}", 1)
        assert info == {
            "filename": f"report{extension}",
            "size": 1,
            "is_pdf": extension == ".pdf",
        }


def test_normalize_parse_file_trims_name_and_uppercase_extension():
    assert normalize_parse_file("  Report.PDF  ", 2) == {
        "filename": "Report.PDF",
        "size": 2,
        "is_pdf": True,
    }
    assert normalize_parse_file("a.DocX", 2)["is_pdf"] is False


def test_normalize_parse_file_rejects_bad_filenames():
    for filename in (
        "",
        "   ",
        ".",
        "..",
        "a/b.pdf",
        "a\\b.pdf",
        "a\x00b.pdf",
        5,
        None,
    ):
        with pytest.raises(ValueError):
            normalize_parse_file(filename, 1)


def test_normalize_parse_file_rejects_bad_sizes():
    for size in (-1, True, 1.5, "5", None):
        with pytest.raises(ValueError):
            normalize_parse_file("a.pdf", size)


def test_normalize_parse_file_rejects_unsupported_extensions():
    for filename in ("a.txt", "a.exe", "archive.tar.gz", "noext", ".hidden"):
        with pytest.raises(ValueError):
            normalize_parse_file(filename, 1)


def test_normalize_parse_file_exact_upload_limit_requires_local_beyond():
    info = normalize_parse_file("a.pdf", MAX_PARSE_FILE_BYTES)
    assert info == {
        "filename": "a.pdf",
        "size": MAX_PARSE_FILE_BYTES,
        "is_pdf": True,
    }
    for size in (MAX_PARSE_FILE_BYTES + 1, MAX_PARSE_FILE_BYTES * 2):
        with pytest.raises(ValueError, match="本地解析"):
            normalize_parse_file("a.pdf", size)
    with pytest.raises(ValueError, match="本地解析"):
        normalize_parse_file("a.docx", MAX_PARSE_FILE_BYTES + 1)


def test_build_parse_options_default_shape():
    assert build_parse_options() == {
        "formats": ["markdown"],
        "parsers": [{"type": "pdf", "maxPages": 20}],
        "checkPromptInjection": False,
    }


def test_build_parse_options_custom_values():
    assert build_parse_options(pdf_max_pages=100, check_prompt_injection=True) == {
        "formats": ["markdown"],
        "parsers": [{"type": "pdf", "maxPages": 100}],
        "checkPromptInjection": True,
    }
    assert build_parse_options(pdf_max_pages=1)["parsers"][0]["maxPages"] == 1
    assert build_parse_options(pdf_max_pages=10000)["parsers"][0]["maxPages"] == 10000


def test_build_parse_options_reuses_pdf_and_bool_validation():
    for pdf_max_pages in (0, 10001, True, 1.5, "20"):
        with pytest.raises(ValueError):
            build_parse_options(pdf_max_pages=pdf_max_pages)
    for check_prompt_injection in ("yes", 1, None):
        with pytest.raises(ValueError):
            build_parse_options(check_prompt_injection=check_prompt_injection)


def test_estimate_parse_credits_pdf_bills_base_pages_and_injection():
    assert estimate_parse_credits(is_pdf=True) == 21
    assert estimate_parse_credits(is_pdf=True, pdf_max_pages=1) == 2
    assert estimate_parse_credits(is_pdf=True, pdf_max_pages=10000) == 10001
    assert (
        estimate_parse_credits(
            is_pdf=True, pdf_max_pages=10000, check_prompt_injection=True
        )
        == 10005
    )


def test_estimate_parse_credits_non_pdf_ignores_page_cap():
    assert estimate_parse_credits(is_pdf=False) == 1
    assert estimate_parse_credits(is_pdf=False, pdf_max_pages=10000) == 1
    assert (
        estimate_parse_credits(
            is_pdf=False, pdf_max_pages=100, check_prompt_injection=True
        )
        == 5
    )


def test_estimate_parse_credits_reuses_budget_validation():
    with pytest.raises(ValueError):
        estimate_parse_credits(is_pdf=1)
    with pytest.raises(ValueError):
        estimate_parse_credits(is_pdf=True, pdf_max_pages=0)
    with pytest.raises(ValueError):
        estimate_parse_credits(is_pdf=True, pdf_max_pages=10001)
    with pytest.raises(ValueError):
        estimate_parse_credits(is_pdf=True, check_prompt_injection="yes")


def test_trim_parse_response_passes_short_markdown_through():
    trimmed = trim_parse_response(
        {"data": {"markdown": "# hello"}}, max_content_chars=100
    )
    assert trimmed == {"content": "# hello"}


def test_trim_parse_response_truncates_and_marks():
    trimmed = trim_parse_response(
        {"data": {"markdown": "x" * 50}}, max_content_chars=10
    )
    assert trimmed == {"content": "x" * 10, "truncated": True}


def test_trim_parse_response_projects_valid_metadata():
    response = {
        "data": {
            "markdown": "body",
            "metadata": {
                "title": "Quarterly Report",
                "sourceFile": "report.pdf",
                "numPages": 4,
                "totalPages": 9,
                "sourceURL": "https://example.com",
            },
        },
        "warning": "partial parse",
    }
    trimmed = trim_parse_response(response, max_content_chars=10)
    assert trimmed == {
        "content": "body",
        "title": "Quarterly Report",
        "sourceFile": "report.pdf",
        "numPages": 4,
        "totalPages": 9,
        "pagesTruncated": True,
        "warning": "partial parse",
    }


def test_trim_parse_response_no_pages_truncated_when_equal():
    trimmed = trim_parse_response(
        {"data": {"markdown": "body", "metadata": {"numPages": 3, "totalPages": 3}}},
        max_content_chars=10,
    )
    assert "pagesTruncated" not in trimmed
    assert trimmed["numPages"] == 3
    assert trimmed["totalPages"] == 3


def test_trim_parse_response_skips_illegal_metadata_fields():
    response = {
        "data": {
            "markdown": "body",
            "metadata": {
                "title": 5,
                "sourceFile": None,
                "numPages": True,
                "totalPages": -1,
            },
        }
    }
    trimmed = trim_parse_response(response, max_content_chars=10)
    assert trimmed == {"content": "body"}


def test_trim_parse_response_handles_missing_or_null_metadata():
    trimmed = trim_parse_response(
        {"data": {"markdown": "body", "metadata": None}}, max_content_chars=10
    )
    assert trimmed == {"content": "body"}
    trimmed = trim_parse_response({"data": {"markdown": "body"}}, max_content_chars=10)
    assert trimmed == {"content": "body"}


def test_trim_parse_response_keeps_only_string_warning():
    trimmed = trim_parse_response(
        {"data": {"markdown": "body"}, "warning": "note"}, max_content_chars=10
    )
    assert trimmed["warning"] == "note"
    trimmed = trim_parse_response(
        {"data": {"markdown": "body"}, "warning": 7}, max_content_chars=10
    )
    assert "warning" not in trimmed


def test_trim_parse_response_rejects_bad_structure():
    bad_responses = (
        "not-a-dict",
        None,
        {},
        {"data": None},
        {"data": "text"},
        {"data": {"markdown": None}},
        {"data": {"markdown": 12345}},
        {"data": {"markdown": "body", "metadata": "not-a-dict"}},
    )
    for response in bad_responses:
        with pytest.raises(ValueError):
            trim_parse_response(response, max_content_chars=10)


def test_trim_parse_response_errors_do_not_echo_response_values():
    with pytest.raises(ValueError) as excinfo:
        trim_parse_response({"data": {"markdown": 987654321}}, max_content_chars=10)
    assert "987654321" not in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        trim_parse_response(
            {"data": {"markdown": "body", "metadata": "LEAK-VALUE"}},
            max_content_chars=10,
        )
    assert "LEAK-VALUE" not in str(excinfo.value)
    with pytest.raises(ValueError) as excinfo:
        trim_parse_response({"data": {"markdown": "SECRET-BODY"}}, max_content_chars=0)
    assert "SECRET-BODY" not in str(excinfo.value)


def test_trim_parse_response_validates_max_content_chars():
    for max_content_chars in (0, -1, True, 1.5, "10"):
        with pytest.raises(ValueError):
            trim_parse_response(
                {"data": {"markdown": "body"}}, max_content_chars=max_content_chars
            )
