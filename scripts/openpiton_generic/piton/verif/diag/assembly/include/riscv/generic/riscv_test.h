#ifndef _ENV_GENERIC_TEST_H
#define _ENV_GENERIC_TEST_H

// Test environment for cores on the generic tile (scripts/openpiton_generic).
// A test reports its result by storing to the transducer's finish register:
// 1 for pass, (TESTNUM << 1) | 1 for fail, as riscv-tests does with tohost.
// The pass and fail labels from test_macros.h still give sims its good_trap
// and bad_trap symbols, which pc_cmp reports when the finish register is set.

#ifndef TEST_FUNC_NAME
#  define TEST_FUNC_NAME mytest
#  define TEST_FUNC_TXT "mytest"
#  define TEST_FUNC_RET mytest_ret
#endif

#define GENERIC_TILE_ID   0xFFFFFF00
#define GENERIC_NUM_TILES 0xFFFFFF04
#define GENERIC_FINISH    0xFFFFFF08

#define RVTEST_RV32U
#define TESTNUM x28

#define RVTEST_CODE_BEGIN		\
	.section .text.init;		\
	.global _start;			\
	.global TEST_FUNC_NAME;		\
	.global TEST_FUNC_RET;		\
_start:					\
TEST_FUNC_NAME:				\
	li x1, 0;			\
	li x2, 0;			\
	li x3, 0;			\
	li x4, 0;			\
	li x5, 0;			\
	li x6, 0;			\
	li x7, 0;			\
	li x8, 0;			\
	li x9, 0;			\
	li x10, 0;			\
	li x11, 0;			\
	li x12, 0;			\
	li x13, 0;			\
	li x14, 0;			\
	li x15, 0;			\
	li x16, 0;			\
	li x17, 0;			\
	li x18, 0;			\
	li x19, 0;			\
	li x20, 0;			\
	li x21, 0;			\
	li x22, 0;			\
	li x23, 0;			\
	li x24, 0;			\
	li x25, 0;			\
	li x26, 0;			\
	li x27, 0;			\
	li x28, 0;			\
	li x29, 0;			\
	li x30, 0;			\
	li x31, 0;

#define RVTEST_PASS			\
	li	a0, GENERIC_FINISH;	\
	li	a1, 1;			\
	sw	a1, 0(a0);		\
TEST_FUNC_RET:				\
1:	j	1b;

#define RVTEST_FAIL			\
	li	a0, GENERIC_FINISH;	\
	slli	a1, TESTNUM, 1;		\
	ori	a1, a1, 1;		\
	sw	a1, 0(a0);		\
2:	j	2b;

#define RVTEST_CODE_END
#define RVTEST_DATA_BEGIN .balign 4;
#define RVTEST_DATA_END

#endif
