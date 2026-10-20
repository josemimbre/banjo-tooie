BUILD_ROOT           := build/us
LD_SCRIPT            := banjotooie.ld
SPLAT_YAML           := baserom.us.yaml
ELF                  := $(BUILD_ROOT)/banjotooie_decompressed.elf
UNCOMPRESSED_ROM     := $(ELF:.elf=.z64)
DECOMPRESSED_BASEROM := decompressed.us.z64
ROM                  := $(BUILD_ROOT)/banjotooie.z64
BASEROM              := baserom.us.z64
ULTRALIB_VERSION     := J
ULTRALIB_TARGET      := libultra_rom
PYTHON3_BIN          ?= $(if $(wildcard .venv/bin/python3),.venv/bin/python3,python3)

# Output: one short coloured line per step by default; `make V=1` prints full commands.
# Colours are off when stderr is not a terminal, or with `make NO_COLOR=1`.
V ?= 0
ifeq ($(V),0)
  Q := @
endif
ifeq ($(NO_COLOR)$(shell test -t 2 || echo nocolor),)
  C_RESET := \033[0m
  C_CC    := \033[36m
  C_AS    := \033[34m
  C_LD    := \033[1;35m
  C_GEN   := \033[33m
  C_ROM   := \033[1;33m
  C_OK    := \033[1;32m
  C_FAIL  := \033[1;31m
endif
# $(call print,TAG,text): print a "TAG text" line (only when V=0)
print = $(if $(Q),@printf '  $(C_$(1))%-7s$(C_RESET) %s\n' '$(1)' '$(2)')

SRC_ROOT     := src
ASM_ROOT     := asm
ASSETS_ROOT  := assets
NONMATCHINGS := nonmatchings

C_SRCS     := $(shell find $(SRC_ROOT) -type f -iname '*.c' 2> /dev/null)
S_SRCS     := $(shell find $(ASM_ROOT) -type f -iname "*.s" -not -path "asm/$(NONMATCHINGS)/*" 2> /dev/null)
HASM_SRCS  := $(shell find $(SRC_ROOT) -type f -iname '*.s' 2> /dev/null)
BIN_SRCS   := $(shell find $(ASSETS_ROOT) -type f -iname '*.bin' -not -iname "relocs.bin" 2> /dev/null)
OVERLAYS   := $(shell $(PYTHON3_BIN) tools/list_overlays.py overlays.us.toml)

C_OBJS         := $(addprefix $(BUILD_ROOT)/,$(C_SRCS:.c=.c.o))
C_BUILD_DIRS   := $(sort $(dir $(C_OBJS)))
S_OBJS         := $(addprefix $(BUILD_ROOT)/,$(S_SRCS:.s=.s.o))
S_BUILD_DIRS   := $(sort $(dir $(S_OBJS)))
HS_OBJS        := $(addprefix $(BUILD_ROOT)/,$(HASM_SRCS:.s=.s.o))
HS_BUILD_DIRS  := $(sort $(dir $(HS_OBJS)))
BIN_OBJS       := $(addprefix $(BUILD_ROOT)/,$(BIN_SRCS:.bin=.bin.o))
BIN_BUILD_DIRS := $(sort $(dir $(BIN_OBJS)))

OVERLAY_TABLE_SRC    := $(BUILD_ROOT)/assets/overlay_table.s
OVERLAY_SYSCALLS_SRC := $(BUILD_ROOT)/assets/overlay_syscalls.s
OVERLAY_TABLE_OBJ    := $(OVERLAY_TABLE_SRC:.s=.bin.o)
OVERLAY_SYSCALLS_OBJ := $(BUILD_ROOT)/asm/core2/overlay_syscalls.s.o
OVERLAY_HEADER_SRCS  := $(foreach ovl,$(OVERLAYS),$(BUILD_ROOT)/assets/overlays/$(ovl)/$(ovl)_header.s)
OVERLAY_HEADER_OBJS  := $(OVERLAY_HEADER_SRCS:.s=.bin.o)
PRELIM_ELF           := $(ELF:.elf=_prelim.elf)
PRELIM_LD_SCRIPT     := $(BUILD_ROOT)/$(LD_SCRIPT:.ld=_prelim.ld)

ASM_PROC_C_SRCS := $(shell grep -rl GLOBAL_ASM $(SRC_ROOT) </dev/null)
ASM_PROC_C_OBJS := $(addprefix $(BUILD_ROOT)/,$(ASM_PROC_C_SRCS:.c=.c.o))
ASM_PROC_C_DEPS := $(addprefix $(BUILD_ROOT)/,$(ASM_PROC_C_SRCS:.c=.asmproc.d))
PURE_C_OBJS     := $(filter-out $(ASM_PROC_C_OBJS),$(C_OBJS))

ULTRALIB_DIR  := lib/ultralib
ULTRALIB_LIB  := $(ULTRALIB_DIR)/build/J/libultra_rom/libultra_rom.a
ULTRALIB_CORE := $(BUILD_ROOT)/libultra_rom.a 
ULTRALIB_BOOT := $(BUILD_ROOT)/libultra_rom_boot.a 

ALL_OBJS     := $(C_OBJS) $(S_OBJS) $(HS_OBJS) $(BIN_OBJS)
BUILD_DIRS   := $(sort $(C_BUILD_DIRS) $(S_BUILD_DIRS) $(HS_BUILD_DIRS) $(BIN_BUILD_DIRS))

CC       := tools/ido/cc
AS       := mips-linux-gnu-gcc
OBJCOPY  := mips-linux-gnu-objcopy
LD       := mips-linux-gnu-ld
ASM_PROC := $(PYTHON3_BIN) tools/asm-processor/asm_processor.py

OPT_LEVEL := -O2
CFLAGS    := -w -c -Wab,-r4300_mul -non_shared -G 0 -Xcpluscomm $(OPT_LEVEL) -mips2 -woff 807
CPPFLAGS  := -I include -I $(ULTRALIB_DIR)/include -I src -DBUILD_VERSION=VERSION_$(ULTRALIB_VERSION) -D_FINALROM -DF3DEX_GBI_2
ASFLAGS   := -w -march=vr4300 -mabi=32 -mgp32 -mfp32 -mips3 -mno-abicalls -G0 -fno-pic -gdwarf -c -x assembler-with-cpp -D_LANGUAGE_ASSEMBLY
LDFLAGS   := --no-warn-rwx-segments -nostdlib -T undefined_syms.us.txt --build-id=none --emit-relocs --whole-archive --no-warn-mismatch
BINOFLAGS := -I binary -O elf32-tradbigmips
Z64OFLAGS := -O binary --gap-fill=0x00

check: $(ROM)
	@if cmp -s $(DECOMPRESSED_BASEROM) $(UNCOMPRESSED_ROM); then \
		printf '$(C_OK)OK$(C_RESET)    $(UNCOMPRESSED_ROM) matches $(DECOMPRESSED_BASEROM)\n'; \
	else \
		printf '$(C_FAIL)FAIL$(C_RESET)  $(UNCOMPRESSED_ROM) differs from $(DECOMPRESSED_BASEROM)\n'; \
		printf '      find the function with: $(PYTHON3_BIN) tools/check_func.py --file src/<file>.c\n'; \
		exit 1; \
	fi

$(UNCOMPRESSED_ROM): $(ELF)
	$(call print,ROM,$@)
	$(Q)$(OBJCOPY) $(Z64OFLAGS) $< $@

$(ROM) : $(UNCOMPRESSED_ROM) $(ELF)
	$(call print,ROM,$@)
	$(Q)tools/rom_compressor $(UNCOMPRESSED_ROM) $(ELF) $(ROM) $(BASEROM) $(DECOMPRESSED_BASEROM)
	$(Q)tools/n64crc $@ > /dev/null

$(PRELIM_LD_SCRIPT): $(LD_SCRIPT)
	$(call print,GEN,$@)
	$(Q)sed 's/$(subst /,\/,$(BUILD_ROOT))\/assets\/overlay.*$$//' $< > $@

$(PRELIM_ELF): $(ALL_OBJS) $(PRELIM_LD_SCRIPT) $(OVERLAY_SYSCALLS_OBJ) $(ULTRALIB_CORE) $(ULTRALIB_BOOT)
	$(call print,LD,$@)
	$(Q)$(LD) -T $(PRELIM_LD_SCRIPT) -Map $(@:.elf=.map) $(LDFLAGS) $(ULTRALIB_CORE) $(ULTRALIB_BOOT) -o $@

$(ELF): $(ALL_OBJS) $(LD_SCRIPT) $(OVERLAY_SYSCALLS_OBJ) $(OVERLAY_TABLE_OBJ) $(OVERLAY_HEADER_OBJS) $(ULTRALIB_CORE) $(ULTRALIB_BOOT)
	$(call print,LD,$@)
	$(Q)$(LD) -T $(LD_SCRIPT) -Map $(@:.elf=.map) $(LDFLAGS) $(ULTRALIB_CORE) $(ULTRALIB_BOOT) -o $@

$(ASM_PROC_C_OBJS): $(BUILD_ROOT)/%.c.o: %.c | $(C_BUILD_DIRS)
	$(call print,CC,$< (asm-processor))
	$(Q)$(ASM_PROC) $(OPT_LEVEL) $< > $(BUILD_ROOT)/$<
	$(Q)$(CC) $(CFLAGS) $(CPPFLAGS) -c $(BUILD_ROOT)/$< -o $@
	$(Q)$(ASM_PROC) $(OPT_LEVEL) $< --post-process $@ --assembler "$(AS) $(ASFLAGS) $(CPPFLAGS)" --asm-prelude include/prelude.inc

$(PURE_C_OBJS): $(BUILD_ROOT)/%.c.o: %.c | $(C_BUILD_DIRS)
	$(call print,CC,$<)
	$(Q)$(CC) $(CFLAGS) $(CPPFLAGS) -c $< -o $@

$(S_OBJS): $(BUILD_ROOT)/%.s.o: %.s | $(S_BUILD_DIRS)
	$(call print,AS,$<)
	$(Q)$(AS) $(ASFLAGS) $(CPPFLAGS) -c $< -o $@

$(HS_OBJS): $(BUILD_ROOT)/%.s.o: %.s | $(HS_BUILD_DIRS)
	$(call print,AS,$<)
	$(Q)$(AS) $(ASFLAGS) $(CPPFLAGS) -c $< -o $@

$(OVERLAY_HEADER_OBJS): $(BUILD_ROOT)/%.bin.o: $(BUILD_ROOT)/%.s
	$(call print,AS,$<)
	$(Q)$(AS) $(ASFLAGS) $(CPPFLAGS) -c $< -o $@

$(OVERLAY_TABLE_OBJ): $(OVERLAY_TABLE_SRC)
	$(call print,AS,$<)
	$(Q)$(AS) $(ASFLAGS) $(CPPFLAGS) -c $< -o $@

$(OVERLAY_HEADER_SRCS) $(OVERLAY_TABLE_SRC) &: $(PRELIM_ELF)
	$(call print,GEN,overlay headers and table)
	$(Q)tools/overlay_processor $(PRELIM_ELF) $(BUILD_ROOT)/assets > /dev/null

$(OVERLAY_SYSCALLS_OBJ): $(OVERLAY_SYSCALLS_SRC)
	$(call print,AS,$<)
	$(Q)$(AS) $(ASFLAGS) $(CPPFLAGS) -c $< -o $@

$(OVERLAY_SYSCALLS_SRC): overlays.us.toml
	$(call print,GEN,$@)
	$(Q)tools/syscall_builder $(BUILD_ROOT)/assets

$(BIN_OBJS): $(BUILD_ROOT)/%.bin.o: %.bin | $(BIN_BUILD_DIRS)
	$(call print,BIN,$<)
	$(Q)$(OBJCOPY) $(BINOFLAGS) $< $@

$(ULTRALIB_LIB):
	$(MAKE) -C $(ULTRALIB_DIR) VERSION=$(ULTRALIB_VERSION) TARGET=$(ULTRALIB_TARGET) NON_MATCHING=1

$(ULTRALIB_CORE): $(ULTRALIB_LIB)
	$(Q)cp $< $@

$(ULTRALIB_BOOT): $(ULTRALIB_CORE)
	$(call print,GEN,$@)
	$(Q)$(OBJCOPY) --prefix-symbols boot_ $< $@

$(BUILD_DIRS):
	$(Q)mkdir -p $@

clean:
	find $(BUILD_ROOT) -type f -not -name '*.asmproc.d' -not -name '*.eeprom' -delete

setup:
	$(MAKE) -C $(ULTRALIB_DIR) setup NON_MATCHING=1
	$(MAKE) -C tools
	tools/rom_decompressor baserom.us.z64 decompressed.us.z64
	$(PYTHON3_BIN) tools/splat/split.py $(SPLAT_YAML)

distclean:
	rm -rf $(BUILD_ROOT) asm assets $(DECOMPRESSED_BASEROM) undefined_*_auto.us.txt $(LD_SCRIPT)
	$(MAKE) -C tools distclean
	$(MAKE) -C $(ULTRALIB_DIR) distclean

-include $(ASM_PROC_C_DEPS)

# Disable implicit rules
MAKEFLAGS += -r

# Disable built-in rules
.SUFFIXES:

# Phony targets
.PHONY: clean check setup distclean

# Debug variable print target
print-% : ; $(info $* is a $(flavor $*) variable set to [$($*)]) @true
