// generic_l15_transducer: connects one core's simple memory bus to the tile's
// L1.5. It carries PicoRV32's pico_decoder/l15_picoencoder logic, with a
// request state machine in place of their valid-edge detector, a tile ID
// register, and a finish register that the testbench monitor reads.
//
// Simple bus (the master side is the core's wrapper or a bridge):
//   - The master raises core_valid with core_addr, core_wstrb, core_wdata and
//     core_amo_op, and holds them until core_ready.
//   - core_ready is high for one cycle, with core_rdata for loads and AMOs.
//     The master may raise core_valid again on the next cycle.
//   - core_wstrb == 0 is a word load. A store's core_wstrb is one of 0001,
//     0010, 0100, 1000, 0011, 1100 or 1111, and its data sits in the byte
//     lanes core_wstrb selects (lane i is core_wdata[8i+7:8i]).
//   - A request with a non-zero core_wstrb and core_amo_op !=
//     L15_AMO_OP_NONE is a 4-byte atomic, sent non-cacheable. core_amo_op is
//     ignored when core_wstrb is zero: PicoRV32 leaves it set on the fetches
//     after an atomic, and its own decoder ignores it there too.
//
// Local registers, answered here without an L1.5 request:
//   0xFFFF_FF00  read: this tile's flat ID
//   0xFFFF_FF04  read: the number of tiles
//   0xFFFF_FF08  write: finish. 1 means pass, any other non-zero value means
//                fail; the first non-zero write is kept.
//
// The L1.5 keeps data big-endian, so data is byte-swapped in both directions.
// A store with any other core_wstrb sets mon_error, which the monitor
// reports as a failed run.

