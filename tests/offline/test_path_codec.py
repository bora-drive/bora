"""Unit tests for cumulus.path_codec."""

from __future__ import annotations

import pytest

from cumulus.path_codec import decode_name, encode_name


class TestEncodeName:
    """Per-character encoding behavior."""

    def test_unreserved_chars_pass_through(self) -> None:
        # RFC 3986 unreserved set: alphanumerics + _.-~
        assert encode_name("simple_name-1.txt~") == "simple_name-1.txt~"

    @pytest.mark.parametrize(
        "raw,encoded",
        [
            ("a/b", "a%2Fb"),
            ("100%", "100%25"),
            ("foo*", "foo%2A"),
            ("a?b", "a%3Fb"),
            ('a"b', "a%22b"),
            ("a<b", "a%3Cb"),
            ("a>b", "a%3Eb"),
            ("a|b", "a%7Cb"),
            ("a\x00b", "a%00b"),
            ("a\x01b", "a%01b"),
            ("a\x1fb", "a%1Fb"),
        ],
    )
    def test_encode_set_chars(self, raw: str, encoded: str) -> None:
        assert encode_name(raw) == encoded

    def test_trailing_space_encoded(self) -> None:
        assert encode_name("foo ") == "foo%20"

    def test_trailing_period_encoded(self) -> None:
        assert encode_name("foo.") == "foo%2E"

    def test_internal_space_passes_through(self) -> None:
        # Only trailing space matters; internal spaces are FS-safe.
        assert encode_name("foo bar") == "foo bar"

    def test_internal_period_passes_through(self) -> None:
        assert encode_name("foo.bar.txt") == "foo.bar.txt"

    def test_uppercase_hex(self) -> None:
        # RFC 3986 canonical form is uppercase hex.
        assert encode_name("/") == "%2F"
        assert encode_name("/").lower() != encode_name("/")  # not "%2f"

    def test_non_ascii_unicode(self) -> None:
        # UTF-8 of "é" is C3 A9; uppercase hex.
        assert encode_name("café") == "caf%C3%A9"

    def test_empty_string(self) -> None:
        assert encode_name("") == ""


class TestDecodeName:
    def test_inverse_of_encode_for_random_inputs(self) -> None:
        cases = [
            "simple",
            "a/b",
            "100%",
            "foo ",
            "foo.",
            "café",
            "weird*?\"<>|",
            "with\x00nul",
            "",
            "已编码",
        ]
        for raw in cases:
            assert decode_name(encode_name(raw)) == raw, raw


class TestEncodeIdempotency:
    """encode_name is NOT idempotent on already-encoded forms.

    The spec contract: a Drive name literally `'%2F'` must round-trip back
    to `'%2F'`, so it must encode to `'%252F'`. encode(encode(x)) is
    therefore double-encoded — that's correct, not a bug.
    """

    def test_already_encoded_double_encodes(self) -> None:
        # encode("%2F"): '%' → '%25', then "2F" passes through → "%252F".
        assert encode_name("%2F") == "%252F"
        # The single decode of the double-encoded form gets back the original.
        assert decode_name("%252F") == "%2F"
