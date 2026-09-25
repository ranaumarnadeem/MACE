#!/bin/bash
# Make an OpenPiton checkout buildable with a modern RISC-V toolchain, and
# (fix 3) fix broken symlinks, (fix 4) fix CRLF shebangs, on a
# Windows-mounted checkout.
#
# OpenPiton's RV64 boot ROM is 2019 code and its Makefile hardcodes compiler
# flags with plain '=' assignments, so neither the environment nor a sims flag
# can override them -- they have to be patched in the checkout. Run this once
# per checkout (and in the worker image build).
#
# Idempotent: safe to re-run, and safe to run on an already-patched tree.
#
# Usage: scripts/patch_openpiton.sh [PITON_ROOT]
set -euo pipefail

ROOT="${1:-${PITON_ROOT:-}}"
if [ -z "$ROOT" ] || [ ! -d "$ROOT/piton" ]; then
    echo "usage: $0 <piton_root>   (or set PITON_ROOT)" >&2
    exit 2
fi

BOOTROM_MK="$ROOT/piton/design/chipset/rv64_platform/bootrom/linux/Makefile"
[ -f "$BOOTROM_MK" ] || { echo "not found: $BOOTROM_MK" >&2; exit 1; }

changed=0

# 1. binutils 2.38+ split zicsr/zifencei out of base RV64I, so the boot ROM's
#    "csrr s2, mhartid" no longer assembles under -march=rv64imac.
if grep -q -- "-march=rv64imac " "$BOOTROM_MK"; then
    sed -i 's/-march=rv64imac /-march=rv64imac_zicsr_zifencei /' "$BOOTROM_MK"
    echo "patched: zicsr/zifencei added to bootrom -march"
    changed=1
fi

# 2. GCC 15+ defaults to C23, where "void init_uart();" declares a function
#    taking NO arguments rather than an unspecified list -- so the boot ROM's
#    own two-argument call became a hard error. Pin the dialect it was written
#    for instead of editing the sources. sims runs `make clean` in this
#    directory before every build, so the flag applies to the next build.
if ! grep -q -- "-std=gnu17" <(grep "^CFLAGS" "$BOOTROM_MK"); then
    sed -i 's/^\(CFLAGS = .*\)$/\1 -std=gnu17/' "$BOOTROM_MK"
    echo "patched: -std=gnu17 pinned for the bootrom (GCC 15+ defaults to C23)"
    changed=1
fi

if [ "$changed" -eq 0 ]; then
    echo "already patched: $ROOT"
fi

echo "bootrom CFLAGS now:"
grep -n "^CFLAGS" "$BOOTROM_MK"

# 3. Windows-mounted checkouts (WSL over /mnt/c, or plain Windows git) default
#    core.symlinks=false, so every git-tracked symlink materializes as a
#    plain text file containing its own target path instead of a real
#    symlink -- e.g. bootrom/baremetal/rv64_platform.dts becomes 20 bytes of
#    literal text "../rv64_platform.dts" rather than a link to the real DTS
#    file. Nothing in a 1x1 build ever opens these paths, so this hid
#    completely until the first multi-tile build exercised the baremetal
#    bootrom and dtc choked trying to parse a path string as a device tree.
#    Not needed on a native Linux checkout (a GCP worker's ext4 clone, for
#    instance) -- git there defaults to real symlinks already.
(
    cd "$ROOT"
    if git config core.symlinks | grep -q true; then
        echo "core.symlinks already true: $ROOT"
    else
        broken=""
        while IFS= read -r path; do
            [ -n "$path" ] || continue
            [ -L "$path" ] || broken="$broken $path"
        done < <(git ls-files -s -- . | awk '$1 == "120000" {print $4}')
        if [ -n "$broken" ]; then
            git config core.symlinks true
            # shellcheck disable=SC2086
            git checkout -- $broken
            echo "patched: re-checked-out $(echo "$broken" | wc -w) broken symlink(s) as real symlinks"
        else
            echo "no broken symlinks found: $ROOT"
        fi
    fi
)

# 4. Same Windows-checkout root cause, different symptom: git's own CRLF
#    auto-conversion (or a plain Windows checkout) leaves some .py/.sh
#    scripts with a shebang line ending in \r\n. /usr/bin/env then looks up
#    a program literally named e.g. "python3\r", which doesn't exist --
#    "/usr/bin/env: 'python3\r': No such file or directory". Found via
#    piton/design/chip/tile/ariane/corev_apu/rv_plic/rtl/gen_plic_addrmap.py,
#    only reached once a build actually needs the PLIC (multi-tile), same
#    "nothing 1x1 ever touched this" pattern as fix 3. A checkout-wide sweep
#    (not just that one file) since the same root cause hit 66 files across
#    the tree, mostly in the ariane submodule -- fixing one at a time isn't
#    worth the churn once the pattern is this clear. Only strips \r from
#    lines that actually need it (sed 's/\r$//' is a no-op on a clean LF
#    file), so this is safe to run on an already-fixed tree too.
(
    cd "$ROOT"
    fixed=0
    while IFS= read -r -d '' f; do
        head -c 200 "$f" | grep -qP '^#!.*\r$' || continue
        sed -i 's/\r$//' "$f"
        fixed=$((fixed + 1))
    done < <(find . -type f \( -name "*.py" -o -name "*.sh" \) -print0)
    if [ "$fixed" -gt 0 ]; then
        echo "patched: stripped CRLF shebangs from $fixed script(s)"
    else
        echo "no CRLF shebangs found: $ROOT"
    fi
)

