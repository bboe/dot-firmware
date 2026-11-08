from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import struct
import zlib
from typing import TYPE_CHECKING

import pytest

from firebreak import backup, twrp
from firebreak.devices import CROWN, RADAR
from firebreak.devices.radar import BUILDS
from firebreak.ui import FatalError

if TYPE_CHECKING:
    import pathlib

    from conftest import FakeAdb

ALTERNATE = 99
BOOT_AREA = bytes(range(256)) * 4
CID = "0123456789abcdef" * 2
IMAGE = b"I" * 2048
KB = b"K" * 2048
LAST_USABLE = 66
SYSTEM = IMAGE + b"T" * 2048
USERDATA = b"U" * 11 * 512


def entry(*, first: int, last: int, name: str) -> bytes:
    return (
        b"T" * 16
        + b"U" * 16
        + struct.pack("<QQ", first, last)
        + bytes(8)
        + name.encode(encoding="utf-16le").ljust(72, b"\0")
    )


def faked(
    *, adb: FakeAdb, monkeypatch: pytest.MonkeyPatch, pulls: dict[str, bytes]
) -> None:
    monkeypatch.setattr(name="twrp_entry", target=backup, value=lambda **_: None)
    monkeypatch.setattr(name="wait_for_twrp_start", target=backup, value=lambda: None)
    monkeypatch.setattr(name="known_device", target=backup, value=lambda: RADAR)
    monkeypatch.setattr(
        name="catalog",
        target=backup,
        value=lambda **_: [
            backup.Image(
                build="8149",
                image="system",
                sha256=hashlib.sha256(IMAGE).hexdigest(),
                size=len(IMAGE),
            ),
            backup.Image(build="8149", image="lk", sha256="0" * 64, size=1024),
        ],
    )
    primary = primary_table()
    adb.raws = [primary, primary, b"B" * 33 * 512]
    adb.answers["/proc/mounts"] = ["left:"]
    adb.answers["ro.serialno"] = [f"serial=G0SERIAL\ncid={CID}\n{backup.IDENTITY_OK}"]
    for node, data in pulls.items():
        adb.answers[f"--getsize64 {twrp.DISK}{node})"] = [
            (
                f"{len(data)} {hashlib.md5(data, usedforsecurity=False).hexdigest()}"
                f" {backup.READ_OK}"
            )
        ]


def primary_table() -> bytes:
    entries = bytearray(128 * 128)
    for index, (first, last, name) in enumerate((
        (40, 43, "kb"),
        (48, 55, "system_a"),
        (56, LAST_USABLE, "userdata"),
    )):
        entries[index * 128 : (index + 1) * 128] = entry(
            first=first, last=last, name=name
        )
    header = bytearray(92)
    header[:8] = b"EFI PART"
    header[12:16] = struct.pack("<I", 92)
    header[24:56] = struct.pack("<QQQQ", 1, ALTERNATE, 34, LAST_USABLE)
    header[72:80] = struct.pack("<Q", 2)
    header[80:88] = struct.pack("<II", 128, 128)
    header[88:92] = struct.pack("<I", zlib.crc32(entries))
    header[16:20] = struct.pack("<I", zlib.crc32(header))
    return b"M" * 512 + bytes(header).ljust(512, b"\0") + bytes(entries)


def pulled(*, adb: FakeAdb, data: dict[str, bytes], folder: pathlib.Path) -> None:
    for name, held in data.items():

        def write(*, held: bytes = held, name: str = name) -> str:
            (folder / f"{name}.img.part").write_bytes(data=held)
            return ""

        adb.answers[f"{name}.img.part"] = [write]


def restored(*, area: dict, folder: pathlib.Path) -> bytes:
    data = b""
    for extent in area["extents"]:
        if extent["kind"] == "copy":
            data += gzip.decompress(data=(folder / extent["file"]).read_bytes())
        else:
            data += IMAGE[: extent["length"]]
    return data


def test_areas_of_lists_a_gap_after_the_last_partition() -> None:
    raw = bytearray(primary_table())
    entries = raw[1024:]
    entries[2 * 128 : 3 * 128] = entry(first=56, last=60, name="userdata")
    header = bytearray(raw[512:604])
    header[88:92] = struct.pack("<I", zlib.crc32(entries))
    header[16:20] = bytes(4)
    header[16:20] = struct.pack("<I", zlib.crc32(header))
    raw[512:604] = header
    raw[1024:] = entries
    _, gaps = backup.areas_of(raw=bytes(raw))
    assert gaps[-1] == {"first": 61, "sectors": 6}


