"""Run checkpoints and rewind for file edits (Phase 20, D19 §8.2, frontier gap X10).

A checkpoint is derived, never stored. For each file a run wrote, the host joins
the run's applied broker edits (before/after text and hashes) with the run's own
provenance witnesses, then adds later undo receipts and earlier rewinds of the
same run. That timeline says what the file held after every model step, and what
it should hold now if nobody else touched it.

`rewind_preview(chat_id, run_id, step)` is a read. It lists the exact diff that
would bring each file back to its state after `step` (0 means before the task),
refuses any file whose current hash is not the one this run's history left, and
lists every non-file effect after that step (commands, connector calls, Git
receipts, app and screen acts, delegated agents, views) as not reverted.

`rewind_apply(chat_id, run_id, step, digest)` re-plans, requires the preview's
digest, and stages each file as an ordinary edit through the existing broker
(App.propose with requires_review, then App.approve as the person's main-window
review), so permissions, hooks, path leases, uncertain-command inspections and
the baseline compare-and-swap all apply. Every written file is read back. Nothing
is replayed: no tool, command or connector runs, and a rewind never deletes a
file. Both routes are main-window only: the shell writes `surface` from the
calling window's label, and anything but 'main' is refused.

Generated UI has no route here, no model tool is added, and memory is untouched.
"""

import difflib
import hashlib
import json
import re
import threading
import uuid

from .hooks import HookInterrupted
from .permissions import DEFAULT
from .policy import PolicyError, Workspace, digest

ROUTES = ('rewind_preview', 'rewind_apply')
LIVE = frozenset({'queued', 'running', 'retry_wait'})
MAX_FILES = 256          # the provenance ledger's MAX_PATHS bounds one run's files
MAX_EFFECTS = 200
DIFF_CHARS = 24_000      # per file shown; the digest covers the full target hash either way
DIFF_TOTAL = 240_000
LABEL_CHARS = 200
STEP = re.compile(r'([0-9]{1,6}):')
HEX64 = re.compile(r'[0-9a-f]{64}')

# Every effect tool except the file writes this checkpoint restores. Integration
# wrappers stage nested Write receipts, which the file timeline already holds.
FILE_TOOLS = frozenset({'Write', 'Append', 'Edit', 'IntegrateAgentPatch', 'IntegrateWorktreeFile'})
KINDS = {
    'RunCommand': 'command', 'StartCommand': 'command', 'RunPlan': 'command', 'StopProcess': 'command',
    'WriteProcessInput': 'command', 'RunTests': 'command', 'RunRecipe': 'command', 'PrepareEnvironment': 'command',
    'PluginCall': 'connector', 'FetchPluginData': 'connector',
    'ProposeGitChange': 'git',
    'OpenApp': 'app', 'ScreenAct': 'app', 'StartPreview': 'app', 'OpenURL': 'app',  # Phase 43: OpenURL
    'AppAction': 'app',  # Phase 43 (1.9.0): what an app was asked to do is not undone by a rewind
    'SpawnAgent': 'agent', 'SpawnWorktreeAgent': 'agent', 'ExecuteAgentEffect': 'agent',
    'UIRender': 'view', 'UIUpdate': 'view', 'RenderMod': 'view',
    'Shell': 'companion', 'WriteFile': 'companion', 'EditFile': 'companion',  # Phase 40 (1.6.0)
}
NOTES = {
    'command': 'Not reverted. Commands are never undone or run again. A file a command changed is restored only '
               'where this task also wrote it, and only if unchanged since.',
    'connector': 'Not reverted. It was sent, saved or changed through a connector, outside this computer.',
    'git': 'Not reverted. Use this Git receipt\'s own reviewed undo.',
    'app': 'Not reverted. App, screen and preview actions are not undone.',
    'agent': 'Not reverted. Delegated work is undone only where this task wrote the files itself.',
    'view': 'Not reverted. Views keep their revisions.',
    'companion': 'Not reverted. Use the change\'s own Undo: Pebble kept a copy of what it changed before it ran.',
}
OUTCOMES = {
    'occurred': 'It happened.',
    'unknown': 'Its outcome is unknown. Reconcile it from the task panel.',
    'pending': 'It still awaits your review. Rewinding neither approves nor cancels it.',
    'reported_not_occurred': 'You reported that it did not happen (unverified).',
}
REASONS = {
    'unchanged': 'Already as it was after that step.',
    'changed_since': 'Changed after this task\'s last write (by you, a command or another task). Pebble will not '
                     'overwrite it.',
    'interleaved': 'Also changed outside this task between its edits. Pebble will not overwrite that work.',
    'created': 'This task created the file after that step. A rewind never deletes files; remove it yourself if '
               'you want it gone.',
    'pending_edit': 'An edit to this file still awaits review. Approve or decline it first.',
    'record_mismatch': 'The edit record and this task\'s file witness disagree, so Pebble cannot rewind this file.',
    'unwitnessed': 'This task has no file witness for one of these edits, so Pebble cannot rewind this file.',
    'undo_unverified': 'An earlier undo of this file was not read back, so its current state is unknown.',
    'step_unknown': 'An edit to this file has no model step. Rewind to the start of the task instead.',
    'root_missing': 'The project folder is missing.',
    'root_replaced': 'The project folder was replaced after this task.',
    'unreadable': 'Pebble cannot read this file now (too large, a link or not a regular file).',
    'inspection_pending': 'A command in this project has an uncertain outcome. Inspect it before rewinding.',
    'leased': 'A delegated agent or a writable background command holds this path. Settle it first.',
    'edits_disabled': 'File edits are turned off in Permissions.',
}