# 5. Verilator's --coverage-* flags instrument the model, but the hand-written
#    testbench Verilator links against never calls Verilator's own
#    coverage-write API -- confirmed by reading its full exit path: even a
#    run that completes cleanly (reaches Verilated::gotFinish()) produces no
#    coverage.dat, because nothing here ever asks for one. Guarded by
#    VM_COVERAGE (Verilator's own auto-define when --coverage was used at
#    Verilate time), so this is meant to be a no-op for every non-coverage
#    build -- mirrors the file's own existing VERILATOR_VCD guard convention
#    right next to each insertion point.
#
#    Must be "#if VM_COVERAGE", not "#ifdef VM_COVERAGE": Verilator's
#    generated Makefile always defines VM_COVERAGE to 0 or 1
#    (-DVM_COVERAGE=0/1) -- it never leaves it undefined. #ifdef only tests
#    definedness, so the guard was true on every build regardless of value:
#    every plain, non-coverage build linked my_top.o against
#    VerilatedCov::write()/threadCovp() while Verilator's own generated
#    Makefile correctly left verilated_cov.o out of the link (no coverage
#    requested), producing "undefined reference to VerilatedCov::..." on
#    exactly the builds this guard was supposed to no-op on. Found by
#    bisecting a link failure that turned out to have nothing to do with
#    the Verilator version installed.
(
    cd "$ROOT"
    MY_TOP_CPP="piton/tools/verilator/my_top.cpp"
    if [ ! -f "$MY_TOP_CPP" ]; then
        echo "not found, skipping fix 5: $MY_TOP_CPP"
    else
        # Same Windows-checkout CRLF root cause as fix 4, a symptom fix 4
        # never caught since its own sweep only looks at *.py/*.sh shebang
        # lines -- my_top.cpp is a plain .cpp file with no shebang, so this
        # went uncaught until a line-anchored patch (below) needed exact
        # end-of-line matches. Harmless no-op on an already-LF file.
        #
        # Must run before the two anchored checks below, not only inside
        # the fresh-install branch: an already-patched file (either #if or
        # the old buggy #ifdef form) whose line endings get reintroduced to
        # CRLF -- e.g. a Windows-side tool re-saving it -- has a trailing
        # \r sitting before the $ end-anchor, so neither grep matches it,
        # and it was falling through to fresh-install and getting a second,
        # duplicate insertion stacked on the first.
        sed -i 's/\r$//' "$MY_TOP_CPP"

        # A full patch inserts the guard at TWO sites (the include block and
        # the exit path). A bare `grep -q` (at least one match) can't tell a
        # fully-patched file from one where execution was interrupted between
        # the two sites' sed calls (a real risk: this whole thing runs inside
        # GCP setup_commands, where OOM-kill / Ray-heartbeat-miss / spot-
        # preemption are documented, previously-hit failure modes) -- a retry
        # would see the one completed site, report "already patched", and
        # never add the missing one. Counting to exactly 2 catches that.
        vm_coverage_count=$(grep -c '^#if VM_COVERAGE$' "$MY_TOP_CPP" || true)
        vm_coverage_old_count=$(grep -c '^#ifdef VM_COVERAGE$' "$MY_TOP_CPP" || true)
        if [ "$vm_coverage_count" -eq 2 ]; then
            echo "already patched: $MY_TOP_CPP"
        elif [ "$vm_coverage_old_count" -eq 2 ]; then
            sed -i 's/^#ifdef VM_COVERAGE$/#if VM_COVERAGE/' "$MY_TOP_CPP"
            echo "patched: my_top.cpp's #ifdef VM_COVERAGE -> #if VM_COVERAGE (was always true, see fix 5 comment)"
        elif [ "$vm_coverage_count" -gt 0 ] || [ "$vm_coverage_old_count" -gt 0 ]; then
            # Neither 0 (untouched) nor 2 (fully patched, either form) --
            # looks like a prior run was interrupted mid-patch. Blindly
            # re-running the fresh-install branch below would insert a
            # second guard at whichever site already has one. Fail loud
            # instead of guessing; this state needs a human to look at it
            # (or restore my_top.cpp from a clean checkout and re-run).
            echo "my_top.cpp: found a partial VM_COVERAGE guard (if-count=$vm_coverage_count ifdef-count=$vm_coverage_old_count, expected 0 or 2 of one form) -- looks like an interrupted previous patch attempt, not auto-repairing" >&2
            exit 1
        else
            # `|| true` on both: grep -c exits 1 on a 0-match count (still
            # printing "0"), which under this script's own set -euo pipefail
            # would abort HERE, before the -ne 1 check two lines down ever
            # runs -- so the diagnostic it prints was only ever reachable for
            # the 2-plus-matches case, never the 0-matches case it was
            # equally written for. Found by bisecting a link failure that
            # turned out to have nothing to do with the Verilator version
            # installed (same investigation as fix 5 itself).
            inc_count=$(grep -c '^#include "verilated_vcd_c.h"$' "$MY_TOP_CPP" || true)
            exit_count=$(grep -c '^delete top;$' "$MY_TOP_CPP" || true)
            if [ "$inc_count" -ne 1 ] || [ "$exit_count" -ne 1 ]; then
                echo "my_top.cpp: expected exactly one match for each coverage-patch anchor, found inc=$inc_count exit=$exit_count -- not patching" >&2
                exit 1
            fi
            inc_line=$(grep -n '^#include "verilated_vcd_c.h"$' "$MY_TOP_CPP" | cut -d: -f1)
            exit_line=$(grep -n '^delete top;$' "$MY_TOP_CPP" | cut -d: -f1)

            # exit-path block first (higher line number) so its own insertion
            # doesn't shift the include-block's already-computed line number.
            # Each sed targets the same original line, inserted in reverse
            # desired order, so repeated single-line inserts (no fragile
            # multi-line sed escaping) stack into the right final order.
            sed -i "${exit_line}i #endif" "$MY_TOP_CPP"
            sed -i "${exit_line}i VerilatedCov::write(\"coverage.dat\");" "$MY_TOP_CPP"
            sed -i "${exit_line}i #if VM_COVERAGE" "$MY_TOP_CPP"

            inc_endif_line=$((inc_line + 1))
            sed -i "${inc_endif_line}a #endif" "$MY_TOP_CPP"
            sed -i "${inc_endif_line}a #include \"verilated_cov.h\"" "$MY_TOP_CPP"
            sed -i "${inc_endif_line}a #if VM_COVERAGE" "$MY_TOP_CPP"

            echo "patched: my_top.cpp writes coverage.dat when VM_COVERAGE is nonzero"
        fi
    fi
)

