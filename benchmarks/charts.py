"""Lab-style SVG charts of a corpus 24a analysis (Phase 27). Offline: reads bp.analyse's output, nothing else.

Every chart states what it measured on its face: the products, the model, the number of cells and the method (a 95%
task-cluster bootstrap interval, intention to treat: every planned cell counted, a cell not run is a failure). Bars
never start above zero and intervals are drawn, so a reader can see when a difference is within noise. No chart shows
a figure the analysis does not hold.
"""

import textwrap
from html import escape

PALETTE = {'pebble': '#506e54', 'other': '#9aa39b', 'text': '#252c28', 'muted': '#66706a', 'grid': '#e3e7e1',
           'zero': '#b9c2b7', 'ci': '#252c28'}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
NAMES = {'pebble': 'Pebble', 'claude_code': 'Claude Code CLI', 'codex': 'Codex CLI', 'baseline': 'Baseline harness'}


def _name(product):
    return NAMES.get(product, product)


def _color(product):
    return PALETTE['pebble'] if product == 'pebble' else PALETTE['other']


def _text(x, y, value, *, size=13, anchor='start', weight='normal', fill=None):
    return ('<text x="%.1f" y="%.1f" font-family="%s" font-size="%d" font-weight="%s" fill="%s" text-anchor="%s">%s</text>'
            % (x, y, escape(FONT, quote=True), size, weight, fill or PALETTE['text'], anchor, escape(str(value))))


def _pct(value):
    return '—' if value is None else '%.0f%%' % (100 * value)


def _frame(width, height, title, subtitle, footnote, body):
    # SVG text does not wrap: the footnote is broken into lines that fit the width (11 px text, about 6.2 px a
    # character), and the canvas grows to hold them.
    lines = textwrap.wrap(footnote, width=max(40, int((width - 48) / 6.2)))
    height += 15 * (len(lines) - 1)
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
             'aria-label="%s">' % (width, height, width, height, escape(title, quote=True)),
             '<rect width="100%" height="100%" fill="#ffffff"/>',
             _text(24, 34, title, size=18, weight='600'), _text(24, 56, subtitle, size=13, fill=PALETTE['muted'])]
    parts += body
    for index, line in enumerate(lines):
        parts.append(_text(24, height - 16 - 15 * (len(lines) - 1 - index), line, size=11, fill=PALETTE['muted']))
    parts.append('</svg>')
    return '\n'.join(parts) + '\n'


def _context(analysis, meta):
    products = analysis['products']
    cells = sum(analysis['summary'][p]['cells'] for p in products)
    model = (meta or {}).get('model') or '—'
    return model, cells


def success(analysis, meta=None):
    """Success rate per product with its 95% task-cluster interval."""
    products = analysis['products']
    model, cells = _context(analysis, meta)
    width, left, top, bar, gap = 760, 190, 84, 34, 22
    scale = width - left - 70
    body = []
    for tick in range(0, 101, 25):
        x = left + scale * tick / 100
        body.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s"/>' % (
            x, top - 8, x, top + len(products) * (bar + gap), PALETTE['grid']))
        body.append(_text(x, top + len(products) * (bar + gap) + 14, '%d%%' % tick, size=11, anchor='middle',
                          fill=PALETTE['muted']))
    for index, product in enumerate(products):
        row = analysis['summary'][product]
        y = top + index * (bar + gap)
        rate = row['success'] or 0.0
        body.append(_text(left - 12, y + bar / 2 + 5, _name(product), anchor='end', weight='600'))
        body.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" fill="%s"/>' % (
            left, y, scale * rate, bar, _color(product)))
        low, high = row['success_cluster95']
        if low is not None and high is not None:
            body.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="2"/>' % (
                left + scale * low, y + bar / 2, left + scale * high, y + bar / 2, PALETTE['ci']))
            for end in (low, high):
                body.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="2"/>' % (
                    left + scale * end, y + 9, left + scale * end, y + bar - 9, PALETTE['ci']))
        body.append(_text(left + scale * max(rate, high or 0) + 10, y + bar / 2 + 5,
                          '%s  (%d of %d)' % (_pct(row['success']), row['passed'], row['cells']), size=13))
    height = top + len(products) * (bar + gap) + 64
    return _frame(width, height, 'Task success, same tasks and same model',
                  '%s · %d cells · corpus 24a' % (model, cells),
                  'Bars: share of planned cells passed (intention to treat). Lines: 95% task-cluster bootstrap interval.',
                  body)


