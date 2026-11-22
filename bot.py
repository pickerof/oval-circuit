"""Survival, loot, and combat policy for Mech Royale."""

from collections import deque
from math import asin, atan2, cos, hypot, pi, sin, sqrt

from arena import Arena
from dodge import avoid
import mechfight


WEAPON_VALUE = {"gun": 0, "mortar": 1, "lightning": 2, "laser": 3}
UPGRADE_VALUE = {
    "nanites": 14, "servos": 9, "gun_mag": 8.5, "gun_damage": 8,
    "plating": 10, "armor": 9.5, "laser_damage": 6.8, "laser_charge": 6.5,
    "gun_rate": 6, "reactor": 5.5, "lightning_range": 5,
    "gun_velocity": 4.5, "jets": 4, "capacitor": 3.5, "sensors": 3,
    "gyros": 2.5, "lightning_efficiency": 2, "mortar_reload": 1.5,
}


def distance(a, b):
    return hypot(a[0] - b[0], a[1] - b[1])


def bearing(a, b):
    return atan2(b[1] - a[1], b[0] - a[0])


def angle_error(a, b):
    return abs((a - b + pi) % (2 * pi) - pi)


def toward(a, b, length):
    d = distance(a, b)
    if d <= 0.001:
        return tuple(a[:2])
    return (a[0] + (b[0] - a[0]) * length / d,
            a[1] + (b[1] - a[1]) * length / d)


def enemy_richer(enemy, me):
    return enemy.get("hp", 1400) > me["hp"] * 1.45 and enemy["cubes"] > me["cubes"] + 3


