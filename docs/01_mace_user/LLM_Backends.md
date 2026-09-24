% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# LLM Backends

MACE calls LLMs through CHIA's model classes.
`mace/llm.py` maps each backend name to one of them.
The planner, task execution, triage, and post-mortem see only an `LLMCallBase`, so switching backends needs no code change.

## Backends

| Backend | CHIA class | Credential | Default model | Cost reported |
|---|---|---|---|---|
| `vertex` | `chia.models.vertex.VertexGeminiLLM` | Google Application Default Credentials and `GOOGLE_CLOUD_PROJECT` | `gemini-2.5-flash` | No |
| `opencode` | `chia.models.opencode.OpenCodeLLM` | `OPENCODE_API_KEY` | the class's own | Yes |
| `claude` | `chia.models.claude.ClaudeCodeLLM` | `ANTHROPIC_API_KEY` | the class's own | No |
| `antigravity` | `chia.models.antigravity.AntigravityLLM` | `ANTIGRAVITY_API_KEY` | the class's own | Yes |

`vertex` is the default backend of `mace init`, `mace shell`, `examples/mace_end_to_end.py`, and `examples/baseline_one_shot_llm.py`.
For the three key-based backends, the table lists the variable `mace init` writes.
For `vertex`, `mace init` writes no file and only checks for Application Default Credentials.
`BACKEND_ENV_VARS` in `mace/cli/config.py` holds this mapping on a best-effort basis.
`init` prints the name it used, so you can check it against what the backend reads.

## Selecting a backend and model

`make_llm(backend=None, **overrides)` builds the backend named by `backend`, or by the `MACE_LLM` environment variable when `backend` is `None`.
With neither set, it builds `vertex`.
`MACE_LLM_MODEL`, when set, becomes the model, and a `model=` override takes precedence over it.
With neither, `vertex` uses `gemini-2.5-flash`.
An unknown name raises `UnknownLLMBackendError`.

`default_model_for_backend(model, backend)` returns `model` when one is given, `gemini-2.5-flash` for `vertex`, and `None` for any other backend, which then uses its own default.
`mace shell` and the two scripts above apply it to `--model`.
When the result is not `None`, the shell writes it to `MACE_LLM_MODEL`.
It also sets `MACE_LLM` to the backend if `MACE_LLM` is unset.

The drivers declare a `<backend>_creds` Ray resource next to `openpiton`, such as `vertex_creds`.
Declare the same resource in your own driver.

## Vertex Gemini

Vertex authenticates with Application Default Credentials and needs no key file:

```bash
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<your-gcp-project>
mace shell --piton-root ~/openpiton --backend vertex
```

`VertexGeminiLLM` reads the project from `GOOGLE_CLOUD_PROJECT`.
`mace shell` exits when it is unset.
The scripts also take `--project`, which overrides it, and stop when neither is given.
To authenticate with a service account instead, put `GOOGLE_APPLICATION_CREDENTIALS=<path>` in an env file and pass it with `--api`.
With `--backend vertex`, the shell exits if that file does not set this variable.

## Key-based backends

```bash
mace init --backend opencode --api-key <key> --env-file .env.mace
mace shell --piton-root ~/openpiton --backend opencode --api .env.mace --model opencode/big-pickle
```

The shell requires `--api` for these backends.
`--model` is optional; without it, `MACE_LLM_MODEL` or the class's own default applies.
The prerequisites of the `opencode`, `claude`, and `antigravity` classes are documented in CHIA.

## Cost reporting

`extract_cost_usd()` reads `usage["cost_usd"]` from a call's result.
OpenCode and Antigravity results carry it.
Claude keeps its cost on the LLM instance, which a remote call does not return, so its calls count as 0.
`VertexGeminiLLM` returns a plain `QueryResult` with no usage field, so every Vertex call also counts as 0.

On every backend, the loop's tally counts per-task calls only and skips planner, triage, and post-mortem calls, so `compute_usd` is a lower bound.
On Vertex the tally stays at 0, so `Budget.max_usd` cannot stop a run.
Rely on `max_iterations` and `max_wall_s`.
[Cost](../06_mace_evaluation/cost.md) gives estimated per-run costs.
