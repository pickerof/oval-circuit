"""Choose a combat and movement policy from the visible opening terrain."""

from math import cos, pi, sin

from bot import Bot as EarlyWithdrawal
from reclaimer_bot import Bot as ReturnFire
import mechfight


class Bot:
    name = "Bing Shing"

    def __init__(self, hello):
        self.withdrawal = EarlyWithdrawal(hello)
        self.return_fire = ReturnFire(hello)
        self.policy = None

    def act(self, obs):
        if self.policy is None:
            self.policy = self.opening_policy(obs)
            self.withdrawal = None
            self.return_fire = None
        return self.policy.act(obs)

    def opening_policy(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        stats = me["stats"]
        arena = self.withdrawal.arena
        origin = x, y, z + stats.get("eye_height", stats["muzzle_height"])
        covered = 0
        walkable = 0
        for direction in range(8):
            yaw = direction * pi / 4
            dx, dy = cos(yaw), sin(yaw)
            ex, ey = x + 54 * dx, y + 54 * dy
            elevation = arena.ground(ex, ey)
            if not arena.visible(origin, (ex, ey, elevation + stats["muzzle_height"]), stride=3.2):
                covered += 1
            exit_point = x + 14 * dx, y + 14 * dy
            if arena.walk_line(me["pos"], exit_point, stats["radius"] + 0.25, stats["height"]):
                walkable += 1
        if covered >= 4 and walkable >= 3:
            return self.return_fire
        return self.withdrawal


if __name__ == "__main__":
    mechfight.run(Bot)
