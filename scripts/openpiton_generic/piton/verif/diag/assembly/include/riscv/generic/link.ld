/* Default link script for the generic tile: RAM at 0x40000000, 1 MB, the reset vector at
   0x40000000, as PicoRV32's. A core descriptor replaces this file with the layout for its own
   RAM and reset vector (chia_openpiton/core_descriptor.py link_ld). rv32_as loads the image at
   RV32_IMAGE_BASE (sims -rv32_image_base). A C runtime uses __bss_start, __bss_end, and
   __stack_top. */
OUTPUT_ARCH( "riscv" )
ENTRY(_start)

SECTIONS
{
  . = 0x40000000;
  .text.init : { *(.text.init) }
  . = ALIGN(0x1000);
  .text : { *(.text .text.*) }
  . = ALIGN(0x1000);
  .rodata : { *(.rodata .rodata.* .srodata .srodata.*) }
  . = ALIGN(0x1000);
  .data : { *(.data .data.* .sdata .sdata.*) }
  .bss : { . = ALIGN(4); __bss_start = .; *(.bss .bss.* .sbss .sbss.* COMMON) . = ALIGN(4); __bss_end = .; }
  _end = .;
  __stack_top = 0x40100000;
  ASSERT(_end <= __stack_top, "the program does not fit in RAM")
}
