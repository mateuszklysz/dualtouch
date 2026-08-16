"""Tray menu construction."""

import pystray
from appsettings import _SC_OSK_OPEN_CHORDS
from triton import skins as triton_skins


def build_menu(app):
    # Transparency: a collapsible submenu with Off + three opacity levels
    # (radio). The levels scale the whole transparent look uniformly — Low is
    # 30% more opaque, High 30% more transparent, than the tuned Medium.
    transparent_submenu = pystray.Menu(
        pystray.MenuItem(
            "Off",
            app.select_transparency("off"),
            checked=app.is_transparency_checked("off"),
            radio=True,
        ),
        pystray.MenuItem(
            "Low",
            app.select_transparency("low"),
            checked=app.is_transparency_checked("low"),
            radio=True,
        ),
        pystray.MenuItem(
            "Medium",
            app.select_transparency("medium"),
            checked=app.is_transparency_checked("medium"),
            radio=True,
        ),
        pystray.MenuItem(
            "High",
            app.select_transparency("high"),
            checked=app.is_transparency_checked("high"),
            radio=True,
        ),
    )

    # OSK window size: "Small" (less screen blocked), "Default" (the original
    # 1286x369 size), "Full Screen" (fills the display - good for a Steam Deck).
    size_submenu = pystray.Menu(
        pystray.MenuItem(
            "Small",
            app.select_osk_size("small"),
            checked=app.is_osk_size_checked("small"),
            radio=True,
        ),
        pystray.MenuItem(
            "Default",
            app.select_osk_size("medium"),
            checked=app.is_osk_size_checked("medium"),
            radio=True,
        ),
        pystray.MenuItem(
            "Full Screen",
            app.select_osk_size("full"),
            checked=app.is_osk_size_checked("full"),
            radio=True,
        ),
    )

    # Steam on-screen-keyboard skins (radio; applied on the next OSK open).
    # The "Size", "Transparent" and "Split Keyboard" items sit at the top,
    # above the skin list.
    skin_submenu = pystray.Menu(
        pystray.MenuItem("Size", size_submenu),
        pystray.MenuItem("Transparent", transparent_submenu),
        pystray.MenuItem(
            "Split Keyboard",
            app.toggle_split_layout,
            checked=app.is_split_layout_checked,
        ),
        pystray.Menu.SEPARATOR,
        *[
            pystray.MenuItem(
                name,
                app.select_skin(name),
                checked=app.is_skin_checked(name),
                radio=True,
            )
            for name in triton_skins.available_skins()
        ],
    )

    # Startup-related settings, grouped under one submenu. (Steam is required
    # — the app coexists with it; there's no "when Steam is running" choice.)
    # OSK-open chord (Steam+<button> opens the keyboard): radio list of the
    # buttons the watcher accepts as the chord companion. Default Steam+X.
    chord_submenu = pystray.Menu(
        *[
            pystray.MenuItem(
                name,
                app.select_osk_open_chord(name),
                checked=app.is_osk_open_chord_checked(name),
                radio=True,
            )
            for name in _SC_OSK_OPEN_CHORDS
        ]
    )

    # Steam Controller settings, all live-editable (saved to settings.json and
    # applied immediately): key click sound, trackpad-click insert, and the
    # click button (bumpers vs triggers).
    click_button_submenu = pystray.Menu(
        pystray.MenuItem(
            "L1/R1 (bumpers)",
            app.select_click_button("L1/R1"),
            checked=app.is_click_button_checked("L1/R1"),
            radio=True,
        ),
        pystray.MenuItem(
            "L2/R2 (triggers)",
            app.select_click_button("L2/R2"),
            checked=app.is_click_button_checked("L2/R2"),
            radio=True,
        ),
    )

    sc_submenu = pystray.Menu(
        pystray.MenuItem(
            "Key Click Sound",
            app.toggle_key_sound,
            checked=app.is_key_sound_checked,
        ),
        pystray.MenuItem(
            "Trackpad Click Inserts Key",
            app.toggle_pad_click_enter,
            checked=app.is_pad_click_enter_checked,
        ),
        pystray.MenuItem("Click Button", click_button_submenu),
    )

    # Diacritic variants (Feature B: hold a letter to pick accented variants):
    # a master on/off plus the active locale (radio over "auto" = the Windows
    # keyboard layout, or one of the locales in the variant map).
    diacritic_locale_submenu = pystray.Menu(
        pystray.MenuItem(
            "Auto (Windows layout)",
            app.select_diacritic_locale("auto"),
            checked=app.is_diacritic_locale_checked("auto"),
            radio=True,
        ),
        *[
            pystray.MenuItem(
                locale,
                app.select_diacritic_locale(locale),
                checked=app.is_diacritic_locale_checked(locale),
                radio=True,
            )
            for locale in app.diacritic_locale_options()
            if locale != "auto"
        ],
    )

    diacritics_submenu = pystray.Menu(
        pystray.MenuItem(
            "Accented Variants",
            app.toggle_diacritics,
            checked=app.is_diacritics_checked,
        ),
        pystray.MenuItem("Locale", diacritic_locale_submenu),
    )

    startup_submenu = pystray.Menu(
        pystray.MenuItem(
            "Start with Windows",
            app.toggle_start_with_windows,
            checked=app.is_start_with_windows_checked,
        ),
        pystray.MenuItem(
            "Enable Logging",
            app.toggle_logging,
            checked=app.is_logging_checked,
        ),
        pystray.MenuItem("View Log", app.view_log),
        pystray.MenuItem("Open Keyboard Chord", chord_submenu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Steam Keyboard Layer: auto-switch",
            app.toggle_steam_kbd_layer,
            checked=app.is_steam_kbd_layer_checked,
        ),
        pystray.MenuItem(
            "Register Steam Keyboard Layer",
            app.register_steam_kbd_layer,
        ),
    )

    menu = pystray.Menu(
        pystray.MenuItem(
            app.battery_menu_label,
            None,
            enabled=False,
            visible=app.is_battery_known,
        ),
        pystray.MenuItem(
            app._kbd_menu_label,
            app.open_or_close_keyboard,
            # Default action: a single LEFT-click on the tray icon invokes this
            # item (pystray's win32 backend fires the default menu item on
            # WM_LBUTTONUP). Right-click still shows the full menu.
            default=True,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Startup", startup_submenu),
        pystray.MenuItem("Steam Controller", sc_submenu),
        pystray.MenuItem("Diacritics", diacritics_submenu),
        pystray.MenuItem("Keyboard Skin", skin_submenu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", app.exit_app),
    )
    return menu