def families(analysis, meta=None):
    """Success per task family, grouped by product."""
    products = analysis['products']
    model, cells = _context(analysis, meta)
    fams = list(analysis['families'])
    width = max(760, 150 + 90 * len(fams))
    left, top, plot = 70, 84, 220
    step = (width - left - 30) / max(1, len(fams))
    bar = min(28, (step - 18) / max(1, len(products)))
    body = []
    for tick in range(0, 101, 25):
        y = top + plot - plot * tick / 100
        body.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="%s"/>' % (left, y, width - 30, y, PALETTE['grid']))
        body.append(_text(left - 8, y + 4, '%d%%' % tick, size=11, anchor='end', fill=PALETTE['muted']))
    for i, family in enumerate(fams):
        group = analysis['families'][family]['products']
        x0 = left + i * step + 9
        for j, product in enumerate(products):
            rate = (group.get(product) or {}).get('success') or 0.0
            h = plot * rate
            body.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="3" fill="%s"/>' % (
                x0 + j * bar, top + plot - h, bar - 3, h, _color(product)))
        body.append(_text(x0 + bar * len(products) / 2, top + plot + 18, family, size=11, anchor='middle'))
    legend_y = top + plot + 44
    for j, product in enumerate(products):
        body.append('<rect x="%d" y="%d" width="12" height="12" rx="2" fill="%s"/>' % (left + j * 190, legend_y - 10,
                                                                                    _color(product)))
        body.append(_text(left + j * 190 + 18, legend_y, _name(product), size=12))
    return _frame(width, legend_y + 40, 'Success by task family', '%s · %d cells' % (model, cells),
                  'Share of each family\'s planned cells passed. Small families carry wide uncertainty.', body)


def paired(analysis, meta=None):
    """The paired success difference (first product minus second) with its 95% interval against zero."""
    products = analysis['products']
    model, cells = _context(analysis, meta)
    row = analysis['paired']
    width, left, top, span = 760, 60, 116, 640
    lo, hi = -1.0, 1.0
    x = lambda v: left + span * (v - lo) / (hi - lo)  # noqa: E731
    body = ['<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="2"/>' % (
        x(0), top - 20, x(0), top + 40, PALETTE['zero'])]
    for tick in (-1, -0.5, 0, 0.5, 1):
        body.append(_text(x(tick), top + 62, '%+d pts' % round(100 * tick) if tick else '0', size=11, anchor='middle',
                          fill=PALETTE['muted']))
    if row and row.get('delta') is not None:
        low, high = row['cluster95']
        body.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s" stroke-width="3"/>' % (
            x(low), top + 10, x(high), top + 10, PALETTE['ci']))
        body.append('<circle cx="%.1f" cy="%d" r="7" fill="%s"/>' % (x(row['delta']), top + 10, PALETTE['pebble']))
        verdict = ('above zero' if low > 0 else 'below zero' if high < 0 else 'includes zero: no difference shown')
        body.append(_text(left, top - 26, '%s minus %s: %+.0f points (95%% interval %+.0f to %+.0f, %s)' % (
            _name(products[0]), _name(products[1]), 100 * row['delta'], 100 * low, 100 * high, verdict), size=13))
    else:
        body.append(_text(left, top - 26, 'No paired cells yet.', size=13))
    return _frame(width, top + 110, 'Paired difference in success',
                  '%s · %d task pairs · %d cells' % (model, (row or {}).get('pairs') or 0, cells),
                  'Same task, repetition and phrasing for both. A claim needs the whole interval above zero.', body)


def efficiency(analysis, meta=None):
    """Median wall seconds per cell, and the cost at the plan's prices where the analysis holds it."""
    products = analysis['products']
    model, cells = _context(analysis, meta)
    costs = analysis.get('cost_usd') or {}
    width, left, top, bar, gap = 760, 190, 84, 30, 20
    walls = {p: analysis['summary'][p]['R3']['wall_median'] for p in products}
    peak = max([w for w in walls.values() if isinstance(w, (int, float)) and w > 0] or [1])
    scale = width - left - 250  # room for the longest label after the longest bar
    body = []
    for index, product in enumerate(products):
        y = top + index * (bar + gap)
        wall = walls[product]
        body.append(_text(left - 12, y + bar / 2 + 5, _name(product), anchor='end', weight='600'))
        body.append('<rect x="%d" y="%d" width="%.1f" height="%d" rx="4" fill="%s"/>' % (
            left, y, scale * (wall or 0) / peak, bar, _color(product)))
        per_cell = None
        if product in costs and analysis['summary'][product]['cells']:
            per_cell = costs[product] / analysis['summary'][product]['cells']
        label = ('%.0f s median' % wall if isinstance(wall, (int, float)) else 'time not reported')
        label += ('  ·  $%.2f a task' % per_cell) if per_cell is not None else ''
        body.append(_text(left + scale * (wall or 0) / peak + 10, y + bar / 2 + 5, label, size=13))
    note = analysis.get('cost_note') or 'Cost is shown only where the analysis holds it.'
    return _frame(width, top + len(products) * (bar + gap) + 60, 'Time and cost per task', '%s · %d cells' % (model, cells),
                  'Wall time per cell, median. Cost ' + note, body)


def render_all(analysis, meta=None):
    """{file name: svg} for every chart."""
    return {'success.svg': success(analysis, meta), 'families.svg': families(analysis, meta),
            'paired.svg': paired(analysis, meta), 'efficiency.svg': efficiency(analysis, meta)}


__all__ = ['efficiency', 'families', 'paired', 'render_all', 'success']
