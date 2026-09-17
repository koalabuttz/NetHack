# DeepSeek Cache-Utilization Harness — Architect Handoff

Implementation handoff for cache-aware strategy prompting in `tools/agent/`.
Produced by architect design review; a coding agent executes this as-is.

## Recommendation

Implement **bounded, episode-local conversation continuity owned by the harness**, with stable-to-volatile state rendering and cache-aware usage settlement. Freeze the entire request before reservation; dispatch exactly that request. Commit a user/assistant pair only after a successful validated result is accepted by the controller's settlement path. Preserve the existing conservative all-input-at-full-price reservation policy.

Use the same preparation/history helper for live play and explicitly opted-in strategy replay. Keep offline replay and strategy-off provider comparisons network-free. Do not change the worker protocol/process implementation or the engine.

This is a design handoff, not an implementation. DeepSeek's automatic caching and reported hit/miss field behavior are operator-supplied facts, not externally reverified here.

## Existing-system evidence and consequential findings

- `tools/agent/providers.py:702-753`: static schema/untrusted-data system prompt; current user prompt starts with status/level, then boundaries/map/messages/inventory/budget; payload has exactly two messages.
- `providers.py:232-261`: StrategyContext already has unused summary/history/goals/postmortem fields; StrategyResult has usage, validated directives, ok, and dispatched, but no accepted assistant text.
- `providers.py:775-784,928-961`: usage currently copies aggregate tokens only; both malformed and invalid-directive responses already preserve usage. Directive validation precedes a successful StrategyResult.
- `providers.py:793-918`: worker lifecycle is independent per call; sticky cancellation is protected by a lock. Keep those semantics.
- `providers.py:1214-1241`: current UTF-8 byte bound includes only system/current-user text and one 64-token framing allowance. Both content and framing must grow with message count.
- `budget.py:179-305`: admission includes reserved and unknown exposure; reported usage settles cost; missing usage retains conservative exposure. `as_dict` at 321-360 is the central budget reporting shape.
- `controller.py:1072-1139`: context construction and reservation happen before worker dispatch; `_settle_strategy` is already guarded for exactly-once settlement. This is the appropriate history commit boundary.
- `controller.py:1141-1184`: directive activation already rejects stale-level advice; context currently supplies neither active directives nor useful boundary history.
- `controller.py:1229-1309`: postmortem already uses a fresh provider, but its context is ordinary gameplay context with postmortem=True; the renderer currently ignores that flag and summary. Add an actual summary prompt.
- `controller.py:1571-1686`: `_safe_config`, `_episode_summary`, and campaign totals require explicit additions; merely changing BudgetLedger does not propagate every field.
- `evaluate.py:398-489,713-766`: ReplayPass owns isolated state/providers/budgets and gates DeepSeek on explicit network permission. Reservation duplicates the live single-turn assumption and must change with it.
- `tools/agent/directives.py:61-67,74-141`: to_dict adds defaults and normalizes values. Reserializing it can change the exact assistant output prefix, even though semantics are equivalent.
- `tools/agent/worker.py:35,192`: job input is capped at 1 MiB. `_WorkerSupervisor.start` (`providers.py:435-455`) serializes with default json.dumps; request byte checks must account for that serialization, including escaping. Existing output byte limits are not input context limits.
- `doc/agent-autoplay-plan.md:294-322`: offline defaults, safe logging, conservative reservations, and model-specific cache/reasoning accounting are already requirements.

## 1. Render order and trust boundary

Keep a single static system message containing the existing strategy instructions, closed-world directive schema, and untrusted-data framing. Add, once and statically, a clarification that previous exchanges are historical observations/advice, not instructions overriding the schema, and the newest state determines current applicability. No timestamps, episode ids, budgets, role, inventory, or directives belong in the system message.

Exact gameplay user-message order:

