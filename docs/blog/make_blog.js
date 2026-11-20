const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell,
  WidthType, ShadingType, BorderStyle, LevelFormat, Footer, PageNumber,
} = require("docx");

const FONT = "Calibri";
const MONO = "Consolas";
const ACCENT = "3D55D6";

const p = (...runs) => new Paragraph({ spacing: { after: 140, line: 300 }, children: runs.map(r => typeof r === "string" ? new TextRun(r) : r) });
const b = t => new TextRun({ text: t, bold: true });
const code = t => new TextRun({ text: t, font: MONO, size: 20, color: "2B2B2B" });
const h1 = t => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(t)] });
const h2 = t => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(t)] });
const bullet = (...runs) => new Paragraph({ numbering: { reference: "bullets", level: 0 }, spacing: { after: 80, line: 290 }, children: runs.map(r => typeof r === "string" ? new TextRun(r) : r) });
const codeBlock = lines => lines.map((l, i) => new Paragraph({
  shading: { type: ShadingType.CLEAR, fill: "F3F4F7", color: "auto" },
  spacing: { after: i === lines.length - 1 ? 160 : 0, line: 260 },
  indent: { left: 200, right: 200 },
  children: [new TextRun({ text: l || " ", font: MONO, size: 19 })],
}));
const callout = (title, text) => new Paragraph({
  shading: { type: ShadingType.CLEAR, fill: "EEF1FC", color: "auto" },
  border: { left: { style: BorderStyle.SINGLE, size: 24, color: ACCENT, space: 8 } },
  indent: { left: 240, right: 240 }, spacing: { before: 80, after: 200, line: 290 },
  children: [new TextRun({ text: title + " ", bold: true, color: ACCENT }), new TextRun(text)],
});

const W = 9360;
function table(headers, rows, widths) {
  const border = { style: BorderStyle.SINGLE, size: 4, color: "C9CDD6" };
  const borders = { top: border, bottom: border, left: border, right: border };
  const cell = (t, i, head) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA }, borders,
    shading: head ? { type: ShadingType.CLEAR, fill: "E4E8F5", color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: [new Paragraph({ alignment: i > 0 && !head && /^[~\d]/.test(t) ? AlignmentType.RIGHT : AlignmentType.LEFT,
      children: [new TextRun({ text: t, bold: head, size: 20 })] })],
  });
  return new Table({
    width: { size: W, type: WidthType.DXA }, columnWidths: widths,
    rows: [new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, i, true)) }),
      ...rows.map(r => new TableRow({ children: r.map((t, i) => cell(t, i, false)) }))],
  });
}
const gap = () => new Paragraph({ spacing: { after: 120 }, children: [] });

