"""
MIDI → Game Boy chiptune encoder.

Reads resources/badapple.mid and emits a C event table that drives
Pulse 1 (melody, MIDI track 1) and Pulse 2 (bass, MIDI track 2). Each
event triggers a note on one channel; the channel keeps playing until
another event for the same channel arrives. note_off events become
"envelope = 0" silence events.

Tick base is the GB VBlank (~59.7 Hz). The runtime player advances one
tick per VBlank and fires every event whose `frame` is <= current tick.

Game Boy note frequency:
    f_hz       = 131072 / (2048 - reg)
    reg        = 2048 - 131072 / f_hz
Range: reg in [0, 2047] → f in [64 Hz, ~131 kHz]. MIDI notes below
roughly C2 (~65 Hz) are below the GB pulse channel's lower limit; we
transpose them up an octave at a time until they fit.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import mido

ROOT = Path(__file__).resolve().parents[1]
MIDI_PATH = ROOT / "resources" / "Alstroemeria Records - Bad Apple.mid"
OUT_PATH  = ROOT / "gen" / "music_data.c"

GB_FRAMES_PER_SEC = 59.7275                # actual DMG VBlank rate
ENVELOPE_ON       = 0xF0                   # vol 15, no envelope sweep
ENVELOPE_OFF      = 0x00                   # DAC off → silence

# Track-to-channel mapping for the Alstroemeria Records arrangement of
# Bad Apple (14 tracks, 215s). Track 6 has the high-register melody;
# track 3 is the bass line. Other tracks (drums on ch 9, harmonies, etc.)
# are dropped — GB only has 2 pulse channels and we're keeping it simple.
# Tracks → channel mapping. Each channel may receive events from
# multiple MIDI tracks; we concatenate them so a track ending mid-song
# can be followed by another track on the same channel (e.g. T6 melody
# 32-208s + T9 outro melody 208-239s on Pulse 1).
MELODY_TRACKS   = [6, 9]                   # Pulse 1: main + outro melody
BASS_TRACKS     = [3, 10, 11]              # Pulse 2: main bass + outro bass voices
DRUM_TRACKS     = [1, 4]                   # Noise:   kick + extended drums
HARMONY_TRACKS  = [8]                      # Wave:    mid-register harmony

# Music event table is banked. Bank 4 already holds the audio_index
# (~700 B from PCM days); plenty of room for the ~12 KB packed music
# table. We share rather than allocating a fresh bank because the PCM
# audio path is being deprecated and bank 4 will eventually be solely
# for music.
MUSIC_DATA_BANK = 4


def midi_to_gb_freq(note: int, base: int = 131072) -> int:
    """MIDI note → 11-bit GB freq register. base is the per-channel rate
    constant: 131072 for Pulse channels (1 wave step per period), 65536
    for the Wave channel (32 sub-samples per period). Transposes up an
    octave if the note is below the channel's lower limit."""
    while True:
        f = 440.0 * (2.0 ** ((note - 69) / 12.0))
        reg = 2048 - base / f
        if reg >= 0:
            return min(2047, max(0, int(round(reg))))
        note += 12


def build_tempo_map(mid: mido.MidiFile) -> list:
    """Build a global (cumulative_ticks, tempo_us_per_beat) list by
    walking ALL tracks and collecting set_tempo events in tick order.
    For type-1 MIDIs, tempo changes are typically in track 0 but apply
    globally — extracting per-track means later tracks miss them and
    drift behind. This restores the correct global timeline."""
    changes = []
    for track in mid.tracks:
        abs_ticks = 0
        for msg in track:
            abs_ticks += msg.time
            if msg.type == "set_tempo":
                changes.append((abs_ticks, msg.tempo))
    changes.sort(key=lambda x: x[0])
    if not changes or changes[0][0] != 0:
        changes.insert(0, (0, 500000))   # default 120 BPM until first set_tempo
    return changes


