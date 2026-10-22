"""Research store: the planner's state on the run's source ledger (D01/D02).

research.py is pure: it asks the host for a run's plan, verified claim rows and child
assignments. source_ledger.py keeps the run's sources, plans, claims and conflicts. This
bridge maps one onto the other exactly. It refuses rows that the host's own text does not
reproduce, and keeps every record bounded. It is operational evidence, never a memory
layer, and everything leaves with its conversation (forget_chat here and in the ledger).

    ResearchStore(ledger)
    .sources(rid, *, tree=True) -> list; .text(rid, source_id) -> str   # the ledger research.coverage reads
    .plan(rid) -> dict | None; .save_plan(rid, value) -> str               # research_plans, re-derived on read
    .claims(rid, *, origins=PRIOR) -> list; .save_claims(rid, rows) -> dict
    .save_conflicts(rid, conflicts) -> dict                                 # into research_conflicts
    .assignments(rid) -> {agent_id: question_id}; .record_assignment(rid, agent_id, question_id) -> dict
    .research_state(rid) -> {'plan', 'claims', 'assignments'}
    .usage(rid, used=None, *, now=None) -> dict; .admit(rid, kind, budget, used=None, *, now=None) -> dict | None
    .research_plan(rid, args, *, budget, today, human_text, delegation_allowed=False, admission=None) -> dict
    .research_status(rid, args, *, budget, used=None, now=None) -> dict
    .research_progress(rid, *, budget, used=None, now=None) -> dict | None  # D02 coverage note for the runner
    .research_check(rid, answer, *, budget, prior_repairs=0, prior_claims=0, used=None, now=None) -> dict
    .assign_spawn(rid, agent_id, arguments) -> str | None
    .check_child(rid, agent_id, report, *, context=None, now=None) -> dict | None
    .forget_chat(chat_id) -> int
    web_calls(receipts, *, exclude=None) -> {'searches', 'reads', 'queries'[, 'shown']}
    review_refusal(review, agent_id, generation) -> dict                    # for delegation_policy.refuse

Storage. The run's plan is one research_plans row holding the planner's inputs (questions,
the model's extra queries, and today/tier/hosts in budgets); reading it re-runs research.plan
and requires the stored plan_id and caps. A child assignment is that child's sub-plan, another
research_plans row (its question and planned searches, with agent_id in budgets). A claim row is
kept whole in research_claim_rows (this module's table). A precise claim also gets a
research_claims anchor, which the ledger verifies against its own text, so the ledger's
conflict table sees every disagreement. Only reads of research_plans ids and runs.chat_id
touch the ledger's tables directly; every ledger write goes through its API.
"""

import hashlib
import json
import math
from urllib.parse import urlsplit

from . import research
from .research import ResearchError
from .source_ledger import MAX_CLAIMS as LEDGER_BATCH, SEPARATORS

PLAN_FORMAT = 'pebble-research-plan/1'
ASSIGNMENT_FORMAT = 'pebble-research-assignment/1'
ORIGINS = ('answer', 'status', 'agent')
PRIOR = ('status', 'agent')  # the earlier verified rows that research.status and coverage take
MAX_PRIOR_CLAIMS = 1536      # plus one answer's 512 rows stays within research's 2,048-row bound
MAX_ANSWER_CLAIMS = 2048     # answer rows are kept for audit and the ledger's conflicts
MAX_ROW_CHARS = 16384
MAX_CONFLICTS = 512
SPAN_MAX = 2000              # source_ledger's longest supported span
RESOLUTION = {'explained': 'newer_explained', 'dated': 'both_cited'}  # research -> source_ledger
# Tools after which a planned run's coverage can change, so the runner refreshes its coverage note (D02).
PROGRESS_TOOLS = frozenset(('ResearchPlan', 'ResearchStatus', 'WebSearch', 'ReadURL', 'ReadSource', 'ReviewAgentResult'))
PLAN_FIELDS = frozenset(('format', 'plan_id', 'today', 'tier', 'hosts', 'merged_questions', 'caps'))
ASSIGNMENT_FIELDS = frozenset(('format', 'plan_id', 'question_id', 'agent_id'))
CONFLICT_FIELDS = frozenset(('conflict_id', 'key', 'state', 'resolution', 'values'))
VALUE_FIELDS = frozenset(('value', 'text', 'claim_ids', 'source_ids'))
DDL = '''
CREATE TABLE IF NOT EXISTS research_claim_rows(
  run_id TEXT NOT NULL, claim_id TEXT NOT NULL CHECK(length(claim_id)=22), chat_id TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('answer','status','agent')), agent_id TEXT, anchor_id TEXT,
  row TEXT NOT NULL CHECK(length(row) <= 16384), created REAL NOT NULL,
  PRIMARY KEY(run_id,claim_id));
CREATE INDEX IF NOT EXISTS research_claim_rows_chat ON research_claim_rows(chat_id);
'''
TABLES = ('research_claim_rows',)


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _key(value):
    """source_ledger's stored form of a claim key."""
    return value.strip()[:200] if isinstance(value, str) and value.strip() else None


