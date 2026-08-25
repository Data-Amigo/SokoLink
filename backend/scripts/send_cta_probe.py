"""
Send a plain link AND a native CTA button, to find out which opens where.

    python scripts/send_cta_probe.py 2547XXXXXXXX [slug]

    plain body link  ──▶ ─┐
                          ├──▶ same handset, two taps, one answer
    CTA URL button   ──▶ ─┘

THE QUESTION. A plain URL in a message body is text WhatsApp linkifies; tapping
it hands an intent to the operating system, which some Android skins (MIUI in
particular) intercept and route to the default browser. A CTA URL button is a
native interactive component instead, so there may be no intent to intercept.

That is a hypothesis, not a documented guarantee — neither Twilio nor Meta
promises which browser a given OEM lands on. So this sends BOTH, seconds apart,
to the same phone. Two taps and the question is answered by evidence rather
than by anyone's confident paragraph, mine included.

IT NEVER READS APP_BASE_URL. The first version of this script did, and locally
that is http://localhost:8000 — so it would have sent a button pointing at a
server on the developer's laptop, which no phone can reach. The production host
is a constant here precisely because the whole point is what a real handset on
a mobile network does.

THE BUTTON URL IS A STATIC BASE PLUS A VARIABLE TAIL. WhatsApp rejects a URL
button whose address is entirely a placeholder; the domain must be fixed at
template-approval time, with only a suffix varying per message. So the template
carries `.../shop/{{1}}` and the slug is the variable.

WHAT THIS DOES NOT DO. It does not add buttons to the product. It is a probe,
deleted once it has answered.
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

#: The deployed host, NOT settings.app_base_url — see the module docstring.
#: A phone cannot reach a laptop's localhost, and that is exactly the mistake
#: this constant exists to make impossible.
PROD_BASE = "https://sokolink-production.up.railway.app"

#: Bumped when the template's shape changes, so a run never silently reuses a
#: template built to the older, rejected shape.
FRIENDLY_NAME = "biashara_shop_cta_v2"


def find_or_create_template(client: httpx.Client) -> str:
    """
    The content SID for our CTA template, created on first use.

    Args:
        client: An authenticated Twilio client.

    Returns:
        The content SID to send with.

    Raises:
        SystemExit: Carrying Twilio's own message, which names the real problem.
    """
    listed = client.get(CONTENT_API, params={"PageSize": 100})
    if listed.status_code == 200:
        for item in listed.json().get("contents", []):
            if item.get("friendly_name") == FRIENDLY_NAME:
                print(f"  reusing template {item['sid']}")
                return str(item["sid"])

    payload = {
        "friendly_name": FRIENDLY_NAME,
        "language": "en",
        "variables": {"1": "vitabu-bora"},
        "types": {
            "twilio/call-to-action": {
                "body": "Your shop is live. Tap the button to see what a buyer sees.",
                "actions": [
                    {
                        "type": "URL",
                        "title": "Open shop",
                        # Static domain, variable tail — WhatsApp requires it.
                        "url": f"{PROD_BASE}/shop/{{{{1}}}}",
                    }
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
    Send the plain link, then the CTA button, to one number.

    Args:
        to: Destination, digits with country code.
        slug: Which shop to open.

    Returns:
        A process exit code.
    """
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    sender = settings.twilio_whatsapp_number
    if not (sid and token and sender):
        print("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER must be set.")
        return 1

    url = f"{PROD_BASE}/shop/{slug}"
    dest = f"whatsapp:+{to.lstrip('+')}"
    src = f"whatsapp:+{sender.lstrip('+')}"
    auth = (sid, token)

    print(f"Shop URL : {url}")
    print(f"To       : {dest}\n")

    ok = True
    with httpx.Client(auth=auth, timeout=30) as client:
        # 1 — the plain link, identical to pasting it by hand. The control.
        plain = client.post(
            MESSAGES_API.format(sid=sid),
            data={"To": dest, "From": src, "Body": f"TEST 1 of 2 — plain link:\n{url}"},
        )
        if plain.status_code in (200, 201):
            print(f"  1. plain link  sent  ({plain.json().get('status')})")
        else:
            ok = False
            print(f"  1. plain link  FAILED ({plain.status_code}):\n{plain.text}\n")

        # 2 — the same URL as a native button. The variable.
        content_sid = find_or_create_template(client)
        button = client.post(
            MESSAGES_API.format(sid=sid),
            data={
                "To": dest,
                "From": src,
                "ContentSid": content_sid,
                "ContentVariables": json.dumps({"1": slug}),
            },
        )
        if button.status_code in (200, 201):
            print(f"  2. CTA button  sent  ({button.json().get('status')})")
        else:
            ok = False
            # An account that cannot send interactive content and a sandbox
            # refusing an unjoined number are different problems entirely.
            print(f"  2. CTA button  FAILED ({button.status_code}):\n{button.text}")

    if ok:
        print("\nOn the phone that was opening Chrome, tap message 1, then message 2.")
        print("For each, check:")
        print("  back lands straight in the chat   -> WhatsApp's own WebView")
        print("  Chrome is a separate app in recents -> it left WhatsApp")
        print("\nIf 1 leaves and 2 does not, the button is the fix.")
        print("If both leave, the handset is overriding and we need Flows.")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "vitabu-bora"))
