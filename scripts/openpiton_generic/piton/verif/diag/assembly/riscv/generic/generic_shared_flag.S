# generic_shared_flag.S: shared memory without atomics (ladder step 5).
# Tile 0 writes eight data words, then a flag. Every other tile waits for
# the flag and checks the data. A core with a cache its descriptor did not
# declare, or a coherence fault, makes a reader see stale data or spin
# until the run times out. There is no fence: PicoRV32 traps on one, and the
# generic tile sends a core's requests one at a time in the order it issues
# them.

#include "riscv_test.h"
#include "test_macros.h"

#define WORDS 8

RVTEST_RV32U
RVTEST_CODE_BEGIN

  li t0, GENERIC_TILE_ID
  lw s0, 0(t0)
  la s1, shared_data
  la s2, shared_flag
  bnez s0, reader

writer:
  li TESTNUM, 2
  li t1, 0
  li t2, WORDS
1:
  slli t3, t1, 2
  add t3, s1, t3
  addi t4, t1, 0x55
  sw t4, 0(t3)
  addi t1, t1, 1
  blt t1, t2, 1b
  li t5, 1
  sw t5, 0(s2)
  j done

reader:
  li TESTNUM, 3
2:
  lw t5, 0(s2)
  beqz t5, 2b
  li TESTNUM, 4
  li t1, 0
  li t2, WORDS
3:
  slli t3, t1, 2
  add t3, s1, t3
  lw t4, 0(t3)
  addi t6, t1, 0x55
  bne t4, t6, fail
  addi t1, t1, 1
  blt t1, t2, 3b

done:
  li TESTNUM, 5
  TEST_PASSFAIL

RVTEST_CODE_END

  .data
RVTEST_DATA_BEGIN

  .balign 64
shared_data: .fill WORDS, 4, 0
  .balign 64
shared_flag: .word 0

RVTEST_DATA_END
