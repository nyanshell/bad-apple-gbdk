# Execution Plan — Bad Apple GB ROM

## Budget reality-check (do this *first*, before coding)

Quick back-of-envelope at the spec'd parameters:

| Asset | Size estimate |
|---|---|
| 128 common tiles (2bpp, 16B each) | 2 KB |
| Per-frame map (20×15 = 300 B × 6572) | ~1.93 MB |
| Per-frame tiles (128 × 16 B avg-worst × 6572) | up to ~13.4 MB |
| Audio (1 sample/byte × 8192 Hz × ~219 s) | ~1.71 MB |

**Per-frame tiles are the gun pointed at the 8 MB cap.** Realistic dedup typically gets per-frame tiles well under 128, but we won't know until we measure. Phase 1 produces measurements before we commit to runtime design — if we're over budget, knobs are: (a) fewer frames (drop to 20 or 24 fps), (b) bigger common-tile pool (256 common with 0 frame-overflow), (c) RLE/delta on the map, (d) 1bpp-on-ROM expanded-on-copy.

## Phase 0 — Project skeleton & toolchain

1. **Submodule GBDK-2020** into `lib/gbdk-2020`, then build it (`make` inside the submodule produces `lib/gbdk-2020/build/gbdk/bin/lcc`).
2. **Drop `reference/gbdk-2020`** (was a snapshot for exploration; the submodule replaces it). Keep `reference/BadBoy/` and `reference/dmg-badapple-av/` — they're documentation.
3. Create dirs: `tools/` (Python encoders), `src/` (handwritten C), `gen/` (encoder output, gitignored), `obj/`, `Makefile`, `.gitignore`.
4. Set up a Python venv with `Pillow` + `numpy` + `tqdm`. Pin in `tools/requirements.txt`.
5. Confirm by building one of GBDK's `examples/` to sanity-check the toolchain.

## Phase 1 — Video encoder (`tools/encode_video.py`)

1. Load each `resources/frames/*.png` in sorted order, threshold to 1bpp at 128.
2. Split into 20×15 grid of 8×8 tiles; canonical bytes = 8-byte 1bpp per tile.
3. Build a global `Counter` over all ~1.97 M tiles → top 128 = common pool (indices `0..127`).
4. Per-frame pass: collect non-common tiles, dedup *within frame*, emit:
   - tile data (frame-specific tiles, 16-byte 2bpp expansion of the 1bpp pattern)
   - 20×15 byte map referencing common (`0..127`) or frame-local (`128..N+127`)
   - Track max frame-specific count across the dataset.
5. **Overflow handling** for any frame that needs >128 unique tiles: pick the 128 most-used tiles in that frame; for each remaining tile, replace the map entry with the closest-Hamming-distance match (prefer common pool first, then chosen 128). Log occurrences — if frequent, raise the common-pool size or drop a frame.
6. Emit:
   - `gen/common_tiles.c` — `#pragma bank 255` + `BANKREF(common_tiles)` + `const uint8_t common_tiles[128*16]`.
   - `gen/frames_NNNN.c` — chunk ~64 frames per file (~100 files total). Each file: `#pragma bank 255`, `BANKREF(frames_NNNN)`, frame tile blobs + maps, plus a within-chunk index.
   - `gen/frame_index.c` — `{bank_ref, offset_in_bank, tile_count}` × 6572, lives in fixed bank.
7. **Round-trip validator**: for a sample of 50 frames, decode from generated arrays, render PNG, diff against source. Bit-identical except for the lossy overflow-tile substitutions.
8. **Print a budget report**: total bytes for tiles, total for maps, overflow-frame count, max tiles/frame.

**Checkpoint** — review the budget report. If totals don't fit in 8 MB minus audio, decide on a knob.

## Phase 2 — Audio encoder (`tools/encode_audio.py`)

1. Read `resources/raw_audio.wav` via `wave` (already 8192 Hz / 8-bit / mono — assert this).
2. Quantize: `q = sample >> 5` (0..7). Store one quantized sample per ROM byte for fast ISR access (no shift/mask in the hot path).
3. Chunk into 16 KB-sized arrays (one per ROM bank), each `gen/audio_NNN.c` with `#pragma bank 255` + `BANKREF(audio_NNN)`.
4. Emit `gen/audio_index.c` — `{bank_ref}` per chunk, plus total sample count.

(If Phase 1 squeezes ROM, fall back to packing 2 samples/byte and add a 1-bit toggle in the ISR — costs ~6 cycles per interrupt.)

## Phase 3 — GBDK runtime (`src/`)

1. **`src/main.c`** — entry: init audio, init video (load common tiles, set LCDC, enable interrupts), then frame loop.
2. **`src/audio.c`** — ISR + setup:
   - `NR52_REG = 0x80; NR51_REG = 0xFF;`
   - Timer config: `TAC = 0x05` (262144 Hz prescaler + enable), `TMA = 256 - 32 = 224` → 8192 Hz fire rate.
   - `add_TIM(audio_isr);`
   - ISR body: save `_current_bank` → `SWITCH_ROM_MBC5(audio_bank)` → load sample byte → write `NR50_REG = (s<<4) | s` (same vol L/R) → restore bank. Keep ISR under ~60 cycles to leave headroom.
3. **`src/video.c`** — render loop:
   - Double-buffer toggle: write into the *inactive* tile bank (e.g., `0x8800` window) and inactive map (`0x9C00`), then flip `LCDC` BG-map-select on VBlank.
   - Pace at 30 FPS: process one frame every 2 VBlanks via VBL counter.
   - VRAM copy: bank-switch to the frame's chunk bank, locate frame ptr from `frame_index`, run an unrolled HBlank-gated copy (BadBoy's 8–12 bytes/HBlank pattern) for tile data, then a similar pass for the 300-byte map.
4. **`src/banks.h`** — extern declarations + `BANKREF_EXTERN()` for the `gen/` symbols, single source of truth for bank addressing.

## Phase 4 — Build (`Makefile`)

- `GBDK ?= lib/gbdk-2020/build/gbdk`, `LCC = $(GBDK)/bin/lcc`
- Targets: `codegen` (runs Python encoders into `gen/`), `rom` (compiles + links), `clean`, `all`.
- Compile: `$(LCC) -Wf--max-allocs-per-node50000 -c -o obj/X.o src/X.c`
- Link: `$(LCC) -autobank -Wl-yt0x1A -Wl-yo512 -Wl-j -Wl-m -o badapple.gb $(OBJS)` — produces a `.map` so we can audit bank distribution.
- `make rom` should be incremental: don't rebuild `gen/` unless `tools/` or `resources/` changed.

## Phase 5 — Validate & tune

1. Run in an emulator (BGB / SameBoy / Emulicious — user picks; I won't assume one is installed). Visual + audio check.
2. Test on a real cartridge if user has one (out of scope unless requested).
3. If audio crackles → ISR too slow; profile and trim.
4. If video glitches → either (a) the per-frame VRAM write doesn't fit in the budget HBlank windows, or (b) the buffer flip isn't atomic w.r.t. PPU scan. Fix by reducing per-frame data or splitting upload across 2 vblanks (see dmg-badapple-av's "8-frame cycle" approach as a reference if needed).

## Defaults for open questions

- Submodule path: `lib/gbdk-2020`
- ROM output name: `badapple.gb`
- Budget overrun policy: **stop and ask** before applying a fallback knob
- Emulator: produce ROM only; user runs manually
