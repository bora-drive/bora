import hashlib
import sqlite3

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from cumulus.client import GoogleDriveClient
from cumulus.database.core import Database
from cumulus.keyring import CredentialStorage
from cumulus.path_cache import PathIDCache
from tests.fake_drive import ROOT_ID, FakeDrive, make_http_error


def test_seed_root_and_file():
    fake = FakeDrive()
    assert fake.files[ROOT_ID]["mimeType"] == "application/vnd.google-apps.folder"

    folder_id = fake.add_folder("/docs")
    file_id = fake.add_file("/docs/a.txt", content=b"hello")

    folder = fake.files[folder_id]
    rec = fake.files[file_id]
    assert folder["parents"] == [ROOT_ID]
    assert folder["mimeType"] == "application/vnd.google-apps.folder"
    assert rec["parents"] == [folder_id]
    assert rec["name"] == "a.txt"
    assert rec["md5Checksum"] == hashlib.md5(b"hello").hexdigest()
    assert rec["size"] == 5
    assert rec["trashed"] is False
    assert rec["headRevisionId"]  # non-empty
    assert rec["content"] == b"hello"


def test_slash_in_name_is_literal():
    fake = FakeDrive()
    fid = fake.add_file("2024-05 report.pdf", content=b"x", literal_name="a/b")
    assert fake.files[fid]["name"] == "a/b"


def test_get_returns_file_fields():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"hello")
    svc = fake.service()
    resp = svc.files().get(
        fileId=fid,
        fields="id, name, parents, mimeType, modifiedTime, size, md5Checksum, "
        "shared, headRevisionId, trashed, lastModifyingUser(permissionId)",
    ).execute()
    assert resp["id"] == fid
    assert resp["name"] == "a.txt"
    assert resp["md5Checksum"] == hashlib.md5(b"hello").hexdigest()
    assert resp["headRevisionId"]
    assert resp["parents"] == [ROOT_ID]
    assert resp["trashed"] is False  # False booleans survive projection


def test_list_children_filters_trashed_and_paginates():
    fake = FakeDrive()
    folder = fake.add_folder("/d")
    fake.add_file("/d/a.txt", content=b"a")
    fake.add_file("/d/b.txt", content=b"b")
    svc = fake.service()
    resp = svc.files().list(
        q=f"'{folder}' in parents and trashed=false",
        fields="nextPageToken, files(id, name)",
        pageSize=1,
        pageToken=None,
    ).execute()
    assert len(resp["files"]) == 1
    token = resp["nextPageToken"]
    resp2 = svc.files().list(
        q=f"'{folder}' in parents and trashed=false",
        fields="nextPageToken, files(id, name)",
        pageSize=1,
        pageToken=token,
    ).execute()
    assert len(resp2["files"]) == 1
    assert "nextPageToken" not in resp2
    names = {resp["files"][0]["name"], resp2["files"][0]["name"]}
    assert names == {"a.txt", "b.txt"}


# ---------------------------------------------------------------------------
# Task A3 — real GoogleDriveClient runs on FakeDrive
# ---------------------------------------------------------------------------


def _client_on(fake: FakeDrive) -> GoogleDriveClient:
    """Build a GoogleDriveClient wired to *fake* without any real credentials.

    ``_get_service`` is monkey-patched to return ``fake.service()`` so the
    ``linked`` check and the Google OAuth stack are bypassed entirely.
    The path-cache root is pre-seeded so ``_ensure_root_id`` is a no-op.
    """
    cache = PathIDCache(Database(sqlite3.connect(":memory:")))
    cache.root_id = ROOT_ID
    client = GoogleDriveClient(
        "test-config", CredentialStorage("test-config"), cache
    )
    # Bypass the linked / OAuth check in _get_service by replacing it with
    # a lambda that returns the fake service directly.
    client._get_service = lambda: fake.service()  # type: ignore[method-assign]
    return client


def test_client_get_metadata_round_trip():
    """get_metadata on the real client returns correct FileMetadata from FakeDrive."""
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"hello")
    client = _client_on(fake)
    # Teach the path cache about the file so path_to_id("/a.txt") resolves.
    client._path_cache.set("/a.txt", fid, ROOT_ID, "a.txt", False)

    md = client.get_metadata("/a.txt")

    assert md is not None
    assert md.rev == fake.files[fid]["headRevisionId"]
    assert md.content_hash == fake.files[fid]["md5Checksum"]
    assert md.size == 5


# ---------------------------------------------------------------------------
# Task A4 — files().create / files().update
# ---------------------------------------------------------------------------


def test_create_simple_upload():
    fake = FakeDrive()
    svc = fake.service()
    media = MediaInMemoryUpload(b"data", mimetype="text/plain", resumable=False)
    resp = svc.files().create(
        body={"name": "n.txt", "parents": [ROOT_ID], "modifiedTime": "2026-01-02T00:00:00Z"},
        media_body=media,
        fields="id,name,md5Checksum,size,headRevisionId",
    ).execute()
    assert resp["name"] == "n.txt"
    assert resp["md5Checksum"] == hashlib.md5(b"data").hexdigest()
    assert resp["size"] == 4
    assert resp["headRevisionId"]
    assert fake.files[resp["id"]]["parents"] == [ROOT_ID]


