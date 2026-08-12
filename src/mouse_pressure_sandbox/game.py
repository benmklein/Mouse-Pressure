"""Pygame presentation and loop for the bundled driver-test sandbox."""

from __future__ import annotations

from collections import deque

import pygame

from mouse_pressure_sandbox.input import PressureSensorReader
from mouse_pressure_sandbox.physics import RockSimulation, Vec2


WINDOW_SIZE = (1280, 760)
FIXED_DT = 1.0 / 120.0
MAX_STEPS_PER_FRAME = 8
WORLD_ZOOM = 0.62
HUD_HEIGHT = 72
FOOTER_HEIGHT = 24
HOOP_RADIUS = 82.0

COLORS = {
    "background": "#0D111B",
    "grid": "#171E2D",
    "panel": "#171D29",
    "panel_border": "#2A3345",
    "text": "#F4F7FB",
    "muted": "#A8B2C3",
    "accent": "#7C6CFF",
    "accent_bright": "#9D92FF",
    "cyan": "#52D6C8",
    "orange": "#FFAA5C",
    "rock": "#7D8798",
    "rock_dark": "#485263",
    "ground": "#222A38",
}


class PressureSandbox:
    def __init__(self) -> None:
        pygame.init()
        pygame.display.set_caption("Mouse Pressure Sandbox")
        try:
            self.screen = pygame.display.set_mode(WINDOW_SIZE, pygame.RESIZABLE, vsync=1)
        except TypeError:
            self.screen = pygame.display.set_mode(WINDOW_SIZE, pygame.RESIZABLE)
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("Segoe UI", 18)
        self.small_font = pygame.font.SysFont("Segoe UI", 14)
        self.sensor = PressureSensorReader()
        self.sensor.start()
        self.simulation = RockSimulation(Vec2(620, 420))
        self.simulation.min_chain_length = 100.0
        self.simulation.max_chain_length = 800.0
        self.simulation.chain_length = 360.0
        self.pressure_retracts = True
        self.locked_mode = True
        self.locked_anchor = Vec2(self.simulation.position)
        self.locked_attachment_length = self.simulation.chain_length
        self.fallback_pressure = 0.0
        self.space_down = False
        self.right_down = False
        self.running = True
        self.accumulator = 0.0
        self.trail: deque[Vec2] = deque(maxlen=90)
        self.toggle_rect = pygame.Rect(0, 0, 190, 38)
        self.anchor_toggle_rect = pygame.Rect(0, 0, 190, 38)
        self.hoop_position = self._default_hoop_position()
        self.hoop_was_inside = False
        self.score = 0
        self.score_flash = 0.0

    def run(self) -> None:
        try:
            while self.running:
                frame_dt = min(0.05, self.clock.tick(144) / 1000.0)
                self._handle_events()
                snapshot = self.sensor.snapshot()
                left_pressure, right_down = self._input_state(snapshot)
                mouse = self._screen_to_world(Vec2(pygame.mouse.get_pos()))

                if right_down and not self.right_down:
                    distance = mouse.distance_to(self.simulation.position)
                    self.simulation.max_chain_length = max(800.0, distance + 450.0)
                    grabbed = self.simulation.try_grab(mouse, require_hit=False)
                    if grabbed:
                        self.locked_anchor = Vec2(mouse)
                        self.locked_attachment_length = self.simulation.chain_length
                elif not right_down and self.right_down:
                    self.simulation.release()
                self.right_down = right_down

                active_anchor = (
                    self.locked_anchor
                    if self.locked_mode and self.simulation.tethered
                    else mouse
                )
                if self.locked_mode and self.simulation.tethered:
                    target_length = self.simulation.target_length_from_attachment(
                        left_pressure,
                        attachment_length=self.locked_attachment_length,
                        pressure_retracts=self.pressure_retracts,
                    )
                else:
                    target_length = self.simulation.target_length(
                        left_pressure,
                        pressure_retracts=self.pressure_retracts,
                    )

                self.accumulator += frame_dt
                bounds = self._world_bounds()
                steps = 0
                while self.accumulator >= FIXED_DT and steps < MAX_STEPS_PER_FRAME:
                    self.simulation.step(
                        FIXED_DT,
                        anchor=active_anchor,
                        target_length=target_length,
                        bounds=bounds,
                    )
                    self.accumulator -= FIXED_DT
                    steps += 1
                if steps == MAX_STEPS_PER_FRAME:
                    self.accumulator = 0.0

                self._update_hoop_score(frame_dt)
                self.trail.append(Vec2(self.simulation.position))
                self._draw(left_pressure, target_length, active_anchor)
                pygame.display.flip()
        finally:
            self.sensor.stop()
            pygame.quit()

    def _handle_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    self.running = False
                elif event.key == pygame.K_SPACE:
                    self.space_down = True
                elif event.key == pygame.K_i:
                    self.pressure_retracts = not self.pressure_retracts
                elif event.key == pygame.K_l:
                    self._toggle_locked_mode()
                elif event.key == pygame.K_r:
                    self._reset()
                elif event.key == pygame.K_F5:
                    self.sensor.retry()
            elif event.type == pygame.KEYUP and event.key == pygame.K_SPACE:
                self.space_down = False
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                if self.toggle_rect.collidepoint(event.pos):
                    self.pressure_retracts = not self.pressure_retracts
                elif self.anchor_toggle_rect.collidepoint(event.pos):
                    self._toggle_locked_mode()
                else:
                    self.fallback_pressure = 1.0
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                self.fallback_pressure = 0.0
            elif event.type == pygame.MOUSEWHEEL:
                self.fallback_pressure = max(
                    0.0, min(1.0, self.fallback_pressure + event.y * 0.08)
                )
            elif event.type == pygame.VIDEORESIZE:
                self.screen = pygame.display.set_mode(event.size, pygame.RESIZABLE)
                self.hoop_position = self._default_hoop_position()

        if pygame.mouse.get_pressed(num_buttons=3)[0]:
            self.fallback_pressure = 1.0

    def _reset(self) -> None:
        bounds = self._world_bounds()
        self.simulation.reset(Vec2(bounds.left + bounds.width * 0.32, bounds.centery))
        self.simulation.min_chain_length = 100.0
        self.simulation.max_chain_length = 800.0
        self.simulation.chain_length = 360.0
        self.trail.clear()

    def _toggle_locked_mode(self) -> None:
        self.locked_mode = not self.locked_mode
        if not self.simulation.tethered:
            return
        mouse_world = self._screen_to_world(Vec2(pygame.mouse.get_pos()))
        if self.locked_mode:
            self.locked_anchor = mouse_world
            self.locked_attachment_length = self.simulation.chain_length
        else:
            pygame.mouse.set_pos(self._world_to_screen(self.locked_anchor))

    def _input_state(self, snapshot: object) -> tuple[float, bool]:
        if bool(getattr(snapshot, "connected")):
            pressure = float(getattr(snapshot, "left_pressure"))
            right_pressure = float(getattr(snapshot, "right_pressure"))
            right_down = right_pressure >= (0.025 if self.right_down else 0.055)
            return pressure, right_down or self.space_down
        mouse_right = bool(pygame.mouse.get_pressed(num_buttons=3)[2])
        return self.fallback_pressure, mouse_right or self.space_down

    def _world_bounds(self) -> pygame.Rect:
        width, height = self.screen.get_size()
        return pygame.Rect(
            round(12 / WORLD_ZOOM),
            round(HUD_HEIGHT / WORLD_ZOOM),
            round(max(100, width - 24) / WORLD_ZOOM),
            round(max(100, height - HUD_HEIGHT - FOOTER_HEIGHT) / WORLD_ZOOM),
        )

    @staticmethod
    def _screen_to_world(point: Vec2) -> Vec2:
        return Vec2(point) / WORLD_ZOOM

    @staticmethod
    def _world_to_screen(point: Vec2) -> Vec2:
        return Vec2(point) * WORLD_ZOOM

    def _default_hoop_position(self) -> Vec2:
        bounds = self._world_bounds()
        return Vec2(bounds.left + bounds.width * 0.79, bounds.top + bounds.height * 0.34)

    def _update_hoop_score(self, frame_dt: float) -> None:
        distance = self.simulation.position.distance_to(self.hoop_position)
        inside = distance <= HOOP_RADIUS - self.simulation.radius * 0.65
        if inside and not self.hoop_was_inside and self.simulation.speed(FIXED_DT) > 140:
            self.score += 1
            self.score_flash = 0.45
        self.hoop_was_inside = inside
        self.score_flash = max(0.0, self.score_flash - frame_dt)

    def _draw(self, pressure: float, target_length: float, anchor: Vec2) -> None:
        width, height = self.screen.get_size()
        self.screen.fill(COLORS["background"])
        self._draw_grid(width, height)
        self._draw_hoop()
        self._draw_trail()
        if self.simulation.tethered:
            self._draw_chain(anchor, self.simulation.position)
            anchor_screen = self._world_to_screen(anchor)
            pygame.draw.circle(self.screen, COLORS["cyan"], anchor_screen, 8, 2)
            pygame.draw.circle(self.screen, COLORS["cyan"], anchor_screen, 3)
        self._draw_rock()
        self._draw_header(pressure, target_length, width)
        self._draw_keybinds(height)

    def _draw_grid(self, width: int, height: int) -> None:
        spacing = round(80 * WORLD_ZOOM)
        for x in range(0, width, spacing):
            pygame.draw.line(self.screen, COLORS["grid"], (x, HUD_HEIGHT), (x, height - FOOTER_HEIGHT))
        for y in range(HUD_HEIGHT, height - FOOTER_HEIGHT + 1, spacing):
            pygame.draw.line(self.screen, COLORS["grid"], (0, y), (width, y))
        pygame.draw.rect(self.screen, COLORS["ground"], (0, height - FOOTER_HEIGHT, width, FOOTER_HEIGHT))

    def _draw_hoop(self) -> None:
        center = self._world_to_screen(self.hoop_position)
        outer = round(HOOP_RADIUS * WORLD_ZOOM)
        inner = round((HOOP_RADIUS - 18) * WORLD_ZOOM)
        color = "#FFD166" if self.score_flash > 0 else COLORS["orange"]
        pygame.draw.circle(self.screen, color, center, outer)
        pygame.draw.circle(self.screen, COLORS["background"], center, inner)
        pygame.draw.circle(self.screen, COLORS["panel_border"], center, inner, 2)

    def _draw_trail(self) -> None:
        if len(self.trail) < 2:
            return
        layer = pygame.Surface(self.screen.get_size(), pygame.SRCALPHA)
        points = list(self.trail)
        for index in range(1, len(points)):
            alpha = round(8 + 72 * index / len(points))
            pygame.draw.line(
                layer,
                (124, 108, 255, alpha),
                self._world_to_screen(points[index - 1]),
                self._world_to_screen(points[index]),
                3,
            )
        self.screen.blit(layer, (0, 0))

    def _draw_chain(self, anchor: Vec2, rock: Vec2) -> None:
        a, b = self._world_to_screen(anchor), self._world_to_screen(rock)
        pygame.draw.line(self.screen, COLORS["rock_dark"], a, b, 6)
        pygame.draw.line(self.screen, COLORS["accent_bright"], a, b, 2)
        delta = rock - anchor
        distance = delta.length()
        if distance <= 0.001:
            return
        direction = delta / distance
        for offset in range(12, round(distance), 18):
            point = self._world_to_screen(anchor + direction * offset)
            pygame.draw.circle(self.screen, COLORS["panel"], point, 4)
            pygame.draw.circle(self.screen, COLORS["accent_bright"], point, 4, 2)

    def _draw_rock(self) -> None:
        pos = self._world_to_screen(self.simulation.position)
        radius = round(self.simulation.radius * WORLD_ZOOM)
        pygame.draw.circle(self.screen, COLORS["rock_dark"], pos, radius + 2)
        pygame.draw.circle(self.screen, COLORS["rock"], pos, radius)

    def _draw_header(self, pressure: float, target_length: float, width: int) -> None:
        pygame.draw.rect(self.screen, COLORS["panel"], (0, 0, width, HUD_HEIGHT))
        pygame.draw.line(self.screen, COLORS["panel_border"], (0, HUD_HEIGHT - 1), (width, HUD_HEIGHT - 1))
        self._text("LEFT PRESSURE", (24, 10), self.small_font, COLORS["muted"])
        bar = pygame.Rect(24, 37, 230, 13)
        pygame.draw.rect(self.screen, COLORS["background"], bar, border_radius=7)
        fill = bar.copy()
        fill.width = round(bar.width * pressure)
        pygame.draw.rect(self.screen, COLORS["orange"], fill, border_radius=7)
        self._text(f"{pressure:4.0%}", (bar.right + 10, 31), self.font)
        self._text(
            f"Chain {self.simulation.chain_length:3.0f}px  ·  Target {target_length:3.0f}px",
            (330, 29), self.small_font, COLORS["muted"]
        )
        score_color = "#FFD166" if self.score_flash > 0 else COLORS["text"]
        score = self.font.render(f"HOOPS  {self.score}", True, score_color)
        self.screen.blit(score, score.get_rect(center=(width // 2, 36)))
        self.anchor_toggle_rect = pygame.Rect(width - 430, 15, 196, 42)
        self._draw_toggle(
            self.anchor_toggle_rect,
            "Anchor = Locked" if self.locked_mode else "Anchor = Follow",
            self.locked_mode,
        )
        self.toggle_rect = pygame.Rect(width - 220, 15, 196, 42)
        self._draw_toggle(
            self.toggle_rect,
            "Press = Retract" if self.pressure_retracts else "Press = Extend",
            self.pressure_retracts,
        )

    def _draw_toggle(self, rect: pygame.Rect, text: str, active: bool) -> None:
        color = COLORS["accent"] if active else COLORS["panel_border"]
        pygame.draw.rect(self.screen, color, rect, border_radius=10)
        pygame.draw.rect(self.screen, COLORS["accent_bright"], rect, 2, border_radius=10)
        label = self.small_font.render(text, True, COLORS["text"])
        self.screen.blit(label, label.get_rect(center=rect.center))

    def _draw_keybinds(self, height: int) -> None:
        self._text(
            "Space  grab/release     I  invert pressure     L  lock anchor     R  reset     F5  reconnect     Esc  quit",
            (18, height - 5), self.small_font, COLORS["muted"], bottom=True,
        )

    def _text(
        self,
        text: str,
        position: tuple[int, int],
        font: pygame.font.Font,
        color: str | tuple[int, int, int] | None = None,
        *,
        bottom: bool = False,
    ) -> None:
        rendered = font.render(text, True, color or COLORS["text"])
        rect = rendered.get_rect()
        if bottom:
            rect.bottomleft = position
        else:
            rect.topleft = position
        self.screen.blit(rendered, rect)