# 6. PicoRV32's own internal `resetn` gate (distinct from the tile-wide
#    reset_l every core shares) only ever turns on via an L15 interrupt
#    (pico_int) -- there is no path to boot on a plain reset. Confirmed via
#    a real Verilator waveform trace (my_top.vcd): reset_l deasserts right
#    on schedule, but resetn/pico_int/mem_valid never move again for the
#    rest of the run and reg_pc stays pinned at PROGADDR_RESET -- the core
#    issues zero memory transactions after boot, exactly matching the
#    previously-observed "generic IOB handshake completes, then silence to
#    maxcycles" signature. Nothing in a bare OpenPiton config (no diag, no
#    testbench code) ever sends that interrupt, so pico can never start.
#
#    A new `booted` register distinguishes "just came out of the tile's own
#    reset, boot for the first time" from "voluntarily asleep, waiting for a
#    real wake interrupt" (the write-to-0xffffffff path this fork clearly
#    added on purpose) -- a bare, unconditional `else resetn <= 1'b1` would
#    also un-sleep the core one cycle after every voluntary sleep write,
#    breaking that mechanism entirely. python3 (not sed) for this one: it's
#    a multi-line structural block, and an exact-match-count replace is
#    safer here than chaining several line-number-dependent sed inserts.
(
    cd "$ROOT"
    PICO_RTL="piton/design/chip/tile/pico/rtl/picorv32.v"
    if [ ! -f "$PICO_RTL" ]; then
        echo "not found, skipping fix 6: $PICO_RTL"
    else
        if grep -q "pico_int || !booted" "$PICO_RTL"; then
            echo "already patched: $PICO_RTL"
        else
            python3 - "$PICO_RTL" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()
old = """    reg        resetn;

    always @ (posedge clk) begin
        if(!reset_l) begin
            resetn <= 1'b0;
        end
        else if (mem_la_write & (mem_la_addr == 32'hffffffff)) begin
            resetn <= 1'b0;
        end
        else if (pico_int) begin
            resetn <= 1'b1;
        end
    end"""
new = """    reg        resetn;
    reg        booted;

    always @ (posedge clk) begin
        if(!reset_l) begin
            resetn <= 1'b0;
            booted <= 1'b0;
        end
        else if (mem_la_write & (mem_la_addr == 32'hffffffff)) begin
            resetn <= 1'b0;
        end
        else if (pico_int || !booted) begin
            resetn <= 1'b1;
            booted <= 1'b1;
        end
    end"""
count = content.count(old)
if count != 1:
    print(f"ERROR: expected exactly 1 match for the resetn block, found {count}", file=sys.stderr)
    sys.exit(1)
content = content.replace(old, new)
with open(path, "w") as f:
    f.write(content)
print("patched: picorv32.v's resetn now self-boots once, still sleep/wake-able via pico_int")
PYEOF
        fi
    fi
)

