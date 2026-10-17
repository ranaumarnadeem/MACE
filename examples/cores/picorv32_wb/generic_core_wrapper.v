// generic_core_wrapper for picorv32_wb: PicoRV32's own Wishbone top, wired by
// hand to the generic tile through generic_bridge_wishbone. It is the first
// core on the Wishbone bridge. Atomics do not reach the bus on this path
// (picorv32_wb leaves mem_amo_op unconnected), so the descriptor says
// atomics: none and the ISA is rv32im.
// picorv32_wb resets on wb_rst_i (active high), starts at PROGADDR_RESET, and
// has no hart ID input; programs read the tile ID register instead.

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

    wire        wb_cyc;
    wire        wb_stb;
    wire        wb_we;
    wire [3:0]  wb_sel;
    wire [31:0] wb_adr;
    wire [31:0] wb_dat_w;
    wire [31:0] wb_dat_r;
    wire        wb_ack;

    picorv32_wb #(
        .ENABLE_MUL     (1),
        .ENABLE_DIV     (1),
        .PROGADDR_RESET (32'h4000_0000)
    ) core (
        .trap       (),
        .wb_rst_i   (~rst_n),
        .wb_clk_i   (clk),
        .wbm_adr_o  (wb_adr),
        .wbm_dat_o  (wb_dat_w),
        .wbm_dat_i  (wb_dat_r),
        .wbm_we_o   (wb_we),
        .wbm_sel_o  (wb_sel),
        .wbm_stb_o  (wb_stb),
        .wbm_ack_i  (wb_ack),
        .wbm_cyc_o  (wb_cyc),
        .pcpi_valid (),
        .pcpi_insn  (),
        .pcpi_rs1   (),
        .pcpi_rs2   (),
        .pcpi_wr    (1'b0),
        .pcpi_rd    (32'b0),
        .pcpi_wait  (1'b0),
        .pcpi_ready (1'b0),
        .irq        (32'b0),
        .eoi        (),
        .trace_valid(),
        .trace_data (),
        .mem_instr  ()
    );

    generic_bridge_wishbone u_bridge (
        .clk        (clk),
        .rst_n      (rst_n),
        .wb_cyc     (wb_cyc),
        .wb_stb     (wb_stb),
        .wb_we      (wb_we),
        .wb_sel     (wb_sel),
        .wb_adr     (wb_adr),
        .wb_dat_w   (wb_dat_w),
        .wb_dat_r   (wb_dat_r),
        .wb_ack     (wb_ack),
        .bus_valid  (bus_valid),
        .bus_addr   (bus_addr),
        .bus_wstrb  (bus_wstrb),
        .bus_wdata  (bus_wdata),
        .bus_amo_op (bus_amo_op),
        .bus_ready  (bus_ready),
        .bus_rdata  (bus_rdata)
    );

endmodule
