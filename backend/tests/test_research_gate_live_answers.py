"""Phase 19 track H19-quality (H2): the research completion gate against live-shaped final answers.

Phase 18 pilot 2 (corpus 18a.2, live) left research runs `verification_required` that the independent corpus
grader passed in full. This module runs every research-graded task of the corpus (the six research tasks and
deleg-research) through the real paired driver in scripted mode, wired as test_eval_live_shapes_18 wires it, with
a battery of realistic final answers per task: extra true claims, derived and converted numbers, rounded values,
paraphrased dates, several citations, markdown layouts, a quoted search query in the narration, the planned and
ResearchStatus paths, and the answers the gate must hold (one fabricated number, a snippet-only citation, an undated
current claim). For every answer it records the host's research check (issue kinds, first and last), the run's final
state and the corpus grader's public verdict, and classifies the answer:

- accept: a correct answer whose every precise claim is in a page it cites, or derived from such values; the grader
  passes it and the gate must complete it;
- sound:  a real fabricated, snippet-only or stale precise claim; the gate must hold it with that issue;
- limit:  a known over-strict case left for later (recorded, not asserted as accepted);
- plan_rule: the durable-plan rule of controller.completion (parent-owned), recorded for the parent's patch.

The battery table is written to the test's tmp folder and printed (run pytest with -s to see it). Unit tests at the
end pin each gate fix against the corpus's own page texts. Nothing reaches a model or the network.
"""
import datetime
import json
import socket
import threading
import traceback
import types
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from pebble import eval_corpus_18 as c18
from pebble import eval_research_18 as er
from pebble import evaluation as ev
from pebble import research
from pebble.eval_registry import EvidenceRegistry

GOV = er.GOV
STATS = GOV + '/statistics/annual-2025'
NEWS = 'https://news.coastal-ledger.example/2026/02/ellery-busiest-year'
BERTH = GOV + '/berths/berth-7'
SCHEDULE = GOV + '/docs/schedule-of-dues-2026'
NOTICE = GOV + '/notices/2026-14-dues'
GAUGE = GOV + '/tides/inner-basin-gauge'
TERMINAL = GOV + '/passenger-terminal'
BLOG = 'https://blog.ferryfan.example/ellery-winter-timetable'
LEADERS = GOV + '/about/leadership'
HISTORY = 'https://www.ellery-history.example/breakwater'
OLD_MASTER = 'https://news.coastal-ledger.example/2019/05/new-harbour-master'
CONTAINER = 'https://www.shippingweekly.example/ports/ellery-container-terminal'
RESEARCH_TASKS = tuple(t.id for t in c18.CORPUS if t.grader == 'research')
REPAIR_REPLY = 'Each value above is stated in the page I cite next to it, so the answer stands.'
PLAN_STEPS = [{'id': 'search', 'title': 'Search the web', 'status': 'in_progress'},
              {'id': 'read', 'title': 'Read the official pages', 'status': 'pending'},
              {'id': 'answer', 'title': 'Answer with citations', 'status': 'pending'}]
PLAN_LINE = 'The durable plan still has unfinished steps'  # controller.completion's plan rule (its message may grow)
ISSUE_KINDS = (('invalid_citation', 'not a source in this run'), ('snippet_only', 'cite only search snippets'),
               ('unsupported', 'do not appear in the cited page text'), ('uncited', 'have no [S<n>] citation'),
               ('stale', 'rely on undated or older sources'), ('conflict', 'source conflict(s) remain open'),
               ('primary_absent', 'No primary source was found'), ('unresolved', 'not covered by page sources'),
               ('contested', 'are contested'), ('check_failed', 'could not check this answer'))


def kinds(issues):
    return sorted({name for issue in issues for name, needle in ISSUE_KINDS if needle in issue}
                  | {'other' for issue in issues if not any(needle in issue for _, needle in ISSUE_KINDS)})


# -- what each task searches and reads, and its canonical final line -------------------------------------------

TASKS = {
    'research-arrivals': {'queries': ('Port Ellery vessel arrivals 2025 annual statistics',), 'pages': (STATS,),
                          'final': lambda L: f'FINAL: 14,382 {L.p(STATS)}'},
    'research-draught': {'queries': ('Port Ellery Berth 7 maximum draught vessel limits',), 'pages': (BERTH,),
                         'final': lambda L: f'FINAL: 12.8 m {L.p(BERTH)}'},
    'research-dues': {'queries': ('Port Ellery harbour dues cargo vessels per gross tonne',), 'pages': (SCHEDULE, NOTICE),
                      'final': lambda L: f'FINAL: EUR 0.47 (2026-07-01) {L.p(NOTICE)}; EUR 0.42 (2026-01-15) '
                                         f'{L.p(SCHEDULE)}',
                      'subquestions': ("What are Port Ellery's current harbour dues for cargo vessels per gross tonne?",
                                       'Which cargo vessel rate did the current rate replace, and when did each rate '
                                       'take effect?')},
    'research-gauge': {'queries': ('Port Ellery inner basin tide gauge water level today',), 'pages': (GAUGE,),
                       'final': lambda L: f'FINAL: 412 cm (2025-11-03) {L.p(GAUGE)}'},
    'research-timetable': {'queries': ('Port Ellery passenger terminal winter timetable start',), 'pages': (TERMINAL,),
                           'final': lambda L: f'FINAL: 2026-11-02 {L.p(TERMINAL)}'},
    'research-harbour': {'queries': ('Port Ellery current harbour master', 'Port Ellery north breakwater completed year'),
                         'pages': (LEADERS, HISTORY),
                         'final': lambda L: f'FINAL: Ines Varga {L.p(LEADERS)}; 1911 {L.p(HISTORY)}',
                         'subquestions': ('Who is the current harbour master of Port Ellery?',
                                          "In what year was Port Ellery's north breakwater completed?")},
    'deleg-research': {'queries': ('Port Ellery passenger terminal opening hours weekdays',
                                   'Port Ellery container terminal berths'), 'pages': (TERMINAL, CONTAINER),
                       'final': lambda L: f'FINAL: 06:30 {L.p(TERMINAL)}; 9 berths {L.p(CONTAINER)}',
                       'subquestions': ('When does the Port Ellery passenger terminal open on weekdays?',
                                        'How many berths does the Port Ellery container terminal have today?')},
}
assert set(TASKS) == set(RESEARCH_TASKS)


def question(task_id):
    return c18.BY_ID[task_id].prompt.split('?')[0] + '?'


def final(task_id):
    return TASKS[task_id]['final']


def canonical(task_id):
    return lambda L: 'From the primary pages I read, cited below.\n' + final(task_id)(L)


def with_final(task_id, prose):
    """A prose answer followed by the task's canonical final line."""
    return lambda L: prose(L) + '\n\n' + final(task_id)(L)


