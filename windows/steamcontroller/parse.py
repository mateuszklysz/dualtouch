from collections import namedtuple
from struct import Struct

from .constants import (
    CHARGE_STATE_CHARGING,
    CHARGE_STATE_CHARGING_DONE,
    CHARGE_STATE_SOURCE_VALIDATE,
    TRITON_BATTERY_REPORT_ID,
    TRITON_INPUT_MIN_LEN,
    TRITON_INPUT_REPORT_IDS,
    SCStatus,
)

# Precompiled parser for the Triton input report body (bytes 1..29). Built once
# and used via unpack_from so the per-frame hot path does a single C-level
# unpack with no intermediate slice allocations.
#   B seq | I buttons | h h triggers | h h h h sticks | h h H h h H pads+pressure
_TRITON_STRUCT = Struct("<BIhhhhhhhhHhhH")


# triton's controller.py expects an SCI tuple with these exact field names.
# Stick fields are appended on the end so existing positional uses keep working.
# lpad_press/rpad_press carry the raw trackpad FORCE (s16 @ 0x16/0x1C of the
# 0x45 report, per the OpenPuck protocol docs) — always streamed by the
# firmware, unlike the discrete click bits which a Steam config can suppress.
# The OSK detects the pad "click" from these; see ControllerManager._press_click.
SteamControllerInput = namedtuple(
    "SteamControllerInput",
    "status seq buttons ltrig rtrig lpad_x lpad_y rpad_x rpad_y "
    "lstick_x lstick_y rstick_x rstick_y "
    "lpad_press rpad_press",
)

SCI_NULL = SteamControllerInput(
    status=0,
    seq=0,
    buttons=0,
    ltrig=0,
    rtrig=0,
    lpad_x=0,
    lpad_y=0,
    rpad_x=0,
    rpad_y=0,
    lstick_x=0,
    lstick_y=0,
    rstick_x=0,
    rstick_y=0,
    lpad_press=0,
    rpad_press=0,
)


# Battery snapshot handed to callers via SteamController.get_battery().
#   percent         0..100
#   charge_state    raw CHARGE_STATE_* byte
#   charging        True while a charger is supplying power (charging, source-
#                   validate, or charge-complete) — mirrors the reference's
#                   IsCharging (anything but Discharging/Reset).
#   charge_complete True once the pack is full and the charger has stopped.
#   voltage_mv      battery voltage in millivolts (diagnostic).
SteamControllerBattery = namedtuple(
    "SteamControllerBattery",
    "percent charge_state charging charge_complete voltage_mv",
)


def _parse_battery(data):
    """Parse a 0x43 power-status report into a SteamControllerBattery, or None
    if the frame isn't a (valid) battery report.

    Windows hidapi delivers 17 bytes; Linux's hidraw backend trims it to 15
    (firmware-side descriptor differs). All fields we actually use sit in the
    first 5 bytes (report id + state + percent + voltage LE), so we only
    require that much."""
    if len(data) < 5 or data[0] != TRITON_BATTERY_REPORT_ID:
        return None
    percent = data[2]
    if percent > 100:
        return None  # firmware sends 0xFF-ish placeholders before it has a reading
    cs = data[1]
    charging = cs in (
        CHARGE_STATE_CHARGING,
        CHARGE_STATE_SOURCE_VALIDATE,
        CHARGE_STATE_CHARGING_DONE,
    )
    # A 0% reading while not charging isn't a real level — the firmware emits it
    # as the controller powers off (e.g. the Steam+Y turn-off) and before it has
    # taken a reading. Treat it as "no reading" so it can't fire a bogus 0%
    # critical-battery warning. (Mirrors the reference's IsDisplayableController-
    # Battery: a battery is real only if it's charging or percent > 0.)
    if percent == 0 and not charging:
        return None
    return SteamControllerBattery(
        percent=percent,
        charge_state=cs,
        charging=charging,
        charge_complete=cs == CHARGE_STATE_CHARGING_DONE,
        voltage_mv=data[3] | (data[4] << 8),
    )


def _parse_triton(data: bytes) -> SteamControllerInput | None:
    """Parse a 54-byte Triton input report into the SCI tuple."""
    if (
        len(data) < TRITON_INPUT_MIN_LEN
        or data[0] not in TRITON_INPUT_REPORT_IDS
    ):
        return None
    # Skip byte 0 (report ID 0x45 / legacy 0x42). Layout after that:
    #   B  seq            (1 byte)
    #   I  buttons        (4 bytes, uint32 LE)
    #   h  sTriggerLeft   (2 bytes, int16)
    #   h  sTriggerRight  (2 bytes, int16)
    #   h  sLeftStickX
    #   h  sLeftStickY
    #   h  sRightStickX
    #   h  sRightStickY
    #   h  sLeftPadX
    #   h  sLeftPadY
    #   H  sPressureLeft  (ignored)
    #   h  sRightPadX
    #   h  sRightPadY
    #   H  sPressureRight (ignored)
    (
        seq,
        buttons,
        ltrig,
        rtrig,
        lstick_x,
        lstick_y,
        rstick_x,
        rstick_y,
        lpad_x,
        lpad_y,
        _pL,
        rpad_x,
        rpad_y,
        _pR,
    ) = _TRITON_STRUCT.unpack_from(data, 1)
    # The press fields are s16 in the protocol; a negative reading is a
    # placeholder/artifact, never a real force — clamp to 0.
    return SteamControllerInput(
        status=SCStatus.INPUT,
        seq=seq,
        buttons=buttons,
        ltrig=ltrig,
        rtrig=rtrig,
        lpad_x=lpad_x,
        lpad_y=lpad_y,
        rpad_x=rpad_x,
        rpad_y=rpad_y,
        lstick_x=lstick_x,
        lstick_y=lstick_y,
        rstick_x=rstick_x,
        rstick_y=rstick_y,
        lpad_press=_pL if _pL < 32768 else 0,
        rpad_press=_pR if _pR < 32768 else 0,
    )