def _value(value):
    """source_ledger's stored form of a claim value."""
    return ' '.join(value.casefold().split())[:200] if isinstance(value, str) and value.strip() else None


def _normalized(text):
    """source_ledger's exact-match normalization: case, whitespace and digit separators."""
    return SEPARATORS.sub('', text.casefold())


def _anchored(row):
    """True when a claim row has a research_claims anchor: a precise row, unless it is supported
    only by a source's recorded dates or by a span longer than the ledger keeps."""
    if row['kind'] is None:
        return False
    span = row['source_span']
    return row['support'] != 'supported' or span['basis'] == 'text' and span['end'] - span['start'] <= SPAN_MAX


def _anchor(row, kinds):
    """The research_claims record of an anchored row (match is added by the caller)."""
    claim = {'sentence_sha256': row['sentence_sha256'], 'token': row['text'], 'support': row['support'],
             'key': row['conflict_key'], 'value': row['value']}
    if row['support'] == 'supported':
        span = row['source_span']
        claim.update(source_id=span['source_id'], span=[span['start'], span['end']])
    else:
        cited = row['cited_source_ids']
        first = next((i for i in cited if kinds.get(i) != 'snippet'), cited[0] if cited else None)
        if first is not None:
            claim['source_id'] = first
    return claim


def web_calls(receipts, *, exclude=None):
    """Parent counts for research.usage: every attempted WebSearch and ReadURL, failures included,
    once per (agent_id, generation, call_id). Rows are run_tools or agent_receipts records
    ({call_id, name, arguments, result, agent_id?, generation?}). A research-cap refusal fetched
    nothing and is not counted; ``exclude`` is the (agent_id, generation, call_id) being admitted
    now, (None, None, call_id) for the root, whose receipt is already journaled.

    ``shown`` (present when a search showed results) pairs each search's query with the [S<n>]
    labels its result listed, including snippets the ledger kept once from an earlier search, so
    research maps a source to every sub-question whose search found it (H19)."""
    seen, searches, reads, queries, shown = set(), 0, 0, [], []
    for receipt in receipts or ():
        if not isinstance(receipt, dict) or receipt.get('name') not in ('WebSearch', 'ReadURL'):
            continue
        ident = (receipt.get('agent_id'), receipt.get('generation'), receipt.get('call_id'))
        result = receipt.get('result')
        refusal = result.get('refusal') if isinstance(result, dict) else None
        if ident in seen or ident == exclude or isinstance(refusal, dict) and refusal.get('kind') == 'research_cap':
            continue
        seen.add(ident)
        if receipt['name'] == 'ReadURL':
            reads += 1
            continue
        searches += 1
        query = (receipt.get('arguments') or {}).get('query') if isinstance(receipt.get('arguments'), dict) else None
        if isinstance(query, str) and query.strip() and len(query) <= 300 and query not in queries:
            queries.append(query)
        listed = result.get('untrusted_search_results') if isinstance(result, dict) else None
        if isinstance(query, str) and query.strip() and len(query) <= 300 and isinstance(listed, list):
            labels = [r['source_id'] for r in listed[:10] if isinstance(r, dict) and isinstance(r.get('source_id'), str)
                      and research.LABEL.fullmatch(r['source_id'])]
            if labels:
                shown.append([query, labels])
    found = {'searches': searches, 'reads': reads, 'queries': queries[-64:]}
    if shown:
        found['shown'] = shown[-64:]
    return found


