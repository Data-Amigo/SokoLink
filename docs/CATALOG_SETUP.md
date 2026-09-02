# Native catalogue setup (Multi-Product Messages)

The chat can show a buyer real product cards they tap into a WhatsApp cart, sent
as a **Multi-Product Message (MPM)**. An MPM references items in a **Meta
Commerce catalogue**, so a shop's published products are mirrored there and kept
in step. Until `WHATSAPP_CATALOG_ID` is set the chat simply falls back to list
menus — nothing breaks, the native cards are just off.

## What the code already does

- **Sync.** Publishing a product enqueues a `catalog_sync` job; the worker
  upserts it into the catalogue (or removes it when it is unpublished or sells
  out). No product waits on a Graph call to go live. See `services/catalog.py`
  and the `catalog_sync` handler in `jobs_handlers.py`.
- **Show.** A buyer who sends `catalogue` (or taps the option) gets an MPM of the
  shop's published products. See `bot/presentation._catalogue`.
- **Order.** When the buyer sends their WhatsApp cart back, Meta delivers an
  `order` message; the webhook routes it to `bot.handle_order`, which rebuilds
  the cart and hands off to the **ordinary checkout** (name → delivery → M-Pesa).
  There is no second, parallel order path.

Every product is keyed by a derived `retailer_id` of `soko_<product_id>` — no
column, no migration, and it reverses cleanly on the way back.

## What you do in Meta (once)

1. **Create a catalogue.** In [Commerce Manager](https://business.facebook.com/commerce)
   create a catalogue of type **Ecommerce** under your Business, or reuse an
   existing one.
2. **Connect it to the WhatsApp Business Account** so messages may reference it:
   WhatsApp Manager → your WABA → **Catalogue** → connect the catalogue above.
3. **Turn on the cart** for the number: WhatsApp Manager → Commerce settings →
   allow the cart. Without this the MPM shows products but the buyer cannot add
   them.
4. **Copy the catalogue id** (Commerce Manager → Catalogue → Settings) into
   `WHATSAPP_CATALOG_ID`.
5. **Permissions on the token.** The `catalog_sync` job writes items with the
   Graph `items_batch` endpoint, which needs `catalog_management` in addition to
   `whatsapp_business_messaging`. Use a System User access token that has both,
   or extend the token in `WHATSAPP_ACCESS_TOKEN`.
6. **Public images.** Catalogue items carry `image_url`, built from
   `APP_BASE_URL`. It must be publicly reachable — a `localhost` value cannot be
   fetched by Meta, so this only shows real images in a deployed environment.

## Verifying

- Publish a priced product, let the worker run, and check the item appears in
  Commerce Manager under `soko_<id>`.
- From a test number, send `catalogue` to the shop and confirm the product cards
  render; add one to the cart and send it back; confirm the bot replies asking
  for a name (the checkout has begun).

## Not built (deliberately)

- **WhatsApp Flows** as the checkout surface — a data-driven Flow needs an
  encrypted Flow endpoint and a published Flow, and MPM + the existing checkout
  already close the buy loop. Revisit if the richer form is wanted.
