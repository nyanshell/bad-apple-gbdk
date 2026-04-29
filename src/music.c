#include <gb/gb.h>
#include <gb/hardware.h>
#include <gbdk/platform.h>

#include "banks.h"
#include "music_data.h"

/* Chiptune player. Drives Pulse 1 (melody), Pulse 2 (bass), Noise (kick
   drum), and the Wave channel (mid harmony) from a precomputed event
   table at MUSIC_DATA_BANK. music_tick advances one tick (= one VBlank)
   and fires every event whose frame <= cursor. */

/* MBC5 16-bit bank tracker, used by SWITCH_BANK_16 in banks.h. Lives
   here because music.c is the smallest always-linked C file that needs
   bank switching; previously this was in src/audio.c which is gone. */
volatile uint16_t current_bank_16 = 1;

volatile uint16_t music_cur_frame;
volatile uint8_t  music_paused;
static uint16_t cur_event;

void music_init(void) {
    music_cur_frame = 0;
    music_paused    = 0;
    cur_event       = 0;

    NR52_REG = 0x80;        /* APU on. */
    NR50_REG = 0x77;        /* Master volume max on both speakers. */
    NR51_REG = 0xFF;        /* Route every channel to L+R. */

    NR12_REG = 0x00;        /* Pulse 1 silent until first event. */
    NR22_REG = 0x00;        /* Pulse 2 silent until first event. */
    NR42_REG = 0x00;        /* Noise   silent until first event. */

    /* Wave channel: 50% duty square (top half 0xF, bottom half 0x0)
       loaded into Wave RAM, then DAC enabled. NRx2-style envelope
       doesn't exist for Wave; volume is set via NR32 output level. */
    NR30_REG = 0x00;                                     /* Disable to access RAM. */
    for (uint8_t i = 0; i < 16; i++) {
        _AUD3WAVERAM[i] = (i < 8) ? 0xFF : 0x00;        /* 50% duty square. */
    }
    NR30_REG = 0x80;                                     /* DAC on. */
    NR32_REG = 0x20;                                     /* Output 100%. */
}

static inline void trigger_pulse1(uint8_t freq_lo, uint8_t freq_hi) {
    NR12_REG = 0xF0;            /* Vol 15, no envelope. */
    NR11_REG = 0x80;            /* Duty 50%, length 0. */
    NR13_REG = freq_lo;
    NR14_REG = freq_hi;
}

static inline void trigger_pulse2(uint8_t freq_lo, uint8_t freq_hi) {
    NR22_REG = 0xF0;
    NR21_REG = 0x80;
    NR23_REG = freq_lo;
    NR24_REG = freq_hi;
}

static inline void trigger_noise(void) {
    /* Punchy 8-bit kick: low polynomial frequency in 7-bit "periodic"
       width-mode (NR43 bit 3 = 1) — gives the noise channel a tonal,
       tom-tom character rather than the hissy white-noise it produces
       in 15-bit mode. Combined with a fast vol 15 → 0 envelope, each
       hit reads as a punchy thump rather than a sustained rumble.
       NR42=0xF1: vol 15, decrease, pace 1 → ~230 ms.
       NR43=0x6F: shift 6 + 7-bit width + divisor 7 → ~585 Hz, periodic
                  → kick / low tom timbre. */
    NR41_REG = 0x00;
    NR42_REG = 0xF1;            /* Vol 15, decrease, pace 1 (~230 ms). */
    NR43_REG = 0x6F;            /* Low-pitch periodic noise — tom-like. */
    NR44_REG = 0x80;            /* Trigger, length disabled. */
}

static inline void trigger_wave(uint8_t freq_lo, uint8_t freq_hi) {
    NR31_REG = 0x00;            /* Length counter cleared. */
    NR32_REG = 0x20;            /* Output 100%. */
    NR33_REG = freq_lo;
    NR34_REG = freq_hi;         /* trigger + length-disable + freq high. */
}

void music_tick(void) {
    if (music_paused) return;               /* A-button pause halts time. */
    uint16_t f = music_cur_frame++;

    /* Music events live in MUSIC_DATA_BANK. Save/restore the caller's
       bank because the main loop may be in a video chunk bank. */
    uint16_t prev = current_bank_16;
    SWITCH_BANK_16(MUSIC_DATA_BANK);

    while (cur_event < music_event_count && music_events[cur_event].frame <= f) {
        const MusicEvent * e = &music_events[cur_event];
        uint8_t  packed   = e->packed;
        uint8_t  channel  = (packed >> 6) & 0x03;
        uint8_t  env_on   = (packed >> 5) & 0x01;
        uint8_t  freq_hi  = 0x80 | (packed & 0x07);
        uint8_t  freq_lo  = e->freq_lo;

        switch (channel) {
            case 0:  /* Pulse 1 - melody */
                if (env_on) trigger_pulse1(freq_lo, freq_hi);
                else        NR12_REG = 0x00;
                break;
            case 1:  /* Pulse 2 - bass */
                if (env_on) trigger_pulse2(freq_lo, freq_hi);
                else        NR22_REG = 0x00;
                break;
            case 2:  /* Noise - drums (only ON events emitted) */
                if (env_on) trigger_noise();
                break;
            case 3:  /* Wave - mid harmony */
                if (env_on) trigger_wave(freq_lo, freq_hi);
                else        NR32_REG = 0x00;        /* Output 0% = silence. */
                break;
        }
        cur_event++;
    }

    SWITCH_BANK_16(prev);
}
