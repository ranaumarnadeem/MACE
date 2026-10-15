// matmul_scaled.c -- gate workload whose total work grows with the hart count,
// and which reports how long each hart took. Hart h multiplies rows
// h*ROWS_PER_HART .. (h+1)*ROWS_PER_HART - 1 of A by B, where A is
// (ROWS_PER_HART * num_harts) x 56 and B is 56x56. With every hart doing the
// same work, the finish time stays flat as harts are added unless they contend
// for something shared, so the spread of the per-hart times points at it.
//
// Every hart reads all of B, which is 12.25 KB: more than OpenPiton's default
// 8 KB L1D, less than a 16 KB one. A hart walks B one column at a time for each
// row, so the finish time depends on whether B stays in the L1D, and with many
// harts reading the same lines, on how the L2 and the network share them out.
// A's elements come from the formula and are not loaded. Each hart stores the
// elements it computes in an array on its own stack, then reads them back and
// compares them again, so the store path is checked as well as the multiplier.
//
// With A[i][k] = 7i + 3k + 1 and B[k][j] = 5k + 2j + 3, element (i, j) of C is
// N*a*b + (5a + 3b)*S1 + 15*S2, where a = 7i + 1, b = 2j + 3, S1 = N(N-1)/2,
// and S2 = (N-1)N(2N-1)/6. Every value fits in 32 bits for up to MAX_HARTS harts.
//
// Each hart reads mcycle (the counter rdcycle reads) before its first row and
// after its last, then prints
//   h<id> <start cycle> <end cycle>
// in hart order, taking turns on an atomic counter the way syscalls.c's _init
// does for its exit output. The line is short because the fake UART costs
// about a thousand cycles a character and the harts print one after another;
// a hart that finds a wrong value ends at the bad trap, so no mismatch count
// is printed. The line is formatted here by hand: printf buffers in
// thread-local storage, which these programs do not set up, and sprintf
// passes a nested function to its formatter.
#include <stdint.h>
#include "util.h"

#define ROWS_PER_HART 4
#define MAX_HARTS 1024
#define POLL_BACKOFF 200  // loop iterations between polls of the print turn
#define N 56
#define S1 (N * (N - 1) / 2)
#define S2 ((N - 1) * N * (2 * N - 1) / 6)
#define B_ELEM(k, j) ((k) * 5 + (j) * 2 + 3)

// Row r of an N-column matrix whose element (r, c) is M(r, c), built 8
// columns at a time, and eight such rows.
#define COLS8(M, r, c) \
  M(r, c), M(r, c + 1), M(r, c + 2), M(r, c + 3), M(r, c + 4), M(r, c + 5), M(r, c + 6), M(r, c + 7)
#define ROW(M, r) \
  { COLS8(M, r, 0), COLS8(M, r, 8), COLS8(M, r, 16), COLS8(M, r, 24), COLS8(M, r, 32), COLS8(M, r, 40), COLS8(M, r, 48) }
#define ROWS8(M, r) \
  ROW(M, r), ROW(M, r + 1), ROW(M, r + 2), ROW(M, r + 3), ROW(M, r + 4), ROW(M, r + 5), ROW(M, r + 6), ROW(M, r + 7)

static const uint32_t B[N][N] = {
  ROWS8(B_ELEM, 0), ROWS8(B_ELEM, 8), ROWS8(B_ELEM, 16), ROWS8(B_ELEM, 24),
  ROWS8(B_ELEM, 32), ROWS8(B_ELEM, 40), ROWS8(B_ELEM, 48),
};

// Whose turn it is to print; changed only with atomics.
static volatile uint32_t print_turn = 0;

extern void printstr(const char* s);  // syscalls.c: the fake UART, one character at a time

static uint32_t expected(uint32_t i, uint32_t j) {
  uint32_t a = 7 * i + 1;
  uint32_t b = 2 * j + 3;
  return N * a * b + (5 * a + 3 * b) * S1 + 15 * S2;
}

static char* put_str(char* p, const char* s) {
  while (*s) *p++ = *s++;
  return p;
}

static char* put_num(char* p, uint64_t v) {
  char digits[20];
  int n = 0;
  do {
    digits[n++] = '0' + v % 10;
    v /= 10;
  } while (v);
  while (n) *p++ = digits[--n];
  return p;
}

int main(int argc, char** argv) {
  uint32_t hart_id = argv[0][0];
  uint32_t num_harts = argv[0][1];
  if (num_harts > MAX_HARTS) return 1;

  volatile uint32_t c[ROWS_PER_HART][N];
  uint32_t wrong = 0;

  __asm__ __volatile__("" ::: "memory");
  uint64_t start = read_csr(mcycle);
  __asm__ __volatile__("" ::: "memory");

  for (uint32_t r = 0; r < ROWS_PER_HART; r++) {
    uint32_t i = hart_id * ROWS_PER_HART + r;
    for (uint32_t j = 0; j < N; j++) {
      uint32_t acc = 0;
      uint32_t a = 7 * i + 1;
      for (uint32_t k = 0; k < N; k++) {
        acc += a * B[k][j];
        a += 3;
      }
      c[r][j] = acc;
      wrong += acc != expected(i, j);
    }
  }
  for (uint32_t r = 0; r < ROWS_PER_HART; r++) {
    for (uint32_t j = 0; j < N; j++) {
      wrong += c[r][j] != expected(hart_id * ROWS_PER_HART + r, j);
    }
  }

  __asm__ __volatile__("" ::: "memory");
  uint64_t end = read_csr(mcycle);
  __asm__ __volatile__("" ::: "memory");

  char line[48] __attribute__((aligned(64)));
  char* p = put_str(line, "h");
  p = put_num(p, hart_id);
  p = put_str(p, " ");
  p = put_num(p, start);
  p = put_str(p, " ");
  p = put_num(p, end);
  *p++ = '\n';
  *p = 0;

  // Wait for this hart's turn. The harts before it print slowly, so the wait
  // backs off between polls instead of loading the network with atomics.
  uint32_t turn;
  do {
    ATOMIC_FETCH_OP(turn, print_turn, 0, add, w);
    if (turn != hart_id) {
      for (volatile uint32_t i = 0; i < POLL_BACKOFF; i++) {
      }
    }
  } while (turn != hart_id);
  printstr(line);
  ATOMIC_OP(print_turn, 1, add, w);

  return wrong != 0;
}
