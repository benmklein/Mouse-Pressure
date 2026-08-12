# Corresponding source and replacement information

This notice applies to the official Mouse Pressure Windows binary distribution.
The exact source revision and dependency versions are recorded in
`SOURCE_REVISION.txt` and `mouse-pressure.cdx.json` beside this file.

## Mouse Pressure

The complete preferred source for the application is available at:

https://github.com/benmklein/analog_mouse_pressure

## Qt for Python, PySide6, and Shiboken6

Mouse Pressure 0.1.x uses the dynamically loaded open-source wheels recorded in
the SBOM. Source for PySide6/Shiboken6 6.11.1 is available from Qt:

https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/

Source for the corresponding Qt 6.11.1 modules is available from:

https://download.qt.io/official_releases/qt/6.11/6.11.1/submodules/

The distribution also includes QtBase 6.11.1's complete upstream `LICENSES`
directory under `licenses/qtbase-6.11.1`.

The libraries are stored as separate DLL/PYD files beneath the installed
application's `_internal` directory. A user may replace them with ABI-compatible
modified builds. Replacing a signed file may invalidate the distributor's
signature on that file but does not require a Mouse Pressure authorization key.

## pygame-ce and SDL dependencies

Source for pygame-ce 2.5.8, including its build scripts and third-party notices,
is available from:

https://github.com/pygame-community/pygame-ce/releases/tag/2.5.8

The pressure sandbox loads pygame-ce and its SDL-family DLLs dynamically from
its `_internal` directory.

## Source-copy offer

If an upstream source link becomes unavailable, open a request at
https://github.com/benmklein/analog_mouse_pressure/issues with the exact Mouse
Pressure release version. For at least three years after that binary release,
the publisher will provide a machine-readable copy of the corresponding LGPL
and GPL source covered by this notice for no more than the reasonable physical
cost of transferring it.
