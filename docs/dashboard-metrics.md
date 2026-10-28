# Dashboard Metrics

Metrics returned by `/api/me` and displayed on the dashboard.

## Design principles

1. **No `resultSizeEstimate`** — every displayed count is exact
2. **Mailbox as source of truth** — label counts and message dates come from Gmail, not DB counters
3. **Batched API calls** — all Gmail calls are bundled into 2-3 batch requests to avoid 429 rate limits
4. **`labelIds` for message-level filtering** — never `q` parameter, which is thread-level in Gmail's search

## Inbox scan metrics

| Metric | Definition | Gmail API call | Exact? | Notes |
|---|---|---|---|---|
| `allmail_labeled_known_count` | Messages with the known-sender label (`c/k`) across all mail | `labels.get(known-sender-id).messagesTotal` | Yes | Single call. Not inbox-scoped, but the label is only applied to inbox messages. |
| `allmail_labeled_unknown_count` | Messages with the unknown-sender label (`c/u`) across all mail | `labels.get(unknown-sender-id).messagesTotal` | Yes | Same as above. |
| `allmail_labeled_total_count` | Sum of known + unknown | Derived from above | Yes | Always equals `known + unknown`. |
| `inbox_unlabeled_first_page_count` | Count of first page of unlabeled inbox messages | `len(messages.list(q=unlabeled_query, maxResults=500).messages)` | Yes, 0-500 | Same query as `scan_inbox`. Used for retrigger decision (> 0 means work remains). |
| `inbox_unlabeled_deep_count` | Total count of unlabeled inbox messages | Paginate `messages.list(q=unlabeled_query)`, count all IDs | Yes | Expensive: ~200-400ms per page of 500. For 10k messages, ~4-8 seconds. |

### Why `allmail_` prefix?

`labels.get().messagesTotal` counts all messages with that label, not just inbox messages. We prefix with `allmail_` to make this explicit. In practice the counts are inbox-accurate because our scan only applies these labels to inbox messages. They diverge if a user archives a labeled message (label stays, message leaves inbox).

### Why `deep_` prefix?

`inbox_unlabeled_deep_count` requires paginating through all matching messages. The `deep_` prefix signals this is an expensive computation, not a single API call. Future expensive metrics should use the same prefix.

### Unlabeled query

The unlabeled query is `in:inbox -label:c/k -label:c/u` (built by `scan.py:_unlabeled_query()`). This is the same query `scan_inbox` uses to find work, so the dashboard count and the scan's exit condition always agree.

## Timestamp metrics

| Metric | Definition | Source | Notes |
|---|---|---|---|
| `newest_mail_at` | `internalDate` of the newest inbox message | `messages.list(labelIds=["INBOX"], maxResults=1)` then `messages.get(format=minimal)` | Gmail returns newest-first. Two API calls (batched). |
| `newest_labeled_at` | `internalDate` of the newest message with any filter label | `messages.list(labelIds=[label_id], maxResults=1)` per label, then `messages.get(format=minimal)` for the newest candidate | Uses `labelIds` (message-level), NOT `q` (thread-level). Gmail's `q` search is thread-oriented: `label:known-sender` returns the newest message in any thread containing a labeled message, even if that message itself has no label. `labelIds` returns only messages that actually have the label. |
| `last_processed_at` | Wall-clock time when the system last applied a label | DB `scan_state.last_processed_at`, set by `touch_last_processed()` | Updated by `scan_inbox` (per batch when applied > 0) and by webhook/poll handlers (when `poll_new_messages` count > 0). |

### `newest_labeled_at` vs `last_processed_at`

These measure different things:
- `newest_labeled_at`: the date of the email that was labeled (when was it sent/received?)
- `last_processed_at`: when our system labeled it (wall-clock time of the server)

If they match closely, the system is keeping up with incoming mail. If `newest_labeled_at` is much newer than `last_processed_at`, new mail is arriving faster than the system processes it — or threading made them appear to diverge (see below).

### Gmail threading caveat

Gmail's `q`-based search (`messages.list(q="label:X")`) is thread-oriented. If message A in a thread has the label, and message B (a newer reply) does not, searching `q="label:X"` returns message B. This made `newest_labeled_at` appear newer than `last_processed_at` even when the system was caught up. Fixed by using `labelIds` parameter which filters at the message level.

## Sent scan metrics

| Metric | Definition | Gmail API call | Exact? |
|---|---|---|---|
| `sent_scanned_count` | Messages with the `c/s` label | `labels.get(sent-scanned-id).messagesTotal` | Yes |
| `sent_total_count` | Total sent messages | `labels.get(SENT).messagesTotal` | Yes |

## General mailbox metrics

| Metric | Definition | Gmail API call | Exact? |
|---|---|---|---|
| `inbox_count` | Total inbox messages | `labels.get(INBOX).messagesTotal` | Yes |
| `unread_count` | Unread inbox messages | `labels.get(INBOX).messagesUnread` | Yes |
| `read_count` | Read inbox messages | `messages.list(labelIds=["INBOX"], q="is:read").resultSizeEstimate` | Approximate |
| `all_mail_count` | Total messages in account | `getProfile().messagesTotal` | Yes |

### Note on `read_count`

`read_count` is the only metric still using `resultSizeEstimate`. There is no `labels.get` equivalent for "read inbox messages" — it requires a query intersection. This metric is informational only and not used for any logic.

## Retrigger logic

When `/api/me` detects `inbox_unlabeled_first_page_count > 0` and `inbox_scan_status == "complete"`, it spawns a background `_run_inbox_scan` thread. The row lock in `try_lock_user_scan` makes concurrent retriggers idempotent.

## API call budget

| Phase | Calls | What |
|---|---|---|
| Batch 1 | 6 | INBOX label, SENT label, labels list, read count, newest inbox msg, profile |
| Batch 2 | 5-7 | sent-scanned label, known-sender label, unknown-sender label, newest mail detail, newest-known msg, newest-unknown msg, unlabeled first page |
| Batch 3 | 1-2 | newest labeled candidate details |
| Sequential | 0-N | unlabeled pagination (N = ceil(unlabeled / 500)) |
| **Total** | **12-15 + pagination** | Down from ~12 sequential calls that hit 429 rate limits |
