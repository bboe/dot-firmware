from __future__ import annotations

import struct

import pytest
from conftest import FakePort, reply_payload, reply_read, sent_payload, sent_read

from firebreak.amonet.payload import NotReadyError
from firebreak.mediatek import kamakiri, usbdl

LINECODE = bytes(range(kamakiri.LINE_CODING_SIZE))
SEND = 0x1234
STAGE1 = b"stage1!!"


class ControlPort(FakePort):
    def __init__(
        self, *, linecode: bytes = LINECODE, refuse: bool = False, replies: bytes = b""
    ) -> None:
        super().__init__(replies=replies)
        self.linecode = linecode
        self.refuse = refuse
        self.transfers: list[tuple[int, bytes | int]] = []

    def control_in(
        self, *, index: int, length: int, request: int, request_type: int, value: int
    ) -> bytes:
        assert index == 0
        assert request_type in {
            kamakiri.TO_INTERFACE | kamakiri.FROM_DEVICE,
            kamakiri.FROM_DEVICE,
        }
        assert value in {0, kamakiri.CONFIGURATION_DESCRIPTOR}
        if self.refuse:
            message = "[Errno 32] Pipe error"
            raise OSError(message)
        self.transfers.append((request, length))
        return self.linecode[:length]

    def control_out(self, *, data: bytes, index: int, request: int, value: int) -> None:
        assert index == value == 0
        if self.refuse:
            message = "[Errno 32] Pipe error"
            raise OSError(message)
        self.transfers.append((request, data))


def access_header(*, address: int, data: bytes = b"", length: int) -> bytes:
    return bytes([usbdl.BootromCommand.ACCESS]) + struct.pack(
        ">III", int(bool(data)), address, length
    )


def accessed_replies(
    *,
    address: int,
    data: bytes = b"",
    end: bool = True,
    held: bytes = b"",
    primed: bool,
) -> bytes:
    length = len(data or held)
    prime = access_header(address=0, length=1)
    return (
        (
            prime
            + usbdl.READ_STATUS
            + b"\0"
            + usbdl.READ_STATUS
            + reply_read(address=kamakiri.WATCHDOG_RESTART, values=(0,))
            if primed
            else prime + b"\x1d\x1a"
        )
        + access_header(
            address=address - kamakiri.ACCESS_OFFSET, data=data, length=length
        )
        + usbdl.READ_STATUS
        + held
        + (usbdl.READ_STATUS if end else b"")
    )


def accessed_sent(
    *, address: int, data: bytes = b"", length: int, primed: bool
) -> bytes:
    return (
        access_header(address=0, length=1)
        + (sent_read(address=kamakiri.WATCHDOG_RESTART, count=1) if primed else b"")
        + access_header(
            address=address - kamakiri.ACCESS_OFFSET, data=data, length=length
        )
        + data
    )


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
    monkeypatch.setattr(name="ELEVATES", target=kamakiri, value=True)
    monkeypatch.setattr(
        name="trigger", target=kamakiri, value=lambda *, index: triggers.append(index)
    )
    answer, _ = pointed()
    port = FakePort(
        replies=answer + ladder_replies() + reply_payload(length=len(STAGE1)) + ready
    )
    return port, triggers


def test_an_overflow_refuses_a_short_line_coding(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(name="ELEVATES", target=kamakiri, value=False)
    port = ControlPort(linecode=LINECODE[:-1])
    with pytest.raises(expected_exception=NotReadyError, match="6 of 7 bytes"):
        kamakiri.start_stage1(
            bootrom=usbdl.Bootrom(port=port, timeout=0), stage1=STAGE1
        )
    assert port.written == bytearray()


def test_an_overflow_says_what_the_line_coding_transfers_answered(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(name="ELEVATES", target=kamakiri, value=False)
    port = ControlPort(refuse=True)
    with pytest.raises(expected_exception=NotReadyError, match="Pipe error"):
        kamakiri.start_stage1(
            bootrom=usbdl.Bootrom(port=port, timeout=0), stage1=STAGE1
        )
    assert port.written == bytearray()


def test_an_overflow_writes_stage1_and_points_the_send_routine_at_it(
    *, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(name="ELEVATES", target=kamakiri, value=False)
    pointer = struct.pack("<I", kamakiri.PAYLOAD_ADDRESS)
    target = SEND + kamakiri.SEND_POINTER_OFFSET
    port = ControlPort(
        replies=accessed_replies(
            address=kamakiri.SEND_POINTER,
            held=struct.pack("<I", SEND),
            primed=False,
        )
        + accessed_replies(address=kamakiri.PAYLOAD_ADDRESS, data=STAGE1, primed=True)
        + accessed_replies(address=target, data=pointer, end=False, primed=True)
        + kamakiri.STAGE1_READY
    )
    kamakiri.start_stage1(bootrom=usbdl.Bootrom(port=port, timeout=0), stage1=STAGE1)
    assert bytes(port.written) == (
        accessed_sent(address=kamakiri.SEND_POINTER, length=4, primed=False)
        + accessed_sent(
            address=kamakiri.PAYLOAD_ADDRESS,
            data=STAGE1,
            length=len(STAGE1),
            primed=True,
        )
        + accessed_sent(address=target, data=pointer, length=4, primed=True)
    )
    assert port.replies == bytearray()
    linecode = LINECODE + b"\0"
    pointers = (
        *(kamakiri.ACCESS_REGISTER + 5 + step for step in range(3)),
        *(kamakiri.ACCESS_REGISTER - 2 - step for step in range(3)),
    )
    walk = [
        transfer
        for each in pointers
        for transfer in (
            (kamakiri.SET_LINE_CODING, linecode + struct.pack("<I", each)),
            (kamakiri.GET_DESCRIPTOR, kamakiri.DESCRIPTOR_LENGTH),
        )
    ]
    assert port.transfers == [
        (kamakiri.SET_LINE_CODING, kamakiri.LINE_CODING),
        (kamakiri.GET_LINE_CODING, kamakiri.LINE_CODING_SIZE),
        *walk * 3,
    ]


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


def test_the_refusal_quotes_what_the_trigger_answered() -> None:
    assert kamakiri.refused(said="") == ""
    assert "gone" in kamakiri.refused(said="gone")
