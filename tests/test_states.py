from __future__ import annotations

from firebreak.states import State, shown


def test_a_locked_fastboot_is_shown_as_stock_and_an_unlocked_one_is_not() -> None:
    assert shown(current=State.STOCK_FASTBOOT) == "stock fastboot"
    assert shown(current=State.STOCK_FIREOS5_FASTBOOT) == "stock fastboot"
    assert shown(current=State.UNLOCKED_FASTBOOT) == "unlocked fastboot"
    assert shown(current=State.AMONET_FIREOS5_FASTBOOT) == "unlocked fastboot"


def test_a_recovery_is_shown_as_twrp_whatever_wrote_it() -> None:
    assert shown(current=State.UNLOCKED_TWRP) == "TWRP"
    assert shown(current=State.AMONET_FIREOS5_BBOE_TWRP) == "TWRP"
    assert shown(current=State.AMONET_TWRP_BOOT_MOVED) == "TWRP"


def test_an_unlocked_echo_booted_is_shown_as_rooted_fire_os_6() -> None:
    assert shown(current=State.UNLOCKED_BOOTED) == "rooted Fire OS 6"


def test_every_other_state_is_shown_by_its_own_name() -> None:
    assert shown(current=State.BOOTROM) == "bootrom"
    assert shown(current=State.STOCK_BOOTED) == "stock-booted"
