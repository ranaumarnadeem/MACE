# generic_amo_counter.S: atomics across tiles (ladder step 6). Every tile
# adds 1 to a shared counter with amoadd.w, then waits until the counter
# reaches the number of tiles. It polls with an atomic add of zero, since a
# plain load may not see another tile's atomic update (see fix 11 of
# scripts/patch_openpiton.sh). Needs the A extension.

#include "riscv_test.h"
#include "test_macros.h"

RVTEST_RV32U
RVTEST_CODE_BEGIN

  li t0, GENERIC_NUM_TILES
  lw s2, 0(t0)
  la s1, counter

  li TESTNUM, 2
  li t1, 1
  amoadd.w t2, t1, (s1)
  bgeu t2, s2, fail

  li TESTNUM, 3
1:
  amoadd.w t3, zero, (s1)
  bltu t3, s2, 1b
  bne t3, s2, fail

  li TESTNUM, 4
  TEST_PASSFAIL

RVTEST_CODE_END

  .data
RVTEST_DATA_BEGIN

  .balign 64
counter: .word 0

RVTEST_DATA_END