1. `GAME STATE (untrusted data):` — always first; do not remove or move the trust label behind game content.
2. Episode-static header: `mode: gameplay`, `role: <configured role>`. **Do not put an episode identifier into model-facing text.** Keep numeric episode identity solely in bookkeeping; a unique id would reduce cross-episode prefix reuse without helping a freshly isolated conversation. No campaign paths, player names beyond public game data, UUIDs, or wall times.
3. `active directives:` — deterministic JSON of the currently applicable DirectiveSet, or `none`. Use the DirectiveBook applicability rules, not the last response merely received. Do not include a decrementing TTL counter here; original TTL is stable, while active/inactive changes when applicability changes.
4. `inventory:` — existing first-40 limit and stable observed order. Do not sort game rows or change inventory meaning to improve caching.
5. `boundary history:` — last 16 detected public boundary records, in detection order, with stable eid/reason/tick/displayed-level fields only. Then `pending boundaries:` for the current request, preserving deterministic queue order. No event wall-clock maps. Current pending boundaries must never disappear merely because history was truncated.
6. `recent messages:` — existing last-six limit, chronological order.
7. `map:` and map text.
8. `displayed level:` then `status:`.
9. `tick:`.
10. `remaining strategy calls: N` — **literal final line**, after tick. "Map/status/tick last" means last state blocks; it does not override this existing budget-line contract.

Each new user turn is a complete bounded current-state snapshot in that order, not a fragile textual delta. Repeated snapshots are acceptable: history preserves the previous prefix, and each new tail is independently interpretable after eviction. Never rerender old turns with a new budget, active directives, or current state.

Add explicit role and active-directive fields to StrategyContext. Give `history` its boundary-history meaning; do not overload it with chat messages. Build boundary history from a bounded harness-owned deque updated at `_note_detected` in live/replay, rather than depending on recorder internals or retaining an unbounded event log. Use explicit labels and deterministic JSON for structured blocks, with no unordered sets or wall timestamps.

## 2. Conversation structures, ownership, and transaction

Add small stdlib-only value structures/helpers in `providers.py` (no new module needed):

- `StrategyExchange`: frozen rendered user text plus validated assistant JSON text.
- `StrategyConversation`: episode identity and a bounded sequence of committed exchanges. No key, supervisor, HTTP body, or provider handle.
- `PreparedStrategyRequest`: immutable snapshot of model/messages/generation settings and its prompt/completion bounds. Messages are internally immutable role/content pairs; convert to API dictionaries only for serialization. Include the new user text and retained history slice needed for settlement, not references to mutable live state.
- Optional `StrategyContext.prepared_request`: permits existing `deliberate(ctx, deadline)` and injected fake-provider signatures to remain intact.
- Optional `StrategyResult.assistant_content`: only set after JSON parsing and `validate_directive_set` succeed; not automatically included in recordings.

Ownership: one StrategyConversation per `_EpisodeRunner`, and one per ReplayPass. **Not one conversation spanning multiple campaign episodes.** Campaign calls share history only within their current episode. Provider/worker respawn does not own or clear it.

Preparation/dispatch sequence:

1. Build a current StrategyContext on the controller thread.
2. A pure preparation helper selects the retained committed history, renders the new tail once, and freezes the full payload.
3. Compute the bound from that frozen request.
4. Ask BudgetLedger to reserve. If refused, suppress as today and do not mutate conversation, spawn a worker, consume a call, or add exposure.
5. Store the prepared request alongside `_strategy_pb`; pass the same prepared request through the context into `DeepSeekStrategy.deliberate`.
6. Provider uses the frozen payload verbatim, not a second render or independently selected history.
7. `_settle_strategy`, guarded by its existing exactly-once token, handles usage as today and commits the selected history plus the newly completed pair only if `res.ok`, validated directives exist, and `cancelled` is false.

The pure preparer also supports direct provider calls without a prepared context: they get a fresh two-message request. Document that direct independent `DeepSeekStrategy.deliberate` calls are not secretly conversation-owning; the harness is the owner. Unit tests needing continuity should exercise the helper/controller, not assume provider-global state.

