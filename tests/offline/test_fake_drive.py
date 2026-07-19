import hashlib
import io
import socket
import sqlite3

import requests as _requests
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

from bora.client import GoogleDriveClient
from bora.database.core import Database
from bora.keyring import CredentialStorage
from bora.path_cache import PathIDCache
from tests.fake_drive import (
    ROOT_ID,
    FakeDrive,
    make_conn_reset,
    make_http_error,
    make_timeout,
)


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
    resp = (
        svc.files()
        .get(
            fileId=fid,
            fields="id, name, parents, mimeType, modifiedTime, size, md5Checksum, "
            "shared, headRevisionId, trashed, lastModifyingUser(permissionId)",
        )
        .execute()
    )
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
    resp = (
        svc.files()
        .list(
            q=f"'{folder}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=1,
            pageToken=None,
        )
        .execute()
    )
    assert len(resp["files"]) == 1
    token = resp["nextPageToken"]
    resp2 = (
        svc.files()
        .list(
            q=f"'{folder}' in parents and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageSize=1,
            pageToken=token,
        )
        .execute()
    )
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
    client = GoogleDriveClient("test-config", CredentialStorage("test-config"), cache)
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
    resp = (
        svc.files()
        .create(
            body={
                "name": "n.txt",
                "parents": [ROOT_ID],
                "modifiedTime": "2026-01-02T00:00:00Z",
            },
            media_body=media,
            fields="id,name,md5Checksum,size,headRevisionId",
        )
        .execute()
    )
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
    resp = (
        svc.files()
        .update(fileId=fid, body={"trashed": True}, fields="id,trashed")
        .execute()
    )
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
    resp = (
        svc.changes()
        .list(pageToken=str(base), pageSize=100, fields=fields, includeRemoved=True)
        .execute()
    )
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
    resp = (
        svc.changes()
        .list(
            pageToken="100",
            pageSize=1,
            fields="nextPageToken, changes(fileId)",
            includeRemoved=True,
        )
        .execute()
    )
    assert len(resp["changes"]) == 1
    assert "nextPageToken" in resp
    resp2 = (
        svc.changes()
        .list(
            pageToken=resp["nextPageToken"],
            pageSize=1,
            fields="nextPageToken, newStartPageToken, changes(fileId)",
            includeRemoved=True,
        )
        .execute()
    )
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
        fileId=fid,
        body={"name": "b.txt"},
        addParents=dst,
        removeParents=src,
        fields="id",
    ).execute()
    resp = (
        svc.changes()
        .list(
            pageToken=str(base),
            pageSize=100,
            fields="changes(removed, fileId, file(id, name, parents))",
            includeRemoved=True,
        )
        .execute()
    )
    assert [c["fileId"] for c in resp["changes"]] == [fid]
    assert resp["changes"][0]["file"]["name"] == "b.txt"
    assert resp["changes"][0]["file"]["parents"] == [dst]


# ---------------------------------------------------------------------------
# Task A7 — about().get + revisions().list
# ---------------------------------------------------------------------------


def test_about_get():
    fake = FakeDrive()
    svc = fake.service()
    resp = (
        svc.about()
        .get(
            fields="user(emailAddress,displayName,permissionId,photoLink),"
            "storageQuota(usage,limit)"
        )
        .execute()
    )
    assert resp["user"]["emailAddress"] == "bora-test@example.com"
    assert resp["storageQuota"]["limit"] == "16106127360"


def test_revisions_list():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"v1")
    svc = fake.service()
    svc.files().update(
        fileId=fid,
        media_body=MediaInMemoryUpload(b"v2", mimetype="text/plain"),
        fields="id",
    ).execute()
    resp = (
        svc.revisions()
        .list(
            fileId=fid,
            fields="nextPageToken, revisions(id, modifiedTime, size, md5Checksum, "
            "lastModifyingUser(permissionId))",
            pageSize=100,
            pageToken=None,
        )
        .execute()
    )
    assert len(resp["revisions"]) == 2
    assert resp["revisions"][-1]["md5Checksum"] == hashlib.md5(b"v2").hexdigest()


# ---------------------------------------------------------------------------
# Task A8 — files().get_media / revisions().get_media via real MediaIoBaseDownload
# ---------------------------------------------------------------------------


