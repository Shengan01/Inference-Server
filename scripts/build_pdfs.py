#!/usr/bin/env python3
"""Build PDF versions of the project documents (docs/pdf/*.pdf).

Markdown is converted to HTML (python-markdown), Mermaid diagrams are rendered
by mermaid.js inside headless Google Chrome, which also prints the PDF.

Requirements: google-chrome (or chromium) on PATH, `pip install markdown`,
internet access for loading mermaid.js from a CDN.

Usage: python3 scripts/build_pdfs.py [file.md ...]
"""
import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "pdf"
DEFAULT_DOCS = [
    "docs/ZADANIE_2_OPIS_I_ARCHITEKTURA.md",
    "docs/ZADANIE_3_IMPLEMENTACJA_I_SPRAWOZDANIE.md",
    "docs/DOKUMENTACJA_TECHNICZNA.md",
    "docs/INSTRUKCJA_URUCHOMIENIA_I_TESTOW.md",
]
MERMAID_JS = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"

CSS = """
@page { size: A4; margin: 16mm 14mm; }
@page wide { size: A4 landscape; margin: 10mm; }
div.diagram p { margin: 0 0 6px 0; font-weight: bold; }
div.diagram { page: wide; break-before: page; break-after: page; }
body { font-family: 'DejaVu Sans', 'Liberation Sans', Arial, sans-serif; font-size: 10pt; line-height: 1.5; color: #1a1a1a; }
h1 { font-size: 20pt; border-bottom: 2px solid #326ce5; padding-bottom: 4px; }
h2 { font-size: 15pt; margin-top: 22px; border-bottom: 1px solid #ccc; padding-bottom: 2px; page-break-after: avoid; }
h3 { font-size: 12pt; margin-top: 16px; page-break-after: avoid; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 8.5pt; page-break-inside: auto; }
tr { page-break-inside: avoid; }
th, td { border: 1px solid #bbb; padding: 4px 6px; vertical-align: top; text-align: left; }
th { background: #eef2fb; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 8.5pt; background: #f3f3f3; padding: 1px 3px; border-radius: 3px; word-break: break-word; }
pre { background: #f6f8fa; border: 1px solid #ddd; border-radius: 4px; padding: 8px; font-size: 8pt; white-space: pre-wrap; word-break: break-word; page-break-inside: avoid; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #326ce5; margin: 8px 0; padding: 2px 12px; color: #444; background: #f7f9fd; }
pre.mermaid { background: #fff; border: 1px solid #ddd; text-align: center; page-break-inside: avoid; white-space: normal; }
pre.mermaid svg { width: 100% !important; max-width: none !important; height: auto; }
div.diagram pre.mermaid { border: none; padding: 0; margin: 0; }
a { color: #1a5fb4; word-break: break-word; }
img { max-width: 100%; border: 1px solid #d0d7de; margin: 4px 0 10px 0; page-break-inside: avoid; }
"""


def render_markdown(text: str) -> str:
    """Markdown -> HTML body; mermaid fences become <pre class="mermaid">."""
    blocks = []

    def stash(match):
        caption = markdown.markdown(match.group(1)).strip() if match.group(1) else ""
        blocks.append('<div class="diagram">' + caption + '<pre class="mermaid">'
                      + html.escape(match.group(2)) + "</pre></div>")
        return f"\n\nMERMAIDBLOCK{len(blocks) - 1}\n\n"

    # An optional bold caption line right before the fence is moved onto the diagram page.
    text = re.sub(r"(?:(\*\*[^\n]+)\n\n)?```mermaid\n(.*?)```", stash, text, flags=re.S)
    # Relative image paths (img/x.png, as used in docs/*.md) -> absolute file URIs for the temp HTML page.
    text = text.replace("](img/", "](" + (ROOT / "docs" / "img").as_uri() + "/")
    # python-markdown needs 4-space indents for nested lists; the sources use 3 (GitHub style).
    text = re.sub(r"^((?: {3})+)(?=(?:[*+-]|\d+\.) )", lambda m: " " * (4 * len(m.group(1)) // 3), text, flags=re.M)
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    for i, block in enumerate(blocks):
        body = body.replace(f"<p>MERMAIDBLOCK{i}</p>", block)
    return body


def build_html(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="pl"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{CSS}</style></head><body>{body}
<script src="{MERMAID_JS}"></script>
<script>
mermaid.initialize({{startOnLoad: false, securityLevel: 'loose', flowchart: {{useMaxWidth: true, htmlLabels: true}}}});
mermaid.run({{querySelector: 'pre.mermaid'}}).then(() => document.body.setAttribute('data-mermaid', 'done'))
  .catch(e => document.body.setAttribute('data-mermaid', 'error: ' + e));
</script></body></html>"""


def chrome_bin() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("Chrome/Chromium not found on PATH")


def main() -> int:
    docs = sys.argv[1:] or DEFAULT_DOCS
    OUT.mkdir(parents=True, exist_ok=True)
    chrome = chrome_bin()
    status = 0
    with tempfile.TemporaryDirectory() as tmp:
        for rel in docs:
            src = ROOT / rel
            title = src.stem.replace("_", " ")
            page = Path(tmp) / (src.stem + ".html")
            page.write_text(build_html(title, render_markdown(src.read_text(encoding="utf-8"))), encoding="utf-8")
            pdf = OUT / (src.stem + ".pdf")
            common = ["--headless=new", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=30000"]

            dom = subprocess.run([chrome, *common, "--dump-dom", page.as_uri()],
                                 capture_output=True, text=True, timeout=180).stdout
            n_svg = len(re.findall(r"<svg[^>]*id=\"mermaid", dom))
            n_src = len(re.findall(r'class="mermaid"', dom))
            failed = "Syntax error" in dom or 'data-mermaid="error' in dom or n_svg < n_src
            print(f"{src.name}: diagrams rendered {n_svg}/{n_src}" + ("  <-- PROBLEM" if failed else ""))
            status |= int(failed)

            subprocess.run([chrome, *common, "--no-pdf-header-footer", f"--print-to-pdf={pdf}", page.as_uri()],
                           capture_output=True, text=True, timeout=180, check=True)
            print(f"  -> {pdf.relative_to(ROOT)} ({pdf.stat().st_size // 1024} KB)")
    return status


if __name__ == "__main__":
    sys.exit(main())
