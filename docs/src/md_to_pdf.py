"""
Turn any project Markdown document into a styled, readable PDF.

    python docs/src/md_to_pdf.py docs/CODEBASE.md --accent slate

    markdown ──> HTML (+ house CSS) ──> headless Chrome ──> PDF

WHY this exists: Markdown is written for diffing, not for reading end to end.
Anything meant to be *read* — the codebase tour, the build log — is easier in a
PDF, and generating it means the PDF can never drift from the source.

WHY headless Chrome rather than a Python PDF library: reportlab and friends make
you lay out every element by hand, and the results look it. Chrome already has a
world-class typesetter; we hand it CSS and let it print.

Reusable on purpose — pass any .md in the repo.
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
from pathlib import Path

import markdown

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Accent palettes, so related documents stay visually distinct without
#: anyone hand-editing CSS.
ACCENTS = {
    "green": ("#047857", "#ecfdf5"),
    "blue": ("#1d4ed8", "#eff6ff"),
    "slate": ("#334155", "#f1f5f9"),
    "purple": ("#6d28d9", "#f5f3ff"),
    "amber": ("#b45309", "#fffbeb"),
}

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)

CSS = """
@page {{ size: A4; margin: 18mm 16mm 16mm; }}
:root {{
  --ink: #16181d; --muted: #5f6672; --line: #dfe3ea;
  --accent: {accent}; --accent-soft: {accent_soft};
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: "Segoe UI", -apple-system, system-ui, Roboto, Helvetica, Arial, sans-serif;
  color: var(--ink); font-size: 10pt; line-height: 1.55; margin: 0;
}}

/* ── Cover ─────────────────────────────────────────────────────────────── */
.cover {{ border-bottom: 3px solid var(--accent); padding-bottom: 14px; margin-bottom: 20px; }}
.cover .eyebrow {{
  font-size: 8pt; letter-spacing: .16em; text-transform: uppercase;
  color: var(--accent); font-weight: 650; margin-bottom: 8px;
}}
.cover .meta {{
  font-size: 9pt; color: var(--muted); margin-top: 8px;
  line-height: 1.5; max-width: 46em;
}}
.cover .meta code {{ font-size: 8pt; }}

/* ── Headings ──────────────────────────────────────────────────────────── */
h1 {{ font-size: 23pt; line-height: 1.12; margin: 0 0 4px; letter-spacing: -.02em; }}
h2 {{
  font-size: 13pt; margin: 24px 0 9px; color: var(--accent);
  border-bottom: 2px solid var(--accent-soft); padding-bottom: 4px;
  break-after: avoid; break-inside: avoid;
}}
h3 {{ font-size: 11pt; margin: 16px 0 6px; break-after: avoid; }}
h4 {{ font-size: 10pt; margin: 12px 0 4px; color: var(--muted); break-after: avoid; }}

p {{ margin: 0 0 8px; }}
ul, ol {{ margin: 0 0 9px; padding-left: 17px; }}
li {{ margin-bottom: 4px; }}
strong {{ font-weight: 650; }}
hr {{ border: 0; border-top: 1px solid var(--line); margin: 20px 0; }}
a {{ color: var(--accent); text-decoration: none; }}

/* ── Tables ────────────────────────────────────────────────────────────── */
table {{
  width: 100%; border-collapse: collapse; margin: 10px 0 14px;
  font-size: 9pt; break-inside: avoid;
}}
th, td {{
  text-align: left; padding: 6px 8px;
  border-bottom: 1px solid var(--line); vertical-align: top;
}}
th {{
  background: #f5f7fa; font-weight: 650; font-size: 8pt;
  text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
}}
tr {{ break-inside: avoid; }}

/* ── Code ──────────────────────────────────────────────────────────────── */
code {{
  font-family: Consolas, "Courier New", monospace; font-size: 8.5pt;
  background: #f1f4f9; padding: 1px 4px; border-radius: 3px;
}}
pre {{
  font-family: Consolas, "Courier New", monospace; font-size: 8pt;
  background: #f7f9fc; border: 1px solid var(--line); border-radius: 5px;
  padding: 10px 12px; line-height: 1.45; margin: 10px 0;
  white-space: pre; overflow-x: auto; break-inside: avoid;
}}
pre code {{ background: none; padding: 0; font-size: inherit; }}

