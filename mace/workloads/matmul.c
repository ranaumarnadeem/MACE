// matmul.c -- memory-heavy gate workload for co-design searches: the harts
// share out the rows of C = A * B for two 32x32 matrices, row i to hart
// i mod num_harts, and every hart checks each of its results against a
// closed form, returning nonzero on any mismatch.
//
// A and B are compile-time constants, 12 KB with C, more than the default
// 8 KB L1D, and the inner loop walks B by column, so cache size and
// associativity change how long the program runs. No hart reads a value
// written at run time: each result is checked from the register that
// accumulated it, then stored, so the program needs none of the atomic
// reads the other gate workloads use for shared data (see
// barrier_atomic.c).
//
// With A[i][k] = 7i + 3k + 1 and B[k][j] = 5k + 2j + 3,
// C[i][j] = N*a*b + (5a + 3b)*S1 + 15*S2, where a = 7i + 1, b = 2j + 3,
// S1 = N(N-1)/2, and S2 = (N-1)N(2N-1)/6. Every value fits in 32 bits.
#include <stdint.h>
#include <stdio.h>
#include "util.h"

#define N 32
#define A_ELEM(i, k) ((i) * 7 + (k) * 3 + 1)
#define B_ELEM(k, j) ((k) * 5 + (j) * 2 + 3)

// Row r of a matrix whose element (r, c) is M(r, c), built 8 columns at a time.
#define COLS8(M, r, c) \
  M(r, c), M(r, c + 1), M(r, c + 2), M(r, c + 3), M(r, c + 4), M(r, c + 5), M(r, c + 6), M(r, c + 7)
#define ROW(M, r) { COLS8(M, r, 0), COLS8(M, r, 8), COLS8(M, r, 16), COLS8(M, r, 24) }
#define ROWS8(M, r) \
  ROW(M, r), ROW(M, r + 1), ROW(M, r + 2), ROW(M, r + 3), ROW(M, r + 4), ROW(M, r + 5), ROW(M, r + 6), ROW(M, r + 7)
#define MATRIX(M) { ROWS8(M, 0), ROWS8(M, 8), ROWS8(M, 16), ROWS8(M, 24) }

static const uint32_t A[N][N] = MATRIX(A_ELEM);
static const uint32_t B[N][N] = MATRIX(B_ELEM);
static uint32_t C[N][N];

int main(int argc, char** argv) {
  uint32_t hart_id = argv[0][0];
  uint32_t num_harts = argv[0][1];
  const uint32_t s1 = N * (N - 1) / 2;
  const uint32_t s2 = (N - 1) * N * (2 * N - 1) / 6;
  uint32_t wrong = 0;

  for (uint32_t i = hart_id; i < N; i += num_harts) {
    for (uint32_t j = 0; j < N; j++) {
      uint32_t acc = 0;
      for (uint32_t k = 0; k < N; k++) {
        acc += A[i][k] * B[k][j];
      }
      C[i][j] = acc;
      uint32_t a = 7 * i + 1;
      uint32_t b = 2 * j + 3;
      wrong += acc != N * a * b + (5 * a + 3 * b) * s1 + 15 * s2;
    }
  }

  if (hart_id == 0) {  // one line of output, not one per hart
    printf("matmul N=%u hart0_mismatches=%u\n", N, wrong);
  }
  return wrong != 0;
}
