# LinkedIn launch post

Attach `retropy-demo.mp4` (or `../games.png` as the image). Put the repo link in the
post itself; LinkedIn shows a preview card for it.

---

## Main version (~230 words)

I've been building RetroPy, a retro console emulator, for a while now, and it's ready for people to try.

It emulates the Game Boy, Game Boy Color, Game Boy Advance and NES, and all four are written in pure Python. There's no C extension and no native code. Python is slow, so most of the work went into making it fast enough: generated CPU interpreters, catch-up timing, cached scanline rendering, and frame pacing driven by the audio clock. Most cores run faster than real time on a laptop. The heavy GBA scenes need frameskip.

On top of the cores there's a Flet desktop app:
- a library with covers
- save states on every core, plus 30 seconds of rewind
- MP4 recording with sound, and "clip the last 30 s"
- gamepads, and per-console key layouts

CI builds a Windows installer and a .deb for every release.

The newest part is an experimental PlayStation core in Rust, loaded through PyO3. I've been checking it against logs recorded on real hardware. The GTE (the 3D maths coprocessor) matches the console on 1,100 out of 1,100 fuzz cases, and the MDEC video decoder and the SPU sound chip are both in. To be clear, commercial PS1 games don't run yet. The CD-ROM drive is the next milestone.

The video shows my own cartridge dumps; no games ship with the project. Accuracy is tracked with blargg's tests, nestest, Acid2 and jsmolka's GBA tests.

If you like emulators, Python performance work, or just old games, try it, break it and open an issue. A star helps too.

https://github.com/abhishekbiswas772/RetroPy

#Python #Emulation #Rust #OpenSource #RetroGaming #GameBoy

---

## Short version (~85 words)

I wrote a Game Boy, Game Boy Color, GBA and NES emulator in pure Python. It's called RetroPy.

It comes with a desktop app that has save states, rewind, MP4 recording and gamepad support, and CI builds a Windows installer and a .deb. There's also an experimental PS1 core in Rust, which matches real-hardware logs on all 1,100 GTE fuzz cases. PS1 games don't boot yet, because CD-ROM support is next.

The clips are from my own cartridges. Try it, and stars are welcome:
https://github.com/abhishekbiswas772/RetroPy

#Python #Emulation #OpenSource #Rust