# 7. pc_cmp.v's RTL_PICO0 active_thread tracking never turns on: unlike
#    RTL_ARIANE0's own equivalent block (a clocked always @(posedge clk)
#    that unconditionally asserts active_thread once out of reset, exactly
#    matching how spc0_inst_done/spc0_phy_pc_w are ALREADY wired for pico
#    right below this block, unconditionally, off PICO_CORE0.launch_next_insn/
#    reg_pc), pico's active_thread block is a combinational latch gated on
#    `PICO_CORE0.pico_int == 1'b1` -- the same L15 wakeup interrupt fix 6
#    already established is never sent in a bare config. With fix 6 applied,
#    the core genuinely boots and runs (confirmed: reaches its own
#    configured good_trap PC, per a real waveform/sim.log trace), but the
#    monitor's own "is this thread active" bit for pico never turns on, so
#    the good/bad-trap detection that gates on active_thread never fires --
#    the simulation just runs to maxcycles regardless of what the core
#    itself actually does. Matches RTL_ARIANE0's own clocked, unconditional
#    pattern (same rst_l reset signal already in scope) rather than
#    inventing a new mechanism.
(
    cd "$ROOT"
    PC_CMP="piton/verif/env/manycore/pc_cmp.v.pyv"
    if [ ! -f "$PC_CMP" ]; then
        echo "not found, skipping fix 7: $PC_CMP"
    else
        # PICO_CORE0.pico_int is unique to the old, buggy pico block --
        # Ariane's own equivalent block (which legitimately contains the
        # same active_thread <= 1'b0/1'b1 lines this patch introduces for
        # pico too) never mentions PICO_CORE0, so checking for its absence
        # -- not for the presence of lines the two blocks now share -- is
        # what actually distinguishes patched from unpatched here.
        if ! grep -q "PICO_CORE0.pico_int" "$PC_CMP"; then
            echo "already patched: $PC_CMP"
        else
            python3 - "$PC_CMP" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()
old = """                always @*
                begin
                    if (`PICO_CORE0.pico_int == 1'b1)
                    begin
                        active_thread[(0*4)] = 1'b1;
                        active_thread[(0*4)+1] = 1'b1;
                        active_thread[(0*4)+2] = 1'b1;
                        active_thread[(0*4)+3] = 1'b1;
                    end
                end"""
new = """                always @(posedge clk) begin
                    if (~rst_l) begin
                      active_thread[(0*4)]   <= 1'b0;
                      active_thread[(0*4)+1] <= 1'b0;
                      active_thread[(0*4)+2] <= 1'b0;
                      active_thread[(0*4)+3] <= 1'b0;
                    end else begin
                      active_thread[(0*4)]   <= 1'b1;
                      active_thread[(0*4)+1] <= 1'b1;
                      active_thread[(0*4)+2] <= 1'b1;
                      active_thread[(0*4)+3] <= 1'b1;
                    end
                end"""
count = content.count(old)
if count != 1:
    print(f"ERROR: expected exactly 1 match for pico's active_thread block, found {count}", file=sys.stderr)
    sys.exit(1)
content = content.replace(old, new)
with open(path, "w") as f:
    f.write(content)
print("patched: pc_cmp.v.pyv's RTL_PICO0 active_thread now tracks unconditionally, matching RTL_ARIANE0's own pattern")
PYEOF
        fi
    fi
)


