"""Thin hand-rolled ctypes binding for the subset of SDL3 + SDL3_ttf this
project uses: core/video/window, renderer, surface+texture, TTF text, events.

SDL3 renamed/re-signatured much of SDL2 — the notable ones handled here:
  * SDL_CreateRGBSurfaceWithFormatFrom -> SDL_CreateSurfaceFrom (w,h,format,px,pitch)
  * SDL_FreeSurface                    -> SDL_DestroySurface
  * SDL_RenderCopy (int SDL_Rect)      -> SDL_RenderTexture (float SDL_FRect)
  * SDL_GetWindowWMInfo                -> window properties (win32 HWND pointer)
  * SDL_CreateWindow drops x,y         -> position set via SDL_SetWindowPosition
  * SDL_ScaleModeLinear                -> SDL_SCALEMODE_LINEAR
Window flags are Uint64 in SDL3, and most functions return bool (true=success).
"""

import ctypes

from sdl3w import _loader

SDL, TTF, DLL_DIR = _loader.load()


def _bind(lib, name, restype, argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


# ===========================================================================
# Types
# ===========================================================================
SDL_DisplayID = ctypes.c_uint32
SDL_PropertiesID = ctypes.c_uint32


class SDL_Rect(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
    ]


class SDL_FRect(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
        ("w", ctypes.c_float),
        ("h", ctypes.c_float),
    ]


class SDL_Color(ctypes.Structure):
    _fields_ = [
        ("r", ctypes.c_ubyte),
        ("g", ctypes.c_ubyte),
        ("b", ctypes.c_ubyte),
        ("a", ctypes.c_ubyte),
    ]


class SDL_Surface(ctypes.Structure):
    # SDL3 layout — we only read w/h (both precede the first pointer field, so
    # alignment padding after them is irrelevant).
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("format", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
        ("pitch", ctypes.c_int),
        ("pixels", ctypes.c_void_p),
        ("refcount", ctypes.c_int),
        ("reserved", ctypes.c_void_p),
    ]


# ===========================================================================
# Constants
# ===========================================================================
# SDL_InitFlags
SDL_INIT_VIDEO = 0x00000020
SDL_INIT_EVENTS = 0x00004000

# SDL_WindowFlags (Uint64)
SDL_WINDOW_HIDDEN = 0x0000000000000008
SDL_WINDOW_BORDERLESS = 0x0000000000000010
SDL_WINDOW_UTILITY = 0x0000000000020000
# Per-pixel-alpha (layered/composited) window — lets the OSK render with a
# transparent background and translucent keys (see screen.Screen + skins).
SDL_WINDOW_TRANSPARENT = 0x0000000040000000

SDL_WINDOWPOS_CENTERED = 0x2FFF0000

# Win32 HWND window property name.
SDL_PROP_WINDOW_WIN32_HWND_POINTER = b"SDL.window.win32.hwnd"

# Pixel format (same packed value as SDL2): ABGR8888 matches Pillow RGBA bytes
# on little-endian.
SDL_PIXELFORMAT_ABGR8888 = 0x16762004

# Scale / blend modes
SDL_SCALEMODE_LINEAR = 1
SDL_BLENDMODE_BLEND = 0x00000001
# Straight-alpha BLEND re-multiplies a texture's RGB by its alpha (and the
# alpha-mod) again at composite time; for a texture rendered onto a cleared
# (0,0,0,0) target with BLEND (which leaves it holding PREMULTIPLIED RGB —
# SDL's well-known render-to-transparent-texture quirk), that double-applies
# the alpha and darkens it. PREMULTIPLIED skips the redundant RGB multiply —
# used for the OSK open-animation's offscreen composite (see
# screen._ensure_anim_target).
SDL_BLENDMODE_BLEND_PREMULTIPLIED = 0x00000010

# SDL_TextureAccess — TARGET makes a texture usable as a render target
# (SDL_SetRenderTarget), e.g. the OSK open-animation offscreen buffer.
SDL_TEXTUREACCESS_TARGET = 2

# Event types
SDL_EVENT_QUIT = 0x100
SDL_EVENT_WINDOW_RESIZED = 0x206
SDL_EVENT_MOUSE_MOTION = 0x400
SDL_EVENT_MOUSE_BUTTON_DOWN = 0x401
SDL_EVENT_MOUSE_BUTTON_UP = 0x402

# Mouse buttons + button mask
SDL_BUTTON_LEFT = 1
SDL_BUTTON_RIGHT = 3
SDL_BUTTON_X1 = 4
SDL_BUTTON_X2 = 5
SDL_BUTTON_LMASK = 1 << (SDL_BUTTON_LEFT - 1)

# Hints
SDL_HINT_WINDOW_ACTIVATE_WHEN_SHOWN = b"SDL_WINDOW_ACTIVATE_WHEN_SHOWN"
SDL_HINT_TOUCH_MOUSE_EVENTS = b"SDL_TOUCH_MOUSE_EVENTS"


# ===========================================================================
# Event structs / union
# ===========================================================================
class SDL_CommonEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint64),
    ]


