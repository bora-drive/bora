import hashlib

from tests.fake_drive import ROOT_ID, FakeDrive


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
    assert resp["md5Checksum"] == __import__("hashlib").md5(b"hello").hexdigest()
    assert resp["headRevisionId"]
    assert resp["parents"] == [ROOT_ID]


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
