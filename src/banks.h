#ifndef BANKS_H
#define BANKS_H

#include <gbdk/platform.h>
#include <stdint.h>

#include "video_data.h"

#define MAP_W 20
#define MAP_H 18
#define MAP_BYTES (MAP_W * MAP_H)
#define TILE_BYTES_1BPP 8
#define TILE_BYTES_2BPP 16
#define MAX_FRAME_TILES 64

#define VRAM_TILES_BUF_A ((uint8_t *)0x8000)
#define VRAM_TILES_BUF_B ((uint8_t *)0x9000)
#define VRAM_MAP_A       ((uint8_t *)0x9800)
#define VRAM_MAP_B       ((uint8_t *)0x9C00)

/* MBC5 supports 9-bit bank numbers (0..511 = 8 MB). GBDK's _current_bank only
   tracks 8 bits, so we maintain our own tracker that the audio ISR uses for
   save/restore around its bank switch. SWITCH_BANK_16 below writes both ROMB0
   and ROMB1 explicitly. */
extern volatile uint16_t current_bank_16;

#define SWITCH_BANK_16(b) do { \
    uint16_t _bk = (uint16_t)(b); \
    current_bank_16 = _bk; \
    rROMB1 = (uint8_t)(_bk >> 8); \
    rROMB0 = (uint8_t)(_bk); \
} while (0)

void video_init(void);
void render_frame(uint16_t frame_no);
void music_init(void);
void music_tick(void);
extern volatile uint16_t music_cur_frame;
extern volatile uint8_t  music_paused;

/* Counts cached into RAM at init time so the main loop doesn't have to
   bank-switch to read them. cached_total_frames lives in WRAM, the
   original total_frames in frame_index.c (bank 3) is only addressable
   when bank 3 is currently mapped. */
extern uint16_t cached_total_frames;

#endif