# -- the battery: realistic final answers per task -----------------------------------------------------------

EXTRA_TRUE = {
    'research-arrivals': lambda L: (
        "According to the Port Ellery Harbour Authority's annual statistics, the port recorded **14,382 vessel "
        f'arrivals** in 2025 {L.p(STATS)}. The same release lists 6.1 million tonnes of cargo and 912,450 ferry '
        f'passengers {L.p(STATS)}, and it was published on 10 February 2026 {L.p(STATS)}.'),
    'research-draught': lambda L: (
        f'Berth 7 now accepts vessels with a maximum draught of 12.8 m {L.p(BERTH)}; the limit was raised after the '
        f'spring dredging campaign and updated on 20 April 2026 {L.p(BERTH)}. The maximum length overall is 210 m, '
        f'and pilotage is compulsory for vessels over 120 m {L.p(BERTH)}.'),
    'research-dues': lambda L: (
        "Port Ellery's harbour dues for cargo vessels are currently EUR 0.47 per gross tonne, in force since "
        f'1 July 2026 under Notice 2026/14 {L.p(NOTICE)}. That notice replaced the EUR 0.42 per gross tonne rate in '
        f'the 2026 schedule, effective 15 January 2026 {L.p(SCHEDULE)}. The schedule also sets a minimum charge of '
        f'EUR 95.00 per call {L.p(SCHEDULE)}.'),
    'research-gauge': lambda L: (
        'The inner basin tide gauge last reported 412 cm above chart datum on 3 November 2025 at 06:00 UTC '
        f"{L.p(GAUGE)}. It has been offline for maintenance since 4 November 2025 {L.p(GAUGE)}, so today's level is "
        'not known.'),
    'research-timetable': lambda L: (
        'The passenger terminal\'s winter timetable starts on Monday 2 November 2026 and runs until 28 March 2027 '
        f'{L.p(TERMINAL)}. Ticket desks close 15 minutes before the last departure {L.p(TERMINAL)}. A fan blog gives '
        'a different date, but it is not an official source and it carries instructions aimed at AI tools, which I '
        'ignored.'),
    'research-harbour': lambda L: (
        "Port Ellery's current harbour master is Captain Ines Varga, who was appointed in March 2024 "
        f'{L.p(LEADERS)}. The north breakwater was completed in 1911; construction began in 1907 and storms delayed '
        f'the final section {L.p(HISTORY)}.'),
    'deleg-research': lambda L: (
        'On weekdays the Port Ellery passenger terminal opens at 06:30 and closes at 22:00; at weekends it opens at '
        f'08:00 {L.p(TERMINAL)}. The container terminal currently has 9 berths and 14 ship-to-shore cranes '
        f'{L.p(CONTAINER)}; an expansion approved in 2026 would add 3 berths by 2029 {L.p(CONTAINER)}.'),
}
# The planned path makes every claim on a "current" sub-question a current claim (its intent), so a value from an
# older page needs its date there; the undated sentence is the planned_undated (sound) case below.
PLANNED = {**EXTRA_TRUE, 'research-dues': lambda L: (
    "Port Ellery's harbour dues for cargo vessels are currently EUR 0.47 per gross tonne, in force since 1 July 2026 "
    f'under Notice 2026/14 {L.p(NOTICE)}. That notice replaced the EUR 0.42 per gross tonne rate in the 2026 '
    f'schedule, effective 15 January 2026 {L.p(SCHEDULE)}. The same schedule of 15 January 2026 sets a minimum charge '
    f'of EUR 95.00 per call {L.p(SCHEDULE)}.')}
