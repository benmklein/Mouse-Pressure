from __future__ import annotations

import pygame

from mouse_pressure_sandbox.physics import RockSimulation, Vec2


DT = 1.0 / 120.0
BOUNDS = pygame.Rect(0, 0, 1200, 700)


def test_pressure_can_extend_or_retract_chain() -> None:
    simulation = RockSimulation(Vec2(400, 300))
    assert simulation.target_length(1.0, pressure_retracts=False) == simulation.max_chain_length
    assert simulation.target_length(1.0, pressure_retracts=True) == simulation.min_chain_length


def test_locked_mode_can_attach_from_any_distance() -> None:
    simulation = RockSimulation(Vec2(900, 500))
    anchor = Vec2(100, 100)
    assert simulation.try_grab(anchor, require_hit=False)
    assert simulation.chain_length == anchor.distance_to(simulation.position)


def test_taut_chain_respects_current_length() -> None:
    simulation = RockSimulation(Vec2(500, 300))
    simulation.tethered = True
    simulation.chain_length = 120.0
    simulation.previous_position = Vec2(450, 300)
    anchor = Vec2(300, 300)
    simulation.step(DT, anchor=anchor, target_length=120.0, bounds=BOUNDS)
    assert simulation.position.distance_to(anchor) <= 120.001
