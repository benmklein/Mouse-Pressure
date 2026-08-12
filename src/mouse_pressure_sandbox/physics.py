"""Fixed-step Verlet physics for a retractable chain and throwable ball."""
from __future__ import annotations

from dataclasses import dataclass, field

import pygame


Vec2 = pygame.Vector2


def move_toward(current: float, target: float, amount: float) -> float:
    if current < target:
        return min(target, current + amount)
    return max(target, current - amount)


@dataclass
class RockSimulation:
    position: Vec2
    radius: float = 34.0
    gravity: Vec2 = field(default_factory=lambda: Vec2(0.0, 1450.0))
    previous_position: Vec2 = field(init=False)
    previous_anchor: Vec2 = field(init=False)
    tethered: bool = False
    chain_length: float = 230.0
    min_chain_length: float = 80.0
    max_chain_length: float = 420.0
    reel_speed: float = 760.0
    restitution: float = 0.68
    air_retention: float = 0.9992

    def __post_init__(self) -> None:
        self.position = Vec2(self.position)
        self.previous_position = Vec2(self.position)
        self.previous_anchor = Vec2(self.position)

    @property
    def velocity_per_step(self) -> Vec2:
        return self.position - self.previous_position

    def speed(self, dt: float) -> float:
        return self.velocity_per_step.length() / max(0.000001, dt)

    def reset(self, position: Vec2) -> None:
        self.position = Vec2(position)
        self.previous_position = Vec2(position)
        self.previous_anchor = Vec2(position)
        self.tethered = False
        self.chain_length = 230.0

    def try_grab(
        self,
        anchor: Vec2,
        *,
        padding: float = 24.0,
        require_hit: bool = True,
    ) -> bool:
        anchor = Vec2(anchor)
        distance = anchor.distance_to(self.position)
        if require_hit and distance > self.radius + padding:
            return False
        self.tethered = True
        self.previous_anchor = anchor
        self.max_chain_length = max(self.max_chain_length, distance)
        self.chain_length = max(self.min_chain_length, distance)
        return True

    def release(self) -> None:
        self.tethered = False

    def target_length(self, pressure: float, *, pressure_retracts: bool) -> float:
        pressure = max(0.0, min(1.0, float(pressure)))
        if pressure_retracts:
            pressure = 1.0 - pressure
        return self.min_chain_length + pressure * (
            self.max_chain_length - self.min_chain_length
        )

    def target_length_from_attachment(
        self,
        pressure: float,
        *,
        attachment_length: float,
        pressure_retracts: bool,
    ) -> float:
        pressure = max(0.0, min(1.0, float(pressure)))
        base = max(
            self.min_chain_length,
            min(self.max_chain_length, float(attachment_length)),
        )
        endpoint = self.min_chain_length if pressure_retracts else self.max_chain_length
        return base + (endpoint - base) * pressure

    def step(
        self,
        dt: float,
        *,
        anchor: Vec2,
        target_length: float,
        bounds: pygame.Rect,
    ) -> None:
        dt = max(0.0001, float(dt))
        anchor = Vec2(anchor)
        old_position = Vec2(self.position)
        motion = (self.position - self.previous_position) * self.air_retention
        self.previous_position = old_position
        self.position += motion + self.gravity * (dt * dt)

        old_length = self.chain_length
        if self.tethered:
            self.chain_length = move_toward(
                self.chain_length,
                max(self.min_chain_length, min(self.max_chain_length, target_length)),
                self.reel_speed * dt,
            )
            delta = self.position - anchor
            distance = delta.length()
            if distance > self.chain_length and distance > 0.0001:
                normal = delta / distance
                self.position = anchor + normal * self.chain_length
                corrected_motion = self.position - old_position
                radial = normal * min(0.0, corrected_motion.dot(normal))
                tangent = corrected_motion - normal * corrected_motion.dot(normal)
                if self.chain_length < old_length:
                    tangent *= min(1.12, old_length / max(self.chain_length, 1.0))
                self.previous_position = self.position - radial - tangent
            self.previous_anchor = anchor

        self._collide(bounds)

    def _collide(self, bounds: pygame.Rect) -> None:
        velocity = self.position - self.previous_position
        left = bounds.left + self.radius
        right = bounds.right - self.radius
        top = bounds.top + self.radius
        bottom = bounds.bottom - self.radius

        if self.position.x < left:
            self.position.x = left
            self.previous_position.x = self.position.x + velocity.x * self.restitution
        elif self.position.x > right:
            self.position.x = right
            self.previous_position.x = self.position.x + velocity.x * self.restitution

        if self.position.y < top:
            self.position.y = top
            self.previous_position.y = self.position.y + velocity.y * self.restitution
        elif self.position.y > bottom:
            self.position.y = bottom
            self.previous_position.y = self.position.y + velocity.y * self.restitution
            horizontal = self.position.x - self.previous_position.x
            self.previous_position.x = self.position.x - horizontal * 0.985