Assistant content policy:

- Retain the original `choices[0].message.content` string **verbatim after validation**, including whitespace/key order. It is validated DirectiveSet JSON actually returned, not unvalidated prose. Validation does not make its explanation an instruction.
- Do not replace normal provider output with `json.dumps(dset.to_dict())`: normalization changes the generated prefix.
- For the existing compatibility path that accepts dictionary content, or injected fake providers lacking text, use stable JSON serialization of the validated DirectiveSet. This fallback is semantically correct but cannot promise generated-prefix fidelity.
- Store no reasoning text. Preserve only reasoning token counts as usage diagnostics. A real model/API compatibility check for multi-turn reasoning models is an implementation acceptance gate; do not add unvalidated reasoning messages speculatively.

### Eviction and resets

Recommend `ProviderConfig.deepseek_history_pairs = 8`, exposed as `--deepseek-history-pairs`; 0 is a clear continuity-off rollback. Default 8 retains all default gameplay calls (7 with the reserved postmortem, or 8 when postmortem is disabled) without eviction. Bound configuration to a reasonable supported range, e.g. 0..64, through the existing validation authority.

Add `deepseek_context_max_bytes`, default 262144, exposed with the matching CLI flag. This is a harness message/payload safety bound, **not a claim about a model's context window**. Define it against the UTF-8 serialization of the full API payload using the worker-compatible JSON serialization convention. Evict oldest complete user/assistant pairs until both pair-count and byte limits fit. Always keep system + the current user message. Reject locally as `strategy-context-too-large` when that irreducible request does not fit; do not truncate JSON/map strings mid-content.

Keep selected eviction transactional: preparation does not destructively change committed history; successful settlement installs the retained slice plus the new pair, capped to K. A failed call leaves the previous committed history intact. At the next prepare, deterministic eviction is selected again.

Also check the serialized **complete worker job** against existing MAX_JOB_BYTES before `_WorkerSupervisor.start`, including envelope/key/url. Do this in the provider without logging it. The default payload ceiling leaves substantial room, but only the actual job check guarantees compliance. Keep worker/supervisor code unchanged. A local oversize refusal must not be recorded as billable exposure.

Do **not** evict opportunistically to meet a remaining token/USD budget: after deterministic context eviction, a too-large reservation follows the existing cap-refusal path. This keeps cost decisions reproducible and avoids silently throwing away context to spend another call.

Reset on episode finish/new episode, explicit continuity-off configuration, or model/base URL identity change. Configuration is ordinarily fixed for an episode. Do **not** reset at level change: historical goals/inventory remain useful and cache reuse survives transitions. Current-level labeling and the existing stale-level activation guard remain mandatory. Valid advice later deemed stale at activation can remain as historical dialogue; it is not reported as active.

Postmortem always gets a separate empty conversation and fresh provider; never transfer gameplay history or mutate/circumvent the gameplay provider's sticky cancellation flag.

### Failure/cancellation rules

Timeout, HTTP error, malformed JSON, invalid directives, local refusal, provider exception, or canceled settlement: append neither user nor assistant. A paid invalid response still contributes usage/cost. Unknown paid exposure retains the entire cumulative request bound. A late result after cancellation must not commit history; worker threads return results, never mutate the conversation. Existing exactly-once settlement also prevents double appends.

One existing asymmetry needs a focused correction: `_settle_strategy` currently commits every started operation, whereas `_settle_postmortem` distinguishes undelivered results. For explicit known pre-dispatch refusals (no key/cooldown/spawn/oversize), use the dispatched evidence and release rather than book phantom exposure, matching postmortem. Preserve conservative exposure for an ambiguous exception/result loss or cancellation after thread start; do not interpret `res is None` as proof of no dispatch. Cover this change independently in tests.

## 3. Reservation contract

