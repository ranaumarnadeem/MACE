// matmul.c -- memory-heavy gate workload for co-design searches: the harts
// share out the rows of C = A * B, where A is 16x56 and B is 56x56, row i
// to hart i mod num_harts, and every hart checks each element it computes
// against a closed form, returning nonzero on any mismatch.
//
// A and B are compile-time constants. For every row it computes, a hart
// walks all of B, one column at a time, so the finish time depends on
// whether B stays in the L1D from one row to the next. B is 12.25 KB: more
// than OpenPiton's default 8 KB L1D, less than a 16 KB one. No hart reads a
// value written at run time: each element is checked in the register that
// accumulated it and never stored, so the program needs none of the atomic
// reads the other gate workloads use for shared data (see
// barrier_atomic.c).
//
// With A[i][k] = 7i + 3k + 1 and B[k][j] = 5k + 2j + 3, element (i, j) of C
// is N*a*b + (5a + 3b)*S1 + 15*S2, where a = 7i + 1, b = 2j + 3,
// S1 = N(N-1)/2, and S2 = (N-1)N(2N-1)/6. Every value fits in 32 bits.
#include <stdint.h>
#include <stdio.h>
#include "util.h"

#define ROWS 16
#define N 56
#define A_ELEM(i, k) ((i) * 7 + (k) * 3 + 1)
#define B_ELEM(k, j) ((k) * 5 + (j) * 2 + 3)

// Row r of an N-column matrix whose element (r, c) is M(r, c), built 8
// columns at a time, and eight such rows.
#define COLS8(M, r, c) \
  M(r, c), M(r, c + 1), M(r, c + 2), M(r, c + 3), M(r, c + 4), M(r, c + 5), M(r, c + 6), M(r, c + 7)
#define ROW(M, r) \
  { COLS8(M, r, 0), COLS8(M, r, 8), COLS8(M, r, 16), COLS8(M, r, 24), COLS8(M, r, 32), COLS8(M, r, 40), COLS8(M, r, 48) }
#define ROWS8(M, r) \
  ROW(M, r), ROW(M, r + 1), ROW(M, r + 2), ROW(M, r + 3), ROW(M, r + 4), ROW(M, r + 5), ROW(M, r + 6), ROW(M, r + 7)

static const uint32_t A[ROWS][N] = { ROWS8(A_ELEM, 0), ROWS8(A_ELEM, 8) };
static const uint32_t B[N][N] = {
  ROWS8(B_ELEM, 0), ROWS8(B_ELEM, 8), ROWS8(B_ELEM, 16), ROWS8(B_ELEM, 24),
  ROWS8(B_ELEM, 32), ROWS8(B_ELEM, 40), ROWS8(B_ELEM, 48),
};

int main(int argc, char** argv) {
  uint32_t hart_id = argv[0][0];
  uint32_t num_harts = argv[0][1];
  const uint32_t s1 = N * (N - 1) / 2;
  const uint32_t s2 = (N - 1) * N * (2 * N - 1) / 6;
  uint32_t wrong = 0;

  for (uint32_t i = hart_id; i < ROWS; i += num_harts) {
    for (uint32_t j = 0; j < N; j++) {
      uint32_t acc = 0;
      for (uint32_t k = 0; k < N; k++) {
        acc += A[i][k] * B[k][j];
      }
      uint32_t a = 7 * i + 1;
      uint32_t b = 2 * j + 3;
      wrong += acc != N * a * b + (5 * a + 3 * b) * s1 + 15 * s2;
    }
  }

  if (hart_id == 0) {  // one line of output, not one per hart
    printf("matmul %ux%u hart0_mismatches=%u\n", ROWS, N, wrong);
  }
  return wrong != 0;
}
