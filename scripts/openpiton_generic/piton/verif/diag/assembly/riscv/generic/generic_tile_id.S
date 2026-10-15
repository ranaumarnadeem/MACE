# generic_tile_id.S: every tile reads the transducer's ID registers. The
# number of tiles is at least 1, the tile ID is below it, and each tile can
# write and read back its own word of a shared array.

#include "riscv_test.h"
#include "test_macros.h"

RVTEST_RV32U
RVTEST_CODE_BEGIN

  li TESTNUM, 2
  li t0, GENERIC_NUM_TILES
  lw s1, 0(t0)
  beqz s1, fail

  li TESTNUM, 3
  li t0, GENERIC_TILE_ID
  lw s0, 0(t0)
  bgeu s0, s1, fail

  li TESTNUM, 4
  la t1, slots
  slli t2, s0, 2
  add t1, t1, t2
  addi t3, s0, 0x100
  sw t3, 0(t1)
  lw t4, 0(t1)
  bne t3, t4, fail

  TEST_PASSFAIL

RVTEST_CODE_END

  .data
RVTEST_DATA_BEGIN

slots: .fill 64, 4, 0

RVTEST_DATA_END
