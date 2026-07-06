import os

from bora.utils.shortcuts import write_lnk, write_platform_symlink


def test_write_platform_symlink_posix(tmp_path):
    target = "../Docs/report.pdf"
    link = str(tmp_path / "link")
    written = write_platform_symlink(target, link)
    assert written == link
    assert os.path.islink(link)
    assert os.readlink(link) == target


def test_write_lnk_produces_file_with_target(tmp_path):
    lnk = str(tmp_path / "link.lnk")
    write_lnk("../Docs/report.pdf", lnk)
    data = open(lnk, "rb").read()
    assert data[:4] == (76).to_bytes(4, "little")  # HeaderSize = 0x4C
    assert "report.pdf".encode("utf-16-le") in data  # relative path embedded (UTF-16LE)


def test_write_platform_symlink_falls_back_to_lnk(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("no symlink privilege")

    monkeypatch.setattr(os, "symlink", boom)
    link = str(tmp_path / "link")
    written = write_platform_symlink("../Docs/report.pdf", link)
    assert written == link + ".lnk"
    assert os.path.exists(link + ".lnk")
    assert not os.path.exists(link)
