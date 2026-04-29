#include <gb/gb.h>
#include <gbdk/platform.h>

#include "banks.h"

extern volatile uint8_t music_paused;

void main(void) {
    /* Music + video. music_tick auto-runs in the VBL handler at 60 Hz
       (correct tempo), the main loop renders the video frame matching
       the current music tick. A button pauses both for screenshots /
       debug. */
    disable_interrupts();
    music_init();
    video_init();
    add_VBL(music_tick);
    set_interrupts(VBL_IFLAG);
    enable_interrupts();

    uint16_t last_rendered = 0xFFFF;
    uint8_t  prev_keys     = joypad();

    while (1) {
        uint8_t keys = joypad();
        if ((keys & J_A) && !(prev_keys & J_A)) music_paused ^= 1;
        prev_keys = keys;

        if (music_paused) {
            wait_vbl_done();
            continue;
        }

        uint16_t target = music_cur_frame >> 1;
        if (target >= cached_total_frames) target = cached_total_frames - 1;
        if (target != last_rendered) {
            render_frame(target);
            last_rendered = target;
        }
        if (target == cached_total_frames - 1) break;
    }

    while (1) {
        wait_vbl_done();
    }
}
