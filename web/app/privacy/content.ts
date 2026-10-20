import { CONTACT_EMAIL, OPERATOR_NAME } from "@/lib/constants";

export const LAST_UPDATED = "October 20, 2026";

const content = `# Privacy Policy

**Last updated:** ${LAST_UPDATED}

Claven ("Claven", "we", "us") is a Gmail labeling service operated by ${OPERATOR_NAME}. This policy explains what data Claven accesses, how it is used, stored, and shared, and how you can have it deleted.

## Limited Use disclosure

Claven's use and transfer of information received from Google APIs to any other app will adhere to [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the Limited Use requirements.

## Google permissions Claven requests

When you sign in with Google, Claven asks for:

- **openid** and **userinfo.email** — to identify your account by its email address.
- **gmail.modify** — to read message headers, create Claven's labels, apply and remove those labels on your messages, and (only if you enable auto-archive) move messages from unknown senders out of your inbox.

Claven never sends, deletes, or drafts email on your behalf.

## What data Claven accesses

- **Your email address**, from your Google account.
- **Message metadata** — the From, To, Cc, Bcc, and Subject headers of messages, plus their message IDs and existing label IDs. Claven requests messages in Gmail's metadata format and **never reads message bodies or attachments**.
- **Your Gmail labels**, so Claven can find or create its own labels.
- **Mailbox change notifications** from Gmail, which tell Claven when new mail arrives.

## How Claven uses this data

Claven uses Google user data only to provide the labeling feature you see in the app:

- It scans your Sent mail to build a list of addresses you have written to ("known senders").
- When new mail arrives, it compares the sender against your known senders and your label rules, then applies the matching label.
- If you turn on auto-archive, it removes unknown-sender messages from your inbox (they stay in All Mail with their label).

Claven does **not**:

- sell, rent, or share your data with advertisers, data brokers, or information resellers;
- use your data for advertising, including personalized or retargeted ads;
- use your data to determine credit-worthiness or for lending purposes;
- use your data to develop, improve, or train generalized AI or machine-learning models;
- allow humans to read your data, except with your explicit permission for a specific message, when necessary for security purposes (such as investigating abuse), to comply with applicable law, or when the data has been aggregated and anonymized for internal operations.

## What Claven stores

- your email address and account creation time;
- your Google OAuth access and refresh tokens, **encrypted at rest**;
- the email addresses of people you have sent mail to (your known-senders list);
- processing state: the Gmail history position Claven has processed up to, scan progress counters, and timestamps;
- your settings (scan scope, auto-archive);
- a short activity log shown on your dashboard (the most recent 100 entries).

Claven does not store message bodies, attachments, or subject lines in its database. Message IDs and subject lines may appear in operational server logs, which are kept by our hosting provider for a limited period (normally 30 days) for debugging and security.

## Who processes your data

Claven runs on infrastructure from the following providers, who process data only on our behalf to run the service:

- **Google Cloud** — application hosting, Gmail push notifications, and server logs;
- **Neon** — the Postgres database;
- **Cloudflare** — hosting for the claven.app website.

We do not transfer Google user data to anyone else, except as required by law or as part of a merger, acquisition, or sale of assets with notice to you.

## Security

Data is transmitted over HTTPS. OAuth tokens are encrypted before they are written to the database, and the encryption key is held separately from the database. Access to production systems is restricted to the operator.

## Cookies

Claven uses a session cookie to keep you signed in and a short-lived cookie to protect the Google sign-in flow. Claven does not use analytics or advertising cookies.

## Retention and deletion

Claven keeps your data while your account exists. **Disconnecting** from the dashboard stops Claven from processing your mail but keeps your sign-in credentials, so you can reconnect with one click.

To have your account and all associated data **deleted**, email ${CONTACT_EMAIL} from the address you signed in with. We will delete your tokens, known-senders list, settings, processing state, and activity log within 30 days. Labels Claven applied in Gmail stay in your mailbox; you can remove them in Gmail at any time.

You can also revoke Claven's access at any time from your [Google Account permissions page](https://myaccount.google.com/permissions). Once access is revoked, Claven can no longer read or change your mail.

## Children

Claven is not directed to children under 13, and we do not knowingly collect data from them.

## Changes to this policy

If we make material changes, we will update the date above and notify signed-in users before the changes take effect.

## Contact

Questions or requests: ${CONTACT_EMAIL}
`;

export default content;