def ticks_to_seconds(target_ticks: int, tempo_map: list, tpb: int) -> float:
    """Convert absolute MIDI ticks to seconds using the global tempo map."""
    seconds = 0.0
    last_tick, last_tempo = tempo_map[0]
    for tick, tempo in tempo_map[1:]:
        if tick >= target_ticks:
            break
        seconds += (tick - last_tick) / tpb * (last_tempo / 1_000_000)
        last_tick, last_tempo = tick, tempo
    seconds += (target_ticks - last_tick) / tpb * (last_tempo / 1_000_000)
    return seconds


def extract_events(mid: mido.MidiFile, track_idx: int, channel: int,
                   tempo_map: list, freq_base: int = 131072) -> list:
    """Pulse/Wave channel extraction. monophonic, last-note-wins. Uses
    the global tempo_map so tracks stay synced even when tempo changes
    only appear in track 0."""
    events = []
    abs_ticks = 0
    tpb = mid.ticks_per_beat
    active_note = None

    for msg in mid.tracks[track_idx]:
        abs_ticks += msg.time
        if msg.type == "set_tempo":
            continue                       # already in tempo_map
        abs_time = ticks_to_seconds(abs_ticks, tempo_map, tpb)
        frame = int(round(abs_time * GB_FRAMES_PER_SEC))
        if msg.type == "note_on" and msg.velocity > 0:
            reg = midi_to_gb_freq(msg.note, base=freq_base)
            freq_lo = reg & 0xFF
            freq_hi = ((reg >> 8) & 0x07) | 0x80
            events.append((frame, channel, freq_lo, freq_hi, ENVELOPE_ON))
            active_note = msg.note
        elif msg.type in ("note_off",) or (msg.type == "note_on" and msg.velocity == 0):
            if msg.note == active_note:
                events.append((frame, channel, 0, 0, ENVELOPE_OFF))
                active_note = None
    return events


def extract_drum_events(mid: mido.MidiFile, track_idx: int, channel: int,
                        tempo_map: list) -> list:
    """Noise-channel extraction. Drums get only trigger events — the
    noise channel's envelope handles its own decay."""
    events = []
    abs_ticks = 0
    tpb = mid.ticks_per_beat
    for msg in mid.tracks[track_idx]:
        abs_ticks += msg.time
        if msg.type == "set_tempo":
            continue
        if msg.type == "note_on" and msg.velocity > 0:
            abs_time = ticks_to_seconds(abs_ticks, tempo_map, tpb)
            frame = int(round(abs_time * GB_FRAMES_PER_SEC))
            events.append((frame, channel, 0, 0, ENVELOPE_ON))
    return events


def merge_consecutive_silences(events: list) -> list:
    """If two adjacent silence events on the same channel land on the same
    frame, keep one. Reduces event count without changing playback."""
    out = []
    for e in events:
        if out and out[-1][:2] == e[:2] and out[-1][4] == 0 and e[4] == 0:
            continue
        out.append(e)
    return out


def drop_redundant_silences(events: list, gap: int = 3) -> list:
    """Drop a silence event if another note_on for the same channel
    arrives within `gap` ticks — the next trigger overwrites the channel
    state anyway, so the brief silence is inaudible. Lets us fit the
    table in a single 16 KB bank without sounding different."""
    out = []
    n = len(events)
    for i, e in enumerate(events):
        if e[4] != ENVELOPE_OFF:
            out.append(e)
            continue
        keep = True
        for j in range(i + 1, n):
            future = events[j]
            if future[0] > e[0] + gap:
                break
            if future[1] == e[1] and future[4] == ENVELOPE_ON:
                keep = False
                break
        if keep:
            out.append(e)
    return out


