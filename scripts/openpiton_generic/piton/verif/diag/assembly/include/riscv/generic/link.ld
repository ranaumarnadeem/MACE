/* Default link script for the generic tile: the reset vector at 0x40000000,
   as PicoRV32's. A core descriptor with another reset vector replaces this
   file. rv32_as loads the image at 0x40000000. */
OUTPUT_ARCH( "riscv" )
ENTRY(_start)

SECTIONS
{
  . = 0x40000000;
  .text.init : { *(.text.init) }
  . = ALIGN(0x1000);
  .text : { *(.text) }
  . = ALIGN(0x1000);
  .data : { *(.data) }
  .bss : { *(.bss) }
  _end = .;
}