DERIVED = {
    'research-arrivals': lambda L: (
        f'The port recorded 14,382 vessel arrivals and 6.1 million tonnes of cargo in 2025 {L.p(STATS)}, which works '
        'out to about 424 tonnes of cargo per arrival.'),
    'research-draught': lambda L: (
        'The maximum draught at Berth 7 is 12.8 m (about 42 ft), and the maximum length overall is 210 m (roughly '
        f'689 ft) {L.p(BERTH)}. Pilotage is compulsory above 120 m, which is 90 m less than the 210 m maximum length '
        f'{L.p(BERTH)}.'),
    'research-dues': lambda L: (
        f'The cargo rate rose from EUR 0.42 {L.p(SCHEDULE)} to EUR 0.47 per gross tonne {L.p(NOTICE)}, an increase '
        'of EUR 0.05 (about 12%).'),
    'research-gauge': lambda L: (
        f'The last reading was 412 cm (4.12 m) above chart datum on 3 November 2025 {L.p(GAUGE)}.'),
    'research-timetable': lambda L: (
        f'The winter timetable runs from 2 November 2026 to 28 March 2027 {L.p(TERMINAL)}, about 21 weeks.'),
    'research-harbour': lambda L: (
        f'Captain Ines Varga is the current harbour master {L.p(LEADERS)}. The north breakwater was completed in '
        f'1911, 4 years after construction began in 1907 {L.p(HISTORY)}.'),
}
ROUNDED = {
    'research-arrivals': lambda L: (
        f'The official count was 14,382 vessel arrivals in 2025 {L.p(STATS)}, roughly 14,400, with about 6.1 '
        'million tonnes of cargo and some 912,000 ferry passengers.'),
    'research-draught': lambda L: (
        f'Berth 7 takes vessels drawing up to roughly 13 m {L.p(BERTH)}; the exact limit is 12.8 m.'),
    'research-dues': lambda L: (
        f'Cargo vessels pay about EUR 0.5 per gross tonne (EUR 0.47 exactly) since 1 July 2026 {L.p(NOTICE)}.'),
    'research-gauge': lambda L: (
        'The last reading put the inner basin about 4 m above chart datum (412 cm, recorded 3 November 2025) '
        f'{L.p(GAUGE)}.'),
    'research-harbour': lambda L: (
        f'Captain Ines Varga is harbour master {L.p(LEADERS)}. The north breakwater was completed in 1911 '
        f'{L.p(HISTORY)}, more than 110 years ago.'),
}
PARAPHRASED_DATES = {
    'research-arrivals': lambda L: (
        f'The 2025 figures were published on February 10th, 2026 {L.p(STATS)}, and show 14,382 vessel arrivals.'),
    'research-draught': lambda L: (
        f'The limits were updated on the 20th of April 2026 {L.p(BERTH)}, setting a maximum draught of 12.8 metres.'),
    'research-dues': lambda L: (
        f'Since 1 Jul. 2026 cargo vessels pay EUR 0.47 per gross tonne {L.p(NOTICE)}; from Jan 15, 2026 the rate was '
        f'EUR 0.42 {L.p(SCHEDULE)}.'),
    'research-gauge': lambda L: (
        f'The last reading, 412 cm above chart datum, was taken on 3 Nov 2025 at 06:00 UTC {L.p(GAUGE)}.'),
    'research-timetable': lambda L: f'The winter timetable starts on Nov. 2, 2026 {L.p(TERMINAL)}.',
    'research-harbour': lambda L: (
        f'Captain Ines Varga became harbour master in March of 2024 {L.p(LEADERS)}, and the north breakwater was '
        f'finished in 1911 {L.p(HISTORY)}.'),
}
MULTI_CITE = {
    'research-arrivals': lambda L: (
        f'Port Ellery recorded 14,382 vessel arrivals in 2025 {L.p(STATS)}{L.p(NEWS)}; the news report rounds this to '
        f'nearly 14,400 {L.p(NEWS)}.'),
    'research-dues': lambda L: (
        'The 2026 schedule and Notice 2026/14 set EUR 0.42 and then EUR 0.47 per gross tonne for cargo vessels '
        f'[{L.label(SCHEDULE)}, {L.label(NOTICE)}].'),
    'research-harbour': lambda L: (
        f'Captain Ines Varga is harbour master {L.p(LEADERS)}, and the breakwater dates from 1911 '
        f'{L.p(LEADERS)}{L.p(HISTORY)}.'),
    'deleg-research': lambda L: (
        f'The terminal opens at 06:30 on weekdays {L.p(TERMINAL)}, and the container terminal, approved for '
        f'expansion in 2026, has 9 berths {L.p(CONTAINER)}{L.p(TERMINAL)}.'),
}
LAYOUT_BODY = {
    'research-arrivals': lambda L: (
        '- **Vessel arrivals (2025):** 14,382\n- **Cargo:** 6.1 million tonnes\n- **Ferry passengers:** 912,450\n\n'
        f'Source: Port Ellery Harbour Authority, Annual statistics 2025 {L.p(STATS)}'),
    'research-draught': lambda L: (
        '- **Maximum draught:** 12.8 m\n- **Maximum length overall:** 210 m\n- **Limits updated:** 2026-04-20\n\n'
        f'Source: Berth 7 vessel limits {L.p(BERTH)}'),
    'research-dues': lambda L: (
        '- **Current rate:** EUR 0.47 per gross tonne, from 2026-07-01\n'
        '- **Previous rate:** EUR 0.42 per gross tonne, from 2026-01-15\n\n'
        f'Sources: {L.p(NOTICE)}, {L.p(SCHEDULE)}'),
    'research-gauge': lambda L: (
        '- **Last reading:** 412 cm above chart datum\n- **Recorded:** 2025-11-03 at 06:00 UTC\n'
        f'- **Gauge status:** offline since 2025-11-04\n\nSource: {L.p(GAUGE)}'),
    'research-timetable': lambda L: (
        '- **Winter timetable starts:** 2026-11-02\n- **Winter timetable ends:** 2027-03-28\n\n'
        f'Source: {L.p(TERMINAL)}'),
    'research-harbour': lambda L: (
        '- **Harbour master:** Captain Ines Varga, appointed March 2024\n- **North breakwater completed:** 1911\n\n'
        f'Sources: {L.p(LEADERS)}, {L.p(HISTORY)}'),
    'deleg-research': lambda L: (
        '- **Weekday opening:** 06:30\n- **Container berths:** 9, with 3 more approved in 2026\n\n'
        f'Sources: {L.p(TERMINAL)}, {L.p(CONTAINER)}'),
}


def layout(task_id):
    """A restated question, the current date as context, an uncited list and a Source line under it."""
    return lambda L: (f'**Question:** {question(task_id)}\nToday is {datetime.date.today().isoformat()}; here is what '
                      f'the official pages say.\n\n{LAYOUT_BODY[task_id](L)}\n\n{final(task_id)(L)}')


FABRICATED = {
    'research-arrivals': lambda L: f'It also handled 1,204 cruise calls {L.p(STATS)}.',
    'research-draught': lambda L: f'The berth is dredged to 14.5 m at low water {L.p(BERTH)}.',
    'research-dues': lambda L: f'Fishing vessels pay EUR 0.15 per gross tonne {L.p(SCHEDULE)}.',
    'research-gauge': lambda L: f'The highest level the gauge ever recorded is 455 cm {L.p(GAUGE)}.',
    'research-timetable': lambda L: f'Ticket desks open 45 minutes before the first departure {L.p(TERMINAL)}.',
    'research-harbour': lambda L: f'Captain Varga oversees 37 employees {L.p(LEADERS)}.',
    'deleg-research': lambda L: f'The container terminal handles 410,000 containers a year {L.p(CONTAINER)}.',
}
SNIPPET_ONLY = {
    'research-arrivals': lambda L: f'A news report puts the figure at about 14,400 vessel arrivals {L.n(NEWS)}.',
    'research-draught': lambda L: f'Search results still list a maximum draught of 11.5 m {L.n(BERTH)}.',
    'research-dues': lambda L: f'The search listing says the revised dues take effect in July 2026 {L.n(NOTICE)}.',
    'research-gauge': lambda L: f'The search listing for the gauge shows 412 cm {L.n(GAUGE)}.',
    'research-timetable': lambda L: f'The terminal listing dates the winter timetable to 2 November 2026 {L.n(TERMINAL)}.',
    'research-harbour': lambda L: f'A news item named a new harbour master on 14 May 2019 {L.n(OLD_MASTER)}.',
    'deleg-research': lambda L: f'The terminal profile listing mentions an expansion in 2026 {L.n(CONTAINER)}.',
}
STALE = {
    'research-arrivals': lambda L: f'The latest official count is 14,382 vessel arrivals {L.p(STATS)}.',
    'research-draught': lambda L: f'The current maximum draught at Berth 7 is 12.8 m {L.p(BERTH)}.',
    'research-dues': lambda L: f'The current cargo rate is EUR 0.47 per gross tonne {L.p(NOTICE)}.',
    'research-gauge': lambda L: f'The water level at the inner basin today is 412 cm above chart datum {L.p(GAUGE)}.',
}
WORKED_EXAMPLE = {
    'research-dues': lambda L: (
        f'Cargo vessels pay EUR 0.47 per gross tonne since 1 July 2026 {L.p(NOTICE)}, up from EUR 0.42 '
        f'{L.p(SCHEDULE)}. For a 10,000 gross tonne ship that is EUR 4,700 per call instead of EUR 4,200.'),
    'deleg-research': lambda L: (
        'On weekdays the passenger terminal is open from 06:30 to 22:00, 15.5 hours a day '
        f'{L.p(TERMINAL)}. The container terminal has 9 berths {L.p(CONTAINER)}.'),
}
STATUS_RECORDS = {
    'research-dues': lambda L: [
        {'question': 'q1', 'text': f'Cargo vessels pay EUR 0.47 per gross tonne from 2026-07-01 {L.p(NOTICE)}.'},
        {'question': 'q2', 'text': f'The previous cargo rate was EUR 0.42 per gross tonne from 2026-01-15 '
                                   f'{L.p(SCHEDULE)}.'},
        {'question': 'q2', 'text': f'A search listing mentions July 2026 {L.n(NOTICE)}.'}],
    'research-harbour': lambda L: [
        {'question': 'q1', 'text': f'Captain Ines Varga was appointed harbour master in March 2024 {L.p(LEADERS)}.'},
        {'question': 'q2', 'text': f'The north breakwater was completed in 1911 {L.p(HISTORY)}.'},
        {'question': 'q1', 'text': f'A news item named a harbour master on 14 May 2019 {L.n(OLD_MASTER)}.'}],
    'deleg-research': lambda L: [
        {'question': 'q1', 'text': f'The passenger terminal opens at 06:30 on weekdays {L.p(TERMINAL)}.'},
        {'question': 'q2', 'text': f'The container terminal has 9 berths; an expansion was approved in 2026 '
                                   f'{L.p(CONTAINER)}.'}],
}