class Bot:
    name = "Bing Shing"

    def __init__(self, hello):
        self.config = hello["config"]
        self.arena = Arena(hello["map"]["dir"], self.config["mech"])
        self.dt = self.config["match"]["decide_every"] / self.config["match"]["tick_hz"]
        self.route = []
        self.route_goal = None
        self.route_time = -10
        self.tactical_goal = None
        self.tactical_time = -10
        self.location = None
        self.stuck = 0
        self.progress_anchor = None
        self.progress_time = 0
        self.escape_goal = None
        self.escape_until = 0
        self.escape_history = []
        self.force_climb = False
        self.zone_progress = None
        self.zone_escape = None
        self.zone_jumped = False
        self.last_damage = {}
        self.recent_fire = deque()
        self.damage_time = -100
        self.damage_bearing = None
        self.heard_fire = None
        self.firing_time = -100
        self.target_id = None
        self.ffa = False
        self.alive_seen = None
        self.other_losses = 0
        self.peer_hp = {}
        self.foreign_beams = set()
        self.peer_wounded = {}
        self.busy_peers = set()
        self.motion = {}
        self.acceleration = {}
        self.enemy_memory = {}
        self.withdraw_until = -100
        self.shelter = None
        self.shelter_time = -100
        self.shelter_costs = {}
        self.tactical_retreat = None
        self.known_loot = {}
        self.loot_clock = -100
        self.loot_plan = None
        self.mortar_cooloff = -100
        self.scout_start = None
        self.scout_time = -100
        self.search_goal = None
        self.search_time = -100
        self.search_history = deque(maxlen=9)
        self.sprinting = False
        self.trade_window = deque()
        self.incoming = 0.0
        self.outgoing = 0.0

    def zone_goal(self, pos, zone):
        center = zone["next_center"]
        radius = zone["next_radius"]
        if radius <= 3 and zone["radius"] < 28:
            return tuple(center)
        target_radius = max(4, radius - min(24, radius * 0.22))
        if distance(pos, center) <= target_radius:
            return tuple(pos[:2])
        return toward(center, pos, target_radius)

    def zone_cost(self, point, zone, t):
        distance_now = distance(point, zone["center"])
        distance_next = distance(point, zone["next_center"])
        future_weight = min(1, max(0.12, (18 + t - zone["shrink_start"]) / 36))
        return (max(0, distance_now - zone["radius"] + 4) * 5 +
                max(0, distance_next - zone["next_radius"] + min(14, zone["next_radius"] * 0.25))
                * 2.2 * future_weight)

    def shape(self, actor):
        if actor["kind"] == "mech":
            factor = 1 + actor.get("cubes", 0) * self.config["cubes"]["size_per_cube"]
            return self.config["mech"]["radius"] * factor, self.config["mech"]["height"] * factor
        stats = self.config["npc" if actor["kind"] == "npc" else "crates"]
        return stats["radius"], stats["height"]

    def observe_opponents(self, obs, enemies):
        if self.config["match"]["mechs"] <= 2:
            return
        t = obs["t"]
        alive = obs["alive"]
        if not self.ffa and t < 58 and self.alive_seen is not None:
            kills = sum(event["type"] == "kill" for event in obs["events"])
            self.other_losses += max(0, self.alive_seen - alive - kills)
        self.alive_seen = alive
        hp = {enemy["id"]: enemy["hp"] for enemy in enemies if "hp" in enemy}
        hit_by_me = {}
        for event in obs["events"]:
            if event["type"] == "hit":
                target_id = event["target"]
                hit_by_me[target_id] = hit_by_me.get(target_id, 0) + event["amount"]
        wounded = [
            enemy for enemy in enemies if enemy["id"] in hp and
            self.peer_hp.get(enemy["id"], 0) - hp[enemy["id"]] - hit_by_me.get(enemy["id"], 0) >= 35
        ]
        self.peer_hp = hp
        for enemy in wounded:
            self.peer_wounded[enemy["id"]] = t
        if not self.ffa:
            for beam in obs.get("beams", ()):
                if beam["path"] and any(e["id"] != beam["by"] and distance(e["pos"], beam["path"][-1]) < 5
                                        for e in wounded):
                    self.foreign_beams.add(beam["by"])
            self.ffa = self.other_losses >= 2 or len(self.foreign_beams) >= 2
        self.busy_peers.clear()
        if not self.ffa or alive <= 3:
            return
        for i, enemy in enumerate(enemies):
            for peer in enemies[i + 1:]:
                if distance(enemy["pos"], peer["pos"]) > 145:
                    continue
                yaw = bearing(enemy["pos"], peer["pos"])
                if (angle_error(enemy["aim"][0], yaw) < 0.23 and
                        angle_error(peer["aim"][0], yaw + pi) < 0.23):
                    self.busy_peers.update((enemy["id"], peer["id"]))

    def opportunity(self, obs, enemy, d):
        if not self.ffa or obs["alive"] <= 3 or d > 160:
            return 0
        enemy_id = enemy["id"]
        hp = enemy.get("hp", 1400)
        if hp < 325:
            return 38
        me = obs["self"]
        if angle_error(enemy["aim"][0], bearing(enemy["pos"], me["pos"])) < 0.7:
            return 0
        wounded = obs["t"] - self.peer_wounded.get(enemy_id, -100) < 3
        if (wounded or enemy_id in self.busy_peers) and hp < min(950, me["hp"] * 1.1):
            return 25 + (15 if wounded else 0)
        return 0

    def take_fight(self, obs, enemy, d):
        me = obs["self"]
        hp = enemy.get("hp", 1400)
        if obs["alive"] <= 2 or d < 36 or hp < min(420, me["hp"] * 0.5):
            return True
        if obs["t"] - self.last_damage.get(enemy["id"], -100) < 3.4:
            return True
        if d < 150 and enemy.get("charging") and me["hp"] >= hp * 0.88:
            return True
        behind = angle_error(enemy["aim"][0], bearing(enemy["pos"], me["pos"])) > 1.3
        return d < 126 and me["hp"] > hp * (0.83 if behind else 1.05)

    def rear_guard(self, obs, target, aim):
        """Face the biggest immediate threat when another target would expose our rear."""
        t = obs["t"]
        while self.recent_fire and t - self.recent_fire[0][0] > 1.35:
            self.recent_fire.popleft()
        incoming = {}
        for time, attacker, yaw, amount in self.recent_fire:
            weight = amount * max(0, 1 - (t - time) / 1.35)
            old = incoming.get(attacker, (0, yaw))
            incoming[attacker] = old[0] + weight, yaw
        if not incoming:
            return None
        attacker = max(incoming, key=lambda i: incoming[i][0])
        damage, yaw = incoming[attacker]
        if target and attacker == target["id"]:
            return None
        target_pressure = incoming.get(target["id"], (0, 0))[0] if target else 0
        if damage < max(55, target_pressure * 1.45):
            return None
        if aim is not None and angle_error(yaw, aim[0]) < 1.23:
            return None
        return yaw

    def listen(self, obs, target):
        t, me = obs["t"], obs["self"]
        if obs["alive"] <= 4 or (target and target["kind"] == "mech"):
            self.heard_fire = None
            return None
        for sound in obs.get("sounds", ()):
            yaw = sound["bearing"]
            if (sound["dist"] == "near" and sound["age"] <= self.dt * 2 and
                    sound["kind"] in ("gun", "laser_charge") and
                    angle_error(yaw, me["aim"][0]) > 0.93):
                self.heard_fire = t + 0.72, yaw
                break
        if self.heard_fire and t <= self.heard_fire[0]:
            return self.heard_fire[1]
        return None

    def observe_motion(self, t, enemies):
        next_motion = {}
        next_acceleration = {}
        for enemy in enemies:
            enemy_id = enemy["id"]
            velocity = enemy["vel"]
            previous = self.motion.get(enemy_id)
            if previous and 0 < t - previous[0] < self.dt * 1.5:
                elapsed, last = t - previous[0], previous[1]
                old = self.acceleration.get(enemy_id, (0, 0))
                next_acceleration[enemy_id] = tuple(
                    0.56 * value + 0.44 * max(-48, min(48, (v - prior) / elapsed))
                    for value, v, prior in zip(old, velocity, last)
                )
            next_motion[enemy_id] = t, velocity
        self.motion, self.acceleration = next_motion, next_acceleration

    def projected_body(self, target, time, airborne, radius, height):
        x, y, z = target["pos"]
        vx, vy, vz = target["vel"]
        ax, ay = self.acceleration.get(target["id"], (0, 0))
        # Extrapolate acceleration briefly. The farther part of the shot assumes a straight run.
        span = min(time, 0.16)
        lead = span * (time - span * 0.5)
        px, py = x + vx * time + ax * lead, y + vy * time + ay * lead
        if airborne:
            gravity = 0.0 if vz > 4 else self.config["mech"]["gravity"]
            z += vz * time - gravity * time * time * 0.5
        floor = self.arena.support(px, py, max(z, target["pos"][2]) + 1.1)
        if not airborne or z < floor:
            z = floor
        if not self.arena.clear(px, py, z, radius, height):
            # A wall often clips only one component of a sliding target's velocity.
            if self.arena.clear(px, y, z, radius, height):
                py = y
            elif self.arena.clear(x, py, z, radius, height):
                px = x
            else:
                px, py, z = target["pos"]
        return px, py, z

    def target(self, obs, actors, muzzle):
        me = obs["self"]
        pos = me["pos"]
        maximum = max((w["stats"].get("range", 170) for w in me["weapons"] if w), default=0)
        visible = []
        for actor in actors:
            d = distance(pos, actor["pos"])
            if d > maximum + 4:
                continue
            _, height = self.shape(actor)
            point = (*actor["pos"][:2], actor["pos"][2] + height * 0.57)
            if not self.arena.visible(muzzle, point):
                high = (*point[:2], actor["pos"][2] + height * 0.82)
                if actor["kind"] != "mech" or not self.arena.visible(muzzle, high):
                    continue
            score = d + actor.get("hp", 1400) * 0.035
            score += {"mech": -65, "npc": -26, "crate": 0}[actor["kind"]]
            if actor["id"] == self.target_id:
                score -= 13
            if self.loot_plan and actor["id"] == self.loot_plan[1]:
                score -= 20
            if actor["kind"] == "mech":
                if actor.get("charging"):
                    score -= 18
                if obs["t"] - self.last_damage.get(actor["id"], -100) < 4:
                    score -= 68
                if d < 120 and actor.get("hp", 1400) < 500:
                    score -= 48
                elif d > 85:
                    score -= 24
                if d < 42:
                    score -= 30
                score -= self.opportunity(obs, actor, d)
            eligible = actor["kind"] != "mech" or self.take_fight(obs, actor, d)
            visible.append((score, actor["id"], actor, eligible))
        chosen = min((v for v in visible if v[3]), default=(0, 0, None, False))[2]
        self.target_id = chosen["id"] if chosen else None
        return chosen, [actor for _, _, actor, _ in visible if actor["kind"] == "mech"]

    def shot(self, weapon, target, muzzle, barrel=3):
        stats = weapon["stats"]
        kind = weapon["kind"]
        px, py, pz = target["pos"]
        vx, vy, vz = target.get("vel", (0, 0, 0))
        radius, height = self.shape(target)
        actor_is_mech = target["kind"] == "mech"
        airborne = actor_is_mech and (
            pz > self.arena.support(px, py, pz + 0.15) + 0.75 or vz > 2.5)
        time = self.dt * 0.4
        pitch = 0
        for _ in range(4):
            if actor_is_mech and kind in ("gun", "laser"):
                x, y, z = self.projected_body(target, time, airborne, self.config["mech"]["radius"], height)
                z += height * 0.53
            else:
                x, y = px + vx * time, py + vy * time
                z = pz + height * 0.56 + max(-height * 0.35, min(height * 0.45, vz * time))
            d = distance(muzzle, (x, y))
            dz = z - muzzle[2]
            travel = max(0.1, d - barrel)
            if kind == "mortar":
                speed = stats["speed"]
                gravity = stats["gravity"]
                discriminant = speed ** 4 - gravity * (gravity * travel ** 2 + 2 * dz * speed ** 2)
                if discriminant < 0 or d < 13:
                    return None
                pitch = atan2(gravity * travel ** 2 + 2 * dz * speed ** 2,
                              gravity * travel * (speed ** 2 + sqrt(discriminant)))
                time = self.dt * 0.4 + travel / (speed * cos(pitch))
                if time > 2.0:
                    return None
            else:
                pitch = atan2(dz, d)
                if kind == "gun":
                    time = self.dt * 0.4 + max(0, hypot(d, dz) - barrel) / stats["speed"]
        if kind != "mortar" and hypot(d, dz) > stats["range"] - (2 if kind == "lightning" else 0):
            return None
        if kind in ("gun", "laser") and actor_is_mech and not self.arena.visible(muzzle, (x, y, z)):
            raised = (x, y, z + height * 0.27)
            if not self.arena.visible(muzzle, raised):
                return None
            pitch = atan2(raised[2] - muzzle[2], d)
        return bearing(muzzle, (x, y)), pitch, d, radius, height

    def laser_release(self, me, target, weapon):
        stats = weapon["stats"]
        charge = weapon.get("charge", 0)
        if charge >= stats["charge"] - self.dt * 0.25:
            return True
        if charge < stats["min_charge"] or "hp" not in target:
            return False
        cubes = self.config["cubes"]
        strength = 1 + min(me["cubes"], cubes["stat_cap"]) * cubes["damage_per_cube"]
        dose = stats["min_frac"] + (1 - stats["min_frac"]) * charge / stats["charge"]
        damage = strength * stats["damage"] * dose
        if target["kind"] == "mech":
            if "mortar" in target["weapons"]:
                damage *= 1 + stats["cookoff"]
            if angle_error(target["aim"][0], bearing(target["pos"], me["pos"])) > 2:
                damage *= self.config["mech"]["rear_mult"]
            damage *= 0.82
        return target["hp"] < damage

    def combat(self, me, target, muzzle, retreat):
        weapons = me["weapons"]
        stats = me["stats"]
        fire = [False, False]
        if target is None:
            return None, fire
        mortar_muzzle = (*muzzle[:2], me["pos"][2] + stats["height"] + 0.5)
        barrel = stats["radius"] + 0.5
        shots = [self.shot(weapon, target, mortar_muzzle if weapon["kind"] == "mortar" else muzzle, barrel)
                 if weapon else None for weapon in weapons]
        priority = []
        for index, weapon in enumerate(weapons):
            if not weapon or not shots[index]:
                continue
            kind = weapon["kind"]
            state = weapon["stats"]
            score = 0
            if kind == "gun":
                score = 3 if weapon.get("reload", 0) == 0 and weapon.get("ammo", 0) > 0 else 0
            elif kind == "lightning" and me["energy"] >= state["energy"] + (8 if retreat else 2):
                score = 5 if weapon["cooldown"] < self.dt else 1
            elif kind == "laser" and (not retreat or weapon.get("charge", 0)):
                charge = weapon.get("charge", 0)
                if self.laser_release(me, target, weapon):
                    score = 7
                elif charge:
                    score = 1
                elif weapon["cooldown"] < self.dt:
                    score = 4
            elif kind == "mortar" and weapon["cooldown"] <= self.dt:
                score = 4 if shots[index][2] < 90 or target["kind"] != "mech" else 2
            priority.append((score, index))
        if not priority:
            return None, fire
        _, chosen = max(priority)
        yaw, pitch, _, _, _ = shots[chosen]
        aim = [yaw, max(stats["min_pitch"], min(stats["max_pitch"], pitch))]
        for index, weapon in enumerate(weapons):
            shot = shots[index]
            if not weapon or shot is None:
                continue
            sy, sp, d, radius, height = shot
            kind = weapon["kind"]
            state = weapon["stats"]
            tolerance = (state["cone_deg"] * pi / 360 if kind == "lightning"
                         else asin(min(0.8, radius / max(0.1, d))) * 0.75)
            turn = stats["turn_rate"] * self.dt * 0.5
            tilt = stats["pitch_rate"] * self.dt * 0.5
            lined_up = (angle_error(sy, aim[0]) < tolerance and
                        angle_error(sy, me["aim"][0]) < turn + tolerance and
                        (kind == "lightning" or
                         (abs(sp - aim[1]) < height * 0.35 / max(1, d) and
                          abs(sp - me["aim"][1]) < tilt + height * 0.35 / max(1, d))))
            if kind == "laser":
                charge = weapon.get("charge", 0)
                if retreat and not charge:
                    continue
                if charge:
                    fire[index] = not self.laser_release(me, target, weapon) or not lined_up
                else:
                    fire[index] = index == chosen and weapon["cooldown"] <= self.dt and lined_up
            elif lined_up and weapon["cooldown"] <= self.dt:
                fire[index] = (kind != "lightning" or
                               me["energy"] >= state["energy"] + (8 if retreat else 2))
        return aim, fire

    def loot(self, obs, safe_to_farm):
        me = obs["self"]
        pos = me["pos"]
        held = {w["kind"] for w in me["weapons"] if w}
        values = dict(WEAPON_VALUE, lightning=4) if obs["zone"]["next_radius"] < 65 else WEAPON_VALUE
        slot = next((i for i, w in enumerate(me["weapons"]) if w is None), None)
        swap = None if slot is not None else next(
            (i for i, w in enumerate(me["weapons"]) if w["kind"] != "gun" and not w.get("charge")), None)
        value = -1 if swap is None else values[me["weapons"][swap]["kind"]]
        candidates = []
        take = {}
        for pickup in obs["pickups"]:
            p = pickup["pos"]
            kind = pickup["kind"]
            if kind == "gun" or kind in held or (slot is None and swap is None):
                continue
            if values[kind] <= value or (kind == "mortar" and obs["t"] < self.mortar_cooloff):
                continue
            d = distance(pos, p)
            dz = abs(pos[2] - p[2])
            if d <= self.config["loot"]["pickup_radius"] and dz <= 4:
                if "take" not in take or values[kind] > take["value"]:
                    take = {"take": pickup["id"], "value": values[kind]}
                    if swap is not None:
                        take["drop"] = swap
            if dz < 6 and d < (100 if kind == "laser" else 72):
                candidates.append((d - 18 * WEAPON_VALUE[kind], pickup["id"], p))
        take.pop("value", None)
        if safe_to_farm or me["cubes"] < 10:
            for cube in obs["cubes"]:
                d = distance(pos, cube["pos"])
                if abs(pos[2] - cube["pos"][2]) < 5 and d < (115 if safe_to_farm else 18):
                    candidates.append((d - (58 if me["cubes"] < 10 else 12), cube["id"], cube["pos"]))
            for crate in obs["crates"] if safe_to_farm else ():
                d = distance(pos, crate["pos"])
                if abs(pos[2] - crate["pos"][2]) < 5 and d < 115:
                    approach = toward(crate["pos"], pos, min(d, 18))
                    candidates.append((d + crate["hp"] * 0.08, crate["id"], approach))
            for actor in obs["mechs"] if safe_to_farm else ():
                if actor["kind"] == "npc" and distance(pos, actor["pos"]) < 105:
                    d = distance(pos, actor["pos"])
                    if abs(pos[2] - actor["pos"][2]) < 5:
                        approach = toward(actor["pos"], pos, min(d, 35))
                        candidates.append((d - 37, actor["id"], approach))
        zone, t = obs["zone"], obs["t"]
        if candidates:
            candidates = [(score + self.zone_cost(p, zone, t), i, p) for score, i, p in candidates]
            return take, min(candidates)[2]
        return take, None

    def search(self, obs, threats, radius, height):
        me, t, zone = obs["self"], obs["t"], obs["zone"]
        pos = me["pos"]
        if self.search_goal is not None and t - self.search_time < 7 and distance(pos, self.search_goal) > 8:
            return self.search_goal
        if self.search_goal:
            self.search_history.append(self.search_goal)
        forward = bearing(pos, zone["next_center"])
        best = None
        for k in range(12):
            angle = forward + k * pi / 6
            point = (pos[0] + 53 * cos(angle), pos[1] + 53 * sin(angle))
            if not self.arena.walk_line(pos, toward(pos, point, 11), radius, height):
                continue
            cell = self.arena.nearest_open(point, radius, height, limit=2)
            if cell is None:
                continue
            point = self.arena.point(cell)
            score = self.zone_cost(point, zone, t)
            score += sum(max(0, 67 - distance(point, old)) * 0.7 for old in self.search_history)
            score += 6 if not self.arena.walk_line(pos, point, radius, height) else 0
            score += 0.10 * distance(point, zone["next_center"])
            for enemy in threats:
                d = distance(point, enemy["pos"])
                score += max(0, 160 - d) * 0.6
            if best is None or score < best[0]:
                best = score, point
        self.search_goal = best[1] if best else self.zone_goal(pos, zone)
        self.search_time = t
        return self.search_goal

    def roof_approach(self, me, roof, radius, height):
        pos = me["pos"]
        if pos[2] >= roof[2] - 0.15:
            return roof, False
        d = distance(pos, roof)
        nx, ny = toward(pos, roof, min(d, 39))
        if d > 46 or abs(me["vel"][2]) > 2 and me["vel"][2] < 0:
            return (nx, ny), False
        yaw = bearing(roof, pos)
        choices = []
        for angle in (yaw, yaw + 0.85, yaw - 0.85):
            for r in (7, 13, 20, 30):
                x, y = roof[0] + r * cos(angle), roof[1] + r * sin(angle)
                low = self.arena.ground(x, y)
                if not self.arena.clear(x, y, low, radius, roof[2] - low + height):
                    continue
                if not self.arena.walk_line(pos, (x, y), radius, height) and me["grounded"]:
                    continue
                if not self.arena.walk_line((x, y, roof[2]), roof, radius, height):
                    continue
                choices.append((distance(pos, (x, y)) + r * 0.55, (x, y)))
        if not choices:
            return (nx, ny), False
        approach = min(choices)[1]
        return approach, distance(pos, approach) < (8 if not me["grounded"] else 4)

    def harvest(self, obs, threats, safe_to_farm):
        me, t, zone = obs["self"], obs["t"], obs["zone"]
        pos = me["pos"]
        for kind, items in (("cube", obs["cubes"]), ("weapon", obs["pickups"]),
                            ("crate", obs["crates"]), ("npc", (m for m in obs["mechs"] if m["kind"] == "npc"))):
            for item in items:
                self.known_loot[item["id"]] = t, kind, item
        if t - self.loot_clock < 0.6 and self.loot_plan:
            cost, item_id, point = self.loot_plan
            old_kind = self.known_loot[item_id][1]
            if (cost < 108 and distance(pos, point) > 3 and t - self.known_loot[item_id][0] < 3.5 and
                    (safe_to_farm or old_kind == "cube")):
                return point
        self.loot_clock = t
        self.known_loot = {i: row for i, row in self.known_loot.items()
                           if t - row[0] <= 7 and (t - row[0] <= self.dt or distance(row[2]["pos"], pos) > 5)}
        armed = {w["kind"] for w in me["weapons"] if w}
        needs_weapon = len(armed) == 1
        candidates = []
        for item_id, (last, kind, item) in self.known_loot.items():
            point = item["pos"]
            d = distance(pos, point)
            elevation = point[2] - pos[2]
            roof_access = (
                kind == "weapon" and needs_weapon and safe_to_farm and
                me["energy"] > (58 if me["grounded"] else 12) and 5 < elevation < 24
            )
            if d > 200 or (abs(elevation) > 5.5 and not roof_access):
                continue
            cost = d + self.zone_cost(point, zone, t) + (t - last) * 2.5
            if kind == "cube":
                cluster = sum(1 for _, peer_kind, peer in self.known_loot.values()
                              if peer_kind == "cube" and distance(point, peer["pos"]) < 9)
                cost -= min(6, cluster) * (21 if me["cubes"] < 10 else 7)
                if not safe_to_farm and d > (42 if cluster >= 3 else 14):
                    continue
            elif kind == "weapon":
                weapon = item["kind"]
                if weapon in armed or weapon == "gun" or "laser" in armed:
                    continue
                cost -= (115 if weapon == "laser" else 80) if needs_weapon else (75 if weapon == "laser" else 34)
                if roof_access:
                    cost += 17
                if not safe_to_farm and d > 24:
                    continue
            elif not safe_to_farm or me["cubes"] >= 8:
                continue
            else:
                cost += (3 if kind == "crate" else -18) + item["hp"] * 0.05
                if not self.arena.visible((*pos[:2], pos[2] + me["stats"]["muzzle_height"]),
                                          (*point[:2], point[2] + 1.6), stride=3.4):
                    cost += 19
                point = toward(point, pos, min(d, 22 if kind == "npc" else 13))
            for enemy in threats:
                r = distance(enemy["pos"], point)
                cost += max(0, 115 - r) * 1.2
            candidates.append((cost, item_id, point))
        self.loot_plan = min(candidates, default=None, key=lambda c: (c[0], c[1]))
        return self.loot_plan[2] if self.loot_plan and self.loot_plan[0] < 108 else None

    def drop_goal(self, obs, threats):
        me, t = obs["self"], obs["t"]
        pos = me["pos"]
        best = None
        for drop in obs.get("drops", ()):
            p = drop["pos"]
            arrival = distance(pos, p) / (me["stats"]["walk_speed"] * 0.9)
            if arrival > 24 or drop["lands_in"] > arrival + 5 or self.zone_cost(p, obs["zone"], t) > 35:
                continue
            if drop["holder"] not in (None, me["id"]):
                continue
            if any(distance(enemy["pos"], p) < 32 for enemy in threats) and drop["holder"] != me["id"]:
                continue
            value = arrival - (1 - me["hp"] / me["max_hp"]) * 10
            if best is None or value < best[0]:
                best = value, p
        return best[1] if best else None

    def shelter_goal(self, obs, threats, radius, height):
        t, me, zone = obs["t"], obs["self"], obs["zone"]
        pos = me["pos"]
        center, next_radius = zone["next_center"], zone["next_radius"]
        if self.shelter is not None and t - self.shelter_time < (1.25 if threats else 4.5):
            if distance(self.shelter, center) < next_radius - 7 and not any(
                    distance(enemy["pos"], self.shelter) < 90 for enemy in threats):
                return self.shelter

        anchor = toward(center, pos, max(0, min(distance(pos, center), next_radius - 18)))
        candidates = [anchor, tuple(pos[:2])]
        for i in range(12):
            yaw = 2 * pi * i / 12
            distance_from_anchor = 26 if i % 2 else 44
            point = (anchor[0] + cos(yaw) * distance_from_anchor,
                     anchor[1] + sin(yaw) * distance_from_anchor)
            candidates.append(point)
        corners = [(cos(i * pi / 4), sin(i * pi / 4)) for i in range(8)]
        best = None
        cutoff = max(10, next_radius - min(28, next_radius * 0.24))
        for point in candidates:
            lx, ly = point
            z = self.arena.support(lx, ly, pos[2] + 1.2)
            if not self.arena.clear(lx, ly, z, radius, height):
                continue
            travel = distance(pos, point)
            zone_distance = distance(point, center)
            cost = self.zone_cost(point, zone, t) + max(0, zone_distance - cutoff) * 1.6
            cost += travel * 0.42 + 0.055 * zone_distance
            if self.shelter is not None and distance(self.shelter, point) < 10:
                cost -= 5
            if travel < 62 and not self.arena.walk_line(pos, point, radius, height):
                cost += 14
            eye = (lx, ly, z + height * 0.65)
            cache_key = round(lx / 4), round(ly / 4), round(z / 2)
            cover = self.shelter_costs.get(cache_key)
            if cover is None:
                cover = 0
                for dx, dy in corners:
                    x, y = lx + dx * 58, ly + dy * 58
                    other = (x, y, self.arena.ground(x, y) + 7)
                    if not self.arena.visible(eye, other, stride=3.7):
                        cover += 1
                    elif self.arena.veiled(eye, other):
                        cover += 0.65
                self.shelter_costs[cache_key] = cover
            cost -= 5.2 * cover
            for enemy in threats:
                p = enemy["pos"]
                d = distance(p, point)
                other = (*p[:2], p[2] + self.shape(enemy)[1] * 0.7)
                cost += max(0, 115 - d) * 0.65
                if d < 195 and self.arena.visible(eye, other, stride=3.7):
                    unseen = d > 68 and self.arena.veiled(eye, other)
                    cost += max(0, 195 - d) * (0.18 if unseen else 0.35)
            if best is None or cost < best[0]:
                best = cost, point
        self.shelter = best[1] if best else anchor
        self.shelter_time = t
        return self.shelter

    def cover_move(self, obs, strategic, threats, retreat, target, radius, height):
        t, me, zone = obs["t"], obs["self"], obs["zone"]
        pos = me["pos"]
        if not threats:
            self.tactical_goal = None
            return strategic
        if (self.tactical_goal is not None and retreat == self.tactical_retreat and
                t - self.tactical_time < 0.32):
            if distance(pos, self.tactical_goal) > 3:
                return self.tactical_goal

        target_id = target["id"] if target else None
        closest = threats[0]
        away = bearing(closest["pos"], pos)
        endpoint = toward(pos, strategic, min(18, distance(pos, strategic)))
        options = [endpoint]
        if not retreat or not any(distance(enemy["pos"], pos) < 100 for enemy in threats):
            options.append(tuple(pos[:2]))
        for offset in range(12):
            yaw = away + offset * pi / 6
            options.append((pos[0] + 18 * cos(yaw), pos[1] + 18 * sin(yaw)))
        peers = []
        for enemy in threats:
            p = enemy["pos"]
            eye = (*p[:2], p[2] + self.shape(enemy)[1] * 0.7)
            last = self.enemy_memory.get(enemy["id"], (t, enemy))[0]
            peers.append((enemy, p, eye, max(0.35, 1 - (t - last) * 0.22)))

        best = None
        dx, dy = me["vel"][:2]
        for point in options:
            if point != tuple(pos[:2]) and not self.arena.walk_line(pos, point, radius, height):
                continue
            z = self.arena.support(*point, pos[2] + 1.2)
            eye = (*point, z + height * 0.80)
            middle = toward(pos, point, distance(pos, point) / 2)
            middle_eye = (*middle, self.arena.support(*middle, pos[2] + 1.2) + height * 0.80)
            score = self.zone_cost(point, zone, t)
            score += 0.55 * (distance(point, strategic) - distance(pos, strategic))
            score -= 0.015 * (dx * (point[0] - pos[0]) + dy * (point[1] - pos[1]))
            for enemy, p, other, confidence in peers:
                d = distance(p, point)
                facing = angle_error(enemy["aim"][0], bearing(p, point)) < 0.9
                pressure = self.arena.visible(eye, other, stride=3.5) if d < 190 else False
                if retreat or enemy["id"] != target_id:
                    value = max(0, 115 - d) * 0.84 + max(0, 55 - d) * 1.7
                    if pressure:
                        value += max(0, 180 - d) * (0.33 if facing else 0.21)
                        if "lightning" in enemy["weapons"]:
                            value += max(0, 64 - d) * 1.5
                        lateral = abs(sin(bearing(p, point) - bearing(p, pos))) * d
                        value += max(0, 11 - lateral) * max(0, 155 - d) / 100
                    if not pressure and d < 145 and self.arena.visible(middle_eye, other, stride=4.5):
                        value += max(0, 145 - d) * 0.13
                    score += value * confidence
                else:
                    own_lightning = any(w and w["kind"] == "lightning" for w in me["weapons"])
                    hostile_lightning = "lightning" in enemy["weapons"]
                    own_laser = any(w and w["kind"] == "laser" for w in me["weapons"])
                    ideal = 30 if own_lightning and not hostile_lightning else (111 if own_laser else 72)
                    score += max(0, ideal - d) * 0.9 + max(0, d - ideal - 8) * 0.5
                    if pressure:
                        score -= 20 if self.outgoing >= self.incoming else 12
                        if enemy.get("charging") and facing:
                            side = abs(sin(bearing(p, point) - bearing(p, pos))) * d
                            score += max(0, 11 - side) * 3
            if best is None or score < best[0]:
                best = score, point
        self.tactical_goal = best[1] if best else strategic
        self.tactical_time = t
        self.tactical_retreat = retreat
        return self.tactical_goal

    def escape(self, pos, goal, radius, height, t):
        if distance(pos, goal) < 9:
            self.progress_anchor = None
            self.escape_goal = None
            return None
        if self.escape_goal is not None and t < self.escape_until and distance(pos, self.escape_goal) > 3:
            return self.escape_goal
        self.escape_goal = None
        self.force_climb = False
        if self.progress_anchor is None or distance(pos, self.progress_anchor) > 5:
            self.progress_anchor = pos
            self.progress_time = t
            return None
        if t - self.progress_time < 1.25:
            return None

        self.progress_anchor = pos
        self.progress_time = t
        self.route.clear()
        self.route_goal = None
        yaw = bearing(pos, goal)
        options = []
        for i in range(12):
            angle = yaw + i * pi / 6
            point = (pos[0] + 15 * cos(angle), pos[1] + 15 * sin(angle))
            if self.arena.walk_line(pos, point, radius, height):
                revisit = sum(max(0, 15 - distance(point, endpoint)) for endpoint in self.escape_history)
                score = distance(point, goal) + revisit * 2.5 - 8 * abs(sin(i * pi / 6))
                options.append((score, point))
        if options:
            self.escape_goal = min(options)[1]
        else:
            self.escape_goal = toward(pos, goal, 15)
            self.force_climb = True
        self.escape_until = t + 1.9
        self.escape_history = (self.escape_history + [self.escape_goal])[-5:]
        return self.escape_goal

    def navigate(self, pos, goal, radius, height, t):
        d = distance(pos, goal)
        if d < 1:
            return tuple(pos[:2])
        route_changed = self.route_goal is None or distance(goal, self.route_goal) > 23
        if route_changed:
            self.route.clear()
        direct = toward(pos, goal, min(d, 29))
        if d <= 70 and self.arena.walk_line(pos, goal, radius, height):
            self.route.clear()
            return direct
        if not self.route and not route_changed and t - self.route_time < 0.35:
            if self.arena.walk_line(pos, direct, radius, height):
                return direct
        elif d > 70 and not self.route and self.arena.walk_line(pos, direct, radius, height):
            return direct
        else:
            if route_changed or not self.route or t - self.route_time >= 2.1:
                self.route_goal = goal
                self.route_time = t
                self.route = self.arena.route(pos, goal, radius, height)
        for i in range(min(8, len(self.route)) - 1, -1, -1):
            point = self.route[i]
            if distance(pos, point) >= 2.4 and self.arena.walk_line(pos, point, radius, height):
                self.route = self.route[i:]
                return point
        self.route.clear()
        if self.arena.walk_line(pos, direct, radius, height):
            return direct
        yaw = bearing(pos, goal)
        for offset in (pi / 5, -pi / 5, pi / 2.5, -pi / 2.5, pi / 1.5, -pi / 1.5, pi):
            direction = yaw + offset
            p = pos[0] + 9 * cos(direction), pos[1] + 9 * sin(direction)
            if self.arena.walk_line(pos, p, radius, height):
                return p
        return goal

    def act(self, obs):
        t, me, zone = obs["t"], obs["self"], obs["zone"]
        pos, stats = me["pos"], me["stats"]
        radius = round(stats["radius"] * 2) / 2 + 0.3
        height = round(stats["height"] * 2) / 2
        muzzle = (*pos[:2], pos[2] + stats["muzzle_height"])
        action = {"tick": obs["tick"]}
        if obs["offer"]:
            action["pick"] = max(range(len(obs["offer"]["choices"])),
                                 key=lambda i: UPGRADE_VALUE.get(obs["offer"]["choices"][i]["id"], 0))
        for event in obs["events"]:
            if event["type"] == "damaged":
                self.damage_time = t
                if event.get("bearing") is not None:
                    self.last_damage[event.get("by")] = t
                    self.damage_bearing = event["bearing"]
                    attacker = event.get("by")
                    if isinstance(attacker, int) and 0 <= attacker < self.config["match"]["mechs"]:
                        self.recent_fire.append((t, attacker, event["bearing"], event["amount"]))
                        self.trade_window.append((t, event["amount"], 0))
            elif event["type"] == "hit":
                self.firing_time = t
                if isinstance(event["target"], int) and event["target"] < self.config["match"]["mechs"]:
                    self.trade_window.append((t, 0, event["amount"]))

        while self.trade_window and t - self.trade_window[0][0] > 2.8:
            self.trade_window.popleft()
        self.incoming = sum(taken for _, taken, _ in self.trade_window)
        self.outgoing = sum(dealt for _, _, dealt in self.trade_window)

        enemies = sorted((e for e in obs["mechs"] if e["kind"] == "mech"),
                         key=lambda e: distance(pos, e["pos"]))
        for enemy in enemies:
            self.enemy_memory[enemy["id"]] = t, enemy
        self.enemy_memory = {i: entry for i, entry in self.enemy_memory.items() if t - entry[0] < 4.2}
        self.observe_opponents(obs, enemies)
        self.observe_motion(t, enemies)
        actors = enemies + [e for e in obs["mechs"] if e["kind"] == "npc"]
        actors += [dict(crate, kind="crate") for crate in obs["crates"]]
        target, exposed = self.target(obs, actors, muzzle)
        known_threats = sorted(
            (e for _, e in self.enemy_memory.values() if distance(pos, e["pos"]) < 195),
            key=lambda e: distance(pos, e["pos"]))[:4]
        exposed.sort(key=lambda e: distance(pos, e["pos"]))
        near = [e for e in exposed if distance(pos, e["pos"]) < 115]
        frac = me["hp"] / me["max_hp"]
        rival_target = target if target and target["kind"] == "mech" else None
        danger = [e for e in exposed if distance(pos, e["pos"]) < 160 and
                  (angle_error(e["aim"][0], bearing(e["pos"], pos)) < 1.05 or
                   distance(pos, e["pos"]) < 55 or t - self.last_damage.get(e["id"], -100) < 2)]
        if danger and (len(danger) >= 3 or (self.incoming > max(290, self.outgoing * 1.45) and frac < 0.73)):
            self.withdraw_until = t + 3.5
        elif frac < 0.48 and t - self.damage_time < 1.8:
            self.withdraw_until = max(t + 2.7, self.withdraw_until)
        avoid_duels = obs["alive"] > 2 and zone["radius"] >= 36
        retreat = t < self.withdraw_until
        if known_threats and not rival_target:
            retreat = retreat or len(known_threats) > 1 or frac < 0.82
        if rival_target and frac < 0.60 and enemy_richer(rival_target, me):
            retreat = True
        if rival_target and len(danger) <= 1 and rival_target.get("hp", 1400) < min(315, me["hp"] * 0.42):
            retreat = False

        laser_threat = any("laser" in e["weapons"] and distance(pos, e["pos"]) < 155 and
                           (e["charging"] or angle_error(e["aim"][0], bearing(e["pos"], pos)) < 0.5)
                           for e in danger)
        if laser_threat:
            self.mortar_cooloff = t + 4
        take, _ = self.loot(obs, safe_to_farm=False)
        action.update(take)
        if laser_threat and "take" not in take:
            mortar_slot = next((i for i, w in enumerate(me["weapons"]) if w and w["kind"] == "mortar"), None)
            if mortar_slot is not None:
                action["drop"] = mortar_slot
        safe_to_farm = not danger and frac > 0.58
        if near and any(distance(pos, enemy["pos"]) < 67 for enemy in near):
            safe_to_farm = False
        loot_goal = self.harvest(obs, known_threats, safe_to_farm)
        center = zone["next_center"]
        zone_goal = self.zone_goal(pos, zone)
        current_margin = zone["radius"] - distance(pos, zone["center"])
        next_margin = zone["next_radius"] - distance(pos, center)
        urgent = not me["in_zone"] or (current_margin < 19 and next_margin < 8 and t >= zone["shrink_start"] - 2)
        drop = self.drop_goal(obs, known_threats)
        seeking_weapon = bool(self.loot_plan and loot_goal is not None and
                              self.known_loot[self.loot_plan[1]][1] == "weapon")
        roof_climb = False
        ambush = None
        if not retreat and not rival_target and me["cubes"] >= 6:
            opportunities = []
            for enemy in known_threats:
                enemy_distance = distance(pos, enemy["pos"])
                reward = self.opportunity(obs, enemy, enemy_distance)
                if reward:
                    opportunities.append((enemy_distance - reward, enemy["id"], enemy))
            if opportunities:
                _, _, prey = min(opportunities)
                ambush = toward(prey["pos"], pos, min(87, distance(pos, prey["pos"])))

        if urgent:
            strategic = zone_goal
        elif drop is not None and not near and (frac < 0.83 or me["cubes"] < 10):
            strategic = drop
        elif safe_to_farm and seeking_weapon and me["weapons"].count(None):
            strategic = loot_goal
            if len(loot_goal) == 3 and loot_goal[2] - pos[2] > 1.2:
                strategic, roof_climb = self.roof_approach(me, loot_goal, radius, height)
        elif avoid_duels and safe_to_farm and me["weapons"].count(None) and loot_goal is None:
            strategic = self.search(obs, known_threats, radius, height)
        elif retreat and known_threats:
            strategic = self.shelter_goal(obs, known_threats, radius, height)
        elif ambush is not None and distance(pos, ambush) > 4:
            strategic = ambush
        elif loot_goal is not None and (safe_to_farm or distance(pos, loot_goal) < 17):
            strategic = loot_goal
        elif zone["radius"] <= 65 or (next_margin < 14 and t > zone["shrink_start"] - 12):
            strategic = toward(zone_goal, center, min(15, distance(zone_goal, center)))
        elif target and target["kind"] == "mech" and not retreat:
            p = target["pos"]
            standoff = 80 if me["weapons"].count(None) else 100
            strategic = toward(pos, p, min(35, max(0, distance(pos, p) - standoff)))
        else:
            # Stay inside the next ring while looking for loot along its interior.
            phase = t / 14 + me["id"]
            patrol_radius = zone["next_radius"] * (0.51 if me["cubes"] < 8 else 0.26)
            strategic = (center[0] + patrol_radius * cos(phase), center[1] + patrol_radius * sin(phase))
            if next_margin < 15:
                strategic = zone_goal

        if frac < 0.93 and not exposed and not known_threats and 0.9 < t - self.damage_time < 23 and next_margin > 16 and drop is None:
            strategic = pos
        if retreat and not known_threats and t - self.damage_time <= 1.2 and self.damage_bearing is not None and not urgent:
            yaw = self.damage_bearing
            strategic = (pos[0] - 30 * cos(yaw), pos[1] - 30 * sin(yaw))
        if drop is not None and strategic is drop and distance(pos, drop) <= 5:
            strategic = pos
        tactical_selected = bool(known_threats and not urgent and strategic is not pos)
        if tactical_selected:
            if retreat and distance(pos, strategic) > 18:
                strategic = self.navigate(pos, strategic, radius, height, t)
            strategic = self.cover_move(obs, strategic, known_threats, retreat, rival_target, radius, height)
        if not urgent:
            self.zone_progress = None
            self.zone_escape = None
        elif self.zone_escape is None:
            if self.zone_progress is None or distance(pos, self.zone_progress[0]) > 15:
                self.zone_progress = pos, t
            elif t - self.zone_progress[1] > 2.8:
                self.zone_escape = toward(pos, zone_goal, min(29, distance(pos, zone_goal)))
                self.zone_jumped = False
        if self.zone_escape is not None and (distance(pos, self.zone_escape) < 4 or
                                            (self.zone_jumped and me["grounded"])):
            self.zone_escape = None
            self.zone_progress = pos, t
        escape = self.escape(pos, strategic, radius, height, t)
        waypoint = escape
        if waypoint is None:
            if tactical_selected and self.arena.walk_line(pos, strategic, radius, height):
                waypoint = strategic
            else:
                waypoint = self.navigate(pos, strategic, radius, height, t)
        if self.zone_escape is not None:
            waypoint = self.zone_escape
        d = distance(pos, waypoint)
        desired_speed = stats["walk_speed"]
        movement = toward((0, 0), (waypoint[0] - pos[0], waypoint[1] - pos[1]),
                          min(1, d / max(1, desired_speed * 0.34)))
        if self.location is not None and d > 3 and distance(pos, self.location) < 0.22:
            self.stuck += 1
        else:
            self.stuck = 0
        self.location = pos
        climb = (escape is not None and self.force_climb) or self.stuck >= 5 or (not me["grounded"] and d > 4 and
                                    not self.arena.walk_line(pos, waypoint, radius, height))
        sprint = d > 5 and (urgent or retreat or (not exposed and distance(pos, strategic) > 34))
        energy_needed = 25 if self.sprinting else 68
        if urgent or (retreat and danger):
            energy_needed = 16 if self.sprinting else 36
        self.sprinting = bool(sprint and me["grounded"] and me["energy"] > energy_needed)
        if escape is not None and self.force_climb and me["grounded"] and me["energy"] <= 28:
            self.sprinting = False
        jump = bool(me["grounded"] and climb and me["energy"] > 28)
        action["move"] = movement
        action["jump"] = jump
        action["boost"] = bool((climb and not me["grounded"] and me["energy"] > 14) or
                               self.sprinting)
        if roof_climb:
            action["jump"] = me["grounded"] and me["energy"] > 48
            action["boost"] = not me["grounded"] and me["energy"] > 10
            action["move"] = (0, 0)
        if self.zone_escape is not None:
            jump = me["grounded"] and me["energy"] >= 54
            self.zone_jumped |= bool(jump)
            action["jump"] = bool(jump)
            action["boost"] = bool(not me["grounded"] and self.zone_jumped and me["energy"] > 7)
        if not urgent and not jump and not roof_climb and obs["projectiles"]:
            action["move"] = avoid(self.arena, obs, movement, action["boost"], radius, height)

        if retreat and (target is None or target["kind"] != "mech") and danger:
            target = danger[0]
        point_blank = bool(target and target["kind"] == "mech" and distance(pos, target["pos"]) < 44)
        aim, fire = self.combat(me, target, muzzle, retreat and not point_blank)
        if target and target["kind"] == "mech" or danger or t - self.damage_time < 1.0:
            self.scout_start = None
        if (not target and not danger and t - self.damage_time > 1.0 and me["weapons"].count(None) and
                loot_goal is None and t - self.scout_time >= 6):
            if self.scout_start is None:
                self.scout_start = t, me["aim"][0]
            started, yaw = self.scout_start
            if t - started < 1.45:
                aim = [yaw + (t - started + self.dt) * 3.85, 0]
            else:
                self.scout_start = None
                self.scout_time = t
        if aim is None:
            if self.damage_bearing is not None and t - self.damage_time < 1.4:
                aim = [self.damage_bearing, 0]
            elif near:
                aim = [bearing(pos, near[0]["pos"]), 0]
            elif d > 1:
                yaw = bearing(pos, waypoint)
                aim = [yaw + 0.62 * sin(t * 2.1), 0]
            else:
                aim = [me["aim"][0] + self.dt * 1.9, 0]
        heard = self.listen(obs, target)
        if heard is not None and self.scout_start is None and t - self.damage_time > 0.55 and (target is None or retreat):
            aim, fire = [heard, 0], [False, False]
        guard = self.rear_guard(obs, target, aim)
        if guard is not None:
            aim = [guard, 0]
            fire = [bool(w and w["kind"] == "laser" and w.get("charge", 0)) for w in me["weapons"]]
        action["aim"] = aim
        action["fire"] = fire
        gun = next((w for w in me["weapons"] if w and w["kind"] == "gun"), None)
        if gun and gun.get("reload", 0) == 0 and gun.get("ammo", 0) < gun["stats"]["mag"] * 0.6:
            action["reload"] = target is None and t - self.firing_time > 0.55
        return action


if __name__ == "__main__":
    mechfight.run(Bot)
