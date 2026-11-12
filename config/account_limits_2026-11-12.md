# Groq account limits (reference snapshot)

- **Captured:** 2026-11-12, copied by the account owner from the console page "Current Limits" (`console.groq.com/settings/limits`).
- **Provenance:** DOCUMENTED (the provider's own account console, supplied by the owner). It is **not** OBSERVED: no request has been sent. Limits "can change without notice", so re-check this page before each quota-sensitive run and keep a new dated copy if it changes.
- **Contains no key or credentials.**

## Chat Completions

| Model | Requests per minute | Requests per day | Tokens per minute | Tokens per day |
|---|---|---|---|---|
| allam-2-7b | 30 | 7K | 6K | 500K |
| meta-llama/llama-prompt-guard-2-22m | 30 | 14.4K | 15K | 500K |
| meta-llama/llama-prompt-guard-2-86m | 30 | 14.4K | 15K | 500K |
| openai/gpt-oss-120b | 30 | 1K | 8K | 200K |
| openai/gpt-oss-20b | 30 | 1K | 8K | 200K |
| openai/gpt-oss-safeguard-20b | 3 | 1K | 2K | 200K |
| qwen/qwen3.8-27b | 30 | 1K | 8K | 200K |

## Speech to Text (not used by this project)

| Model | Requests per minute | Requests per day | Audio seconds per hour | Audio seconds per day |
|---|---|---|---|---|
| whisper-large-v3 | 20 | 2K | 7.2K | 28.8K |
| whisper-large-v3-turbo | 20 | 2K | 7.2K | 28.8K |

## Text to Speech (not used by this project)

| Model | Requests per minute | Requests per day | Tokens per minute | Tokens per day |
|---|---|---|---|---|
| canopylabs/orpheus-arabic-saudi | 10 | 100 | 1.2K | 3.6K |
| canopylabs/orpheus-v1-english | 10 | 100 | 1.2K | 3.6K |

## Notes

- `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` are **not listed** for this account, so they are not available for planning.
- This table matches the public rate-limits page for every model both cover, so the earlier "which plan?" question is resolved: the extracted table is this account's limits.
- `allam-2-7b` appears here but was not on the public page. Its limits were previously unknown.
- "K" is read as thousands (1K = 1,000). Whether a limit is a fixed or rolling window is still not stated.
