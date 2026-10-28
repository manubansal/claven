import { CONTACT_EMAIL, OPERATOR_NAME } from "@/lib/constants";

export const LAST_UPDATED = "October 27, 2026";

const content = `# Terms of Service

**Last updated:** ${LAST_UPDATED}

These terms govern your use of Claven, a Gmail labeling service operated by ${OPERATOR_NAME}. By signing in to Claven, you agree to them.

## The service

Claven connects to your Gmail account with your permission and applies labels to your messages according to your rules and the people you have emailed. In Gatekeeper or Sunset mode, it also moves messages from unknown senders out of your inbox (Gatekeeper right away; Sunset after a timer, if they are still unread and unstarred). How Claven handles your data is described in the [Privacy Policy](/privacy).

## Your account

You must have the right to use the Gmail account you connect. You are responsible for the rules and settings you choose, including the inbox mode you pick. You can disconnect Claven at any time from the dashboard, or revoke its access from your Google Account.

## Acceptable use

Do not use Claven to break the law, to access accounts you are not authorized to use, or to interfere with or overload the service.

## Availability

Claven is provided free of charge during its early-access period. We may change, suspend, or discontinue the service, and we will give reasonable notice before discontinuing it where we can.

## No warranty

Claven is provided "as is", without warranties of any kind. Labeling may be delayed, incomplete, or incorrect. Claven only changes labels and inbox placement; it never deletes your email, and everything it does can be undone in Gmail.

## Limitation of liability

To the extent permitted by law, ${OPERATOR_NAME} is not liable for indirect, incidental, or consequential damages, or for any loss of data or missed email, arising from your use of Claven.

## Termination

You can stop using Claven at any time and ask for your data to be deleted, as described in the Privacy Policy. We may suspend accounts that violate these terms.

## Changes

We may update these terms. If we make material changes, we will update the date above and notify signed-in users before the changes take effect.

## Contact

Questions about these terms: ${CONTACT_EMAIL}
`;

export default content;
