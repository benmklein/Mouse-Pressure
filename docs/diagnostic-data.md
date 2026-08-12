# Diagnostic data policy

Raw USB captures, HID instance paths, runtime handles, full stroke traces, and
unreviewed terminal logs are development artifacts and are not committed to the
public source tree. The repository retains derived protocol descriptions and
small sanitized analysis summaries needed to reproduce engineering decisions.

Before committing diagnostic material:

1. Prefer a minimal derived table or test fixture over a raw capture.
2. Remove usernames, home-directory paths, device-instance paths, serial
   numbers, runtime handles, document names, and unrelated traffic.
3. Remove pointer geometry when it could reproduce private drawing content.
4. Run `python scripts/check_public_artifacts.py`.
5. Manually inspect the diff; automated patterns are not a privacy guarantee.

Raw captures needed for private development belong outside the repository or in
an encrypted, access-controlled archive with a documented retention period.
`scripts/analyze_input_backend_evidence.py --capture-root <private-directory>`
writes its generated report under `work/` by default so analysis does not
silently republish the source captures.
