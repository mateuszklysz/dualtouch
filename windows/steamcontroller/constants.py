from enum import IntEnum

VENDOR_ID = 0x28DE
PRODUCT_ID_PROTEUS = 0x1304  # Steam Controller Puck / Triton (wireless dongle)
PRODUCT_ID_WIRED = 0x1302  # Steam Controller 2026 (wired USB)

# Triton input ("state") report IDs. A firmware update bumped the primary
# REPORT_STATE id from 0x42 to 0x45 (the BLE / "no-quaternion" variant); wired
# units can still emit the legacy 0x42 (USB / full-state). The byte layout after
# the id is IDENTICAL for both, so accept EITHER — mirroring the reference
# project's IsStateReportId(), which treats 0x45 and 0x42 the same. Gating on a
# single id (and on the full 54-byte length) is what broke input after the
# update: the 0x45 report can be shorter than the USB one, so we only require
# enough bytes to unpack the fields (TRITON_INPUT_MIN_LEN), not the full length.
TRITON_INPUT_REPORT_IDS = (0x45, 0x42)
TRITON_INPUT_MIN_LEN = 30  # bytes needed to unpack the SCI fields below

# Power/battery status report (HID input report id 0x43, 17 bytes). It streams
# interleaved with the game-input reports on the same vendor interface. Layout
# after the report id byte: [1]=charge state, [2]=battery percent (0..100),
# [3:5]=battery voltage mV (uint16 LE), then system/input voltage, current and
# temperature (unused here). Charge-state values and the percent/voltage offsets
# were taken from Valve's SDL steam_triton driver and the Bloss battery-indicator
# reference (GamepadBatteryParser.TryParseSteamTritonBatteryStatus).
TRITON_BATTERY_REPORT_ID = 0x43
TRITON_BATTERY_REPORT_LEN = 17

# report[1] charge-state byte values.
CHARGE_STATE_DISCHARGING = 1
CHARGE_STATE_CHARGING = 2
CHARGE_STATE_SOURCE_VALIDATE = 3
CHARGE_STATE_CHARGING_DONE = 4

# Treat the battery as unknown if no input/battery frame has arrived for this
# long. The controller streams input continuously while connected, so a gap this
# big means it powered off (Steam+Y) or dropped its wireless link — even though
# the dongle is still plugged in. Keeps the tray from showing a stale %.
BATTERY_FRESH_SECONDS = 4.0

# Wireless link-status reports (byte[1]: 1=disconnected, 2=connected). We skip
# them in the read loop so they don't get mis-parsed as input frames.
TRITON_WIRELESS_STATUS_IDS = (0x46, 0x79)

# Feature-report commands (sent via send_feature_report with report ID 1)
FEATURE_REPORT_ID = 0x01
FEATURE_REPORT_LEN = 64

ID_SET_SETTINGS_VALUES = 0x87
SETTING_LIZARD_MODE = 9
LIZARD_MODE_OFF = 0
LIZARD_MODE_ON = 1

# Haptics. Unlike lizard/turn-off (feature reports), haptics are HID OUTPUT
# reports sent with a plain write (byte 0 = report ID, 65-byte buffer). Format
# and actuator mapping confirmed on real 2026 hardware by the SteamHapticsSinger
# project: 0x83 plays an LFO tone on one actuator, 0x82 stops it.
HID_OUTPUT_REPORT_LEN = 65
ID_OUT_HAPTIC_LFO_TONE = (
    0x83  # play a tone: [id, actuator, gain, freqLo, freqHi, 0xFF, 0x7F]
)
ID_OUT_HAPTIC_STOP = 0x82  # stop an actuator: [id, actuator]

# Actuator indices (no-swap mapping from SteamHapticsSinger):
HAPTIC_PAD_LEFT = 0  # left trackpad
HAPTIC_PAD_RIGHT = 1  # right trackpad
HAPTIC_RUMBLE_LEFT = 3  # left back rumble motor
HAPTIC_RUMBLE_RIGHT = 4  # right back rumble motor

