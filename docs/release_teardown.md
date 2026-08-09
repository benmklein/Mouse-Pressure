# Synthetic Pen Release Teardown

## Problem

In some Windows Ink apps (notably Krita), releasing a synthetic pen stroke with
only `UP | INRANGE` can leave a visible end-of-stroke tail/lag.

Observed symptom:

- Stroke start and body are smooth.
- On release, stroke appears to linger for ~100-300ms.

## Root Behavior

The pointer may remain "in range" after `UP`, and some apps continue smoothing
the stroke until they see a clearer pointer teardown.

## Default behavior

The normal release sends `UP` without `INRANGE`, ending both contact and hover.
This avoids retaining a synthetic hover pointer at the release coordinate,
which can pull the mouse cursor back after physical movement resumes.

## Optional compatibility mitigation

Only if Krita visibly keeps extending a stroke after release, enable the
experimental teardown sequence (`--release-teardown`):

1. `UP | INRANGE`
2. `UPDATE | INRANGE` (hover frame)
3. `UPDATE` (out-of-range / end-hover frame)

This sequence is implemented in `SyntheticPenEmitter._emit_release_teardown()`.

## Verification

Run:

```bash
uv run python scripts/pressure_to_pen.py --release-teardown
```

Check in Krita Tablet Tester that pen-up ends cleanly and end-of-stroke lag is
reduced.

## Regression Guard

Automated unit tests assert release frame sequences:

```bash
uv run python -m unittest tests.test_synthetic_pen_release
```
