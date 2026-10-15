# generic_expect_timeout.S: a test that never writes the finish register.
# A run of it must time out, never pass, even though the program contains
# the pass and fail labels that give sims its good_trap and bad_trap.

#include "riscv_test.h"
#include "test_macros.h"

RVTEST_RV32U
RVTEST_CODE_BEGIN

  li TESTNUM, 2
spin:
  j spin

  TEST_PASSFAIL

RVTEST_CODE_END

  .data
RVTEST_DATA_BEGIN
RVTEST_DATA_END
