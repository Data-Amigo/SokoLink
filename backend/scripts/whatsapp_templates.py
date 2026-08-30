"""
Register and inspect the WhatsApp templates the product needs.

    python scripts/whatsapp_templates.py list
    python scripts/whatsapp_templates.py create

WHY A TEMPLATE EXISTS AT ALL, when the bot can already send a link. Meta's
in-app browser opens links from CTA buttons on APPROVED TEMPLATES. A link inside
a free-form message — which is every reply the bot makes — is handed to the
device's default browser instead. That is documented behaviour, and it is why a
plain link and a native CTA button both left WhatsApp when we tested on a real
handset: free-form on one side, and an account below the eligibility threshold
on the other.

THE THRESHOLD IS A MESSAGING TIER. The in-app browser needs a limit of at least
1,000 business-initiated conversations per day. Business verification moves an
account from 250 to 1,000, which is why it looked like verification was the gate.

APPROVAL IS NOT INSTANT. A submitted template sits at PENDING until Meta reviews
it — usually minutes for a utility template, occasionally longer. Sending one
before it is APPROVED fails with an error that does not always say that is why,
so `list` exists to check before blaming the code.

This script needs the same four WhatsApp variables the deployed app uses. They
live in Railway; put them in .env to run it from here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.whatsapp_cloud import (  # noqa: E402
    CloudApiError,
    create_shop_template,
    list_templates,
)


def show() -> int:
    """Print every template and, more importantly, its review status."""
    try:
        templates = list_templates()
    except CloudApiError as exc:
        print(f"Could not list templates:\n  {exc}")
        return 1

    if not templates:
        print("No templates on this account yet. Run `create`.")
        return 0

    print(f"{len(templates)} template(s):\n")
    for item in templates:
        status = str(item.get("status", "?"))
        mark = {"APPROVED": "ok ", "PENDING": "...", "REJECTED": "NO "}.get(status, "?  ")
        print(f"  {mark} {item.get('name'):<32} {status:<10} {item.get('category', '')}")
        if status == "REJECTED" and item.get("rejected_reason"):
            print(f"        reason: {item['rejected_reason']}")
    return 0


def create() -> int:
    """Submit the shop-link template for review."""
    name = settings.whatsapp_shop_template
    base = settings.app_base_url

    if "localhost" in base or "127.0.0.1" in base:
        # Meta reviews the DOMAIN. Submitting localhost burns the template name
        # on a rejection, and template names cannot be reused immediately.
        print(f"APP_BASE_URL is {base!r} — Meta cannot approve a localhost domain.")
        print("Set it to the public host before creating the template.")
        return 1

    existing = {t.get("name") for t in list_templates()} if settings.whatsapp_access_token else set()
    if name in existing:
        print(f"{name!r} already exists. Run `list` to see its status.")
        return 0

    print(f"Submitting {name!r} with button URL {base}/shop/{{{{1}}}}\n")
    try:
        result = create_shop_template(name, base)
    except CloudApiError as exc:
        print(f"Rejected:\n  {exc}")
        return 1

    print(f"Submitted. id={result.get('id')} status={result.get('status')}")
    print("\nIt is PENDING until Meta reviews it — usually minutes for a utility")
    print("template. Run `list` to check before testing, because sending an")
    print("unapproved template fails with an error that does not say so.")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "list"
    if command == "create":
        raise SystemExit(create())
    if command == "list":
        raise SystemExit(show())
    print(__doc__)
    raise SystemExit(2)
