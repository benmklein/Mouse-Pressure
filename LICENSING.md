# Licensing

The Mouse Pressure desktop application, native relay, pressure sandbox, and
Python libraries are licensed under the [MIT License](LICENSE).

The Windows distribution contains separately licensed runtime components,
including Qt for Python/PySide6, pygame-ce, HIDAPI, Python, PyInstaller's
bootloader, and Lucide artwork. Their licenses remain in force. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), the checked-in
`packaging/legal/licenses` directory, and the CycloneDX SBOM generated with:

```powershell
.\.venv\Scripts\python.exe scripts\generate_release_metadata.py
```

The public installer does not distribute VMulti, a kernel driver, or the
historical Krita plugin experiment. Source-tree experiments that are not part
of the binary distribution retain the licenses stated in their own files.

Redistributing Mouse Pressure does not grant rights to Logitech, Microsoft,
Krita, Qt, or other third-party names and marks beyond those supplied by
applicable law and their respective owners.
