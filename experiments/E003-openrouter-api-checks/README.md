# E003: Live API checks, OpenRouter and cost reporting

| | |
|---|---|
| Status | Done |
| Dates | run 2026-11-13 |
| rho-agent commit | branch `claude/terminal-bench-4-eval-f1a3f1` (smith-nathanh/rho-agent#29, before merge) |
| Cost | $0.003 (9 small calls) |

Small direct API calls, outside Harbor, to settle two design questions.

## 1. Does OpenRouter carry GPT-6.1 Sol's reasoning?

**Design:** `openai/gpt-6.1-sol` through `https://openrouter.ai/api/v1`,
Responses API, `store=false`, `include=["reasoning.encrypted_content"]`, effort
medium, one function tool. A first call, then a follow-up with the reasoning
items replayed, a follow-up without them, a streaming call, and the same two
turns through rho-agent's `ModelClient`. 7 calls, $0.0027.

**Results:**

- Encrypted reasoning is returned (3,053 characters on the first call).
- Replayed reasoning is accepted. The follow-up with it was billed 162 input
  tokens against 115 without; the extra 47 match the 45 reasoning tokens of the
  first call, which suggests the reasoning reached the model.
- `usage.cost` is present on every response, streaming included, with an
  input/output split in `cost_details`.
- `ModelClient` stayed on the Responses endpoint, stored and replayed the
  reasoning, and answered correctly with no errors.
- OpenRouter charged OpenAI's list prices ($2 / $10 per million).
- Not tested: prompt caching through OpenRouter. Every prompt was under the
  1,024-token minimum.

## 2. Is the reworked cost accounting right?

**Design:** one tiny call through OpenRouter (cost read from the response) and
the same call direct to OpenAI (cost from the built-in price table). 2 calls,
$0.0002.

**Results:** both came to $0.000082. OpenRouter accepted the `x-session-id`
header. Whether calls group into one session in OpenRouter's Broadcast view was
not checked.
