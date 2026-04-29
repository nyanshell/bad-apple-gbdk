#include <gb/gb.h>
#include <gb/hardware.h>
#include <gbdk/platform.h>
#include <string.h>

#include "banks.h"

typedef struct {
    uint16_t frame_count;
    uint16_t bank;
    const uint8_t * data;
    const uint16_t * offsets;
} VideoChunkRam;

#define MAX_VIDEO_CHUNKS 320
static VideoChunkRam video_table[MAX_VIDEO_CHUNKS];
static uint16_t video_chunk_count;
static uint16_t saved_common_tiles_bank;

uint16_t cached_total_frames;

static uint16_t walk_chunk;
static uint16_t walk_first_frame;

/* WRAM staging buffers — frame tiles get expanded from 1bpp (8 B/tile on ROM)
   to 2bpp (16 B/tile in VRAM-format) here, then a single set_bkg_data call
   writes them out. The map is staged so we can switch out of the chunk's
   bank before the long-running set_bkg_tiles upload. */
static uint8_t tile_staging[MAX_FRAME_TILES * TILE_BYTES_2BPP];
static uint8_t map_staging[MAP_BYTES];

static void expand_1bpp_to_2bpp(uint8_t * dst, const uint8_t * src,
                                uint8_t n_tiles) {
    /* For each 1bpp source byte, write it to both planes. Foreground bits
       (1) read as color 3, background bits (0) as color 0 with BGP=0xE4. */
    uint16_t total = (uint16_t)n_tiles * TILE_BYTES_1BPP;
    for (uint16_t i = 0; i < total; i++) {
        uint8_t b = src[i];
        *dst++ = b;
        *dst++ = b;
    }
}

static void load_common_tiles(void) {
    uint16_t prev = current_bank_16;
    SWITCH_BANK_16(saved_common_tiles_bank);
    /* Common tiles are pre-expanded to 2bpp (16 B each) by the encoder.
       set_bkg_data(128, 128, ...) writes 2048 raw bytes to slots 128..255
       (VRAM 0x8800-0x8FFF — the shared region under both LCDC.4 modes). */
    set_bkg_data(128, 128, common_tiles);
    SWITCH_BANK_16(prev);
}

/* Double-buffer state.
 *
 * Tile slots are split: buffer X owns slots 0..63, buffer Y owns 64..127.
 * BG maps are double-buffered too: A at 0x9800, B at 0x9C00.
 * `active_buf` (0 or 1) indicates which pair is currently on screen; the
 * complementary pair is the write target.
 *
 * The active map is selected by LCDC bit 3 (LCDCF_BG9C00). The "inactive"
 * map address is reachable via the WINDOW map register (LCDC bit 6,
 * LCDCF_WIN9C00) — we keep WIN map pointing at the OPPOSITE half of BG
 * map and disable WINON, so set_win_tiles writes raw cells to the
 * inactive map without affecting display.
 *
 * Swap is a single XOR of bits 3 and 6 (mask 0x48), done at VBlank so
 * the change appears atomically between frames. */
static uint8_t active_buf;

#define TILE_BUF_BASE(b)  ((b) ? 64 : 0)
#define BG_MAP_FLIP_MASK  ((uint8_t)(LCDCF_BG9C00 | LCDCF_WIN9C00))

void video_init(void) {
    SWITCH_BANK_16(FRAME_INDEX_BANK);
    video_chunk_count       = num_chunks;
    cached_total_frames     = total_frames;
    saved_common_tiles_bank = common_tiles_bank;
    for (uint16_t i = 0; i < video_chunk_count; i++) {
        video_table[i].frame_count = video_chunks[i].frame_count;
        video_table[i].bank        = video_chunks[i].bank;
        video_table[i].data        = video_chunks[i].data;
        video_table[i].offsets     = video_chunks[i].offsets;
    }
    SWITCH_BANK_16(1);

    walk_chunk = 0;
    walk_first_frame = 0;
    active_buf = 0;

    /* LCD off + mode bits set:
     *  - LCDCF_BG8000  : tile data 0x8000-mode (slots 0..255 direct)
     *  - LCDCF_BG9800  : BG map 0x9800 active (buffer A)
     *  - LCDCF_WIN9C00 : window map at 0x9C00 (buffer B = write target)
     *  - LCDCF_BGON    : BG enabled. Window is intentionally NOT enabled. */
    LCDC_REG = LCDCF_OFF | LCDCF_BG8000 | LCDCF_BG9800 | LCDCF_WIN9C00 | LCDCF_BGON;
    BGP_REG  = 0xE4;

    load_common_tiles();

    /* Fill off-screen rows (18..31) of BOTH BG maps with tile 128 so the
       inactive half doesn't flash boot-ROM data the first time we swap. */
    {
        uint8_t blank[20];
        memset(blank, 128, sizeof(blank));
        for (uint8_t y = 18; y < 32; y++) {
            set_bkg_tiles(0, y, 20, 1, blank);  /* 0x9800 */
            set_win_tiles(0, y, 20, 1, blank);  /* 0x9C00 */
        }
    }

    SCX_REG = 0;
    SCY_REG = 0;
    LCDC_REG |= LCDCF_ON;
}

void render_frame(uint16_t frame_no) {
    while (frame_no >= walk_first_frame + video_table[walk_chunk].frame_count) {
        walk_first_frame += video_table[walk_chunk].frame_count;
        walk_chunk++;
    }
    uint16_t local = frame_no - walk_first_frame;
    uint16_t chunk_bank = video_table[walk_chunk].bank;
    const uint8_t * chunk_data = video_table[walk_chunk].data;
    const uint16_t * chunk_offsets = video_table[walk_chunk].offsets;

    uint16_t prev = current_bank_16;
    SWITCH_BANK_16(chunk_bank);

    uint16_t local_off = chunk_offsets[local];
    const uint8_t * p = chunk_data + local_off;
    uint8_t tile_count = *p++;
    expand_1bpp_to_2bpp(tile_staging, p, tile_count);
    p += (uint16_t)tile_count * TILE_BYTES_1BPP;
    memcpy(map_staging, p, MAP_BYTES);
    SWITCH_BANK_16(prev);

    /* Target the inactive half of the double buffer. */
    uint8_t next_buf  = active_buf ^ 1;
    uint8_t tile_base = TILE_BUF_BASE(next_buf);

    /* When writing to the high tile half, every frame-specific cell
       (value < 64 from the encoder, since MAX_FRAME_TILES = 64) needs +64
       so it points into slots 64..127. Common cells (>= 128) untouched. */
    if (next_buf) {
        for (uint16_t i = 0; i < MAP_BYTES; i++) {
            if (map_staging[i] < 64) map_staging[i] += 64;
        }
    }

    if (tile_count) set_bkg_data(tile_base, tile_count, tile_staging);
    /* set_win_tiles targets the WINDOW map (LCDC bit 6 = WIN9C00). We keep
       it the OPPOSITE of LCDC bit 3 (BG9C00), so this always writes to
       the currently-inactive map. */
    set_win_tiles(0, 0, MAP_W, MAP_H, map_staging);

    /* Atomic swap: flip both bits at VBlank so the next displayed frame
       reads the freshly written map and tile range. wait_vbl_done() is
       cheap (HALT) and ensures we're inside VBlank when we touch LCDC. */
    wait_vbl_done();
    LCDC_REG ^= BG_MAP_FLIP_MASK;
    active_buf = next_buf;
}
