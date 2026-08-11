"""A tiny 3D ASCII renderer for the live console.

Real geometry, not a canned animation: points on a surface are rotated by two
angles, projected with perspective, depth-sorted through a z-buffer, and shaded
by the angle between the surface normal and a fixed light. The shape morphs
with the pipeline so the picture is also a progress indicator.
"""

from __future__ import annotations

import math

RAMP = ".,-~:;=!*#$@"

# Precomputed trig for the surface parameters: this runs every frame, and
# recomputing sin/cos for a few thousand points at 12fps is wasteful.
_THETA = [t * 0.14 for t in range(int(2 * math.pi / 0.14) + 1)]
_PHI = [p * 0.05 for p in range(int(2 * math.pi / 0.05) + 1)]
_TRIG_T = [(math.cos(t), math.sin(t)) for t in _THETA]
_TRIG_P = [(math.cos(p), math.sin(p)) for p in _PHI]
# Latitude runs pole to pole; reusing the 0..2pi table would cover the sphere
# twice and leave the poles ragged.
_LAT = [-math.pi / 2 + i * 0.05 for i in range(int(math.pi / 0.05) + 1)]
_TRIG_LAT = [(math.cos(u), math.sin(u)) for u in _LAT]


def torus(width: int, height: int, a: float, b: float, r1: float = 1.0,
          r2: float = 2.0) -> list[str]:
    """A rotating torus. ``a`` and ``b`` are the two rotation angles."""
    cos_a, sin_a = math.cos(a), math.sin(a)
    cos_b, sin_b = math.cos(b), math.sin(b)

    k2 = 5.0
    k1 = width * k2 * 3 / (8 * (r1 + r2))

    cells = width * height
    screen = [" "] * cells
    depth = [0.0] * cells

    for cos_t, sin_t in _TRIG_T:
        circle_x = r2 + r1 * cos_t
        circle_y = r1 * sin_t
        for cos_p, sin_p in _TRIG_P:
            x = circle_x * (cos_b * cos_p + sin_a * sin_b * sin_p) - circle_y * cos_a * sin_b
            y = circle_x * (sin_b * cos_p - sin_a * cos_b * sin_p) + circle_y * cos_a * cos_b
            z = k2 + cos_a * circle_x * sin_p + circle_y * sin_a
            if z <= 0:
                continue
            ooz = 1.0 / z

            # Terminal cells are about twice as tall as they are wide.
            col = int(width / 2 + k1 * ooz * x)
            row = int(height / 2 - k1 * ooz * y * 0.5)
            if not (0 <= col < width and 0 <= row < height):
                continue

            light = (
                cos_p * cos_t * sin_b
                - cos_a * cos_t * sin_p
                - sin_a * sin_t
                + cos_b * (cos_a * sin_t - cos_t * sin_a * sin_p)
            )
            if light <= 0:
                continue

            index = row * width + col
            if ooz > depth[index]:
                depth[index] = ooz
                screen[index] = RAMP[min(len(RAMP) - 1, int(light * 8))]

    return ["".join(screen[r * width : (r + 1) * width]) for r in range(height)]


def sphere(width: int, height: int, a: float, b: float, radius: float = 2.0) -> list[str]:
    """A rotating shaded sphere -- the same machinery, one fewer parameter."""
    cos_a, sin_a = math.cos(a), math.sin(a)
    cos_b, sin_b = math.cos(b), math.sin(b)
    k2 = 6.0
    k1 = width * k2 * 3 / (10 * radius)

    cells = width * height
    screen = [" "] * cells
    depth = [0.0] * cells

    for cos_t, sin_t in _TRIG_LAT:            # latitude, -pi/2 .. pi/2
        for cos_p, sin_p in _TRIG_P:          # longitude, 0 .. 2pi
            nx = cos_t * cos_p
            ny = sin_t
            nz = cos_t * sin_p
            x0, y0, z0 = nx * radius, ny * radius, nz * radius

            # rotate about Y then X
            x1 = x0 * cos_b + z0 * sin_b
            z1 = -x0 * sin_b + z0 * cos_b
            y1 = y0 * cos_a - z1 * sin_a
            z2 = y0 * sin_a + z1 * cos_a + k2
            if z2 <= 0:
                continue
            ooz = 1.0 / z2
            col = int(width / 2 + k1 * ooz * x1)
            row = int(height / 2 - k1 * ooz * y1 * 0.5)
            if not (0 <= col < width and 0 <= row < height):
                continue

            # light from the upper-left front
            lx1 = nx * cos_b + nz * sin_b
            lz1 = -nx * sin_b + nz * cos_b
            ly1 = ny * cos_a - lz1 * sin_a
            lz2 = ny * sin_a + lz1 * cos_a
            light = (-0.5 * lx1 + 0.6 * ly1 + 0.62 * lz2)
            if light <= 0:
                continue

            index = row * width + col
            if ooz > depth[index]:
                depth[index] = ooz
                screen[index] = RAMP[min(len(RAMP) - 1, int(light * 9))]

    return ["".join(screen[r * width : (r + 1) * width]) for r in range(height)]



SHAPES = {"torus": torus, "sphere": sphere}


def render(shape: str, width: int, height: int, a: float, b: float) -> list[str]:
    return SHAPES.get(shape, torus)(width, height, a, b)
