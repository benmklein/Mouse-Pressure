# Third-party notices

This file describes the principal third-party components shipped by the
official Mouse Pressure Windows distribution. Exact versions for a particular
build are recorded in `mouse-pressure.cdx.json` beside the installed notices.

| Component | Use | License |
| --- | --- | --- |
| Python | Embedded application runtime | PSF License Version 2 |
| PySide6 Essentials and Shiboken6 | Desktop interface and Qt bindings | LGPL-3.0-only, GPL alternatives, or commercial terms |
| Qt 6 | Dynamically loaded desktop interface libraries | LGPL-3.0 and other component licenses |
| cython-hidapi / HIDAPI | Access to compatible HID interfaces | BSD-3-Clause selected from its offered alternatives |
| pygame-ce and bundled SDL runtime | Pressure Sandbox | LGPL-2.1-only and component-specific licenses |
| PyInstaller bootloader | Frozen Windows executables | GPL-2.0-or-later with Bootloader Exception; runtime hooks include Apache-2.0/MIT material |
| Lucide | Mouse icon | ISC |

The Pressure Sandbox's pygame-ce wheel also carries native libraries including
SDL2, SDL2_image, SDL2_mixer, SDL_gfx, FreeType, FLAC, JPEG, Ogg/Vorbis, Opus,
PNG, PortMidi, TIFF, WebP, and zlib. Their pinned upstream notices are included
under `licenses/pygame-ce`, and each is represented in the release SBOM.

## Copyright notices

### Lucide

Portions are Copyright (c) Cole Bemis and Lucide Contributors under the ISC
License. The complete license text is included in `licenses/Lucide-ISC.txt`.

### HIDAPI bindings

The application selects the BSD-style option offered by `cython-hidapi`.
Copyright (c) 2011 Gary Bishop. The complete selected license is included in
`licenses/HIDAPI-BSD-3-Clause.txt`.

## LGPL use and replacement

The official Windows build uses the open-source LGPL editions of PySide6, Qt,
Shiboken6, and pygame-ce. Their DLLs and Python extension modules remain
separate dynamically loaded files in the installed application directories;
they are not statically linked into project code. Users may inspect or replace
those libraries with compatible modified builds. Mouse Pressure does not add
DRM or contractual restrictions that prohibit modification or reverse
engineering for exercising LGPL rights.

Corresponding upstream source locations and a source-copy offer are provided
in `SOURCE_OFFER.md`. Full standard license texts are in the `licenses`
directory. Qt and pygame-ce include additional third-party components under
their own permissive or weak-copyleft terms. The pygame-ce 2.5.8 component
notices are included under `licenses/pygame-ce`. QtBase 6.11.1's complete
upstream license-text set is under `licenses/qtbase-6.11.1`, and corresponding
source is identified in `SOURCE_OFFER.md`.
