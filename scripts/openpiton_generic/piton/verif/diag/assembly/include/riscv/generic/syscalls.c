// syscalls.c: the C library a program on the generic tile gets. It has exit,
// putchar, printstr, a small printf, and the string functions gcc may call
// on its own. Output goes to the fake UART (util.h), unbuffered.

#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include "util.h"

// crt.S: write main's return value to the finish register and stop.
extern void _generic_exit(int code) __attribute__((noreturn));

void __attribute__((noreturn)) exit(int code)
{
  _generic_exit(code);
}

void __attribute__((noreturn)) abort(void)
{
  _generic_exit(134);
}

#undef putchar
int putchar(int c)
{
  *(volatile uint32_t *)GENERIC_UART = (uint8_t)c;
  return (uint8_t)c;
}

void printstr(const char *s)
{
  while (*s)
    putchar(*s++);
}

int puts(const char *s)
{
  printstr(s);
  putchar('\n');
  return 0;
}

// Prints v in the given base, padded on the left to width with pad.
static int put_number(uint32_t v, unsigned base, int width, char pad)
{
  char digits[32];
  int n = 0, count = 0;
  do {
    unsigned d = v % base;
    digits[n++] = d < 10 ? '0' + d : 'a' + d - 10;
    v /= base;
  } while (v);
  for (; width > n; width--, count++)
    putchar(pad);
  while (n--) {
    putchar(digits[n]);
    count++;
  }
  return count;
}

// %d %i %u %x %c %s %%, with an optional 0 flag, a width, and l (ignored:
// long is 32 bits). Anything else prints as written.
int printf(const char *fmt, ...)
{
  va_list ap;
  int count = 0;
  va_start(ap, fmt);
  for (; *fmt; fmt++) {
    if (*fmt != '%') {
      putchar(*fmt);
      count++;
      continue;
    }
    const char *spec = fmt++;
    char pad = ' ';
    int width = 0;
    if (*fmt == '0') {
      pad = '0';
      fmt++;
    }
    for (; *fmt >= '0' && *fmt <= '9'; fmt++)
      width = width * 10 + (*fmt - '0');
    while (*fmt == 'l')
      fmt++;
    switch (*fmt) {
    case 'd':
    case 'i': {
      int32_t v = va_arg(ap, int32_t);
      if (v < 0) {
        putchar('-');
        count++;
        width = width > 0 ? width - 1 : 0;
        count += put_number(-(uint32_t)v, 10, width, pad);
      } else {
        count += put_number(v, 10, width, pad);
      }
      break;
    }
    case 'u':
      count += put_number(va_arg(ap, uint32_t), 10, width, pad);
      break;
    case 'x':
      count += put_number(va_arg(ap, uint32_t), 16, width, pad);
      break;
    case 'c':
      putchar(va_arg(ap, int));
      count++;
      break;
    case 's': {
      const char *s = va_arg(ap, const char *);
      if (!s)
        s = "(null)";
      for (; *s; s++, count++)
        putchar(*s);
      break;
    }
    case '%':
      putchar('%');
      count++;
      break;
    default:
      for (; spec <= fmt && *spec; spec++, count++)
        putchar(*spec);
      if (!*fmt)
        fmt--;
    }
  }
  va_end(ap);
  return count;
}

// gcc turns copy and fill loops into calls to these, so the loops must not
// be turned back into calls.
#define NO_LOOP_PATTERNS __attribute__((optimize("no-tree-loop-distribute-patterns")))

NO_LOOP_PATTERNS void *memset(void *dst, int c, size_t n)
{
  unsigned char *p = dst;
  while (n--)
    *p++ = (unsigned char)c;
  return dst;
}

NO_LOOP_PATTERNS void *memcpy(void *dst, const void *src, size_t n)
{
  unsigned char *d = dst;
  const unsigned char *s = src;
  while (n--)
    *d++ = *s++;
  return dst;
}

NO_LOOP_PATTERNS void *memmove(void *dst, const void *src, size_t n)
{
  unsigned char *d = dst;
  const unsigned char *s = src;
  if (d < s)
    while (n--)
      *d++ = *s++;
  else
    while (n--)
      d[n] = s[n];
  return dst;
}

NO_LOOP_PATTERNS int memcmp(const void *a, const void *b, size_t n)
{
  const unsigned char *x = a, *y = b;
  for (; n; n--, x++, y++)
    if (*x != *y)
      return *x - *y;
  return 0;
}

NO_LOOP_PATTERNS size_t strlen(const char *s)
{
  size_t n = 0;
  while (s[n])
    n++;
  return n;
}
