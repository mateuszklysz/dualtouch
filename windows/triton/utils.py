import math


def round_to_int(f):
    return int(round(f))


def clamp(i, min_val, max_val):
    return max(min_val, min(max_val, i))


def compute_lowpass(curr, prev, alpha):
    return prev + alpha * (curr - prev)


# --- iOS-style spring easing -------------------------------------------------
# Underdamped spring (m*x'' + c*x' + k*x = 0) step response from rest toward
# 1, solved in closed form. ζ (damping ratio) sets the bounce: 0.5 = pronounced,
# 0.7 = subtle, 1.0 = none. ω0 (natural angular frequency) sets speed; higher =
# faster. Time-based so it's frame-rate independent (identical at 60/120/144fps).
#
#   p(t) = 1 - e^(-ζ·ω0·t) · [ cos(ωd·t) + (ζ/√(1-ζ²))·sin(ωd·t) ]
#   ωd = ω0·√(1-ζ²)
#
# One exp + one sin + one cos per evaluation — ~1-2µs, fine for a single panel
# or a handful of keys per frame at 120fps.


def spring_p(t, zeta, omega0):
    """Spring step response 0→1 from rest (may overshoot for ζ<1)."""
    if t <= 0.0:
        return 0.0
    s = 1.0 - zeta * zeta
    wd = omega0 * math.sqrt(s)
    e = math.exp(-zeta * omega0 * t)
    return 1.0 - e * (
        math.cos(wd * t) + (zeta / math.sqrt(s)) * math.sin(wd * t)
    )


def ease_out_back(t, c1=1.70158):
    """Fixed-duration back-ease 0→1 with a subtle overshoot (~10% at c1≈1.7),
    for exact-duration animations that want a springy landing without the
    spring's variable settle time. c1=1.3 → ~6% overshoot (subtler)."""
    t = clamp(t, 0.0, 1.0)
    c3 = c1 + 1.0
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2
