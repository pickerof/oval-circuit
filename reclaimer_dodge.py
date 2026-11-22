"""Compare short movement trajectories against observed projectile flight."""

from math import atan2, cos, hypot, pi, sin


def avoid(arena, obs, movement, boost, radius, height):
    me = obs["self"]
    pos = me["pos"]
    x, y, z = pos
    vx, vy, vz = me["vel"]
    stats = me["stats"]
    grounded = me["grounded"]
    speed = stats["boost_speed"] if boost and grounded else stats["walk_speed"]
    if any(w and w.get("charge", 0) for w in me["weapons"]):
        speed *= stats["charge_move_mult"]
    accel = stats["ground_accel"] if grounded else stats["air_accel"]
    relevant = []
    for shot in obs["projectiles"]:
        px, py, pz = shot["pos"]
        sx, sy, sz = shot["vel"]
        rx, ry = px - x, py - y
        s2 = sx * sx + sy * sy
        if s2 < 25 or hypot(rx, ry) > 165:
            continue
        crossing = -(rx * sx + ry * sy) / s2
        if not 0 < crossing < 0.68:
            continue
        mortar = shot["kind"] == "mortar"
        h = pz + sz * crossing - (11 * crossing * crossing if mortar else 0)
        if hypot(rx + sx * crossing, ry + sy * crossing) > 7 + speed * crossing:
            continue
        if not z - 6 <= h <= z + height + 8:
            continue
        relevant.append((crossing, mortar, shot))
    if not relevant:
        return movement

    _, _, shot = min(relevant, key=lambda row: row[0])
    heading = atan2(movement[1], movement[0])
    dx, dy = shot["vel"][:2]
    perpendicular = atan2(dy, dx) + pi / 2
    alternatives = [
        (cos(heading + i * pi / 4), sin(heading + i * pi / 4)) for i in (1, -1, 2, -2, 3, -3, 4)
    ]
    alternatives += [(cos(perpendicular), sin(perpendicular)), (-cos(perpendicular), -sin(perpendicular)), (0, 0)]
    options = [movement] + [
        p for p in alternatives if arena.walk_line(pos, (x + 6 * p[0], y + 6 * p[1]), radius, height)
    ]
    weights = []
    for eta, mortar, shot in sorted(relevant, key=lambda row: row[0])[:5]:
        px, py, pz = shot["pos"]
        sx, sy, sz = shot["vel"]
        times = [max(0, eta - 0.1), eta, eta + 0.1]
        if mortar:
            times += [eta + 0.2]
        positions = []
        for t in times:
            q = px + sx * t, py + sy * t, pz + sz * t - (11 * t * t if mortar else 0)
            if not mortar and not arena.visible(shot["pos"], q, stride=2.5):
                continue
            positions.append((t, q))
        if positions:
            weights.append((mortar, positions))

    def score(option):
        mx, my = option
        merit = hypot(mx - movement[0], my - movement[1]) * 1.45
        tx, ty = mx * speed, my * speed
        rx, ry = tx - vx, ty - vy
        dt = hypot(rx, ry) / accel
        for mortar, positions in weights:
            impact = 0
            for t, (px, py, pz) in positions:
                blend = min(t, dt)
                offset = (t - blend / 2) * blend / dt if dt > 1e-5 else 0
                nx = x + vx * t + rx * offset
                ny = y + vy * t + ry * offset
                nz = arena.support(nx, ny, z + 1.2) if grounded else z + vz * t - stats["gravity"] * t * t / 2
                hgap = max(0, nz - pz, pz - nz - height)
                gap = hypot(px - nx, py - ny, hgap)
                margin = radius + (7.6 if mortar else 1.0)
                impact = max(impact, max(0, margin - gap) * (5 if mortar else 7))
            merit += impact
        return merit

    return min(options, key=score)
