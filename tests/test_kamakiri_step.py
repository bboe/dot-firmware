from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import types

import pytest

from firebreak import kamakiri_step
from firebreak.devices import DONUT
from firebreak.devices.donut import KAMAKIRI_V1_0_0
from firebreak.host import NO_ACCESS
from firebreak.mediatek.libusb import NoAccessError, NoLibusbError, NotThereError
from firebreak.mediatek.usbdl import HandshakeTimeoutError
from firebreak.ui import FatalError

HARDWARE = 0x8167
IDME = b"beefdeed2.1\0" + bytes(0x2AA0) + b"A32DOYMUN6DTXA"
NOTHING = "nothing answered"


class FakeBootrom:
    def __init__(self, *, code: int = HARDWARE, shook: Exception | None = None) -> None:
        self.did: list[str] = []
        self.code = code
        self.port = FakePort()
        self.shook = shook

    def disable_watchdog(self) -> None:
        self.did.append("watchdog")

    def handshake(self) -> None:
        if self.shook is not None:
            raise self.shook
        self.did.append("handshake")

    def hardware_code(self) -> int:
        self.did.append("hardware code")
        return self.code


class FakePayload:
    def __init__(self, *, idme: bytes = IDME) -> None:
        self.areas: list[int] = []
        self.idme = idme
        self.port = FakePort()

    def read_blocks(self, *, count: int, index: int) -> bytes:
        assert index == 0
        return self.idme.ljust(count * 512, b"\0")[: count * 512]

    def switch_partition(self, *, partition: int) -> None:
        self.areas.append(partition)


class FakePort:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def patched(
    *,
    bootroms: list[FakeBootrom],
    monkeypatch: pytest.MonkeyPatch,
    permitted: bool = True,
) -> list[FakeBootrom]:
    made: list[FakeBootrom] = []
    kamakiri_step.SESSION.hardware_code = None

    def open_bootrom() -> object:
        if not bootroms:
            raise NotThereError(NOTHING)
        made.append(bootroms.pop(0))
        return made[-1].port

    monkeypatch.setattr(name="open_bootrom", target=kamakiri_step, value=open_bootrom)
    monkeypatch.setattr(name="authorize", target=kamakiri_step, value=lambda: permitted)
    monkeypatch.setattr(
        name="Bootrom", target=kamakiri_step, value=lambda **_: made[-1]
    )
    monkeypatch.setattr(name="sleep", target=kamakiri_step.time, value=lambda _: None)
    return made


