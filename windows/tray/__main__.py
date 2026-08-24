import os
import sys

from tray import _relaunch_elevated, main
from tray.helpers import _create_tray_mutex, _tray_mutex_held


def _trace_base():
    try:
        from applog import user_data_dir

        return user_data_dir()
    except Exception:
        return os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "DualTouch"
        )


def _write_crash_log():
    """Persist the active exception for post-mortem. A windowed exe has no
    stderr, so without this any startup crash is completely invisible."""
    import traceback

    text = traceback.format_exc()
    try:
        base = _trace_base()
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "crash.log"), "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n\n")
    except OSError:
        pass


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
        # A failed elevation check used to vanish silently (windowed exe);
        # leave evidence, then keep the original fall-through behavior.
        _write_crash_log()

    mutex = _create_tray_mutex()
    if mutex is None:
        sys.exit(0)  # another tray already owns the mutex
    try:
        main()
    except BaseException:
        _write_crash_log()
        raise
    finally:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    _run()
