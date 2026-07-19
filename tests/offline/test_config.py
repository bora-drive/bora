"""Offline tests for bora's default configuration (bora.config.main)."""

import pytest

from bora.config import BoraConfig, remove_configuration

_CONFIG = "test-config-defaults"


@pytest.fixture
def conf():
    conf = BoraConfig(_CONFIG)
    yield conf
    remove_configuration(_CONFIG)


def test_export_defaults_present(conf) -> None:
    """Google Docs export is on by default (Docs export to Markdown by
    default); adding these keys must not require a CONF_VERSION bump since
    they are purely additive (config/main.py note 3)."""
    assert conf.get("sync", "export_google_docs") is True
    assert conf.get("sync", "export_google_docs_markdown") is True
