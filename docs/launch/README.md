# Launch kit

Material for announcing RetroPy (LinkedIn, release notes, social posts).

| File | What it is |
|---|---|
| [`retropy-demo.mp4`](retropy-demo.mp4) | A ~45 s demo, 1280×720, 30 fps, H.264, silent. It runs: title card → Pokémon Red (GB) → Zelda: Oracle of Ages (GBC) → Pokémon FireRed (GBA) → Contra (NES) → the app (library, save states and rewind, recording, controls) → closing card with the repo URL. |
| [`linkedin-post.md`](linkedin-post.md) | The launch post, with a long and a short version. |
| [`../games.png`](../games.png) | One frame per console, for use as a static image or a link preview. |
| [`../game-gb.gif`](../game-gb.gif), [`../game-gbc.gif`](../game-gbc.gif), [`../game-gba.gif`](../game-gba.gif), [`../game-nes.gif`](../game-nes.gif) | Looping clips, one per console. |
| [`../ui-library.png`](../ui-library.png), [`../ui-player.png`](../ui-player.png), [`../ui-recording.png`](../ui-recording.png), [`../ui-settings.png`](../ui-settings.png), [`../ui-moments.png`](../ui-moments.png) | Screenshots of the desktop app (1920×1080 window). |

## How it was made

Every game frame is drawn by RetroPy's own cores. There is no capture from another emulator, and no frame is edited.

1. **Game clips and the banner.** Run `uv run python docs/make_game_media.py`. It boots each ROM headlessly and feeds it a scripted list of button presses. Pokémon Red goes through New Game and Oak's speech, then walks out into Pallet Town. Contra starts stage 1 and runs and shoots. Zelda and FireRed play their intros by themselves. The script writes the GIFs and `games.png`. Battery saves go to a temporary folder. With `--state-dir DIR` it also writes the Pallet Town save state that appears in the player screenshot.
2. **App screenshots.** These are the real Flet app, run with `FLET_APP_STORAGE_DATA` pointed at a throwaway folder, so no real settings, saves or history were touched. The library is the four games in `rom/`. Each window was captured with Win32 `PrintWindow`.
3. **Video.** Run `uv run python docs/launch/make_demo_video.py`. It stitches the GIFs and screenshots with Pillow and encodes them with PyAV, which bundles FFmpeg.

## ROMs are not included

The clips and screenshots come from the author's own cartridge dumps, which are kept in the git-ignored `rom/` folder. No ROM, BIOS or other game data is in this repository. To regenerate the media, use dumps of cartridges you own. A console whose ROM is missing is skipped. Pokémon, Zelda and Contra are trademarks of their owners (Nintendo, Game Freak, Creatures, Capcom, Konami). RetroPy isn't affiliated with any of them.

The PlayStation core doesn't appear in the gameplay footage, because the CD-ROM drive isn't emulated yet.
