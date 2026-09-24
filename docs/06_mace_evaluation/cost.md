% Copyright (c) 2026 Rana Umar Nadeem, Samrah Mumtaz, Muhammad Imran

# Cost

## Total

All experiments behind the paper cost $37 of a $300 GCP credit allocation, covering simulation compute and LLM inference.

## Per run

Estimated cost of one MACE loop run on a 2x2 mesh:

| Core | LLM tokens | Compute | Total |
|---|---|---|---|
| Ariane | $0.04 | $0.08 | about $0.12 |
| PicoRV32 | | | about $0.02 |
| OpenSPARC T1 (exhausting a three-iteration budget) | | | about $0.25, estimated, mostly its one-time build |

## Pricing assumptions

| Item | Rate |
|---|---|
| Compute | `e2-highmem-8`, $0.36 per hour, over each run's wall time |
| Gemini 2.5 Flash input tokens | $0.30 per million |
| Gemini 2.5 Flash output tokens | $2.50 per million; thinking tokens are billed as output |

## Measuring tokens

MACE's own cost counter (`compute_usd` in the run database) reads zero for Vertex runs. CHIA's Vertex backend reports no price, and its token count skips thinking tokens, which were more than half of the billed output in these runs. The token figures above come from re-sending representative planner and task prompts and counting every output token, thinking included.