def _step(call_id):
    found = STEP.match(call_id) if isinstance(call_id, str) else None
    return int(found.group(1)) if found else None


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def _clip(value, limit=LABEL_CHARS):
    text = ' '.join(str(value).split()) if value is not None else ''
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _number(value, fallback):
    return value if type(value) in (int, float) else fallback


class Rewinds:
    """The two main-window routes. Construction creates nothing: no table, no row."""

    def __init__(self, app):
        self.app, self.store = app, app.store
        self._applying = threading.Lock()

    def methods(self):
        return {'rewind_preview': self.preview, 'rewind_apply': self.apply}

    # Routes

    def preview(self, chat_id, run_id, step=0, surface=None):
        self._main(surface)
        plan = self._plan(chat_id, run_id, step)
        return {key: value for key, value in plan.items() if not key.startswith('_')}

    def apply(self, chat_id, run_id, step, digest, surface=None):
        self._main(surface)
        if not isinstance(digest, str) or not HEX64.fullmatch(digest):
            raise PolicyError('Preview the rewind, then apply that preview.')
        if not self._applying.acquire(blocking=False):
            raise PolicyError('Another rewind is being applied. Wait for it to finish.')
        try:
            if self.app.active:
                raise PolicyError('Wait for the current task to finish before rewinding files.')
            plan = self._plan(chat_id, run_id, step)
            if plan['digest'] != digest:
                raise PolicyError('The files, effects or permissions changed since this preview. Nothing was written. '
                                  'Review the new preview.')
            tag = {'rewind_id': uuid.uuid4().hex, 'run_id': run_id, 'step': step, 'previous_step': plan['previous_step']}
            if plan['reverses']:
                tag['reverses'] = plan['reverses']
            files = [self._revert(chat_id, entry, tag) if entry['status'] == 'revert' else self._kept(entry)
                     for entry in plan['_files']]
            summary = {'rewind_id': tag['rewind_id'], 'step': step, 'digest': digest,
                       'previous_step': plan['previous_step'], 'reverses': plan['reverses'],
                       'files': [{k: f[k] for k in ('path', 'status', 'action_id', 'target_hash', 'readback_hash', 'verified')}
                                 for f in files],
                       'effects_not_reverted': plan['counts']['effects']}
            try:
                self.app.runs.note(run_id, 'rewind_applied', summary)
                journaled = True
            except Exception:  # the broker receipts above stay authoritative; never mask their outcomes
                journaled = False
            return {'rewind_id': tag['rewind_id'], 'chat_id': chat_id, 'run_id': run_id, 'step': step,
                    'previous_step': plan['previous_step'], 'reverses': plan['reverses'], 'files': files,
                    'effects': plan['effects'], 'effects_omitted': plan['effects_omitted'],
                    'counts': {'reverted': sum(f['status'] == 'reverted' for f in files),
                               'verified': sum(f['verified'] for f in files),
                               'refused': sum(f['status'] == 'refused' for f in files),
                               'uncertain': sum(f['status'] in ('uncertain', 'diverged') for f in files),
                               'effects_not_reverted': plan['counts']['effects']},
                    'effects_reverted': 0, 'replayed': 0, 'journaled': journaled, 'authorization': 'none'}
        finally:
            self._applying.release()

    # Planning (reads only)

    @staticmethod
    def _main(surface):
        if surface != 'main':
            raise PolicyError('Rewind files from the main window.')

    def _plan(self, chat_id, run_id, step):
        for value in (chat_id, run_id):
            if not isinstance(value, str) or not 1 <= len(value) <= 128:
                raise PolicyError('Use the scoped conversation and task identities.')
        run = self.app.runs.get(run_id, chat_id)
        if type(step) is not int or not 0 <= step <= run['steps']:
            raise PolicyError(f'Choose a step from 0 (before this task) to {run["steps"]}.')
        if run['state'] in LIVE:
            raise PolicyError('Pause or stop this task before rewinding its files.')
        timelines, busy = self._timelines(chat_id, run_id)
        context = {'policy': self.store.setting('permissions', DEFAULT), 'inspections': {}}
        files, budget = [], [DIFF_TOTAL]
        for key in sorted(timelines):
            entry = self._file(key, timelines[key], step, busy, context, budget)
            if entry is not None:
                files.append(entry)
        if len(files) > MAX_FILES:
            raise PolicyError('This task changed more files than one rewind can review.')
        every = self._effects(chat_id, run_id)
        effects = [e for e in every if e['step'] is None or e['step'] > step]
        history = self._history(chat_id, run_id)
        latest = next((h for h in reversed(history) if h['applied']), None)
        previous = latest['step'] if latest else run['steps']
        reverses = latest['rewind_id'] if latest and step == latest['previous_step'] else None
        sealed = {'chat_id': chat_id, 'run_id': run_id, 'step': step,
                  'files': [[f['_root'], f['path'], f['status'], f['reason'], f['current_hash'], f['target_hash'],
                             f['last_hash'], f['actions']] for f in files],
                  'effects': [[e['call_id'], e['action_id'], e['receipt_id'], e['outcome']] for e in effects]}
        public = [{k: v for k, v in f.items() if not k.startswith('_')} for f in files]
        return {
            'chat_id': chat_id, 'run_id': run_id, 'step': step, 'steps': run['steps'], 'state': run['state'],
            'digest': hashlib.sha256(_canonical(sealed).encode()).hexdigest(),
            'files': public, 'effects': effects[:MAX_EFFECTS], 'effects_omitted': max(0, len(effects) - MAX_EFFECTS),
            'timeline': self._steps(timelines, every),
            'rewinds': history[-20:], 'previous_step': previous, 'reverses': reverses,
            'counts': {'revert': sum(f['status'] == 'revert' for f in files),
                       'refused': sum(f['status'] == 'refused' for f in files),
                       'kept': sum(f['status'] == 'kept' for f in files),
                       'unchanged': sum(f['status'] == 'unchanged' for f in files),
                       'effects': len(effects)},
            'effects_reverted': 0, 'authorization': 'none', '_files': files}

    def _timelines(self, chat_id, run_id):
        """Per (root, path): this run's applied edits, their undos and this run's rewinds, oldest first."""
        owners = {r['action_id']: r['call_id'] for r in self.store.query(
            "SELECT action_id,call_id FROM run_action_owners WHERE run_id=? AND kind='edit'", (run_id,))}
        witnessed = {(r['receipt_id'], r['path'], r['file_hash']) for r in self.store.query(
            'SELECT receipt_id,path,file_hash FROM run_sources WHERE run_id=?', (run_id,))}
        timelines, busy = {}, set()
        for order, action in enumerate(self.store.actions(chat_id)):
            payload = action['payload']
            if action['kind'] != 'edit' or not isinstance(payload, dict):
                continue
            key = (payload.get('root'), payload.get('path'))
            if not all(isinstance(part, str) for part in key):
                continue
            if action['status'] in ('pending', 'running'):
                busy.add(key)  # any owner: approving it later would race the rewind
                continue
            tag = payload.get('rewind') if isinstance(payload.get('rewind'), dict) else {}
            own = action['id'] in owners
            if action['status'] not in ('applied', 'undone') or not (own or tag.get('run_id') == run_id):
                continue
            result = payload.get('result') if isinstance(payload.get('result'), dict) else {}
            before, after, baseline, written = payload.get('before'), payload.get('after'), payload.get('baseline'), result.get('hash')
            problem = None
            if (not isinstance(after, str) or not isinstance(written, str) or digest(after.encode('utf-8')) != written
                    or baseline is not None and (not isinstance(before, str) or digest(before.encode('utf-8')) != baseline)):
                problem = 'record_mismatch'
            elif own and (owners[action['id']], key[1], written) not in witnessed:
                problem = 'unwitnessed'
            line = timelines.setdefault(key, [])
            line.append({'kind': 'run' if own else 'rewind', 'step': _step(owners.get(action['id'])),
                         'time': action['created'], 'order': 2 * order, 'action_id': action['id'],
                         'from': baseline, 'to': written, 'from_text': before if baseline is not None else None,
                         'to_text': after, 'identity': payload.get('root_identity'), 'problem': problem})
            if action['status'] == 'undone':
                undo = payload.get('undo') if isinstance(payload.get('undo'), dict) else {}
                verified = undo.get('verified') is True and undo.get('expected_hash') == baseline
                line.append({'kind': 'undo', 'step': None, 'time': _number(undo.get('undone_at'), action['created']),
                             'order': 2 * order + 1, 'action_id': action['id'], 'from': written, 'to': baseline,
                             'from_text': after, 'to_text': before if baseline is not None else None,
                             'identity': payload.get('root_identity'),
                             'problem': problem or (None if verified else 'undo_unverified')})
        for line in timelines.values():
            line.sort(key=lambda t: (t['time'], t['order']))
        return timelines, busy

    def _file(self, key, line, step, busy, context, budget):
        root, path = key
        runs = [i for i, t in enumerate(line) if t['kind'] == 'run']
        if not runs:
            return None
        steps = [line[i]['step'] for i in runs]
        entry = {'path': path, 'status': 'refused', 'reason': None, 'detail': None, 'current_hash': None, 'target_hash': None,
                 'last_hash': line[-1]['to'], 'steps': [], 'actions': [], 'diff': '', 'diff_truncated': False,
                 '_root': root, '_identity': line[-1]['identity'], '_target_text': None}
        if step == 0:
            cut = 0
        elif None in steps or steps != sorted(steps):
            return self._refuse(entry, 'step_unknown' if None in steps else 'record_mismatch')
        else:
            before = [i for i in runs if line[i]['step'] <= step]
            cut = before[-1] + 1 if before else 0
        if cut >= len(line):
            return None  # nothing this task wrote here after that step
        source = line[0] if cut == 0 else line[cut - 1]
        target_hash = source['from'] if cut == 0 else source['to']
        target_text = source['from_text'] if cut == 0 else source['to_text']
        later = line[cut:]
        entry.update(target_hash=target_hash, _target_text=target_text,
                     steps=sorted({t['step'] for t in later if t['kind'] == 'run' and t['step'] is not None}),
                     actions=sorted({t['action_id'] for t in later}))
        problem = next((t['problem'] for t in ([] if cut == 0 else [source]) + later if t['problem']), None)
        if problem:
            return self._refuse(entry, problem)
        try:
            ws = Workspace(root)
        except (OSError, PolicyError, ValueError):
            return self._refuse(entry, 'root_missing')
        if any(t['identity'] != list(ws.identity) for t in line):
            return self._refuse(entry, 'root_replaced')
        if key in busy:
            return self._refuse(entry, 'pending_edit')
        try:
            data = ws.snapshot(path, missing_ok=True)
        except (OSError, PolicyError, UnicodeError):
            return self._refuse(entry, 'unreadable')
        current = digest(data) if data is not None else None
        entry['current_hash'] = current
        if current == target_hash:
            return self._settle(entry, 'unchanged')
        if current != line[-1]['to']:
            return self._refuse(entry, 'changed_since')
        for index in range(cut, len(line)):
            expected = target_hash if index == cut else line[index - 1]['to']
            if line[index]['from'] != expected:
                return self._refuse(entry, 'interleaved')
        if target_hash is None:
            return self._settle(entry, 'kept', 'created')
        if not context['policy']['tools'].get('edits'):
            return self._refuse(entry, 'edits_disabled')
        if root not in context['inspections']:
            context['inspections'][root] = self.app.process_inspections.pending(root)
        if context['inspections'][root]:
            return self._refuse(entry, 'inspection_pending')
        try:
            self.app.agents.assert_effect_paths(root, path)
        except PolicyError:
            return self._refuse(entry, 'leased')
        now = line[-1]['to_text'] or ''  # None: an undo removed the file; the rewind would write it again
        diff = ''.join(difflib.unified_diff(now.splitlines(True), target_text.splitlines(True),
                                            fromfile=f'a/{path} (now)', tofile=f'b/{path} (after rewind)'))
        shown = diff[:max(0, min(DIFF_CHARS, budget[0]))]
        budget[0] -= len(shown)
        entry.update(status='revert', diff=shown, diff_truncated=len(shown) < len(diff))
        return entry

    @staticmethod
    def _refuse(entry, reason):
        entry.update(status='refused', reason=reason, detail=REASONS[reason])
        return entry

    @staticmethod
    def _settle(entry, status, reason=None):
        reason = reason or status
        entry.update(status=status, reason=reason, detail=REASONS[reason])
        return entry

    def _effects(self, chat_id, run_id):
        """Every non-file effect this run admitted, each marked not reverted (the caller keeps those after a step)."""
        tables = {r['name'] for r in self.store.query("SELECT name FROM sqlite_master WHERE type='table'")}
        actions = {a['id']: a for a in self.store.actions(chat_id)}
        owners = {r['action_id']: r['call_id'] for r in self.store.query(
            'SELECT action_id,call_id FROM run_action_owners WHERE run_id=?', (run_id,))}
        connectors = {r['action_id']: r for r in self.store.query(
            'SELECT action_id,plugin_id,tool,effect_class,state FROM connector_effects WHERE run_id=? ORDER BY created',
            (run_id,))} if 'connector_effects' in tables else {}
        receipts = self.store.query('SELECT id,action_id,operation,state FROM git_plan_receipts WHERE run_id=? '
                                    'ORDER BY created', (run_id,)) if 'git_plan_receipts' in tables else []
        by_action = {r['action_id']: r for r in receipts if r['action_id']}
        items, seen = [], set()
        tools = sorted(self.app.runs.tools(run_id), key=lambda t: (_step(t['call_id']) or 0, t['call_id']))
        for tool in tools:
            kind = KINDS.get(tool['name'])
            at = _step(tool['call_id'])
            if kind is None:
                continue
            result = tool['result'] if isinstance(tool['result'], dict) else {}
            aid = result.get('action_id') if isinstance(result.get('action_id'), str) else None
            outcome = self._outcome(tool, result, actions.get(aid) if aid else None, aid)
            if outcome is None:
                continue
            seen.add(aid)
            payload = actions[aid]['payload'] if aid in actions and isinstance(actions[aid]['payload'], dict) else {}
            receipt = by_action.get(aid)
            items.append(self._effect(kind, tool['name'], at, tool['call_id'], aid, outcome,
                                      self._label(tool['name'], tool['arguments'], payload, connectors.get(aid), receipt),
                                      connectors.get(aid), receipt))
        # Journal rows whose tool receipt is not listed above (a nested or recovered call) still count.
        for aid, row in connectors.items():
            at = _step(owners.get(aid))
            if aid not in seen:
                items.append(self._effect('connector', 'PluginCall', at, owners.get(aid), aid,
                                          'unknown' if row['state'] in ('admitted', 'uncertain', 'unknown') else 'occurred',
                                          self._label('PluginCall', {}, {}, row, None), row, None))
        for row in receipts:
            at = _step(owners.get(row['action_id'])) if row['action_id'] else None
            if row['action_id'] is None or row['action_id'] not in seen:
                items.append(self._effect('git', 'ProposeGitChange', at, owners.get(row['action_id']), row['action_id'],
                                          'unknown' if row['state'] in ('executing', 'uncertain') else 'occurred',
                                          self._label('ProposeGitChange', {}, {}, None, row), None, row))
        return items

    @staticmethod
    def _outcome(tool, result, action, aid):
        if tool['state'] in ('running', 'uncertain'):
            return 'unknown'
        if tool['state'] != 'completed':
            return None  # planned only: never admitted
        if result.get('status') == 'readback_verified':
            return 'occurred' if result.get('effect') == 'occurred' else None
        if result.get('status') == 'user_reported':
            return 'occurred' if result.get('effect') == 'occurred' else 'reported_not_occurred'
        if aid:
            if action is None:
                return 'unknown'
            return {'applied': 'occurred', 'undone': 'occurred', 'running': 'unknown',
                    'pending': 'pending'}.get(action['status'])
        if result.get('error') or result.get('status') == 'not_executed':
            return None
        return 'occurred'

    @staticmethod
    def _label(name, arguments, payload, connector, receipt):
        arguments = arguments if isinstance(arguments, dict) else {}
        if name in ('RunCommand', 'StartCommand'):
            return _clip(arguments.get('command') or payload.get('command') or name)
        if name in ('RunTests', 'RunRecipe'):
            return _clip('Recipe ' + str(arguments.get('recipe') or payload.get('recipe') or ''))
        if name in ('StopProcess', 'WriteProcessInput'):
            return _clip(name + ' ' + str(arguments.get('process_id') or ''))
        if name in ('PluginCall', 'FetchPluginData'):
            source = payload.get('name') or (connector or {}).get('plugin_id') or arguments.get('plugin_id') or 'Connector'
            tool = payload.get('tool') or (connector or {}).get('tool') or arguments.get('tool') or ''
            return _clip(f'{source} / {tool}')
        if name == 'ProposeGitChange':
            operation = (receipt or {}).get('operation') or payload.get('operation') or arguments.get('operation') or ''
            return _clip(f'Git {operation}'.strip())
        if name == 'OpenApp':
            return _clip('Open ' + str(arguments.get('name') or payload.get('name') or 'an app'))
        if name in ('UIRender', 'UIUpdate'):
            return _clip(arguments.get('title') or 'View')
        return name

    @staticmethod
    def _effect(kind, tool, at, call_id, aid, outcome, label, connector, receipt):
        return {'kind': kind, 'tool': tool, 'step': at, 'call_id': call_id, 'action_id': aid,
                'receipt_id': (receipt or {}).get('id'), 'label': label, 'outcome': outcome,
                'outcome_text': OUTCOMES[outcome],
                'connector_class': (connector or {}).get('effect_class'),
                'connector_state': (connector or {}).get('state'),
                'git_state': (receipt or {}).get('state'),
                'reverted': False, 'note': NOTES[kind]}

    def _history(self, chat_id, run_id):
        groups = {}
        for action in self.store.actions(chat_id):
            tag = action['payload'].get('rewind') if isinstance(action['payload'], dict) else None
            if action['kind'] != 'edit' or not isinstance(tag, dict) or tag.get('run_id') != run_id \
                    or not isinstance(tag.get('rewind_id'), str):
                continue
            group = groups.setdefault(tag['rewind_id'], {
                'rewind_id': tag['rewind_id'], 'step': tag.get('step'), 'previous_step': tag.get('previous_step'),
                'reverses': tag.get('reverses'), 'created': action['created'], 'files': 0, 'applied': 0})
            group['files'] += 1
            group['applied'] += action['status'] in ('applied', 'undone')
        return sorted(groups.values(), key=lambda g: g['created'])

    @staticmethod
    def _steps(timelines, effects):
        """Per model step: the files this task wrote and how many other effects it had (for choosing a step)."""
        steps = {}
        for (_, path), line in timelines.items():
            for t in line:
                if t['kind'] == 'run' and t['step'] is not None:
                    steps.setdefault(t['step'], {'step': t['step'], 'files': set(), 'effects': 0})['files'].add(path)
        for effect in effects:
            if effect['step'] is not None:
                steps.setdefault(effect['step'], {'step': effect['step'], 'files': set(), 'effects': 0})['effects'] += 1
        return [{**item, 'files': sorted(item['files'])[:20]} for _, item in sorted(steps.items())][:256]

    # Applying (ordinary reviewed broker writes)

    def _revert(self, chat_id, entry, tag):
        out = {'path': entry['path'], 'status': 'refused', 'reason': None, 'detail': None, 'action_id': None,
               'target_hash': entry['target_hash'], 'readback_hash': None, 'verified': False}
        try:
            ws = Workspace(entry['_root'])
            if list(ws.identity) != entry['_identity']:
                raise PolicyError(REASONS['root_replaced'])
            data = ws.draft(entry['path'], entry['_target_text'])
            if data['baseline'] != entry['current_hash']:
                raise PolicyError('This file changed after the preview. Nothing was written.')
            staged = self.app.propose(chat_id, 'edit', {**data, 'requires_review': True, 'rewind': dict(tag)})
        except (PolicyError, ValueError, OSError) as exc:
            out.update(reason='refused', detail=_clip(exc, 300))
            return out
        aid = out['action_id'] = staged['action_id']
        failure = None
        try:
            self.app.approve(chat_id, aid)
        except (PolicyError, ValueError, HookInterrupted) as exc:  # App.approve's own effect-free classes
            failure = ('refused', _clip(exc, 300))
        except Exception as exc:  # the broker may have written before failing: readback decides
            failure = ('uncertain', 'The write did not settle (' + type(exc).__name__ + '). Inspect the file.')
        finally:
            action = next((a for a in self.store.actions(chat_id) if a['id'] == aid), None)
            if action and action['status'] == 'pending':
                try:
                    self.app.reject(chat_id, aid)  # never leave a staged rewind waiting for a later approval
                except (PolicyError, ValueError):
                    pass
        try:
            now = ws.snapshot(entry['path'], missing_ok=True)
            out['readback_hash'] = digest(now) if now is not None else None
        except (OSError, PolicyError):
            out['readback_hash'] = None
        out['verified'] = out['readback_hash'] == entry['target_hash']
        status = action['status'] if action else None
        if status == 'applied':
            out.update(status='reverted' if out['verified'] else 'diverged', reason=None if out['verified'] else 'diverged',
                       detail=None if out['verified'] else 'Written, but the file read back differently. Inspect it.')
        elif out['verified'] or failure and failure[0] == 'uncertain' or status == 'running':
            # The rewound text is on disk without a broker receipt, or the failure was not a clean refusal.
            out.update(status='uncertain', reason='uncertain', verified=False,
                       detail=failure[1] if failure and failure[0] == 'uncertain' else
                       'The broker did not confirm this write. Inspect the file.')
        else:
            detail = failure[1] if failure else 'The broker did not apply it.'
            if out['readback_hash'] != entry['current_hash']:
                detail += ' The file also changed after the preview.'
            out.update(status='refused', reason='refused', detail=detail, verified=False)
        return out

    @staticmethod
    def _kept(entry):
        return {'path': entry['path'], 'status': entry['status'], 'reason': entry['reason'], 'detail': entry.get('detail'),
                'action_id': None, 'target_hash': entry['target_hash'], 'readback_hash': entry['current_hash'],
                'verified': entry['status'] == 'unchanged'}