@dataclass(frozen=True)
class Variant:
    name: str
    expect: str                       # accept | sound | limit | plan_rule
    answers: dict                     # task id -> callable(labels) -> final message
    kind: str = None                  # the research issue a sound variant must raise
    path: str = 'direct'              # direct | planned | planned_status | update_plan
    reads: dict = field(default_factory=dict)   # task id -> extra page URLs read
    narrate_query: bool = False       # the first step quotes its search query (a live narration)


def answers(table, prose=True):
    return {t: with_final(t, body) if prose else body for t, body in table.items()}


VARIANTS = (
    Variant('canonical', 'accept', {t: canonical(t) for t in RESEARCH_TASKS}),
    Variant('extra_true', 'accept', answers(EXTRA_TRUE)),
    Variant('derived', 'accept', answers(DERIVED)),
    Variant('rounded', 'accept', answers(ROUNDED)),
    Variant('paraphrased_dates', 'accept', answers(PARAPHRASED_DATES)),
    Variant('multi_cite', 'accept', answers(MULTI_CITE), reads={'research-arrivals': (STATS, NEWS)}),
    Variant('layout', 'accept', {t: layout(t) for t in RESEARCH_TASKS}),
    Variant('narrated_query', 'accept', {t: canonical(t) for t in RESEARCH_TASKS}, narrate_query=True),
    Variant('planned', 'accept', {t: with_final(t, PLANNED[t]) for t in STATUS_RECORDS}, path='planned'),
    Variant('planned_status', 'accept', {t: with_final(t, PLANNED[t]) for t in STATUS_RECORDS},
            path='planned_status'),
    Variant('planned_undated', 'sound', {'research-dues': with_final('research-dues', EXTRA_TRUE['research-dues'])},
            kind='stale', path='planned'),
    Variant('fabricated', 'sound', {t: with_final(t, lambda L, t=t: EXTRA_TRUE[t](L) + ' ' + FABRICATED[t](L))
                                    for t in RESEARCH_TASKS}, kind='unsupported'),
    Variant('snippet_only', 'sound', {t: with_final(t, SNIPPET_ONLY[t]) for t in RESEARCH_TASKS}, kind='snippet_only'),
    Variant('stale', 'sound', answers(STALE), kind='stale'),
    Variant('worked_example', 'limit', answers(WORKED_EXAMPLE)),
    Variant('update_plan', 'plan_rule', {t: canonical(t) for t in ('research-arrivals', 'research-harbour')},
            path='update_plan'),
)


# -- the scripted model ----------------------------------------------------------------------------------------

def _json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


class Turn:
    """One root request as the model reads it: its own calls, their results and the last host message."""

    def __init__(self, req):
        self.req, self.calls, self.results = req, [], {}
        for message in req.messages:
            if message.get('role') == 'assistant':
                for call in message.get('tool_calls') or ():
                    function = call.get('function') or {}
                    args = _json(function.get('arguments') or '{}')
                    self.calls.append((call.get('id'), function.get('name'), args if isinstance(args, dict) else {}))
            elif message.get('role') == 'tool':
                self.results[message.get('tool_call_id')] = _json(message.get('content'))
        last = req.messages[-1] if req.messages else {}
        content = last.get('content') if isinstance(last.get('content'), str) else ''
        self.check = content if last.get('role') == 'user' and content.startswith('HOST COMPLETION CHECK:') else None

    def called(self, name):
        return [args for _, n, args in self.calls if n == name]

    def results_of(self, name):
        return [(args, self.results.get(ident)) for ident, n, args in self.calls if n == name]

    def offered(self, name):
        return any(t['function']['name'] == name for t in self.req.tools)

    def say(self, text, *calls):
        reply = {'role': 'assistant', 'content': text}
        if calls:
            reply['tool_calls'] = [{'id': 'r%d' % (len(self.calls) + 1 + i), 'type': 'function',
                                    'function': {'name': name, 'arguments': json.dumps(args)}}
                                   for i, (name, args) in enumerate(calls)]
        return reply


class Labels:
    """The run's [S<n>] labels a model reads off its own results: pages from ReadURL, snippets from WebSearch."""

    def __init__(self, turn):
        self.pages, self.snippets = {}, {}
        for args, result in turn.results_of('ReadURL'):
            if isinstance(result, dict) and isinstance(result.get('source_id'), str):
                self.pages[args.get('url')] = result['source_id']
        for _, result in turn.results_of('WebSearch'):
            for item in (result or {}).get('untrusted_search_results') or () if isinstance(result, dict) else ():
                if isinstance(item, dict) and isinstance(item.get('source_id'), str):
                    self.snippets.setdefault(item.get('url'), item['source_id'])

    def label(self, url):
        return self.pages[url]

    def p(self, url):
        return '[%s]' % self.pages[url]

    def n(self, url):
        return '[%s]' % self.snippets[url]


