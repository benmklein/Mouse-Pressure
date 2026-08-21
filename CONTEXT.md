# Project context

## Glossary

### Stroke planner

The stateful, synchronous logic that turns current pressure and mouse-button
facts into an ordered pen-report sequence. It owns contact transitions,
pressure shaping, path preparation, stationary behavior, and release behavior.

### Pen emitter

The lifecycle-facing interface used by the runtime to start, stop, and advance
pressure output. It delegates stroke behavior to the stroke planner.

### Pen report

One synthetic Windows pen update with position, pressure, optional tilt or
rotation, pointer flags, and a diagnostic tag. Report order is observable
behavior.

### Managed button

A mouse button configured to produce pressure, X-tilt, Y-tilt, or rotation
output. A stroke has at most one pressure-owning managed button, while another
managed button may simultaneously modify a pen property.

### Device-settings lease

The temporary ownership of a mouse's original DPI, haptic, and onboard-profile
settings while pressure output is active. The lease applies session settings,
keeps the original snapshot through live changes, restores it on Stop or startup
failure, and disarms crash recovery only after restoration succeeds.

### Pen output

The active native pen-output session. It composes the stroke emitter, native
injector, and transformed-input capture, then owns staged startup, input arming,
updates, fail-open behavior, reconfiguration, and shutdown ordering.

### Settings draft

A toolkit-independent snapshot of every editable driver setting plus the normal
mouse hardware state and injection rate. It owns linked-button semantics,
validation, reset behavior, runtime patch creation, and the pressure values used
by both the graph and live telemetry.

### Native relay binding

The single Python definition of the native relay DLL's structure layouts,
exported functions, discovery order, and version check. Native mouse capture
and pen injection are separate adapters over this binding.
