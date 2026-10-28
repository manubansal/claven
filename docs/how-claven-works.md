# How Claven Works

Claven automatically labels your Gmail inbox messages as known (`c/k`) or unknown (`c/u`) based on your Sent mail history. If you've ever emailed someone, they're a known sender. Everyone else is unknown.

This lets you focus on mail from people you know and deal with the rest on your own schedule.

## Why Claven

Your inbox is a chronological stream — newest on top, oldest buried. If you process it that way, you spend your attention on whatever arrived most recently, not on what matters most. A marketing email from five minutes ago sits above a question from your manager sent an hour ago.

**Your attention is your most precious resource.** Claven is designed around this principle. Instead of processing email in the order it arrived, Claven gives you the tools to process it **semantically** — by who sent it, what kind of attention it needs, and how you naturally think about your relationships.

### People, not messages

Email is a conversation with people, but your inbox treats it as a list of messages. Claven flips this: it organizes your inbox around **senders**. Known senders are people you have a relationship with. Unknown senders are everyone else. This single distinction lets you immediately separate mail that deserves your attention from mail that can wait.

### Triage by trust, not by time

When you sit down with your inbox, the first question isn't "what arrived most recently?" — it's "who needs something from me?" Claven's known/unknown split answers the first-order triage question automatically. Gatekeeper mode takes it further by keeping unknown-sender messages out of your inbox entirely, so you only see mail from people you know. Sunset mode is the gentler version: everything reaches your inbox, and unknown-sender mail you ignore files itself away.

### Service by sender

The top known senders list shows who has the most unread messages waiting for you. Processing email sender-by-sender is more effective than processing chronologically because you have the full context for that person in your head — their project, their communication style, what you owe them. You also cut your unread count the fastest by servicing the sender with the most messages first.

### Don't get DDoS'd

Every email in your inbox demands a slice of your attention — even if it's just to decide it doesn't matter. Unknown senders, marketing blasts, cold outreach, automated notifications: they all compete for the same finite resource as a message from your team or your most important client. At scale, this is a denial-of-service attack on your attention. The noise drowns out the signal, and the cost isn't spam — it's the important email you missed, the request you forgot to respond to, the relationship you let slip.

No solution to this problem exists in Gmail. Spam filters catch the obvious junk, but everything else lands in one undifferentiated stream. Claven puts guards in place so you **cannot be overwhelmed**. Known-sender separation ensures that the people who matter to you are never buried under noise. Gatekeeper and Sunset modes keep the noise out of sight. You process it on your schedule, in batches, without the risk of missing something that matters.

### Your sent mail is the signal

Most email tools ask you to configure rules, maintain lists, or train a classifier. Claven doesn't. Your Sent mail is the ground truth for who matters to you. If you've emailed someone, they're known. This requires no configuration, no maintenance, and no training data. The signal is already in your mailbox.

## What happens when you connect

Three scans run in sequence:

### 1. Sent scan

Reads your entire Sent mail to build a list of everyone you've ever emailed. This is your known senders list. Each processed sent message is marked with a hidden `c/s` label so it's not reprocessed on future runs.

The sent scan always completes before any labeling begins. This ensures the known senders list is complete before any decisions are made.

### 2. Relabel scan

Checks for messages that were previously labeled `c/u` but whose sender is now known (e.g., you replied to them since the last scan). For each newly discovered known sender, finds their `c/u` messages and atomically swaps the label to `c/k`.

This fixes mislabeled messages without requiring a full rescan. Each sender is processed individually — if the scan is interrupted, remaining senders are picked up on the next run.

### 3. Label scan (inbox scan)

Goes through every unlabeled message and applies a label:
- `c/k` if the sender is in your known senders list
- `c/u` if the sender is not

The scope can be set to **Inbox** (default — only labels inbox messages) or **All mail** (labels every message in your account). The label scan only processes messages that don't already have either label.

### After the initial scans

