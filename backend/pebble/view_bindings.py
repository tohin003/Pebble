"""Read-only, chat-scoped, counts-only host data for dashboard panels.

A binding names one fixed source and bounded parameters; it never carries a chat, run,
path or record identifier. The chat is taken from the artifact registry row only. A
read stores nothing: resolution counts current host records, labels them with fixed
host text and keeps the result for two seconds in memory. No file content, prompt text,
memory record, credential or another chat's identifier is ever returned. The person's
native Refresh is the same read: it appends no revision and writes no row.
"""
from collections import namedtuple
import hashlib
import json
import threading
import time

from .artifact_schema import PANEL_KINDS, inert_text, panel_data, rejected_keys

CACHE_SECONDS = 2.0
CACHE_ENTRIES = 256
LAYERS = ('working', 'episodic', 'semantic', 'procedural', 'temporal', 'profile')
RUN_STATES = ('queued', 'running', 'retry_wait', 'waiting_input', 'waiting_approval', 'paused', 'completed', 'failed',
              'stopped', 'interrupted', 'budget_exhausted', 'verification_required', 'needs_reconciliation')
FIRE_STATES = ('claimed', 'bound', 'running', 'waiting', 'attention', 'completed', 'failed', 'cancelled',
               'needs_review', 'blocked', 'dismissed')
CHILD_STATES = ('queued', 'running', 'waiting', 'blocked', 'completed', 'failed', 'stopped', 'interrupted', 'cancelled')
UNAVAILABLE = 'This host data is unavailable right now.'


class Unavailable(Exception):
    """A source could not be read; the panel says so without any host detail."""


# kinds: panel kinds the counts can fill; params: name -> (low, high, default); unit: metric unit.
Source = namedtuple('Source', 'kinds params caption unit read')


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _label(key):
    return key.replace('_', ' ').capitalize()


def _states(pairs, order):
    counts = dict(pairs)
    return [(state, _label(state), counts[state]) for state in order if counts.get(state)]


def _runs_recent(app, cid, params, db):
    rows = db.execute('SELECT state FROM runs WHERE chat_id=? ORDER BY created DESC LIMIT ?', (cid, params['limit'])).fetchall()
    counts = {}
    for (state,) in rows:
        counts[state] = counts.get(state, 0) + 1
    return _states(counts.items(), RUN_STATES), len(rows), None


def _runs_usage(app, cid, params, db):
    totals, unreported = {'input_tokens': 0, 'output_tokens': 0, 'cached_tokens': 0}, 0
    if _table(db, 'metrics'):
        for (payload,) in db.execute('SELECT payload FROM metrics WHERE chat_id=?', (cid,)).fetchall():
            try:
                usage = json.loads(payload)
            except (TypeError, ValueError, RecursionError):
                usage = None
            values = [usage.get(key) if isinstance(usage, dict) else None for key in ('input_tokens', 'output_tokens')]
            if any(type(v) is not int or not 0 <= v <= 1_000_000_000 for v in values):
                unreported += 1
                continue
            totals['input_tokens'] += values[0]
            totals['output_tokens'] += values[1]
            cached = usage.get('cached_tokens')
            totals['cached_tokens'] += cached if type(cached) is int and 0 <= cached <= values[0] else 0
    rows = [(key, label, totals[key]) for key, label in (('input_tokens', 'Input tokens'), ('output_tokens', 'Output tokens'),
                                                          ('cached_tokens', 'Cached input tokens'))]
    rows.append(('unreported', 'Replies without usage', unreported))
    return rows, totals['input_tokens'] + totals['output_tokens'], None


def _jobs_fires(app, cid, params, db):
    if not _table(db, 'job_fires'):
        return [], 0, 'No schedules in this chat.'
    since = time.time() - params['days'] * 86400
    pairs = db.execute('SELECT status,count(*) FROM job_fires WHERE chat_id=? AND claimed>=? GROUP BY status', (cid, since)).fetchall()
    rows = _states(pairs, FIRE_STATES)
    return rows, sum(r[2] for r in rows), None if rows else 'No scheduled runs in this period.'