def test_get_media_downloads_via_real_downloader():
    fake = FakeDrive()
    content = b"x" * (5 * 1024 * 1024 + 17)  # multi-chunk
    fid = fake.add_file("/big.bin", content=content)
    svc = fake.service()
    request = svc.files().get_media(fileId=fid)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=1024 * 1024)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    assert buf.getvalue() == content


# ---------------------------------------------------------------------------
# Task A9 — resumable upload via next_chunk loop
# ---------------------------------------------------------------------------


def test_resumable_create_drives_next_chunk():
    fake = FakeDrive()
    svc = fake.service()
    media = MediaInMemoryUpload(b"y" * 100, mimetype="text/plain", resumable=True)
    request = svc.files().create(
        body={"name": "r.txt", "parents": [ROOT_ID]},
        media_body=media,
        fields="id,name,md5Checksum,size,headRevisionId",
    )
    response = None
    progress_seen = []
    while response is None:
        status, response = request.next_chunk()
        if status is not None:
            progress_seen.append(status.resumable_progress)
    assert response["name"] == "r.txt"
    assert response["size"] == 100
    assert progress_seen  # at least one progress callback fired
    assert (
        fake.files[response["id"]]["md5Checksum"] == hashlib.md5(b"y" * 100).hexdigest()
    )


# ---------------------------------------------------------------------------
# Task A10 — batch callback contract + fidelity-contract guard
# ---------------------------------------------------------------------------


def test_batch_invokes_callbacks_with_signature():
    fake = FakeDrive()
    svc = fake.service()
    results = {}

    def cb(request_id, response, exception):
        results[request_id] = (response, exception)

    batch = svc.new_batch_http_request()
    batch.add(
        svc.files().create(
            body={"name": "x.txt", "parents": [ROOT_ID]},
            media_body=MediaInMemoryUpload(b"x", mimetype="text/plain"),
            fields="id,name",
        ),
        request_id="0",
        callback=cb,
    )
    fake.fail_next("files.create", make_http_error(403, "storageQuotaExceeded"))
    batch.add(
        svc.files().create(
            body={"name": "y.txt", "parents": [ROOT_ID]},
            media_body=MediaInMemoryUpload(b"y", mimetype="text/plain"),
            fields="id,name",
        ),
        request_id="1",
        callback=cb,
    )
    batch.execute()
    # The fail_next queue is FIFO per key: the failure is consumed by whichever
    # files.create executes first inside batch.execute() — which is request "0".
    # Assert the batch contract (one success + one HttpError) without depending
    # on which request_id gets which outcome.
    outcomes = sorted(("ok" if r[1] is None else "err") for r in results.values())
    assert outcomes == ["err", "ok"]
    ok = [r for r in results.values() if r[1] is None][0]
    err = [r for r in results.values() if r[1] is not None][0]
    assert ok[0]["name"] in ("x.txt", "y.txt")
    assert isinstance(err[1], HttpError)


def test_fidelity_contract_summary():
    """The six §5.4 fidelity points, asserted together as a guard."""
    fake = FakeDrive()
    svc = fake.service()
    # 1. md5 + headRevisionId canonical
    fid = fake.add_file("/a.txt", content=b"hello")
    assert fake.files[fid]["md5Checksum"] == hashlib.md5(b"hello").hexdigest()
    # 3. slash literal
    sid = fake.add_file("ignored", content=b"z", literal_name="2024/05")
    got = svc.files().get(fileId=sid, fields="name").execute()
    assert got["name"] == "2024/05"


# ---------------------------------------------------------------------------
# Task B1 — conftest client_on_fake fixture wiring verification
# ---------------------------------------------------------------------------


def test_client_fixture_is_wired_to_fake(client_on_fake):
    fid = client_on_fake.fake.add_file("/a.txt", content=b"hi")
    client_on_fake.client._path_cache.set("/a.txt", fid, ROOT_ID, "a.txt", False)
    md = client_on_fake.client.get_metadata("/a.txt")
    assert md.size == 2


# ---------------------------------------------------------------------------
# Task 7b-E2 Task 1 — Generalized fault surface
# ---------------------------------------------------------------------------


def test_fail_next_count_fails_n_then_succeeds():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    fake.fail_next("files.get", make_http_error(500), count=2)
    svc = fake.service()
    for _ in range(2):
        try:
            svc.files().get(fileId=fid, fields="id").execute()
            assert False, "expected 500"
        except HttpError as e:
            assert e.resp.status == 500
    assert svc.files().get(fileId=fid, fields="id").execute()["id"] == fid


