from __future__ import annotations

import struct

import pytest
from conftest import FakePort, reply_payload, reply_read, sent_payload, sent_read

from firebreak.amonet.payload import NotReadyError
from firebreak.mediatek import kamakiri, usbdl

STAGE1 = b"stage1!!"


def ladder_replies() -> bytes:
    return b"".join(
        reply_read(
            address=kamakiri.WATCHDOG_RESTART - step * usbdl.WORD_SIZE,
            values=(0,) * (step + 1),
        )
        for step in kamakiri.LADDER
    )


def ladder_sent() -> bytes:
    return b"".join(
        sent_read(
            address=kamakiri.WATCHDOG_RESTART - step * usbdl.WORD_SIZE,
            count=step + 1,
        )
        for step in kamakiri.LADDER
    )


def pointed() -> tuple[bytes, bytes]:
    words = (kamakiri.reversed_word(value=kamakiri.PAYLOAD_ADDRESS),)
    return (
        bytes([usbdl.BootromCommand.WRITE_WORDS])
        + struct.pack(">II", kamakiri.WATCHDOG_RESTART, 1)
        + usbdl.WRITE_STATUS
        + struct.pack(">I", *words)
        + usbdl.WRITE_STATUS,
        bytes([usbdl.BootromCommand.WRITE_WORDS])
        + struct.pack(">II", kamakiri.WATCHDOG_RESTART, 1)
        + struct.pack(">I", *words),
    )


def started(
    *, monkeypatch: pytest.MonkeyPatch, ready: bytes = kamakiri.STAGE1_READY
) -> tuple[FakePort, list[int]]:
    triggers: list[int] = []
    monkeypatch.setattr(
        name="trigger", target=kamakiri, value=lambda *, index: triggers.append(index)
    )
    answer, _ = pointed()
    port = FakePort(
        replies=answer + ladder_replies() + reply_payload(length=len(STAGE1)) + ready
    )
    return port, triggers


def test_ladder_climbs_towards_the_written_word() -> None:
    assert [
        (kamakiri.WATCHDOG_RESTART - step * usbdl.WORD_SIZE, step + 1)
        for step in kamakiri.LADDER
    ] == [(0x10007014 + index * usbdl.WORD_SIZE, 16 - index) for index in range(15)]


def test_reversed_word_swaps_the_byte_order() -> None:
    assert kamakiri.reversed_word(value=0x100A00) == 0x000A1000
    assert kamakiri.reversed_word(value=0x01020304) == 0x04030201


def test_start_stage1_points_the_bootrom_at_the_payload_then_triggers_it(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, triggers = started(monkeypatch=monkeypatch)
    kamakiri.start_stage1(bootrom=usbdl.Bootrom(port=port, timeout=0), stage1=STAGE1)
    _, sent = pointed()
    assert bytes(port.written) == sent + ladder_sent() + sent_payload(payload=STAGE1)
    assert triggers == [kamakiri.TRIGGER_INDEX]
    assert port.replies == bytearray()


def test_start_stage1_refuses_a_wrong_ready_pattern(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, triggers = started(monkeypatch=monkeypatch, ready=b"\x00\x00\x00\x00")
    with pytest.raises(expected_exception=NotReadyError, match="ready pattern"):
        kamakiri.start_stage1(
            bootrom=usbdl.Bootrom(port=port, timeout=0), stage1=STAGE1
        )
    assert triggers == [kamakiri.TRIGGER_INDEX]


def test_start_stage1_says_what_the_trigger_answered_when_nothing_comes_back(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, _ = started(monkeypatch=monkeypatch, ready=b"")
    monkeypatch.setattr(
        name="trigger", target=kamakiri, value=lambda *, index: f"[Errno 16] {index}"
    )
    with pytest.raises(expected_exception=NotReadyError, match="trigger itself"):
        kamakiri.start_stage1(
            bootrom=usbdl.Bootrom(port=port, timeout=0), stage1=STAGE1
        )


def test_start_stage1_tells_a_linux_run_the_kernel_refused_the_trigger(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, _ = started(monkeypatch=monkeypatch, ready=b"")
    monkeypatch.setattr(name="ELEVATES", target=kamakiri, value=False)
    monkeypatch.setattr(
        name="trigger",
        target=kamakiri,
        value=lambda **_: "[Errno 5] Input/Output Error",
    )
    with pytest.raises(expected_exception=NotReadyError, match="from macOS") as caught:
        kamakiri.start_stage1(
            bootrom=usbdl.Bootrom(port=port, timeout=0), stage1=STAGE1
        )
    assert "204" in str(caught.value)
    assert "EINVAL" in str(caught.value)


def test_the_refusal_names_no_platform_where_a_helper_sends_the_trigger(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(name="ELEVATES", target=kamakiri, value=True)
    assert kamakiri.refused(said="") == ""
    assert "macOS" not in kamakiri.refused(said="gone")