Restate `strategy_token_bound` as: "Return a conservative prompt/output bound for **the exact complete prepared request that will be dispatched**, including static system, every retained historical user and assistant message, current user tail, message framing, and configured max completion tokens. Never discount cached prompt tokens at reservation time."

Keep a compatibility path for contexts without prepared requests by preparing a zero-history request. Internal live/replay callers must supply the frozen request explicitly/through the context.

Recommended framing formula: UTF-8 bytes of every message role and content, plus a fixed 64-token request allowance and 64 tokens per message. This intentionally exceeds the present two-message framing allowance; name/document the components and test their growth. Do not claim a mathematical guarantee for arbitrary server chat templates: the byte bound applies to content under the current tokenizer assumption, and framing remains a documented conservative allowance. A model change requires checking that assumption.

Completion bound remains deepseek_max_tokens, including reasoning; do not reserve reasoning again. Cumulative requests raise both token and full-price USD reservations even when likely cached. Therefore later strategy calls may be correctly refused sooner. Token caps count hit tokens too. Pricing savings can reduce settled USD and permit later calls only after reported usage is known.

## 4. Cache and reasoning usage accounting

### Config and tariff

- `ProviderConfig.deepseek_price_cache_hit: Optional[float] = None` and `--deepseek-price-cache-hit`, USD per million prompt-cache-hit tokens.
- `Tariff.cache_hit_per_mtok: Optional[float] = None`, appended with a default to preserve existing two-positional-argument callers.
- Effective hit price is configured cache price, otherwise prompt_per_mtok. Never invent rates.
- "Complete tariff" for USD cap remains **input + output**. Cache price is optional because full input price is the conservative fallback. Cache price alone must not make a tariff complete.
- Validate finite, nonnegative numbers at both ProviderConfig and BudgetLedger boundaries. To preserve full-input-price reservation as an upper bound, reject an explicitly configured cache-hit price greater than configured input price. Explain this policy in the error/doc; do not silently clamp it.
- `tariff_from_config`, `tariff_complete`, Tariff.to_dict and `_check_tariff` need updates. Serialize the optional configured price and make the effective fallback visible/documented. Preserve existing partial-tariff/no-USD-cap behavior, explicitly labeled incomplete; do not misrepresent it as enforceable pricing.

### Provider parsing

Extend `_usage_of` to preserve nonnegative, non-bool integer `prompt_cache_hit_tokens` and `prompt_cache_miss_tokens`, alongside aggregate prompt/completion/total. Preserve `completion_tokens_details.reasoning_tokens` when valid; no reasoning text. Reasoning tokens are included in completion_tokens, not an additional cost.

Normalize defensively again at the ledger boundary, which can receive fake/programmatic provider usage. Never let negative, floating, bool, contradictory, or missing cache fields reduce billed exposure.

For the first version, grant a cache discount only for a complete consistent partition: P, H, M are valid and H+M=P. Aggregate usage without a usable partition remains fully priced. Partial/malformed cache reports are not fatal to an otherwise valid directive response; they lose the discount and are counted as unclassified prompt tokens. This deliberately conservative policy avoids guessing backend-specific semantics.

If aggregate prompt is missing but both valid cache counts exist, derive P=H+M. If completion or prompt totals cannot be established, do not let an otherwise nonempty `{reported: true}` discard the reservation: retain conservative unknown exposure for missing components. The current truthiness-only commit path has this hazard; extend its settlement normalization or component handling with focused tests. Genuine reported zero counts remain distinct from missing usage.

### Ledger fields and math

Add under `BudgetLedger.as_dict()['usage']`:

- `cache_hit_tokens`: accumulated H from valid cache partitions.
- `cache_miss_tokens`: accumulated M from valid cache partitions.
- `cache_unclassified_tokens`: reported prompt tokens lacking a usable cache partition.
- `cache_hit_rate`: H/(H+M), or null when denominator is zero.
- `reasoning_tokens`: diagnostic subset of completion tokens, when supplied.

