// generic_core_wrapper for Ibex (lowRISC ibex_top) on the generic tile.
//
// Ibex starts at boot_addr + 0x80, so boot_addr 0x4000_0000 gives the reset
// vector 0x4000_0080 in examples/cores/ibex.yaml. Its instruction and data
// ports are OBI-style, so generic_bridge_obi joins them into the simple bus.
// Ibex has no A extension, so the bus carries no atomics. Interrupts, debug,
// CHERIoT, and the security features are tied off; hart_id is wired, and
// programs can also read the tile ID register.

module generic_core_wrapper (
    input  logic                         clk,
    input  logic                         rst_n,
    input  logic [31:0]                  hart_id,

    output logic                         bus_valid,
    output logic [31:0]                  bus_addr,
    output logic [3:0]                   bus_wstrb,
    output logic [31:0]                  bus_wdata,
    output logic [`L15_AMO_OP_WIDTH-1:0] bus_amo_op,
    input  logic                         bus_ready,
    input  logic [31:0]                  bus_rdata,
    input  logic                         wake
);

    logic        instr_req, instr_gnt, instr_rvalid, instr_err;
    logic [31:0] instr_addr, instr_rdata;
    logic        data_req, data_gnt, data_rvalid, data_we, data_err;
    logic [3:0]  data_be;
    logic [31:0] data_addr, data_wdata, data_rdata;

    ibex_top #(
        .RV32M   (ibex_pkg::RV32MFast),
        .RegFile (ibex_pkg::RegFileFF)
    ) u_ibex (
        .clk_i                    (clk),
        .rst_ni                   (rst_n),
        .test_en_i                (1'b0),
        .ram_cfg_icache_tag_i     ('0),
        .ram_cfg_icache_tag_o     (),
        .ram_cfg_icache_data_i    ('0),
        .ram_cfg_icache_data_o    (),
        .cheriot_enable_i         (ibex_pkg::IbexMuBiOff),
        .hart_id_i                (hart_id),
        .boot_addr_i              (32'h4000_0000),
        .trvk_heap_base_addr_i    (32'h0),

        .instr_req_o              (instr_req),
        .instr_gnt_i              (instr_gnt),
        .instr_rvalid_i           (instr_rvalid),
        .instr_addr_o             (instr_addr),
        .instr_rdata_i            (instr_rdata),
        .instr_rdata_intg_i       (7'b0),
        .instr_err_i              (instr_err),

        .data_req_o               (data_req),
        .data_gnt_i               (data_gnt),
        .data_rvalid_i            (data_rvalid),
        .data_we_o                (data_we),
        .data_be_o                (data_be),
        .data_addr_o              (data_addr),
        .data_wdata_o             (data_wdata),
        .data_wdata_intg_o        (),
        .data_tag_o               (),
        .data_rdata_i             (data_rdata),
        .data_rdata_intg_i        (7'b0),
        .data_tag_i               (1'b0),
        .data_err_i               (data_err),

        .trvk_revbm_req_o         (),
        .trvk_revbm_gnt_i         (1'b0),
        .trvk_revbm_rvalid_i      (1'b0),
        .trvk_revbm_addr_o        (),
        .trvk_revbm_rdata_i       (32'b0),
        .trvk_revbm_rdata_intg_i  (7'b0),
        .trvk_revbm_err_i         (1'b0),

        .irq_software_i           (1'b0),
        .irq_timer_i              (1'b0),
        .irq_external_i           (1'b0),
        .irq_fast_i               (15'b0),
        .irq_nm_i                 (1'b0),

        .scramble_key_valid_i     (1'b0),
        .scramble_key_i           ('0),
        .scramble_nonce_i         ('0),
        .scramble_req_o           (),

        .debug_req_i              (1'b0),
        .crash_dump_o             (),
        .double_fault_seen_o      (),

        .fetch_enable_i           (ibex_pkg::IbexMuBiOn),
        .mcounteren_writable_i    (ibex_pkg::IbexMuBiOff),
        .alert_minor_o            (),
        .alert_major_internal_o   (),
        .alert_major_bus_o        (),
        .core_sleep_o             (),
        .scan_rst_ni              (1'b1),
        .lockstep_cmp_en_o        (),

        .data_req_shadow_o        (),
        .data_we_shadow_o         (),
        .data_be_shadow_o         (),
        .data_addr_shadow_o       (),
        .data_wdata_shadow_o      (),
        .data_wdata_intg_shadow_o (),
        .instr_req_shadow_o       (),
        .instr_addr_shadow_o      ()
    );

    generic_bridge_obi u_bridge (
        .clk          (clk),
        .rst_n        (rst_n),
        .instr_req    (instr_req),
        .instr_gnt    (instr_gnt),
        .instr_rvalid (instr_rvalid),
        .instr_addr   (instr_addr),
        .instr_rdata  (instr_rdata),
        .instr_err    (instr_err),
        .data_req     (data_req),
        .data_gnt     (data_gnt),
        .data_rvalid  (data_rvalid),
        .data_we      (data_we),
        .data_be      (data_be),
        .data_addr    (data_addr),
        .data_wdata   (data_wdata),
        .data_rdata   (data_rdata),
        .data_err     (data_err),
        .bus_valid    (bus_valid),
        .bus_addr     (bus_addr),
        .bus_wstrb    (bus_wstrb),
        .bus_wdata    (bus_wdata),
        .bus_amo_op   (bus_amo_op),
        .bus_ready    (bus_ready),
        .bus_rdata    (bus_rdata)
    );

endmodule
