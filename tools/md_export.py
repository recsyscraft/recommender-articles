#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Export Markdown to portable HTML and Chromium PDF (Python 3.10+)."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def walk(value, action):
    if isinstance(value, dict):
        action(value)
        for child in list(value.values()):
            walk(child, action)
    elif isinstance(value, list):
        for child in value:
            walk(child, action)


def convert(source, target, pandoc, exported):
    result = subprocess.run(
        [pandoc, '--from=markdown+pipe_tables+tex_math_dollars', '--to=json', str(source)],
        check=True, capture_output=True, encoding='utf-8')
    document = json.loads(result.stdout)

    def adjust(node):
        if node.get('t') == 'Image':
            attrs, caption, _ = node['c']
            # Obsidian image widths: ![caption|384](image.png)
            if caption and caption[-1].get('t') == 'Str':
                text = caption[-1]['c']
                head, sep, width = text.rpartition('|')
                if sep and width.isdigit():
                    caption[-1]['c'] = head
                    attrs[2].append(['width', width])
        if node.get('t') == 'Figure':
            # Pandoc also copies image alt text into a separate figure caption.
            import re
            def clean_caption(part):
                if part.get('t') == 'Str':
                    part['c'] = re.sub(r'\|[0-9]+$', '', part['c'])
            walk(node['c'][1], clean_caption)
        if node.get('t') == 'Link':
            from urllib.parse import urlsplit, unquote, urlunsplit
            url = urlsplit(node['c'][2][0])
            if not url.scheme and not url.netloc and url.path:
                linked = (source.parent / unquote(url.path)).resolve()
                if linked in exported:
                    node['c'][2][0] = urlunsplit(('', '', str(Path(url.path).with_suffix('.html')).replace('\\', '/'), url.query, url.fragment))

    walk(document, adjust)
    title = next((line.lstrip('#').strip() for line in source.read_text(encoding='utf-8-sig').splitlines()
                  if line.startswith('# ')), source.stem)
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [pandoc, '--from=json', '--to=html5', '--standalone', '--embed-resources',
         '--mathml', '--fail-if-warnings', '--toc', '--css', str(HERE / 'style.css'),
         '--metadata', 'pagetitle=' + title, '--output', str(target)],
        input=json.dumps(document), encoding='utf-8', cwd=source.parent, check=True)


def print_pdf(html, target, browser):
    page = browser.new_page(java_script_enabled=False)
    try:
        page.goto(html.as_uri(), wait_until='load')
        page.evaluate('document.fonts.ready')
        broken = page.locator('img').evaluate_all('(imgs) => imgs.filter(i => !i.complete || i.naturalWidth === 0).map(i => i.alt)')
        if broken:
            raise RuntimeError('Images failed to load: ' + ', '.join(broken))
        page.pdf(path=str(target), format='A4', print_background=True,
                 margin={'top': '18mm', 'bottom': '18mm', 'left': '16mm', 'right': '16mm'})
    finally:
        page.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Markdown file or directory (recursive)')
    parser.add_argument('--format', choices=['html', 'pdf', 'both'], default='html')
    parser.add_argument('--output-dir', type=Path, default=Path('dist'))
    parser.add_argument('--force', action='store_true', help='overwrite existing output files')
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.exists() or (source.is_file() and source.suffix.lower() != '.md'):
        parser.error('source must be a Markdown file or directory')
    output = args.output_dir.resolve()
    if source.is_dir() and (output == source or source in output.parents):
        parser.error('output directory must be outside the input directory')
    files = sorted(p for p in source.rglob('*') if p.is_file() and p.suffix.lower() == '.md') if source.is_dir() else [source]
    if not files:
        parser.error('no Markdown files found')
    pandoc = shutil.which('pandoc')
    if not pandoc:
        parser.error('Pandoc is missing. Install Pandoc 3+ and add it to PATH; see tools/README.md')
    base = source if source.is_dir() else source.parent
    plans = [(p, output / p.relative_to(base).with_suffix('.html')) for p in files]
    outputs = [h.with_suffix('.' + ext) for _, h in plans
               for ext in (['html', 'pdf'] if args.format == 'both' else [args.format])]
    if len(set(outputs)) != len(outputs):
        parser.error('output filename collision')
    if not args.force and any(p.exists() for p in outputs):
        parser.error('output already exists; use --force to overwrite')
    try:
        from contextlib import ExitStack
        with ExitStack() as stack:
            browser = None
            if args.format != 'html':
                try:
                    from playwright.sync_api import sync_playwright
                except ImportError:
                    parser.error('PDF requires: python -m pip install -r tools/requirements.txt')
                runtime = stack.enter_context(sync_playwright())
                browser = runtime.chromium.launch()
                stack.callback(browser.close)
            for article, html in plans:
                with tempfile.TemporaryDirectory(prefix='md-export-') as temp:
                    staged = Path(temp) / 'article.html'
                    convert(article, staged, pandoc, set(files))
                    html.parent.mkdir(parents=True, exist_ok=True)
                    if browser:
                        pdf = Path(temp) / 'article.pdf'
                        print_pdf(staged, pdf, browser)
                        shutil.copyfile(pdf, html.with_suffix('.pdf'))
                    if args.format != 'pdf':
                        shutil.copyfile(staged, html)
                print(f'OK: {article}')
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f'Export failed: {exc}', file=sys.stderr)
        return 1
    except Exception as exc:
        print(f'Export failed: {exc}\nFor PDF, run: python -m playwright install chromium', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
