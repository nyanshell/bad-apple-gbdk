"""
Phase 1 — Video tile dedup encoder.

Reads grayscale PNG frames from resources/frames, thresholds to 1bpp,
splits into 8x8 tiles, picks the 128 most frequent tiles globally as the
"common pool" (VRAM tile indices 128..255), and per-frame collects up to
128 frame-specific tiles (VRAM tile indices 0..127). Frames that need
more than 128 unique non-common tiles fall back to nearest-Hamming-match
substitution.

Each frame is serialized as: [tile_count: u8][tiles: tile_count*8 bytes
in 1bpp form][map: 300 bytes]. Frames are packed into ROM-bank-sized
chunks (one .c file per chunk, with #pragma bank 255 and BANKREF).
A chunk index (chunks[]) lets the runtime locate any frame.

Outputs C files under gen/. Also prints a budget report so we can
decide whether to ship as-is or apply a knob.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
FRAMES_DIR = ROOT / "resources" / "frames"
OUT_DIR = ROOT / "gen"

TILE_W = TILE_H = 8
GRID_W, GRID_H = 20, 18            # 20x18 tiles per frame -> 160x144 px (full GB viewport)
FRAME_W = GRID_W * TILE_W
FRAME_H = GRID_H * TILE_H
TILES_PER_FRAME = GRID_W * GRID_H  # 360

COMMON_COUNT = 128
MAX_FRAME_TILES = 64               # Each tile-buffer half holds 64 slots (slots
                                   # 0..63 vs 64..127). Common pool fills
                                   # 128..255. Runtime alternates buffers per
                                   # frame so writes never touch the active half.
THRESHOLD = 128                    # 8-bit gray cutoff for foreground/background

# We reserve 384 bytes of headroom in each 16KB ROM bank for safety.
BANK_SIZE = 16 * 1024
CHUNK_BUDGET = BANK_SIZE - 384

# Bank allocation (matches encode_audio.py — both encoders must agree). The
# linker's autobank tool is capped at MBC5 bank 255; we assign banks manually
# so we can use the full 8 MB MBC5 range up to 511.
BANK_COMMON_TILES = 2
BANK_FRAME_INDEX  = 3
BANK_AUDIO_INDEX  = 4
BANK_VIDEO_FIRST  = 5    # video chunks fill upward from here
# encode_audio.py reads max video chunk count via gen/.video_chunk_count and
# starts audio chunks immediately after.

POW2 = (1 << np.arange(7, -1, -1)).astype(np.uint8)


def load_frame_tiles(path: Path) -> np.ndarray:
    """Load a frame and return a (GRID_H, GRID_W, 8) uint8 array of tile rows
    (1bpp).

    Pixel >= THRESHOLD becomes background (0); below becomes foreground (1).
    Each tile is 8 bytes; bit i of byte j is the foreground bit of pixel
    (j, 7 - i) within the tile.

    Frames on disk are 160x120 (4:3). The GB viewport is 160x144 (~10:9).
    To fill the screen without stretching the silhouette we scale the
    frame aspect-preserving so the height fits FRAME_H, then centre-crop
    horizontally to FRAME_W. Bad Apple's silhouette is always near the
    horizontal centre so the cropped strips are background.
    """
    im = Image.open(path).convert("L")
    src_w, src_h = im.size
    if (src_w, src_h) != (FRAME_W, FRAME_H):
        # Scale so height == FRAME_H, preserving aspect.
        scaled_w = (src_w * FRAME_H + src_h // 2) // src_h
        im = im.resize((scaled_w, FRAME_H), resample=Image.LANCZOS)
        if scaled_w > FRAME_W:
            left = (scaled_w - FRAME_W) // 2
            im = im.crop((left, 0, left + FRAME_W, FRAME_H))
        elif scaled_w < FRAME_W:
            # Pillarbox: pad with black on both sides (rare, source is 4:3).
            pad = Image.new("L", (FRAME_W, FRAME_H), 0)
            pad.paste(im, ((FRAME_W - scaled_w) // 2, 0))
            im = pad
    img = np.array(im, dtype=np.uint8)
    fg = (img < THRESHOLD).astype(np.uint8)  # 1 = "ink" / black pixel
    tiles = fg.reshape(GRID_H, TILE_H, GRID_W, TILE_W).transpose(0, 2, 1, 3)
    return np.einsum("ijkl,l->ijk", tiles, POW2, dtype=np.uint8)


def hamming(a: bytes, b: bytes) -> int:
    return bin(int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).count("1")


def encode_all(frames_dir: Path, out_dir: Path, limit: int | None = None) -> None:
    frame_paths = sorted(frames_dir.glob("*.png"))
    if limit:
        frame_paths = frame_paths[:limit]
    if not frame_paths:
        sys.exit(f"no PNG frames found in {frames_dir}")

    # Pass 1: hash every tile & build global frequency table.
    counter: Counter[bytes] = Counter()
    all_keys: list[list[bytes]] = []
    for fp in tqdm(frame_paths, desc="pass1 (hash)"):
        tb = load_frame_tiles(fp)
        keys = [bytes(tb[y, x]) for y in range(GRID_H) for x in range(GRID_W)]
        counter.update(keys)
        all_keys.append(keys)

    common_list = [k for k, _ in counter.most_common(COMMON_COUNT)]
    common_to_idx = {k: 128 + i for i, k in enumerate(common_list)}

    # Pass 2: per-frame classify, dedup, build map + tile blob.
    chunks: list[list[bytes]] = []
    cur: list[bytes] = []
    cur_size = 0
    overflow_frames = 0
    max_frame_tiles = 0
    total_tile_bytes = 0
    total_map_bytes = 0

    for keys in tqdm(all_keys, desc="pass2 (encode)"):
        local = Counter(k for k in keys if k not in common_to_idx)

        if len(local) > MAX_FRAME_TILES:
            overflow_frames += 1
            chosen = [k for k, _ in local.most_common(MAX_FRAME_TILES)]
            chosen_set = set(chosen)
            # Build candidate pool for substitution: common + chosen (both reachable).
            pool = common_list + chosen
            sub: dict[bytes, bytes] = {}
            for k in local:
                if k not in chosen_set:
                    sub[k] = min(pool, key=lambda c, k=k: hamming(k, c))
            fs_to_idx = {k: i for i, k in enumerate(chosen)}
        else:
            sub = {}
            fs_to_idx = {k: i for i, k in enumerate(local)}

        max_frame_tiles = max(max_frame_tiles, len(fs_to_idx))

        # Build the 20x15 map.
        map_bytes = bytearray(TILES_PER_FRAME)
        for i, k in enumerate(keys):
            if k in common_to_idx:
                map_bytes[i] = common_to_idx[k]
            elif k in sub:
                s = sub[k]
                map_bytes[i] = common_to_idx[s] if s in common_to_idx else fs_to_idx[s]
            else:
                map_bytes[i] = fs_to_idx[k]

        # Pack frame-specific tile bytes in 1bpp form (8 B/tile).
        # Runtime expands to 2bpp into a WRAM staging buffer before VRAM upload.
        tile_blob = bytearray()
        for k in sorted(fs_to_idx, key=fs_to_idx.get):
            tile_blob.extend(k)

        total_tile_bytes += len(tile_blob)
        total_map_bytes += len(map_bytes)

        frame_blob = bytes([len(fs_to_idx)]) + bytes(tile_blob) + bytes(map_bytes)
        if cur_size + len(frame_blob) > CHUNK_BUDGET and cur:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(frame_blob)
        cur_size += len(frame_blob)
    if cur:
        chunks.append(cur)

    # Validator: re-render a few frames from the encoded blobs and compare to
    # the original PNGs. For non-overflow frames the result must be identical
    # (modulo our threshold). For overflow frames there will be substitutions.
    validate_sample(frame_paths, all_keys, common_list, chunks)

    # Emit C files.
    out_dir.mkdir(parents=True, exist_ok=True)
    # Only purge files we own. Audio encoder writes its own files in the same
    # directory, and we don't want to delete those when re-running just video.
    for child in out_dir.glob("frames_chunk_*.c"):
        child.unlink()
    for child in (out_dir / "common_tiles.c", out_dir / "frame_index.c",
                  out_dir / "video_data.h"):
        if child.exists():
            child.unlink()
    write_common_tiles(out_dir, common_list)
    chunk_meta: list[tuple[str, int, list[int], int]] = []
    for ci, chunk in enumerate(chunks):
        name = f"frames_chunk_{ci:03d}"
        bank = BANK_VIDEO_FIRST + ci
        offsets = write_chunk(out_dir, name, chunk, bank)
        chunk_meta.append((name, len(chunk), offsets, bank))
    write_frame_index(out_dir, chunk_meta)
    # Tell encode_audio.py where to start its bank assignments.
    (out_dir / ".video_chunk_count").write_text(str(len(chunks)) + "\n")

    # Budget report.
    print()
    print("=== budget report ===")
    print(f"frames               : {len(frame_paths)}")
    print(f"common pool          : {COMMON_COUNT} tiles ({COMMON_COUNT*8} B, 1bpp)")
    print(f"frame-specific max   : {max_frame_tiles}/frame")
    print(f"overflow frames      : {overflow_frames}/{len(frame_paths)}"
          f" ({100*overflow_frames/len(frame_paths):.2f}%)")
    print(f"tile data bytes      : {total_tile_bytes:,}")
    print(f"map bytes            : {total_map_bytes:,}")
    print(f"chunks               : {len(chunks)}  (~{BANK_SIZE//1024} KB each)")
    video_total = COMMON_COUNT * 8 + total_tile_bytes + total_map_bytes \
                  + sum(len(c) for c in chunks) * 0  # framing already counted
    overhead = sum(2 * len(c) for c in chunks)  # offset tables (uint16 each)
    print(f"chunk overhead       : {overhead:,} B (offset tables)")
    print(f"video ROM total      : ~{(video_total + overhead) / 1024:.1f} KB"
          f" ({(video_total + overhead) / (1024*1024):.2f} MB)")


def validate_sample(frame_paths, all_keys, common_list, chunks, n: int = 10):
    """Decode a sample of encoded frames and verify they round-trip."""
    common_set = set(common_list)
    # Sample every Nth frame.
    step = max(1, len(frame_paths) // n)
    sample_ids = list(range(0, len(frame_paths), step))[:n]
    fails = 0
    for fid in sample_ids:
        original = all_keys[fid]
        original_specific = [k for k in original if k not in common_set]
        # Find which chunk this frame ended up in.
        running = 0
        chunk_id = None
        local_id = None
        for ci, c in enumerate(chunks):
            if running + len(c) > fid:
                chunk_id = ci
                local_id = fid - running
                break
            running += len(c)
        blob = chunks[chunk_id][local_id]
        tile_count = blob[0]
        # Walk the map and check that every cell either references a common
        # tile we know about or a frame-specific tile that exists in the blob.
        tiles = blob[1:1 + tile_count * 8]
        m = blob[1 + tile_count * 8:]
        if len(m) != TILES_PER_FRAME:
            print(f"frame {fid}: bad map length {len(m)}")
            fails += 1
            continue
        # Sanity: max map index < 128 + len(common_list); fs indices < tile_count.
        for v in m:
            if v >= 128:
                if v - 128 >= len(common_list):
                    print(f"frame {fid}: bad common idx {v}")
                    fails += 1
                    break
            else:
                if v >= tile_count:
                    print(f"frame {fid}: bad frame-specific idx {v} >= {tile_count}")
                    fails += 1
                    break
        # Compare unique frame-specific tile bytes to the originals (when
        # there was no overflow / substitution).
        if len(set(original_specific)) <= MAX_FRAME_TILES:
            decoded_tiles = {bytes(tiles[i*8:(i+1)*8]) for i in range(tile_count)}
            orig_unique = set(original_specific)
            if decoded_tiles != orig_unique:
                missing = orig_unique - decoded_tiles
                extra = decoded_tiles - orig_unique
                print(f"frame {fid}: tile mismatch missing={len(missing)}"
                      f" extra={len(extra)}")
                fails += 1
    if fails:
        print(f"validator: {fails}/{len(sample_ids)} sample frames failed")
    else:
        print(f"validator: {len(sample_ids)}/{len(sample_ids)} sample frames OK")


def write_common_tiles(out_dir: Path, common_list: list[bytes]) -> None:
    """Emit common tiles in 2bpp form (16 bytes/tile). Both planes are set
    to the 1bpp pattern, so foreground bits read as color 3 and background
    bits as color 0 with a standard BGP. This avoids set_bkg_1bpp_data and
    its on-the-fly expansion entirely — set_bkg_data writes the raw 2bpp
    bytes directly. Costs an extra 1024 bytes of ROM (negligible)."""
    path = out_dir / "common_tiles.c"
    with path.open("w") as f:
        f.write('#include <gbdk/platform.h>\n#include <stdint.h>\n\n')
        f.write(f'#pragma bank {BANK_COMMON_TILES}\n\n')
        f.write(f'const uint8_t common_tiles[{COMMON_COUNT * 16}] = {{\n')
        for k in common_list:
            row = []
            for b in k:
                row.append(f'0x{b:02X}')
                row.append(f'0x{b:02X}')
            f.write('    ' + ', '.join(row) + ',\n')
        f.write('};\n')


def write_chunk(
    out_dir: Path, name: str, frames: list[bytes], bank: int
) -> list[int]:
    data = bytearray()
    offsets: list[int] = []
    for blob in frames:
        offsets.append(len(data))
        data.extend(blob)
    path = out_dir / f"{name}.c"
    with path.open("w") as f:
        f.write('#include <gbdk/platform.h>\n#include <stdint.h>\n\n')
        f.write(f'#pragma bank {bank}\n\n')
        f.write(f'const uint8_t {name}_data[{len(data)}] = {{\n')
        for i in range(0, len(data), 16):
            row = data[i:i + 16]
            f.write('    ' + ', '.join(f'0x{b:02X}' for b in row) + ',\n')
        f.write('};\n\n')
        f.write(f'const uint16_t {name}_offsets[{len(offsets)}] = {{\n')
        # 8 offsets per line for readability.
        for i in range(0, len(offsets), 8):
            f.write('    ' + ', '.join(str(o) for o in offsets[i:i + 8]) + ',\n')
        f.write('};\n')
    return offsets


def write_frame_index(
    out_dir: Path, chunk_meta: list[tuple[str, int, list[int], int]]
) -> None:
    path = out_dir / "frame_index.c"
    with path.open("w") as f:
        f.write('#include <gbdk/platform.h>\n#include <stdint.h>\n')
        f.write('#include "video_data.h"\n\n')
        f.write(f'#pragma bank {BANK_FRAME_INDEX}\n\n')
        f.write(f'const uint16_t common_tiles_bank = {BANK_COMMON_TILES};\n')
        f.write(f'const uint16_t total_frames = {sum(c[1] for c in chunk_meta)};\n')
        f.write(f'const uint16_t num_chunks = {len(chunk_meta)};\n\n')
        f.write(f'const VideoChunk video_chunks[{len(chunk_meta)}] = {{\n')
        for name, count, _, bank in chunk_meta:
            f.write(f'    {{ {count}, {bank}, {name}_data, {name}_offsets }},\n')
        f.write('};\n')

    # Also emit the header it includes.
    hdr = out_dir / "video_data.h"
    with hdr.open("w") as f:
        f.write('#ifndef VIDEO_DATA_H\n#define VIDEO_DATA_H\n\n')
        f.write('#include <gbdk/platform.h>\n#include <stdint.h>\n\n')
        f.write('typedef struct VideoChunk {\n')
        f.write('    uint16_t frame_count;\n')
        f.write('    uint16_t bank;\n')
        f.write('    const uint8_t * data;\n')
        f.write('    const uint16_t * offsets;\n')
        f.write('} VideoChunk;\n\n')
        f.write('extern const uint16_t common_tiles_bank;\n')
        f.write(f'#define FRAME_INDEX_BANK {BANK_FRAME_INDEX}\n')
        f.write('extern const uint16_t total_frames;\n')
        f.write('extern const uint16_t num_chunks;\n')
        f.write('extern const VideoChunk video_chunks[];\n\n')
        f.write('extern const uint8_t common_tiles[];\n\n')
        for name, _, _, _ in chunk_meta:
            f.write(f'extern const uint8_t {name}_data[];\n')
            f.write(f'extern const uint16_t {name}_offsets[];\n')
        f.write('\n#endif\n')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames-dir", type=Path, default=FRAMES_DIR)
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--limit", type=int, default=None,
                    help="encode only the first N frames (for fast iteration)")
    args = ap.parse_args()
    encode_all(args.frames_dir, args.out_dir, args.limit)


if __name__ == "__main__":
    main()
