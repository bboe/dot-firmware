from __future__ import annotations

import contextlib
import subprocess
import time
from typing import TYPE_CHECKING

from firebreak import bootrom
from firebreak.bootrom_step import bootloader_plan, unwritten
from firebreak.emmc import EmmcArea
from firebreak.host import NO_ACCESS, getvar, in_fastboot, rerun
from firebreak.kamakiri.donut_v1_0_0 import (
    STAGE1_SHA256,
    Client,
    check_stage,
    start_stage2,
)
from firebreak.mediatek import kamakiri
from firebreak.mediatek.libusb import (
    NoAccessError,
    NoLibusbError,
    NoPyusbError,
    NotPermittedError,
    NotThereError,
    authorize,
    authorized_already,
    open_bootrom,
)
from firebreak.mediatek.usbdl import (
    PORT_POLL_INTERVAL,
    Bootrom,
    HandshakeTimeoutError,
    ProtocolMismatchError,
    UsbdlError,
)
from firebreak.ui import (
    MINUTE,
    PROGRESS,
    SESSION,
    FatalError,
    again,
    asked_for,
    say,
    show,
    title,
)

if TYPE_CHECKING:
    import pathlib

    from firebreak.plugin import Unlock

BOOTROM_WAIT = 10 * MINUTE
FASTBOOT_TRIES = 30
IDME_BLOCKS = 64
IDME_MAGIC = b"beefdeed"
STAGE1 = "brom-payload/stage1/stage1.bin"
STAGE2 = "brom-payload/stage2/stage2.bin"


def answered(*, device_code: int | None) -> Bootrom | None:
    try:
        return checked_bootrom(device_code=device_code)
    except NotThereError:
        SESSION.hardware_code = None
        return None


def authorized(*, explained: bool = False) -> None:
    if authorized_already():
        return
    if not explained:
        say(
            text=f"Unlocking this {title()} needs one privileged USB call, the one"
            " that starts its unlock running in its bootrom, so sudo asks for your"
            " password now. Nothing is written yet."
        )
    show(text="")
    granted = authorize()
    show(text="")
    if not granted:
        raise FatalError(
            message="this run needs one privileged USB call to start the unlock's"
            " payload, and sudo refused. Nothing was written."
        )


def await_fastboot() -> None:
    PROGRESS.begin(estimate=30, label="waiting for kamakiri's fastboot")
    for _ in range(FASTBOOT_TRIES):
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            if in_fastboot() and getvar(name="unlock_status").lower() == "true":
                PROGRESS.end()
                return
        time.sleep(2)
    raise FatalError(
        message=f"the {title()} did not come back in kamakiri's fastboot. " + again()
    )


def check_device_type(*, live: Client, unlock: Unlock) -> None:
    want = unlock.device.device_type_id
    if not want:
        return
    try:
        live.switch_partition(partition=EmmcArea.BOOT1)
        held = live.read_blocks(count=IDME_BLOCKS, index=0)
        live.switch_partition(partition=EmmcArea.USER)
    except (OSError, UsbdlError) as error:
        close(client=live)
        raise FatalError(
            message=f"the {title()}'s IDME could not be read: {error}."
            f" {unwritten()} Unplug the {title()}. " + again()
        ) from error
    if not held.startswith(IDME_MAGIC):
        close(client=live)
        raise FatalError(
            message=f"the second boot area does not begin {IDME_MAGIC.decode()},"
            f" so this run cannot tell that this is an {unlock.device.title}."
            f" {unwritten()} Unplug it. " + asked_for()
        )
    if want.encode() not in held:
        close(client=live)
        raise FatalError(
            message=f"IDME does not name this an {unlock.device.title}:"
            f" {want} is not among its values. {unwritten()} Unplug it."
        )


def checked_bootrom(*, device_code: int | None) -> Bootrom:
    try:
        port = open_bootrom()
    except NoAccessError as error:
        raise FatalError(
            message=f"{NO_ACCESS}{rerun()}\n\n{error} {unwritten()}"
        ) from error
    except (NoLibusbError, NoPyusbError, NotPermittedError) as error:
        raise FatalError(message=f"{error} {unwritten()}") from error
    SESSION.raw_usb = True
    client = Bootrom(port=port)
    try:
        if SESSION.hardware_code is None:
            client.handshake()
        found = client.hardware_code()
        SESSION.hardware_code = found
    except (HandshakeTimeoutError, OSError, UsbdlError) as error:
        close(client=client)
        raise FatalError(
            message=f"the {title()}'s bootrom did not answer as one: {error}."
            f" {unwritten()} Unplug the {title()}. " + again()
        ) from error
    if device_code is not None and found != device_code:
        close(client=client)
        raise FatalError(
            message=f"the bootrom reports hardware {found:#06x}, not"
            f" {device_code:#06x}, so this is not the Echo {title()} unlocks."
            f" {unwritten()} Unplug it."
        )
    return client


def close(*, client: Bootrom | Client) -> None:
    with contextlib.suppress(OSError, UsbdlError):
        client.port.close()


def exploited(*, client: Bootrom, source: pathlib.Path) -> Client:
    stage1 = (source / STAGE1).read_bytes()
    try:
        check_stage(data=stage1, label="stage1.bin", wanted=STAGE1_SHA256)
    except ProtocolMismatchError as error:
        close(client=client)
        raise FatalError(
            message=f"the kit's payload is not the one this run speaks: {error}."
            f" {unwritten()} Unplug the {title()}."
        ) from error
    try:
        client.disable_watchdog()
        kamakiri.start_stage1(bootrom=client, stage1=stage1)
        return start_stage2(stage1=client, stage2=(source / STAGE2).read_bytes())
    except (OSError, UsbdlError) as error:
        close(client=client)
        raise FatalError(
            message=f"the exploit did not go through the {title()}'s bootrom:"
            f" {error}. {unwritten()} Unplug the {title()}. " + again()
        ) from error


def kamakiri_step(*, source: pathlib.Path, unlock: Unlock) -> bool:
    authorized()
    PROGRESS.begin(estimate=40, label="waiting for the bootrom")
    client = waited(device_code=unlock.device.hardware_code)
    authorized(explained=True)
    live = exploited(client=client, source=source)
    check_device_type(live=live, unlock=unlock)
    actions = bootrom.check(
        actions=bootloader_plan(amonet=source, live=live, unlock=unlock)
    )
    if PROGRESS.steps:
        PROGRESS.steps += bootrom.counted(actions=actions)
    bootrom.execute(actions=actions, payload=live)
    await_fastboot()
    return True


def waited(*, device_code: int | None) -> Bootrom:
    deadline = time.monotonic() + BOOTROM_WAIT
    while True:
        client = answered(device_code=device_code)
        if client is not None:
            return client
        if time.monotonic() >= deadline:
            raise FatalError(
                message=f"no bootrom answered in {BOOTROM_WAIT // MINUTE} minutes."
                f" Unplug the {title()}, hold its action button, and plug it back"
                " in. " + again()
            )
        time.sleep(PORT_POLL_INTERVAL)
