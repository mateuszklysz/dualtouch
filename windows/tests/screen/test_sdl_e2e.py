"""Layer 3 (opt-in): real Screen() + render under SDL's dummy video driver.

Run with DUALTOUCH_E2E_SDL=1 — skipped by default so the fast headless
suite stays deterministic on machines/CI where the SDL DLL or dummy
driver is unavailable. Exercises window creation, skin load and a full
render pass with the production input pipeline feeding it.
"""

import os
from contextlib import suppress

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DUALTOUCH_E2E_SDL") != "1",
    reason="opt-in: set DUALTOUCH_E2E_SDL=1",
)

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")


@pytest.fixture
def screen():
    import sdl3w as S
    from triton.screen import Screen, set_dims

    with suppress(Exception):
        S.TTF_Init()
    set_dims(1286, 369)
    scr = Screen()
    yield scr
    scr.destroy_textures()
    del scr


def test_screen_constructs_and_renders_headlessly(screen, runner):
    from steamcontroller import SCButtons
    from triton import state
    from triton.triton import drain_input_work

    state.set_virtual_kb(runner.virtual_kb)
    screen.maybe_reload_skin()

    # Type through the REAL pipeline, then paint one frame.
    state.set_cursor(*runner.cell_rc("a"))
    runner.push(buttons=SCButtons.A)
    runner.push()
    drain_input_work(runner.cstate, runner.virtual_kb)
    ptrs = runner.cstate.get_pointers()
    screen.render_vkb(runner.virtual_kb, ptrs)
    screen.clear()


def test_screen_survives_skin_reload_cycle(screen, runner):
    from triton import skins

    skins.set_active_skin(skins.DEFAULT_SKIN)
    from triton import state as _st

    kb = _st.get_virtual_kb()
    ptrs = runner.cstate.get_pointers()
    _st.set_virtual_kb(kb)
    screen.maybe_reload_skin()
    screen.content_changed(kb, ptrs)
    screen.render(kb, ptrs)