# 8. sims' -vlt_build/-vlt_run hardcode "cmp_top"/"Vcmp_top" in three places,
#    completely ignoring -toplevel= from the sys's own .config (confirmed:
#    piton/tools/src/sims/ifu_esl_lfsr.config sets -toplevel=ifu_esl_lfsr_top,
#    and VCS/ICV's own build paths already honor $opt{toplevel} -- only the
#    free/open Verilator path never did). Without this, `sims -sys=<any
#    non-manycore env> -vlt_build` always tries to elaborate a "cmp_top"
#    module that doesn't exist in that sys's own flist and fails immediately
#    -- meaning none of OpenPiton's own unit-test environments (piton/verif/
#    env/*, registered via piton/tools/src/sims/*.config) have ever actually
#    been built under Verilator, by anyone, only under commercial simulators.
#
#    Separately, my_top.cpp (the C++ driver -vlt_build links against) is
#    itself hardcoded to a manycore Vcmp_top instantiation with real
#    simulation work (JBUS/DRAM model init, IOB/reset sequencing) that a
#    standalone module has no use for -- piton/tools/verilator/unit_top.cpp
#    is a new, minimal, generic driver for this case (these testbenches are
#    fully self-contained Verilog, TEST_INFRSTRCT_BEGIN/END drives its own
#    clk/rst_n and calls $finish itself, so the C++ side has nothing
#    DUT-specific to do), parameterized via one -CFLAGS define naming the
#    generated toplevel class -- see that file's own header comment for why
#    it's a bare identifier rather than an already-quoted string (the latter
#    doesn't survive sims' own system() call, Verilator's Makefile
#    generation, and make's own recipe shell intact).
(
    cd "$ROOT"
    SIMS_PL="piton/tools/src/sims/sims,2.0"
    UNIT_TOP_CPP="piton/tools/verilator/unit_top.cpp"
    if [ ! -f "$SIMS_PL" ]; then
        echo "not found, skipping fix 8: $SIMS_PL"
    else
        if grep -q "MACE_UNIT_TOP" "$SIMS_PL"; then
            echo "already patched: $SIMS_PL"
        else
            python3 - "$SIMS_PL" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

old_a = """    if ($opt{vlt_build}) {
      $build_cmd = "verilator -cc " ;
      $build_cmd .= "-exe $dv_root/tools/verilator/my_top.cpp " ;
      $build_cmd .= "$dv_root/tools/pli/iop/b_ary.c " ;
      $build_cmd .= "$dv_root/tools/pli/iop/bw_lib.c " ;
      $build_cmd .= "$dv_root/tools/pli/iop/iob_main.cc " ;
      $build_cmd .= "$dv_root/tools/pli/iop/iob.cc " ;
      $build_cmd .= "$dv_root/tools/pli/iop/cpx.cc " ;
      $build_cmd .= "$dv_root/tools/pli/iop/pcx.cc " ;
      $build_cmd .= "--top-module cmp_top " ;
      $build_cmd .= "-Wno-fatal " ;"""
new_a = """    if ($opt{vlt_build}) {
      my $is_manycore_sys = ($opt{sys} eq "manycore") ;
      my $vlt_top = $is_manycore_sys ? "cmp_top" : $opt{toplevel}[0] ;
      $build_cmd = "verilator -cc " ;
      if ($is_manycore_sys) {
        # manycore's own driver: owns real simulation work (JBUS/DRAM model
        # init, IOB/reset sequencing) that a standalone module doesn't have
        # or need.
        $build_cmd .= "-exe $dv_root/tools/verilator/my_top.cpp " ;
        $build_cmd .= "$dv_root/tools/pli/iop/b_ary.c " ;
        $build_cmd .= "$dv_root/tools/pli/iop/bw_lib.c " ;
        $build_cmd .= "$dv_root/tools/pli/iop/iob_main.cc " ;
        $build_cmd .= "$dv_root/tools/pli/iop/iob.cc " ;
        $build_cmd .= "$dv_root/tools/pli/iop/cpx.cc " ;
        $build_cmd .= "$dv_root/tools/pli/iop/pcx.cc " ;
      } else {
        # A unit-test sys's own testbench (TEST_INFRSTRCT_BEGIN/END, see
        # piton/verif/env/test_infrstrct/test_infrstrct.v) generates and
        # drives its own clk/rst_n and calls $finish itself -- nothing
        # DUT-specific for the C++ side to do, so a small generic driver
        # (piton/tools/verilator/unit_top.cpp) replaces my_top.cpp and its
        # manycore-only JBUS/IOB/CPX/PCX C++ model files entirely.
        $build_cmd .= "-exe $dv_root/tools/verilator/unit_top.cpp " ;
        $build_cmd .= "-CFLAGS -DMACE_UNIT_TOP=V${vlt_top} " ;
      }
      $build_cmd .= "--top-module $vlt_top " ;
      $build_cmd .= "-Wno-fatal " ;"""
count_a = content.count(old_a)
if count_a != 1:
    print(f"ERROR: expected exactly 1 match for the vlt_build command block, found {count_a}", file=sys.stderr)
    sys.exit(1)
content = content.replace(old_a, new_a)

old_b = """    if ($opt{vlt_build}) {
      $build_cmd = "make -j -C $model_path/obj_dir -f Vcmp_top.mk Vcmp_top" ;"""
new_b = """    if ($opt{vlt_build}) {
      my $vlt_top = @{$opt{toplevel}} ? $opt{toplevel}[0] : "cmp_top" ;
      $build_cmd = "make -j -C $model_path/obj_dir -f V${vlt_top}.mk V${vlt_top}" ;"""
count_b = content.count(old_b)
if count_b != 1:
    print(f"ERROR: expected exactly 1 match for the vlt_build make block, found {count_b}", file=sys.stderr)
    sys.exit(1)
content = content.replace(old_b, new_b)

old_c = """    if ($opt{vlt_run}) {
      $cmd .= "$model_path/obj_dir/Vcmp_top " ;"""
new_c = """    if ($opt{vlt_run}) {
      $cmd .= "$model_path/obj_dir/V" . (@{$opt{toplevel}} ? $opt{toplevel}[0] : "cmp_top") . " " ;"""
count_c = content.count(old_c)
if count_c != 1:
    print(f"ERROR: expected exactly 1 match for the vlt_run block, found {count_c}", file=sys.stderr)
    sys.exit(1)
content = content.replace(old_c, new_c)

with open(path, "w") as f:
    f.write(content)
print("patched: sims,2.0's vlt_build/vlt_run now honor -toplevel= instead of always hardcoding cmp_top")
PYEOF
        fi

        if [ ! -f "$UNIT_TOP_CPP" ]; then
            cat > "$UNIT_TOP_CPP" <<'CPPEOF'
// Generic Verilator C++ driver for OpenPiton's non-manycore -sys= unit-test
// environments (piton/tools/src/sims/<sys>.config, piton/verif/env/<sys>/),
// e.g. ifu_esl_lfsr: a single module built alone against the reusable
// test_infrstrct stimulus/check harness (piton/verif/env/test_infrstrct/).
//
// Unlike my_top.cpp (the manycore driver, which owns real simulation work:
// JBUS/DRAM model init, IOB/reset sequencing, VCD/coverage wiring), these
// testbenches are fully self-contained Verilog -- TEST_INFRSTRCT_BEGIN
// generates its own clk/rst_n and drives them, TEST_INFRSTRCT_END/TEST_CHECK
// call $finish -- so the C++ side has nothing DUT-specific to do. The one
// thing that varies per environment is the toplevel class Verilator
// generates, so this file is parameterized via a single -CFLAGS define:
//   -CFLAGS -DMACE_UNIT_TOP=Vfoo_top   (the generated class name, a bare
//                                        identifier -- no quotes, so it
//                                        survives sims' own system() call,
//                                        Verilator's Makefile generation and
//                                        make's own recipe shell unescaped).
// The #include filename ("Vfoo_top.h") is built from that same identifier
// via the standard stringify-after-expand trick, rather than trying to pass
// an already-quoted string through three nested layers of shell parsing.
#define MACE_STR(x) #x
#define MACE_XSTR(x) MACE_STR(x)
#include MACE_XSTR(MACE_UNIT_TOP.h)
#include "verilated.h"
#ifdef VERILATOR_VCD
#include "verilated_vcd_c.h"
#endif

int main(int argc, char **argv) {
    VerilatedContext *contextp = new VerilatedContext;
    contextp->commandArgs(argc, argv);
    MACE_UNIT_TOP *top = new MACE_UNIT_TOP{contextp};

#ifdef VERILATOR_VCD
    Verilated::traceEverOn(true);
    VerilatedVcdC *tfp = new VerilatedVcdC;
    top->trace(tfp, 99);
    tfp->open("unit_top.vcd");
#endif

    while (!contextp->gotFinish()) {
        top->eval();
#ifdef VERILATOR_VCD
        tfp->dump(contextp->time());
#endif
        contextp->timeInc(1);
    }

#ifdef VERILATOR_VCD
    tfp->close();
#endif
    top->final();
    delete top;
    delete contextp;
    return 0;
}
CPPEOF
            echo "created: $UNIT_TOP_CPP"
        else
            echo "already exists: $UNIT_TOP_CPP"
        fi
    fi
)

