"""Map queries and ground routing for the Mech Royale bot."""

from array import array
from collections import defaultdict
from heapq import heappop, heappush
from math import ceil, floor, hypot
from pathlib import Path
from time import perf_counter
import json
import sys


def inside(x, y, edges):
    result = False
    for ax, ay, bx, by in edges:
        if (ay > y) != (by > y) and x < ax + (bx - ax) * (y - ay) / (by - ay):
            result = not result
    return result


def edges(ring):
    points = list(zip(ring[::2], ring[1::2]))
    return [(*a, *b) for a, b in zip(points, points[1:] + points[:1])]


def near_edge(x, y, ring, radius):
    radius_sq = radius * radius
    for ax, ay, bx, by in ring:
        if not (min(ax, bx) - radius < x < max(ax, bx) + radius):
            continue
        if not (min(ay, by) - radius < y < max(ay, by) + radius):
            continue
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        fraction = min(1, max(0, ((x - ax) * dx + (y - ay) * dy) / span)) if span else 0
        if (x - ax - fraction * dx) ** 2 + (y - ay - fraction * dy) ** 2 < radius_sq:
            return True
    return False


class Arena:
    bucket = 16
    grid = 6
    neighbors = ((1, 0, 1), (0, 1, 1), (-1, 0, 1), (0, -1, 1),
                 (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414))

    def __init__(self, directory, stats):
        directory = Path(directory) / "v2"
        world = json.loads((directory / "world.json").read_bytes())
        self.bounds = world["bounds"]
        meta = world["terrain"]
        self.x0, self.y0 = meta["minX"], meta["minY"]
        self.nx, self.ny, self.step = meta["nx"], meta["ny"], meta["step"]
        self.terrain = array("h", (directory / meta["file"]).read_bytes())
        if sys.byteorder != "little":
            self.terrain.byteswap()
        self.scale = meta["scale"]
        self.step_up = stats["step_up"]
        self.prisms = defaultdict(list)
        self.clearance = 14
        for prism in world["prisms"]:
            ring = prism["ring"]
            xs, ys = ring[::2], ring[1::2]
            left, right, bottom, top = min(xs), max(xs), min(ys), max(ys)
            item = (left, right, bottom, top, prism["base"], prism["top"],
                    edges(ring), [edges(hole) for hole in prism["holes"]])
            for i in range(floor((left - self.clearance) / self.bucket),
                           floor((right + self.clearance) / self.bucket) + 1):
                for j in range(floor((bottom - self.clearance) / self.bucket),
                               floor((top + self.clearance) / self.bucket) + 1):
                    self.prisms[i, j].append(item)
        self.walk_cache = {}

    def ground(self, x, y):
        i = max(0, min(self.nx - 1, int((x - self.x0) / self.step + 0.5)))
        j = max(0, min(self.ny - 1, int((y - self.y0) / self.step + 0.5)))
        return self.terrain[j * self.nx + i] * self.scale

    def support(self, x, y, ceiling):
        surface = self.ground(x, y)
        for left, right, bottom, top, _, roof, ring, holes in self.prisms.get(
                (floor(x / self.bucket), floor(y / self.bucket)), ()):
            if (surface < roof <= ceiling and left <= x <= right and bottom <= y <= top
                    and inside(x, y, ring) and not any(inside(x, y, hole) for hole in holes)):
                surface = roof
        return surface

    def clear(self, x, y, z, radius, height):
        if not (self.bounds["minX"] + radius <= x <= self.bounds["maxX"] - radius and
                self.bounds["minY"] + radius <= y <= self.bounds["maxY"] - radius):
            return False
        for left, right, bottom, top, base, roof, ring, holes in self.prisms.get(
                (floor(x / self.bucket), floor(y / self.bucket)), ()):
            if z >= roof - 0.05 or z + height <= base:
                continue
            if not (left - radius <= x <= right + radius and bottom - radius <= y <= top + radius):
                continue
            within = inside(x, y, ring) and not any(inside(x, y, hole) for hole in holes)
            if within or (radius and (near_edge(x, y, ring, radius) or
                                      any(near_edge(x, y, hole, radius) for hole in holes))):
                return False
        return True

    def visible(self, start, end, stride=3):
        x, y, z = start
        dx, dy, dz = end[0] - x, end[1] - y, end[2] - z
        count = max(1, ceil(hypot(dx, dy) / stride))
        for i in range(1, count):
            f = i / count
            sx, sy, sz = x + dx * f, y + dy * f, z + dz * f
            if sz < self.ground(sx, sy) - 0.15 or not self.clear(sx, sy, sz, 0, 0):
                return False
        return True

    def walk_line(self, start, end, radius, height):
        x, y, z = start
        dx, dy = end[0] - x, end[1] - y
        count = max(1, ceil(hypot(dx, dy) / 2))
        for i in range(1, count + 1):
            f = i / count
            sx, sy = x + dx * f, y + dy * f
            ground = self.ground(sx, sy)
            if ground > z + self.step_up + 0.25:
                return False
            if not self.clear(sx, sy, max(ground, z), radius, height):
                return False
            # Keep the present roof height while crossing upper geometry.
            if ground >= z - 2.5:
                z = ground
        return True

    def cell(self, point):
        return round(point[0] / self.grid), round(point[1] / self.grid)

    def point(self, cell):
        return cell[0] * self.grid, cell[1] * self.grid

    def open_cell(self, cell, radius, height):
        key = cell, radius, height
        if key not in self.walk_cache:
            if len(self.walk_cache) > 32000:
                self.walk_cache.clear()
            x, y = self.point(cell)
            self.walk_cache[key] = self.clear(x, y, self.ground(x, y), radius, height)
        return self.walk_cache[key]

    def nearest_open(self, point, radius, height, limit=4, reachable_from=None):
        x, y = self.cell(point)
        for r in range(limit + 1):
            ring = [(x + i, y + j) for i in range(-r, r + 1) for j in range(-r, r + 1)
                    if max(abs(i), abs(j)) == r]
            ring.sort(key=lambda cell: hypot(point[0] - cell[0] * self.grid,
                                             point[1] - cell[1] * self.grid))
            for cell in ring:
                if not self.open_cell(cell, radius, height):
                    continue
                if reachable_from is not None and not self.walk_line(
                        reachable_from, self.point(cell), max(1, radius - 0.45), height):
                    continue
                return cell
        return None

    def route(self, start, goal, radius, height, limit=330):
        start_cell = self.nearest_open(start, radius, height, reachable_from=start)
        goal_cell = self.nearest_open(goal, radius, height, limit=8)
        if start_cell is None or goal_cell is None:
            return []

        def distance(cell):
            return hypot(cell[0] - goal_cell[0], cell[1] - goal_cell[1])

        frontier = [(distance(start_cell), 0, start_cell)]
        parents = {start_cell: None}
        costs = {start_cell: 0}
        best = start_cell
        best_distance = distance(best)
        deadline = perf_counter() + 0.009
        for expanded in range(limit):
            if not frontier or (expanded % 4 == 0 and perf_counter() >= deadline):
                break
            _, traveled, cell = heappop(frontier)
            if traveled != costs[cell]:
                continue
            remaining = distance(cell)
            if remaining < best_distance:
                best, best_distance = cell, remaining
            if cell == goal_cell:
                break
            for dx, dy, cost in self.neighbors:
                neighbor = cell[0] + dx, cell[1] + dy
                new_cost = traveled + cost
                if new_cost >= costs.get(neighbor, float("inf")):
                    continue
                if not self.open_cell(neighbor, radius, height):
                    continue
                if dx and dy and not (self.open_cell((cell[0] + dx, cell[1]), radius, height) and
                                      self.open_cell((cell[0], cell[1] + dy), radius, height)):
                    continue
                ax, ay = self.point(cell)
                bx, by = self.point(neighbor)
                if not self.walk_line((ax, ay, self.ground(ax, ay)), (bx, by), radius, height):
                    continue
                costs[neighbor] = new_cost
                parents[neighbor] = cell
                heappush(frontier, (new_cost + distance(neighbor) * 1.5, new_cost, neighbor))

        path = []
        while best is not None:
            path.append(self.point(best))
            best = parents[best]
        return path[::-1]
