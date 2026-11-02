# NTP time-change approval

The `bianbu.bibit.intent` BeforeRun hook can return a private `ContextPatch.approval`
alongside a direct reply. Only this plugin and hook point may provide it, and the
only supported action is `disable_ntp_and_set_time`. The patch is not a generic
callback URL or command execution facility.

The local intent service owns an expiring, session-bound one-shot ticket. Runtime
does not pass the ticket or bearer token into model context, public metadata, or
hook observations. The approval event has an independent ID, a description, the
requested datetime, and exactly `approve`/`reject` decisions. The existing run
control and session-scoped APIs deliver the user's decision to the same live run.

The run waits up to 150 seconds without calling the model. Rejection, cancellation
and timeout resolve as a rejection to the intent service. Approval uses the fixed
local `/v1/settings/time/confirmation` endpoint and a private request header; the
destination is restricted to loopback HTTP. A failed or ambiguous completion is
reported without retry, since the host might already have executed it.

The intent service rechecks NTP, disables it only after approval, then resolves
missing date/time fields from the execution-time clock. Failed time writes attempt
to restore the prior enabled NTP state. System authorization remains separate.
This initial bridge covers deterministic BeforeRun time requests; raw MCP calls
remain fail-closed when NTP is enabled and cannot approve via model arguments.

## Regression

- `tests/sdk/test_time_confirmation.py`: event lifecycle, timeout/cancel rejection,
  private token transport, fixed destination and hook protocol validation.
- `tests/sdk/test_run_lifecycle.py`: a real RunManager parks, resumes exactly once,
  rejects the wrong approval ID and never calls the model.
- `tests/sdk/plugins/test_hooks.py`: plugin ownership and observation redaction.
- Intent repository: `tests/test_time_confirmation.py` covers host state,
  rollback, ticket identity/expiry/replay and HTTP continuation.
- BiBit worktree: `TimeConfirmation.test.tsx` covers contextual buttons and
  existing permission decisions; the UI uses its usual approval card, not a new
  OS modal dialog.

No real host clock or NTP change is needed for these tests. Coordinated deployment
and device UI verification are still required before calling the feature deployed.
