from __future__ import annotations

from dataclasses import fields

from bora.core import Account, FullAccount


def test_file_metadata_has_single_modified_field():
    from datetime import datetime, timezone

    from bora.core import FileMetadata

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


def test_shared_link_metadata_field_set():
    """Pin the SharedLinkMetadata surface produced by the sharing client
    (Phase 5h): url/name/path_lower/expires/link_permissions."""
    from bora.core import LinkPermissions, ListSharedLinkResult, SharedLinkMetadata

    assert {f.name for f in fields(SharedLinkMetadata)} == {
        "url",
        "name",
        "path_lower",
        "expires",
        "link_permissions",
    }
    assert {f.name for f in fields(LinkPermissions)} == {
        "can_revoke",
        "allow_download",
        "effective_audience",
        "link_access_level",
        "require_password",
    }
    assert {f.name for f in fields(ListSharedLinkResult)} == {
        "entries",
        "has_more",
        "cursor",
    }


def test_shared_link_metadata_drive_defaults_construct():
    """A Drive-flavored link (no expiry, no password, public reader) is
    representable without any Dropbox-only state."""
    from bora.core import (
        LinkAccessLevel,
        LinkAudience,
        LinkPermissions,
        SharedLinkMetadata,
    )

    md = SharedLinkMetadata(
        url="https://drive.google.com/file/d/file-1/view",
        name="f.txt",
        path_lower="/f.txt",
        expires=None,
        link_permissions=LinkPermissions(
            can_revoke=True,
            allow_download=True,
            effective_audience=LinkAudience.Public,
            link_access_level=LinkAccessLevel.Viewer,
            require_password=False,
        ),
    )
    assert md.expires is None
    assert md.link_permissions.require_password is False
