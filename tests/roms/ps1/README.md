# PS1 hardware tests

Ten programs from [JaCzekanski/ps1-tests](https://github.com/JaCzekanski/ps1-tests)
(MIT, see `LICENSE`), built with PSn00bSDK. Each folder holds the `.exe` and what a
real console produced: `psx.log` (TTY output) or `vram.png` (frame buffer).
`tests/test_ps1.py` runs them against the Rust core.

The bundled `otc-test.exe` is a newer build than its `psx.log`: it prints two extra
passing sub-tests, so its log is checked in order rather than line for line.
