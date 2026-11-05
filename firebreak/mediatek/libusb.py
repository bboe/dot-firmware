from __future__ import annotations

import contextlib
import ctypes
import functools
import subprocess
import sys
import types
from typing import TYPE_CHECKING, Any

from firebreak.mediatek.usbdl import (
    MEDIATEK_VENDOR_ID,
    READ_TIMEOUT,
    ProductId,
    UsbdlError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

CDC_DATA_CLASS = 0x0A
CONTROL_INTERFACE = 0
DRAIN_TIMEOUT = 0.05
ELEVATES = sys.platform == "darwin"
FROM_DEVICE = 0x80
MILLISECONDS = 1000
TO_INTERFACE = 0x21
TRANSFER_LIMIT = 0x4000
TRIGGER = """
import sys
import usb.core

device = usb.core.find(idVendor=0x0E8D, idProduct=0x0003)
if device is None:
    sys.exit("no bootrom answered the privileged helper")
device._ctx.managed_claim_interface = lambda *held, **named: None
try:
    device.ctrl_transfer(0xA1, 0, 0, int(sys.argv[1]), 0)
except usb.core.USBError:
    pass
"""
UNCONFIGURED = "Configuration not set"
USBDK = sys.platform == "win32"
USE_USBDK = 1


class NoLibusbError(UsbdlError):
    pass


class NoPyusbError(UsbdlError):
    pass


class NotPermittedError(UsbdlError):
    pass


class NotThereError(UsbdlError):
    pass


class Usb:
    def __init__(
        self,
        *,
        device: Any,  # ruff: ignore[any-type]
        inward: Any,  # ruff: ignore[any-type]
        outward: Any,  # ruff: ignore[any-type]
        timeout: float = READ_TIMEOUT,
    ) -> None:
        self.device = device
        self.held = b""
        self.inward = inward
        self.outward = outward
        self.timeout = timeout

    def _gathered(self, *, size: int) -> bytes:
        core = pyusb()
        try:
            return bytes(bytearray(self.inward.read(size, self._milliseconds())))
        except core.core.USBTimeoutError:
            return b""

    def _milliseconds(self) -> int:
        return max(1, int(self.timeout * MILLISECONDS))

    def close(self) -> None:
        core = pyusb()
        self.held = b""
        with contextlib.suppress(core.core.USBError):
            core.util.dispose_resources(self.device)

    def control_in(
        self, *, index: int, length: int, request: int, request_type: int, value: int
    ) -> bytes:
        return bytes(
            bytearray(
                self.device.ctrl_transfer(
                    request_type, request, value, index, length, self._milliseconds()
                )
            )
        )

    def control_out(self, *, data: bytes, index: int, request: int, value: int) -> None:
        self.device.ctrl_transfer(
            TO_INTERFACE, request, value, index, data, self._milliseconds()
        )

    def read(self, *, size: int) -> bytes:
        if len(self.held) < size:
            packet = self.inward.wMaxPacketSize
            want = -(-(size - len(self.held)) // packet) * packet
            self.held += self._gathered(size=min(want, TRANSFER_LIMIT))
        got, self.held = self.held[:size], self.held[size:]
        return got

    def reset_input_buffer(self) -> None:
        self.held = b""
        was, self.timeout = self.timeout, DRAIN_TIMEOUT
        try:
            while self._gathered(size=self.inward.wMaxPacketSize):
                pass
        finally:
            self.timeout = was

    def write(self, *, data: bytes) -> int | None:
        for chunk in chunked(data=data, limit=self.outward.wMaxPacketSize):
            self.outward.write(chunk, self._milliseconds())
        return len(data)


def authorize() -> bool:
    return (
        subprocess.run(["sudo", "-v"], check=False).returncode == 0
        if ELEVATES
        else True
    )


@functools.cache
def backend() -> Any:  # ruff: ignore[any-type]
    if not USBDK:
        return None
    from usb.backend import libusb1  # ruff: ignore[import-outside-top-level]

    chosen = libusb1.get_backend()
    if chosen is None:
        raise NoLibusbError(missing(why="no libusb-1.0.dll on the PATH loaded"))
    try:
        option = chosen.lib.libusb_set_option
    except AttributeError as error:
        why = "the libusb-1.0.dll on the PATH is older than 1.0.22"
        raise NoLibusbError(missing(why=why)) from error
    option.argtypes = [ctypes.c_void_p, ctypes.c_int]
    answered = option(chosen.ctx, USE_USBDK)
    if answered:
        message = (
            "UsbDk did not answer libusb, which needs it to take the bootrom"
            f" from its serial driver (libusb answered {answered}). The steps at"
            " https://github.com/bboe/firebreak#windows install it."
        )
        raise NotPermittedError(message)
    return chosen


def chunked(*, data: bytes, limit: int) -> Iterator[bytes]:
    step = max(limit, TRANSFER_LIMIT // limit * limit)
    for index in range(0, len(data), step):
        yield data[index : index + step]


def configured(*, core: Any, device: Any) -> Any:  # ruff: ignore[any-type]
    try:
        return device.get_active_configuration()
    except core.core.USBError as error:
        if not USBDK or UNCONFIGURED not in str(error):
            raise
    device.set_configuration()
    return device.get_active_configuration()


def detached(*, core: Any, device: Any, interface: int) -> None:  # ruff: ignore[any-type]
    try:
        if not device.is_kernel_driver_active(interface):
            return
        device.detach_kernel_driver(interface)
    except NotImplementedError:
        return
    except core.core.USBError as error:
        message = (
            "the kernel holds the interface libusb needs to reach the bootrom,"
            f" and firebreak cannot take it: {error}. On Linux the udev rules"
            " from https://github.com/bboe/firebreak#linux give this user that"
            " access."
        )
        raise NotPermittedError(message) from error


def elevated(*, index: int) -> None:
    done = subprocess.run(
        [*sudo_python(), "-c", TRIGGER, str(index)],
        capture_output=True,
        check=False,
        text=True,
    )
    if done.returncode != 0:
        message = (
            "the privileged helper that sets the payload running did not run:"
            f" {done.stderr.strip() or f'exit {done.returncode}'}"
        )
        raise NotPermittedError(message)


def endpoints(*, device: Any) -> tuple[Any, Any, Any]:  # ruff: ignore[any-type]
    core = pyusb()
    try:
        configuration = configured(core=core, device=device)
    except core.core.USBError as error:
        message = f"libusb cannot open the bootrom it can see: {error}." + (
            " On Windows UsbDk takes it from its serial driver, and the steps at"
            " https://github.com/bboe/firebreak#windows install it."
            if USBDK
            else " On Linux the udev rules from https://github.com/bboe/firebreak#linux"
            " give this user access to it, and a run needs no root with them in"
            " place."
        )
        raise NotPermittedError(message) from error
    data = core.util.find_descriptor(configuration, bInterfaceClass=CDC_DATA_CLASS)
    if data is None:
        message = "the bootrom offers no CDC data interface"
        raise NotThereError(message)
    inward, outward = (
        facing(core=core, data=data, wanted=wanted)
        for wanted in (core.util.ENDPOINT_IN, core.util.ENDPOINT_OUT)
    )
    if inward is None or outward is None:
        message = "the bootrom's data interface has no pair of bulk endpoints"
        raise NotThereError(message)
    return data, inward, outward


def facing(*, core: Any, data: Any, wanted: int) -> Any:  # ruff: ignore[any-type]
    return core.util.find_descriptor(
        data,
        custom_match=lambda each: (
            core.util.endpoint_direction(each.bEndpointAddress) == wanted
        ),
    )


def found(*, core: Any) -> Any:  # ruff: ignore[any-type]
    try:
        device = core.core.find(
            backend=backend(), idProduct=ProductId.BOOTROM, idVendor=MEDIATEK_VENDOR_ID
        )
    except core.core.NoBackendError as error:
        raise NoLibusbError(missing(why=str(error))) from error
    if device is None:
        message = f"no {MEDIATEK_VENDOR_ID:04x}:{ProductId.BOOTROM:04x} answered libusb"
        raise NotThereError(message)
    return device


def missing(*, why: str) -> str:
    return (
        f"this run cannot load libusb, so it cannot speak raw USB: {why}."
        " On macOS `brew install libusb` provides it, and on Linux it is the"
        " libusb-1.0 package. On Windows it is libusb-1.0.dll from libusb's"
        " release in a folder on the PATH, as"
        " https://github.com/bboe/firebreak#windows says."
    )


def open_bootrom(*, timeout: float = READ_TIMEOUT) -> Usb:
    core = pyusb()
    device = found(core=core)
    try:
        data, inward, outward = endpoints(device=device)
    except NotThereError:
        with contextlib.suppress(core.core.USBError):
            core.util.dispose_resources(device)
        raise
    if not ELEVATES:
        detached(core=core, device=device, interface=CONTROL_INTERFACE)
        detached(core=core, device=device, interface=data.bInterfaceNumber)
    return Usb(device=device, inward=inward, outward=outward, timeout=timeout)


def pyusb() -> Any:  # ruff: ignore[any-type]
    try:
        from usb import core, util  # ruff: ignore[import-outside-top-level]
    except ImportError as error:
        message = (
            "pyusb is not installed, so this run cannot speak raw USB:"
            f" {error}. `pip install pyusb` provides it."
        )
        raise NoPyusbError(message) from error
    return types.SimpleNamespace(core=core, util=util)


def sudo_python() -> list[str]:
    return ["sudo", sys.executable]


def trigger(*, index: int) -> str:
    if ELEVATES:
        elevated(index=index)
        return ""
    core = pyusb()
    device = found(core=core)
    device._ctx.managed_claim_interface = (  # ruff: ignore[private-member-access]
        lambda *_, **__: None
    )
    said = ""
    try:
        device.ctrl_transfer(TO_INTERFACE | FROM_DEVICE, 0, 0, index, 0)
    except core.core.USBError as error:
        said = str(error)
    core.util.dispose_resources(device)
    return said