def _agents_tree(app, cid, params, db):
    if not _table(db, 'agent_nodes'):
        return [], 0, 'No helper agents in this chat.'
    pairs = db.execute('SELECT n.status,count(*) FROM agent_nodes n JOIN runs r ON r.id=n.run_id WHERE r.chat_id=? '
                       'GROUP BY n.status', (cid,)).fetchall()
    known = [p for p in pairs if p[0] in CHILD_STATES]
    rows = _states(known, CHILD_STATES)
    other = sum(n for state, n in pairs if state not in CHILD_STATES)
    if other:
        rows.append(('other', 'Other', other))
    return rows, sum(r[2] for r in rows), None if rows else 'No helper agents in this chat.'


def _artifact_revisions(app, cid, params, db):
    pairs = db.execute('SELECT a.kind,count(*),coalesce(sum(h.revision),0) FROM artifacts a LEFT JOIN artifact_heads h '
                       'ON h.artifact_id=a.id WHERE a.chat_id=? GROUP BY a.kind ORDER BY a.kind', (cid,)).fetchall()
    kinds = {'mcp_media': 'Media'}
    rows = [(kind, kinds.get(kind, _label(kind)) + ' views', count) for kind, count, _ in pairs]
    return rows, sum(r[2] for r in rows), None, {'revisions': sum(p[2] for p in pairs)}


def _memory_stats(app, cid, params, db):
    memory = getattr(app, 'memory', None)
    if memory is None or not memory.enabled():
        return [], 0, 'Memory is off.'
    chat = db.execute('SELECT workspace FROM chats WHERE id=?', (cid,)).fetchone()
    scopes = ('global', (chat[0] if chat and chat[0] else 'global'), 'chat:' + cid)
    pairs = dict(db.execute('SELECT kind,count(*) FROM memories WHERE scope IN (?,?,?) AND (expires IS NULL OR expires>?) '
                            'GROUP BY kind', (*scopes, time.time())).fetchall())
    # Disabled layers are not counted; the six layers stay exactly these.
    rows = [(kind, _label(kind), pairs.get(kind, 0)) for kind in LAYERS if memory.layer_enabled(kind)]
    return rows, sum(r[2] for r in rows), None if rows else 'Every memory layer is off.'


def _git_summary(app, cid, params, db):
    chat = db.execute('SELECT workspace FROM chats WHERE id=?', (cid,)).fetchone()
    if not chat or not chat[0]:
        return [], 0, 'No project folder in this chat.'
    git = getattr(app, 'git', None)
    if git is None:
        raise Unavailable()
    return 'deferred', None, None


def _git_counts(app, cid):
    try:
        status = app.git.status(cid, 0, 1)
        counts = status['counts']
        rows = [(key, _label(key), counts[key]) for key in ('staged', 'unstaged', 'untracked', 'unmerged')]
        rows += [(key, _label(key), status[key]) for key in ('ahead', 'behind') if type(status.get(key)) is int]
    except Exception:
        raise Unavailable() from None
    if any(type(n) is not int or n < 0 for _, _, n in rows):
        raise Unavailable()
    return rows, sum(r[2] for r in rows[:4]), None


def _goals_progress(app, cid, params, db):
    if not _table(db, 'goals'):
        return [], 0, 'No current goal in this chat.'
    goal = db.execute("SELECT id FROM goals WHERE chat_id=? AND state IN ('active','paused','blocked') LIMIT 1", (cid,)).fetchone()
    if not goal:
        return [], 0, 'No current goal in this chat.'
    criteria = dict(db.execute("SELECT status,count(*) FROM goal_criteria WHERE goal_id=? AND status IN ('open','met','unmet') "
                               'GROUP BY status', (goal[0],)).fetchall())
    steps = dict(db.execute('SELECT status,count(*) FROM goal_steps WHERE goal_id=? GROUP BY status', (goal[0],)).fetchall())
    rows = [('criteria_met', 'Criteria met', criteria.get('met', 0)), ('criteria_open', 'Criteria open', criteria.get('open', 0)),
            ('criteria_unmet', 'Criteria unmet', criteria.get('unmet', 0)),
            ('steps_completed', 'Steps completed', steps.get('completed', 0)), ('steps', 'Steps', sum(steps.values()))]
    return rows, sum(criteria.values()), None


