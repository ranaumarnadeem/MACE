#ifndef _GENERIC_UTIL_H
#define _GENERIC_UTIL_H

// Runtime header for C programs on the generic tile (crt.S and syscalls.c
// are the rest of the runtime). The register addresses are the transducer's
// local registers (generic_l15_transducer.v).

#define GENERIC_TILE_ID    0xFFFFFF00
#define GENERIC_NUM_TILES  0xFFFFFF04
#define GENERIC_FINISH     0xFFFFFF08

// OpenPiton's fake UART in the manycore simulation: every write's low byte
// goes to fake_uart.log in the run directory. It needs no status polling.
#define GENERIC_UART       0xF0C2C000

// Each hart's stack is GENERIC_STACK_BYTES, below __stack_top by tile ID.
#define GENERIC_STACK_SHIFT 12
#define GENERIC_STACK_BYTES (1 << GENERIC_STACK_SHIFT)

#ifndef __ASSEMBLER__

#include <stdint.h>

// Atomic memory operations (not LR/SC), as in Ariane's util.h. They need a
// core with the A extension and a descriptor with atomics: bus.
#define ATOMIC_FETCH_OP(ret, mem, i, asm_op, asm_type) \
  __asm__ __volatile__ (                               \
    " amo" #asm_op "." #asm_type " %1, %2, %0"         \
    : "+A" (mem), "=r" (ret)                           \
    : "r" (i)                                          \
    : "memory");

#define ATOMIC_OP(mem, i, asm_op, asm_type)      \
  __asm__ __volatile__ (                         \
    " amo" #asm_op "." #asm_type " zero, %1, %0" \
    : "+A" (mem)                                 \
    : "r" (i)                                    \
    : "memory");

// CSR access. -march without zicsr (rv32ima) does not assemble csrr, so the
// instruction turns the extension on for itself.
#define read_csr(reg) ({ unsigned long __tmp; \
  __asm__ __volatile__ (".option push\n.option arch, +zicsr\ncsrr %0, " #reg "\n.option pop" \
                        : "=r" (__tmp)); \
  __tmp; })

#define write_csr(reg, val) ({ \
  __asm__ __volatile__ (".option push\n.option arch, +zicsr\ncsrw " #reg ", %0\n.option pop" \
                        :: "rK" (val)); })

void printstr(const char *s);
int putchar(int c);
int printf(const char *fmt, ...);
void exit(int code) __attribute__((noreturn));

#endif // __ASSEMBLER__

#endif // _GENERIC_UTIL_H
