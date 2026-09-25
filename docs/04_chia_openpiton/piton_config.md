% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# PitonConfig

`PitonConfig`, in `chia_openpiton/state_def.py`, describes one OpenPiton configuration and names the model directory its build lives in. It is a frozen dataclass: assigning to a field raises `dataclasses.FrozenInstanceError`. [configure()](workspace_node.md) returns a `PitonConfig` with the checkout's revisions filled in. Code can also construct one directly, as MACE's loop does for each task.

## Fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `sys` | `str` | `"manycore"` | `sims -sys=` value. `"manycore"` is the full-chip mesh; any other name selects a registered OpenPiton unit-test environment, `piton/tools/src/sims/<sys>.config` |
| `core` | `PitonCore` | `"ariane"` | `"ariane"`, `"sparc"`, or `"pico"` |
| `x_tiles` | `int` | `1` | Mesh width |
| `y_tiles` | `int` | `1` | Mesh height |
| `network_config` | `NetworkConfig` | `"2dmesh_config"` | `"2dmesh_config"` or `"xbar_config"` |
| `config_rtl` | `tuple[str, ...]` | `("MINIMAL_MONITORING",)` | RTL define names |
| `caches` | `dict[str, tuple[int, int]]` | A copy of `DEFAULT_CACHES` | Cache name to `(size_bytes, associativity)` |
| `extra_flags` | `tuple[str, ...]` | `()` | Further `sims` flags, appended verbatim |
| `source_rev` | `str` | `""` | Checkout commit, set by `configure()` |
| `ariane_rev` | `str` | `""` | Ariane submodule commit, set by `configure()` |
| `verilator_version` | `str` | `""` | `verilator --version` text, set by `configure()` |
| `diff` | `str` | `""` | Unstaged diff under `piton/verif/env/manycore`, set by `configure()` |

## Constants

```python
DEFAULT_CACHES = {
    "l1i": (16384, 4),
    "l1d": (8192, 4),
    "l15": (8192, 4),
    "l2": (65536, 4),
}
MAX_TILES_PER_AXIS = 256
COVERAGE_LINE_FLAG = "-vlt_build_args=--coverage-line"
```

`DEFAULT_CACHES` mirrors `piton/tools/src/sims/manycore.config`. Each cache name fills the `<name>` in `-config_<name>_size` and `-config_<name>_associativity`. `MAX_TILES_PER_AXIS` matches the limit `sims` enforces with `DIE. x_tiles can be at most 256`. Adding `COVERAGE_LINE_FLAG` to `extra_flags` turns on Verilator line coverage (see [Code Coverage](../01_mace_user/Code_Coverage.md)).

## Validation

`__post_init__` raises `ValueError` on an invalid configuration:

| Condition | Example message |
|---|---|
| Unknown core | `core must be 'ariane', 'sparc', or 'pico', got 'mips'` |
| Tile count not an `int`, or a `bool` | `x_tiles must be an int, got True` |
| Tile count outside 1 to 256 | `x_tiles must be 1..256, got 0` |
| Unknown network | `network_config must be '2dmesh_config' or 'xbar_config', got '2d_mesh'` |
| Unknown cache name | `unknown cache 'l3'; valid: ['l15', 'l1d', 'l1i', 'l2']` |
| Size or associativity not positive | `cache l2 size/associativity must be positive, got (0, 4)` |

`__post_init__` does not check `sys`.

## Derived values

| Member | Value |
|---|---|
| `num_tiles` | `x_tiles * y_tiles` |
| `key` | SHA-256 hex digest of the configuration's identity |
| `build_id` | `"mace_"` followed by the first 12 hex digits of `key` |
| `finish_mask` | `"1" * num_tiles`, one digit per tile |
| `sims_flags()` | The `sims` arguments the configuration implies, in a stable order |

### key and build_id

`key` hashes a JSON object, serialized with sorted keys, that holds `sys`, `core`, `x_tiles`, `y_tiles`, `network_config`, `config_rtl` (sorted), `caches` (sorted by name), `extra_flags` (in order), `source_rev`, `ariane_rev`, `verilator_version`, and `diff`. [build()](workspace_node.md) passes `build_id` to `sims` as `-build_id`. `sims` writes every model to `rel-0.1` by default, so without a per-configuration ID two configurations overwrite each other's model.

Beyond the configuration fields, the key covers only what `configure()` records: the committed revision, the Ariane submodule commit, the Verilator version, and the unstaged diff under `piton/verif/env/manycore`. Edits anywhere else in the working tree leave the key unchanged. A directly constructed `PitonConfig` leaves those four fields empty, so its key depends on the configuration fields alone. The build cache covers the rest of the checkout: [build()](workspace_node.md) reuses a model only while the checkout's source fingerprint matches the one recorded when the model was built.

## sims_flags()

For `sys="manycore"`, `sims_flags()` emits these flags in order:

| Source | Flags |
|---|---|
| `sys` | `-sys=manycore` |
| `x_tiles`, `y_tiles` | `-x_tiles=<x>`, `-y_tiles=<y>` |
| `network_config` | `-network_config=<value>` |
| `core="ariane"` | `-ariane` |
| `core="pico"` | `-pico`, `-rv32_target_triple=riscv64-unknown-elf` |
| `core="sparc"` | No flag |
| `config_rtl` | `-config_rtl=<define>` for each entry |
| `caches` | `-config_<name>_size=<size>` and `-config_<name>_associativity=<assoc>` for each cache, sorted by name |
| `extra_flags` | Each flag, verbatim |

The default configuration produces:

```text
-sys=manycore -x_tiles=1 -y_tiles=1 -network_config=2dmesh_config -ariane
-config_rtl=MINIMAL_MONITORING
-config_l15_size=8192 -config_l15_associativity=4
-config_l1d_size=8192 -config_l1d_associativity=4
-config_l1i_size=16384 -config_l1i_associativity=4
-config_l2_size=65536 -config_l2_associativity=4
```

`-network_config` is always explicit. Left unset, `sims` defaults to the string `2d_mesh`, which `pyhplib.py` does not recognise. For `pico`, `-rv32_target_triple` makes `piton/tools/bin/rv32_as` use the installed `riscv64-unknown-elf-gcc` for rv32ima/ilp32 instead of a `riscv32-unknown-elf` toolchain (see [PicoRV32](../05_mace_cores/pico.md)). A `caches` mapping that omits a cache emits no flags for it.

For any other `sys`, `sims_flags()` returns `-sys=<sys>` followed by `extra_flags`. The mesh, core, and cache fields still count toward `key`.

`extra_flags` carries anything else `sims` accepts. For example, `-rv64_march=rv64imafdc_zicsr_zifencei` lets OpenPiton's diags assemble under binutils 2.38 and later.