def test_update_move_and_rename_single_entry():
    fake = FakeDrive()
    src = fake.add_folder("/src")
    dst = fake.add_folder("/dst")
    fid = fake.add_file("/src/a.txt", content=b"x")
    svc = fake.service()
    svc.files().update(
        fileId=fid,
        body={"name": "b.txt"},
        addParents=dst,
        removeParents=src,
        fields="id,name,headRevisionId",
    ).execute()
    rec = fake.files[fid]
    assert rec["name"] == "b.txt"
    assert rec["parents"] == [dst]
    # changes-feed assertion added in A6


def test_update_trash_sets_flag():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    svc = fake.service()
    resp = svc.files().update(
        fileId=fid, body={"trashed": True}, fields="id,trashed"
    ).execute()
    assert resp["trashed"] is True
    assert fake.files[fid]["trashed"] is True


def test_update_precondition_412():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    svc = fake.service()
    fake.fail_next("files.update", make_http_error(412, "conditionNotMet"))
    try:
        svc.files().update(fileId=fid, body={"name": "z"}, fields="id").execute()
        assert False, "expected 412"
    except HttpError as e:
        assert e.resp.status == 412


# ---------------------------------------------------------------------------
# Task A5 — files().delete (hard-delete) + files().generateIds
# ---------------------------------------------------------------------------


def test_delete_hard_removes_record():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    svc = fake.service()
    svc.files().delete(fileId=fid).execute()
    assert fid not in fake.files
    # a change was recorded so A6's changes.list will surface removed=true
    assert any(f == fid for _seq, f in fake._change_log[-1:])


def test_generate_ids():
    fake = FakeDrive()
    svc = fake.service()
    resp = svc.files().generateIds(count=3).execute()
    assert len(resp["ids"]) == 3
    assert len(set(resp["ids"])) == 3


# ---------------------------------------------------------------------------
# Task A6 — changes().getStartPageToken / changes().list
# ---------------------------------------------------------------------------


def test_changes_removed_reason_semantics():
    fake = FakeDrive()
    trashed = fake.add_file("/t.txt", content=b"a")
    hard = fake.add_file("/h.txt", content=b"b")
    base = fake._seq
    svc = fake.service()
    svc.files().update(fileId=trashed, body={"trashed": True}, fields="id").execute()
    svc.files().delete(fileId=hard).execute()
    fields = "nextPageToken, newStartPageToken, changes(removed, fileId, file(id, name, trashed))"
    resp = svc.changes().list(
        pageToken=str(base), pageSize=100, fields=fields, includeRemoved=True
    ).execute()
    by_id = {c["fileId"]: c for c in resp["changes"]}
    assert by_id[trashed]["removed"] is False
    assert by_id[trashed]["file"]["trashed"] is True
    assert by_id[hard]["removed"] is True
    assert "file" not in by_id[hard]
    assert "nextPageToken" not in resp
    assert resp["newStartPageToken"] == str(fake._seq)


def test_changes_pagination():
    fake = FakeDrive()
    fake.add_file("/a.txt", content=b"a")
    fake.add_file("/b.txt", content=b"b")
    svc = fake.service()
    resp = svc.changes().list(
        pageToken="100", pageSize=1, fields="nextPageToken, changes(fileId)",
        includeRemoved=True,
    ).execute()
    assert len(resp["changes"]) == 1
    assert "nextPageToken" in resp
    resp2 = svc.changes().list(
        pageToken=resp["nextPageToken"], pageSize=1,
        fields="nextPageToken, newStartPageToken, changes(fileId)", includeRemoved=True,
    ).execute()
    assert len(resp2["changes"]) == 1
    assert "newStartPageToken" in resp2


def test_get_start_page_token():
    fake = FakeDrive()
    svc = fake.service()
    resp = svc.changes().getStartPageToken().execute()
    assert resp["startPageToken"] == str(fake._seq)


def test_changes_move_rename_single_entry():
    """Fidelity point 4: a combined move+rename is ONE change entry."""
    fake = FakeDrive()
    src = fake.add_folder("/src")
    dst = fake.add_folder("/dst")
    fid = fake.add_file("/src/a.txt", content=b"x")
    base = fake._seq
    svc = fake.service()
    svc.files().update(
        fileId=fid, body={"name": "b.txt"}, addParents=dst, removeParents=src,
        fields="id",
    ).execute()
    resp = svc.changes().list(
        pageToken=str(base), pageSize=100,
        fields="changes(removed, fileId, file(id, name, parents))", includeRemoved=True,
    ).execute()
    assert [c["fileId"] for c in resp["changes"]] == [fid]
    assert resp["changes"][0]["file"]["name"] == "b.txt"
    assert resp["changes"][0]["file"]["parents"] == [dst]
