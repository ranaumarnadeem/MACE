{
  description = "MACE: Multi-core Agentic Co-design Engine -- CHIA support for OpenPiton";

  inputs = {
    # Pinned to an exact commit, not a floating channel -- the whole point
    # of packaging this with Nix is reproducibility, which a floating
    # nixpkgs-unstable reference would defeat. Re-pin deliberately, not by
    # accident, if this ever needs to move.
    #
    # The tarball/archive form, not github:NixOS/nixpkgs/<hash> -- that form
    # fetches nixpkgs' entire git history through Nix's git cache, which is
    # real, and really slow (confirmed directly: 25+ minutes, still not
    # done). The archive URL fetches a compressed source snapshot at the
    # exact same commit instead -- identical pin, a fraction of the time.
    #
    # Pinned to nixos-24.11's own exact commit, not a newer/unstable one:
    # confirmed directly that nixpkgs-unstable as of 2026-09 has already
    # dropped python310 entirely (pyproject.toml's own requires-python is
    # ">=3.10,<3.11", and this project has only ever run against 3.10.19 --
    # not something to casually widen as a side effect of packaging). This
    # revision has python310 (3.10.16, close to what's validated) and a
    # real tagged verilator release (5.028, not a "devel" snapshot -- see
    # the comment on the verilator package below for why that distinction
    # is a real, previously-hit bug in this project, not a hypothetical).
    nixpkgs.url = "https://github.com/NixOS/nixpkgs/archive/50ab793786d9de88ee30ec4e4c24fb4236fc2674.tar.gz";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };

        # The exact prebuilt RISC-V toolchain this project already validated
        # against real hardware (cluster/local.yaml's own GCP worker
        # setup_commands, scripts/patch_openpiton.sh) -- fetched directly
        # rather than built from nixpkgs' own cross-compilation sources, so
        # this stays byte-identical to what every real acceptance test in
        # this project has actually run against. autoPatchelfHook rewrites
        # the prebuilt (Ubuntu-built, FHS-assuming) binaries' dynamic loader/
        # rpath to real Nix store paths -- without it they only work by
        # accident, on a host that happens to also have a normal /lib.
        riscvToolchain = pkgs.stdenv.mkDerivation {
          pname = "riscv64-elf-gcc-prebuilt";
          version = "2026.08.27";
          src = pkgs.fetchurl {
            url = "https://github.com/riscv-collab/riscv-gnu-toolchain/releases/download/2026.08.27/riscv64-elf-ubuntu-24.04-gcc.tar.xz";
            # This hash is Nix's own, from a real, complete, verified fetch
            # of this exact URL -- not the one first computed here, which
            # came from a `curl` download that had actually been silently
            # truncated by a timeout (the file *looked* complete by size,
            # but never had its own completion confirmed before the hash
            # was taken -- a real mistake, caught by Nix's own fixed-output
            # hash check refusing to accept it, which is exactly what that
            # check is for).
            sha256 = "sha256-/n2t+Z367lmFW0vl+NSR3GZZO+wpUJDhVaPsUfDRT1Y=";
          };
          nativeBuildInputs = [ pkgs.autoPatchelfHook ];
          # gmp/mpfr/libmpc/zstd: GCC's own arbitrary-precision math and
          # compression deps -- real, found by actually running
          # autoPatchelf, not anticipated up front.
          buildInputs = [
            pkgs.stdenv.cc.cc.lib pkgs.zlib pkgs.ncurses pkgs.expat pkgs.python310
            pkgs.zstd pkgs.gmp pkgs.mpfr pkgs.libmpc
          ];
          # The only remaining unmet deps, also found by running this for
          # real, are for two bundled extras this project has never once
          # invoked: qemu-riscv64/32 (needs glib/gmodule -- simulation here
          # has only ever gone through Verilator, never QEMU) and the
          # bundled riscv64-unknown-elf-gdb (needs libpython3.12, a version
          # mismatch with this project's own pinned 3.10 -- debugging here
          # has only ever gone through sim.log/status.log/trace_hart dumps,
          # never an interactive gdb session). Named explicitly, not a
          # blanket ignore, so a real future dependency gap still fails loud.
          autoPatchelfIgnoreMissingDeps = [
            "libglib-2.0.so.0" "libgmodule-2.0.so.0" "libpython3.12.so.1.0"
          ];
          dontConfigure = true;
          dontBuild = true;
          installPhase = ''
            mkdir -p $out
            cp -r . $out/
          '';
        };
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            # Python + the two real PyPI deps mace declares (typer, rich) --
            # chia and mace itself install editable via pip inside the venv
            # below, same as every non-Nix install this project has ever
            # used (chia is intentionally not a Nix/PyPI package -- see
            # pyproject.toml's own comment on why: not on PyPI at the pinned
            # revision this project builds against).
            python310
            python310Packages.pip
            python310Packages.virtualenv

            # OpenPiton/Verilator toolchain -- the exact apt package list
            # cluster/local.yaml's own GCP worker setup_commands installs,
            # mapped to nixpkgs equivalents.
            verilator       # a tagged release here, not a "devel" snapshot --
                            # a real finding this project hit: a devel
                            # Verilator build's own verilator_coverage was
                            # flatly broken (faulted on --version alone).
                            # Pinning a real release through Nix is exactly
                            # the kind of toolchain drift this is meant to
                            # prevent, not a hypothetical concern.
            gawk
            gnumake
            bison
            flex
            texinfo
            python310Packages.pexpect
            libusb1
            jdk
            zlib
            valgrind
            tcsh            # csh-compatible; OpenPiton's own scripts assume csh
            dtc             # device-tree-compiler
            perl
            perlPackages.BitVector
            libelf
            dos2unix
            openssh
            rsync
            git
            cacert
            xz

            riscvToolchain
          ];

          shellHook = ''
            export RISCV=${riscvToolchain}
            export PATH="$RISCV/bin:$PATH"

            if [ ! -d .venv ]; then
              echo "Creating .venv (first run)..."
              python3 -m venv .venv
              source .venv/bin/activate
              pip install --quiet --upgrade pip
              echo "venv ready. Install chia + mace editable yourself:"
              echo "  pip install -e /path/to/chia   # see README for the pinned commit"
              echo "  pip install -e '.[test]'"
            else
              source .venv/bin/activate
            fi

            echo ""
            echo "MACE dev shell ready."
            echo "  Verilator: $(verilator --version 2>&1 | head -1)"
            echo "  RISCV:     $RISCV"
          '';
        };
      });
}
