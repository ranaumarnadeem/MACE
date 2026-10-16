// generic_bridge_wishbone: connects a Wishbone classic master (cyc, stb, we,
// sel, adr, dat) to the transducer's simple bus.
//
// The bridge takes a request when cyc and stb are high, holds it on the
// simple bus, and raises ack for one cycle with the simple bus's ready. The
// master's adr is a byte address; sel gives the byte lanes of a write. A
// read is a word read. err and rty are never raised.

`include "iop.h"

module generic_bridge_wishbone (
    input  wire                         clk,
    input  wire                         rst_n,

    input  wire                         wb_cyc,
    input  wire                         wb_stb,
    input  wire                         wb_we,
    input  wire [3:0]                   wb_sel,
    input  wire [31:0]                  wb_adr,
    input  wire [31:0]                  wb_dat_w,
    output wire [31:0]                  wb_dat_r,
    output wire                         wb_ack,

    output wire                         bus_valid,
    output reg  [31:0]                  bus_addr,
    output reg  [3:0]                   bus_wstrb,
    output reg  [31:0]                  bus_wdata,
    output wire [`L15_AMO_OP_WIDTH-1:0] bus_amo_op,
    input  wire                         bus_ready,
    input  wire [31:0]                  bus_rdata
);

    reg busy;

    assign bus_valid  = busy;
    assign bus_amo_op = `L15_AMO_OP_NONE;
    assign wb_ack     = busy & bus_ready;
    assign wb_dat_r   = bus_rdata;

    always @(posedge clk) begin
        if (!rst_n) begin
            busy      <= 1'b0;
            bus_addr  <= 32'b0;
            bus_wstrb <= 4'b0;
            bus_wdata <= 32'b0;
        end else if (!busy) begin
            if (wb_cyc && wb_stb) begin
                busy      <= 1'b1;
                bus_addr  <= wb_we ? wb_adr : {wb_adr[31:2], 2'b00};
                bus_wstrb <= wb_we ? wb_sel : 4'b0000;
                bus_wdata <= wb_dat_w;
            end
        end else if (bus_ready) begin
            busy <= 1'b0;
        end
    end

endmodule