def test_a_bootrom_that_will_not_shake_hands_says_nothing_was_written(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = patched(
        bootroms=[FakeBootrom(shook=HandshakeTimeoutError("no answer"))],
        monkeypatch=monkeypatch,
    )
    with pytest.raises(expected_exception=FatalError, match="did not answer as one"):
        kamakiri_step.waited(device_code=HARDWARE)
    assert made[0].port.closed


def test_a_device_that_declares_no_type_id_skips_the_idme_check() -> None:
    live = FakePayload()
    plain = dataclasses.replace(DONUT, device_type_id="")
    kamakiri_step.check_device_type(
        live=live, unlock=dataclasses.replace(KAMAKIRI_V1_0_0, device=plain)
    )
    assert live.areas == []


def test_a_second_boot_area_without_idme_stops_the_run() -> None:
    live = FakePayload(idme=b"\0" * 64)
    with pytest.raises(expected_exception=FatalError, match="does not begin beefdeed"):
        kamakiri_step.check_device_type(live=live, unlock=KAMAKIRI_V1_0_0)
    assert live.port.closed


def test_a_spent_handshake_is_reused_rather_than_sent_again(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = patched(bootroms=[FakeBootrom()], monkeypatch=monkeypatch)
    kamakiri_step.SESSION.hardware_code = HARDWARE
    assert kamakiri_step.waited(device_code=HARDWARE) is made[0]
    assert made[0].did == ["hardware code"]


def test_a_stage1_the_client_does_not_speak_is_refused_before_the_watchdog(
    *, tmp_path: pathlib.Path
) -> None:
    client = FakeBootrom()
    made = tmp_path / kamakiri_step.STAGE1
    made.parent.mkdir(exist_ok=True, parents=True)
    made.write_bytes(b"not the kit's stage1")
    with pytest.raises(expected_exception=FatalError, match="not the one this run"):
        kamakiri_step.exploited(client=client, source=tmp_path)
    assert client.did == []
    assert client.port.closed


def test_a_step_runs_the_plan_then_waits_for_fastboot(
    *, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    order: list[str] = []
    live = FakeBootrom()
    patched(bootroms=[FakeBootrom()], monkeypatch=monkeypatch)
    monkeypatch.setattr(
        name="exploited",
        target=kamakiri_step,
        value=lambda **_: order.append("exploit") or live,
    )
    monkeypatch.setattr(
        name="check_device_type",
        target=kamakiri_step,
        value=lambda **_: order.append("device type"),
    )
    monkeypatch.setattr(
        name="bootloader_plan", target=kamakiri_step, value=lambda **_: ()
    )
    monkeypatch.setattr(
        name="bootrom",
        target=kamakiri_step,
        value=types.SimpleNamespace(
            check=lambda *, actions: actions,
            counted=lambda **_: 0,
            execute=lambda **_: order.append("execute"),
        ),
    )
    monkeypatch.setattr(
        name="await_fastboot", target=kamakiri_step, value=lambda: order.append("wait")
    )
    assert kamakiri_step.kamakiri_step(source=tmp_path, unlock=KAMAKIRI_V1_0_0) is True
    assert order == ["exploit", "device type", "execute", "wait"]
    assert not live.port.closed


def test_an_exploit_that_fails_says_the_echo_was_not_written(
    *, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    client = FakeBootrom()

    def fails(**_: object) -> None:
        message = "device not configured"
        raise OSError(message)

    monkeypatch.setattr(name="start_stage1", target=kamakiri_step.kamakiri, value=fails)
    monkeypatch.setattr(
        name="STAGE1_SHA256",
        target=kamakiri_step,
        value=hashlib.sha256(b"payload").hexdigest(),
    )
    for name in (kamakiri_step.STAGE1, kamakiri_step.STAGE2):
        made = tmp_path / name
        made.parent.mkdir(exist_ok=True, parents=True)
        made.write_bytes(b"payload")
    with pytest.raises(expected_exception=FatalError, match="did not go through"):
        kamakiri_step.exploited(client=client, source=tmp_path)
    assert client.did == ["watchdog"]
    assert client.port.closed


def test_another_chip_is_refused_before_anything_is_written(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = patched(bootroms=[FakeBootrom(code=0x8163)], monkeypatch=monkeypatch)
    with pytest.raises(expected_exception=FatalError, match="0x8163, not 0x8167"):
        kamakiri_step.waited(device_code=HARDWARE)
    assert made[0].port.closed


def test_closing_leaves_the_raw_usb_mark_alone() -> None:
    client = FakeBootrom()
    kamakiri_step.SESSION.raw_usb = False
    kamakiri_step.close(client=client)
    assert client.port.closed
    assert kamakiri_step.SESSION.raw_usb is False


def test_fastboot_is_reached_when_it_reports_itself_unlocked(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(name="in_fastboot", target=kamakiri_step, value=lambda: True)
    monkeypatch.setattr(name="getvar", target=kamakiri_step, value=lambda **_: "TRUE")
    kamakiri_step.await_fastboot()


def test_fastboot_that_never_unlocks_is_reported(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(name="FASTBOOT_TRIES", target=kamakiri_step, value=1)
    monkeypatch.setattr(name="sleep", target=kamakiri_step.time, value=lambda _: None)
    monkeypatch.setattr(name="in_fastboot", target=kamakiri_step, value=lambda: False)
    with pytest.raises(expected_exception=FatalError, match="kamakiri's fastboot"):
        kamakiri_step.await_fastboot()


def test_idme_that_names_another_echo_stops_the_run() -> None:
    live = FakePayload(idme=b"beefdeed2.1\0" + bytes(0x2AA0) + b"A32CRUMPETXXXX")
    with pytest.raises(expected_exception=FatalError, match="does not name this"):
        kamakiri_step.check_device_type(live=live, unlock=KAMAKIRI_V1_0_0)
    assert live.port.closed


def test_idme_that_names_the_echo_passes_and_leaves_the_user_area_selected() -> None:
    live = FakePayload()
    kamakiri_step.check_device_type(live=live, unlock=KAMAKIRI_V1_0_0)
    assert live.areas == [2, 0]
    assert not live.port.closed


def test_opening_a_bootrom_marks_the_run_as_having_driven_raw_usb(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = patched(bootroms=[FakeBootrom()], monkeypatch=monkeypatch)
    kamakiri_step.SESSION.raw_usb = False
    assert kamakiri_step.waited(device_code=HARDWARE) is made[0]
    assert kamakiri_step.SESSION.raw_usb is True


def test_the_donut_this_step_unlocks_declares_its_hardware_code() -> None:
    assert DONUT.hardware_code == HARDWARE
    assert DONUT.named_by_bootloader is False
    assert KAMAKIRI_V1_0_0.device is DONUT


def test_the_stage_paths_are_the_kit_s_own(*, tmp_path: pathlib.Path) -> None:
    assert kamakiri_step.STAGE1.endswith("stage1.bin")
    assert kamakiri_step.STAGE2.endswith("stage2.bin")
    assert not (tmp_path / kamakiri_step.STAGE1).exists()


def test_the_step_names_brew_install_libusb_when_libusb_is_missing(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> object:
        message = "libusb is not installed: `brew install libusb`"
        raise NoLibusbError(message)

    monkeypatch.setattr(name="open_bootrom", target=kamakiri_step, value=missing)
    with pytest.raises(expected_exception=FatalError, match="brew install libusb"):
        kamakiri_step.waited(device_code=HARDWARE)


def test_the_step_prints_the_udev_rules_when_linux_denies_the_bootrom(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied() -> object:
        message = "libusb cannot open the bootrom it can see: Access denied."
        raise NoAccessError(message)

    monkeypatch.setattr(name="open_bootrom", target=kamakiri_step, value=denied)
    with pytest.raises(expected_exception=FatalError) as stopped:
        kamakiri_step.waited(device_code=HARDWARE)
    assert stopped.value.message.startswith(NO_ACCESS)
    assert "sg plugdev -c " in stopped.value.message
    assert "Access denied." in stopped.value.message


def test_the_step_refuses_to_start_without_the_privileged_call(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched(bootroms=[], monkeypatch=monkeypatch, permitted=False)
    with pytest.raises(expected_exception=FatalError, match="sudo refused"):
        kamakiri_step.kamakiri_step(source=pathlib.Path(), unlock=KAMAKIRI_V1_0_0)


def test_waiting_forgets_the_handshake_when_the_echo_goes_away(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched(bootroms=[], monkeypatch=monkeypatch)
    kamakiri_step.SESSION.hardware_code = HARDWARE
    monkeypatch.setattr(name="BOOTROM_WAIT", target=kamakiri_step, value=0)
    with pytest.raises(expected_exception=FatalError, match="hold its action button"):
        kamakiri_step.waited(device_code=HARDWARE)
    assert kamakiri_step.SESSION.hardware_code is None


def test_waiting_gives_up_and_says_how_to_reach_the_bootrom(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    patched(bootroms=[], monkeypatch=monkeypatch)
    monkeypatch.setattr(name="BOOTROM_WAIT", target=kamakiri_step, value=0)
    with pytest.raises(expected_exception=FatalError, match="hold its action button"):
        kamakiri_step.waited(device_code=HARDWARE)


def test_waiting_keeps_trying_until_a_bootrom_is_there(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = FakeBootrom()
    tries: list[int] = []

    def open_bootrom() -> object:
        tries.append(1)
        if len(tries) < 3:
            raise NotThereError(NOTHING)
        return answers.port

    monkeypatch.setattr(name="open_bootrom", target=kamakiri_step, value=open_bootrom)
    monkeypatch.setattr(name="Bootrom", target=kamakiri_step, value=lambda **_: answers)
    monkeypatch.setattr(name="sleep", target=kamakiri_step.time, value=lambda _: None)
    assert kamakiri_step.waited(device_code=HARDWARE) is answers
    assert len(tries) == 3


def test_waiting_takes_the_bootrom_that_answers(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    made = patched(bootroms=[FakeBootrom()], monkeypatch=monkeypatch)
    assert kamakiri_step.waited(device_code=HARDWARE) is made[0]
    assert made[0].did == ["handshake", "hardware code"]
    assert not made[0].port.closed
