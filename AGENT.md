# Bad Apple Game Boy ROM Implementation Guide

Reference projects & libraries is under ./reference

frames: resources/frames
audio file: resources/raw_audio.wav


## Objective
Develop a Game Boy (GB/GBC) ROM that plays "Bad Apple!!" at 30 FPS with synchronized PCM audio using GBDK-2020. Frame extraction to ./resources is complete.

Task 1: Video Tile Deduplication (Python)
Read the 160x120 grayscale frames from ./resources.

Convert pixels to 1bpp (black and white).

Split frames into 8x8 pixel tiles.

Perform global tile deduplication: Identify the 128 most frequently used tiles across all frames to serve as the "common tiles" stored permanently in VRAM.

For each frame, extract the remaining "frame-specific tiles" and generate a 20x15 background map array containing tile indices.

Export the data as C source files. Include #pragma bank 255 and BANKREF() declarations in each file to enable GBDK autobanking.

Task 2: Audio Processing (Python)
Extract audio from the source video and downsample it to 8192Hz, mono, 8-bit WAV.

Quantize the 8-bit audio samples into 3-bit values (range 0-7).

Export the 3-bit audio stream as C arrays, chunked into C files with #pragma bank 255 for autobanking.

Task 3: ROM Application Logic (C / GBDK-2020)
Audio ISR Setup:

Initialize the audio master control registers: NR52_REG = 0x80; NR51_REG = 0xFF;.

Use add_TIM() to attach a timer interrupt service routine (ISR) executing at 8192Hz.

Inside the ISR, read the current 3-bit audio sample and write it to NR50_REG to modulate the master volume.

Ensure you save the current ROM bank using _current_bank, switch to the audio data bank using SWITCH_ROM_MBC5(), and restore the previous bank before the ISR exits.

Video Rendering Loop:

Implement double buffering by alternating the LCDC background map between 0x9800 and 0x9C00 every frame.

Copy frame-specific tiles and the background map to VRAM using loop unrolling.

Restrict VRAM writes to the H-Blank period by polling the STATF_B_BUSY bit of STAT_REG to prevent graphical corruption.

Task 4: Compilation and Linking
Compile the C code using the GBDK-2020 lcc compiler.

Apply the -autobank flag to instruct the linker to automatically distribute the frame and audio data across available ROM banks.

Link with MBC5 support by passing -Wl-yt0x1A.

Enable an 8MB ROM size by passing -Wl-yo512.

Optimize the SDCC compilation by including -Wf--max-allocs-per-node50000.
