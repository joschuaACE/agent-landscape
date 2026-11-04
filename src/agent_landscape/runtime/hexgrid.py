"""Axial hex-grid helpers. Three nested hexagonal levels:

* cell      – one map tile (axial q, r)
* territory – one mission: a hexagon of ``TERRITORY_RADIUS`` cells; territory centers form a lattice
* region    – one topic area: a hexagon of ``REGION_RADIUS`` *territory units* (19 mission slots)

The lattice basis ``(2N+1, -N), (N, N+1)`` tiles hexagons of radius N without gaps or overlaps.
"""

from __future__ import annotations

from typing import Iterator

Hex = tuple[int, int]

DIRECTIONS: list[Hex] = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]

TERRITORY_RADIUS = 3  # each mission owns a hexagon of radius 3 (37 cells)
REGION_RADIUS = 2  # each region holds a hexagon of radius 2 territories (19 missions)


def _basis(n: int) -> tuple[Hex, Hex]:
    return (2 * n + 1, -n), (n, n + 1)


_TA, _TB = _basis(TERRITORY_RADIUS + 1)  # territory centers 9 cells apart → 2-cell belt between them
_RA, _RB = _basis(REGION_RADIUS)


def _lin(v: Hex, a: Hex, b: Hex) -> Hex:
    return v[0] * a[0] + v[1] * b[0], v[0] * a[1] + v[1] * b[1]


def add(a: Hex, b: Hex) -> Hex:
    return a[0] + b[0], a[1] + b[1]


def scale(a: Hex, k: int) -> Hex:
    return a[0] * k, a[1] * k


def distance(a: Hex, b: Hex) -> int:
    dq, dr = a[0] - b[0], a[1] - b[1]
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def ring(center: Hex, radius: int) -> Iterator[Hex]:
    if radius == 0:
        yield center
        return
    cell = add(center, scale(DIRECTIONS[4], radius))
    for i in range(6):
        for _ in range(radius):
            yield cell
            cell = add(cell, DIRECTIONS[i])


def spiral(center: Hex, max_radius: int) -> Iterator[Hex]:
    for k in range(max_radius + 1):
        yield from ring(center, k)


def nth_in_spiral(index: int) -> Hex:
    for i, c in enumerate(spiral((0, 0), 1000)):
        if i == index:
            return c
    raise IndexError(index)


def territory_cell(t: Hex) -> Hex:
    """Center cell of the territory at territory-lattice coordinate ``t``."""
    return _lin(t, _TA, _TB)


def region_center(index: int) -> Hex:
    """Territory-lattice coordinate of the index-th region's center (spiralling out from HQ)."""
    return _lin(nth_in_spiral(index), _RA, _RB)


def region_slots(index: int) -> list[Hex]:
    """Mission slots (territory-lattice coords) of a region, center first."""
    return list(spiral(region_center(index), REGION_RADIUS))