Maintain existing prompt_tokens/completion_tokens/unknown exposure fields and meanings. Hit rate is measured over classified tokens, not fabricated misses when metadata is absent. Document cache_unclassified_tokens so a high rate over low reporting coverage is not misleading.

Settled estimated cost:

`(cache_hit_tokens * effective_hit_price + (prompt_tokens - cache_hit_tokens) * input_price + completion_tokens * output_price) / 1_000_000`.

This prices both misses and unclassified input at full input price. Accumulate unrounded costs; round only for artifacts as today. Ledger tariffs are fixed for an episode; do not retrospectively reprice already-settled calls if somebody mutates a config object.

Keep `_price(prompt,completion)` as the conservative full-price pricing function for reservations and unknown exposure; introduce a separate reported-usage pricing calculation rather than changing `_price` to speculate about cache hits. Never add H+M to prompt_tokens a second time. Never add reasoning_tokens to completion_tokens a second time.

### Reporting

- `_episode_summary`: copy all five new usage fields with compatibility defaults (counts 0, rate null).
- `campaign_summary`: sum token counts; compute campaign rate from summed H and M, **not average episode percentages**. Include postmortem usage as existing totals already do. Preserve unknown-price and unknown-exposure separation.
- `_safe_config`: add history pair/byte settings and all three configured tariff values; these are nonsecret. Do not add prepared payloads, keys, key-file content, request headers, reasoning text, or HTTP error bodies.
- `campaign.json`/budget/meta schema changes are additive; keep schema version 1 unless repository consumer policy demands a bump. Consumers should tolerate absent new fields in old artifacts.
- Add a human readout such as `cache hit: 75.0% (H hit / M miss; U unclassified)`; `n/a` for zero classified tokens. Identify the existing auto summary printing site in `tools/agent/__main__.py` during implementation; do not alter unrelated output.

## 5. Postmortem and evaluation

Postmortem user prompt starts fresh with the same static system/schema and the untrusted label. Use `mode: postmortem`, role, then an allowlisted deterministic episode-summary JSON (outcome/stop reason, final tick/visible level/HP, action/invalid/boundary counts, strategy dispatch count, validated advice summary if useful), bounded terminal messages and final visible state, tick, and the remaining-budget line last. Do not include wall duration, secrets, raw transcript, or not-yet-finalized recorder metadata. Retain the DirectiveSet response contract; its explanation carries retrospective advice. Build and reserve this request independently, not using gameplay history. Do not add a new durable cross-episode learning store.

Evaluation decision: **continuity on for the explicitly enabled DeepSeek strategy pass**, using the same K/byte policy, render/preparation helper, whole-request bound, and successful-settlement commit rule. Every ReplayPass starts empty. Comparison passes with strategy off remain off and unchanged in behavior. Default evaluation remains offline even when keys exist.

Determinism claim is deliberately scoped: same wire/config/canned response sequence produces byte-identical requests and cleaned artifacts. Network calls were never reproducible merely because harness preparation is deterministic; cache usage can also vary between real requests. Do not claim byte-identical live-provider artifacts. No random ids, real clocks, or wall-timed history eviction. New usage fields are deterministic zeros/null in offline runs. Preserve existing wall-field stripping.

## 6. Per-file implementation map

