from .constants import (
    FEATURE_REPORT_ID,
    FEATURE_REPORT_LEN,
    HID_OUTPUT_REPORT_LEN,
    ID_OUT_HAPTIC_LFO_TONE,
    ID_OUT_HAPTIC_STOP,
    ID_SET_SETTINGS_VALUES,
    LIZARD_MODE_OFF,
    LIZARD_MODE_ON,
    SETTING_LIZARD_MODE,
)


def _build_lizard_report(mode_value):
    """Build the 65-byte feature report that sets the LIZARD_MODE setting."""
    buf = bytearray(FEATURE_REPORT_LEN + 1)  # +1 for report ID prefix
    buf[0] = FEATURE_REPORT_ID
    buf[1] = ID_SET_SETTINGS_VALUES
    buf[2] = 3  # length: 1 ControllerSetting = 1+2 bytes
    buf[3] = SETTING_LIZARD_MODE  # settingNum
    buf[4] = mode_value & 0xFF  # settingValue low byte
    buf[5] = (mode_value >> 8) & 0xFF
    return list(buf)


DISABLE_LIZARD_REPORT = _build_lizard_report(LIZARD_MODE_OFF)
ENABLE_LIZARD_REPORT = _build_lizard_report(LIZARD_MODE_ON)


def _build_haptic_tone_report(actuator, freq_hz, gain, count=0x7FFF):
    """Build the 0x83 LFO-tone OUTPUT report: play `freq_hz` on `actuator`.
    `count` (bytes 5-6) is the burst length; 0x7FFF ~= continuous (until a
    stop), while a small value plays just a few cycles for a crisp click."""
    f = int(freq_hz) & 0xFFFF
    c = int(count) & 0xFFFF
    buf = bytearray(HID_OUTPUT_REPORT_LEN)  # 65 bytes, id included
    buf[0] = ID_OUT_HAPTIC_LFO_TONE
    buf[1] = actuator & 0xFF
    buf[2] = gain & 0xFF
    buf[3] = f & 0xFF
    buf[4] = (f >> 8) & 0xFF
    buf[5] = c & 0xFF
    buf[6] = (c >> 8) & 0xFF
    return bytes(buf)


def _build_haptic_stop_report(actuator):
    """Build the 0x82 stop OUTPUT report for `actuator`."""
    buf = bytearray(HID_OUTPUT_REPORT_LEN)  # 65 bytes, id included
    buf[0] = ID_OUT_HAPTIC_STOP
    buf[1] = actuator & 0xFF
    return bytes(buf)