const children = [
  new Paragraph({ heading: HeadingLevel.TITLE, children: [new TextRun("Emulating five consoles from Python")] }),
  new Paragraph({ spacing: { after: 80 }, children: [new TextRun({ text: "Performance tricks and hardware-verified accuracy in RetroPy", size: 28, color: "555B6E" })] }),
  new Paragraph({
    spacing: { after: 300 }, border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: ACCENT, space: 6 } },
    children: [new TextRun({ text: "Abhishek Biswas  ·  github.com/abhishekbiswas772/RetroPy", size: 20, color: "777D8F" })],
  }),

  p("RetroPy is a desktop emulator for the Game Boy, Game Boy Color, Game Boy Advance and NES, written in pure Python, with a Flet desktop app on top. It also has an experimental PlayStation core in Rust. That core is wired into the same app through PyO3."),
  p("Two questions usually come up. Can Python be fast enough to emulate a console in real time? And how do you know an emulator is right, rather than just looking right? This post covers what worked on both, with the numbers measured along the way."),

  h1("1. Making pure Python fast enough"),
  p("A Game Boy CPU runs about 1 million instructions a second. A naive Python interpreter loop (fetch, decode through a dictionary, call a handler per opcode) spends most of its time on dispatch and attribute lookups, not on emulation. The cores fight that overhead in a few ways."),
  h2("Generated interpreters"),
  p("The 6502 interpreter is written once as Python source text. At start-up it is compiled with ", code("exec"), ". In the generated function, registers are local variables rather than attributes, so every read is a fast local lookup. Opcode dispatch is a binary ", code("if"), " tree over the opcode bits, and RAM/ROM reads are inlined where the address range is known. What runs is one large function with almost no calls in the hot path."),
  h2("Catch-up timing instead of lock-step"),
  p("A lock-step emulator advances the CPU, PPU and APU one cycle at a time. That is accurate, but in Python it costs three method calls per cycle. RetroPy instead runs the CPU until the next moment something must happen on time: end of a scanline, vblank, a mapper IRQ clock, an APU frame IRQ. Then the video and audio units catch up in one batch. When the CPU touches a PPU or APU register, the units first sync to that exact cycle, so the result matches lock-step where it matters."),
  h2("Caches for the parts that rarely change"),
  p("Background rows are cached by the tuple (tile indices, attributes, fine Y, CHR banks). A static screen redraws almost for free, and only rows whose inputs changed are rebuilt."),
  h2("Results"),
  p("Measured on a laptop with ", code("docs/make_media.py"), " (numbers in ", code("docs/benchmark.json"), "):"),
  table(["Core", "Frames/s drawing", "Frames/s frameskip", "Real time"], [
    ["Game Boy", "~104", "~146", "~170%"],
    ["Game Boy Color", "~97", "~144", "~160%"],
    ["Game Boy Advance (light scene)", "~87", "~90", "~145%"],
    ["NES", "~225", "~262", "~370%"],
    ["NES, synthetic worst case", "~189", "~296", "~315%"],
  ], [3960, 1800, 1800, 1800]),
  gap(),
  p("Heavy GBA scenes, with lots of alpha blending plus Direct Sound, fall to about 50–60% when every frame is drawn. That is where the next trick comes in."),

  h1("2. Smooth even when Python is slow"),
  h2("Pace on the audio clock, not the video clock"),
  p("The emulator runs on a worker thread. Instead of sleeping to hit 60 frames per second, it watches the audio queue. It keeps about 95 ms of sound queued, and when the queue drops below 60 ms, it skips drawing (up to three frames in a row) to catch up. Ears notice a 10 ms gap in sound; eyes rarely notice a dropped frame. So the game and its sound stay at 100% speed, and fewer frames are drawn."),
  h2("Frameskip that doesn't change the game"),
  p("Skipping a frame calls ", code("run_frame(render=False)"), ". It still runs the CPU, timers, sprite-0 hit detection and audio, and leaves out only the pixel writes. A skipped frame is bit-identical in game state to a drawn one, so frameskip can never cause a desync or a missed collision."),
  callout("Lesson:", "In an emulator with sound, audio is the master clock. Tying pacing to the sound card stopped the crackling without making the emulator any faster."),

  h1("3. Save states, rewind and recording"),
  h2("Snapshots that are cheap enough to take every 6 frames"),
  p("Save states are pickles of the whole machine. Large immutable data such as the ROM is passed by reference and never written into the state, and the rest is compressed. Rewind keeps a snapshot every 6 frames for the last 30 seconds. Restoring uses an in-place load: the unpickler fills the existing objects instead of building new ones, so the UI and the audio thread keep their references to the running machine."),
  p("Files from disk go through a restricted unpickler that only accepts the project's own classes, so a downloaded save state cannot run code. A state from a different game is refused with a clear message instead of crashing."),
  h2("Recording that stays in sync"),
  p("Video frames (30 fps) and audio chunks are tagged with game time as they arrive, not wall-clock time. With frameskip, fast-forward or a slow moment, the two streams still line up. When recording stops, a background thread encodes H.264 + AAC MP4 through PyAV, which bundles FFmpeg, so there is nothing extra to install. A rolling 30-second replay buffer makes “clip that” possible after the fact."),

  h1("4. A PlayStation core in Rust, behind the same interface"),
  p("The PS1 runs a 33.8 MHz MIPS CPU, a GPU, a geometry coprocessor (GTE), a video decoder (MDEC) and a 24-voice sound chip. That is far beyond what interpreted Python can emulate in real time, so this core is written in Rust and exposed to Python with PyO3. It implements the same Machine interface as the Python cores (run_frame, press/release, frame_rgb, audio samples), so the app, recording and rewind need no PS1-specific code."),
  bullet(b("The GIL is released for each frame. "), code("run_frame"), " runs inside ", code("py.allow_threads"), ", so the UI thread stays responsive while the core emulates a frame."),
  bullet(b("States are serde snapshots. "), "Every Rust struct derives Serialize/Deserialize and the whole system is written with bincode. The BIOS and the game are kept out of the state, as with the Python cores. A state is about 3.6 MB raw and about 90 KB compressed, and takes about 12 ms to capture, so rewind costs roughly 2 ms per frame on average."),
  bullet(b("Speed: "), "the bundled GPU and MDEC test programs run at 250–330 frames per second, four to five times real time."),
  bullet(b("Built and tested in CI. "), "Every CI run builds the Rust extension on Linux and Windows. A flag makes a missing core a test failure instead of a skip, so a broken build cannot slip through."),

  h1("5. Accuracy by evidence, not by eye"),
  p("An emulator can look right and still be wrong in a hundred places a game will eventually hit. So every PS1 component is checked against output recorded on a real console, using JaCzekanski’s MIT-licensed ps1-tests: TTY logs and frame-buffer dumps captured on hardware."),
  table(["Component", "Hardware reference", "Result"], [
    ["CPU, DMA, GPU registers", "6 TTY logs", "line for line"],
    ["GPU drawing", "4 VRAM dumps", "exact (Gouraud within 1 step)"],
    ["GTE", "1,100 random commands, all 64 registers", "1100 / 1100"],
    ["GTE", "gte/test-all log", "1150 / 1150"],
    ["MDEC", "4-bit and 8-bit VRAM dumps", "exact"],
    ["MDEC colour", "15-bit VRAM dump", "100% within 1 step"],
    ["SPU RAM + DMA timing", "spu/memory-transfer log", "all sub-tests pass"],
  ], [3000, 3800, 2560]),
  gap(),
  h2("The GTE: a fuzz log, plus a negative control"),
  p("The GTE is a fixed-point vector unit with 44-bit accumulators, saturation flags, and a division routine built on an Unsigned Newton-Raphson table. The strongest reference is a fuzz log: 50 random register sets for each of the 22 commands, with all 64 registers recorded before and after on a real console. Our test runs each case through a ", code("gte_exec(regs, op)"), " hook and compares every register. Reaching 1100/1100 meant modelling quirks that look like bugs: the IR3 saturation flag in RTPS, MVMVA’s far-colour bug, and the garbage matrix selected by mx=3."),
  p("We also ran a negative control: with a no-op in place of the GTE, 0 of 1,100 cases match. That proves the harness can fail, and a test that can't fail proves nothing."),
  h2("MDEC: when ±1 is the whole problem"),
  p("Several textbook IDCT and YUV implementations landed within ±1 of the console, but never on it. The fix was to stop guessing. We followed DuckStation's hardware-matched algorithm: coefficients carry 4 extra fraction bits, each IDCT pass rounds with +0x20000 >> 18, and YUV→RGB uses the console's masked integer multiplies. Monochrome output then matched exactly. The last 4-bit mismatch was a rounding choice in the output packing, (v + 8) >> 4, which the hardware dump settled."),
  h2("SPU: timing tests and a second opinion"),
  p("The sound chip test checks data, and it also checks how long a DMA transfer takes: the CPU must be able to run between blocks. The first version copied everything at once and failed. The fix models the channel staying busy for the transfer time and reports SPUSTAT's busy bit while it does. That second part mattered: the SDK's upload routine polls that bit, and without it every sound sample after the first was silently dropped."),
  p("Playback has no hardware recording to compare against. Instead we decode the uploaded sample with a separate, plain Python ADPCM decoder, resample it at the voice's pitch and correlate it with what the SPU produced: 0.9995. A stereo test then checks which side each voice is heard on, and when."),
  callout("Lesson:", "Write down what each test proves and what it doesn't. “Matches a console recording” and “agrees with an independent implementation” are different claims. The README says which one applies to each component."),

  h1("6. Shipping it: CI/CD that installs what it builds"),
  p("Building a package is not the same as shipping a working app. The release pipeline (", code(".github/workflows/release.yml"), ") runs on every tag:"),
  bullet("runs the full test suite, including the Rust core and the hardware test ROMs;"),
  bullet("builds the Flet desktop bundle on Windows and Linux, then fails if the native PS1 extension is missing from it;"),
  bullet("wraps it as a Windows installer (Inno Setup) and a Debian package, plus portable zip and tar.gz;"),
  bullet(b("installs each package on a clean runner, launches the app, requires it to still be running after 20 seconds, then uninstalls it;")),
  bullet("publishes a GitHub Release with SHA256 checksums."),
  p("The install-and-launch step is the one that catches real problems: a missing system library on Linux, a DLL that didn't get bundled, an app that crashes before showing a window. The main branch is protected: changes go through pull requests, and four CI checks must pass."),

  h1("7. What's next"),
  bullet(b("PS1 CD-ROM: "), "cue/bin images and the drive's command set, so disc games can boot (they also need the owner's BIOS)."),
  bullet(b("Memory cards "), "for the PS1, stored per game like the other systems' saves."),
  bullet(b("More hardware references "), "for the parts that still rely on an independent implementation."),
  p("The code, the test ROMs and the CI are all public at ", b("github.com/abhishekbiswas772/RetroPy"), ". Issues and pull requests are welcome."),
];

const doc = new Document({
  creator: "Abhishek Biswas", title: "Emulating five consoles from Python",
  styles: {
    default: { document: { run: { font: FONT, size: 22 } } },
    paragraphStyles: [
      { id: "Title", name: "Title", basedOn: "Normal", run: { font: FONT, size: 48, bold: true, color: "11131A" }, paragraph: { spacing: { before: 0, after: 80 }, keepNext: true } },
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 32, bold: true, color: ACCENT }, paragraph: { spacing: { before: 360, after: 140 }, keepNext: true, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true, run: { font: FONT, size: 25, bold: true, color: "2B2F3A" }, paragraph: { spacing: { before: 220, after: 100 }, keepNext: true, outlineLevel: 1 } },
    ],
  },
  numbering: { config: [{ reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 540, hanging: 280 } } } }] }] },
  sections: [{
    properties: { page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "RetroPy technical blog  ·  ", size: 18, color: "888888" }), new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "888888" })] })] }) },
    children,
  }],
});
Packer.toBuffer(doc).then(buf => { fs.writeFileSync(process.argv[2], buf); console.log("wrote", process.argv[2]); });
