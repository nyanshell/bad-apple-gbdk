GBDK ?= lib/gbdk-2020/build/gbdk
LCC = $(GBDK)/bin/lcc

PYTHON ?= .venv/bin/python

SRC_DIR = src
GEN_DIR = gen
OBJ_DIR = obj

ROM = badapple.gb

# CFLAGS:
# -Isrc/-Igen   : header search paths
# -Wf--max-allocs-per-node50000 : SDCC optimization budget (per spec)
CFLAGS = -Wf--max-allocs-per-node50000 -Isrc -Igen

# LDFLAGS:
# -Wl-yt0x19    : MBC5 plain (no SRAM) — we don't use cart RAM, so the
#                 RAM+BATT variant 0x1A would be a lie that some emulators
#                 (e.g., SameBoy) treat strictly.
# -Wl-yo512     : 512 ROM banks (8 MB)
# -Wl-j -Wl-m   : produce link map (.map) and noi/symbol files for inspection
LDFLAGS = -Wl-yt0x19 -Wl-yo512 -Wl-j -Wl-m

GEN_SOURCES := $(wildcard $(GEN_DIR)/*.c)
SRC_SOURCES := $(wildcard $(SRC_DIR)/*.c)

GEN_OBJS := $(patsubst $(GEN_DIR)/%.c,$(OBJ_DIR)/gen_%.o,$(GEN_SOURCES))
SRC_OBJS := $(patsubst $(SRC_DIR)/%.c,$(OBJ_DIR)/src_%.o,$(SRC_SOURCES))
OBJS := $(SRC_OBJS) $(GEN_OBJS)

.PHONY: all rom codegen clean distclean

all: codegen
	@$(MAKE) --no-print-directory rom

# Step 1: Run Python encoders if their inputs changed.
codegen: $(GEN_DIR)/.stamp

$(GEN_DIR)/.stamp: tools/encode_video.py tools/encode_music.py \
                   "resources/Alstroemeria Records - Bad Apple.mid" | $(GEN_DIR)
	$(PYTHON) tools/encode_video.py
	$(PYTHON) tools/encode_music.py
	@touch $@

$(GEN_DIR) $(OBJ_DIR):
	mkdir -p $@

# Step 2: Compile + link. Run as a separate make so wildcard sees gen/ files.
rom: $(ROM)

$(ROM): $(OBJS)
	$(LCC) $(LDFLAGS) -o $@ $(OBJS)

$(OBJ_DIR)/src_%.o: $(SRC_DIR)/%.c | $(OBJ_DIR)
	$(LCC) $(CFLAGS) -c -o $@ $<

$(OBJ_DIR)/gen_%.o: $(GEN_DIR)/%.c | $(OBJ_DIR)
	$(LCC) $(CFLAGS) -c -o $@ $<

# clean removes only files we own (object files + the ROM + linker artifacts).
# We use named-file rm -f rather than recursive rm -rf so a stray file the user
# drops into obj/ or the project root won't be wiped by accident.
clean:
	@find $(OBJ_DIR) -maxdepth 1 -type f -name '*.o' -delete 2>/dev/null || true
	@rmdir $(OBJ_DIR) 2>/dev/null || true
	rm -f $(ROM) badapple.map badapple.sym badapple.cdb badapple.lst \
	      badapple.ihx badapple.lk badapple.noi badapple.asm

# distclean also discards generated C sources. Removes only files matching
# encoder output names so non-encoder content in gen/ would survive.
distclean: clean
	@find $(GEN_DIR) -maxdepth 1 -type f \( \
	    -name 'frames_chunk_*.c' -o -name 'audio_chunk_*.c' \
	    -o -name 'common_tiles.c' -o -name 'frame_index.c' \
	    -o -name 'audio_index.c'  -o -name 'video_data.h' \
	    -o -name 'audio_data.h'   -o -name '.stamp' \
	    -o -name '.video_chunk_count' \
	  \) -delete 2>/dev/null || true
	@rmdir $(GEN_DIR) 2>/dev/null || true
