# PS1 hardware tests

Programs from [JaCzekanski/ps1-tests](https://github.com/JaCzekanski/ps1-tests)
(MIT, see `LICENSE`), built with PSn00bSDK. Each folder holds the `.exe` and what a
real console produced: `psx.log` (TTY output) or `vram.png` (frame buffer).
`tests/test_ps1.py` runs them against the Rust core.

| Folder | Checks | Reference |
|---|---|---|
| cop, code-in-io, dpcr, otc-test, gp0-e1, mask-bit | CPU, DMA, GPU registers | `psx.log` |
| test-all | GTE | `psx.log` |
| gte-fuzz | GTE, 1,100 random commands | log of every register before/after |
| rectangles, triangle, transparency, quad | GPU drawing | `vram.png` |
| 4bit, 8bit, frame-15bit-dma | MDEC decoding | `vram.png` |
| memory-transfer | SPU RAM: manual, DMA, DMA timing | `psx.log` |
| stereo | SPU voices and panning | none (checked for left/right by design of the test) |

`otc-test.exe`, `test-all.exe` and `memory-transfer.exe` are newer builds than their
`psx.log`: they print extra passing sub-tests, so their logs are checked in order
rather than line for line.
