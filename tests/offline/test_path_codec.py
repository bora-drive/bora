"""Unit tests for cumulus.path_codec."""

from __future__ import annotations

import pytest

from cumulus.path_codec import (
    compose_path,
    decode_name,
    encode_name,
    fold_key,
    fold_path,
)


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


class TestFoldKey:
    """Unicode case-fold + NFC normalization."""

    def test_ascii_case_folds(self) -> None:
        assert fold_key("Documents") == fold_key("documents") == "documents"

    def test_german_sharp_s_folds_to_ss(self) -> None:
        # casefold('ß') == 'ss'; both inputs collapse.
        assert fold_key("Straße") == fold_key("STRASSE") == "strasse"

    def test_greek_final_sigma_collapses(self) -> None:
        # 'ς' (final sigma) and 'σ' (medial sigma) both casefold to 'σ'.
        assert fold_key("Σοφος") == fold_key("σοφος") == fold_key("σοφος")

    def test_turkish_dotted_i_folds_consistently(self) -> None:
        # 'İ' (U+0130) casefolds to 'i' + COMBINING DOT ABOVE (U+0307).
        # The combining dot is UTF-8 0xCC 0x87 → encodes to %CC%87.
        assert fold_key("İstanbul") == "i%CC%87stanbul"
        # Equivalent input (already-decomposed form) maps to the same key.
        assert fold_key("i̇stanbul") == "i%CC%87stanbul"

    def test_double_nfc_observable(self) -> None:
        # Decomposed input: 'é' as 'e' + combining acute (U+0301).
        decomposed = "café"
        composed = "café"
        # casefold(x) without leading NFC differs by composition order;
        # fold_key normalizes both to the same canonical form.
        assert fold_key(decomposed) == fold_key(composed)

    def test_url_encoded_output(self) -> None:
        # Output is URL-encoded so it's a stable byte string for indexing.
        assert fold_key("Café") == "caf%C3%A9"

    def test_empty_string(self) -> None:
        assert fold_key("") == ""

    def test_encode_set_chars_in_input(self) -> None:
        # fold_key encodes the encode-set the same way encode_name does.
        # ASCII letters and the encoded form are case-stable.
        assert fold_key("a/b") == "a%2Fb"
        assert fold_key("A/B") == "a%2Fb"


class TestComposePath:
    def test_root_parent(self) -> None:
        assert compose_path("/", "report.pdf") == "/report.pdf"

    def test_nested_parent(self) -> None:
        assert (
            compose_path("/Documents", "report.pdf") == "/Documents/report.pdf"
        )

    def test_parent_ending_in_slash_normalized(self) -> None:
        # Caller may pass "/" or "/Documents"; both yield correct output.
        assert (
            compose_path("/Documents/", "report.pdf") == "/Documents/report.pdf"
        )

    def test_raw_name_with_slash_encoded(self) -> None:
        # The Q4 spike case.
        assert (
            compose_path("/Documents", "2024/05 report.pdf")
            == "/Documents/2024%2F05 report.pdf"
        )

    def test_raw_name_with_non_ascii(self) -> None:
        assert compose_path("/", "café.txt") == "/caf%C3%A9.txt"

    def test_raw_name_empty_string(self) -> None:
        # Defensive: empty leaf returns parent + "/" + "".
        assert compose_path("/Documents", "") == "/Documents/"

    def test_parent_already_encoded_passes_through(self) -> None:
        # Parent path is already encoded; compose_path doesn't re-encode it.
        assert (
            compose_path("/2024%2F05 reports", "Q1.pdf")
            == "/2024%2F05 reports/Q1.pdf"
        )


class TestFoldPath:
    def test_root_only(self) -> None:
        assert fold_path("/") == "/"

    def test_simple_path(self) -> None:
        assert fold_path("/Documents/Report.pdf") == "/documents/report.pdf"

    def test_path_with_encoded_segment(self) -> None:
        # The encoded segment %2F is decoded, folded, re-encoded.
        # "2024%2F05" → raw "2024/05" → fold → "2024%2F05" (digits unchanged).
        assert (
            fold_path("/Documents/2024%2F05 report.pdf")
            == "/documents/2024%2F05 report.pdf"
        )

    def test_path_with_non_ascii(self) -> None:
        assert fold_path("/Caf%C3%A9/index.txt") == "/caf%C3%A9/index.txt"

    def test_mixed_case_segments(self) -> None:
        assert fold_path("/FOO/Bar/BAZ") == "/foo/bar/baz"
