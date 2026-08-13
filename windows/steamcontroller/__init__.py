"""Windows port of the Steam Controller driver targeting the newer
"Triton" wireless adapter (PID 0x1304 — Valve internal codename Proteus).

The original ynsta/steamcontroller library targets the 2015 wired/wireless
SteamController (PID 0x1102/0x1142) with a 64-byte input report. The Triton
hardware uses a different format with state report ID 0x45 (newer firmware;
46 bytes) or the legacy 0x42 (USB/full state, 54 bytes) — same field layout,
see TRITON_INPUT_REPORT_IDS. Both layouts
were identified from Valve's open-source headers in libsdl-org/SDL
(src/joystick/hidapi/steam/controller_structs.h and the steam_triton
driver). This file maps the Triton wire format onto the small triton-facing
API surface (SteamController, SCButtons, SCStatus, SteamControllerInput,
SCI_NULL, EventMapper.process inputs).
"""

from .constants import (
    PRODUCT_ID_PROTEUS,
    PRODUCT_ID_WIRED,
    VENDOR_ID,
    SCButtons,
    SCStatus,
)
from .device import SteamController, present_product_ids
from .parse import SCI_NULL, SteamControllerBattery, SteamControllerInput

__all__ = [
    "PRODUCT_ID_PROTEUS",
    "PRODUCT_ID_WIRED",
    "VENDOR_ID",
    "SCButtons",
    "SCStatus",
    "SteamController",
    "present_product_ids",
    "SCI_NULL",
    "SteamControllerBattery",
    "SteamControllerInput",
]
