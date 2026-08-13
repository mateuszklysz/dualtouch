"""Window-pixel coordinate space shared by the renderer and the input thread:
the current OSK width/height plus the CoordFraction value type.

CoordFraction is an (x, y) pair in fraction-of-window space, so pointer
positions survive window resizes without re-computation. The active
dimensions are published here by screen.py at window construction and on
resize (set_dims); everything that converts fractions <-> pixels reads
them from this module.
"""

# Active OSK window size in pixels (the base 1286x369 until the first
# Screen() construction — see screen.set_osk_size).
width = 1286
height = 369


def set_dims(w, h):
    global width, height
    width = int(w)
    height = int(h)


class CoordFraction:
    @staticmethod
    def from_absolute(x, y):
        return CoordFraction(x / width, y / height)

    def __init__(self, x_fraction, y_fraction):
        self.x_fraction = x_fraction
        self.y_fraction = y_fraction

    def to_absolute(self):
        return self.x_fraction * width, self.y_fraction * height

    def update_absolute(self, x, y):
        self.x_fraction = x / width
        self.y_fraction = y / height