def test_areas_of_lists_every_area_and_gap() -> None:
    areas, gaps = backup.areas_of(raw=primary_table())
    assert [area.name for area in areas] == [
        "gpt-primary",
        "boot0",
        "boot1",
        "kb",
        "system_a",
        "userdata",
        "gpt-backup",
    ]
    assert areas[3] == backup.Area(
        first=40, name="kb", node=f"{twrp.DISK}p1", size=2048
    )
    assert areas[-1] == backup.Area(
        first=LAST_USABLE + 1, name="gpt-backup", node=twrp.DISK, size=33 * 512
    )
    assert gaps == [{"first": 34, "sectors": 6}, {"first": 44, "sectors": 4}]


def test_areas_of_refuses_a_broken_table() -> None:
    with pytest.raises(FatalError, match="does not pass its own checksums"):
        backup.areas_of(raw=b"M" * 512 + bytes(512) + bytes(16384))


def test_backup_names_an_ota_image_and_copies_the_rest(
    *,
    adb: FakeAdb,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    folder = tmp_path / "out"
    data = {
        "boot0": BOOT_AREA,
        "boot1": BOOT_AREA[::-1],
        "kb": KB,
        "system_a": SYSTEM,
        "userdata": USERDATA,
    }
    faked(
        adb=adb,
        monkeypatch=monkeypatch,
        pulls={
            "boot0": BOOT_AREA,
            "boot1": BOOT_AREA[::-1],
            "p1": KB,
            "p2": SYSTEM,
            "p3": USERDATA,
        },
    )
    pulled(adb=adb, data=data, folder=folder)
    monkeypatch.setattr(name="step", target=backup.PROGRESS, value=5)
    backup.backup(directory=str(folder), force=False)
    shown = capsys.readouterr().out
    assert "[ 1/ 7] record gpt-primary" in shown
    assert "[ 7/ 7] record gpt-backup" in shown
    manifest = json.loads((folder / backup.MANIFEST).read_text())
    assert manifest["device"] == "radar"
    assert manifest["serial"] == "G0SERIAL"
    assert manifest["cid"] == CID
    assert manifest["gaps"] == [
        {"first": 34, "sectors": 6},
        {"first": 44, "sectors": 4},
    ]
    areas = {area["name"]: area for area in manifest["areas"]}
    system = areas["system_a"]["extents"]
    assert system[0] == {
        "build": "8149",
        "image": "system",
        "kind": "ota",
        "length": len(IMAGE),
        "ota": {
            "name": BUILDS["8149"].name,
            "sha256": BUILDS["8149"].sha256,
            "url": BUILDS["8149"].url,
        },
        "sha256": hashlib.sha256(IMAGE).hexdigest(),
    }
    assert system[1] == {"file": "system_a.gz", "kind": "copy", "length": 2048}
    for name, held in {
        **data,
        "gpt-backup": b"B" * 33 * 512,
        "gpt-primary": primary_table(),
    }.items():
        assert restored(area=areas[name], folder=folder) == held
        assert areas[name]["sha256"] == hashlib.sha256(held).hexdigest()
    assert not list(folder.glob("*.part"))


def test_backup_names_its_directory_after_the_echo(
    *, adb: FakeAdb, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    faked(adb=adb, monkeypatch=monkeypatch, pulls={})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        name="unmount",
        target=backup,
        value=lambda: (_ for _ in ()).throw(FatalError(message="stop here")),
    )
    with pytest.raises(FatalError, match="stop here"):
        backup.backup(directory="", force=False)
    assert len(list(tmp_path.glob("radar-G0SERIAL-2*"))) == 1


def test_backup_refuses_a_directory_with_files(
    *, adb: FakeAdb, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    faked(adb=adb, monkeypatch=monkeypatch, pulls={})
    (tmp_path / "held").write_text(data="")
    monkeypatch.setattr(
        name="twrp_entry",
        target=backup,
        value=lambda **_: pytest.fail("reached TWRP before checking the directory"),
    )
    with pytest.raises(FatalError, match="already holds files"):
        backup.backup(directory=str(tmp_path), force=False)
    with pytest.raises(FatalError, match="is a file"):
        backup.backup(directory=str(tmp_path / "held"), force=False)


def test_build_of() -> None:
    assert backup.build_of(label="Fire OS 6 8149") == "8149"
    assert backup.build_of(label="Fire OS 5.5.5.4") == "5.5.5.4"
    assert backup.build_of(label="Fire OS 5.5.0.3-1973") == "5.5.0.3-1973"
    assert not backup.build_of(label="something else")


def test_catalog_keeps_only_builds_firebreak_downloads(
    *, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    images = backup.catalog(device=RADAR)
    assert images
    assert {image.build for image in images} == set(BUILDS)
    assert not backup.catalog(device=CROWN)
    monkeypatch.setattr(name="CATALOGS", target=backup, value=tmp_path)
    assert not backup.catalog(device=RADAR)


def test_checked_read_gives_up_after_its_tries(
    *, adb: FakeAdb, tmp_path: pathlib.Path
) -> None:
    area = backup.Area(name="kb", node=f"{twrp.DISK}p1", size=len(KB))
    adb.answers["--getsize64"] = [
        (
            f"{len(KB)} {hashlib.md5(KB, usedforsecurity=False).hexdigest()}"
            f" {backup.READ_OK}"
        )
    ]
    local = tmp_path / "kb.img.part"
    timeout = backup.subprocess.TimeoutExpired(cmd=["adb", "pull"], timeout=3600)

    def short() -> str:
        local.write_bytes(data=KB[:-1])
        return ""

    def wrong() -> str:
        local.write_bytes(data=KB[::-1][:-1] + b"X")
        return ""

    adb.answers["kb.img.part"] = [timeout, (1, ""), short, wrong]
    with pytest.raises(FatalError, match="did not arrive intact after 3 tries"):
        backup.checked_read(area=area, images=[], local=local)
    adb.answers["kb.img.part"] = [(1, "no such file")]
    with pytest.raises(FatalError, match="no such file"):
        backup.checked_read(area=area, images=[], local=local)
    adb.answers["kb.img.part"] = [wrong]
    with pytest.raises(FatalError, match="its md5 is"):
        backup.checked_read(area=area, images=[], local=local)


def test_identity_needs_its_marker_and_a_whole_cid(*, adb: FakeAdb) -> None:
    adb.answers["ro.serialno"] = [f"serial=G0SERIAL\ncid={CID}"]
    with pytest.raises(FatalError, match="did not say which Echo"):
        backup.identity()
    adb.answers["ro.serialno"] = [f"serial=\ncid=\n{backup.IDENTITY_OK}"]
    with pytest.raises(FatalError, match="CID reads nothing"):
        backup.identity()
    adb.answers["ro.serialno"] = [
        f"serial=\ncid=cat: no such file\n{backup.IDENTITY_OK}"
    ]
    with pytest.raises(FatalError, match="CID reads cat"):
        backup.identity()
    adb.answers["ro.serialno"] = [f"serial=\ncid={CID}\n{backup.IDENTITY_OK}"]
    assert backup.identity() == ("", CID)


def test_named_credits_the_fewest_builds() -> None:
    def image(*, build: str, name: str) -> backup.Image:
        return backup.Image(build=build, image=name, sha256=name, size=1)

    found = [
        (
            {"extents": []},
            [image(build="8138", name="lk"), image(build="8349", name="lk")],
        ),
        (
            {"extents": []},
            [image(build="8138", name="tee"), image(build="8349", name="tee")],
        ),
        ({"extents": []}, [image(build="8349", name="boot")]),
        ({"extents": [{"kind": "copy"}]}, []),
    ]
    builds = {"8138": BUILDS["8149"], "8349": BUILDS["8149"]}
    recorded = backup.named(builds=builds, found=found)
    assert [
        [extent.get("build") for extent in record["extents"]] for record in recorded
    ] == [["8349"], ["8349"], ["8349"], [None]]


def test_on_echo_checks_what_the_echo_says(*, adb: FakeAdb) -> None:
    area = backup.Area(name="kb", node=f"{twrp.DISK}p1", size=2048)
    adb.answers["--getsize64"] = ["cut"]
    with pytest.raises(FatalError, match="did not say how large"):
        backup.on_echo(area=area)
    adb.answers["--getsize64"] = [f"4096 {'0' * 32} {backup.READ_OK}"]
    with pytest.raises(FatalError, match="running table is not the one"):
        backup.on_echo(area=area)


def test_scanned_takes_the_longest_matching_prefix(*, tmp_path: pathlib.Path) -> None:
    path = tmp_path / "held"
    path.write_bytes(data=SYSTEM)
    short = backup.Image(
        build="1",
        image="lk",
        sha256=hashlib.sha256(IMAGE[:1024]).hexdigest(),
        size=1024,
    )
    long = backup.Image(
        build="2",
        image="system",
        sha256=hashlib.sha256(IMAGE).hexdigest(),
        size=len(IMAGE),
    )
    past = backup.Image(build="3", image="system", sha256="0" * 64, size=1 << 20)
    twin = dataclasses.replace(long, build="4")
    md5 = hashlib.md5(SYSTEM, usedforsecurity=False).hexdigest()
    sha256 = hashlib.sha256(SYSTEM).hexdigest()
    assert backup.scanned(images=[short, long, past, twin], path=path) == (
        [long, twin],
        md5,
        sha256,
    )
    assert backup.scanned(images=[], path=path) == ([], md5, sha256)