def review_refusal(review, agent_id, generation):
    """A delegation_policy 'review' refusal (refuse(...)) for a research child whose claim
    records failed the host check. Host counts and fixed labels only."""
    counts = review.get('counts') or {}
    failed = ', '.join(f'{counts[k]} {k}' for k in ('snippet_only', 'uncited', 'unsupported') if counts.get(k))
    if review.get('reason'):
        found = 'no readable claim records (' + str(review['reason'])[:40] + ')'
    else:
        found = failed + ' claim(s)' if failed else 'no claim supported by a page it cites'
    reason = ('Research review refused: the host re-checked this child\'s claim records against the root ledger and '
              'found ' + found + '. Precise values must appear in cited page text; snippets never support them.')
    return {'kind': 'review', 'limit': 'research_claims', 'reason': reason, 'agent_id': agent_id,
            'generation': generation, 'status': 'completed', 'accepted_evidence_ids': [], 'rejected_evidence_ids': [],
            'counts': {k: counts.get(k, 0) for k in research.OUTCOMES}}


class ResearchStore:
    def __init__(self, ledger):
        self.ledger, self.store = ledger, ledger.store
        with self.store.lock:
            self.store.db.executescript('BEGIN IMMEDIATE;' + DDL + 'COMMIT;')

    # -- the ledger as research.py reads it -------------------------------------------------------

    def sources(self, rid, *, tree=True):
        """The ledger's sources for research.py: cite is the [S<n>] label, a snippet row carries
        its shown snippet, and domain is research.registrable_domain's (one rule for both, F2)."""
        rows = self.ledger.sources(rid, tree=tree)
        for row in rows:
            row['cite'] = row['source_id']
            row['domain'] = research.registrable_domain(urlsplit(row['final_url'] or row['url']).hostname) or row['domain']
            if row['kind'] == 'snippet':
                # record_search stores title and snippet joined by one newline (title has none).
                text, title = self.ledger.text(rid, row['source_id']), row['title']
                row['snippet'] = text[len(title) + 1:] if title and text.startswith(title + '\n') else '' if title else text
        return rows

    def text(self, rid, source_id):
        return self.ledger.text(rid, source_id)

    def _reader(self, rid):
        cache = {}

        def text_of(source_id):
            if source_id not in cache:
                cache[source_id] = self.ledger.text(rid, source_id)
            return cache[source_id]
        return text_of

    def _chat(self, rid):
        rows = self.store.query('SELECT chat_id FROM runs WHERE id=?', (rid,)) if isinstance(rid, str) and rid else []
        if not rows:
            raise ResearchError('run_not_found')
        return rows[0]['chat_id']

    # -- plans and assignments (research_plans) ---------------------------------------------------

    def _rows(self, rid):
        """This run's plan and assignment rows as the ledger reads them; other formats are not ours."""
        roots, assigned = [], []
        for row in self.store.query('SELECT id FROM research_plans WHERE run_id=? ORDER BY created, rowid', (rid,)):
            view = self.ledger.plan(rid, row['id'])
            budgets = view['budgets'] if isinstance(view['budgets'], dict) else {}
            if budgets.get('format') == PLAN_FORMAT:
                roots.append(view)
            elif budgets.get('format') == ASSIGNMENT_FORMAT:
                assigned.append(view)
        return roots, assigned

    def _root(self, rid):
        """(plan, its ledger row, assignment rows). The plan is re-derived from its stored inputs
        and must reproduce the stored plan_id and caps."""
        roots, assigned = self._rows(rid)
        if not roots:
            if assigned:
                raise ResearchError('assignment_mismatch')
            return None, None, assigned
        if len(roots) > 1:
            raise ResearchError('duplicate_plan')
        view = roots[0]
        budgets = view['budgets']
        try:
            value = research.plan(view['questions'], today=budgets.get('today'), tier=budgets.get('tier'),
                                  queries=view['queries'], hosts=budgets.get('hosts'))
        except ResearchError:
            value = None
        merged = budgets.get('merged_questions')
        if (value is None or set(budgets) != PLAN_FIELDS or value['plan_id'] != budgets['plan_id']
                or value['caps'] != budgets['caps'] or type(merged) is not int
                or not 0 <= merged <= research.MAX_QUESTIONS - 2):
            raise ResearchError('plan_mismatch')
        value['merged_questions'] = merged
        return value, view, assigned

    @staticmethod
    def _assigned(value, rows):
        out = {}
        for view in rows:
            budgets = view['budgets']
            agent, ident = budgets.get('agent_id'), budgets.get('question_id')
            question = next((q for q in value['questions'] if q['id'] == ident), None) if value else None
            if (question is None or set(budgets) != ASSIGNMENT_FIELDS or budgets['plan_id'] != value['plan_id']
                    or type(agent) is not str or not 0 < len(agent) <= 200 or out.get(agent, ident) != ident
                    or view['questions'] != [question['text']]
                    or view['queries'] != [q['text'] for q in value['queries'] if q['question_id'] == ident]):
                raise ResearchError('assignment_mismatch')
            out[agent] = ident
        return out

    def plan(self, rid):
        """The run's research plan exactly as research.plan returned it, or None."""
        return self._root(rid)[0]

    def save_plan(self, rid, value):
        """Store the run's one plan as its planner inputs; returns the ledger's plan row id.

        A value research.plan would not produce from its own inputs is refused ('bad_plan'), as
        is a second, different plan for the run ('plan_exists'). The same plan again is a no-op."""
        try:
            questions = [q['text'] for q in value['questions']]
            again = research.plan(questions, today=value['today'], tier=value['tier'],
                                  queries=value['model_queries'], hosts=value['hosts'])
        except (ResearchError, TypeError, KeyError):
            again = None
        merged = value.get('merged_questions') if isinstance(value, dict) else None
        if (again is None or type(merged) is not int or not 0 <= merged <= research.MAX_QUESTIONS - 2
                or {**again, 'merged_questions': merged} != value):
            raise ResearchError('bad_plan')
        budgets = {'format': PLAN_FORMAT, 'plan_id': value['plan_id'], 'today': value['today'], 'tier': value['tier'],
                   'hosts': value['hosts'], 'merged_questions': merged, 'caps': value['caps']}
        with self.store.lock:
            stored, view, _ = self._root(rid)
            if stored is not None:
                if stored['plan_id'] == value['plan_id']:
                    return view['plan_id']
                raise ResearchError('plan_exists')
            self._chat(rid)
            try:
                return self.ledger.record_plan(rid, questions, list(value['model_queries']), budgets)
            except ResearchError:
                raise
            except ValueError:
                raise ResearchError('plan_too_large') from None  # budgets over the ledger's 2,000 characters

    def assignments(self, rid):
        """{agent_id: question_id} for children spawned from the synthesis plan."""
        value, _, rows = self._root(rid)
        return self._assigned(value, rows)

    def record_assignment(self, rid, agent_id, question_id):
        """Record that a child researches one plan question (its sub-plan row). One question per
        child, at most the plan's children cap; the same assignment again is a no-op."""
        if type(agent_id) is not str or not 0 < len(agent_id) <= 200 or agent_id == rid:
            raise ResearchError('bad_agent')
        with self.store.lock:
            value, _, rows = self._root(rid)
            if value is None:
                raise ResearchError('no_plan')
            assigned = self._assigned(value, rows)
            question = next((q for q in value['questions'] if q['id'] == question_id), None)
            if question is None:
                raise ResearchError('bad_question')
            if agent_id in assigned:
                if assigned[agent_id] != question_id:
                    raise ResearchError('assignment_conflict')
                return {'agent_id': agent_id, 'question_id': question_id, 'recorded': False}
            if len(assigned) >= value['caps']['children']:
                raise ResearchError('children_cap')
            try:
                self.ledger.record_plan(rid, [question['text']],
                                        [q['text'] for q in value['queries'] if q['question_id'] == question_id],
                                        {'format': ASSIGNMENT_FORMAT, 'plan_id': value['plan_id'],
                                         'question_id': question_id, 'agent_id': agent_id})
            except ResearchError:
                raise
            except ValueError:
                raise ResearchError('assignment_not_stored') from None
        return {'agent_id': agent_id, 'question_id': question_id, 'recorded': True}

    def research_state(self, rid):
        """research.py's {'plan', 'claims', 'assignments'}: claims are the earlier status and child rows."""
        value, _, rows = self._root(rid)
        return {'plan': value, 'claims': self.claims(rid), 'assignments': self._assigned(value, rows)}

    # -- claims (research_claim_rows, anchored in research_claims) --------------------------------

    def save_claims(self, rid, rows):
        """Keep claim rows from research.check, verify_claims or coverage, once per claim_id.

        Every row is re-derived first (research.confirm_claims): its citations, support,
        supporting sources and span must be what this run's sources state. A precise row also
        gets a research_claims anchor, which the ledger verifies against its own text, so its
        conflict table sees every disagreement. Returns counts."""
        chat = self._chat(rid)
        if not isinstance(rows, (list, tuple)):
            raise ResearchError('claims_not_list')
        # Sources are append-only and their texts immutable, so the re-derivation runs outside the
        # store lock; only the write below holds it.
        value, _, assigned = self._root(rid)
        sources, text_of = self.sources(rid), self._reader(rid)
        rows = research.confirm_claims(rows, sources, text_of, plan=value, assignments=self._assigned(value, assigned))
        encoded = [json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) for row in rows]
        if any(len(e) > MAX_ROW_CHARS for e in encoded):
            raise ResearchError('claim_too_large')
        kinds, claims = {s['source_id']: s['kind'] for s in sources}, {}
        for index, row in enumerate(rows):
            if not _anchored(row):
                continue
            claim = _anchor(row, kinds)
            if row['support'] == 'supported':
                span = row['source_span']
                excerpt = text_of(span['source_id'])[span['start']:span['end']]
                if _sha(excerpt) != span['sha256']:
                    raise ResearchError('forged_claim', index)
                claim['match'] = 'exact' if _normalized(row['text']) in _normalized(excerpt) else 'normalized'
            claims[index] = claim
        with self.store.lock:
            known = {r['claim_id']: r['origin'] for r in self.store.query(
                'SELECT claim_id, origin FROM research_claim_rows WHERE run_id=?', (rid,))}
            new = [index for index, row in enumerate(rows) if row['claim_id'] not in known]
            origins = list(known.values()) + [rows[index]['origin'] for index in new]
            if (sum(o in PRIOR for o in origins) > MAX_PRIOR_CLAIMS
                    or sum(o == 'answer' for o in origins) > MAX_ANSWER_CLAIMS):
                raise ResearchError('claims_cap')
            todo = [(index, claims[index]) for index in new if index in claims]
            anchors = {}
            for start in range(0, len(todo), LEDGER_BATCH):
                batch = todo[start:start + LEDGER_BATCH]
                try:
                    recorded = self.ledger.record_claims(rid, [claim for _, claim in batch])
                except ResearchError:
                    raise
                except ValueError:
                    raise ResearchError('anchor_refused') from None
                anchors.update(zip((index for index, _ in batch), recorded['claim_ids']))
            now = float(self.ledger.clock())
            with self.store.db as db:
                db.executemany('INSERT INTO research_claim_rows VALUES(?,?,?,?,?,?,?,?)', [
                    (rid, rows[index]['claim_id'], chat, rows[index]['origin'], rows[index]['agent_id'],
                     anchors.get(index), encoded[index], now) for index in new])
        return {'stored': len(new), 'anchored': len(anchors), 'known': len(rows) - len(new)}

    def claims(self, rid, *, origins=PRIOR):
        """Stored claim rows in the order kept (default: the status and child rows research.status
        and coverage take as earlier claims). A row that differs from its anchor, or an anchor
        that is missing or shared, is refused ('claim_mismatch')."""
        if isinstance(origins, str) or not set(origins) <= set(ORIGINS):
            raise ResearchError('bad_origin')
        stored = self.store.query('SELECT * FROM research_claim_rows WHERE run_id=? ORDER BY created, rowid', (rid,))
        anchors, kinds, used, out = {}, {}, set(), []
        if any(r['anchor_id'] for r in stored):
            anchors = {c['claim_id']: c for c in self.ledger.claims(rid)}
            kinds = {s['source_id']: s['kind'] for s in self.ledger.sources(rid)}
        for index, record in enumerate(stored):
            try:
                row = json.loads(record['row'])
                consistent = self._consistent(record, row, anchors, kinds, used)
            except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                consistent = False
            if not consistent:
                raise ResearchError('claim_mismatch', index)
            if record['anchor_id']:
                used.add(record['anchor_id'])
            if row['origin'] in origins:
                out.append(row)
        return out

    @staticmethod
    def _consistent(record, row, anchors, kinds, used):
        """A stored row matches its columns and its ledger anchor; an unanchored row is a
        paraphrase (never support) or a supported value the ledger cannot hold."""
        if (not isinstance(row, dict) or set(row) != research.CLAIM_FIELDS or row['claim_id'] != record['claim_id']
                or row['origin'] != record['origin'] or row['agent_id'] != record['agent_id']
                or row['support'] not in research.OUTCOMES
                or bool(record['anchor_id']) != _anchored(row) or record['anchor_id'] in used):
            return False
        if not record['anchor_id']:
            if row['kind'] is None:
                return (row['support'] in ('paraphrase_unverified', 'uncited') and not row['supporting_source_ids']
                        and row['source_span'] is None)
            return row['support'] == 'supported'  # by recorded dates, or over the ledger's span length
        anchor, claim = anchors.get(record['anchor_id']), _anchor(row, kinds)
        span = row['source_span'] if row['support'] == 'supported' else None
        return not (anchor is None or anchor['sentence_sha256'] != claim['sentence_sha256']
                    or anchor['token'] != claim['token'] or anchor['support'] != claim['support']
                    or anchor['key'] != _key(claim['key']) or anchor['value'] != _value(claim['value'])
                    or anchor['source_id'] != claim.get('source_id') or anchor['span'] != claim.get('span')
                    or anchor['span_sha256'] != (span['sha256'] if span else None)
                    or (anchor['match'] in ('exact', 'normalized')) != bool(span))

    def save_conflicts(self, rid, conflicts):
        """Record research.find_conflicts states in the ledger's conflict table (one source of
        truth). Each conflict must name stored supported claims under its key. A resolved conflict
        closes the ledger's conflict with the same key when the ledger knows exactly these values
        ('explained' -> newer_explained, 'dated' -> both_cited). Returns host counts and
        research conflict ids: unrecorded (values the ledger has no anchor for), partial (the
        ledger knows more values) and reopen_needed (the ledger says resolved, this check open)."""
        if not isinstance(conflicts, (list, tuple)) or len(conflicts) > MAX_CONFLICTS:
            raise ResearchError('bad_conflicts')
        summary = {'recorded': 0, 'resolved': 0, 'unrecorded': [], 'partial': [], 'reopen_needed': []}
        with self.store.lock:
            rows = {row['claim_id']: row for row in self.claims(rid, origins=ORIGINS)}
            for index, conflict in enumerate(conflicts):
                if not self._conflict_ok(conflict, rows):
                    raise ResearchError('forged_conflict', index)
            known = {c['key']: c for c in self.ledger.conflicts(rid)}
            for conflict in conflicts:
                ident, values = conflict['conflict_id'], {_value(v['value']) for v in conflict['values']}
                found = known.get(_key(conflict['key']))
                if found is None or not values <= set(found['values']):
                    summary['unrecorded'].append(ident)
                    continue
                summary['recorded'] += 1
                if conflict['state'] == 'resolved':
                    if set(found['values']) != values:
                        summary['partial'].append(ident)
                    elif found['state'] == 'open':
                        self.ledger.resolve_conflict(rid, found['conflict_id'], RESOLUTION[conflict['resolution']])
                        summary['resolved'] += 1
                elif found['state'] == 'resolved':
                    summary['reopen_needed'].append(ident)
        return summary

    @staticmethod
    def _conflict_ok(conflict, rows):
        if (not isinstance(conflict, dict) or set(conflict) != CONFLICT_FIELDS or type(conflict['key']) is not str
                or conflict['conflict_id'] != 'x:' + _sha(conflict['key'])[:16]
                or (conflict['state'], conflict['resolution']) not in (('open', None), ('resolved', 'explained'),
                                                                       ('resolved', 'dated'))
                or not isinstance(conflict['values'], list) or not 2 <= len(conflict['values']) <= 64):
            return False
        seen = set()
        for entry in conflict['values']:
            if not isinstance(entry, dict) or set(entry) != VALUE_FIELDS or entry['value'] in seen:
                return False
            seen.add(entry['value'])
            ids = entry['claim_ids']
            claims = [rows.get(i) for i in ids] if isinstance(ids, list) and ids and ids == sorted(set(ids)) else [None]
            if any(c is None or c['support'] != 'supported' or c['conflict_key'] != conflict['key']
                   or c['value'] != entry['value'] for c in claims):
                return False
            if (entry['source_ids'] != sorted({s for c in claims for s in c['supporting_source_ids']})
                    or entry['text'] not in {c['text'] for c in claims}):
                return False
        return True

    # -- caps and usage ----------------------------------------------------------------------------

    def _used(self, rid, used, now):
        """The parent's counts, raised to the host's own: seconds since the plan was stored and the
        children assigned from it. Issued queries pass through for research's pending-query check."""
        if used is not None and not isinstance(used, dict):
            raise ResearchError('bad_usage')
        used = dict(used or {})
        for field in ('seconds', 'children'):
            if field in used and (isinstance(used[field], bool) or not isinstance(used[field], (int, float))):
                raise ResearchError('bad_usage')
        value, view, rows = self._root(rid)
        if view is not None and now is not None:
            if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
                raise ResearchError('bad_now')
            used['seconds'] = max(used.get('seconds', 0), max(0.0, now - view['created']))
        children = len(self._assigned(value, rows))
        if children:
            used['children'] = max(used.get('children', 0), children)
        return used

    def usage(self, rid, used=None, *, now=None):
        """research.usage over the ledger, raised to the parent's counts (web_calls) and the host's."""
        return research.usage(self.sources(rid), self._used(rid, used, now))

    def admit(self, rid, kind, budget, used=None, *, now=None):
        """None when one more search, read or child fits the run's caps (the tier's caps without a
        plan, never above the plan's), else research's typed refusal."""
        limits = research.caps_for(self.plan(rid), research.tier(budget))
        return research.admit(kind, self.usage(rid, used, now=now), limits)

    # -- tool, completion and review adapters ------------------------------------------------------

    def research_plan(self, rid, args, *, budget, today, human_text, delegation_allowed=False, admission=None):
        """ResearchPlan: one plan per run, so caps and variants stay fixed. Primary hosts come only
        from human-origin text (the run goal or the human turn), never from page or model text."""
        if not isinstance(args, dict):
            raise ResearchError('bad_arguments')
        stored = self.plan(rid)
        if stored is not None:
            return research.plan_result(stored)
        value = research.plan(args.get('questions'), today=today, tier=research.tier(budget),
                              queries=args.get('queries'), hosts=research.named_hosts(human_text))
        if value is None:
            return research.plan_result(None)
        self.save_plan(rid, value)
        parallel = research.synthesis_plan(value, budget=budget, delegation_allowed=delegation_allowed,
                                           admission=admission)
        return research.plan_result(value, parallel)

    def research_status(self, rid, args, *, budget, used=None, now=None):
        """ResearchStatus: optional draft claim records checked against the ledger and kept, then
        each sub-question's state inside the run's fixed caps. Needs a plan ('no_plan')."""
        if not isinstance(args, dict):
            raise ResearchError('bad_arguments')
        state = self.research_state(rid)
        if state['plan'] is None:
            raise ResearchError('no_plan')
        sources, text_of = self.sources(rid), self._reader(rid)
        new = research.verify_claims(args.get('claims') or '[]', sources, text_of, origin='status',
                                     plan=state['plan'], assignments=state['assignments'], now=now)
        if new:
            self.save_claims(rid, new)
        return research.status(state['plan'], sources, state['claims'] + new, tier=research.tier(budget),
                               text_of=text_of, used=self._used(rid, used, now),
                               assignments=state['assignments'], now=now)

    def research_progress(self, rid, *, budget, used=None, now=None):
        """D02: the coverage note the runner shows after research steps (research.progress over this run's status,
        with the stored claims; nothing is checked or stored). None at or below 8K, where no plan is offered, and
        for a run without a plan. Its 'text' carries host labels and counts, never page text or URLs."""
        if type(budget) is not int or budget <= research.OFFER_ABOVE:
            return None
        state = self.research_state(rid)
        if state['plan'] is None:
            return None
        result = research.status(state['plan'], self.sources(rid), state['claims'], tier=research.tier(budget),
                                 text_of=self._reader(rid), used=self._used(rid, used, now),
                                 assignments=state['assignments'], now=now)
        return research.progress(result)

    def research_check(self, rid, answer, *, budget, prior_repairs=0, prior_claims=0, used=None, now=None):
        """The completion claim check (controller.completion): research.completion_issues for the
        final answer. Answer rows and the conflicts found are recorded; the answer is never edited.
        A run with no sources and no [S<n>] is not checked. prior_claims: the claims the draft before
        the last research repair made (its research_check record)."""
        if not self.ledger.sources(rid) and not research.CITE.search(answer if isinstance(answer, str) else ''):
            return {'issues': [], 'repairable': True, 'summary': {}, 'unverified': []}
        state = self.research_state(rid)
        report = research.coverage(self, rid, answer, plan=state['plan'], claims=state['claims'], now=now,
                                   used=self._used(rid, used, now), assignments=state['assignments'])
        found = research.completion_issues(report, budget=budget, prior_repairs=prior_repairs, prior_claims=prior_claims)
        ledger = {'claims_stored': 0, 'conflicts_recorded': 0, 'conflicts_resolved': 0, 'conflicts_unrecorded': 0,
                  'audit_full': False}
        try:
            ledger['claims_stored'] = self.save_claims(rid, report['claims'])['stored'] if report['claims'] else 0
        except ResearchError as exc:
            if exc.reason != 'claims_cap':
                raise
            ledger['audit_full'] = True  # answer rows are audit only; the check above stands
        if not ledger['audit_full']:
            recorded = self.save_conflicts(rid, report['conflicts'])
            ledger.update(conflicts_recorded=recorded['recorded'], conflicts_resolved=recorded['resolved'],
                          conflicts_unrecorded=len(recorded['unrecorded']))
        found['summary']['ledger'] = ledger
        return found

    def assign_spawn(self, rid, agent_id, arguments):
        """After a root SpawnAgent is admitted: when its task, name and context are exactly the
        synthesis plan's proposal for a question, record the assignment and return the question
        id; otherwise None (the child is ordinary delegation)."""
        value = self.plan(rid)
        if value is None or not isinstance(arguments, dict):
            return None
        context = arguments.get('context')
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except ValueError:
                return None
        proposal = research.synthesis_plan(value, budget=research.OFFER_ABOVE + 1, delegation_allowed=True,
                                           admission=None)
        for child in proposal['children']:
            proposed = child['arguments']
            if (arguments.get('task') == proposed['task'] and arguments.get('name') == proposed['name']
                    and context == json.loads(proposed['context'])):
                self.record_assignment(rid, agent_id, child['question_id'])
                return child['question_id']
        return None

    def check_child(self, rid, agent_id, report, *, context=None, now=None):
        """Before ReviewAgentResult accepts a research child (assigned, or a context naming
        research_question_id): its report's claim records re-checked against the root ledger.
        None for other children; else {'review': research.review_check result, 'claims': rows}.
        Nothing is stored: call save_claims(rid, result['claims']) once the acceptance commits."""
        state = self.research_state(rid)
        named = isinstance(context, dict) and isinstance(context.get('research_question_id'), str)
        if agent_id not in state['assignments'] and not named:
            return None
        try:
            checked = research.verify_claims(report if isinstance(report, str) else '', self.sources(rid),
                                             self._reader(rid), origin='agent', agent_id=agent_id,
                                             plan=state['plan'], assignments=state['assignments'], now=now)
        except ResearchError as exc:
            return {'review': {'kind': 'research_review', 'authorization': 'none', 'acceptable': False,
                               'counts': {o: 0 for o in research.OUTCOMES}, 'unverified': [], 'reason': exc.reason},
                    'claims': []}
        return {'review': research.review_check(checked), 'claims': checked}

    def forget_chat(self, chat_id):
        """Delete this module's rows of a conversation (the ledger's forget_chat deletes the rest)."""
        with self.store.lock, self.store.db as db:
            return db.execute('DELETE FROM research_claim_rows WHERE chat_id=?', (chat_id,)).rowcount