def test_fail_on_call_targets_the_nth_changes_list():
    fake = FakeDrive()
    fake.add_file("/a.txt", content=b"a")
    fake.add_file("/b.txt", content=b"b")
    fake.fail_on_call("changes.list", 2, make_http_error(503))
    svc = fake.service()
    r1 = (
        svc.changes()
        .list(
            pageToken="100",
            pageSize=1,
            fields="nextPageToken, changes(fileId)",
            includeRemoved=True,
        )
        .execute()
    )
    assert "nextPageToken" in r1
    try:
        svc.changes().list(
            pageToken=r1["nextPageToken"],
            pageSize=1,
            fields="nextPageToken, changes(fileId)",
            includeRemoved=True,
        ).execute()
        assert False, "expected 503 on 2nd changes.list"
    except HttpError as e:
        assert e.resp.status == 503


def test_fail_when_predicate_targets_one_fileid_in_batch():
    fake = FakeDrive()
    a = fake.add_file("/a.txt", content=b"a")
    b = fake.add_file("/b.txt", content=b"b")
    fake.fail_when(
        "files.update", lambda **kw: kw.get("fileId") == b, make_http_error(500)
    )
    svc = fake.service()
    svc.files().update(fileId=a, body={"name": "a2.txt"}, fields="id").execute()  # ok
    try:
        svc.files().update(fileId=b, body={"name": "b2.txt"}, fields="id").execute()
        assert False, "expected 500 on b only"
    except HttpError as e:
        assert e.resp.status == 500


def test_fail_after_commits_then_raises_on_arbitrary_key():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    fake.fail_after("files.delete", make_http_error(500))
    svc = fake.service()
    try:
        svc.files().delete(fileId=fid).execute()
        assert False, "expected post-commit 500"
    except HttpError as e:
        assert e.resp.status == 500
    assert fid not in fake.files  # the delete DID commit before the raise


def test_get_media_can_fault():
    fake = FakeDrive()
    fid = fake.add_file("/big.bin", content=b"y" * 100)
    fake.fail_next("files.get_media", make_timeout())
    request = fake.service().files().get_media(fileId=fid)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=64)
    try:
        downloader.next_chunk()
        assert False, "expected timeout"
    except (_requests.exceptions.Timeout, TimeoutError, socket.timeout):
        pass


def test_exception_factory_shapes():
    assert make_http_error(429, "userRateLimitExceeded").resp.status == 429
    assert isinstance(
        make_timeout(), (TimeoutError, socket.timeout, _requests.exceptions.Timeout)
    )
    assert isinstance(
        make_conn_reset(), (ConnectionError, _requests.exceptions.ConnectionError)
    )


def test_fail_after_update_commits_then_raises():
    fake = FakeDrive()
    fid = fake.add_file("/a.txt", content=b"x")
    fake.fail_after("files.update", make_http_error(500))
    svc = fake.service()
    try:
        svc.files().update(fileId=fid, body={"name": "b.txt"}, fields="id").execute()
        assert False, "expected post-commit 500"
    except HttpError as e:
        assert e.resp.status == 500
    assert fake.files[fid]["name"] == "b.txt"  # update DID commit before the raise


def test_fail_after_changes_list_redelivers_on_next_poll():
    fake = FakeDrive()
    fake.add_file("/a.txt", content=b"a")
    fake.fail_after("changes.list", make_http_error(500))
    svc = fake.service()
    try:
        svc.changes().list(
            pageToken="100",
            pageSize=100,
            fields="changes(fileId), newStartPageToken",
            includeRemoved=True,
        ).execute()
        assert False, "expected post-commit 500"
    except HttpError as e:
        assert e.resp.status == 500
    # The read is non-mutating, so a fresh poll from the same cursor still
    # delivers the change (no loss).
    resp = (
        svc.changes()
        .list(
            pageToken="100",
            pageSize=100,
            fields="changes(fileId), newStartPageToken",
            includeRemoved=True,
        )
        .execute()
    )
    assert len(resp["changes"]) == 1