**Live labeling** begins. New incoming mail is labeled automatically as it arrives via Gmail push notifications. If any scan fails or is interrupted, it automatically resumes on the next dashboard load.

## Inbox modes

The inbox mode decides what happens to unknown-sender mail after it is labeled:

| Mode | What happens to unknown-sender mail |
|---|---|
| **Label only** | Stays in your inbox with its label. Nothing is archived. |
| **Gatekeeper** (default) | Archived as soon as it is labeled. Your inbox only shows mail from people you know. |
| **Sunset** | Stays in your inbox for a timer you pick (4h, 12h, 24h, or 3 days). If it is still unread and unstarred when the timer runs out, it is archived. Opening or starring it keeps it in your inbox. |

Sunset gives you a chance to skim every message in your normal inbox, by sender, subject and preview, without visiting a separate label. Whatever you ignore is filed away for you.

While a message waits, it carries a countdown label saying when it will be archived, such as `c/4h`: gone within four hours. The countdown label takes the place of `c/u`, since a countdown already means the sender is unknown. Countdowns go hour by hour. A new message starts at the timer you picked (`c/24h` for the 24-hour timer, `c/72h` for 3 days) and keeps it for its first hour, then steps down to `c/23h`, `c/22h`, … `c/1h`, the last hour. So if you look through your inbox often, the messages showing the full timer are the ones that arrived since you last looked; anything lower you have already seen. The colour runs from calm to urgent: blue above 12 hours, yellow to 5 hours, orange to 2 hours, red in the last hour. In the Gmail sidebar, a countdown label is listed only while it has unread mail. A new message gets its countdown as soon as it is labeled, and countdowns step down every 15 minutes. Reading or starring the message swaps its countdown label back to `c/u`. If you leave Sunset mode, Claven puts `c/u` back on waiting mail and deletes the countdown labels. Claven checks for expired mail every 15 minutes, so an expired message can stay up to 15 minutes past its timer. When you switch to Sunset, mail already in your inbox gets a full timer from the moment you switch.

Archived mail is never deleted. It stays in All Mail with its `c/u` label (Sunset swaps the countdown back to `c/u` when it archives).

## Labels are on messages, not threads

Gmail labels are applied to **individual messages**, not threads. A single thread (email conversation) can contain messages with different labels. For example:

- A message from someone you haven't emailed yet arrives and gets labeled `c/u`
- You reply to them — they're now a known sender
- Their next message in the same thread gets labeled `c/k`

The thread now has both labels. This is expected behavior. Gmail shows a thread in your inbox as long as any message in it has the INBOX label.

## Gmail labels managed by Claven

Claven keeps its labels short and groups them under one `c` label in your Gmail sidebar:

| Label | Meaning | Colour |
|---|---|---|
| `c/k` | Known sender | Green |
| `c/u` | Unknown sender | Grey |
| `c/72h` … `c/13h` | Sunset: archived within that many hours | Light blue |
| `c/12h` … `c/5h` | Sunset: archived within that many hours | Yellow |
| `c/4h` … `c/2h` | Sunset: archived within that many hours | Orange |
| `c/1h` | Sunset: archived within the hour | Red |
| `c/s` | Sent message already scanned (hidden) | — |

Earlier versions used longer names (`known-sender`, `unknown-sender`, `sunset/4h left`, …) and a `c/3d` step (now `c/72h`). Claven renames those in place the next time it runs, so every message keeps its label. If you change a label's colour in Gmail, Claven leaves your choice alone.

### Labels added

| Label | Added by | When | To which messages |
|---|---|---|---|
| `c/s` | Sent scan | After extracting recipients from a sent message | Sent messages, after their recipients are saved to the database |
| `c/k` | Label scan | When an unlabeled message's sender is in the known senders list | Unlabeled inbox messages (or all mail, depending on scope) |
| `c/u` | Label scan | When an unlabeled message's sender is NOT in the known senders list | Unlabeled inbox messages (or all mail, depending on scope) |
| `c/72h` … `c/1h` | Live labeling, then the Sunset sweep | When unknown-sender mail is labeled in Sunset mode, then every 15 minutes | Unread, unstarred unknown-sender messages in the inbox, in place of `c/u`. One countdown label per message, moved to the next step as time runs down |