/* ── Blockquotes become callouts ───────────────────────────────────────── */
blockquote {{
  background: var(--accent-soft); border-left: 3px solid var(--accent);
  padding: 9px 13px; margin: 12px 0; break-inside: avoid;
}}
blockquote p {{ margin: 0 0 6px; }}
blockquote p:last-child {{ margin-bottom: 0; }}

footer {{
  margin-top: 26px; padding-top: 9px; border-top: 1px solid var(--line);
  font-size: 8pt; color: var(--muted);
}}
"""


def find_browser() -> str:
    """
    Locate a Chromium-family browser to print with.

    Returns:
        Path to the executable.

    Raises:
        RuntimeError: If none is installed — with the fix, not just the fact.
    """
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "No Chrome or Edge found. Install either, or add its path to "
        "CHROME_CANDIDATES in this file."
    )


def split_title(text: str) -> tuple[str, str, str]:
    """
    Pull the H1 and any leading blockquote out, to render as a cover.

    Keeping the title and the "who this is for" note out of the body flow makes
    the first page read like a document rather than a README.

    Args:
        text: Raw markdown.

    Returns:
        (title, subtitle, remaining markdown).
    """
    lines = text.splitlines()
    title = ""
    subtitle = ""
    start = 0

    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            start = i + 1
            break

    # A blockquote immediately after the H1 is the document's own summary.
    body_start = start
    quote: list[str] = []
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        if not stripped:
            if quote:
                body_start = i + 1
                break
            continue
        if stripped.startswith(">"):
            quote.append(stripped.lstrip("> ").strip())
            body_start = i + 1
        else:
            break

    subtitle = " ".join(q for q in quote if q)
    return title, subtitle, "\n".join(lines[body_start:])


def render(md_path: Path, accent_name: str) -> str:
    """Convert one markdown file into a complete, styled HTML document."""
    raw = md_path.read_text(encoding="utf-8")
    title, subtitle, body = split_title(raw)

    converted = markdown.markdown(
        body,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list"],
        output_format="html5",
    )

    accent, accent_soft = ACCENTS.get(accent_name, ACCENTS["slate"])

    # The subtitle is markdown too — it comes from the document's own leading
    # blockquote, which routinely contains bold and inline code. Escaping it
    # would print literal asterisks and backticks on the cover.
    subtitle_html = ""
    if subtitle:
        rendered = markdown.markdown(subtitle, extensions=["attr_list"])
        # Unwrap the single <p> so the cover styling applies to the text itself.
        rendered = re.sub(r"^<p>|</p>$", "", rendered.strip())
        subtitle_html = f'<div class="meta">{rendered}</div>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{CSS.format(accent=accent, accent_soft=accent_soft)}</style>
</head><body>
<div class="cover">
  <div class="eyebrow">SokoLink</div>
  <h1>{html.escape(title)}</h1>
  {subtitle_html}
</div>
{converted}
<footer>SokoLink &middot; generated from <code>{html.escape(md_path.name)}</code>
&middot; regenerate with <code>python docs/src/md_to_pdf.py {html.escape(str(md_path.name))}</code></footer>
</body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a project Markdown file to PDF.")
    parser.add_argument("source", help="Path to the .md file, relative to the repo root.")
    parser.add_argument(
        "--accent", default="slate", choices=sorted(ACCENTS), help="Accent colour."
    )
    parser.add_argument("--out", default=None, help="Output PDF path.")
    args = parser.parse_args()

    md_path = (REPO_ROOT / args.source).resolve()
    if not md_path.exists():
        md_path = Path(args.source).resolve()
    if not md_path.exists():
        print(f"Not found: {args.source}", file=sys.stderr)
        return 1

    html_doc = render(md_path, args.accent)

    build_dir = REPO_ROOT / "docs" / "src" / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    html_path = build_dir / f"{md_path.stem}.html"
    html_path.write_text(html_doc, encoding="utf-8")

    out_path = Path(args.out) if args.out else REPO_ROOT / "docs" / f"SokoLink_{md_path.stem}.pdf"

    subprocess.run(
        [
            find_browser(),
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out_path}",
            html_path.as_uri(),
        ],
        check=False,
        capture_output=True,
        timeout=180,
    )

    if not out_path.exists():
        print(f"Chrome produced no PDF for {md_path.name}", file=sys.stderr)
        return 1

    size_kb = out_path.stat().st_size / 1024
    print(f"OK  {out_path.name}  ({size_kb:.0f} kb)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