# Tone gain is a signed int8: nearer +127 is loudest, more-negative is quieter
# (the changelog warns the loud end can damage the motors). SteamHapticsSinger
# ships -2 (0xFE) for audible music; UI ticks want much less, so HAPTIC_CLICK_GAIN
# is well down the scale for a light tap.
# Gain is ~dB-like and steep: -2 is near full blast, -80 is inaudible. A light
# but feelable click sits near the top; the SHORT burst count keeps it clicky.
HAPTIC_CLICK_GAIN = -6
# The simulated trackpad-click (a physical pad press) gets its own tick: a
# short, crisp, slightly firmer pop than the light key tap. Kept high-frequency
# and short so it reads as a real button click, not a deep buzz.
HAPTIC_PAD_CLICK_GAIN = -5
HAPTIC_PAD_CLICK_FREQ = 500
# Watchdog: the controller re-enables lizard mode if we don't keep disabling
# it. SDL re-sends every 3s; we use a slightly tighter interval to be safe.
LIZARD_REFRESH_SECONDS = 2.0


class SCStatus(IntEnum):
    INPUT = 0x42  # Triton input-state report type


# Button bit assignments — Triton-specific. Names map to what triton's
# controller.py expects (LGRIP, LB, RB, A, B, LPADTOUCH, RPADTOUCH, LT, RT).
# Source: TritonButtons enum in SDL_hidapi_steam_triton.c
class SCButtons(IntEnum):
    # Face buttons
    A = 0x00000001
    B = 0x00000002
    X = 0x00000004
    Y = 0x00000008
    # Right cluster
    QAM = 0x00000010
    R3 = 0x00000020  # right stick click
    VIEW = 0x00000040  # select/view/back
    RGRIP1 = 0x00000080  # right back paddle (Triton R4)
    RGRIP2 = 0x00000100  # right back paddle (Triton R5)
    RB = 0x00000200  # right bumper
    DPAD_DOWN = 0x00000400
    DPAD_RIGHT = 0x00000800
    DPAD_LEFT = 0x00001000
    DPAD_UP = 0x00002000
    START = 0x00004000  # menu
    L3 = 0x00008000  # left stick click
    STEAM = 0x00010000
    LGRIP1 = 0x00020000  # left back paddle (Triton L4) — bound to KEY_LEFTSHIFT in triton
    LGRIP2 = 0x00040000  # left back paddle (Triton L5)
    LB = 0x00080000  # left bumper
    RPADJOY_TOUCH = 0x00100000  # right joystick touch
    RPADTOUCH = 0x00200000  # right trackpad touch
    RPAD = 0x00400000  # right trackpad click
    RT = 0x00800000  # right trigger digital click (full pull)
    LPADJOY_TOUCH = 0x01000000  # left joystick touch
    LPADTOUCH = 0x02000000  # left trackpad touch
    LPAD = 0x04000000  # left trackpad click
    LT = 0x08000000  # left trigger digital click
    RGRIP_REST = 0x10000000  # right grip touch (always-on resting)
    LGRIP_REST = 0x20000000  # left grip touch
    # triton expects an "LGRIP" alias — combined mask for either left paddle.
    LGRIP = 0x00060000  # LGRIP1 (L4) | LGRIP2 (L5)
    RGRIP = 0x00000180  # RGRIP1 (R4) | RGRIP2 (R5)


# HID-open retry: toggling the block setting (block_sc_hid) closes the current
# handle and immediately reopens in the other mode (shared<->exclusive). The OS
# can take a moment to release the just-closed handle, so the first reopen can
# hit a transient sharing violation in EITHER direction. Retry a few times so
# the toggle applies live (turning the block both on AND off) instead of only
# after a restart.
# Retries run only on a *failed* open, so a cold/first open — the normal case,
# and every interface probe in _open_first_responsive — pays nothing.
OPEN_RETRY_ATTEMPTS = 5
OPEN_RETRY_DELAY = 0.1