def encode(midi_path: Path, out_path: Path) -> None:
    mid = mido.MidiFile(str(midi_path))
    print(f"midi: type={mid.type} length={mid.length:.1f}s tpb={mid.ticks_per_beat}")
    print(f"tracks: {len(mid.tracks)}")

    tempo_map = build_tempo_map(mid)
    print(f"global tempo map: {len(tempo_map)} entries")

    melody, bass, drums, harmony = [], [], [], []
    for ti in MELODY_TRACKS:
        melody  += extract_events(mid, ti, channel=0, tempo_map=tempo_map)
    for ti in BASS_TRACKS:
        bass    += extract_events(mid, ti, channel=1, tempo_map=tempo_map)
    for ti in DRUM_TRACKS:
        drums   += extract_drum_events(mid, ti, channel=2, tempo_map=tempo_map)
    for ti in HARMONY_TRACKS:
        harmony += extract_events(mid, ti, channel=3, tempo_map=tempo_map, freq_base=65536)
    print(f"tracks {MELODY_TRACKS}  → Pulse 1: {len(melody)} events")
    print(f"tracks {BASS_TRACKS}    → Pulse 2: {len(bass)} events")
    print(f"tracks {DRUM_TRACKS}    → Noise  : {len(drums)} events")
    print(f"tracks {HARMONY_TRACKS} → Wave   : {len(harmony)} events")

    # Silence channels with sustained notes (pulse + wave) at the end.
    # Noise decays via its envelope, no explicit silence needed.
    all_events = melody + bass + drums + harmony
    last_frame = max((e[0] for e in all_events), default=0)
    melody.append((last_frame + 1, 0, 0, 0, ENVELOPE_OFF))
    bass.append((last_frame + 1, 1, 0, 0, ENVELOPE_OFF))
    harmony.append((last_frame + 1, 3, 0, 0, ENVELOPE_OFF))

    events = sorted(melody + bass + drums + harmony, key=lambda e: (e[0], e[1]))
    events = merge_consecutive_silences(events)
    pre_drop = len(events)
    events = drop_redundant_silences(events)
    print(f"merged: {pre_drop} → {len(events)} events after dropping redundant silences")

    last_frame = events[-1][0] if events else 0
    print(f"last frame: {last_frame} (~{last_frame / GB_FRAMES_PER_SEC:.1f}s)")

    # Packed event layout (4 bytes total):
    #   byte 0..1: frame (uint16)
    #   byte 2:    bits 6..7 = channel (00=Pulse1, 01=Pulse2, 10=Noise, 11=Wave)
    #              bit 5     = envelope-on (1 = trigger / 0 = silence)
    #              bits 0..2 = high 3 bits of 11-bit GB freq reg
    #   byte 3:    low 8 bits of GB freq reg (unused for Noise)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        f.write('#include <gbdk/platform.h>\n#include <stdint.h>\n\n')
        f.write('/* Auto-generated by tools/encode_music.py. */\n\n')
        f.write('#include "music_data.h"\n\n')
        f.write(f'#pragma bank {MUSIC_DATA_BANK}\n\n')
        f.write(f'const uint16_t music_event_count = {len(events)};\n\n')
        f.write(f'const MusicEvent music_events[{len(events)}] = {{\n')
        for frame, ch, lo, hi, env in events:
            packed = ((ch & 0x03) << 6) \
                   | (1 << 5 if env else 0) \
                   | (hi & 0x07)
            f.write(f'    {{ {frame}, 0x{packed:02X}, 0x{lo:02X} }},\n')
        f.write('};\n')

    hdr = out_path.with_suffix(".h")
    with hdr.open("w") as f:
        f.write('#ifndef MUSIC_DATA_H\n#define MUSIC_DATA_H\n\n')
        f.write('#include <stdint.h>\n\n')
        f.write(f'#define MUSIC_DATA_BANK {MUSIC_DATA_BANK}\n\n')
        f.write('typedef struct MusicEvent {\n')
        f.write('    uint16_t frame;\n')
        f.write('    uint8_t  packed;   /* see encode_music.py for layout */\n')
        f.write('    uint8_t  freq_lo;\n')
        f.write('} MusicEvent;\n\n')
        f.write('extern const uint16_t music_event_count;\n')
        f.write('extern const MusicEvent music_events[];\n\n')
        f.write('#endif\n')
    print(f"wrote {out_path} and {hdr}")
    print(f"compact size: {len(events) * 4} B (fits in 16 KB bank: {len(events) * 4 < 16128})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--midi", type=Path, default=MIDI_PATH)
    ap.add_argument("--out",  type=Path, default=OUT_PATH)
    args = ap.parse_args()
    encode(args.midi, args.out)


if __name__ == "__main__":
    main()