COUNTS = ('metric', 'table', 'chart')
SOURCES = {
    'runs.recent': Source(COUNTS, {'limit': (1, 50, 10)}, 'Latest runs in this chat by state.', 'runs', _runs_recent),
    'runs.usage': Source(COUNTS, {}, 'Reported model tokens for replies in this chat.', 'tokens', _runs_usage),
    'jobs.fires': Source(COUNTS, {'days': (1, 90, 7)}, 'Scheduled runs in this chat by outcome.', 'runs', _jobs_fires),
    'agents.tree': Source(COUNTS, {}, 'Helper agents in this chat by state.', 'agents', _agents_tree),
    'artifacts.revisions': Source(COUNTS, {}, 'Views in this chat by kind.', 'views', _artifact_revisions),
    'memory.stats': Source(COUNTS, {}, 'Memories this chat can recall, by layer.', 'memories', _memory_stats),
    'git.summary': Source(('metric', 'table'), {}, 'Project changes from Git status (counts only).', 'files', _git_summary),
    'goals.progress': Source(COUNTS, {}, 'Current goal progress in this chat.', None, _goals_progress),
}
FIELDS = {'runs.recent': RUN_STATES, 'runs.usage': ('input_tokens', 'output_tokens', 'cached_tokens', 'unreported'),
          'jobs.fires': FIRE_STATES, 'agents.tree': CHILD_STATES + ('other',),
          'artifacts.revisions': ('revisions',), 'memory.stats': LAYERS,
          'git.summary': ('staged', 'unstaged', 'untracked', 'unmerged', 'ahead', 'behind'),
          'goals.progress': ('criteria_met', 'criteria_open', 'criteria_unmet', 'steps_completed', 'steps')}


def validate_binding(binding, kind=None):
    """An exact {source, params?} for one allowed source; params are bounded counts or a metric field."""
    if type(binding) is not dict:
        raise ValueError('A dashboard binding is {source, params}.')
    rejected_keys(binding)
    if 'source' not in binding or set(binding) - {'source', 'params'}:
        raise ValueError('A dashboard binding is {source, params}.')
    source = SOURCES.get(binding['source']) if type(binding['source']) is str else None
    if source is None:
        raise ValueError('Bind dashboard panels to an allowed host source: ' + ', '.join(SOURCES) + '.')
    if kind is not None and kind not in source.kinds:
        raise ValueError('This host source fills ' + ', '.join(source.kinds) + ' panels only.')
    params = binding.get('params', {})
    if type(params) is not dict:
        raise ValueError('Binding params are an object.')
    allowed = set(source.params) | ({'field'} if kind in (None, 'metric') else set())
    if set(params) - allowed:
        raise ValueError('This host source does not accept that parameter.')
    for name, (low, high, _) in source.params.items():
        if name in params and (type(params[name]) is not int or not low <= params[name] <= high):
            raise ValueError(f'Use a whole number from {low} to {high} for {name}.')
    if 'field' in params and (type(params['field']) is not str or params['field'] not in ('total',) + FIELDS[binding['source']]):
        raise ValueError('Choose a metric field this host source counts.')
    result = {'source': binding['source']}
    if 'params' in binding:
        result['params'] = dict(params)
    return result


def _read(app, cid, name, params):
    source = SOURCES[name]
    values = {key: params.get(key, default) for key, (_, _, default) in source.params.items()}
    with app.store.lock:
        result = source.read(app, cid, values, app.store.db)
    if result[0] == 'deferred':
        # Git runs a confined subprocess; never hold the store lock across it.
        result = _git_counts(app, cid)
    rows, total, note = result[:3]
    fields = {key: count for key, _, count in rows}
    fields.update(result[3] if len(result) > 3 else {})
    return rows, total, note, fields


