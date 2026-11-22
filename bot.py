"""Survival, loot, and combat policy for Mech Royale."""

from math import asin, atan2, cos, hypot, pi, sin, sqrt

from arena import Arena
from dodge import avoid
import mechfight


WEAPON_VALUE = {"gun": 0, "mortar": 1, "lightning": 2, "laser": 3}
UPGRADE_VALUE = {
    "nanites": 10, "servos": 9, "gun_mag": 8.5, "gun_damage": 8,
    "plating": 7.5, "armor": 7, "laser_damage": 6.8, "laser_charge": 6.5,
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


class Bot:
    name = "Oval Circuit"

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
        self.damage_time = -100
        self.damage_bearing = None
        self.firing_time = -100
        self.target_id = None
        self.ffa = False
        self.alive_seen = None
        self.other_losses = 0
        self.peer_hp = {}
        self.foreign_beams = set()
        self.peer_wounded = {}
        self.busy_peers = set()

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
        if not self.ffa or obs["alive"] <= 3:
            return True
        me, t = obs["self"], obs["t"]
        if d < 37 or t - self.last_damage.get(enemy["id"], -100) < 4.0:
            return True
        toward_me = angle_error(enemy["aim"][0], bearing(enemy["pos"], me["pos"])) < 0.35
        hp = enemy.get("hp", 1400)
        if self.opportunity(obs, enemy, d) and me["hp"] > 310:
            return True
        return toward_me and d < 92 and me["hp"] > hp * 1.18

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
                continue
            score = d + actor.get("hp", 1400) * 0.035
            score += {"mech": -65, "npc": 4, "crate": 0}[actor["kind"]]
            if actor["id"] == self.target_id:
                score -= 13
            if actor["kind"] == "mech":
                if actor.get("charging"):
                    score -= 18
                if obs["t"] - self.last_damage.get(actor["id"], -100) < 4:
                    score -= 58
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
        time = self.dt * 0.4
        pitch = 0
        for _ in range(4):
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
        return bearing(muzzle, (x, y)), pitch, d, radius, height

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
            elif kind == "laser" and not retreat:
                score = 7 if weapon.get("charge", 0) else (4 if weapon["cooldown"] < self.dt else 0)
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
                if retreat:
                    continue
                if charge:
                    fire[index] = (charge < state["charge"] - self.dt * 0.25 or not lined_up)
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
        slot = next((i for i, w in enumerate(me["weapons"]) if w is None), None)
        swap = None if slot is not None else next(
            (i for i, w in enumerate(me["weapons"]) if w["kind"] not in ("gun", "laser")), None)
        value = -1 if swap is None else WEAPON_VALUE[me["weapons"][swap]["kind"]]
        candidates = []
        take = {}
        for pickup in obs["pickups"]:
            p = pickup["pos"]
            kind = pickup["kind"]
            if kind == "gun" or kind in held or (slot is None and swap is None):
                continue
            if WEAPON_VALUE[kind] <= value:
                continue
            d = distance(pos, p)
            dz = abs(pos[2] - p[2])
            if d <= self.config["loot"]["pickup_radius"] and dz <= 4:
                if "take" not in take or WEAPON_VALUE[kind] > take["value"]:
                    take = {"take": pickup["id"], "value": WEAPON_VALUE[kind]}
                    if swap is not None:
                        take["drop"] = swap
            if dz < 6 and d < (125 if kind == "laser" else 85):
                candidates.append((d - 18 * WEAPON_VALUE[kind], pickup["id"], p))
        take.pop("value", None)
        if safe_to_farm:
            for cube in obs["cubes"]:
                d = distance(pos, cube["pos"])
                if abs(pos[2] - cube["pos"][2]) < 5 and d < 115:
                    candidates.append((d - (58 if me["cubes"] < 10 else 12), cube["id"], cube["pos"]))
            for crate in obs["crates"]:
                d = distance(pos, crate["pos"])
                if abs(pos[2] - crate["pos"][2]) < 5 and d < 115:
                    approach = toward(crate["pos"], pos, min(d, 18))
                    candidates.append((d + crate["hp"] * 0.08, crate["id"], approach))
            for actor in obs["mechs"]:
                if actor["kind"] == "npc" and distance(pos, actor["pos"]) < 105:
                    d = distance(pos, actor["pos"])
                    if abs(pos[2] - actor["pos"][2]) < 5:
                        approach = toward(actor["pos"], pos, min(d, 35))
                        candidates.append((d - 8, actor["id"], approach))
        zone, t = obs["zone"], obs["t"]
        if candidates:
            candidates = [(score + self.zone_cost(p, zone, t), i, p) for score, i, p in candidates]
            return take, min(candidates)[2]
        return take, None

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

    def tactical(self, obs, strategic, threats, retreat, target, radius, height):
        t, me = obs["t"], obs["self"]
        pos = me["pos"]
        if not threats:
            self.tactical_goal = None
            return strategic
        if self.tactical_goal and t - self.tactical_time < 0.28 and distance(pos, self.tactical_goal) > 4:
            return self.tactical_goal
        closest = min(threats, key=lambda e: distance(pos, e["pos"]))
        direction = bearing(closest["pos"], pos)
        ideal = 35 if any(w and w["kind"] == "lightning" for w in me["weapons"]) else 103
        if retreat:
            ideal = 105
        options = [strategic]
        for i in range(12):
            yaw = direction + i * pi / 6
            options.append((pos[0] + 19 * cos(yaw), pos[1] + 19 * sin(yaw)))
        ranked = []
        target_id = target["id"] if target and target["kind"] == "mech" else None
        for point in options:
            endpoint = toward(pos, point, min(20, distance(pos, point)))
            if distance(pos, endpoint) < 1 or not self.arena.walk_line(pos, endpoint, radius, height):
                continue
            score = self.zone_cost(endpoint, obs["zone"], t)
            score += (distance(endpoint, strategic) - distance(pos, strategic)) * 0.36
            for i, enemy in enumerate(threats[:4]):
                enemy_pos = enemy["pos"]
                d = distance(endpoint, enemy_pos)
                if retreat:
                    score += max(0, ideal - d) * (0.85 if i == 0 else 0.45)
                else:
                    score += max(0, ideal - d) * 1.15 + max(0, d - ideal - 12) * (0.48 if i == 0 else 0)
                if d < 142:
                    eye = (*endpoint, self.arena.ground(*endpoint) + height * 0.65)
                    other = (*enemy_pos[:2], enemy_pos[2] + self.shape(enemy)[1] * 0.75)
                    if self.arena.visible(eye, other, stride=4):
                        if enemy["id"] == target_id:
                            score += 24 if retreat else -11
                        else:
                            aligned = angle_error(enemy["aim"][0], bearing(enemy_pos, endpoint)) < 0.9
                            recently_hit = t - self.last_damage.get(enemy["id"], -100) < 4
                            score += 22 if aligned or recently_hit else 10
            # Prefer lateral motion in a duel and retain momentum on near ties.
            score -= (me["vel"][0] * (endpoint[0] - pos[0]) +
                      me["vel"][1] * (endpoint[1] - pos[1])) * 0.017
            ranked.append((score, point))
        self.tactical_goal = min(ranked)[1] if ranked else strategic
        self.tactical_time = t
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
            elif event["type"] == "hit":
                self.firing_time = t

        enemies = sorted((e for e in obs["mechs"] if e["kind"] == "mech"),
                         key=lambda e: distance(pos, e["pos"]))
        self.observe_opponents(obs, enemies)
        actors = enemies + [e for e in obs["mechs"] if e["kind"] == "npc"]
        actors += [dict(crate, kind="crate") for crate in obs["crates"]]
        target, exposed = self.target(obs, actors, muzzle)
        known_threats = [enemy for enemy in enemies if distance(pos, enemy["pos"]) < 155]
        if target and target["kind"] == "mech":
            known_threats.sort(key=lambda enemy: (enemy["id"] != target["id"],
                                                  distance(pos, enemy["pos"])))
        exposed.sort(key=lambda e: distance(pos, e["pos"]))
        near = [e for e in exposed if distance(pos, e["pos"]) < 115]
        frac = me["hp"] / me["max_hp"]
        retreat = (frac < 0.43 or (frac < 0.67 and len(near) >= 2) or
                   (frac < 0.74 and t - self.damage_time > 1.9 and t - self.firing_time > 1.9))
        if self.ffa and obs["alive"] > 3 and not (target and target["kind"] == "mech") and near:
            retreat = True
        if len(near) == 1 and near[0].get("hp", 1400) < min(270, me["hp"] * 0.48):
            retreat = False

        take, loot_goal = self.loot(obs, safe_to_farm=not near and not retreat)
        action.update(take)
        center = zone["next_center"]
        zone_goal = self.zone_goal(pos, zone)
        current_margin = zone["radius"] - distance(pos, zone["center"])
        next_margin = zone["next_radius"] - distance(pos, center)
        urgent = not me["in_zone"] or (current_margin < 19 and next_margin < 8 and t >= zone["shrink_start"] - 2)
        drop = self.drop_goal(obs, known_threats)
        ambush = None
        if not retreat and (target is None or target["kind"] != "mech"):
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
        elif drop is not None and (not near or frac < 0.8):
            strategic = drop
        elif ambush is not None and distance(pos, ambush) > 4:
            strategic = ambush
        elif loot_goal is not None and not near and not retreat:
            strategic = loot_goal
        elif zone["radius"] <= 65 or (next_margin < 14 and t > zone["shrink_start"] - 12):
            strategic = toward(zone_goal, center, min(15, distance(zone_goal, center)))
        elif target and target["kind"] == "mech" and not retreat:
            p = target["pos"]
            strategic = toward(pos, p, min(35, max(0, distance(pos, p) - 96)))
        else:
            # Stay inside the next ring while looking for loot along its interior.
            phase = t / 14 + me["id"]
            patrol_radius = zone["next_radius"] * 0.55
            strategic = (center[0] + patrol_radius * cos(phase), center[1] + patrol_radius * sin(phase))
            if next_margin < 15:
                strategic = zone_goal

        if retreat and not exposed and t - self.damage_time > 0.8 and next_margin > 16 and drop is None:
            strategic = pos
        if retreat and not exposed and t - self.damage_time <= 0.8 and self.damage_bearing is not None and not urgent:
            yaw = self.damage_bearing
            strategic = (pos[0] - 30 * cos(yaw), pos[1] - 30 * sin(yaw))
        if drop is not None and strategic is drop and distance(pos, drop) <= 5:
            strategic = pos
        if known_threats and not urgent and strategic is not pos:
            strategic = self.tactical(obs, strategic, known_threats, retreat, target, radius, height)
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
        waypoint = escape if escape is not None else self.navigate(pos, strategic, radius, height, t)
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
        sprint = d > 5 and (urgent or retreat or (not exposed and distance(pos, strategic) > 100))
        if escape is not None and self.force_climb and me["grounded"] and me["energy"] <= 28:
            sprint = False
        jump = bool(me["grounded"] and climb and me["energy"] > 28)
        action["move"] = movement
        action["jump"] = jump
        action["boost"] = bool((climb and not me["grounded"] and me["energy"] > 14) or
                               (sprint and me["grounded"] and me["energy"] > (12 if urgent else 42)))
        if self.zone_escape is not None:
            jump = me["grounded"] and me["energy"] >= 54
            self.zone_jumped |= bool(jump)
            action["jump"] = bool(jump)
            action["boost"] = bool(not me["grounded"] and self.zone_jumped and me["energy"] > 7)
        if not urgent and not jump and not climb and obs["projectiles"]:
            action["move"] = avoid(self.arena, obs, movement, action["boost"], radius, height)

        fight_while_withdrawing = bool(retreat and target and target["kind"] == "mech" and
                                      len(near) == 1 and target["id"] == near[0]["id"])
        if retreat and target and not fight_while_withdrawing and (
                target["kind"] != "mech" or (distance(pos, target["pos"]) > 42 and frac < 0.55)):
            target = None
        aim, fire = self.combat(me, target, muzzle, retreat and not fight_while_withdrawing)
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
        action["aim"] = aim
        action["fire"] = fire
        gun = next((w for w in me["weapons"] if w and w["kind"] == "gun"), None)
        if gun and gun.get("reload", 0) == 0 and gun.get("ammo", 0) < gun["stats"]["mag"] * 0.6:
            action["reload"] = target is None and t - self.firing_time > 0.55
        return action


if __name__ == "__main__":
    mechfight.run(Bot)