1. `tools/agent/providers.py`: ProviderConfig/validate; StrategyContext/StrategyResult; pure conversation/prepared-request helpers; `_SYSTEM_PROMPT`, `_render_strategy_prompt`, `deepseek_payload`, `strategy_token_bound`; `_parse_chat_response` or a companion extractor preserving validated content; `_usage_of`; `DeepSeekStrategy.deliberate/_interpret`; tariff helpers. Leave `_WorkerSupervisor` transport/lifecycle unchanged.
2. `tools/agent/budget.py`: Tariff and validation; counters; normalized reported settlement and conservative handling of incomplete totals; cache-aware settled pricing; `as_dict`. Preserve reservation and unknown-exposure full-price semantics.
3. `tools/agent/controller.py`: runner initialization owns conversation and bounded boundary history; `_note_detected`, `_build_strategy_context`, `_dispatch_strategy`, `_settle_strategy`, cancellation cleanup; fresh postmortem preparation/summary; `_safe_config`, `_episode_summary`, campaign aggregation.
4. `tools/agent/evaluate.py`: ReplayPass initialization/history/context, `_service_strategy` prepare/reserve/commit; report additions flow through ledger. Carry new config defaults/arguments where the evaluator already accepts corresponding provider configuration, without widening network opt-in.
5. `tools/agent/__main__.py`: auto parser/config mapping for cache tariff/history controls; safe reporting readout. Existing price arguments/mapping are at 77-81 and 130-131.
6. Existing tests: `test/agent/test_auto_providers.py`, `test/agent/test_auto.py`, `test/agent/test_auto_replay.py`.
7. `doc/agent-autoplay.md`: cache section, flags/fallback/complete-tariff rule, bounded history/reset policy, hit-rate coverage, conservative admission, postmortem behavior, deterministic/offline guarantees. Briefly align `doc/agent-autoplay-plan.md` Configuration section without rewriting historical findings.

## 7. Required tests and mutation demonstrations

Implementer verifications; not tests the architect ran.

### Rendering and request fidelity

- Assert exact block order, untrusted label first, budget line last, first-40 inventory and last-six messages unchanged. Same static input gives identical bytes; changing only tick/status/map must leave the earlier stable blocks identical.
- Confirm active directives reflect applicability; no real-time countdown contaminates stable prefix. Boundary history is bounded/deterministic and current pending ids survive truncation.
- Fake endpoint observes roles `[system,user]`, then `[system,user,assistant,user]`, then six messages. Verify previous request messages are byte-for-byte equal to the prefix of the next request, and assistant text equals the validated original response, including deliberate unusual whitespace/key order.
- Reject executable/unknown directive fields as before; invalid response/usage gets billed but adds no history.
- K=0 stateless; K=1 exact oldest-pair eviction; default K=8 retains the normal episode. Byte-ceiling eviction removes complete pairs only; irreducible oversize produces no worker. Check complete worker-job size with non-ASCII escaping.
- New episode and postmortem start with two messages; level change retains history but stale directive activation remains rejected. Worker restart does not clear harness history.

### Bound and budget safety

- Update `TestStrategyTokenBound` at `test_auto_providers.py:2042-2118` to derive expectation from the actual frozen messages and per-message framing. Do not keep `_rendered = system + current user` as the oracle for multi-turn requests.
- Preserve `test_cjk_context_refuses_dispatch_under_a_tight_cap`: independently construct a chars/4 candidate using the same message set/framing, set cap at that wrong bound plus output, and assert correct UTF-8 bound refuses before dispatch, fake.calls stays empty, and boundary suppression remains one. Avoid hard-coded old prompt lengths/order.
- Add the stronger continuity discriminator: current tail alone fits the remaining cap but cumulative history+tail does not; no worker, no history mutation, no reserve leak.
- Validate previous assistant CJK/emoji bytes and growing message framing count toward the bound. Assert the captured endpoint payload equals the reserved prepared payload, even if live state changes while the call is in flight.
- Timeout with history carries the **cumulative** reservation as unknown exposure. Reported hits do not lower pre-dispatch reserve. USD fallback to input price is conservative.
- Cancellation race and double settlement never append twice or append after cancellation. Known local refusal releases; ambiguous dispatched loss retains exposure.

### Accounting and artifacts