class SDL_WindowEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint64),
        ("windowID", ctypes.c_uint32),
        ("data1", ctypes.c_int32),
        ("data2", ctypes.c_int32),
    ]


class SDL_MouseMotionEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint64),
        ("windowID", ctypes.c_uint32),
        ("which", ctypes.c_uint32),
        ("state", ctypes.c_uint32),
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
        ("xrel", ctypes.c_float),
        ("yrel", ctypes.c_float),
    ]


class SDL_MouseButtonEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("timestamp", ctypes.c_uint64),
        ("windowID", ctypes.c_uint32),
        ("which", ctypes.c_uint32),
        ("button", ctypes.c_ubyte),
        ("down", ctypes.c_bool),
        ("clicks", ctypes.c_ubyte),
        ("padding", ctypes.c_ubyte),
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
    ]


class SDL_Event(ctypes.Union):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("common", SDL_CommonEvent),
        ("window", SDL_WindowEvent),
        ("motion", SDL_MouseMotionEvent),
        ("button", SDL_MouseButtonEvent),
        ("padding", ctypes.c_ubyte * 128),
    ]


# ===========================================================================
# Core
# ===========================================================================
SDL_InitSubSystem = _bind(
    SDL, "SDL_InitSubSystem", ctypes.c_bool, [ctypes.c_uint32]
)
SDL_QuitSubSystem = _bind(SDL, "SDL_QuitSubSystem", None, [ctypes.c_uint32])
SDL_GetError = _bind(SDL, "SDL_GetError", ctypes.c_char_p, [])
SDL_SetHint = _bind(
    SDL, "SDL_SetHint", ctypes.c_bool, [ctypes.c_char_p, ctypes.c_char_p]
)


def get_error():
    err = SDL_GetError()
    return err.decode("utf-8", "replace") if err else ""


# ===========================================================================
# Video / window
# ===========================================================================
SDL_GetPrimaryDisplay = _bind(SDL, "SDL_GetPrimaryDisplay", SDL_DisplayID, [])
SDL_GetDisplayUsableBounds = _bind(
    SDL,
    "SDL_GetDisplayUsableBounds",
    ctypes.c_bool,
    [SDL_DisplayID, ctypes.POINTER(SDL_Rect)],
)
SDL_GetDisplayBounds = _bind(
    SDL,
    "SDL_GetDisplayBounds",
    ctypes.c_bool,
    [SDL_DisplayID, ctypes.POINTER(SDL_Rect)],
)
SDL_CreateWindow = _bind(
    SDL,
    "SDL_CreateWindow",
    ctypes.c_void_p,
    [ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint64],
)
SDL_DestroyWindow = _bind(SDL, "SDL_DestroyWindow", None, [ctypes.c_void_p])
SDL_SetWindowPosition = _bind(
    SDL,
    "SDL_SetWindowPosition",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_int, ctypes.c_int],
)
SDL_GetWindowPosition = _bind(
    SDL,
    "SDL_GetWindowPosition",
    ctypes.c_bool,
    [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    ],
)
SDL_SetWindowSize = _bind(
    SDL,
    "SDL_SetWindowSize",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_int, ctypes.c_int],
)
SDL_ShowWindow = _bind(SDL, "SDL_ShowWindow", ctypes.c_bool, [ctypes.c_void_p])
SDL_HideWindow = _bind(SDL, "SDL_HideWindow", ctypes.c_bool, [ctypes.c_void_p])
SDL_SetWindowAlwaysOnTop = _bind(
    SDL,
    "SDL_SetWindowAlwaysOnTop",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_bool],
)
SDL_GetWindowProperties = _bind(
    SDL, "SDL_GetWindowProperties", SDL_PropertiesID, [ctypes.c_void_p]
)
SDL_GetPointerProperty = _bind(
    SDL,
    "SDL_GetPointerProperty",
    ctypes.c_void_p,
    [SDL_PropertiesID, ctypes.c_char_p, ctypes.c_void_p],
)


def get_win32_hwnd(window):
    """Return the Win32 HWND (as an int) backing an SDL window, or None."""
    props = SDL_GetWindowProperties(window)
    if not props:
        return None
    hwnd = SDL_GetPointerProperty(
        props, SDL_PROP_WINDOW_WIN32_HWND_POINTER, None
    )
    return int(hwnd) if hwnd else None