### Labels swapped

| From | To | By | When |
|---|---|---|---|
| `c/u` | `c/k` | Relabel scan | When a sender was previously unknown but is now in the known senders list (e.g., you replied to them). Both labels are swapped atomically in a single API call — there is no moment where the message has neither label. A countdown label is swapped to `c/k` the same way. |
| `c/u` | `c/72h` … `c/1h` | Sunset sweep | When unknown-sender mail in the inbox is unread and unstarred in Sunset mode (mail labeled before Sunset was turned on). |
| `c/72h` … `c/1h` | `c/u` | Sunset sweep | When the message is read, starred, or archived, or you leave Sunset mode. |

### Labels removed

| Label | Removed by | When |
|---|---|---|
| `INBOX` | Gatekeeper mode | When an unknown-sender message is labeled. |
| `INBOX` | Sunset mode | When an unknown-sender message is still unread and unstarred after the Sunset timer. |
| `INBOX` | Archive action | When the user clicks "Archive unknown-sender" on the dashboard. Removes the INBOX label from individual messages with the `c/u` label (or a countdown label, in Sunset), moving them out of the inbox. The label itself stays on the message. |

### Labels Claven never modifies

Claven does not add, remove, or modify any Gmail labels other than the ones listed above and the INBOX label (via Gatekeeper, Sunset, or the archive action). In Sunset mode it reads, but never changes, UNREAD and STARRED. It never changes UNREAD, STARRED, IMPORTANT, SPAM, TRASH, or any user-created labels.

## Inbox-only labeling

By default, Claven only labels messages that are currently in your inbox (scope = Inbox). When set to All mail, it labels every message in your account. Archived, trashed, or spam messages are not labeled in Inbox scope. If you archive a labeled message, the label stays on it but Claven won't re-label it if it returns to the inbox through a different path.

## The known senders list

Your known senders list is built from the **To**, **Cc**, and **Bcc** fields of every message in your Sent mail. If you've ever included someone on an email, they're known.

The list updates incrementally:
- When you send a new email, the recipients are added to your known senders list
- The sent scan marks processed messages with a `c/s` label so it doesn't reprocess them

## Top known senders and domains

The dashboard shows two ranked lists: **top known senders** and **top known domains**, both based on unread inbox messages from known senders.

**Top known senders** shows which individuals have the most unread messages waiting for you. Each sender links directly to a Gmail search filtered to their unread inbox messages — click to jump straight to their mail.

**Top known domains** aggregates the same data by email domain. This surfaces organizations that generate the most unread mail. For example, if you have 15 unread messages from various people at `acme.com`, the domain view shows `acme.com: 15` even if no single sender has more than a few.

Both lists update on every dashboard load and are capped at the top 10.

## What "Noise reduced" means

The noise reduced percentage shows how much of your labeled mail came from unknown senders:

```
Noise reduced = unknown-sender messages / total labeled messages
```

This is based on **all messages ever labeled**, including messages that have since been archived or moved out of your inbox. It's a lifetime metric, not a snapshot of your current inbox.

## Archive unknown-sender action

The "Archive unknown-sender" action removes the INBOX label from all messages that have the `c/u` label. This moves them out of your inbox without deleting them — they're still in All Mail.

Because labels are per-message (not per-thread), archiving only affects the specific messages labeled `c/u`. If a thread contains both known and unknown messages, only the unknown ones are archived. The thread stays in your inbox because the known messages still have the INBOX label.

The action shows progress as it works through your messages and can be cancelled at any time. Partial progress is kept — cancelled messages stay archived, remaining messages stay in your inbox.

## Dashboard metrics

