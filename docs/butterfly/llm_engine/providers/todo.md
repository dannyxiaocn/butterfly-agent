# Providers — Todo

## Completed

- [x] Anthropic thinking mode via beta namespace (4978494)
- [x] Kimi thinking via extra_body
- [x] Codex provider with OAuth + SSE (bd5d01d)
- [x] thinking_effort for Codex (91762f5)
- [x] v2.0.4 bug-fix pass — revert Codex default to gpt-5.4 (ChatGPT-OAuth
      rejects gpt-5-codex); `_is_codex_compatible_model` replaces hardcoded
      claude-sonnet-4-6 sentinel; `thinking_effort="none"` honored on
      Responses API (reasoning block omitted); invalid effort uniformly
      defaults to "medium"; `openai_api._tc_map_to_list` filters empty-name
      entries (matches codex/responses); multi-text tool_result blocks are
      byte-for-byte concatenated (was space-joined); `Agent._get_fallback_provider`
      reuses primary provider when only `fallback_model` is set; docstring
      softens the prompt_cache_key claim to reflect observed no-cache behavior

## Future

- [ ] OpenAI streaming tool calls
- [ ] Provider-specific rate limiting
