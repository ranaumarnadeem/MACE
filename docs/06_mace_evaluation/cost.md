% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Cost

## Total

All experiments behind the paper cost $37 of a $300 GCP credit allocation, covering simulation compute and LLM inference.

## Per run

Estimated cost of one MACE loop run on a 2x2 mesh:

| Core | LLM | Compute | Total |
|---|---|---|---|
| Ariane | $0.04 | $0.08 | about $0.12 |
| PicoRV32 | | | about $0.02 |
| OpenSPARC T1 (exhausting a three-iteration budget) | | | an estimated $0.25, mostly its one-time build |

## Pricing assumptions

| Item | Rate |
|---|---|
| Compute | `e2-highmem-8`, $0.36 per hour, over each run's wall time |
| Gemini 2.5 Flash input tokens | $0.30 per million |
| Gemini 2.5 Flash output tokens | $2.50 per million; thinking tokens are billed as output |

## Measuring tokens

The run databases behind these numbers hold no per-call token counts, so their `compute_usd` reads zero. The LLM costs above come from re-sending representative planner and task prompts and counting all output, thinking included; thinking tokens were more than half of the billed output. MACE now records every call's tokens, thinking included, in the `llm_calls` table; see [Results and Metrics](../01_mace_user/Results_and_Metrics.md).