| Metric | What it measures |
|---|---|
| Allmail labeled | Total messages with known-sender or unknown-sender label (all mail, not just inbox) |
| Allmail known-sender | Messages labeled known-sender (all mail) |
| Allmail unknown-sender | Messages labeled unknown-sender (all mail) |
| Inbox unlabeled | Inbox messages without either label — these still need processing |
| Last fetched | When the system last checked for unlabeled messages |
| Last labeled | When the system last applied a label to a message |
| Newest email | Date of the newest message in your inbox |
| Newest labeled | Date of the newest message that has a label |

"Allmail" counts include archived messages because `labels.get()` counts all messages with a label regardless of location. In practice this is accurate because Claven only applies these labels to inbox messages — the counts only diverge if you archive labeled messages.

## When labels might be wrong

Labels are applied once and not automatically reconsidered. There are cases where a message could have the wrong label:

- **You email someone after their message was labeled**: Their inbox message was labeled `c/u` before you replied. After you reply, they become a known sender, but the existing label isn't updated. A future relabel (removing all labels and rescanning) will fix this.

- **Multiple workers scanning simultaneously**: This was a bug that has been fixed. The sent scan now always completes before the inbox scan starts, ensuring the known senders list is complete.

If you suspect labels are wrong, you can remove all `c/k` and `c/u` labels and let the system rescan. The known senders list (from Sent mail) is preserved — only the inbox labels are reapplied.

## Best practices

### Pick the inbox mode that fits how you read mail

Gatekeeper is on by default. It moves unknown-sender messages out of your inbox as they're labeled, so your inbox only shows mail from people you know. You don't need to manually archive — just check All Mail periodically for anything you missed.

If you'd rather see everything once, use Sunset. Skim new mail as usual; open or star anything from an unknown sender that matters, and let the rest expire. A shorter timer keeps your inbox tighter; a longer one suits days when you check mail less often.

### Process unknown-sender mail in batches

Don't check unknown-sender messages one by one as they arrive. Instead, set aside time (daily or weekly) to go through your unknown-sender messages in All Mail. You'll find:
- Newsletters and marketing you can unsubscribe from
- Legitimate contacts you should reply to (which adds them to known senders)
- Spam that Gmail's filter missed

### Reply to promote senders

The simplest way to make someone a known sender is to reply to them. Once you reply, Claven picks up the new recipient on the next sent scan and relabels their future messages as known-sender. Their existing unknown-sender messages get relabeled too.

### Use Gmail filters alongside Claven

Claven handles the known/unknown split. Gmail filters handle everything else. Good combinations:
- Gmail filter to auto-label newsletters → Claven won't touch messages that already have your labels
- Gmail filter to star messages from VIPs → stars are preserved regardless of Claven labels
- Gmail filter to skip inbox for certain senders → Claven only labels inbox messages (in default scope)

### Reset sent scan after major email changes

If you've migrated email accounts, merged mailboxes, or sent a large batch of emails, use the "Reset" button on the sent scan to rebuild your known senders list from scratch. This ensures no recipients are missed.

### Scope: Inbox vs All Mail

- **Inbox** (default): only labels messages currently in your inbox. Best for most users — fast, focused.
- **All Mail**: labels every message in your account. Use this for a one-time cleanup of your entire mailbox. Switch back to Inbox afterwards to keep scans fast.

### Use top known senders to prioritize

The "Top known senders" section shows which known senders have the most unread messages in your inbox right now. Use it to:
- Spot senders you're falling behind on — a high count means you owe them attention
- Identify senders whose volume is noise even though they're "known" (e.g., automated notifications from a service you once emailed) — consider demoting them or filtering in Gmail
- Quickly jump to the sender with the most unread by copying their email and searching in Gmail: `from:alice@example.com is:unread in:inbox`. Servicing the top sender first cuts your unread known-sender count the fastest. Processing by sender is also more efficient than processing by recency — you have the full context for that sender in your head while you work through their messages

### Check the activity log

The activity log at the bottom of the dashboard shows what Claven has been doing. If something looks wrong (repeated errors, unexpected cancellations), the log will tell you what happened. Error codes are clickable — copy them for support.