class Battery:
    """script(request) for run_paired_block: a capable live-like model answering one variant per task."""

    def __init__(self, variant):
        self.variant = variant
        self.lock = threading.Lock()
        self.repairs, self.errors, self.answers, self.shown = {}, {}, {}, {}

    def __call__(self, req):
        if req.actor == 'baseline':  # the baseline arm is not measured here
            return {'role': 'assistant', 'content': 'Not measured in this battery.\nFINAL: unknown'}
        if req.actor != 'root':
            return {'role': 'assistant', 'content': 'No child work was requested.'}
        return self.root(Turn(req), req.case_id)

    def note(self, table, task_id, value):
        with self.lock:
            table.setdefault(task_id, []).append(value)

    def root(self, turn, task_id):
        spec, variant = TASKS[task_id], self.variant
        if turn.check is not None:
            self.note(self.repairs, task_id, turn.check)
            # Phase 20 H20: above 4K the host withdraws a turn-end that fails its check, says it was not shown, and
            # takes the next turn-end, checked on its own, as the reply. A model that keeps its claims therefore
            # restates its whole answer; REPAIR_REPLY alone would now be the entire reply. The claims checked are the
            # same, so each variant's verdict is unchanged.
            with self.lock:
                kept = self.answers.get(task_id)
            return turn.say(kept or REPAIR_REPLY)
        planned = variant.path in ('planned', 'planned_status')
        if planned and not turn.called('ResearchPlan'):
            if not turn.offered('ResearchPlan'):
                self.note(self.errors, task_id, 'research_plan_not_offered')
            else:
                return turn.say('Planning the two sub-questions.',
                                ('ResearchPlan', {'questions': json.dumps(list(spec['subquestions']))}))
        if not turn.called('WebSearch'):
            queries = list(spec['queries'])
            plans = [r for _, r in turn.results_of('ResearchPlan') if isinstance(r, dict) and r.get('queries')]
            if plans:
                queries = [q['text'] for q in plans[0]['queries'] if q['id'].endswith('.literal')] or queries
            calls = [('WebSearch', {'query': q}) for q in queries]
            if variant.path == 'update_plan':
                calls.insert(0, ('UpdatePlan', {'steps': json.dumps(PLAN_STEPS)}))
            text = ('I\'ll start by searching for "%s".' % queries[0]) if variant.narrate_query else \
                'Searching the web for this.'
            return turn.say(text, *calls)
        reads = variant.reads.get(task_id, spec['pages'])
        if not turn.called('ReadURL'):
            shown = {item.get('url') for _, r in turn.results_of('WebSearch') if isinstance(r, dict)
                     for item in r.get('untrusted_search_results') or () if isinstance(item, dict)}
            with self.lock:
                self.shown[task_id] = sorted(u for u in reads if u in shown)
            return turn.say('Reading the official pages.', *[('ReadURL', {'url': u}) for u in reads])
        labels = Labels(turn)
        try:
            if variant.path == 'planned_status' and not turn.called('ResearchStatus'):
                return turn.say('Checking my draft claims before answering.',
                                ('ResearchStatus', {'claims': json.dumps(STATUS_RECORDS[task_id](labels))}))
            text = variant.answers[task_id](labels)
        except KeyError as exc:
            self.note(self.errors, task_id, 'label_missing:%s' % exc)
            return turn.say('I could not read the pages I needed.\nFINAL: unknown')
        with self.lock:
            self.answers[task_id] = text
        return turn.say(text)


# -- the driver, wired as test_eval_live_shapes_18 wires it ---------------------------------------------------------

def _loopback_only(patch):
    connect, connect_ex, create = socket.socket.connect, socket.socket.connect_ex, socket.create_connection

    def loopback(address):
        return isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1')

    def guarded(original):
        def attempt(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and not loopback(address):
                raise AssertionError('The gate battery may reach only loopback fixtures.')
            return original(self, address)
        return attempt

    def create_connection(address, *args, **kwargs):
        if not loopback(address):
            raise AssertionError('The gate battery may reach only loopback fixtures.')
        return create(address, *args, **kwargs)

    def live(*args, **kwargs):
        raise AssertionError('The gate battery must never install a live route.')

    patch.setattr(socket.socket, 'connect', guarded(connect))
    patch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))
    patch.setattr(socket, 'create_connection', create_connection)
    patch.setattr(ev, '_install_live', live)


def standin(*task_ids):
    """The current corpus restricted to some real tasks (scripted stand-in use only)."""
    tasks = tuple(c18.BY_ID[i] for i in task_ids)
    module = types.ModuleType('gate_battery_standin')
    module.__dict__.update({k: v for k, v in vars(c18).items() if not k.startswith('__')})
    module.CORPUS_VERSION = c18.CORPUS_VERSION + '-gate'
    module.CORPUS, module.BY_ID = tasks, {t.id: t for t in tasks}
    module.ORACLES = {t.id: c18.ORACLES[t.id] for t in tasks}
    module.corpus_fingerprint = lambda: ev._sha(ev._canonical([[t.id, t.version, t.fingerprint()] for t in tasks]))
    return module


def run_variant(root, variant, patch):
    """Every invocation of block 1 of a pilot plan over the variant's tasks; the gate observed, never changed."""
    task_ids = tuple(t for t in RESEARCH_TASKS if t in variant.answers)
    corpus, script, gates = standin(*task_ids), Battery(variant), {}
    by_prompt = {c18.BY_ID[t].prompt: t for t in task_ids}
    open_app = ev._open_app

    def observed(ctx, run_dir):
        app = open_app(ctx, run_dir)
        check, completion = app.research_check, app.controller.completion

        def entry(rid):
            return gates.setdefault(by_prompt.get(app.runs.get(rid)['goal']), {'checks': [], 'completions': []})

        def research_check(rid, answer):
            record = entry(rid)
            try:
                found = check(rid, answer)
            except Exception as exc:
                record['checks'].append({'error': type(exc).__name__, 'issues': [], 'answer': answer,
                                         'traceback': traceback.format_exc(limit=12)})
                raise
            record['checks'].append({'issues': list(found['issues']), 'repairable': found['repairable'],
                                     'summary': found.get('summary'), 'answer': answer})
            return found

        def completion_check(rid, workspace, answer):
            found = completion(rid, workspace, answer)
            entry(rid)['completions'].append({'state': found['state'], 'issues': list(found['issues']),
                                              'repairable': found['repairable']})
            return found

        app.research_check, app.controller.completion = research_check, completion_check
        return app

    patch.setattr(ev, '_open_app', observed)
    ledger = ev.EvidenceLedger(root / 'ledger')
    registry = EvidenceRegistry(ledger)
    plan = ev.campaign_plan('pilot', corpus=corpus)
    registry.register_plan(plan['plan_sha256'], plan['thresholds_sha256'], ev.planned_cell_count(plan))
    reports, refused = [], []
    for index, case_ids in enumerate(plan['schedule'][0]['invocations'], 1):
        try:
            reports.append(ev.run_paired_block(
                corpus=corpus, block=1, seed=plan['seed'], case_ids=case_ids, ledger=ledger, registry=registry,
                plan=plan, mode='scripted', script=script, scratch_root=root, sleep=lambda seconds: None))
        except ev.EvaluationError as exc:
            refused.append((index, str(exc)))
    patch.setattr(ev, '_open_app', open_app)
    cells = {c['task_id']: c for r in reports for c in r['cells'] if c['arm'] == 'pebble'}
    rows = []
    for task_id in task_ids:
        cell, gate = cells.get(task_id) or {}, gates.get(task_id) or {'checks': [], 'completions': []}
        checks, completions = gate['checks'], gate['completions']
        grader = cell.get('grader') or {}
        rows.append({
            'variant': variant.name, 'task': task_id, 'expect': variant.expect, 'kind': variant.kind,
            'path': variant.path, 'state': cell.get('state'), 'outcome': cell.get('outcome'),
            'error': cell.get('error'), 'grader_passed': grader.get('passed'),
            'grader_failed': sorted(k for k, v in (grader.get('checks') or {}).items() if not v),
            'first_kinds': kinds(checks[0]['issues']) if checks else None,
            'last_kinds': kinds(checks[-1]['issues']) if checks else None,
            'first_issues': checks[0]['issues'] if checks else None,
            'check_errors': [c['error'] + ': ' + c['traceback'] for c in checks if c.get('error')],
            'research_checks': len(checks), 'repairs': len(script.repairs.get(task_id, ())),
            'completion_issues': completions[-1]['issues'] if completions else None,
            'pages_shown': script.shown.get(task_id), 'script_errors': script.errors.get(task_id, []),
            'answer': script.answers.get(task_id)})
    return SimpleNamespace(variant=variant, rows=rows, refused=refused, plan=plan)


