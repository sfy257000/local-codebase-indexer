"""Render verified CLI output into a 32-second terminal walkthrough.

Install .[media], then run from the repository root. Pauses are for readability;
the raw JSON contains the actual measured timings, not animation durations.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', default='/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run([sys.executable, '-m', 'local_codebase_indexer', 'demo', '--json'],
                          check=True, capture_output=True, text=True, cwd=repo)
    report = json.loads(proc.stdout)
    if not report['passed']:
        raise SystemExit('Demo verification failed; refusing to render a success walkthrough')
    from local_codebase_indexer.demo import render_demo
    output = render_demo(report)
    assets = repo / 'assets'
    assets.mkdir(exist_ok=True)
    (assets / 'demo-report.json').write_text(proc.stdout, encoding='utf-8')
    (assets / 'demo-output.txt').write_text(output + '\n', encoding='utf-8')
    lines = output.splitlines()
    table_start = next(i for i, line in enumerate(lines) if line.startswith('table '))
    access_end = next(i for i, line in enumerate(lines) if line.strip().startswith(('SCHEMA CHANGED BY', 'USED BY PROGRAMS')))
    program_start = next(i for i, line in enumerate(lines) if line.strip().startswith('USED BY PROGRAMS'))
    program_end = next(i for i, line in enumerate(lines[program_start:], program_start) if line.startswith('note:'))
    checks_start = next(i for i, line in enumerate(lines) if line.startswith('Verified '))
    sections = [
        ('1 / 4  Start without a database', lines[:3] + ['', 'Question: which C# code and pages reach Sales_OrderHeader?']),
        ('2 / 4  Find direct and indirect SQL access', lines[table_start:access_end]),
        ('3 / 4  Follow C# calls to controller actions and a page', lines[program_start:program_end] + ['',
         'Confidence is shown in the CLI output.', 'Probable links need source verification.']),
        ('4 / 4  Check the result, then try your project', lines[checks_start:checks_start + 4] + ['', *lines[-2:]]),
    ]
    font = ImageFont.truetype(args.font, 20)
    title_font = ImageFont.truetype(args.font, 24)
    small_font = ImageFont.truetype(args.font, 17)
    frames = []
    for title, body in sections:
        frame = Image.new('RGB', (1280, 660), '#101924')
        draw = ImageDraw.Draw(frame)
        draw.rounded_rectangle((20, 20, 1260, 640), radius=18, fill='#172330', outline='#334454', width=2)
        draw.text((48, 42), 'Local Codebase Indexer  /  SQL Server -> C# -> page', font=title_font, fill='#f3f6fa')
        draw.text((48, 96), title, font=font, fill='#73d7c5')
        draw.text((48, 145), '$ local-codebase-indexer demo', font=font, fill='#a1c8ff')
        y = 194
        for line in body:
            for wrapped in textwrap.wrap(line, width=96, subsequent_indent='    ') or ['']:
                draw.text((48, y), wrapped, font=font, fill='#e0e7ef')
                y += 28
        if y > 580:
            raise SystemExit(f'Output overflows scene: {title}')
        draw.text((48, 602), 'Synthetic sample | Actual CLI output | Reading pauses added | MIT', font=small_font, fill='#a4b5c5')
        frames.append(frame)
    frames[0].save(assets / 'demo.gif', save_all=True, append_images=frames[1:], duration=8000, loop=0)
    frames[2].save(assets / 'demo-preview.png')
    print(f"Rendered verified demo: {report['index']['scanned']} files, {len(report['checks'])} checks; 32-second loop")


if __name__ == '__main__':
    main()
