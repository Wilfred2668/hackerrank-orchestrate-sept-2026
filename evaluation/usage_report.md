# Buy or Wait? — Final Full-Dataset LLM Usage Report

This report documents all model usage, token consumption, and estimated costs for producing the final evaluation predictions for all 250 requests in `dataset/requests.csv`.

## 1. Executive Summary

- **Total Evaluation Requests**: 250
- **New Model Calls During Final Run**: 0
- **Cached Extraction Results Reused**: 231 (16 images, 215 messages)
- **Earlier Calls Recorded in Cache Provenance**: 140
- **Total Input Tokens**: 370,890
- **Total Output Tokens**: 52,057
- **Total Tokens Consumed**: 422,947
- **Average Input Tokens Per Request**: 1483.56
- **Average Output Tokens Per Request**: 208.23
- **Average Total Tokens Per Request**: 1691.79
- **Estimated Total Evidence Extraction Cost**: $0.0615
- **Estimated Cost Per Evaluation Request**: $0.000246

## 2. Model Breakdown & Token Consumption

| Provider | Model | Call Type | Calls | Input Tokens | Output Tokens | Total Tokens | Est. Cost (USD) |
|---|---|---|---|---|---|---|---|
| google | `gemini-3.1-flash-lite` | `image_extraction` | 1 | 1,702 | 73 | 1,775 | $0.0002 |
| google | `gemini-3.1-flash-lite` | `message_batch` | 24 | 230,381 | 31,170 | 261,551 | $0.0355 |
| google | `gemini-3.1-flash-lite` | `message_extraction` | 86 | 109,508 | 10,028 | 119,536 | $0.0150 |
| google | `gemini-3.1-flash-lite` | `test` | 11 | 45 | 12 | 57 | $0.0000 |
| google | `gemini-3.6-flash` | `image_extraction` | 15 | 25,418 | 7,698 | 33,116 | $0.0084 |
| google | `gemini-3.6-flash` | `message_extraction` | 3 | 3,836 | 3,076 | 6,912 | $0.0024 |
| **Total** | | | **140** | **370,890** | **52,057** | **422,947** | **$0.0615** |

## 3. Pricing Assumptions & Methodology

- **Pricing As-Of**: September 2026 (Google Cloud / Google AI Studio API standard rates).
- **`gemini-3.1-flash-lite`**: $0.10 / 1M input tokens, $0.40 / 1M output tokens.
- **`gemini-3.6-flash`**: $0.15 / 1M input tokens, $0.60 / 1M output tokens (used for visual document extraction).
- **Cost Derivation**: Calculated exactly as `(input_tokens / 1,000,000) * input_price + (output_tokens / 1,000,000) * output_price` without rounding before final sum.

## 4. Cache & Determinism Invariant

- All evidence extractions are strictly deterministic and cached under `code/cache/` by sha256 prompt version.
- During the production run, 100% of required extractions hit the verified local cache.
- No API keys, secrets, or sensitive PII are recorded in this report or committed to the repository.