# ===========================================================================
# Renderer
# ===========================================================================
SDL_CreateRenderer = _bind(
    SDL,
    "SDL_CreateRenderer",
    ctypes.c_void_p,
    [ctypes.c_void_p, ctypes.c_char_p],
)
SDL_DestroyRenderer = _bind(
    SDL, "SDL_DestroyRenderer", None, [ctypes.c_void_p]
)
SDL_SetRenderDrawColor = _bind(
    SDL,
    "SDL_SetRenderDrawColor",
    ctypes.c_bool,
    [
        ctypes.c_void_p,
        ctypes.c_ubyte,
        ctypes.c_ubyte,
        ctypes.c_ubyte,
        ctypes.c_ubyte,
    ],
)
SDL_SetRenderDrawBlendMode = _bind(
    SDL,
    "SDL_SetRenderDrawBlendMode",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_uint],
)
SDL_RenderClear = _bind(
    SDL, "SDL_RenderClear", ctypes.c_bool, [ctypes.c_void_p]
)
# Clip rendering to a rect (int SDL_Rect); pass NULL/None to disable clipping.
SDL_SetRenderClipRect = _bind(
    SDL,
    "SDL_SetRenderClipRect",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.POINTER(SDL_Rect)],
)
SDL_RenderFillRect = _bind(
    SDL,
    "SDL_RenderFillRect",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.POINTER(SDL_FRect)],
)
SDL_RenderLine = _bind(
    SDL,
    "SDL_RenderLine",
    ctypes.c_bool,
    [
        ctypes.c_void_p,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
    ],
)
SDL_RenderTexture = _bind(
    SDL,
    "SDL_RenderTexture",
    ctypes.c_bool,
    [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(SDL_FRect),
        ctypes.POINTER(SDL_FRect),
    ],
)
SDL_RenderPresent = _bind(
    SDL, "SDL_RenderPresent", ctypes.c_bool, [ctypes.c_void_p]
)
# Render-to-texture: redirect drawing into a TARGET texture, or pass None to
# restore the window as the target. Used by the OSK open animation, which draws
# the keyboard into an offscreen texture at full opacity, then composites it to
# the window faded + clipped (the fade/reveal can't be done per-pixel + uniform
# at once on a layered Win32 window otherwise — see screen.render_open_anim).
SDL_SetRenderTarget = _bind(
    SDL,
    "SDL_SetRenderTarget",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_void_p],
)


# ===========================================================================
# Surface / texture
# ===========================================================================
SDL_CreateSurfaceFrom = _bind(
    SDL,
    "SDL_CreateSurfaceFrom",
    ctypes.POINTER(SDL_Surface),
    [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int],
)
SDL_DestroySurface = _bind(
    SDL, "SDL_DestroySurface", None, [ctypes.POINTER(SDL_Surface)]
)
SDL_CreateTexture = _bind(
    SDL,
    "SDL_CreateTexture",
    ctypes.c_void_p,
    [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ],
)
SDL_CreateTextureFromSurface = _bind(
    SDL,
    "SDL_CreateTextureFromSurface",
    ctypes.c_void_p,
    [ctypes.c_void_p, ctypes.POINTER(SDL_Surface)],
)
SDL_DestroyTexture = _bind(SDL, "SDL_DestroyTexture", None, [ctypes.c_void_p])
SDL_SetTextureScaleMode = _bind(
    SDL,
    "SDL_SetTextureScaleMode",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_int],
)
SDL_SetTextureBlendMode = _bind(
    SDL,
    "SDL_SetTextureBlendMode",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_uint],
)
SDL_SetTextureColorMod = _bind(
    SDL,
    "SDL_SetTextureColorMod",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte],
)
SDL_SetTextureAlphaMod = _bind(
    SDL,
    "SDL_SetTextureAlphaMod",
    ctypes.c_bool,
    [ctypes.c_void_p, ctypes.c_ubyte],
)


# ===========================================================================
# TTF text
# ===========================================================================
TTF_Init = _bind(TTF, "TTF_Init", ctypes.c_bool, [])
TTF_Quit = _bind(TTF, "TTF_Quit", None, [])
TTF_OpenFont = _bind(
    TTF, "TTF_OpenFont", ctypes.c_void_p, [ctypes.c_char_p, ctypes.c_float]
)
# SDL3_ttf: text + byte length (0 = NUL-terminated) + SDL_Color by value.
TTF_RenderText_Blended = _bind(
    TTF,
    "TTF_RenderText_Blended",
    ctypes.POINTER(SDL_Surface),
    [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, SDL_Color],
)


# ===========================================================================
# Events
# ===========================================================================
SDL_PollEvent = _bind(
    SDL, "SDL_PollEvent", ctypes.c_bool, [ctypes.POINTER(SDL_Event)]
)
