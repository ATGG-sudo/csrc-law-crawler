"""Shared machine-readable failure reason constants."""

from __future__ import annotations

from enum import Enum


class FailureReason(str, Enum):
    NETWORK_TIMEOUT = "network.timeout"
    NETWORK_BLOCKED = "network.blocked"
    NETWORK_REQUEST_EXCEPTION = "network.request_exception"
    HTTP_STATUS_ERROR = "http.status_error"
    CONTENT_EMPTY_RESPONSE = "content.empty_response"
    CONTENT_HTML_ERROR_PAGE = "content.html_error_page"
    CONTENT_TOO_LARGE = "content.too_large"
    PARSE_MISSING_BODY = "parse.missing_body"
    PARSE_SCHEMA_MISMATCH = "parse.schema_mismatch"
    STORAGE_WRITE_ERROR = "storage.write_error"
    VALIDATION_INCOMPLETE_OUTPUT = "validation.incomplete_output"
    UNCAUGHT_EXCEPTION = "uncaught_exception"
    NONZERO_EXIT = "nonzero_exit"
