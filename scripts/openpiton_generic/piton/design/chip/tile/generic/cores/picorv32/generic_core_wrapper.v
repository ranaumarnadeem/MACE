// generic_core_wrapper for PicoRV32: the regression core of the generic tile.
// PicoRV32's memory interface is the simple bus, so no bridge is needed.
// It boots itself after reset (scripts/patch_openpiton.sh fix 6) and has no
// hart ID input; programs read the tile ID register instead.

`include "iop.h"

module generic_core_wrapper (
    input  wire                         clk,
    input  wire                         rst_n,
    input  wire [31:0]                  hart_id,

    output wire                         bus_valid,
    output wire [31:0]                  bus_addr,
    output wire [3:0]                   bus_wstrb,
    output wire [31:0]                  bus_wdata,
    output wire [`L15_AMO_OP_WIDTH-1:0] bus_amo_op,
    input  wire                         bus_ready,
    input  wire [31:0]                  bus_rdata,
    input  wire                         wake
);

    picorv32 core (
        .clk        (clk),
        .reset_l    (rst_n),
        .trap       (),
        .mem_valid  (bus_valid),
        .mem_instr  (),
        .mem_ready  (bus_ready),
        .mem_addr   (bus_addr),
        .mem_wdata  (bus_wdata),
        .mem_wstrb  (bus_wstrb),
        .mem_amo_op (bus_amo_op),
        .mem_rdata  (bus_rdata),
        .pico_int   (wake)
    );

endmodule
