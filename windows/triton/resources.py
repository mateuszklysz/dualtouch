import os.path
import sys

_search_path_env_str = os.environ.get("TRITON_DATA")
# os.pathsep, not ":" — Windows drive letters contain a colon, so splitting on
# ":" turned "C:\...\data" into ("C", "\...\data") and the drive-relative
# second element only worked by accident when cwd was on the same drive as
# TRITON_DATA (it broke in the frozen exe, whose onefile temp dir is on C:).
env_search_paths = (
    tuple(_search_path_env_str.split(os.pathsep))
    if _search_path_env_str is not None
    else ()
)

# On Windows the bundled TRITON_DATA dir is always authoritative (tray/__init__
# forces it before any triton import), so the ~/.config/triton, ~/.triton and
# /etc/triton fallbacks — POSIX conventions — are dropped entirely: they are
# user-writable and could shadow the bundle if a bundled file ever goes
# missing. They only make sense on non-Windows.
if sys.platform == "win32":
    cfg_search_paths = ()
else:
    cfg_search_paths = (
        "~/.config/triton/",
        "~/.triton/",
        "/etc/triton/",
    )

static_search_paths = (f"{sys.prefix}/share/triton/",)


def find_cfg_resource(name):
    for path in env_search_paths:
        fullpath = f"{os.path.expanduser(path)}/cfg/{name}"
        if os.path.exists(fullpath):
            return fullpath
    for path in cfg_search_paths:
        fullpath = f"{os.path.expanduser(path)}/{name}"
        if os.path.exists(fullpath):
            return fullpath
    for path in static_search_paths:
        fullpath = f"{os.path.expanduser(path)}/cfg/{name}"
        if os.path.exists(fullpath):
            return fullpath
    return None


def find_data_resource(name):
    for path in env_search_paths:
        fullpath = f"{os.path.expanduser(path)}/{name}"
        if os.path.exists(fullpath):
            return fullpath
    for path in static_search_paths:
        fullpath = f"{os.path.expanduser(path)}/{name}"
        if os.path.exists(fullpath):
            return fullpath
    return None
