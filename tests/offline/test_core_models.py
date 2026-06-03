from __future__ import annotations

from dataclasses import fields

from cumulus.core import Account, FullAccount


def test_file_metadata_has_single_modified_field():
    from datetime import datetime, timezone
    from cumulus.core import FileMetadata

    md = FileMetadata(
        name="f.txt",
        path_lower="/f.txt",
        path_display="/f.txt",
        id="ID1",
        modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        rev="REV1",
        size=3,
        symlink_target=None,
        shared=False,
        modified_by=None,
        is_downloadable=True,
        content_hash="abc",
    )
    assert md.modified.year == 2026
    assert not hasattr(md, "client_modified")
    assert not hasattr(md, "server_modified")


def test_account_field_set_matches_drive_about_response():
    field_names = {f.name for f in fields(Account)}
    assert field_names == {
        "account_id",
        "display_name",
        "email",
        "profile_photo_url",
        "disabled",
    }


def test_full_account_drops_dropbox_only_fields():
    field_names = {f.name for f in fields(FullAccount)}
    assert "country" not in field_names
    assert "locale" not in field_names
    assert "team" not in field_names
    assert "team_member_id" not in field_names
    assert "account_type" not in field_names
    assert "root_info" not in field_names
    # But the Account base fields stay:
    assert {
        "account_id",
        "display_name",
        "email",
        "profile_photo_url",
        "disabled",
    }.issubset(field_names)
