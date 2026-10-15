// generic_reset: drives the tile's spc_grst_l from rst_n through three
// flip-flops, as pico_reset does for PicoRV32.

module generic_reset (
    input  wire gclk,
    input  wire rst_n,
    output reg  spc_grst_l
);

    reg reset_l_f;
    reg reset_l_ff;
    reg reset_l_fff;

    always @(posedge gclk) begin
        reset_l_f   <= rst_n;
        reset_l_ff  <= reset_l_f;
        reset_l_fff <= reset_l_ff;
        spc_grst_l  <= reset_l_fff;
    end

endmodule
