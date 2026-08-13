from triton import state


class _TriggerMixin:
    def _osk_trigger_pressed(self, buttons, bit, analog):
        """True if the OSK should treat this trigger (L2/R2) as pressed for
        Shift/Enter. Always true on the firmware full-pull digital bit; with a
        lowered actuation set (sc_osk_trigger_actuation settings.json key) it
        also engages at a lighter analog pull (0..32767). The SC is gated out
        before this is called (sc_triggers_off in handle_input — trackpad click
        is the SC's insert), so the threshold applies to the trigger roles."""
        if buttons & bit:
            return True
        thr = state.get_sc_osk_trigger_threshold()
        if thr is None:
            return False
        return analog >= thr
