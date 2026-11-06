from __future__ import annotations

from firebreak.cache import Download
from firebreak.plugin import (
    BOOT0,
    Device,
    DeviceConfig,
    FastbootFlash,
    Feature,
    FireOs6,
    PayloadCommand,
    Reboot,
    Target,
    Unlock,
    Write,
)
from firebreak.states import State
from firebreak.targets import FIREOS6, STOCK
from firebreak.updates import cdn_ota

BOOTONCE_BOOTLOADER = b"bootonce-bootloader"
BUILDS = {
    "8349": cdn_ota(
        path="2026/10/27/403ee71e-c316-4460-845b-3f50df862132/"
        "update-kindle-donut_puffin-NS6575_user_8349_0013323246980.bin",
        sha256="ecf40ee592c938a25a89d1205723d0beb898372f29cfd35c5f90df01cf6ac84f",
    )
}
BUTTON_GESTURE = (
    "Unplug it. Press and hold its action button, the circle on top, before you"
    " plug the cable in. Keep holding while you plug it in, until the run says"
    " the bootrom answered."
)
DEFAULT_BUILD = "8349"
FASTBOOT_MODE = (
    "Unplug the USB cable and plug it back in, then press and hold the action"
    " button, the circle on top, a second or two later, until the ring turns"
    " green. A hold from the moment the cable goes in reaches this Echo's"
    " bootrom instead: a run that says the bootrom answered means this one needs"
    " starting again, with the hold later."
)
DONUT = Device(
    device_type_id="A32DOYMUN6DTXA",
    fastboot_command=BOOTONCE_BOOTLOADER,
    fastboot_mode=FASTBOOT_MODE,
    features=frozenset({Feature.AB_SLOTS, Feature.BOOT0, Feature.RPMB}),
    hardware_code=0x8167,
    models=frozenset({"AEODN"}),
    name="donut",
    named_by_bootloader=False,
    product="DONUT",
    short_gesture=BUTTON_GESTURE,
    title="Echo Dot (3rd Generation)",
    unlock_keeps_misc=True,
)
SET_FASTBOOT_FLAG = 0x6000
SOURCE = Download(
    browser=True,
    directory="donut-v1.0.0",
    mirror=True,
    name="kamakiri-donut-v1.0.0.zip",
    root="kamakiri",
    sha256="4d2bb52eaf661f6616aa4268584d44cf9155c1328c34bfcba01a6558f942b738",
    size=23442578,
    url="https://xdaforums.com/attachments/kamakiri-donut-v1-0-0-zip.6384586/",
)
KAMAKIRI_V1_0_0 = Unlock(
    device=DONUT,
    family="kamakiri",
    plan=(
        Write(image="bin/tee-donut.bin", label="write the TEE", target="tee2"),
        Write(image="bin/lk-donut.bin", label="write the bootloader", target="lk_a"),
        Write(image="bin/lk-donut.bin", label="write the bootloader", target="lk_b"),
        Write(image="bin/kaeru-donut.bin", label="write kaeru", target="expdb"),
        Write(
            image="bin/tee-donut-payload.bin",
            label="write the TEE payload",
            target="tee1",
        ),
        Write(
            image="bin/preloader-donut.bin",
            label="write the preloader",
            target=BOOT0,
            unrecoverable=True,
        ),
        PayloadCommand(
            command=SET_FASTBOOT_FLAG, label="set the fastboot flag", settle=5.0
        ),
        Reboot(label="restarting into kamakiri's fastboot"),
        FastbootFlash(
            image="bin/twrp-donut.img", label="write TWRP", target="recovery"
        ),
        Reboot(into="recovery", label="reboot into recovery"),
    ),
    requires=frozenset({Feature.AB_SLOTS, Feature.BOOT0, Feature.RPMB}),
    source=SOURCE,
    version="1.0.0",
)
CONFIG = DeviceConfig(
    builds=BUILDS,
    default_build=DEFAULT_BUILD,
    default_target=FIREOS6,
    device=DONUT,
    kamakiri_unlock=KAMAKIRI_V1_0_0,
    lk_values=frozenset({
        "0d18cf4-20181206_233555",
        "6f7225e-20230407_013816",
    }),
    targets=(
        Target(
            device=DONUT,
            goal=State.STOCK_BOOTED,
            installs=FireOs6(build=None),
            name=STOCK,
            summary=f"Amazon's Fire OS BUILD, {DEFAULT_BUILD} when left out,"
            " which erases the whole Echo and locks it again.",
            unlock=None,
        ),
        Target(
            device=DONUT,
            goal=State.UNLOCKED_BOOTED,
            installs=FireOs6(build=DEFAULT_BUILD),
            name=FIREOS6,
            summary="boot-root.zip's root adb on the Fire OS 6 already installed,"
            " which keeps the Echo's setup; with --factory-reset, Fire OS 6"
            f" {DEFAULT_BUILD}, with the Echo erased.",
            unlock=KAMAKIRI_V1_0_0,
        ),
    ),
)
