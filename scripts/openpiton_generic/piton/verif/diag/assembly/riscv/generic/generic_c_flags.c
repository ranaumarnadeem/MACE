// generic_c_flags.c: shared memory without atomics, in C (the counterpart of
// generic_shared_flag.S). Hart 0 writes sixteen data words, then a flag. Every
// other hart waits for the flag and checks the data, and returns the number
// of wrong words. A core with a cache its descriptor did not declare, or a
// coherence fault, makes a reader see stale data or wait until the run times
// out.
#include <stdint.h>
#include "util.h"

#define WORDS 16

static volatile uint32_t shared_data[WORDS] __attribute__((aligned(64)));
static volatile uint32_t shared_flag __attribute__((aligned(64)));

int main(int argc, char **argv)
{
  uint32_t hart_id = argv[0][0];
  uint32_t wrong = 0;

  if (hart_id == 0) {
    for (uint32_t i = 0; i < WORDS; i++)
      shared_data[i] = 0x1000 + i * 7;
    shared_flag = 1;
  } else {
    while (shared_flag == 0)
      ;
    for (uint32_t i = 0; i < WORDS; i++)
      wrong += shared_data[i] != 0x1000 + i * 7;
  }
  return wrong;
}
