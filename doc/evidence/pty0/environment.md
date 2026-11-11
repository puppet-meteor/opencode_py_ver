# PTY T0 spike evidence environment (T-C1-071)

Local manual run of `tools/pty0/pty_probe.py` (probe version `pty0-1`) that
produced `platform.json` in this directory. Human environment note for the
committed evidence; the machine-recorded, leak-guarded data lives in the
JSON artifact itself.

- Platform: Windows 11, AMD64 (`sys.platform == "win32"`), Python 3.12.
- Environment: the project `uv` workspace with the pywinpty extras
  (`opencode-py[pty]`, pywinpty 3.1.2) installed; the probe drives pywinpty's
  ConPTY/WinPTY backends through ctypes/kernel32 directly -- no `src/` code
  is imported or registered anywhere in this spike (zero src wiring).
- Generation mode: `manual` (the `ci` face comes from the CI dual-OS matrix;
  manual and ci_verified never impersonate each other).
- Sanitization: host paths appear only as `<WORKSPACE>`/`<TEMP>` placeholders;
  no usernames, PIDs, prompts, secrets or endpoints are recorded. The
  committed artifact is guarded by the leak tests in
  `tests/integration/test_pty0_sanitization.py`.
- Verdicts on this run: resize / ctrl_c / encoding / process_tree_exit all
  `verified`. The ctrl_c record carries both backend observations because the
  two pywinpty faces differ on this stack: the legacy winpty agent delivers
  PTY-written `\x03` (child KeyboardInterrupt, exit code 130), while the
  vendored ConPTY host does not dispatch the control event within the
  observation window (child sleeps through; the verdict is carried by the
  delivering backend, and both faces are recorded).
- The POSIX leg is NOT run on this seat: it is carried by the CI dual-OS
  matrix and closed by the dispatcher (`os.forkpty` + `TIOCSWINSZ` +
  `os.killpg` under the same four primitive tests). A Windows seat never
  impersonates a POSIX result.
