// generic_bridge_wishbone2: joins a core's separate Wishbone classic instruction
// and data masters into the transducer's simple bus.
//
// One request is outstanding at a time. A master's request is taken only when
// the bridge is idle and its cyc and stb are both high, data before
// instruction. The bridge raises that master's ack for one cycle, with the
// simple bus's ready. The adr ports are byte addresses; a core with a word
// address drives {adr, 2'b00}. The data port's sel gives the byte lanes of a
// write. A read, and every instruction fetch, is a word read. err and rty are
// never raised, and no atomics are carried.

`include "iop.h"

module generic_bridge_wishbone2 (
    input  wire                         clk,
    input  wire                         rst_n,

    // instruction master
    input  wire                         iwb_cyc,
    input  wire                         iwb_stb,
    input  wire [31:0]                  iwb_adr,
    output wire [31:0]                  iwb_dat_r,
    output wire                         iwb_ack,

    // data master
    input  wire                         dwb_cyc,
    input  wire                         dwb_stb,
    input  wire                         dwb_we,
    input  wire [3:0]                   dwb_sel,
    input  wire [31:0]                  dwb_adr,
    input  wire [31:0]                  dwb_dat_w,
    output wire [31:0]                  dwb_dat_r,
    output wire                         dwb_ack,

    // simple bus to the transducer
    output wire                         bus_valid,
    output reg  [31:0]                  bus_addr,
    output reg  [3:0]                   bus_wstrb,
    output reg  [31:0]                  bus_wdata,
    output wire [`L15_AMO_OP_WIDTH-1:0] bus_amo_op,
    input  wire                         bus_ready,
    input  wire [31:0]                  bus_rdata
);

    reg busy;
    reg busy_data;

    wire data_take  = dwb_cyc & dwb_stb;
    wire instr_take = iwb_cyc & iwb_stb;

    assign bus_valid  = busy;
    assign bus_amo_op = `L15_AMO_OP_NONE;

    assign dwb_ack   = busy & busy_data & bus_ready;
    assign iwb_ack   = busy & ~busy_data & bus_ready;
    assign dwb_dat_r = bus_rdata;
    assign iwb_dat_r = bus_rdata;

    always @(posedge clk) begin
        if (!rst_n) begin
            busy      <= 1'b0;
            busy_data <= 1'b0;
            bus_addr  <= 32'b0;
            bus_wstrb <= 4'b0;
            bus_wdata <= 32'b0;
        end else if (!busy) begin
            if (data_take) begin
                busy      <= 1'b1;
                busy_data <= 1'b1;
                bus_addr  <= dwb_we ? dwb_adr : {dwb_adr[31:2], 2'b00};
                bus_wstrb <= dwb_we ? dwb_sel : 4'b0000;
                bus_wdata <= dwb_dat_w;
            end else if (instr_take) begin
                busy      <= 1'b1;
                busy_data <= 1'b0;
                bus_addr  <= {iwb_adr[31:2], 2'b00};
                bus_wstrb <= 4'b0000;
                bus_wdata <= 32'b0;
            end
        end else if (bus_ready) begin
            busy <= 1'b0;
        end
    end

endmodule
