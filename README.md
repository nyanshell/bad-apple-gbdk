# Bad Apple!! Game Boy ROM

A Game Boy (DMG / GBC compatible) ROM that plays the Bad Apple!! music
video at 160×144 with synchronized 4-channel chiptune audio. Built on
GBDK-2020, packed into a 8 MB MBC5 cartridge image.

The renderer uses tile + BG-map double-buffering (slots 0..63 / 64..127,
maps 0x9800 / 0x9C00, atomic swap via `LCDC ^= 0x48` at VBlank) so the
silhouette plays without tearing on real hardware. The chiptune driver
maps four selected MIDI tracks onto the GB's pulse / wave / noise
channels.

## Layout

```
resources/
  badapple.webm                          # source video (480x360 30 fps)
  frames/                                # 6572 PNG frames, 160x120 grayscale
  Alstroemeria Records - Bad Apple.mid   # source MIDI for the chiptune
tools/
  encode_video.py                        # frames → tile-deduped C tables
  encode_music.py                        # MIDI    → chiptune event table
src/                                     # GBDK-2020 C source
gen/                                     # auto-generated C tables
```

## Controls

- **A** — pause / resume playback (useful for screenshots)

## Build

Requirements:

- Python 3 with `mido`, `numpy`, `Pillow`, `tqdm`
  (a `.venv/` with `tools/requirements.txt` installed works)
- GBDK-2020 (vendored as a submodule under `lib/gbdk-2020`)

The first time, fetch + build GBDK and install Python deps:

```sh
git submodule update --init --recursive
python3 -m venv .venv
.venv/bin/pip install -r tools/requirements.txt
make -C lib/gbdk-2020 PORTS="sm83 z80" PLATFORMS=gb -j8
```

After that, generate the data tables and compile the ROM:

```sh
make -j8
```

`make` (the default `all` target) runs `tools/encode_video.py` (frames
→ 4-bit tiles, tile dedup, 1 chunk per ROM bank) and
`tools/encode_music.py` (MIDI → chiptune events), then builds
`badapple.gb`.

`make rom` is the link-only sub-target — use it when you know the
generated tables are already current and you just want to re-link.
`make all` (or just `make`) is the full pipeline.

To re-run just the encoders without rebuilding the ROM:

```sh
.venv/bin/python tools/encode_video.py
.venv/bin/python tools/encode_music.py
```

To clean:

```sh
make clean        # remove object files + ROM
make distclean    # also remove generated C tables in gen/
```

## Run

Any DMG / GBC emulator (e.g. SameBoy) or a flashable MBC5 8 MB
cartridge:

```sh
sameboy badapple.gb
```
