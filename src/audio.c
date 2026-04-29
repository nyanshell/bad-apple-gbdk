#include <gb/gb.h>
#include <gb/hardware.h>
#include <gbdk/platform.h>

#include "banks.h"

volatile uint16_t current_bank_16 = 1;
uint16_t cached_num_audio_chunks;

typedef struct {
    uint16_t length;
    uint16_t bank;
    const uint8_t * data;
} AudioChunkRam;

#define MAX_AUDIO_CHUNKS 160
static AudioChunkRam audio_table[MAX_AUDIO_CHUNKS];
static uint16_t audio_chunk_count;

static volatile uint16_t cur_chunk;
static volatile uint16_t cur_offset;
static volatile uint16_t cur_length;
static volatile uint16_t cur_bank;
static const volatile uint8_t * volatile cur_data;

void audio_init(void) {
    /* Snapshot the audio chunk metadata into WRAM so the ISR doesn't need to
       bank-switch into the audio_index bank just to read the length/data of
       the current chunk. Capture the count under that bank too — we cannot
       read num_audio_chunks once we've switched away. */
    SWITCH_BANK_16(AUDIO_INDEX_BANK);
    audio_chunk_count = num_audio_chunks;
    cached_num_audio_chunks = audio_chunk_count;
    for (uint16_t i = 0; i < audio_chunk_count; i++) {
        audio_table[i].length = audio_chunks[i].length;
        audio_table[i].bank   = audio_chunks[i].bank;
        audio_table[i].data   = audio_chunks[i].data;
    }
    SWITCH_BANK_16(1);

    cur_chunk  = 0;
    cur_offset = 0;
    cur_length = audio_table[0].length;
    cur_bank   = audio_table[0].bank;
    cur_data   = audio_table[0].data;

    /* APU reset + DAC enable, per dmg-badapple-av. NR50 alone is silent
       without an active DAC: NR50 is a *gain*, so it needs something to
       scale. Writing 0xFF to each channel's envelope register (NR12 /
       NR22 / NR42) and to NR30 (wave channel control) turns on each
       channel's DAC at a non-zero quiescent level — without triggering
       the channel itself. With the DACs on, modulating NR50 at audio
       rate produces audible PCM. */
    NR52_REG = 0x00;          /* APU off — clears all channels. */
    NR52_REG = 0xFF;          /* APU on. */
    NR12_REG = 0xFF;          /* Pulse 1 DAC on. */
    NR22_REG = 0xFF;          /* Pulse 2 DAC on. */
    NR30_REG = 0xFF;          /* Wave channel DAC on (bit 7). */
    NR42_REG = 0xFF;          /* Noise DAC on. */
    NR51_REG = 0xFF;          /* Route all channels to L+R. */
    NR50_REG = 0x00;          /* ISR will modulate this for PCM. */

    /* 4194304 / 32 ticks at 262144 Hz prescaler = 8192 Hz fire rate. */
    TMA_REG = 224;
    TAC_REG = 0x05;

    add_TIM(audio_isr);
}

void audio_isr(void) {
    /* Per spec: save current bank, switch to audio bank, read sample, write
       NR50, restore bank. We track 16-bit bank ourselves so this works for
       banks > 255 (MBC5 8 MB range). */
    uint16_t prev = current_bank_16;
    SWITCH_BANK_16(cur_bank);
    uint8_t s = cur_data[cur_offset];
    NR50_REG = (uint8_t)((s << 4) | s);

    if (++cur_offset >= cur_length) {
        cur_offset = 0;
        if (++cur_chunk >= audio_chunk_count) {
            cur_chunk = audio_chunk_count - 1;
            cur_offset = cur_length - 1;
            NR50_REG = 0x00;
        } else {
            cur_bank   = audio_table[cur_chunk].bank;
            cur_data   = audio_table[cur_chunk].data;
            cur_length = audio_table[cur_chunk].length;
        }
    }

    SWITCH_BANK_16(prev);
}