# 9. sims' own vlt_build step invokes a bare "make -j" (unlimited parallel
#    jobs) to compile Verilator's generated C++, with no number after -j --
#    a command-line "-j" always overrides MAKEFLAGS from the environment, no
#    matter what it's set to, so a caller's own MAKEFLAGS=-j1 (set to avoid
#    exactly the OOM this causes: many parallel cc1plus processes, each
#    100MB-850MB, for a large multi-tile design) gets silently ignored. This
#    is the confirmed cause of a real Ray-reported OOM (22GB+/23GB used)
#    during a 4x4 Ariane build. Drop the bare -j so this make invocation
#    falls back to ordinary GNU Make behavior: parallel only when MAKEFLAGS
#    from the environment actually asks for it, serial otherwise.
(
    cd "$ROOT"
    SIMS_PL="piton/tools/src/sims/sims,2.0"
    if [ ! -f "$SIMS_PL" ]; then
        echo "not found, skipping fix 9: $SIMS_PL"
    elif grep -q 'build_cmd = "make -C \$model_path' "$SIMS_PL"; then
        echo "already patched: $SIMS_PL (fix 9)"
    else
        python3 - "$SIMS_PL" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()

old = '''      $build_cmd = "make -j -C $model_path/obj_dir -f V${vlt_top}.mk V${vlt_top}" ;'''
new = '''      $build_cmd = "make -C $model_path/obj_dir -f V${vlt_top}.mk V${vlt_top}" ;'''
count = content.count(old)
if count != 1:
    print(f"ERROR: expected exactly 1 match for the vlt_build make -j line, found {count}", file=sys.stderr)
    sys.exit(1)
content = content.replace(old, new)
with open(path, "w") as f:
    f.write(content)
print("patched: sims,2.0's vlt_build make step no longer hardcodes bare -j, MAKEFLAGS now actually controls it")
PYEOF
    fi
)

# 10. pc_cmp.v declares finish_mask as a Verilog "integer" (always exactly 32
#     bits) under Verilator only, while its siblings active_thread/good are
#     "reg [31:0]" that this template's own replace("31", ...) widens to
#     4 bits per tile. So under Verilator the finish mask silently truncates
#     past 8 tiles: a 4x4 Ariane run reported PASS after only 8 of its 16
#     tiles finished (confirmed per tile from each core's own trace).
#     Declaring it "reg [31:0]" everywhere lets the template widen it too.
#     Deliberately no Verilog comment added here: a backtick inside one
#     broke Verilator's parse of the generated file.
(
    cd "$ROOT"
    PC_CMP="piton/verif/env/manycore/pc_cmp.v.pyv"
    if [ ! -f "$PC_CMP" ]; then
        echo "not found, skipping fix 10: $PC_CMP"
    elif ! grep -q "integer      finish_mask;" "$PC_CMP"; then
        echo "already patched: $PC_CMP (fix 10)"
    else
        python3 - "$PC_CMP" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()
old = """    `ifndef VERILATOR
    reg [31:0]   finish_mask;
    `else
    integer      finish_mask;
    `endif
"""
new = """    reg [31:0]   finish_mask;
"""
count = content.count(old)
if count != 1:
    print(f"ERROR: expected exactly 1 match for the finish_mask declaration, found {count}", file=sys.stderr)
    sys.exit(1)
content = content.replace(old, new)
with open(path, "w") as f:
    f.write(content)
print("patched: pc_cmp.v.pyv's finish_mask is now reg [31:0], widened per tile like active_thread")
PYEOF
    fi
)

# 11. Ariane's shared syscalls.c (linked into every ariane C diagnostic)
#     polls its multi-hart exit barrier (finish_sync0/finish_sync1) with
#     plain loads. That is the same staleness bug mace/workloads/barrier_atomic.c
#     works around with atomic_read(): a plain load does not reliably observe
#     another tile's atomic update, so on any multi-tile mesh every hart but
#     the last spins forever (a 1x1 mesh hides it, since nc=1). Poll through
#     an atomic fetch-add-zero instead.
(
    cd "$ROOT"
    SYSCALLS="piton/verif/diag/assembly/include/riscv/ariane/syscalls.c"
    if [ ! -f "$SYSCALLS" ]; then
        echo "not found, skipping fix 11: $SYSCALLS"
    elif ! grep -q "while(finish_sync0 != nc);" "$SYSCALLS"; then
        echo "already patched: $SYSCALLS (fix 11)"
    else
        python3 - "$SYSCALLS" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()
swaps = [
    ("  while(finish_sync0 != nc);",
     "  { uint32_t v; do { ATOMIC_FETCH_OP(v, finish_sync0, 0, add, w); } while (v != nc); }"),
    ("  while(finish_sync1 != cid);",
     "  { uint32_t v; do { ATOMIC_FETCH_OP(v, finish_sync1, 0, add, w); } while (v != cid); }"),
]
for old, new in swaps:
    count = content.count(old)
    if count != 1:
        print(f"ERROR: expected exactly 1 match for {old.strip()!r}, found {count}", file=sys.stderr)
        sys.exit(1)
    content = content.replace(old, new)
with open(path, "w") as f:
    f.write(content)
print("patched: syscalls.c's exit barrier now polls through atomic fetch-add-zero")
PYEOF
    fi
)

