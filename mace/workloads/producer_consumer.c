// producer_consumer.c -- gate workload: hart 0 publishes a buffer of
// values through per-slot ready flags; the highest-numbered hart consumes
// and checksums them. Exercises store-then-flag visibility across cores --
// a design that lets the flag become visible before the data it guards
// lets the consumer read stale data, which the checksum below catches.
//
// Every array element (data[] and ready[]) is written with ATOMIC_OP and
// read with atomic_read() (ATOMIC_FETCH_OP), never a plain load or store:
// measured on real hardware, a plain volatile access to an array element
// here is not reliably visible across hart 0's write and the read below,
// even for a scalar this project tried the same way (see
// barrier_atomic.c); a plain scalar access to a non-array variable was
// fine. Until that is root-caused, every workload here goes through the
// atomic path uniformly rather than trusting which cases happen to work.
#include <stdint.h>
#include <stdio.h>
#include "util.h"

#define N 64

static uint32_t atomic_read(volatile uint32_t *mem) {
  uint32_t ret;
  ATOMIC_FETCH_OP(ret, *mem, 0, add, w);
  return ret;
}

int main(int argc, char** argv) {
  uint32_t hart_id = argv[0][0];
  uint32_t num_harts = argv[0][1];
  uint32_t producer = 0;
  uint32_t consumer = num_harts - 1;

  static volatile uint32_t data[N];
  static volatile uint32_t ready[N];
  static volatile uint32_t arrived = 0;

  ATOMIC_OP(arrived, 1, add, w);
  while (atomic_read(&arrived) < num_harts);

  if (hart_id == producer) {
    for (uint32_t i = 0; i < N; i++) {
      ATOMIC_OP(data[i], i * i + 1, add, w);
      ATOMIC_OP(ready[i], 1, add, w);
    }
  }

  if (hart_id == consumer) {
    uint32_t checksum = 0;
    for (uint32_t i = 0; i < N; i++) {
      while (atomic_read(&ready[i]) == 0);
      checksum += atomic_read(&data[i]);
    }
    uint32_t expected = 0;
    for (uint32_t i = 0; i < N; i++) {
      expected += i * i + 1;
    }
    printf("checksum=%u expected=%u\n", checksum, expected);
    return checksum != expected;
  }

  return 0;
}