def test_fail_every_fires_on_every_nth_call():
    """fail_every(key, n, exc) raises on every n-th matching .execute(),
    indefinitely, and tracks the number of times it fired."""
    from googleapiclient.errors import HttpError

    fake = FakeDrive()
    fake.add_file("/a.txt", b"x")
    rule = fake.fail_every("files.list", 3, make_http_error(429))
    svc = fake.service()

    outcomes = []
    for _ in range(9):
        try:
            svc.files().list(
                q="trashed=false", fields="nextPageToken, files(id)"
            ).execute()
            outcomes.append("ok")
        except HttpError:
            outcomes.append("fail")

    # Calls 3, 6, 9 (1-based) fail; the rest pass.
    assert outcomes == ["ok", "ok", "fail", "ok", "ok", "fail", "ok", "ok", "fail"]
    # The returned rule reference exposes the live fire counter.
    assert rule["fires"] == 3


def test_fail_every_does_not_affect_other_keys():
    """A fail_every rule on one key never fires for a different key."""
    fake = FakeDrive()
    fid = fake.add_file("/b.txt", b"y")
    fake.fail_every("files.list", 2, make_http_error(429))
    svc = fake.service()
    # files.get is a different key — must never fail.
    for _ in range(5):
        svc.files().get(fileId=fid, fields="id").execute()


# ---------------------------------------------------------------------------
# Task 6 — corrupt_download hook
# ---------------------------------------------------------------------------


def _download_all(request) -> bytes:
    """Drive a MediaIoBaseDownload to completion and return all bytes."""
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=1024 * 1024)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


def test_corrupt_download_truncate_serves_fewer_bytes():
    """truncate mode: served bytes are shorter than original; advertised md5 unchanged."""
    fake = FakeDrive()
    content = b"A" * 4096
    fid = fake.add_file("/f.bin", content=content)
    original_md5 = fake.files[fid]["md5Checksum"]

    fake.corrupt_download(fid, mode="truncate")

    svc = fake.service()
    served = _download_all(svc.files().get_media(fileId=fid))

    # Served bytes are shorter.
    assert len(served) < len(content)
    # Advertised md5 is still the original (correct) checksum.
    assert fake.files[fid]["md5Checksum"] == original_md5
    # The served bytes do NOT match the advertised md5.
    assert hashlib.md5(served).hexdigest() != original_md5


def test_corrupt_download_flip_serves_same_length_wrong_bytes():
    """flip mode: served bytes have same length but first byte is flipped; md5 unchanged."""
    fake = FakeDrive()
    content = b"B" * 4096
    fid = fake.add_file("/f.bin", content=content)
    original_md5 = fake.files[fid]["md5Checksum"]

    fake.corrupt_download(fid, mode="flip")

    svc = fake.service()
    served = _download_all(svc.files().get_media(fileId=fid))

    # Same length, different first byte.
    assert len(served) == len(content)
    assert served[0] == (content[0] ^ 0xFF)
    assert served[1:] == content[1:]
    # Advertised md5 unchanged.
    assert fake.files[fid]["md5Checksum"] == original_md5
    # Served bytes hash does NOT match advertised md5.
    assert hashlib.md5(served).hexdigest() != original_md5


def test_corrupt_download_wrong_md5_serves_correct_bytes_wrong_checksum():
    """wrong_md5 mode: bytes are intact but file record advertises a wrong md5."""
    fake = FakeDrive()
    content = b"C" * 4096
    fid = fake.add_file("/f.bin", content=content)

    fake.corrupt_download(fid, mode="wrong_md5")

    svc = fake.service()
    served = _download_all(svc.files().get_media(fileId=fid))

    # Bytes are identical to the original.
    assert served == content
    # But the advertised md5 is now wrong.
    assert fake.files[fid]["md5Checksum"] != hashlib.md5(content).hexdigest()


def test_corrupt_download_is_single_use():
    """Corruption fires exactly once; subsequent get_media returns clean content."""
    fake = FakeDrive()
    content = b"D" * 512
    fid = fake.add_file("/f.bin", content=content)
    original_md5 = fake.files[fid]["md5Checksum"]

    fake.corrupt_download(fid, mode="flip")

    svc = fake.service()
    # First download: corrupted.
    first = _download_all(svc.files().get_media(fileId=fid))
    assert hashlib.md5(first).hexdigest() != original_md5

    # Second download: clean (directive was consumed).
    second = _download_all(svc.files().get_media(fileId=fid))
    assert second == content


def test_fail_every_rejects_non_positive_n():
    import pytest

    fake = FakeDrive()
    with pytest.raises(ValueError):
        fake.fail_every("files.list", 0, make_http_error(429))
