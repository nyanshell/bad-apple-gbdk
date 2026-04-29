"""
Phase 2 — Audio 3-bit quantizer.

Reads resources/raw_audio.wav (asserted 8192 Hz / 8-bit / mono PCM),
quantizes each sample from 0..255 to 0..7 by a right-shift of 5, and
emits one .c file per ROM bank chunk plus an audio_index.c with the
chunk table. One sample per ROM byte (no packing) keeps the audio ISR
fast.
"""
from __future__ import annotations

import argparse
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAV_PATH = ROOT / "resources" / "raw_audio.wav"
OUT_DIR = ROOT / "gen"

EXPECTED_RATE = 8192
EXPECTED_CHANNELS = 1
EXPECTED_SAMPWIDTH = 1  # bytes -> 8-bit unsigned

BANK_SIZE = 16 * 1024
CHUNK_BUDGET = BANK_SIZE - 256  # leave headroom for any per-bank overhead

# Bank allocation matches encode_video.py.
BANK_AUDIO_INDEX = 4
BANK_VIDEO_FIRST = 5  # video chunks start here
BANK_AUDIO_FIRST_DEFAULT = 5  # overridden by .video_chunk_count if present


def load_quantized(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != EXPECTED_RATE:
            raise ValueError(f"expected {EXPECTED_RATE} Hz, got {w.getframerate()}")
        if w.getnchannels() != EXPECTED_CHANNELS:
            raise ValueError(f"expected mono, got {w.getnchannels()} channels")
        if w.getsampwidth() != EXPECTED_SAMPWIDTH:
            raise ValueError(f"expected 8-bit, got sampwidth {w.getsampwidth()}")
        raw = w.readframes(w.getnframes())
    # 8-bit WAV is unsigned (0..255). Quantize to 0..7.
    return bytes(b >> 5 for b in raw)


def chunkify(data: bytes, budget: int) -> list[bytes]:
    return [data[i:i + budget] for i in range(0, len(data), budget)]


def write_audio_chunk(out_dir: Path, name: str, data: bytes, bank: int) -> None:
    path = out_dir / f"{name}.c"
    with path.open("w") as f:
        f.write('#include <gbdk/platform.h>\n#include <stdint.h>\n\n')
        f.write(f'#pragma bank {bank}\n\n')
        f.write(f'const uint8_t {name}_data[{len(data)}] = {{\n')
        for i in range(0, len(data), 24):
            row = data[i:i + 24]
            f.write('    ' + ', '.join(str(b) for b in row) + ',\n')
        f.write('};\n')


def write_audio_index(
    out_dir: Path, chunk_names: list[str], chunk_sizes: list[int],
    chunk_banks: list[int], total_samples: int
) -> None:
    path = out_dir / "audio_index.c"
    with path.open("w") as f:
        f.write('#include <gbdk/platform.h>\n#include <stdint.h>\n')
        f.write('#include "audio_data.h"\n\n')
        f.write(f'#pragma bank {BANK_AUDIO_INDEX}\n\n')
        f.write(f'const uint32_t total_audio_samples = {total_samples};\n')
        f.write(f'const uint16_t num_audio_chunks = {len(chunk_names)};\n\n')
        f.write(f'const AudioChunk audio_chunks[{len(chunk_names)}] = {{\n')
        for name, size, bank in zip(chunk_names, chunk_sizes, chunk_banks):
            f.write(f'    {{ {size}, {bank}, {name}_data }},\n')
        f.write('};\n')

    hdr = out_dir / "audio_data.h"
    with hdr.open("w") as f:
        f.write('#ifndef AUDIO_DATA_H\n#define AUDIO_DATA_H\n\n')
        f.write('#include <gbdk/platform.h>\n#include <stdint.h>\n\n')
        f.write('typedef struct AudioChunk {\n')
        f.write('    uint16_t length;\n')
        f.write('    uint16_t bank;\n')
        f.write('    const uint8_t * data;\n')
        f.write('} AudioChunk;\n\n')
        f.write(f'#define AUDIO_INDEX_BANK {BANK_AUDIO_INDEX}\n')
        f.write('extern const uint32_t total_audio_samples;\n')
        f.write('extern const uint16_t num_audio_chunks;\n')
        f.write('extern const AudioChunk audio_chunks[];\n\n')
        for name in chunk_names:
            f.write(f'extern const uint8_t {name}_data[];\n')
        f.write('\n#endif\n')


def encode(wav_path: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    quantized = load_quantized(wav_path)
    chunks = chunkify(quantized, CHUNK_BUDGET)

    # Clear stale audio chunks from prior runs.
    for child in out_dir.glob("audio_chunk_*.c"):
        child.unlink()
    for stale in (out_dir / "audio_index.c", out_dir / "audio_data.h"):
        if stale.exists():
            stale.unlink()

    # Audio banks start right after the last video chunk. encode_video.py
    # writes its chunk count to .video_chunk_count.
    count_file = out_dir / ".video_chunk_count"
    audio_first_bank = BANK_AUDIO_FIRST_DEFAULT
    if count_file.exists():
        audio_first_bank = BANK_VIDEO_FIRST + int(count_file.read_text().strip())

    chunk_names: list[str] = []
    chunk_sizes: list[int] = []
    chunk_banks: list[int] = []
    for ci, ch in enumerate(chunks):
        name = f"audio_chunk_{ci:03d}"
        bank = audio_first_bank + ci
        write_audio_chunk(out_dir, name, ch, bank)
        chunk_names.append(name)
        chunk_sizes.append(len(ch))
        chunk_banks.append(bank)
    write_audio_index(
        out_dir, chunk_names, chunk_sizes, chunk_banks, len(quantized)
    )
    print(f"audio first bank: {audio_first_bank}, last: {audio_first_bank + len(chunks) - 1}")

    print("=== audio budget ===")
    print(f"samples         : {len(quantized):,}")
    print(f"duration        : {len(quantized)/EXPECTED_RATE:.2f} s")
    print(f"chunks          : {len(chunks)}")
    print(f"audio ROM total : ~{len(quantized) / 1024:.1f} KB"
          f" ({len(quantized) / (1024*1024):.2f} MB)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", type=Path, default=WAV_PATH)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    encode(args.wav, args.out_dir)


if __name__ == "__main__":
    main()
