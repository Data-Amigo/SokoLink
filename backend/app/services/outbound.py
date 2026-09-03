"""
Putting one bot ``Reply`` on the wire, as the right Cloud API call(s).

    Reply ──▶ send_reply(to, reply) ──▶ send_text / send_image+buttons / …

WHY THIS IS ITS OWN MODULE. Two callers put replies on the wire and they must
not drift. The webhook answers the person who just wrote in; the worker sends a
seller the result of a parse that took too long to do in the webhook. The
mapping — a link is a cta_url, an image with buttons is two messages because
Meta cannot caption an image with buttons, a plain body is send_text — is the
same for both, so it lives in one place rather than being copied into each.

The senders are called through the ``whatsapp_cloud`` module (not imported by
name) on purpose: a test patches ``whatsapp_cloud.send_text`` and this picks the
patch up, because there is no second binding to go stale.
"""

from __future__ import annotations

from app.config import settings
from app.services import whatsapp_cloud
from app.services.bot import Reply


def send_reply(to: str, reply: Reply) -> None:
    """
    Deliver one :class:`Reply` to ``to``, swallowing a provider failure.

    Args:
        to: The recipient's number, bare digits with country code.
        reply: What to send.

    Notes:
        THE FAILURE IS SWALLOWED ON PURPOSE. A send that fails must not fail the
        caller. In the webhook, raising would return a non-200, Meta would
        redeliver, and the whole conversation step would run twice. In the
        worker, it would mark a completed parse as failed. The inbound message
        is already recorded either way — a missed send is recoverable (the
        seller sends ``orders`` / ``drafts`` and sees the truth), a replayed one
        is not.

        AN IMAGE WITH BUTTONS IS TWO MESSAGES. Meta cannot draw buttons on an
        image, so the photo goes bare and the card with the choices follows.
    """
    try:
        if reply.product_list:
            # A Multi-Product Message, if this deployment has a catalogue; if
            # not, the body still carries the shop and the buyer is not stranded.
            header, sections = reply.product_list
            catalog_id = settings.whatsapp_catalog_id
            if catalog_id:
                whatsapp_cloud.send_product_list(to, header, reply.body, catalog_id, sections)
            else:
                whatsapp_cloud.send_text(to, reply.body)
        elif reply.link:
            url, label = reply.link
            whatsapp_cloud.send_cta_url(to, reply.body, url, label=label)
        elif reply.template:
            name, body_params, button_param = reply.template
            whatsapp_cloud.send_template(
                to, name, body_params=body_params, button_param=button_param
            )
        elif reply.media_url:
            if reply.buttons:
                whatsapp_cloud.send_image(to, reply.media_url)
                whatsapp_cloud.send_buttons(to, reply.body, reply.buttons)
            else:
                whatsapp_cloud.send_image(to, reply.media_url, caption=reply.body)
        elif reply.buttons:
            whatsapp_cloud.send_buttons(to, reply.body, reply.buttons)
        elif reply.rows:
            whatsapp_cloud.send_list(to, reply.body, reply.list_label, reply.rows)
        else:
            whatsapp_cloud.send_text(to, reply.body)
    except whatsapp_cloud.CloudApiError:
        return
