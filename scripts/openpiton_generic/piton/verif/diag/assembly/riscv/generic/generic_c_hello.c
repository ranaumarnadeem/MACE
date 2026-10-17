// generic_c_hello.c: every hart prints its ID and returns 0. The harts take
// turns on a plain shared counter, so one hart writes the UART at a time. No
// atomics, so it runs on any core.
#include <stdint.h>
#include "util.h"

int main(int argc, char **argv)
{
  uint32_t hart_id = argv[0][0];
  uint32_t num_harts = argv[0][1];
  static volatile uint32_t turn;

  while (turn != hart_id)
    ;
  printf("hello from hart %u of %u\n", hart_id, num_harts);
  turn = hart_id + 1;
  return 0;
}
