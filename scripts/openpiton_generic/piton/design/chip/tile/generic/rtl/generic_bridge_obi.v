// generic_bridge_obi: joins a core's separate instruction and data ports, in
// OBI style (req/gnt, then rvalid), into the transducer's simple bus.
//
// One request is outstanding at a time. A port's request is granted only
// when the bridge is idle, data before instruction, and its rvalid comes with
// the simple bus's ready. Addresses pass through; a core that issues
// word-aligned addresses with byte enables (Ibex, CV32E40P) fits the simple
// bus as it is. Bus errors are never reported.

`include "iop.h"

module generic_bridge_obi (
    input  wire                         clk,
    input  wire                         rst_n,

    // instruction port
    input  wire                         instr_req,
    output wire                         instr_gnt,
    output wire                         instr_rvalid,
    input  wire [31:0]                  instr_addr,
    output wire [31:0]                  instr_rdata,
    output wire                         instr_err,

    // data port
    input  wire                         data_req,
    output wire                         data_gnt,
    output wire                         data_rvalid,
    input  wire                         data_we,
    input  wire [3:0]                   data_be,
    input  wire [31:0]                  data_addr,
    input  wire [31:0]                  data_wdata,
    output wire [31:0]                  data_rdata,
    output wire                         data_err,

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

    assign data_gnt  = ~busy & data_req;
    assign instr_gnt = ~busy & ~data_req & instr_req;

    assign bus_valid  = busy;
    assign bus_amo_op = `L15_AMO_OP_NONE;

    assign data_rvalid  = busy & busy_data & bus_ready;
    assign instr_rvalid = busy & ~busy_data & bus_ready;
    assign data_rdata   = bus_rdata;
    assign instr_rdata  = bus_rdata;
    assign data_err     = 1'b0;
    assign instr_err    = 1'b0;

    always @(posedge clk) begin
        if (!rst_n) begin
            busy      <= 1'b0;
            busy_data <= 1'b0;
            bus_addr  <= 32'b0;
            bus_wstrb <= 4'b0;
            bus_wdata <= 32'b0;
        end else if (!busy) begin
            if (data_gnt) begin
                busy      <= 1'b1;
                busy_data <= 1'b1;
                bus_addr  <= data_addr;
                bus_wstrb <= data_we ? data_be : 4'b0000;
                bus_wdata <= data_wdata;
            end else if (instr_gnt) begin
                busy      <= 1'b1;
                busy_data <= 1'b0;
                bus_addr  <= {instr_addr[31:2], 2'b00};
                bus_wstrb <= 4'b0000;
                bus_wdata <= 32'b0;
            end
        end else if (bus_ready) begin
            busy <= 1'b0;
        end
    end

endmodule