`include "iop.h"

module generic_l15_transducer #(
    parameter NUM_TILES = 1
) (
    input  wire                           clk,
    input  wire                           rst_n,
    input  wire [31:0]                    tile_id,

    // core -> transducer
    input  wire                           core_valid,
    input  wire [31:0]                    core_addr,
    input  wire [3:0]                     core_wstrb,
    input  wire [31:0]                    core_wdata,
    input  wire [`L15_AMO_OP_WIDTH-1:0]   core_amo_op,
    // transducer -> core
    output wire                           core_ready,
    output reg  [31:0]                    core_rdata,
    output reg                            core_wake,

    // transducer -> L1.5
    output wire [4:0]                     transducer_l15_rqtype,
    output wire [`L15_AMO_OP_WIDTH-1:0]   transducer_l15_amo_op,
    output wire [2:0]                     transducer_l15_size,
    output wire                           transducer_l15_val,
    output wire [39:0]                    transducer_l15_address,
    output wire [63:0]                    transducer_l15_data,
    output wire                           transducer_l15_nc,
    output wire [0:0]                     transducer_l15_threadid,
    output wire                           transducer_l15_prefetch,
    output wire                           transducer_l15_invalidate_cacheline,
    output wire                           transducer_l15_blockstore,
    output wire                           transducer_l15_blockinitstore,
    output wire [1:0]                     transducer_l15_l1rplway,
    output wire [63:0]                    transducer_l15_data_next_entry,
    output wire [32:0]                    transducer_l15_csm_data,
    input  wire                           l15_transducer_ack,
    input  wire                           l15_transducer_header_ack,

    // L1.5 -> transducer
    input  wire                           l15_transducer_val,
    input  wire [3:0]                     l15_transducer_returntype,
    input  wire [63:0]                    l15_transducer_data_0,
    input  wire [63:0]                    l15_transducer_data_1,
    output wire                           transducer_l15_req_ack,

    // read by pc_cmp through `GENERIC_TRANSDUCER<n>
    output reg                            mon_finish_val,
    output reg                            mon_finish_pass,
    output reg                            mon_error
);

    localparam [27:0] LOCAL_BASE = 28'hFFFF_FF0;
    localparam [3:0]  LOCAL_TILE_ID   = 4'h0;
    localparam [3:0]  LOCAL_NUM_TILES = 4'h4;
    localparam [3:0]  LOCAL_FINISH    = 4'h8;

    localparam [1:0] S_IDLE = 2'd0;
    localparam [1:0] S_REQ  = 2'd1;
    localparam [1:0] S_RESP = 2'd2;
    localparam [1:0] S_DONE = 2'd3;

    reg [1:0]                   state;
    reg [31:0]                  req_addr;
    reg [3:0]                   req_wstrb;
    reg [31:0]                  req_wdata;
    reg [`L15_AMO_OP_WIDTH-1:0] req_amo_op;

    wire core_local = (core_addr[31:4] == LOCAL_BASE);
    wire core_store = (core_wstrb != 4'b0000);
    wire core_amo   = core_store & (core_amo_op != `L15_AMO_OP_NONE);

    reg  core_wstrb_ok;
    always @(*) begin
        case (core_wstrb)
            4'b0000, 4'b0001, 4'b0010, 4'b0100, 4'b1000,
            4'b0011, 4'b1100, 4'b1111: core_wstrb_ok = 1'b1;
            default:                   core_wstrb_ok = 1'b0;
        endcase
    end

    //--- request fields, from the latched request
    wire req_amo   = (req_wstrb != 4'b0000) & (req_amo_op != `L15_AMO_OP_NONE);
    wire req_store = (req_wstrb != 4'b0000) & ~req_amo;

    reg [1:0]  req_offset;
    reg [2:0]  req_size;
    reg [31:0] req_lanes;
    always @(*) begin
        req_offset = 2'b00;
        req_size   = `MSG_DATA_SIZE_4B;
        req_lanes  = req_wdata;
        if (req_store) begin
            case (req_wstrb)
                4'b0001: begin req_offset = 2'd0; req_size = `MSG_DATA_SIZE_1B; req_lanes = {4{req_wdata[7:0]}};   end
                4'b0010: begin req_offset = 2'd1; req_size = `MSG_DATA_SIZE_1B; req_lanes = {4{req_wdata[15:8]}};  end
                4'b0100: begin req_offset = 2'd2; req_size = `MSG_DATA_SIZE_1B; req_lanes = {4{req_wdata[23:16]}}; end
                4'b1000: begin req_offset = 2'd3; req_size = `MSG_DATA_SIZE_1B; req_lanes = {4{req_wdata[31:24]}}; end
                4'b0011: begin req_offset = 2'd0; req_size = `MSG_DATA_SIZE_2B; req_lanes = {2{req_wdata[15:0]}};  end
                4'b1100: begin req_offset = 2'd2; req_size = `MSG_DATA_SIZE_2B; req_lanes = {2{req_wdata[31:16]}}; end
                default: begin req_offset = 2'd0; req_size = `MSG_DATA_SIZE_4B; req_lanes = req_wdata;            end
            endcase
        end
    end

    wire [31:0] req_byte_addr = {req_addr[31:2], req_offset};
    wire [31:0] req_data_be   = {req_lanes[7:0], req_lanes[15:8], req_lanes[23:16], req_lanes[31:24]};

    assign transducer_l15_val     = (state == S_REQ);
    assign transducer_l15_rqtype  = req_amo   ? `PCX_REQTYPE_AMO
                                  : req_store ? `STORE_RQ
                                  :             `LOAD_RQ;
    assign transducer_l15_amo_op  = req_amo ? req_amo_op : `L15_AMO_OP_NONE;
    assign transducer_l15_size    = req_size;
    assign transducer_l15_address = {{8{req_byte_addr[31]}}, req_byte_addr};
    assign transducer_l15_nc      = req_addr[31] | req_amo;
    assign transducer_l15_data    = (req_store | req_amo) ? {req_data_be, req_data_be} : 64'b0;

    // no private cache, one thread
    assign transducer_l15_threadid             = 1'b0;
    assign transducer_l15_prefetch             = 1'b0;
    assign transducer_l15_invalidate_cacheline = 1'b0;
    assign transducer_l15_blockstore           = 1'b0;
    assign transducer_l15_blockinitstore       = 1'b0;
    assign transducer_l15_l1rplway             = 2'b0;
    assign transducer_l15_data_next_entry      = 64'b0;
    assign transducer_l15_csm_data             = 33'b0;

    //--- responses
    assign transducer_l15_req_ack = l15_transducer_val;

    wire l15_resp = l15_transducer_val &
                    ((l15_transducer_returntype == `LOAD_RET) |
                     (l15_transducer_returntype == `CPX_RESTYPE_ATOMIC_RES) |
                     (l15_transducer_returntype == `ST_ACK));

    reg [31:0] l15_word;
    always @(*) begin
        case (req_addr[3:2])
            2'b00: l15_word = l15_transducer_data_0[63:32];
            2'b01: l15_word = l15_transducer_data_0[31:0];
            2'b10: l15_word = l15_transducer_data_1[63:32];
            default: l15_word = l15_transducer_data_1[31:0];
        endcase
    end
    wire [31:0] l15_word_le = {l15_word[7:0], l15_word[15:8], l15_word[23:16], l15_word[31:24]};
    wire        l15_has_data = (l15_transducer_returntype != `ST_ACK);

    assign core_ready = (state == S_DONE);

    always @(posedge clk) begin
        if (!rst_n) begin
            state           <= S_IDLE;
            req_addr        <= 32'b0;
            req_wstrb       <= 4'b0;
            req_wdata       <= 32'b0;
            req_amo_op      <= `L15_AMO_OP_NONE;
            core_rdata      <= 32'b0;
            mon_finish_val  <= 1'b0;
            mon_finish_pass <= 1'b0;
            mon_error       <= 1'b0;
        end else begin
            case (state)
                S_IDLE: begin
                    if (core_valid) begin
                        req_addr   <= core_addr;
                        req_wstrb  <= core_wstrb;
                        req_wdata  <= core_wdata;
                        req_amo_op <= core_amo_op;
                        core_rdata <= 32'b0;
                        if (core_local) begin
                            if (!core_store && !core_amo) begin
                                case (core_addr[3:0])
                                    LOCAL_TILE_ID:   core_rdata <= tile_id;
                                    LOCAL_NUM_TILES: core_rdata <= NUM_TILES;
                                    default:         core_rdata <= 32'b0;
                                endcase
                            end else if (core_store && !core_amo && core_addr[3:0] == LOCAL_FINISH &&
                                         core_wdata != 32'b0 && !mon_finish_val) begin
                                mon_finish_val  <= 1'b1;
                                mon_finish_pass <= (core_wdata == 32'd1);
                            end
                            state <= S_DONE;
                        end else if (core_store && !core_amo && !core_wstrb_ok) begin
                            mon_error <= 1'b1;
                            state     <= S_DONE;
                        end else begin
                            state <= S_REQ;
                        end
                    end
                end
                S_REQ: begin
                    if (l15_transducer_ack) begin
                        if (l15_resp) begin
                            core_rdata <= l15_has_data ? l15_word_le : 32'b0;
                            state      <= S_DONE;
                        end else begin
                            state <= S_RESP;
                        end
                    end
                end
                S_RESP: begin
                    if (l15_resp) begin
                        core_rdata <= l15_has_data ? l15_word_le : 32'b0;
                        state      <= S_DONE;
                    end
                end
                default: begin
                    state <= S_IDLE;
                end
            endcase
        end
    end

`ifdef GENERIC_L15_DEBUG
    // -config_rtl=GENERIC_L15_DEBUG prints every L1.5 request and return
    always @(posedge clk) begin
        if (rst_n && transducer_l15_val && l15_transducer_ack)
            $display("%0t: generic tile %0d: request rqtype=%h amo=%h size=%h nc=%b addr=%h data=%h",
                     $time, tile_id, transducer_l15_rqtype, transducer_l15_amo_op, transducer_l15_size,
                     transducer_l15_nc, transducer_l15_address, transducer_l15_data);
        if (rst_n && l15_transducer_val)
            $display("%0t: generic tile %0d: return type=%h data_0=%h data_1=%h state=%0d",
                     $time, tile_id, l15_transducer_returntype, l15_transducer_data_0,
                     l15_transducer_data_1, state);
    end
`endif

    // wake-up interrupt (INT_RET with data[17:16] == 01), as l15_picoencoder
    always @(posedge clk) begin
        if (!rst_n)
            core_wake <= 1'b0;
        else
            core_wake <= l15_transducer_val &
                         (l15_transducer_returntype == `INT_RET) &
                         (l15_transducer_data_0[17:16] == 2'b01);
    end

endmodule
