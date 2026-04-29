#include <gb/gb.h>
#include <gbdk/platform.h>

#include "banks.h"

void main(void) {
    /* Music + video, audio-driven sync.
       music_tick runs in the VBL handler at ~59.7 Hz and increments
       music_cur_frame. The main loop computes the target video source
       frame from the music tick (30 fps source → target = music_tick/2)
       and skips ahead if rendering can't keep up — render_frame walks
       walk_chunk forward only, so monotonically increasing frame_no
       handles frame-skip cleanly. */
    disable_interrupts();
    music_init();
    video_init();
    add_VBL(music_tick);
    set_interrupts(VBL_IFLAG);
    enable_interrupts();

    uint16_t last_rendered = 0xFFFF;
    while (1) {
        uint16_t target = music_cur_frame >> 1;     /* 30 fps */
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
