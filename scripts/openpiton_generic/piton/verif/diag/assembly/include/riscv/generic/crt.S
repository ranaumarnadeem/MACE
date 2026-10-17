# crt.S: startup for C programs on the generic tile, modelled on Ariane's
# runtime. Every tile runs it. A tile
#   - clears the registers and sets its stack, GENERIC_STACK_BYTES below
#     __stack_top by tile ID (link.ld defines __stack_top, __bss_start,
#     and __bss_end);
#   - waits while tile 0 clears .bss, then calls main(1, argv), where
#     argv[0][0] is the tile ID and argv[0][1] the number of tiles, as in
#     Ariane's runtime;
#   - writes main's return value to the finish register: 0 becomes 1
#     (pass), anything else becomes (value << 1) | 1 (fail), as riscv_test.h
#     does. The pass and fail labels give rv32_as its good_trap and bad_trap
#     symbols, which pc_cmp reports when the finish register is set.
# There is no trap handler: a trap ends in a timeout.

#include "util.h"

  .section ".text.init"
  .globl _start
_start:
  li x1, 0
  li x2, 0
  li x3, 0
  li x4, 0
  li x5, 0
  li x6, 0
  li x7, 0
  li x8, 0
  li x9, 0
  li x10, 0
  li x11, 0
  li x12, 0
  li x13, 0
  li x14, 0
  li x15, 0
  li x16, 0
  li x17, 0
  li x18, 0
  li x19, 0
  li x20, 0
  li x21, 0
  li x22, 0
  li x23, 0
  li x24, 0
  li x25, 0
  li x26, 0
  li x27, 0
  li x28, 0
  li x29, 0
  li x30, 0
  li x31, 0

  li t0, GENERIC_TILE_ID
  lw s0, 0(t0)
  li t0, GENERIC_NUM_TILES
  lw s1, 0(t0)

  # sp = __stack_top - tile ID * GENERIC_STACK_BYTES
  la sp, __stack_top
  slli t0, s0, GENERIC_STACK_SHIFT
  sub sp, sp, t0

  # Tile 0 clears .bss, then releases the others. Words when the bounds
  # allow it, bytes otherwise.
  bnez s0, wait_for_bss
  la t0, __bss_start
  la t1, __bss_end
  or t2, t0, t1
  andi t2, t2, 3
  bnez t2, clear_bytes
1:
  bgeu t0, t1, bss_cleared
  sw zero, 0(t0)
  addi t0, t0, 4
  j 1b
clear_bytes:
  bgeu t0, t1, bss_cleared
  sb zero, 0(t0)
  addi t0, t0, 1
  j clear_bytes
bss_cleared:
  la t0, _generic_bss_ready
  li t1, 1
  sw t1, 0(t0)
  j start_main
wait_for_bss:
  la t0, _generic_bss_ready
2:
  lw t1, 0(t0)
  beqz t1, 2b

start_main:
  # argv[0] = {tile ID, number of tiles}; argv[1] = NULL
  addi sp, sp, -16
  sb s0, 8(sp)
  sb s1, 9(sp)
  addi t0, sp, 8
  sw t0, 0(sp)
  sw zero, 4(sp)
  li a0, 1
  mv a1, sp
  call main

  .globl _generic_exit
_generic_exit:
  beqz a0, pass
  slli a0, a0, 1
  ori a0, a0, 1
  li t0, 1
  bne a0, t0, fail
  li a0, 3                    # a code of 0x80000000 would shift to 1 (pass)
  j fail

  .text
  .globl pass
pass:
  li t0, GENERIC_FINISH
  li t1, 1
  sw t1, 0(t0)
1:
  j 1b

  .globl fail
fail:
  li t0, GENERIC_FINISH
  sw a0, 0(t0)
2:
  j 2b

  # Set by tile 0 when .bss is clear. Alone in its cache line, in .data.
  .data
  .balign 64
  .globl _generic_bss_ready
_generic_bss_ready:
  .word 0
  .fill 15, 4, 0
