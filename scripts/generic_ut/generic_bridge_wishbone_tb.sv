// Unit testbench for generic_bridge_wishbone (one master) and
// generic_bridge_wishbone2 (instruction and data masters).
//
// Each bridge drives a stub simple-bus slave: a 1 KB memory whose ready comes
// after a random wait, and which also raises ready while idle, so a bridge that
// acks without a request is caught. The stub checks the bus side on every
// cycle: ack exactly with valid and ready, valid low in the cycle after an
// ack, fields held while valid, no AMO. The masters check the Wishbone side:
// data, byte lanes, address alignment, and the order of service.
//
// Prints "PASS: generic_bridge_wishbone_tb" or the first "FAIL: ...".

`timescale 1ns/1ps

module wb_bus_stub (
    input  wire                         clk,
    input  wire                         rst_n,
    input  wire                         bus_valid,
    input  wire [31:0]                  bus_addr,
    input  wire [3:0]                   bus_wstrb,
    input  wire [31:0]                  bus_wdata,
    input  wire [`L15_AMO_OP_WIDTH-1:0] bus_amo_op,
    output wire                         bus_ready,
    output wire [31:0]                  bus_rdata,
    input  wire                         ack_a,
    input  wire                         ack_b,
    input  wire [1:0]                   max_wait,
    output reg                          err
);
    reg [31:0] mem [0:255];
    reg [1:0]  wcnt, target;
    reg [31:0] lfsr;
    reg        prev_valid, prev_ack;
    reg [31:0] h_addr, h_wdata;
    reg [3:0]  h_wstrb;

    integer    n_done, n_rise;
    reg [31:0] last_addr, last_wdata;
    reg [3:0]  last_wstrb;

    // ready after a random wait while valid; random noise while idle
    assign bus_ready = bus_valid ? (wcnt >= target) : lfsr[3];
    assign bus_rdata = mem[bus_addr[9:2]];

    initial begin
        n_done = 0; n_rise = 0; err = 0; lfsr = 32'h1badcafe;
        wcnt = 0; target = 0; prev_valid = 0; prev_ack = 0;
    end

    task automatic bad(input string why);
        begin
            if (!err) $display("FAIL: %0s (t=%0t)", why, $time);
            err = 1;
            $finish;
        end
    endtask

    always @(posedge clk) begin
        lfsr <= {lfsr[30:0], lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};
        if (!rst_n) begin
            wcnt <= 0; target <= 0; prev_valid <= 0; prev_ack <= 0;
        end else begin
            prev_valid <= bus_valid;
            prev_ack   <= ack_a | ack_b;

            if (bus_valid && !prev_valid) begin
                n_rise = n_rise + 1;
                h_addr <= bus_addr; h_wdata <= bus_wdata; h_wstrb <= bus_wstrb;
            end
            if (bus_valid && prev_valid &&
                (bus_addr !== h_addr || bus_wstrb !== h_wstrb || (|h_wstrb && bus_wdata !== h_wdata)))
                bad("bus fields changed while valid");
            if (bus_valid && bus_amo_op !== `L15_AMO_OP_NONE) bad("bus_amo_op is not NONE");
            if (bus_valid && bus_ready && ((ack_a ^ ack_b) !== 1'b1)) bad("ack not given to exactly one master with ready");
            if (!(bus_valid && bus_ready) && (ack_a | ack_b)) bad("ack without a request in flight");
            if (prev_ack && bus_valid) bad("bus_valid in the cycle after an ack");

            if (bus_valid && bus_ready) begin
                wcnt   <= 0;
                target <= lfsr[1:0] & max_wait;
                n_done = n_done + 1;
                last_addr  <= bus_addr;
                last_wstrb <= bus_wstrb;
                last_wdata <= bus_wdata;
                if (bus_wstrb[0]) mem[bus_addr[9:2]][7:0]   <= bus_wdata[7:0];
                if (bus_wstrb[1]) mem[bus_addr[9:2]][15:8]  <= bus_wdata[15:8];
                if (bus_wstrb[2]) mem[bus_addr[9:2]][23:16] <= bus_wdata[23:16];
                if (bus_wstrb[3]) mem[bus_addr[9:2]][31:24] <= bus_wdata[31:24];
            end else if (bus_valid) begin
                wcnt <= wcnt + 2'd1;
            end
        end
    end
endmodule


module generic_bridge_wishbone_tb;
    reg clk = 1'b0;
    always #5 clk = ~clk;
    reg rst_n = 1'b0;

    integer cyc_n = 0;
    always @(posedge clk) cyc_n <= cyc_n + 1;

    reg [1:0] max_wait = 2'd0;
    reg failed = 1'b0;

    // port 0: the one-port bridge's master. ports 1 and 2: the data and
    // instruction masters of the two-port bridge.
    reg         a_cyc = 0, a_stb = 0, a_we = 0;
    reg  [3:0]  a_sel = 0;
    reg  [31:0] a_adr = 0, a_dat_w = 0;
    wire [31:0] a_dat_r;
    wire        a_ack;

    reg         d_cyc = 0, d_stb = 0, d_we = 0;
    reg  [3:0]  d_sel = 0;
    reg  [31:0] d_adr = 0, d_dat_w = 0;
    wire [31:0] d_dat_r;
    wire        d_ack;

    reg         i_cyc = 0, i_stb = 0;
    reg  [31:0] i_adr = 0;
    wire [31:0] i_dat_r;
    wire        i_ack;

    wire        b1_valid, b2_valid, b1_ready, b2_ready, err1, err2;
    wire [31:0] b1_addr, b1_wdata, b1_rdata, b2_addr, b2_wdata, b2_rdata;
    wire [3:0]  b1_wstrb, b2_wstrb;
    wire [`L15_AMO_OP_WIDTH-1:0] b1_amo, b2_amo;

    generic_bridge_wishbone u1 (
        .clk(clk), .rst_n(rst_n),
        .wb_cyc(a_cyc), .wb_stb(a_stb), .wb_we(a_we), .wb_sel(a_sel), .wb_adr(a_adr),
        .wb_dat_w(a_dat_w), .wb_dat_r(a_dat_r), .wb_ack(a_ack),
        .bus_valid(b1_valid), .bus_addr(b1_addr), .bus_wstrb(b1_wstrb), .bus_wdata(b1_wdata),
        .bus_amo_op(b1_amo), .bus_ready(b1_ready), .bus_rdata(b1_rdata));

    generic_bridge_wishbone2 u2 (
        .clk(clk), .rst_n(rst_n),
        .iwb_cyc(i_cyc), .iwb_stb(i_stb), .iwb_adr(i_adr), .iwb_dat_r(i_dat_r), .iwb_ack(i_ack),
        .dwb_cyc(d_cyc), .dwb_stb(d_stb), .dwb_we(d_we), .dwb_sel(d_sel), .dwb_adr(d_adr),
        .dwb_dat_w(d_dat_w), .dwb_dat_r(d_dat_r), .dwb_ack(d_ack),
        .bus_valid(b2_valid), .bus_addr(b2_addr), .bus_wstrb(b2_wstrb), .bus_wdata(b2_wdata),
        .bus_amo_op(b2_amo), .bus_ready(b2_ready), .bus_rdata(b2_rdata));

    wb_bus_stub s1 (.clk(clk), .rst_n(rst_n), .bus_valid(b1_valid), .bus_addr(b1_addr),
        .bus_wstrb(b1_wstrb), .bus_wdata(b1_wdata), .bus_amo_op(b1_amo), .bus_ready(b1_ready),
        .bus_rdata(b1_rdata), .ack_a(a_ack), .ack_b(1'b0), .max_wait(max_wait), .err(err1));

    wb_bus_stub s2 (.clk(clk), .rst_n(rst_n), .bus_valid(b2_valid), .bus_addr(b2_addr),
        .bus_wstrb(b2_wstrb), .bus_wdata(b2_wdata), .bus_amo_op(b2_amo), .bus_ready(b2_ready),
        .bus_rdata(b2_rdata), .ack_a(d_ack), .ack_b(i_ack), .max_wait(max_wait), .err(err2));

    // ---- reference memories: [0:255] for bridge 1, [256:511] for bridge 2 ----
    reg [31:0] refm [0:511];
    integer mc1 = 0, mc2 = 0;       // transactions completed, per bridge

    function automatic [31:0] pat(input integer k);
        reg [7:0] b;
        begin
            b = k[7:0];
            pat = {b, b * 8'd3 + 8'd1, b * 8'd7 + 8'd5, ~b};
        end
    endfunction

    function automatic [31:0] merge(input [31:0] old, input [31:0] nw, input [3:0] sel);
        begin
            merge[7:0]   = sel[0] ? nw[7:0]   : old[7:0];
            merge[15:8]  = sel[1] ? nw[15:8]  : old[15:8];
            merge[23:16] = sel[2] ? nw[23:16] : old[23:16];
            merge[31:24] = sel[3] ? nw[31:24] : old[31:24];
        end
    endfunction

    task automatic die(input string why);
        begin
            if (!failed) $display("FAIL: %0s (t=%0t)", why, $time);
            failed = 1;
            $finish;
        end
    endtask

    function automatic ack_of(input integer p);
        ack_of = (p == 0) ? a_ack : (p == 1) ? d_ack : i_ack;
    endfunction

    function automatic [31:0] rdat_of(input integer p);
        rdat_of = (p == 0) ? a_dat_r : (p == 1) ? d_dat_r : i_dat_r;
    endfunction

    task automatic drive(input integer p, input cyc, input stb, input we, input [3:0] sel,
                         input [31:0] adr, input [31:0] dat);
        begin
            case (p)
                0: begin a_cyc = cyc; a_stb = stb; a_we = we; a_sel = sel; a_adr = adr; a_dat_w = dat; end
                1: begin d_cyc = cyc; d_stb = stb; d_we = we; d_sel = sel; d_adr = adr; d_dat_w = dat; end
                default: begin i_cyc = cyc; i_stb = stb; i_adr = adr; end
            endcase
        end
    endtask

    // Drop the request at the next falling edge, then wait n more cycles. The
    // fields are garbage, so a bridge that looks at them while idle is caught.
    // Tasks return 1 ns after an edge, never on it, so the next wait for an
    // edge cannot catch the one that woke the caller.
    task automatic idle(input integer p, input integer n);
        integer w;
        begin
            @(negedge clk);
            drive(p, 1'b0, 1'b0, 1'b1, 4'hf, 32'hdeadbee3, 32'hdeadbeef);
            for (w = 0; w < n; w = w + 1) @(negedge clk);
            #1;
        end
    endtask

    // One Wishbone transaction on port p. It drives at the (pre+1)-th falling
    // edge from now, holds until the ack, and leaves cyc and stb high, so the
    // next call is back-to-back; idle() drops them. Port 2 only reads.
    task automatic xfer(input integer p, input we, input [3:0] sel, input [31:0] adr, input [31:0] dat,
                        input integer pre, output [31:0] rd, output integer ack_at);
        integer n, w, base, k;
        reg [31:0] exp_addr, got_addr, got_wdata, want;
        reg [3:0]  exp_wstrb, got_wstrb;
        begin
            k    = adr[9:2];
            base = (p == 0) ? 0 : 256;
            for (w = 0; w <= pre; w = w + 1) @(negedge clk);
            drive(p, 1'b1, 1'b1, we, we ? sel : 4'hf, adr, we ? dat : 32'hdeadbeef);
            n = 0;
            @(posedge clk);
            while (!ack_of(p)) begin
                n = n + 1;
                if (n > 60) die("a master saw no ack");
                @(posedge clk);
            end
            ack_at = cyc_n;
            rd     = rdat_of(p);
            #1;
            exp_addr  = we ? adr : {adr[31:2], 2'b00};
            exp_wstrb = we ? sel : 4'b0000;
            got_addr  = (p == 0) ? s1.last_addr  : s2.last_addr;
            got_wstrb = (p == 0) ? s1.last_wstrb : s2.last_wstrb;
            got_wdata = (p == 0) ? s1.last_wdata : s2.last_wdata;
            if (got_addr !== exp_addr)
                begin $display("  port %0d adr %h we %0d: bus_addr %h, wanted %h", p, adr, we, got_addr, exp_addr); die("wrong bus_addr"); end
            if (got_wstrb !== exp_wstrb)
                begin $display("  port %0d adr %h we %0d sel %b: bus_wstrb %b, wanted %b", p, adr, we, sel, got_wstrb, exp_wstrb); die("wrong bus_wstrb"); end
            if (we) begin
                if (got_wdata !== dat) die("wrong bus_wdata");
                refm[base + k] = merge(refm[base + k], dat, sel);
                want = (p == 0) ? s1.mem[k] : s2.mem[k];
                if (want !== refm[base + k]) die("stub memory differs from the reference after a write");
            end else begin
                if (rd !== refm[base + k])
                    begin $display("  port %0d adr %h: read %h, wanted %h", p, adr, rd, refm[base + k]); die("wrong read data"); end
            end
            if (p == 0) mc1 = mc1 + 1; else mc2 = mc2 + 1;
        end
    endtask

    // cyc without stb, or stb without cyc, must not start a request
    task automatic spurious(input integer p, input cyc_only);
        integer r0;
        begin
            r0 = (p == 0) ? s1.n_rise : s2.n_rise;
            @(negedge clk);
            drive(p, cyc_only, !cyc_only, 1'b1, 4'hf, 32'h0000_0040, 32'h1234_5678);
            repeat (6) @(negedge clk);
            #1;
            idle(p, 1);
            if (((p == 0) ? s1.n_rise : s2.n_rise) !== r0) die("a request started without both cyc and stb");
        end
    endtask

    // ---- tests for one data-capable port ----
    reg [31:0] rd, rd2;
    integer at, at2, at3, ii, jj;
    reg [3:0] sels [0:6];
    reg [1:0] wmax [0:3];

    task automatic battery(input integer p);
        integer base;
        begin
            base = (p == 0) ? 0 : 256;
            // aligned and unaligned reads, with gaps between
            xfer(p, 0, 4'hf, 32'h00, 0, 0, rd, at); idle(p, 2);
            xfer(p, 0, 4'hf, 32'h10, 0, 0, rd, at); idle(p, 0);
            xfer(p, 0, 4'hf, 32'h3fc, 0, 0, rd, at); idle(p, 3);
            xfer(p, 0, 4'hf, 32'h13, 0, 0, rd, at); idle(p, 1);
            xfer(p, 0, 4'hf, 32'h1e, 0, 0, rd, at); idle(p, 1);
            xfer(p, 0, 4'hf, 32'h2d, 0, 0, rd, at); idle(p, 1);
            // writes with each byte-lane select, read back
            for (ii = 0; ii < 7; ii = ii + 1) begin
                xfer(p, 1, sels[ii], 32'h40 + 4 * ii, 32'h11223344 + ii * 32'h01010101, 0, rd, at);
                idle(p, ii % 3);
                xfer(p, 0, 4'hf, 32'h40 + 4 * ii, 0, 0, rd, at);
                idle(p, 0);
            end
            // a write whose address is not word aligned passes through
            xfer(p, 1, 4'b1100, 32'h82, 32'hbeef0000, 0, rd, at); idle(p, 0);
            xfer(p, 1, 4'b0010, 32'h91, 32'h0000aa00, 0, rd, at); idle(p, 0);
            xfer(p, 0, 4'hf, 32'h80, 0, 0, rd, at); idle(p, 0);
            xfer(p, 0, 4'hf, 32'h90, 0, 0, rd, at); idle(p, 0);
            // back-to-back: stb stays high between transactions
            for (ii = 0; ii < 8; ii = ii + 1) xfer(p, 0, 4'hf, 32'h100 + 4 * ii, 0, 0, rd, at);
            for (ii = 0; ii < 8; ii = ii + 1) xfer(p, 1, sels[ii % 7], 32'h140 + 4 * ii, 32'hc0de0000 + ii, 0, rd, at);
            for (ii = 0; ii < 8; ii = ii + 1) begin
                xfer(p, ii % 2, sels[ii % 7], 32'h140 + 4 * (ii / 2), 32'h0badf00d ^ ii, 0, rd, at);
            end
            idle(p, 2);
            spurious(p, 1'b1);
            spurious(p, 1'b0);
            // still works after the spurious cycles
            xfer(p, 0, 4'hf, 32'h44, 0, 0, rd, at); idle(p, 0);
        end
    endtask

    // random traffic from both masters of bridge 2 at once; the instruction
    // master reads words 64..127, the data master works on words 0..63
    task automatic dstress(input integer count);
        integer q, g;
        reg [31:0] a, x;
        begin
            for (q = 0; q < count; q = q + 1) begin
                a = {24'b0, 6'($urandom_range(0, 63)), 2'($urandom_range(0, 3))};
                if ($urandom_range(0, 1)) xfer(1, 1, sels[$urandom_range(0, 6)], a, $urandom, 0, x, at3);
                else xfer(1, 0, 4'hf, a, 0, 0, x, at3);
                g = $urandom_range(0, 3);
                if (g != 0) idle(1, g - 1);
            end
            idle(1, 0);
        end
    endtask

    task automatic istress(input integer count);
        integer q, g;
        reg [31:0] a, x;
        begin
            for (q = 0; q < count; q = q + 1) begin
                a = {24'b0, 6'($urandom_range(64, 127)), 2'($urandom_range(0, 3))};
                xfer(2, 0, 4'hf, a, 0, 0, x, at3);
                g = $urandom_range(0, 3);
                if (g != 0) idle(2, g - 1);
            end
            idle(2, 0);
        end
    endtask

    integer wm, dn_d, dn_i;
    initial begin : main
        sels[0] = 4'b0001; sels[1] = 4'b0010; sels[2] = 4'b0100; sels[3] = 4'b1000;
        wmax[0] = 2'd0; wmax[1] = 2'd3; wmax[2] = 2'd2; wmax[3] = 2'd1;
        sels[4] = 4'b0011; sels[5] = 4'b1100; sels[6] = 4'b1111;
        for (ii = 0; ii < 256; ii = ii + 1) begin
            refm[ii]       = pat(ii);
            refm[256 + ii] = pat(ii);
            s1.mem[ii]     = pat(ii);
            s2.mem[ii]     = pat(ii);
        end
        repeat (5) @(negedge clk);
        if (b1_valid !== 1'b0 || b2_valid !== 1'b0) die("bus_valid high in reset");
        rst_n = 1'b1;
        repeat (3) @(negedge clk);
        #1;

        for (wm = 0; wm < 4; wm = wm + 1) begin
            max_wait = wmax[wm];               // zero wait, slowest, medium, short
            battery(0);
            battery(1);
            // instruction master, alone
            xfer(2, 0, 4'hf, 32'h100, 0, 0, rd, at); idle(2, 1);
            xfer(2, 0, 4'hf, 32'h10e, 0, 0, rd, at); idle(2, 0);
            xfer(2, 0, 4'hf, 32'h1fd, 0, 0, rd, at); idle(2, 2);
            for (ii = 0; ii < 6; ii = ii + 1) xfer(2, 0, 4'hf, 32'h120 + 4 * ii + (ii % 4), 0, 0, rd, at);
            idle(2, 1);
            spurious(2, 1'b1);
            spurious(2, 1'b0);

            // both masters at once: data first, then the instruction fetch
            fork
                begin xfer(1, 1, 4'b1111, 32'h08, 32'hfeedf00d, 0, rd, at); idle(1, 0); end
                begin xfer(2, 0, 4'hf, 32'h104, 0, 0, rd2, at2); idle(2, 0); end
            join
            if (!(at < at2)) die("data master was not served before the instruction master");

            // the data master keeps going: the instruction fetch waits for it
            fork
                begin
                    xfer(1, 0, 4'hf, 32'h0c, 0, 0, rd, at);
                    xfer(1, 0, 4'hf, 32'h10, 0, 0, rd, at);
                    xfer(1, 1, 4'b0110, 32'h14, 32'h00a5a500, 0, rd, at);
                    idle(1, 0);
                end
                begin xfer(2, 0, 4'hf, 32'h108, 0, 0, rd2, at2); idle(2, 0); end
            join
            if (!(at2 > at)) die("instruction fetch was served between back-to-back data accesses");

            // the instruction fetch is already under way when the data access arrives
            fork
                begin xfer(2, 0, 4'hf, 32'h10c, 0, 0, rd2, at2); idle(2, 0); end
                begin xfer(1, 1, 4'b1111, 32'h18, 32'h600dcafe, 1, rd, at); idle(1, 0); end
            join
            if (!(at2 < at)) die("a running instruction fetch was not finished first");
            xfer(1, 0, 4'hf, 32'h18, 0, 0, rd, at); idle(1, 1);

            // random traffic from both masters
            fork
                dstress(150);
                istress(150);
            join
        end

        repeat (4) @(negedge clk);
        if (s1.n_done !== mc1) begin $display("  bridge 1: stub saw %0d requests, masters made %0d", s1.n_done, mc1); die("request count differs (bridge 1)"); end
        if (s2.n_done !== mc2) begin $display("  bridge 2: stub saw %0d requests, masters made %0d", s2.n_done, mc2); die("request count differs (bridge 2)"); end
        if (!failed && !err1 && !err2) begin
            $display("PASS: generic_bridge_wishbone_tb (%0d + %0d transactions)", mc1, mc2);
            $finish;
        end
    end

    initial begin
        #5000000;
        $display("FAIL: watchdog");
        $finish;
    end
endmodule
