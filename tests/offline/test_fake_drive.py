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
