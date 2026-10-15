// Unit testbench for generic_l15_transducer (scripts/openpiton_generic).
// A stub L1.5 with a byte memory answers loads, stores, and AMO adds after
// random delays. The core side drives the simple bus through tasks.
// Prints "PASS: generic_l15_transducer_tb" or "FAIL: <reason>" and finishes.
//
// Build and run (from a checkout that has built a manycore model, so the
// generated include files exist):
//   bash scripts/generic_ut/run_generic_ut.sh /path/to/openpiton

`timescale 1ns/1ps

module generic_l15_transducer_tb;

    localparam [31:0] TILE_ID   = 32'd7;
    localparam        NUM_TILES = 16;

    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst_n = 1'b0;

    // core side
    reg         core_valid = 1'b0;
    reg  [31:0] core_addr  = 32'b0;
    reg  [3:0]  core_wstrb = 4'b0;
    reg  [31:0] core_wdata = 32'b0;
    reg  [`L15_AMO_OP_WIDTH-1:0] core_amo_op = `L15_AMO_OP_NONE;
    wire        core_ready;
    wire [31:0] core_rdata;
    wire        core_wake;

    // L1.5 side
    wire [4:0]  rqtype;
    wire [`L15_AMO_OP_WIDTH-1:0] amo_op;
    wire [2:0]  size;
    wire        val;
    wire [39:0] address;
    wire [63:0] data;
    wire        nc;
    reg         l15_ack = 1'b0;
    reg         l15_val = 1'b0;
    reg  [3:0]  l15_returntype = 4'b0;
    reg  [63:0] l15_data_0 = 64'b0;
    reg  [63:0] l15_data_1 = 64'b0;
    wire        req_ack;

    wire mon_finish_val, mon_finish_pass, mon_error;
    wire [31:0] mon_first_addr;

    generic_l15_transducer #(.NUM_TILES(NUM_TILES)) dut (
        .clk(clk), .rst_n(rst_n), .tile_id(TILE_ID),
        .core_valid(core_valid), .core_addr(core_addr), .core_wstrb(core_wstrb),
        .core_wdata(core_wdata), .core_amo_op(core_amo_op),
        .core_ready(core_ready), .core_rdata(core_rdata), .core_wake(core_wake),
        .transducer_l15_rqtype(rqtype), .transducer_l15_amo_op(amo_op),
        .transducer_l15_size(size), .transducer_l15_val(val),
        .transducer_l15_address(address), .transducer_l15_data(data),
        .transducer_l15_nc(nc), .transducer_l15_threadid(),
        .transducer_l15_prefetch(), .transducer_l15_invalidate_cacheline(),
        .transducer_l15_blockstore(), .transducer_l15_blockinitstore(),
        .transducer_l15_l1rplway(), .transducer_l15_data_next_entry(),
        .transducer_l15_csm_data(),
        .l15_transducer_ack(l15_ack), .l15_transducer_header_ack(l15_ack),
        .l15_transducer_val(l15_val), .l15_transducer_returntype(l15_returntype),
        .l15_transducer_data_0(l15_data_0), .l15_transducer_data_1(l15_data_1),
        .transducer_l15_req_ack(req_ack),
        .mon_finish_val(mon_finish_val), .mon_finish_pass(mon_finish_pass),
        .mon_error(mon_error), .mon_first_addr(mon_first_addr)
    );

    task automatic fail(input string why);
        $display("FAIL: %s", why);
        $finish;
    endtask

    //------------------------------------------------------------------
    // stub L1.5: byte memory, big-endian words on the L1.5 side
    //------------------------------------------------------------------
    reg [7:0] mem [0:4095];
    integer   l15_requests = 0;
    reg       same_cycle_resp = 1'b0;   // answer in the cycle of the ack
    reg [4:0]  last_rqtype;
    reg [2:0]  last_size;
    reg [39:0] last_address;
    reg        last_nc;

    function automatic [31:0] mem_word_be(input [11:0] a);
        mem_word_be = {mem[{a[11:2], 2'b00}], mem[{a[11:2], 2'b01}],
                       mem[{a[11:2], 2'b10}], mem[{a[11:2], 2'b11}]};
    endfunction

    task automatic stub_write(input [39:0] a, input [2:0] sz, input [31:0] w_be);
        integer k;
        case (sz)
            `MSG_DATA_SIZE_1B: mem[a[11:0]] = w_be[31 - 8*a[1:0] -: 8];
            `MSG_DATA_SIZE_2B: begin
                mem[{a[11:1], 1'b0}] = w_be[31 - 16*a[1] -: 8];
                mem[{a[11:1], 1'b1}] = w_be[23 - 16*a[1] -: 8];
            end
            default: for (k = 0; k < 4; k = k + 1) mem[{a[11:2], 2'(k)}] = w_be[31 - 8*k -: 8];
        endcase
    endtask

    task automatic stub_respond(input [3:0] rt, input [39:0] a);
        reg [11:0] blk;
        blk = {a[11:4], 4'b0000};
        l15_val        <= 1'b1;
        l15_returntype <= rt;
        l15_data_0     <= {mem_word_be(blk), mem_word_be(blk + 12'd4)};
        l15_data_1     <= {mem_word_be(blk + 12'd8), mem_word_be(blk + 12'd12)};
    endtask

    // One request at a time: wait 0-3 cycles, ack for one cycle, then answer
    // for one cycle 1-4 cycles later (or in the ack's cycle).
    initial begin : stub
        reg [31:0] old, o_le, v_le, s_le;
        reg [3:0]  rt;
        reg [39:0] a;
        forever begin
            @(posedge clk);
            if (val) begin
                repeat ($urandom_range(0, 3)) @(posedge clk);
                if (!val) fail("transducer_l15_val dropped before the ack");
                last_rqtype  = rqtype;
                last_size    = size;
                last_address = address;
                last_nc      = nc;
                a            = address;
                l15_requests = l15_requests + 1;
                old = mem_word_be(a[11:0]);
                if (rqtype == `PCX_REQTYPE_AMO) begin
                    rt = `CPX_RESTYPE_ATOMIC_RES;
                    if (amo_op == `L15_AMO_OP_ADD) begin
                        o_le = {old[7:0], old[15:8], old[23:16], old[31:24]};
                        v_le = {data[39:32], data[47:40], data[55:48], data[63:56]};
                        s_le = o_le + v_le;
                        stub_write(a, `MSG_DATA_SIZE_4B, {s_le[7:0], s_le[15:8], s_le[23:16], s_le[31:24]});
                    end
                end else if (rqtype == `STORE_RQ) begin
                    rt = `ST_ACK;
                    stub_write(a, size, data[63:32]);
                end else begin
                    rt = `LOAD_RET;
                end
                l15_ack <= 1'b1;
                if (!same_cycle_resp) begin
                    @(posedge clk);
                    l15_ack <= 1'b0;
                    repeat ($urandom_range(0, 3)) @(posedge clk);
                end
                stub_respond(rt, a);
                if (rt == `CPX_RESTYPE_ATOMIC_RES) begin
                    // the response carries the old value
                    l15_data_0 <= {old, old};
                    l15_data_1 <= {old, old};
                end
                @(posedge clk);
                l15_ack <= 1'b0;
                l15_val <= 1'b0;
            end
        end
    end

    //------------------------------------------------------------------
    // core side
    //------------------------------------------------------------------
    reg prev_ready = 1'b0;
    always @(posedge clk) begin
        if (core_ready && prev_ready) fail("core_ready high two cycles in a row");
        prev_ready <= core_ready;
    end

    // One request; valid drops after ready unless hold is set.
    task automatic req(input [31:0] a, input [3:0] be, input [31:0] wd,
                       input [`L15_AMO_OP_WIDTH-1:0] amo, input hold, output [31:0] rd);
        core_valid  <= 1'b1;
        core_addr   <= a;
        core_wstrb  <= be;
        core_wdata  <= wd;
        core_amo_op <= amo;
        @(posedge clk);
        while (!core_ready) @(posedge clk);
        rd = core_rdata;
        if (!hold) core_valid <= 1'b0;
    endtask

    task automatic store(input [31:0] a, input [3:0] be, input [31:0] wd);
        reg [31:0] unused;
        req(a, be, wd, `L15_AMO_OP_NONE, 1'b0, unused);
    endtask

    task automatic load(input [31:0] a, output [31:0] rd);
        req(a, 4'b0000, 32'b0, `L15_AMO_OP_NONE, 1'b0, rd);
    endtask

    task automatic expect_load(input [31:0] a, input [31:0] want, input string what);
        reg [31:0] got;
        load(a, got);
        if (got !== want) begin
            $display("  %s: load 0x%08x got 0x%08x want 0x%08x", what, a, got, want);
            fail(what);
        end
    endtask

    task automatic reset_dut;
        rst_n <= 1'b0;
        repeat (4) @(posedge clk);
        rst_n <= 1'b1;
        @(posedge clk);
    endtask

    integer n_before, i;
    reg [31:0] rd;

    initial begin
        for (i = 0; i < 4096; i = i + 1) mem[i] = 8'h00;
        reset_dut();

        // 1. word store and load; byte order on the L1.5 side
        store(32'h0000_0100, 4'b1111, 32'h1122_3344);
        if (last_rqtype !== `STORE_RQ || last_size !== `MSG_DATA_SIZE_4B) fail("word store request fields");
        if (last_nc !== 1'b0) fail("cacheable store sent non-cacheable");
        if (mem[12'h100] !== 8'h44 || mem[12'h103] !== 8'h11) fail("store byte order");
        if (mon_first_addr !== 32'h0000_0100) fail("first request address not recorded");
        expect_load(32'h0000_0100, 32'h1122_3344, "word load");
        if (last_rqtype !== `LOAD_RQ || last_size !== `MSG_DATA_SIZE_4B) fail("load request fields");

        // 2. byte and halfword stores from their byte lanes (Ibex style)
        store(32'h0000_0100, 4'b0010, 32'h0000_AB00);
        if (last_size !== `MSG_DATA_SIZE_1B || last_address[1:0] !== 2'd1) fail("byte store size or offset");
        expect_load(32'h0000_0100, 32'h1122_AB44, "byte store");
        store(32'h0000_0100, 4'b1100, 32'hBEEF_0000);
        if (last_size !== `MSG_DATA_SIZE_2B || last_address[1:0] !== 2'd2) fail("halfword store size or offset");
        expect_load(32'h0000_0100, 32'hBEEF_AB44, "halfword store");
        // PicoRV32 style: byte replicated in every lane, byte address
        store(32'h0000_0103, 4'b1000, {4{8'h5A}});
        expect_load(32'h0000_0100, 32'h5AEF_AB44, "replicated byte store");
        // a load at an unaligned byte address is sent word-aligned
        expect_load(32'h0000_0102, 32'h5AEF_AB44, "unaligned load address");
        if (last_address[1:0] !== 2'b00) fail("load address not word-aligned");

        // 3. the other words of a 16-byte block
        store(32'h0000_0104, 4'b1111, 32'hA1A2_A3A4);
        store(32'h0000_0108, 4'b1111, 32'hB1B2_B3B4);
        store(32'h0000_010C, 4'b1111, 32'hC1C2_C3C4);
        expect_load(32'h0000_0104, 32'hA1A2_A3A4, "word 1 of block");
        expect_load(32'h0000_0108, 32'hB1B2_B3B4, "word 2 of block");
        expect_load(32'h0000_010C, 32'hC1C2_C3C4, "word 3 of block");

        // 4. address bit 31 is non-cacheable, sign-extended to 40 bits
        load(32'h8000_0010, rd);
        if (last_nc !== 1'b1 || last_address !== 40'hFF_8000_0010) fail("non-cacheable load");

        // 5. AMO add: request type, non-cacheable, old value returned
        store(32'h0000_0200, 4'b1111, 32'd5);
        req(32'h0000_0200, 4'b1111, 32'd3, `L15_AMO_OP_ADD, 1'b0, rd);
        if (last_rqtype !== `PCX_REQTYPE_AMO || last_nc !== 1'b1) fail("AMO request type or nc");
        if (rd !== 32'd5) fail("AMO did not return the old value");
        expect_load(32'h0000_0200, 32'd8, "AMO add result");
        // PicoRV32 leaves amo_op set on the fetches after an AMO; with
        // wstrb zero it is a plain load and the memory is not changed
        req(32'h0000_0200, 4'b0000, 32'd3, `L15_AMO_OP_ADD, 1'b0, rd);
        if (last_rqtype !== `LOAD_RQ || last_nc !== 1'b0) fail("load with a stale amo_op sent as an AMO");
        if (rd !== 32'd8) fail("load with a stale amo_op returned the wrong data");
        expect_load(32'h0000_0200, 32'd8, "memory after a load with a stale amo_op");

        // 6. back-to-back requests with valid held high
        n_before = l15_requests;
        req(32'h0000_0300, 4'b1111, 32'h0000_0001, `L15_AMO_OP_NONE, 1'b1, rd);
        req(32'h0000_0304, 4'b1111, 32'h0000_0002, `L15_AMO_OP_NONE, 1'b1, rd);
        req(32'h0000_0300, 4'b0000, 32'h0, `L15_AMO_OP_NONE, 1'b1, rd);
        if (rd !== 32'h1) fail("back-to-back load 1");
        req(32'h0000_0304, 4'b0000, 32'h0, `L15_AMO_OP_NONE, 1'b0, rd);
        if (rd !== 32'h2) fail("back-to-back load 2");
        repeat (8) @(posedge clk);
        if (l15_requests - n_before !== 4) fail("back-to-back requests duplicated or lost");

        // 7. response in the same cycle as the ack
        same_cycle_resp = 1'b1;
        store(32'h0000_0310, 4'b1111, 32'hCAFE_F00D);
        expect_load(32'h0000_0310, 32'hCAFE_F00D, "same-cycle response");
        same_cycle_resp = 1'b0;

        // 8. ID registers answer without an L1.5 request
        n_before = l15_requests;
        expect_load(32'hFFFF_FF00, TILE_ID, "tile ID register");
        expect_load(32'hFFFF_FF04, NUM_TILES, "number-of-tiles register");
        repeat (8) @(posedge clk);
        if (l15_requests !== n_before) fail("ID register read reached the L1.5");

        // 9. finish register: only a non-zero store to it finishes
        store(32'hFFFF_FF08, 4'b1111, 32'd0);
        load(32'hFFFF_FF08, rd);
        store(32'hFFFF_FF0C, 4'b1111, 32'd1);
        store(32'h0000_0400, 4'b1111, 32'd1);
        @(posedge clk);
        if (mon_finish_val) fail("finished without a non-zero store to the finish register");
        store(32'hFFFF_FF08, 4'b1111, 32'd1);
        @(posedge clk);
        if (!mon_finish_val || !mon_finish_pass) fail("pass store did not finish with pass");
        store(32'hFFFF_FF08, 4'b1111, 32'd5);
        @(posedge clk);
        if (!mon_finish_pass) fail("a later store changed the result");
        if (mon_error) fail("error set on a good run");

        // 10. fail value
        reset_dut();
        if (mon_finish_val) fail("reset did not clear finish");
        store(32'hFFFF_FF08, 4'b1111, (32'd3 << 1) | 32'd1);
        @(posedge clk);
        if (!mon_finish_val || mon_finish_pass) fail("fail store did not finish with fail");
        if (mon_first_addr !== 32'hFFFF_FF08) fail("first request after reset not recorded");

        // 11. a store the L1.5 cannot take sets mon_error and is not sent
        reset_dut();
        n_before = l15_requests;
        store(32'h0000_0500, 4'b0110, 32'h00FF_FF00);
        repeat (8) @(posedge clk);
        if (!mon_error) fail("misaligned store did not set mon_error");
        if (l15_requests !== n_before) fail("misaligned store reached the L1.5");

        // 12. wake-up interrupt
        @(posedge clk);
        l15_val        <= 1'b1;
        l15_returntype <= `INT_RET;
        l15_data_0     <= 64'h0000_0000_0001_0000;
        @(posedge clk);
        @(posedge clk);
        if (!core_wake) fail("INT_RET did not raise core_wake");

        $display("PASS: generic_l15_transducer_tb");
        $finish;
    end

    initial begin
        #200000;
        fail("testbench timeout");
    end

endmodule
