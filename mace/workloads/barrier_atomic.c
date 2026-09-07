// barrier_atomic.c -- gate workload: every hart bumps one shared atomic
// counter between two barriers, maximizing contention on it. A broken
// coherence implementation loses updates under concurrent read-modify-
// write, so the final count comes up short of num_harts; a correct one
// always reaches exactly num_harts.
//
// Every read of a location this file writes with ATOMIC_OP goes through
// atomic_read() (a fetch-add-zero), never a plain load: measured on real
// hardware, a plain volatile load immediately after an ATOMIC_OP on the
// same address does not reliably observe that write here, though a
// subsequent ATOMIC_FETCH_OP always does. The barrier is hand-rolled from
// that primitive rather than util.h's barrier(), which additionally
// depends on RISC-V TLS (__thread) that this baremetal environment's
// crt.S sets up by hand and is not proven correct here.
#include <stdint.h>
#include <stdio.h>
#include "util.h"

static uint32_t atomic_read(volatile uint32_t *mem) {
  uint32_t ret;
  ATOMIC_FETCH_OP(ret, *mem, 0, add, w);
  return ret;
}

int main(int argc, char** argv) {
  uint32_t hart_id = argv[0][0];
  uint32_t num_harts = argv[0][1];

  static volatile uint32_t arrived1 = 0;
  static volatile uint32_t counter = 0;
  static volatile uint32_t arrived2 = 0;

  ATOMIC_OP(arrived1, 1, add, w);
  while (atomic_read(&arrived1) < num_harts);  // barrier: everyone starts together

  ATOMIC_OP(counter, 1, add, w);

  ATOMIC_OP(arrived2, 1, add, w);
  while (atomic_read(&arrived2) < num_harts);  // barrier: everyone has incremented

  if (hart_id == 0) {
    uint32_t final = atomic_read(&counter);
    printf("counter=%u expected=%u\n", final, num_harts);
    return final != num_harts;
  }
  return 0;
}
