// scatter_gather.c -- gate workload: every hart scatters its own id into a
// shared array at its own index (no two harts write the same slot, so any
// failure here is a visibility/coherence bug, not a race); after a sync
// point, hart 0 gathers the whole array and checksums it against the
// known id sequence.
//
// slots[] is written with ATOMIC_OP and read with atomic_read()
// (ATOMIC_FETCH_OP), never a plain load or store: measured on real
// hardware, a plain volatile access to an array element here is not
// reliably visible across hart 0's write and the read below, even though a
// plain scalar (non-array) variable accessed the same way was fine (see
// barrier_atomic.c). Until that is root-caused, every workload here goes
// through the atomic path uniformly rather than trusting which cases
// happen to work.
#include <stdint.h>
#include <stdio.h>
#include "util.h"

#define MAX_HARTS 64

static uint32_t atomic_read(volatile uint32_t *mem) {
  uint32_t ret;
  ATOMIC_FETCH_OP(ret, *mem, 0, add, w);
  return ret;
}

int main(int argc, char** argv) {
  uint32_t hart_id = argv[0][0];
  uint32_t num_harts = argv[0][1];

  static volatile uint32_t slots[MAX_HARTS];
  static volatile uint32_t arrived = 0;

  if (hart_id < MAX_HARTS) {
    ATOMIC_OP(slots[hart_id], hart_id + 1, add, w);
  }

  ATOMIC_OP(arrived, 1, add, w);
  while (atomic_read(&arrived) < num_harts);

  if (hart_id == 0) {
    uint32_t n = num_harts < MAX_HARTS ? num_harts : MAX_HARTS;
    uint32_t checksum = 0;
    for (uint32_t i = 0; i < n; i++) {
      checksum += atomic_read(&slots[i]);
    }
    uint32_t expected = n * (n + 1) / 2;
    printf("checksum=%u expected=%u\n", checksum, expected);
    return checksum != expected;
  }

  return 0;
}