# 12. CVA6's Verilator instruction tracer opens a hardcoded
#     "trace_hart_00.dasm" regardless of hart_id_i, so every tile of a
#     multi-tile Ariane build truncates and shares one file, and every tile
#     but one looks like it never booted. Name the file per hart, the way
#     instr_tracer.sv already does. Lives in the ariane submodule.
(
    cd "$ROOT"
    CVA6_SV="piton/design/chip/tile/ariane/core/cva6.sv"
    if [ ! -f "$CVA6_SV" ]; then
        echo "not found, skipping fix 12 (ariane submodule not initialized?): $CVA6_SV"
    elif ! grep -q 'f = $fopen("trace_hart_00.dasm", "w");' "$CVA6_SV"; then
        echo "already patched: $CVA6_SV (fix 12)"
    else
        python3 - "$CVA6_SV" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path) as f:
    content = f.read()
old = """    f = $fopen("trace_hart_00.dasm", "w");"""
new = """    string dasm_fn;
    $sformat(dasm_fn, "trace_hart_%0.0f.dasm", hart_id_i);
    f = $fopen(dasm_fn, "w");"""
count = content.count(old)
if count != 1:
    print(f"ERROR: expected exactly 1 match for the trace_hart_00.dasm open, found {count}", file=sys.stderr)
    sys.exit(1)
content = content.replace(old, new)
with open(path, "w") as f:
    f.write(content)
print("patched: cva6.sv's tracer now writes trace_hart_<hart_id>.dasm per tile")
PYEOF
    fi
)

