"""Steer away from short-horizon projectile intersections."""

from math import atan2, cos, hypot, pi, sin


def avoid(arena, obs, movement, boost, radius, height):
    me = obs["self"]
    x, y, z = me["pos"]
    vx, vy, _ = me["vel"]
    stats = me["stats"]
    horizon = 0.38
    speed = stats["boost_speed"] if boost and me["grounded"] else stats["walk_speed"]
    accel = stats["ground_accel"] if me["grounded"] else stats["air_accel"]
    threats = []
    for proj in obs["projectiles"]:
        px, py, pz = proj["pos"]
        pvx, pvy, pvz = proj["vel"]
        rx, ry = px - x, py - y
        rvx, rvy = pvx - vx, pvy - vy
        span = rvx * rvx + rvy * rvy
        if span < 100 or hypot(rx, ry) > 108:
            continue
        impact = -(rx * rvx + ry * rvy) / span
        if not 0.04 < impact < horizon:
            continue
        if hypot(rx + impact * rvx, ry + impact * rvy) > radius + 3.8:
            continue
        p_height = pz + pvz * impact
        if not z - 1.2 <= p_height <= z + height + 1.2:
            continue
        if not arena.visible(proj["pos"], (x + vx * impact, y + vy * impact, p_height), stride=3):
            continue
        threats.append((impact, rx, ry, rvx, rvy, 1.7 if proj["kind"] == "mortar" else 1))
    if not threats:
        return movement

    heading = atan2(movement[1], movement[0])
    length = hypot(*movement)
    options = [(0, movement)]
    for i in (1, -1, 2, -2, 3, -3):
        angle = heading + i * pi / 4
        direction = (cos(angle), sin(angle))
        if arena.walk_line(me["pos"], (x + 5 * direction[0], y + 5 * direction[1]), radius, height):
            options.append((abs(i) + (i % 2) * 0.05 + (1 - length) * 0.4, direction))

    def cost(option):
        penalty, (mx, my) = option
        dvx, dvy = mx * speed - vx, my * speed - vy
        delta = hypot(dvx, dvy)
        for t, rx, ry, rvx, rvy, weight in threats:
            if delta > 0.01:
                change_over = min(t, delta / accel)
                displacement = accel / delta * change_over * (t - change_over * 0.5)
                gap = hypot(rx + rvx * t - dvx * displacement,
                            ry + rvy * t - dvy * displacement)
            else:
                gap = hypot(rx + rvx * t, ry + rvy * t)
            penalty += weight * max(0, radius + 0.7 - gap) * 5
        return penalty

    turn, result = min(options, key=cost)
    return result if turn else movement
