"""Run the Wishbone bridge testbench against mutated copies of the bridges; each must FAIL.

Usage: python scripts/generic_ut/mutate_wishbone_ut.py <piton_root> [name_filter ...]

Needs the same generated include files as run_bridge_wishbone_ut.sh. Exits 0 when every
mutant is caught. Work directories go under $WORK (default ~/mace-d/wb_ut_mut).
"""
import os
import shutil
import subprocess
import sys

M = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
RTL = f"{M}/scripts/openpiton_generic/piton/design/chip/tile/generic/rtl"
RUN = f"{M}/scripts/generic_ut/run_bridge_wishbone_ut.sh"
WORK = os.path.expanduser(os.environ.get("WORK", "~/mace-d/wb_ut_mut"))

W1 = "generic_bridge_wishbone.v"
W2 = "generic_bridge_wishbone2.v"
MUTANTS = {
    # one-port bridge
    "w1 ack not gated by busy": (W1, "assign wb_ack     = busy & bus_ready;", "assign wb_ack     = bus_ready;"),
    "w1 request on cyc alone": (W1, "if (wb_cyc && wb_stb) begin", "if (wb_cyc) begin"),
    "w1 request on stb alone": (W1, "if (wb_cyc && wb_stb) begin", "if (wb_stb) begin"),
    "w1 read carries sel": (W1, "bus_wstrb <= wb_we ? wb_sel : 4'b0000;", "bus_wstrb <= wb_sel;"),
    "w1 write drops sel": (W1, "bus_wstrb <= wb_we ? wb_sel : 4'b0000;", "bus_wstrb <= wb_we ? 4'b1111 : 4'b0000;"),
    "w1 read not word aligned": (W1, "wb_we ? wb_adr : {wb_adr[31:2], 2'b00}", "wb_adr"),
    "w1 write address aligned": (W1, "wb_we ? wb_adr : {wb_adr[31:2], 2'b00}", "{wb_adr[31:2], 2'b00}"),
    "w1 busy never clears": (W1, "end else if (bus_ready) begin\n            busy <= 1'b0;", "end else if (bus_ready) begin\n            busy <= 1'b1;"),
    "w1 write data lost": (W1, "bus_wdata <= wb_dat_w;", "bus_wdata <= 32'b0;"),
    "w1 read data dropped": (W1, "assign wb_dat_r   = bus_rdata;", "assign wb_dat_r   = 32'b0;"),
    # two-port bridge
    "w2 data ack not gated by busy": (W2, "assign dwb_ack   = busy & busy_data & bus_ready;", "assign dwb_ack   = busy_data & bus_ready;"),
    "w2 instruction ack not gated by busy": (W2, "assign iwb_ack   = busy & ~busy_data & bus_ready;", "assign iwb_ack   = ~busy_data & bus_ready;"),
    "w2 instruction before data": (W2, "wire data_take  = dwb_cyc & dwb_stb;", "wire data_take  = dwb_cyc & dwb_stb & ~(iwb_cyc & iwb_stb);"),
    "w2 acks to the same port": (W2, "assign iwb_ack   = busy & ~busy_data & bus_ready;", "assign iwb_ack   = busy & busy_data & bus_ready;"),
    "w2 data request marked instruction": (W2, "busy_data <= 1'b1;", "busy_data <= 1'b0;"),
    "w2 instruction address not aligned": (W2, "bus_addr  <= {iwb_adr[31:2], 2'b00};", "bus_addr  <= iwb_adr;"),
    "w2 instruction fetch carries sel": (W2, "bus_wstrb <= 4'b0000;", "bus_wstrb <= dwb_sel;"),
    "w2 data request on cyc alone": (W2, "wire data_take  = dwb_cyc & dwb_stb;", "wire data_take  = dwb_cyc;"),
    "w2 instruction request on stb alone": (W2, "wire instr_take = iwb_cyc & iwb_stb;", "wire instr_take = iwb_stb;"),
    "w2 data read not word aligned": (W2, "dwb_we ? dwb_adr : {dwb_adr[31:2], 2'b00}", "dwb_adr"),
    "w2 data write drops sel": (W2, "dwb_we ? dwb_sel : 4'b0000", "dwb_we ? 4'b1111 : 4'b0000"),
    "w2 busy never clears": (W2, "end else if (bus_ready) begin\n            busy <= 1'b0;", "end else if (bus_ready) begin\n            busy <= 1'b1;"),
    "w2 data write data lost": (W2, "bus_wdata <= dwb_dat_w;", "bus_wdata <= 32'b0;"),
    "w2 instruction data dropped": (W2, "assign iwb_dat_r = bus_rdata;", "assign iwb_dat_r = 32'b0;"),
}

os.makedirs(WORK, exist_ok=True)
bad = 0
ROOT = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
only = sys.argv[2:]
for name, (fn, old, new) in MUTANTS.items():
    if only and not any(o in name for o in only):
        continue
    src = open(f"{RTL}/{fn}").read()
    assert src.count(old) == 1, f"{name}: pattern found {src.count(old)} times"
    d = f"{WORK}/{name.replace(' ', '_')}"
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(f"{d}/rtl")
    for f in (W1, W2):
        shutil.copy(f"{RTL}/{f}", f"{d}/rtl/{f}")
    with open(f"{d}/rtl/{fn}", "w") as f:
        f.write(src.replace(old, new))
    env = dict(os.environ, RTL_DIR=f"{d}/rtl", VERILATOR=os.environ.get("VERILATOR", "verilator"))
    r = subprocess.run(["bash", RUN, ROOT, f"{d}/out"], capture_output=True, text=True, env=env, timeout=600)
    line = next((l for l in r.stdout.splitlines() if l.startswith(("PASS", "FAIL"))), None)
    if line is None:
        print(f"BUILD/RUN FAILED  {name}\n{(r.stdout + r.stderr)[-600:]}")
        bad += 1
        continue
    killed = line.startswith("FAIL")
    bad += not killed
    print(f"{'caught' if killed else 'MISSED'}  {name}: {line}")
print(f"mutants missed or broken: {bad}")
sys.exit(1 if bad else 0)