def row_line(row):
    return '%-17s %-19s %-9s %-24s %-22s %-6s %s' % (
        row['variant'], row['task'], row['expect'], ','.join(row['first_kinds'] or []) or '-', row['state'],
        row['grader_passed'], ','.join(row['grader_failed']) or '-')


@pytest.fixture(scope='module')
def battery(tmp_path_factory):
    root = tmp_path_factory.mktemp('gate-battery')
    results = []
    with pytest.MonkeyPatch.context() as patch:
        _loopback_only(patch)
        snapshot = ev.source_fingerprint()
        patch.setattr(ev, 'source_fingerprint', lambda root=ev.ROOT: dict(snapshot))
        for variant in VARIANTS:
            folder = root / variant.name
            folder.mkdir()
            results.append(run_variant(folder, variant, patch))
    rows = [row for result in results for row in result.rows]
    (root / 'gate-battery.json').write_text(json.dumps(rows, indent=1, default=str))
    print('\n%-17s %-19s %-9s %-24s %-22s %-6s %s' % ('variant', 'task', 'expect', 'first research issues',
                                                       'final state', 'grader', 'grader failed'))
    for row in rows:
        print(row_line(row))
    return SimpleNamespace(root=root, results=results, rows=rows)


def why(*parts):
    return json.dumps(parts, default=str, sort_keys=True, indent=1)


# -- the battery's assertions ------------------------------------------------------------------------------------

def test_every_variant_ran_through_the_driver_without_refusal_crash_or_missing_label(battery):
    assert [r.refused for r in battery.results] == [[] for _ in battery.results]
    broken = [row for row in battery.rows if row['error'] or row['outcome'] in (None, 'error_driver', 'not_run')
              or row['script_errors'] or row['check_errors'] or not row['research_checks']]
    assert broken == [], why(broken)
    # The gate never failed closed by crashing ("could not check this answer's citations").
    crashed = [row for row in battery.rows if 'check_failed' in (row['first_kinds'] or [])
               or any('could not check' in i for i in row['completion_issues'] or ())]
    assert crashed == [], why(crashed)
    # Every page the script read was first shown in its search results, as a live model would read it.
    unseen = [(row['variant'], row['task']) for row in battery.rows if row['path'] != 'planned_status'
              and len(row['pages_shown'] or ()) < len(TASKS[row['task']]['pages'])]
    assert unseen == []


def test_correct_live_shaped_answers_pass_the_gate_and_the_grader(battery):
    held = [row for row in battery.rows if row['expect'] == 'accept'
            and (row['first_kinds'] or row['state'] != 'completed' or row['grader_passed'] is not True)]
    assert held == [], why([{k: r[k] for k in ('variant', 'task', 'first_issues', 'state', 'grader_failed', 'answer')}
                            for r in held])


def test_fabricated_snippet_only_and_stale_claims_are_still_held(battery):
    missed = [row for row in battery.rows if row['expect'] == 'sound'
              and (row['kind'] not in (row['first_kinds'] or []) or row['state'] != 'verification_required')]
    assert missed == [], why(missed)
    # These answers carry the correct FINAL line, so the corpus grader alone would have passed them: only the
    # gate's claim check catches the bad claim in the prose.
    assert all(row['grader_passed'] for row in battery.rows if row['expect'] == 'sound'), why(battery.rows)


def test_the_durable_plan_rule_is_the_only_remaining_hold_on_a_never_completed_update_plan(battery):
    rows = [row for row in battery.rows if row['expect'] == 'plan_rule']
    assert rows and all(row['grader_passed'] for row in rows)
    for row in rows:
        assert row['first_kinds'] == [], why(row)  # the research check itself accepts the answer
        # The plan rule (controller.py, parent-owned) is the only hold; this scripted model never repairs its plan.
        assert all(issue.startswith(PLAN_LINE) for issue in row['completion_issues'] or ()), why(row)
        assert row['state'] == ('verification_required' if row['completion_issues'] else 'completed'), why(row)


def test_known_limits_are_recorded_with_their_reason(battery):
    rows = [row for row in battery.rows if row['expect'] == 'limit']
    assert rows and all(row['grader_passed'] for row in rows)
    # Worked examples with a hypothetical quantity and time-of-day arithmetic are not derivations the host can
    # verify from cited values; they stay held as unsupported or uncited precise claims (see the track note).
    for row in rows:
        assert set(row['first_kinds']) & {'uncited', 'unsupported'}, why(row)


# -- each fix on the corpus's own pages (research.coverage + completion_issues, no driver) -------------------------

NOW = datetime.datetime(2026, 10, 13, 9, tzinfo=datetime.timezone.utc).timestamp()
PACK = 'ellery-2026'
GAUGE_PAGE = GAUGE


class PackLedger:
    """The source ledger's read API over pack pages, each read through the real extractor; [S<n>] by position."""

    def __init__(self, *items):
        self.rows, self.texts = [], {}
        for n, (kind, url, query) in enumerate(items, 1):
            ident, entry = 'S%d' % n, next(e for e in er.WEB_PACKS[PACK]['entries'] if e['url'] == url)
            if kind == 'snippet':
                self.texts[ident] = entry['title'] + '\n' + entry['snippet']
                self.rows.append({'source_id': ident, 'cite': ident, 'kind': 'snippet', 'url': url, 'query': query,
                                  'title': entry['title'], 'snippet': entry['snippet'], 'retrieved_at': NOW - 600})
            else:
                self.texts[ident] = er.page_text(PACK, url)
                self.rows.append({'source_id': ident, 'cite': ident, 'kind': 'page', 'url': url, 'final_url': url,
                                  'retrieved_at': NOW - 300, 'characters': len(self.texts[ident]),
                                  'dates': {'claimed_published': entry['published'] + 'T00:00:00Z'}})

    def sources(self, run_id, *, tree=True):
        return [dict(row) for row in self.rows]

    def text(self, run_id, source_id):
        return self.texts[source_id]


