"""
The one Jinja environment, and the filters every template can rely on.

    api/*.py ──> templates.TemplateResponse(...) ──> templates/*.html

WHY IT IS SHARED. This started life inside ``api/storefront.py``. The moment a
second router needed to render HTML, that meant two environments, two template
directories to keep in step, and a ``| media`` filter that existed in one of
them — so a dashboard image would render a raw database path and nobody would
know why until a seller complained.

A filter registered here is available to every template, forever. That is the
whole point: a template cannot forget to apply something it never has to apply.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _register_filters() -> None:
    """
    Attach our filters to the environment.

    Imported inside the function rather than at module scope: ``services.media``
    imports from ``services.scraper``, and a top-level import here would drag
    the scraper stack into every module that merely wants to render a page.
    """
    from app.services.media import absolute_url, public_url

    # `| media` turns a stored relative path into a servable URL and passes a
    # legacy absolute URL through untouched.
    templates.env.filters["media"] = public_url

    # `| media_abs` is the same thing fully qualified, for og:image and anything
    # else read by a crawler that has no origin to resolve a relative path
    # against. Getting this wrong costs a link preview with no picture.
    templates.env.filters["media_abs"] = absolute_url


_register_filters()
