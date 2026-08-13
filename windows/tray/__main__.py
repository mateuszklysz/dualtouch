import sys

from tray import _relaunch_elevated, main
from tray.helpers import _create_tray_mutex, _tray_mutex_held


def _run():
    # The scheduled task (DualTouchCursor) re-invokes THIS exe with
    # --cursor-helper to manipulate the interactive session cursors
    # non-elevated (see cursor_ctrl). Detect that early, before any
    # SDL/Steam/tray initialization, and hand off to the helper logic.
    if "--cursor-helper" in sys.argv:
        try:
            import cursor_helper

            sys.exit(cursor_helper.main())
        except Exception:
            sys.exit(1)

    # Self-elevate: the main tray needs admin (UIPI typing into games), but
    # the exe has no admin manifest (see build.py) so it can double as the
    # non-elevated cursor helper. If we are not elevated yet, relaunch via
    # ShellExecute runas (shows the UAC prompt) and let this instance exit.
    #
    # SINGLE-INSTANCE guard: only one tray may read the controller HID. Two
    # instances would both dispatch the same key press -> 2-3 letters at once.
    # The non-elevated launcher PROBES (does not own) the mutex so the elevated
    # child it spawns can take ownership without a handoff race; the elevated
    # tray owns the mutex for its whole lifetime.
    try:
        import ctypes

        if not ctypes.windll.shell32.IsUserAnAdmin():
            if _tray_mutex_held():
                sys.exit(0)  # an elevated tray is already running
            _relaunch_elevated()
            sys.exit(0)  # ALWAYS exit the parent; never run main() here
    except Exception:
        pass

    mutex = _create_tray_mutex()
    if mutex is None:
        sys.exit(0)  # another tray already owns the mutex
    try:
        main()
    finally:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    _run()