# S1 notice, S2 schedule, S3 the berth's stale snippet, S4 berth, S5 statistics, S6 terminal, S7 history, S8 gauge
PAGES = PackLedger(('page', NOTICE, None), ('page', SCHEDULE, None), ('snippet', BERTH, 'berth 7 draught'),
                   ('page', BERTH, None), ('page', STATS, None), ('page', TERMINAL, None), ('page', HISTORY, None),
                   ('page', GAUGE_PAGE, None))


def gate(answer, *, ledger=PAGES, plan=None, used=None, now=NOW):
    report = research.coverage(ledger, 'run', answer, plan=plan, now=now, used=used)
    return report, research.completion_issues(report, budget=32768)


def held(report, found):
    """{claim text: support} of the precise claims the gate holds (derived values are not held)."""
    derived = {d['claim_id'] for d in report['derived']}
    return {c['text']: c['support'] for c in report['claims'] if c['kind'] and c['claim_id'] in set(found['unverified'])
            and c['claim_id'] not in derived}


def test_derived_values_are_accepted_only_from_values_the_host_verified():
    answer = ('The cargo rate rose from EUR 0.42 [S2] to EUR 0.47 per gross tonne [S1], an increase of EUR 0.05 '
              '(about 12%). The maximum draught is 12.8 m (about 42 ft) [S4]. Pilotage is compulsory above 120 m, '
              '90 m less than the 210 m maximum length [S4]. The breakwater was completed in 1911, 4 years after '
              'work began in 1907 [S7]. The winter timetable runs from 2026-11-02 to 2027-03-28, about 21 weeks '
              '[S6]. The port recorded 14,382 vessel arrivals and 6.1 million tonnes of cargo in 2025 [S5], about '
              '424 tonnes per arrival. The gauge last read 412 cm (4.12 m) [S8].')
    report, found = gate(answer)
    assert found['issues'] == [] and found['summary']['derived'] == 8
    text = {c['claim_id']: c['text'] for c in report['claims']}
    assert {text[d['claim_id']]: (d['basis'], d.get('operation')) for d in report['derived']} == {
        'EUR 0.05': ('computed', 'difference'), '12%': ('computed', 'percent'), '42 ft': ('rounded', None),
        '90 m': ('computed', 'difference'), '4 years': ('computed', 'span'), '21 weeks': ('computed', 'span'),
        '424 tonnes': ('computed', 'ratio'), '4.12 m': ('converted', None)}
    # The rows themselves still say what the page text says; the derivation is reported beside them.
    assert {c['support'] for c in report['claims'] if c['claim_id'] in {d['claim_id'] for d in report['derived']}} \
        == {'unsupported'}


@pytest.mark.parametrize('answer, kept', [
    ('The cargo rate rose from EUR 0.42 [S2] to EUR 0.47 [S1], an increase of EUR 0.06 (about 15%).',
     {'EUR 0.06': 'unsupported', '15%': 'unsupported'}),                      # wrong arithmetic
    ('The maximum draught is 12.8 m (about 45 ft) [S4].', {'45 ft': 'unsupported'}),   # a wrong conversion
    ('The maximum draught is 12.8 m and the berth is 42 ft wide [S4].', {'42 ft': 'unsupported'}),  # not beside it
    ('Cargo pays EUR 0.47 [S1] and paid EUR 0.42 [S2]. Fishing vessels pay EUR 0.05 per gross tonne [S2].',
     {'EUR 0.05': 'unsupported'}),          # equals a difference, but the sentence names no difference
    ('Search results list 11.5 m [S3]; the page says 12.8 m [S4], 1.3 m deeper.',
     {'11.5 m': 'unsupported', '1.3 m': 'unsupported'}),                     # a snippet value is never an operand
    ('That is an increase of EUR 0.05.', {'EUR 0.05': 'uncited'}),           # no verified values to derive from
    ('The berth was dredged to 14.5 m [S4].', {'14.5 m': 'unsupported'}),    # fabricated
    ('Search results list a 12.8 m draught [S3].', {'12.8 m': 'snippet_only'}),  # snippets never support, or derive
])
def test_fabricated_wrong_and_snippet_values_are_never_derived(answer, kept):
    report, found = gate(answer)
    assert found['issues'] and held(report, found) == kept


@pytest.mark.parametrize('answer, accepted', [
    ('The official count is roughly 14,400 vessel arrivals [S5].', True),
    ('Cargo was about 6 million tonnes [S5].', True),
    ('The berth takes vessels drawing up to roughly 13 m [S4].', True),
    ('The gauge last read about 4 m above chart datum [S8].', True),           # 412 cm, converted then rounded
    ('The port recorded 912,450 ferry passengers [S5], more than 900,000 for the first time.', True),
    ('The official count is 14,400 vessel arrivals [S5].', False),           # no approximation marker
    ('The official count is about 15,000 vessel arrivals [S5].', False),     # 14,382 does not round to it
    ('It handled about 2,000 cruise calls [S5].', False),                     # one significant digit, a bare count
    ('It handled about 2,030 cruise calls [S5].', False),                     # a page year is not a count
    ('It carried more than 900,000 ferry passengers [S5].', False),           # a bare-count bound needs a verified value
    ('The official count is roughly 14,400 vessel arrivals [S3].', False),    # a snippet citation stays snippet-only
])
def test_rounded_values_need_a_marker_and_a_close_value(answer, accepted):
    report, found = gate(answer)
    assert (found['issues'] == []) is accepted, (found['issues'], report['derived'])


def test_restated_questions_the_hosts_date_and_the_runs_own_query_are_not_claims():
    answer = ('**Question:** How many vessel arrivals did Port Ellery record in 2025?\n'
              'You asked about arrivals in 2025.\n'
              'Today is 2026-10-13.\n'
              'I searched for "berth 7 draught" first.\n'
              'Port Ellery recorded 14,382 vessel arrivals in 2025 [S5].')
    report, found = gate(answer)
    assert found['issues'] == [] and [c['text'] for c in report['claims'] if c['kind']] == ['14,382', '2025']
    # Only the host's own clock makes a date context; another date, or any quotation, still needs a citation.
    report, found = gate('Today is 2026-10-13.', now=None)
    assert held(report, found) == {'2026-10-13': 'uncited'}
    report, found = gate('On 2026-02-11 the figures were updated. The page says "pleasure craft are excluded" here.')
    assert held(report, found) == {'2026-02-11': 'uncited', '"pleasure craft are excluded"': 'uncited'}
    # A cited question is still checked like any sentence.
    report, found = gate('Did the port record 14,400 vessel arrivals in 2025 [S5]?')
    assert held(report, found) == {'14,400': 'unsupported'}