- Hit+miss parsing and settled arithmetic; zero hits; all hits; absent cache price; explicit zero cache price; no cache fields; partial/contradictory/negative/bool/float fields; valid zero totals versus missing totals.
- reasoning_tokens survives normalization/reporting but is not billed twice.
- Complete tariff still means in+out for USD caps; cache-only is incomplete; NaN/negative and cache price above input rejected at config and ledger boundaries.
- Ledger and episode/campaign counts agree; weighted campaign rate uses token totals; zero denominator is null; old result dicts lacking cache fields still summarize.
- Fake endpoint usage reaches decisions, budget metadata, postmortem totals, and campaign.json. Existing secret-in-artifacts test remains green; raw prepared payload/assistant/credential envelopes are not newly persisted.

### Replay and regression

- Run existing offline/no-network/default-key-present and fixture-integrity tests in `test_auto_replay.py`; retain provider-comparison strategy-off isolation.
- Run identical offline fixture evaluation twice and byte-compare outputs. Add a deterministic canned multi-turn strategy replay twice, including eviction and a failure, and compare request sequence plus cleaned artifacts.
- Run all existing auto/provider/replay tests, including postmortem fresh lifecycle, cancellation race, recording health, and stale-level tests. No real API is needed for this gate.

Mutation demos must show targeted failures, then restore the mutation: (1) bind only newest tail; (2) revert UTF-8 bytes to chars/4; (3) use constant framing despite message growth; (4) discard history or rerender old turns; (5) append failed/canceled responses; (6) drop parsed cache fields or price hits at input despite a configured discount; (7) discount reservations using presumed hits; (8) average episode rates; (9) reuse gameplay conversation for postmortem. Keep each demo scoped and record exact failing test(s).

## 8. Ordered commits and acceptance gates

1. Cache usage/config/tariff/accounting plus focused tests, preserving existing stateless requests. Acceptance: optional fallback and full-price reserve are demonstrated.
2. Stable renderer, context fields, pure frozen request/conversation helpers, cumulative byte/framing bound and eviction tests. Acceptance: request preparation is deterministic and bounded.
3. Controller transactional history, local-refusal settlement distinction, and fresh postmortem summary. Acceptance: endpoint growth/failure/cancellation/reset/cap tests pass.
4. Replay parity, campaign/human reporting, CLI controls, and documentation. Acceptance: offline artifacts deterministic and comparison passes unchanged.
5. Full relevant regression suite and mutation demonstrations; review actual diff and evidence. **Reviewer gate before declaring implementation complete or doing an optional operator-approved real-provider smoke.** Commit subjects follow AGENTS.md's <=50-character convention.

## Risks, alternatives, and remaining checks

- Cache reuse is an optimization, not an SLA. Prefix blocks, cache eviction, model behavior, and server serialization determine actual hit rate. No latency or savings claims should precede measured usage.
- Longer prompts increase reserve requirements and can increase actual cost when cache misses occur. K=8 + byte ceiling is chosen to preserve default-episode continuity; K=0 is the explicit rollback. Stable ordering remains useful with continuity off and after eviction.
- Keep-last-K eviction breaks the oldest prefix at rollover. This is intentional bounded behavior; retaining an immutable anchor plus summaries adds complexity and is unnecessary for the default cap.
- Retaining original validated assistant JSON improves prefix fidelity versus canonicalizing it. It does not prove the server caches the generated assistant exactly or that reasoning-prefix reuse is available. A model-specific smoke must verify that ordinary JSON assistant history is accepted without reasoning_content. If not, pause for a model-specific decision; do not silently ship unvalidated reasoning transcripts.
- Framing is a conservative engineering allowance, not a verified model-template proof. Context ceiling is bytes, not the model's token window. These assumptions must be documented, especially for operator model overrides.
- Historical assistant explanation is model-authored text, not a trusted instruction or executable action. Continue strict directive validation and current-level/precondition enforcement.
- I found no blocker to this design. The only external compatibility check needed before enabling a real-provider rollout is the chosen model's multi-turn content-only/reasoning behavior; fake-endpoint tests cannot establish server cache effectiveness. No real API pricing values are proposed.
