"""
Send a native WhatsApp CTA URL button, to find out where it actually opens.

    python scripts/send_cta_probe.py 2547XXXXXXXX [slug]

    Content template (twilio/call-to-action) ──▶ Messages.json ──▶ a real handset

THE QUESTION THIS ANSWERS. A plain URL in a message body is text that WhatsApp
linkifies, and tapping it hands an intent to the operating system — which on
some Android skins (MIUI especially) is intercepted and routed to the default
browser. A CTA URL button is a native interactive component instead, and the
claim is that WhatsApp opens it in its own WebView rather than delegating.

THAT CLAIM IS UNVERIFIED AND THIS SCRIPT EXISTS TO TEST IT, not to assume it.
Twilio's docs describe the component; they do not promise which browser a given
Android OEM ends up using. The only evidence that settles it is a thumb on the
phone that is currently failing.

It prints every Twilio error in full. The interesting failures here are
specific and fixable — an unjoined sandbox recipient, a content type the
account cannot send, a session window that has closed — and each needs a
different response, so "it failed" is not a useful thing to report.

WHAT IT DOES NOT DO. It does not add CTA buttons to the product. This is a
probe: one message, one question, thrown away once answered.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402

CONTENT_API = "https://content.twilio.com/v1/Content"
MESSAGES_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

#: Reused across runs so repeated probes do not litter the account with
#: near-identical templates.
FRIENDLY_NAME = "biashara_shop_cta_probe"


def find_or_create_template(auth: tuple[str, str], shop_url: str) -> str:
    """
    Get the content SID for our CTA template, creating it the first time.

    Args:
        auth: Twilio account SID and auth token.
        shop_url: The absolute URL the button should open.

    Returns:
        The content SID to send with.

    Raises:
        SystemExit: With Twilio's own message, which names the actual problem.
    """
    with httpx.Client(auth=auth, timeout=30) as client:
        existing = client.get(CONTENT_API, params={"PageSize": 100})
        if existing.status_code == 200:
            for item in existing.json().get("contents", []):
                if item.get("friendly_name") == FRIENDLY_NAME:
                    print(f"  reusing template {item['sid']}")
                    return str(item["sid"])

        payload = {
            "friendly_name": FRIENDLY_NAME,
            "language": "en",
            "variables": {"1": shop_url},
            "types": {
                "twilio/call-to-action": {
                    "body": ("Your shop is ready. Tap below to see what buyers see."),
                    "actions": [
                        {"type": "URL", "title": "Open my shop", "url": "{{1}}"},
                    ],
                }
            },
        }
        created = client.post(
            CONTENT_API,
            headers={"Content-Type": "application/json"},
            content=json.dumps(payload),
        )
        if created.status_code not in (200, 201):
            raise SystemExit(
                f"Could not create the content template ({created.status_code}):\n{created.text}"
            )
        sid = str(created.json()["sid"])
        print(f"  created template {sid}")
        return sid


def main(to: str, slug: str) -> int:
    """
    Send one CTA button message.

    Args:
        to: Destination number, digits only, with country code.
        slug: Which shop the button should open.

    Returns:
        A process exit code.
    """
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    sender = settings.twilio_whatsapp_number
    if not (sid and token and sender):
        print("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER must be set.")
        return 1

    shop_url = f"{settings.app_base_url}/shop/{slug}"
    print(f"Shop URL : {shop_url}")
    print(f"To       : whatsapp:+{to}\n")

    content_sid = find_or_create_template((sid, token), shop_url)

    data = {
        "To": f"whatsapp:+{to.lstrip('+')}",
        "From": f"whatsapp:+{sender.lstrip('+')}",
        "ContentSid": content_sid,
        "ContentVariables": json.dumps({"1": shop_url}),
    }
    sent = httpx.post(MESSAGES_API.format(sid=sid), data=data, auth=(sid, token), timeout=30)
    if sent.status_code not in (200, 201):
        # The sandbox refusing an unjoined number, and an account that cannot
        # send interactive content, are different problems with different fixes.
        print(f"\nSEND FAILED ({sent.status_code}):\n{sent.text}")
        return 1

    body = sent.json()
    print(f"\nSent. sid={body.get('sid')} status={body.get('status')}")
    print("\nNow tap the BUTTON on the handset that was opening Chrome, and check:")
    print("  - does back land you straight in the chat?  -> WhatsApp's own WebView")
    print("  - does Chrome appear as a separate app?     -> it left WhatsApp")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "vitabu-bora"))
