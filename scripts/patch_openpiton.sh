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
#    for instead of editing the sources.
#
#    This one hides: the bootrom's `clean` target removes only the image and
#    the DTB, never the .o files, so a stale main.o masks the failure until
#    something invalidates it (a fresh checkout, a new worker, the image).
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
      my $is_manycore_sys = ! @{$opt{toplevel}} ;
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