def _shape(name, kind, params, rows, total, note, fields):
    source = SOURCES[name]
    caption = inert_text(source.caption + (' ' + note if note else ''), 240)
    if kind == 'metric':
        field = params.get('field', 'total')
        data = {'value': total if field == 'total' else fields.get(field, 0), 'caption': caption}
        if source.unit:
            data['unit'] = source.unit
        return data
    items = [{'item': label, 'count': count} for _, label, count in rows]
    if kind == 'table':
        return {'columns': [{'id': 'item', 'label': 'Item'}, {'id': 'count', 'label': 'Count'}], 'rows': items, 'caption': caption}
    return {'type': 'bar', 'x_key': 'item', 'y_keys': ['count'], 'labels': {'item': 'Item', 'count': 'Count'},
            'rows': items or [{'item': 'None', 'count': 0}], 'caption': caption}


def resolve(app, chat_id, binding, kind=None):
    """Current counts for one binding in one chat, validated by the panel kind, with a stamp.

    `chat_id` must come from the artifact registry row; nothing here accepts a chat from
    the binding. `kind` defaults to the source's table shape. Nothing is stored.
    """
    if type(chat_id) is not str or not chat_id:
        raise ValueError('Dashboard bindings resolve within one conversation.')
    kind = kind or 'table'
    if kind not in PANEL_KINDS:
        raise ValueError('Dashboard panels are metric, table, chart, plan or evidence.')
    binding = validate_binding(binding, kind)
    params = binding.get('params', {})
    rows, total, note, fields = _read(app, chat_id, binding['source'], params)
    data = panel_data(kind, _shape(binding['source'], kind, params, rows, total, note, fields))
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return {'data': data, 'stamp': {'source': binding['source'], 'as_of': time.time(),
                                    'digest': hashlib.sha256(encoded.encode()).hexdigest()}}


class Cache:
    """A two-second in-memory read cache keyed by chat, panel kind and exact binding."""

    def __init__(self, seconds=CACHE_SECONDS, clock=time.monotonic, entries=CACHE_ENTRIES):
        self.seconds, self.clock, self.entries = seconds, clock, entries
        self.lock, self.items = threading.Lock(), {}

    def get(self, key, compute):
        now = self.clock()
        with self.lock:
            hit = self.items.get(key)
            if hit and 0 <= now - hit[0] < self.seconds:
                return hit[1]
        return self.put(key, compute())

    def put(self, key, value):
        now = self.clock()
        with self.lock:
            for stale in [k for k, (at, _) in self.items.items() if not 0 <= now - at < self.seconds]:
                self.items.pop(stale)
            while len(self.items) >= self.entries:
                self.items.pop(next(iter(self.items)))
            self.items[key] = (now, value)
        return value

    def clear(self, chat_id=None):
        with self.lock:
            for key in [k for k in self.items if chat_id is None or k[0] == chat_id]:
                self.items.pop(key)


def resolve_panels(app, chat_id, data, cache=None):
    """Every bound panel of one dashboard revision, in panel order:
    {panel_id, source, as_of, digest, data} or {panel_id, source, error}."""
    entries = []
    for panel in data.get('panels', []) if isinstance(data, dict) else []:
        if not isinstance(panel, dict) or 'binding' not in panel:
            continue
        binding, kind = panel['binding'], panel['kind']
        source = binding.get('source') if isinstance(binding, dict) and isinstance(binding.get('source'), str) else ''
        key = (chat_id, kind, json.dumps(binding, sort_keys=True, separators=(',', ':')))
        compute = lambda: resolve(app, chat_id, binding, kind)
        try:
            resolved = cache.get(key, compute) if cache else compute()
            stamp = resolved['stamp']
            entries.append({'panel_id': panel['id'], 'source': stamp['source'], 'as_of': stamp['as_of'],
                            'digest': stamp['digest'], 'data': resolved['data']})
        except Exception:
            # Never echo an exception: Git and SQLite messages can name host paths.
            entries.append({'panel_id': panel['id'], 'source': source, 'error': UNAVAILABLE})
    return entries