# Addition (not a bug fix): pico_reset_ut, a real, standalone unit test for
# picorv32.v's self-boot behavior (finding 6) -- proves finding 8's -sys=
# generalization end to end by authoring a genuinely NEW unit-test
# environment, not just running an OpenPiton-provided one (ifu_esl_lfsr).
# Drives clk/reset_l/pico_int directly against the real DUT (no manycore
# boot, no L15/L2/memory-model chain) and checks mem_valid && mem_addr ==
# PROGADDR_RESET shortly after reset -- exactly the behavior finding 6
# fixed, and exactly what a full-manycore run can only observe indirectly.
#
# Known limitation, not fixed here: actually RUNNING this (or any
# non-manycore sys's testbench, including OpenPiton's own ifu_esl_lfsr) hits
# a real, pre-existing Verilator incompatibility in the shared
# piton/verif/env/test_infrstrct/test_infrstrct.v harness -- three of its
# macros use `always @*` blocks containing real time delays (#10000, #500,
# #2500), a pattern VCS/Questa tolerate but Verilator's combinational-settle
# algorithm does not ("Settle region did not converge", a hard abort before
# any useful simulation happens). A fix was attempted (switching to explicit
# `always @(posedge clk or test_case_num)` sensitivity) and reverted after
# real testing showed it introduces a re-entrancy race (a #delay-containing
# block re-triggered by every clock edge during its own pending delay spawns
# concurrent invocations) -- worse than the original bug, not better. Fixing
# this correctly needs a real redesign of that shared sequencing mechanism,
# not a quick patch; left for a dedicated follow-up rather than risking a
# subtly broken shared test harness. The BUILD side of this addition is
# real and verified (a genuine Verilator build of this new environment
# succeeds); the RUN side is blocked on this same pre-existing gap.
(
    cd "$ROOT"
    UT_DIR="piton/verif/env/pico_reset_ut"
    UT_TOP="$UT_DIR/pico_reset_ut_top.v"
    UT_FLIST="$UT_DIR/pico_reset_ut.flist"
    UT_CONFIG="piton/tools/src/sims/pico_reset_ut.config"
    SIMS_CONFIG="piton/tools/src/sims/sims.config"
    if [ -f "$UT_TOP" ]; then
        echo "already exists: $UT_TOP"
    else
        mkdir -p "$UT_DIR/test_cases"
        cat > "$UT_TOP" <<'VEOF'
/*
 * Unit test for picorv32.v's self-boot behavior (chia_openpiton /
 * scripts/patch_openpiton.sh finding 6): the core must issue its first
 * real memory fetch (mem_valid && mem_addr == PROGADDR_RESET) shortly
 * after reset_l deasserts, WITHOUT ever depending on pico_int -- tied 0
 * for the whole run here, which is exactly the bug this project fixed
 * (the core used to wait forever for an L15 wakeup interrupt nothing in
 * a bare config ever sends).
 *
 * Deliberately does not respond to the fetch (mem_ready tied 0): the
 * assertion under test is "does the core attempt its first fetch on its
 * own", not "does a full memory transaction complete" -- decoupling this
 * from the L15/L2/memory-model chain keeps the test fast and focused on
 * the one behavior finding 6 actually changed.
 */

`include "test_infrstrct.v"
`include "l15.tmp.h"   // defines L15_AMO_OP_WIDTH, used below -- included
                        // directly rather than relying on picorv32.v's own
                        // include being processed first by flist order

`define VERBOSITY 1

module pico_reset_ut_top;

    `TEST_INFRSTRCT_BEGIN("pico_reset_ut")

    wire        mem_valid;
    wire        mem_instr;
    wire [31:0] mem_addr;
    wire [31:0] mem_wdata;
    wire [ 3:0] mem_wstrb;
    wire [`L15_AMO_OP_WIDTH-1:0] mem_amo_op;
    wire        mem_la_read, mem_la_write;
    wire [31:0] mem_la_addr;
    wire [31:0] mem_la_wdata;
    wire [ 3:0] mem_la_wstrb;
    wire [`L15_AMO_OP_WIDTH-1:0] mem_la_amo_op;
    wire        pcpi_valid;
    wire [31:0] pcpi_insn;
    wire [31:0] pcpi_rs1, pcpi_rs2;
    wire [31:0] eoi;
    wire        trap;
    wire        trace_valid;
    wire [35:0] trace_data;

    // Real reset vector for this checkout's non-FPGA-synth build
    // (picorv32.v's own PROGADDR_RESET default under `else`).
    localparam [31:0] PROGADDR_RESET = 32'h4000_0000;

    picorv32 dut (
        .clk       (clk),
        .reset_l   (rst_n),
        .trap      (trap),

        .mem_valid (mem_valid),
        .mem_instr (mem_instr),
        .mem_ready (1'b0),        // never responds -- see file header

        .mem_addr  (mem_addr),
        .mem_wdata (mem_wdata),
        .mem_wstrb (mem_wstrb),
        .mem_amo_op(mem_amo_op),
        .mem_rdata (32'b0),

        .pico_int  (1'b0),        // the exact condition finding 6 fixes

        .mem_la_read (mem_la_read),
        .mem_la_write(mem_la_write),
        .mem_la_addr (mem_la_addr),
        .mem_la_wdata(mem_la_wdata),
        .mem_la_wstrb(mem_la_wstrb),
        .mem_la_amo_op(mem_la_amo_op),

        .pcpi_valid(pcpi_valid),
        .pcpi_insn (pcpi_insn),
        .pcpi_rs1  (pcpi_rs1),
        .pcpi_rs2  (pcpi_rs2),
        .pcpi_wr   (1'b0),
        .pcpi_rd   (32'b0),
        .pcpi_wait (1'b0),
        .pcpi_ready(1'b0),

        .irq (32'b0),
        .eoi (eoi),

        .trace_valid(trace_valid),
        .trace_data (trace_data)
    );

    `TEST_CASE_BEGIN(1, "self_boot_no_interrupt")
    begin
        `TEST_CASE_RESET

        // A handful of cycles is generous: finding 6's fix asserts resetn
        // (and therefore the first fetch) one cycle after reset_l
        // deasserts. Before the fix this never happened at all, so this
        // check would still be false at any cycle count, not just a tight
        // one -- the margin here is about robustness, not tuning against
        // the exact timing.
        #10000
        `TEST_CHECK("Core self-boots without pico_int",
                     mem_valid && (mem_addr == PROGADDR_RESET), `VERBOSITY)
    end
    `TEST_CASE_END

    `TEST_INFRSTRCT_END(1)

endmodule
VEOF
        cat > "$UT_FLIST" <<'FEOF'
// Flist for pico_reset_ut testbench environment

pico_reset_ut_top.v
FEOF
        cat > "$UT_CONFIG" <<'CEOF'
// Testbench configuration for pico_reset_ut: a real, standalone unit test
// for picorv32.v's self-boot behavior (scripts/patch_openpiton.sh finding
// 6), built alone against the reusable test_infrstrct harness -- no
// manycore boot required. See piton/verif/env/pico_reset_ut/ for the
// testbench itself.

<pico_reset_ut>
    -model=pico_reset_ut
    -toplevel=pico_reset_ut_top
    -flist=$DV_ROOT/design/include/Flist.include
    -flist=$DV_ROOT/design/chip/tile/pico/rtl/Flist.pico
    -flist=$DV_ROOT/verif/env/pico_reset_ut/pico_reset_ut.flist
    -flist=$DV_ROOT/verif/env/test_infrstrct/test_infrstrct_include.flist
    -sim_build_args=+incdir+$DV_ROOT/verif/env/test_infrstrct/
    -sim_build_args=+incdir+$DV_ROOT/design/include/
    -sim_run_args=+test_cases_path=$DV_ROOT/verif/env/pico_reset_ut/test_cases/
</pico_reset_ut>
CEOF
        echo "created: $UT_TOP, $UT_FLIST, $UT_CONFIG"
    fi

    if [ -f "$SIMS_CONFIG" ] && ! grep -q 'pico_reset_ut.config' "$SIMS_CONFIG"; then
        echo '#include "pico_reset_ut.config"' >> "$SIMS_CONFIG"
        echo "patched: registered pico_reset_ut.config in sims.config"
    fi
)
