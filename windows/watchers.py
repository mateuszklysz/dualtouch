"""Steam Controller frame watcher (no input injection).

Watches the raw HID stream while the OSK is closed to (a) hold the
persistent SteamController HID handle open (so Steam Input never sees a
detach/re-enumerate), (b) end sc.run() cleanly when the app tears down,
and (c) open the OSK on the user's Steam+<button> chord (default Steam+X,
tray-configurable via sc_osk_open_chord).

All desktop controller input is left to Steam Input's own config (which is
always running): the app injects NO shortcuts of its own while the OSK is
closed.
"""

from steamcontroller import SCButtons, SCStatus


class _ChordState:
    """Shared chord state kept across sc.run() rebuilds so a mid-hold
    rebuild can't strand Alt held at the OS level. No chords are injected
    anymore; this only guards the teardown path."""

    def __init__(self):
        # True while LEFTALT is currently being held by us.
        self.alt_held = False

    def release_alt(self):
        self.alt_held = False

    def release_all_held(self):
        self.release_alt()


class _Watcher:
    def __init__(self, should_abort, chord=None, open_chord=SCButtons.X):
        # True once the Steam+<open_chord> press opens the OSK. Set on the
        # rising edge; the launcher checks it to decide whether to open.
        self.triggered = False
        self._should_abort = should_abort
        self._chord = chord if chord is not None else _ChordState()
        # Button (SCButtons bit) that opens the OSK when held with Steam
        # (Guide). Read from the raw HID, so the chord works even though the
        # app runs elevated (Steam Input's injected key chords can't reach an
        # elevated process — UIPI). Rising-edge latch: one press = one open.
        self._open_chord = open_chord
        self._open_chord_was_pressed = False

    def on_input(self, sc, sci):
        if sci.status != SCStatus.INPUT:
            return
        if self._should_abort():
            # Drop any held modifier so it doesn't stick at the OS level when
            # this watcher tears down (e.g. tray Exit / Steam launch).
            self._chord.release_all_held()
            sc.addExit()
            return

        steam_now = bool(sci.buttons & (SCButtons.STEAM | SCButtons.QAM))

        # Steam + <open_chord> opens the OSK (rising edge, one press = one
        # open). Release the controller here so triton can grab it; the
        # launcher checks self.triggered after sc.run() returns.
        chord_now = bool(sci.buttons & self._open_chord)
        if steam_now and chord_now and not self._open_chord_was_pressed:
            self.triggered = True
            sc.addExit()
        self._open_chord_was_pressed = chord_now
        # No other input handling: the desktop controller input belongs to
        # Steam Input's config, not the app.