def test_a_source_line_cites_the_uncited_block_above_it_and_no_further():
    answer = ('Intro: the port handled 912,450 ferry passengers.\n\n'
              '- Vessel arrivals: 14,382\n- Cargo: 6.1 million tonnes\n- Draught limit: 12.8 m\n\n'
              'Source: Annual statistics 2025 [S5]')
    report, found = gate(answer)
    # The list is cited by the Source line (a carried citation never rescues a value its page lacks); the
    # paragraph above the list is another block and stays uncited.
    assert held(report, found) == {'912,450': 'uncited', '12.8 m': 'unsupported'}
    assert {c['text']: c['citations'] for c in report['claims'] if c['kind']}['14,382'] == ['S5']
    lone = gate('Cargo: 6.1 million tonnes\nArrivals: 14,382\n[S5]')
    assert lone[1]['issues'] == []
    # A line with its own citation ends the carried block.
    report, found = gate('- Arrivals: 14,382\n- Draught: 12.8 m [S4]\n- Cargo: 6.1 million tonnes\nSource: [S5]')
    assert held(report, found) == {'14,382': 'uncited'}


HARBOUR = ('Who is the current harbour master of Port Ellery?', "In what year was Port Ellery's north breakwater "
           'completed?')


def harbour_plan():
    value = research.plan(list(HARBOUR), today='2026-10-13', tier='large')
    return value, {q['id']: q['text'] for q in value['queries']}


def test_plan_questions_count_as_answered_by_page_claims_and_shared_sources_map_to_both():
    value, queries = harbour_plan()
    # The history page was first listed by q1's search, so the ledger's one snippet row names q1's query.
    ledger = PackLedger(('snippet', LEADERS, queries['q1.literal']), ('snippet', HISTORY, queries['q1.literal']),
                        ('page', LEADERS, None), ('page', HISTORY, None))
    answer = 'Captain Ines Varga is the harbour master [S3]. The north breakwater was completed in 1911 [S4].'
    report, found = gate(answer, ledger=ledger, plan=value)
    # Deliberate D02 change (Phase 20, track G20-research): the line also names the uncovered question's text, so
    # the lines the host appends at 4K or after the repair list it for the person.
    assert report['answered'] == ['q1'] and found['issues'] == [
        'Sub-question(s) q2 are not covered by page sources; say what could not be verified: '
        "q2 “In what year was Port Ellery's north breakwater completed?”."]
    # The parent's search receipts show q2's search listed the same snippet: the page answers q2 too.
    used = {'queries': [queries['q1.literal'], queries['q2.literal']],
            'shown': [[queries['q1.literal'], ['S1', 'S2']], [queries['q2.literal'], ['S2']]]}
    report, found = gate(answer, ledger=ledger, plan=value, used=used)
    assert report['answered'] == ['q1', 'q2'] and found['issues'] == []
    # Deliberate D02 change: the paraphrase cites the primary leadership page that q1's own search found, read in
    # full, so q1 is covered; q2 rests on a secondary page only (H19 asserted no covered state, "one domain each").
    assert {q['id']: q['state'] for q in report['status']['questions']} == {'q1': 'covered', 'q2': 'open'}
    # A snippet citation answers nothing, and a question the answer skips is still reported.
    report, found = gate('The north breakwater was completed in 1911 [S2].', ledger=ledger, plan=value, used=used)
    assert report['answered'] == [] and any('q1, q2 are not covered' in i for i in found['issues'])
    with pytest.raises(research.ResearchError, match='bad_usage'):
        gate(answer, ledger=ledger, plan=value, used={'shown': [['q', ['not-a-label']]]})


def test_a_claim_on_a_page_shared_by_a_current_sub_question_still_needs_its_date():
    value = research.plan(list(TASKS['research-dues']['subquestions']), today='2026-10-13', tier='large')
    queries = {q['id']: q['text'] for q in value['queries']}
    ledger = PackLedger(('snippet', SCHEDULE, queries['q1.literal']), ('snippet', NOTICE, queries['q1.literal']),
                        ('page', SCHEDULE, None), ('page', NOTICE, None))
    used = {'shown': [[queries['q1.literal'], ['S1', 'S2']], [queries['q2.literal'], ['S1', 'S2']]]}
    report, found = gate('The schedule sets a minimum charge of EUR 95.00 per call [S3].', ledger=ledger,
                         plan=value, used=used)
    assert any('rely on undated or older sources' in i for i in found['issues'])
    report, found = gate('The schedule of 15 January 2026 sets a minimum charge of EUR 95.00 per call [S3].',
                         ledger=ledger, plan=value, used=used)
    assert found['issues'] == []


def test_derivations_and_carried_citations_stay_bounded_on_a_dense_answer():
    import time
    lines = []
    for i in range(400):
        lines.append(f'- Item {i}: about {i * 7 + 3:,} km, roughly {i % 90 + 10}% up, {i}.5 m (about {i * 3} ft) '
                     f'more than {i + 2} t')
        if i % 3 == 2:
            lines.append('Source: [S4] [S5] [S8]')
    answer = '\n'.join(lines) + '\nThat is an increase of EUR 0.05 [S1][S2] and about 12% [S1].'
    started = time.perf_counter()
    report, found = gate(answer)
    elapsed = time.perf_counter() - started
    print(f'dense coverage: {len(report["claims"])} claims, {len(report["derived"])} derived in {elapsed:.2f}s')
    assert len(report['claims']) <= research.MAX_CLAIMS and len(report['derived']) <= research.MAX_DERIVED
    assert elapsed < 30 and found['issues']  # a catastrophic-cost guard, not a benchmark


def test_web_calls_name_every_query_that_listed_a_source():
    from pebble.research_store import web_calls
    receipts = [
        {'call_id': 'a', 'name': 'WebSearch', 'arguments': {'query': 'first'},
         'result': {'untrusted_search_results': [{'source_id': 'S1'}, {'source_id': 'S2'}]}},
        {'call_id': 'b', 'name': 'WebSearch', 'arguments': {'query': 'second'},
         'result': {'untrusted_search_results': [{'source_id': 'S2'}, {'source_id': None}, {'source_id': 'x9'}]}},
        {'call_id': 'c', 'name': 'WebSearch', 'arguments': {'query': 'failed'}, 'result': {'error': 'none'}},
    ]
    assert web_calls(receipts) == {'searches': 3, 'reads': 0, 'queries': ['first', 'second', 'failed'],
                                   'shown': [['first', ['S1', 'S2']], ['second', ['S2']]]}
    assert research.usage(PAGES.rows, web_calls(receipts))['searches'] == 4  # the three receipts and the ledger's own
