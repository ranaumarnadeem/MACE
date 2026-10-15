# generic_expect_fail.S: a test that fails on purpose (test 2 compares 1
# with 2). A run of it must end in a failure: it checks that the finish
# register's fail value reaches the monitor.

#include "riscv_test.h"
#include "test_macros.h"

RVTEST_RV32U
RVTEST_CODE_BEGIN

  li TESTNUM, 2
  li t0, 1
  li t1, 2
  bne t0, t1, fail

  TEST_PASSFAIL

RVTEST_CODE_END

  .data
RVTEST_DATA_BEGIN
RVTEST_DATA_END
