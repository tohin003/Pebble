"""MR-1: a seeded synthetic memory-recall evaluation with no model.

The corpus lives in a private in-memory SQLite store: twelve scenario
families, each with one target record, three query paraphrases
(morphological, abbreviation, reordered, numeric or synonym) and seven
labelled distractors (superseded, expired, forgotten, other project, disabled
layer, contested, near-miss attribute), plus 30, 300 or 1,500 seeded noise
records. The same corpus is recalled through the v1 (Phase 11) and v2
(hybrid) rankers at window-scaled budgets.

Per ranker, noise level and window it reports precision and recall at the
budget, MRR over the ranked candidates, harmful inclusions (v2 must have
none), conflict-note presence and p50/p95 latency, and it keeps every failure.
Synonym paraphrases share no spelling with their target and are expected to
miss lexically; they are reported, not hidden. Abbreviation queries that rely
on the ranker's own abbreviation table are counted separately, because that
gain is in-vocabulary by construction.

Contested records come from a synthetic oracle with W3's
`memory_graph.withheld(db, ids)` shape over the corpus labels. It exercises
the relation-filter plumbing, not contradiction detection. Nothing is
written except by the caller (the CLI writes one new report file).
"""

import hashlib
import json
import math
import random
import sqlite3
import threading
import time
from types import SimpleNamespace

from . import memory_retrieval
from .memory import Memory
from .memory_learning import Learning
from .store import Store

SCHEMA = 'pebble-mr1-report/1'
NOISE_LEVELS = (30, 300, 1500)
WINDOWS = (4096, 32768)
RANKERS = ('v1', 'v2')
PARAPHRASES = ('morphological', 'abbreviation', 'reordered', 'numeric', 'synonym')
DISTRACTORS = ('superseded', 'expired', 'forgotten', 'other_project', 'disabled_layer', 'contested', 'near_miss')
ALWAYS_HARMFUL = ('superseded', 'expired', 'forgotten', 'other_project', 'contested')
WORKSPACE = '/mr1/workspace'
OTHER_WORKSPACE = '/mr1/other-project'
CHAT = 'mr1-chat'
DAY = 86400.0
MAX_SEED = 2 ** 63 - 1
# Fixed clock values keep serialized items (and so budget packing) identical
# across runs. Ranking uses ANCHOR as `now`; recall's own expiry pass uses
# real time, which lies between EXPIRED_AT and ANCHOR.
ANCHOR = 4_000_000_000.0
EXPIRED_AT = 1_000_000_000.0

ENTITIES = ('Heron', 'Egret', 'Osprey', 'Kestrel', 'Plover', 'Avocet', 'Bittern', 'Curlew', 'Dunlin', 'Gannet',
            'Merlin', 'Puffin', 'Shrike', 'Siskin', 'Tanager', 'Vireo', 'Warbler', 'Pipit', 'Lapwing', 'Godwit')
NOISE_ENTITIES = ('Andromeda', 'Aquila', 'Auriga', 'Bootes', 'Caelum', 'Carina', 'Cetus', 'Columba', 'Corvus',
                  'Crater', 'Cygnus', 'Delphinus', 'Dorado', 'Draco', 'Fornax', 'Grus', 'Hydra', 'Indus', 'Lepus',
                  'Libra', 'Lupus', 'Lyra', 'Mensa', 'Musca', 'Norma', 'Octans', 'Pavo', 'Perseus', 'Phoenix',
                  'Pictor', 'Puppis', 'Pyxis', 'Sagitta', 'Scutum', 'Sextans', 'Taurus', 'Tucana', 'Vela',
                  'Volans', 'Vulpecula')
DATABASES = ('PostgreSQL', 'MySQL', 'Memcached', 'MongoDB', 'Redis', 'CouchDB', 'SQLite', 'Cassandra', 'MariaDB')
DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
WORDS = {2: 'two', 3: 'three', 4: 'four', 5: 'five', 6: 'six', 7: 'seven', 8: 'eight', 9: 'nine'}
VALUE_KEYS = ('V', 'O', 'X', 'Y', 'Z', 'Q', 'C')


def _ints(low, high, step=1):
    return lambda rng: dict(zip(VALUE_KEYS + ('W',), rng.sample(range(low, high + 1, step), 8)))


def _databases(rng):
    return dict(zip(VALUE_KEYS, rng.sample(DATABASES, 7)))


def _schedule(rng):
    days = rng.sample(DAYS, 7)
    values = {key: f'{day} at {rng.randint(9, 17)}:00' for key, day in zip(VALUE_KEYS, days)}
    values['W'] = rng.choice(DAYS) + f' at {rng.randint(9, 17)}:00'
    return values


def _rate(rng):
    values = _ints(1100, 9900, 100)(rng)
    values['Vc'] = f"{values['V']:,}"
    return values


def _timeout(rng):
    values = _ints(20, 95, 5)(rng)
    values['W'] = rng.randint(2, 64)
    return values


def _retries(rng):
    values = dict(zip(('V', 'O', 'X', 'Y', 'Z', 'Q'), rng.sample(range(2, 10), 6)))
    values['Vw'] = WORDS[values['V']]
    return values


def _versions(rng):
    pairs = rng.sample([(major, minor) for major in range(16, 23) for minor in range(1, 16)], 7)
    values = {key: f'{a}.{b}' for key, (a, b) in zip(VALUE_KEYS, pairs)}
    values['W'] = f'5.{rng.randint(1, 90)}'
    return values


def _none(rng):
    return {}


# Each family: target layer and scope, value generator, templates for the
# target and its seven labelled distractors, the layer disabled while its
# queries run, and three paraphrased queries.
FAMILIES = (
    {'id': 'staging-port', 'kind': 'semantic', 'scope': 'project', 'values': _ints(7000, 7999),
     'target': '{E} staging server listens on port {V}.',
     'near_miss': '{E} production server listens on port {W}.',
     'superseded': '{E} staging server listened on port {O} before the move.',
     'expired': '{E} staging server temporarily listens on port {X} this week.',
     'forgotten': '{E} staging server should listen on port {Y}.',
     'other_project': '{E} staging server listens on port {Z}.',
     'disabled_layer': ('episodic', 'Earlier run: the {E} staging server answered on port {Q}.'),
     'contested': '{E} staging server listens on port {C}, says the old runbook.',
     'queries': (('reordered', 'Port of the {E} server used for staging?'),
                 ('morphological', '{E} staging servers listening port'),
                 ('synonym', 'Which socket does the {E} pre-production host accept connections on?'))},
    {'id': 'session-store', 'kind': 'semantic', 'scope': 'project', 'values': _databases,
     'target': '{E} keeps user sessions in a {V} database.',
     'near_miss': '{E} keeps uploaded files in object storage buckets.',
     'superseded': '{E} kept user sessions in a {O} database before the upgrade.',
     'expired': '{E} keeps user sessions in a {X} database during the trial.',
     'forgotten': '{E} keeps user sessions in a {Y} database.',
     'other_project': '{E} keeps user sessions in a {Z} database.',
     'disabled_layer': ('procedural', 'To inspect {E} user sessions, query the {Q} session table.'),
     'contested': '{E} keeps user sessions in a {C} database, according to a stale note.',
     'queries': (('abbreviation', '{E} user session db'),
                 ('morphological', 'Where is {E} storing its user session data?'),
                 ('synonym', 'Which datastore holds the {E} login state?'))},
    {'id': 'deploy-steps', 'kind': 'procedural', 'scope': 'project', 'values': _none,
     'target': 'To deploy {E}, run the release checklist, then tag the build.',
     'near_miss': 'To test {E}, run the unit suite with coverage enabled.',
     'superseded': 'To deploy {E}, copy the files to the server by hand.',
     'expired': 'To deploy {E} this week, file a freeze exception first.',
     'forgotten': 'To deploy {E}, ask the on-call engineer to push it.',
     'other_project': 'To deploy {E}, run the container pipeline and wait.',
     'disabled_layer': ('episodic', 'Last time {E} was deployed by rerunning the failed job.'),
     'contested': 'To deploy {E}, skip the checklist and push directly.',
     'queries': (('morphological', 'How are {E} deployments done?'),
                 ('reordered', 'Release checklist and build tag: steps to deploy {E}'),
                 ('synonym', 'How do I ship {E} to users?'))},
    {'id': 'indentation', 'kind': 'profile', 'scope': 'global', 'slot': 'code-indentation', 'values': _none,
     'target': 'I prefer tabs for indentation in Python files.',
     'near_miss': 'I prefer double quotes for strings in Python files.',
     'superseded': 'I prefer two spaces for indentation in Python files.',
     'expired': 'For this sprint I prefer four spaces for indentation in Python files.',
     'forgotten': 'I prefer eight spaces for indentation in Python files.',
     'other_project': 'I prefer three spaces for indentation in Python files here.',
     'disabled_layer': ('semantic', 'The style guide says Python indentation uses spaces.'),
     'contested': 'I prefer spaces for indentation in Python files.',
     'queries': (('morphological', 'How should I indent Python code?'),
                 ('abbreviation', 'py indentation preference'),
                 ('synonym', 'What whitespace style do I like for code blocks?'))},
    {'id': 'parser-failure', 'kind': 'episodic', 'scope': 'project', 'values': _none,
     'target': 'Outcome: the {E} parser tests failed on empty input files.',
     'near_miss': 'Outcome: the {E} renderer tests passed after the font fix.',
     'superseded': 'Outcome: the {E} parser tests failed on unicode input files.',
     'expired': 'Outcome today: the {E} parser tests are flaky on empty input files.',
     'forgotten': 'Outcome: the {E} parser tests crashed on large input files.',
     'other_project': 'Outcome: the {E} parser tests failed on binary input files.',
     'disabled_layer': ('semantic', 'The {E} parser rejects empty input files with an error.'),
     'contested': 'Outcome: the {E} parser tests never failed on empty input files.',
     'queries': (('morphological', 'Why did the {E} parsing test fail?'),
                 ('reordered', 'Empty input files and the {E} parser tests: what failed?'),
                 ('synonym', "What broke in the {E} syntax analyzer checks?"))},
    {'id': 'demo-schedule', 'kind': 'temporal', 'scope': 'chat', 'values': _schedule,
     'target': 'The {E} demo is scheduled for {V}.',
     'near_miss': 'The {E} retrospective is scheduled for {W}.',
     'superseded': 'The {E} demo was scheduled for {O}.',
     'expired': 'The {E} demo is scheduled for {X}.',
     'forgotten': 'The {E} demo moves to {Y}.',
     'other_project': 'The {E} demo is scheduled for {Z} in the other project.',
     'disabled_layer': ('episodic', 'We rehearsed the {E} demo on {Q}.'),
     'contested': 'The {E} demo is scheduled for {C}, per another invite.',
     'queries': (('reordered', 'Scheduled for when: the {E} demo?'),
                 ('abbreviation', 'When is the {E} demonstration scheduled?'),
                 ('numeric', 'Is the {E} demo scheduled at 2pm?'))},
    {'id': 'rate-limit', 'kind': 'semantic', 'scope': 'project', 'values': _rate,
     'target': '{E} API allows {V} requests per minute.',
     'near_miss': '{E} API allows {W} concurrent connections.',
     'superseded': '{E} API allowed {O} requests per minute before the upgrade.',
     'expired': '{E} API allows {X} requests per minute during the promotion.',
     'forgotten': '{E} API allows {Y} requests per minute for partners.',
     'other_project': '{E} API allows {Z} requests per minute.',
     'disabled_layer': ('episodic', 'Load test: the {E} API handled {Q} requests per minute.'),
     'contested': '{E} API allows {C} requests per minute per the vendor page.',
     'queries': (('numeric', 'Does {E} allow {Vc} requests/min?'),
                 ('abbreviation', '{E} API req/min limit'),
                 ('reordered', 'Per minute, how many requests does the {E} API allow?'))},
    {'id': 'build-timeout', 'kind': 'semantic', 'scope': 'project', 'values': _timeout,
     'target': '{E} build timeout is {V} minutes.',
     'near_miss': '{E} build cache size is {W} gigabytes.',
     'superseded': '{E} build timeout was {O} minutes last quarter.',
     'expired': '{E} build timeout is {X} minutes during the freeze.',
     'forgotten': '{E} build timeout is {Y} minutes on weekends.',
     'other_project': '{E} build timeout is {Z} minutes.',
     'disabled_layer': ('procedural', 'Raise the {E} build timeout to {Q} minutes when builds stall.'),
     'contested': '{E} build timeout is {C} minutes per the CI dashboard.',
     'queries': (('numeric', 'Is the {E} build timeout {V}min?'),
                 ('morphological', 'How long before {E} builds time out?'),
                 ('synonym', "What is the {E} compile deadline?"))},
    {'id': 'upload-retries', 'kind': 'semantic', 'scope': 'project', 'values': _retries,
     'target': '{E} uploader retries failed uploads {V} times with backoff.',
     'near_miss': '{E} uploader compresses images before sending them.',
     'superseded': '{E} uploader retried failed uploads {O} times without backoff.',
     'expired': '{E} uploader retries failed uploads {X} times during the outage.',
     'forgotten': '{E} uploader retries failed uploads {Y} times on mobile.',
     'other_project': '{E} uploader retries failed uploads {Z} times.',
     'disabled_layer': ('episodic', 'Last week the {E} uploader retried {Q} times before giving up.'),
     'contested': '{E} uploader never retries failed uploads.',
     'queries': (('numeric', 'Does the {E} uploader retry {Vw} times?'),
                 ('morphological', '{E} upload retry policy'),
                 ('reordered', 'With backoff, how many times are failed {E} uploads retried?'))},
    {'id': 'node-version', 'kind': 'semantic', 'scope': 'project', 'values': _versions,
     'target': '{E} dashboard builds with Node version {V}.',
     'near_miss': '{E} dashboard bundles with webpack version {W}.',
     'superseded': '{E} dashboard built with Node version {O} last year.',
     'expired': '{E} dashboard builds with Node version {X} during the migration.',
     'forgotten': '{E} dashboard builds with Node version {Y} on Windows.',
     'other_project': '{E} dashboard builds with Node version {Z}.',
     'disabled_layer': ('procedural', 'Install Node version {Q} before building the {E} dashboard.'),
     'contested': '{E} dashboard builds with Node version {C} per the old README.',
     'queries': (('numeric', 'Is the {E} dashboard on node v{V}?'),
                 ('abbreviation', '{E} dashboard node ver'),
                 ('synonym', "Which JavaScript runtime release does the {E} admin panel use?"))},
    {'id': 'doc-spelling', 'kind': 'profile', 'scope': 'global', 'slot': 'doc-spelling', 'values': _none,
     'target': 'I prefer British English spelling in documentation.',
     'near_miss': 'I prefer short paragraphs in documentation.',
     'superseded': 'I prefer American English spelling in documentation.',
     'expired': 'This week I prefer Canadian English spelling in documentation.',
     'forgotten': 'I prefer Australian English spelling in documentation.',
     'other_project': 'I prefer American spelling in documentation for this project.',
     'disabled_layer': ('semantic', 'The documentation style guide mandates Oxford spelling.'),
     'contested': 'I prefer American English spelling in docs.',
     'queries': (('abbreviation', 'docs spelling preference'),
                 ('morphological', 'How should documents be spelled?'),
                 ('synonym', 'Which orthography do I want in manuals?'))},
    {'id': 'log-location', 'kind': 'semantic', 'scope': 'project', 'values': _none,
     'target': '{E} writes application logs to /var/log/{e}/app.log.',
     'near_miss': '{E} writes crash dumps to /var/crash/{e}.',
     'superseded': '{E} wrote application logs to /tmp/{e}.log before packaging.',
     'expired': '{E} writes application logs to /mnt/scratch/{e}.log during debugging.',
     'forgotten': '{E} writes application logs to /opt/{e}/logs/app.log.',
     'other_project': '{E} writes application logs to /srv/{e}/app.log.',
     'disabled_layer': ('episodic', 'Yesterday the {E} logs were found in /home/{e}/debug.log.'),
     'contested': '{E} writes application logs to syslog only.',
     'queries': (('abbreviation', '{E} app log dir'),
                 ('reordered', 'Application logs of {E}: where are they written?'),
                 ('morphological', 'Where does {E} log application output?'))},
)

NOISE = (
    ('semantic', '{N} staging server listens on port {p}.'),
    ('semantic', '{N} keeps user sessions in a {db} database.'),
    ('procedural', 'To deploy {N}, run the {tool} pipeline, then tag the build.'),
    ('semantic', '{N} build timeout is {m} minutes.'),
    ('semantic', '{N} API allows {r} requests per minute.'),
    ('semantic', '{N} uploader retries failed uploads {k} times with backoff.'),
    ('semantic', '{N} dashboard builds with Node version {nv}.'),
    ('semantic', '{N} writes application logs to /var/log/{n}/app.log.'),
    ('temporal', 'The {N} demo is scheduled for {day} at {h}:00.'),
    ('episodic', 'Outcome: the {N} parser tests failed on {thing} input files.'),
    ('profile', 'For {N} work I prefer {style} in {topic}.'),
    ('semantic', '{N} team lunch order: {food}.'),
    ('episodic', 'Outcome: the {N} release was rolled back after {thing} errors.'),
    ('semantic', '{N} on-call rotation changes every {k} weeks.'),
)
NOISE_FILL = {'tool': ('release', 'container', 'canary', 'blue-green'), 'thing': ('truncated', 'malformed', 'oversized', 'binary'),
              'style': ('short sentences', 'bullet lists', 'tables', 'diagrams'),
              'topic': ('reviews', 'reports', 'summaries', 'tickets'),
              'food': ('two vegetarian wraps', 'noodle soup', 'falafel plates', 'rice bowls')}
BASELINE = ('I prefer concise answers.', 'Keep explanations in plain English.')


class _MemoryStore(Store):
    """Store semantics over a private in-memory SQLite database."""

    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript("""
          CREATE TABLE chats(id TEXT PRIMARY KEY,title TEXT,workspace TEXT,updated REAL);
          CREATE TABLE messages(id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,content TEXT,created REAL);
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE actions(id TEXT PRIMARY KEY,chat_id TEXT,kind TEXT,status TEXT,payload TEXT,created REAL);
        """)
        self.db.commit()


def normal(text):
    return ' '.join(str(text).casefold().split())


class Corpus:
    """One seeded corpus at one noise level; records carry fixed IDs."""

    def __init__(self, seed, noise, anchor):
        self.rng = random.Random(f'mr1:{seed}:{noise}')
        self.anchor, self.noise = anchor, noise
        self.store = _MemoryStore()
        # Curated procedures are inserted below with fixed IDs instead.
        self.store.set_setting('memory_initialized', True)
        self.memory = Memory(self.store)
        self.learning = Learning(SimpleNamespace(store=self.store, memory=self.memory))
        self.labels, self.queries, self.contested = {}, [], set()
        self.harmful_content = {}
        entities = self.rng.sample(ENTITIES, len(FAMILIES))
        self._insert('mr1-curated-engineering', 'procedural', 'Engineering workflow: inspect relevant source; make a minimal '
                     'change; verify with relevant tests.', 'global', 'curated', 400)
        self._insert('mr1-curated-current', 'procedural', 'Current-information workflow: retrieve a current source for '
                     'weather, prices and news.', 'global', 'curated', 400)
        for family, entity in zip(FAMILIES, entities):
            self._family(family, entity)
        self._noise(noise)
        self.store.db.commit()
        self.records = self.store.query('SELECT count(*) AS n FROM memories')[0]['n']

    def close(self):
        self.store.close()

    def _insert(self, ident, kind, content, scope, origin, age_days, expires=None):
        updated = self.anchor - age_days * DAY
        chat = CHAT if scope == 'chat:' + CHAT else None
        self.store.db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?)',
                              (ident, kind, content, scope, chat, origin, updated, updated, expires))

    def _evidence(self, ident, kind, scope, slot, status, proof, superseded_by=None):
        self.store.db.execute(
            'INSERT INTO memory_evidence(memory_id,candidate_id,run_id,chat_id,scope,kind,slot,fingerprint,status,'
            'proof,superseded_by,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident, 'mr1-candidate-' + ident, 'mr1-run', CHAT, scope, kind, slot,
             hashlib.sha256(ident.encode()).hexdigest(), status, json.dumps(proof), superseded_by, self.anchor))

    def _family(self, family, entity):
        values = family['values'](self.rng)
        fill = {'E': entity, 'e': entity.lower(), **values}
        kind = family['kind']
        scope = {'project': WORKSPACE, 'global': 'global', 'chat': 'chat:' + CHAT}[family['scope']]
        origin = {'profile': 'auto-learned', 'episodic': 'conversation'}.get(kind, 'user')
        prefix = 'mr1-' + family['id'] + '-'
        target = prefix + 'target'
        age = self.rng.uniform(1, 30)
        expires = self.anchor + 5 * DAY if kind == 'temporal' else None
        texts = {'target': family['target'].format(**fill)}
        self._insert(target, kind, texts['target'], scope, origin, age, expires)
        slot = family.get('slot') or family['id']
        proof = {'version': 1, 'authorization': 'none', 'evidence_class': 'human_preference'} if kind == 'profile' else {
            'version': 1, 'authorization': 'none'}
        if kind == 'profile':
            self._evidence(target, kind, scope, slot, 'active', proof)
        layer, disabled_text = family['disabled_layer']
        for label in DISTRACTORS:
            template = disabled_text if label == 'disabled_layer' else family[label]
            text = template.format(**fill)
            texts[label] = text
            ident = prefix + label
            distractor_kind = {'disabled_layer': layer, 'expired': 'temporal'}.get(label, kind)
            distractor_scope = OTHER_WORKSPACE if label == 'other_project' else scope
            distractor_origin = origin if distractor_kind == kind else 'user'
            distractor_expires = (self.anchor + 5 * DAY if distractor_kind == 'temporal' else None)
            if label == 'expired':
                # Validity already ended in real time, so recall's own expiry
                # pass must remove it before ranking.
                distractor_expires = EXPIRED_AT
            distractor_age = age + self.rng.uniform(20, 90) if label == 'superseded' else self.rng.uniform(0, 60)
            self._insert(ident, distractor_kind, text, distractor_scope, distractor_origin, distractor_age, distractor_expires)
            if label == 'superseded':
                self._evidence(ident, kind, scope, slot, 'superseded', proof, superseded_by=target)
            elif kind == 'profile' and distractor_kind == 'profile':
                self._evidence(ident, kind, distractor_scope, slot + ':' + label, 'active', proof)
            self.labels[ident] = (family['id'], label, distractor_kind)
            if label == 'contested':
                self.contested.add(ident)
            if label in ALWAYS_HARMFUL:
                self.harmful_content[normal(text)] = label
        if len(set(map(normal, texts.values()))) != len(texts):
            raise AssertionError('MR-1 family texts must differ from their target: ' + family['id'])
        self.store.db.commit()
        self.learning.forget(prefix + 'forgotten')
        for paraphrase, template in family['queries']:
            self.queries.append({'id': f"{family['id']}:{paraphrase}", 'family': family['id'], 'paraphrase': paraphrase,
                                 'text': template.format(**fill), 'target': target, 'disabled': layer})

    def _noise(self, count):
        rng = self.rng
        for index in range(count):
            ident = f'mr1-noise-{index:04d}'
            if index < len(BASELINE):
                self._insert(ident, 'profile', BASELINE[index], 'global', 'user', rng.uniform(30, 300))
                continue
            kind, template = NOISE[rng.randrange(len(NOISE))]
            name = rng.choice(NOISE_ENTITIES)
            fill = {'N': name, 'n': name.lower(), 'p': rng.randint(7000, 7999), 'db': rng.choice(DATABASES),
                    'm': rng.randrange(20, 96, 5), 'r': rng.randrange(1100, 9901, 100), 'k': rng.randint(2, 9),
                    'nv': f'{rng.randint(16, 22)}.{rng.randint(1, 15)}', 'day': rng.choice(DAYS), 'h': rng.randint(9, 17),
                    **{key: rng.choice(options) for key, options in NOISE_FILL.items()}}
            draw = rng.random()
            scope = (WORKSPACE if draw < 0.5 else 'global' if draw < 0.7 else 'chat:' + CHAT if draw < 0.8
                     else OTHER_WORKSPACE)
            origin = {'profile': 'auto-learned', 'episodic': 'conversation'}.get(kind, 'user')
            expires = self.anchor + rng.uniform(1, 30) * DAY if kind == 'temporal' else None
            self._insert(ident, kind, template.format(**fill), scope, origin, rng.uniform(0, 180), expires)

    def oracle(self):
        """W3's memory_graph.withheld interface over the contested labels."""
        contested = frozenset(self.contested)
        return lambda ids: {ident: 'contested' for ident in ids if ident in contested}

    def harmful(self, query, item):
        """Label of a harmful recalled item for this query, else None."""
        label = self.labels.get(item.get('id'))
        if label and (label[1] in ALWAYS_HARMFUL or label[1] == 'disabled_layer' and label[2] == query['disabled']):
            return label[1]
        return self.harmful_content.get(normal(item.get('content', '')))


def percentile(values, share):
    """Nearest-rank percentile of a nonempty list."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(share * len(ordered)) - 1)]


def _score(corpus, query, result):
    items = [i for i in result['items'] if i.get('kind') not in ('working', 'recall_note')]
    selected = [i.get('id') for i in items]
    records = result['diagnostics'].get('records', [])
    eligible = [r['id'] for r in records if r.get('decision') not in ('withheld', 'permission', 'source_not_current')]
    decision = next((r.get('decision') for r in records if r['id'] == query['target']), 'not_candidate')
    rank = eligible.index(query['target']) + 1 if query['target'] in eligible else None
    harmful = [(i.get('id'), label) for i in items if (label := corpus.harmful(query, i))]
    contested = [r for r in records if r['id'] in corpus.contested
                 and (r.get('decision') in ('selected', 'omitted_for_budget') or r.get('match'))]
    note = any(i.get('note') == 'conflict' for i in result['items'])
    baseline = sum(1 for r in records if r.get('decision') == 'selected' and r.get('signals', {}).get('baseline'))
    return {'hit': query['target'] in selected, 'selected': len(selected), 'baseline': baseline, 'rank': rank,
            'decision': decision, 'harmful': harmful, 'conflict_expected': bool(contested), 'conflict_note': note,
            'used': result['diagnostics'].get('used', 0)}


def _aggregate(scores, latencies):
    count = len(scores)
    selected = sum(s['selected'] for s in scores)
    ranked = selected - sum(s['baseline'] for s in scores)
    hits = sum(s['hit'] for s in scores)
    expected = [s for s in scores if s['conflict_expected']]
    labels = {}
    for score in scores:
        for _, label in score['harmful']:
            labels[label] = labels.get(label, 0) + 1
    return {'queries': count, 'recall': round(hits / count, 4) if count else None,
            'precision': round(hits / selected, 4) if selected else None,
            'precision_excluding_baseline': round(hits / ranked, 4) if ranked else None,
            'mrr': round(sum(1 / s['rank'] for s in scores if s['rank']) / count, 4) if count else None,
            'harmful_inclusions': sum(len(s['harmful']) for s in scores), 'harmful_by_label': dict(sorted(labels.items())),
            'conflict_note': {'expected': len(expected), 'present': sum(s['conflict_note'] for s in expected),
                              'unexpected': sum(s['conflict_note'] for s in scores if not s['conflict_expected'])},
            'selected_items_mean': round(selected / count, 3) if count else None,
            'used_chars_mean': round(sum(s['used'] for s in scores) / count, 1) if count else None,
            'latency_ms': {'p50': round(percentile(latencies, 0.5), 3), 'p95': round(percentile(latencies, 0.95), 3),
                           'max': round(max(latencies), 3)} if latencies else None}


def _strip_timing(value):
    if isinstance(value, dict):
        return {k: _strip_timing(v) for k, v in value.items() if k != 'latency_ms'}
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


def digest(report):
    """SHA-256 of the canonical report without latency, for comparing runs."""
    body = _strip_timing({k: v for k, v in report.items() if k != 'deterministic_sha256'})
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def evaluate(seed=15, noise_levels=NOISE_LEVELS, windows=WINDOWS, rankers=RANKERS, trigram='auto',
             clock=time.perf_counter):
    """Run MR-1 and return the JSON-ready report. Deterministic for a seed
    except latency, which comes from `clock` (inject one for exact reruns)."""
    if type(seed) is not int or not 0 <= seed <= MAX_SEED:
        raise ValueError('Use a nonnegative integer seed.')
    if not noise_levels or any(type(n) is not int or not 0 <= n <= 5000 for n in noise_levels):
        raise ValueError('Use noise levels from 0 to 5,000 records.')
    if not windows or any(type(w) is not int or not 1 <= w <= memory_retrieval.MAX_WINDOW for w in windows):
        raise ValueError('Use model windows from 1 to 10,000,000 tokens.')
    if not rankers or any(r not in RANKERS for r in rankers):
        raise ValueError('Use rankers v1 and/or v2.')
    if trigram not in ('auto', 'python', 'fts5'):
        raise ValueError('Use trigram mode auto, python or fts5.')
    anchor = ANCHOR
    results, failures, comparison, modes = [], [], [], set()
    corpus_info = {}
    table_queries = 0
    for noise in noise_levels:
        corpus = Corpus(seed, noise, anchor)
        try:
            corpus.memory.trigram_fts = trigram != 'python'
            mode = corpus.memory.trigram_mode()
            if trigram == 'fts5' and mode != 'fts5':
                raise ValueError('This SQLite build has no FTS5 trigram tokenizer.')
            modes.add(mode)
            corpus_info[str(noise)] = {'records_before_expiry': corpus.records, 'noise': noise}
            table_queries = sum(1 for q in corpus.queries if q['paraphrase'] == 'abbreviation'
                                and memory_retrieval.HybridQuery(q['text']).expansions)
            rows = {}
            for ranker in rankers:
                for window in windows:
                    scores, latencies = [], []
                    by_type = {}
                    for query in corpus.queries:
                        corpus.store.set_setting('memory_layers', {query['disabled']: False})
                        started = clock()
                        result = corpus.memory.recall(query['text'], CHAT, WORKSPACE, window=window, ranker=ranker,
                                                      graph=corpus.oracle(), now=anchor)
                        latencies.append((clock() - started) * 1000)
                        score = _score(corpus, query, result)
                        scores.append(score)
                        by_type.setdefault(query['paraphrase'], []).append(score)
                        base = {'ranker': ranker, 'noise': noise, 'window': window, 'query': query['id'],
                                'family': query['family'], 'paraphrase': query['paraphrase']}
                        if not score['hit']:
                            failures.append({**base, 'type': 'missed_target', 'target_rank': score['rank'],
                                             'target_decision': score['decision'],
                                             'expected_lexical_miss': query['paraphrase'] == 'synonym'})
                        for ident, label in score['harmful']:
                            failures.append({**base, 'type': 'harmful_inclusion', 'record': ident, 'label': label})
                        if score['conflict_expected'] and not score['conflict_note']:
                            failures.append({**base, 'type': 'missing_conflict_note'})
                    row = {'ranker': ranker, 'noise': noise, 'window': window,
                           'budget': memory_retrieval.recall_budget(window), **_aggregate(scores, latencies),
                           'by_paraphrase': {kind: {'queries': len(group),
                                                    'recall': round(sum(s['hit'] for s in group) / len(group), 4),
                                                    'mrr': round(sum(1 / s['rank'] for s in group if s['rank']) / len(group), 4)}
                                             for kind, group in sorted(by_type.items())}}
                    rows[ranker, window] = row
                    results.append(row)
            if set(rankers) == set(RANKERS):
                for window in windows:
                    old, new = rows['v1', window], rows['v2', window]
                    comparison.append({'noise': noise, 'window': window, **{
                        metric: {'v1': old[metric], 'v2': new[metric],
                                 'delta': None if old[metric] is None or new[metric] is None
                                 else round(new[metric] - old[metric], 4)}
                        for metric in ('recall', 'precision', 'mrr', 'harmful_inclusions')}})
        finally:
            corpus.close()
    v2 = [r for r in results if r['ranker'] == 'v2']
    report = {
        'schema': SCHEMA, 'evaluation': 'MR-1 synthetic memory recall', 'seed': seed,
        'noise_levels': list(noise_levels), 'windows': list(windows), 'rankers': list(rankers),
        'corpus': {'families': len(FAMILIES), 'queries': len(FAMILIES) * 3,
                   'paraphrases': {p: sum(1 for f in FAMILIES for kind, _ in f['queries'] if kind == p) for p in PARAPHRASES},
                   'distractors_per_family': list(DISTRACTORS), 'always_harmful': list(ALWAYS_HARMFUL),
                   'abbreviation_queries_using_ranker_table': table_queries, 'levels': corpus_info},
        'environment': {'model': 'none', 'network': 'none', 'embedder': 'none (default no-op)',
                        'trigram_modes': sorted(modes), 'sqlite': sqlite3.sqlite_version,
                        'graph': 'synthetic contested-label oracle (memory_graph.withheld interface)'},
        'results': results, 'comparison': comparison, 'failures': failures,
        'gates': {'v2_harmful_inclusions_zero': bool(v2) and all(r['harmful_inclusions'] == 0 for r in v2),
                  'v2_conflict_note_when_contested_matches': bool(v2) and all(
                      r['conflict_note']['present'] == r['conflict_note']['expected'] for r in v2)},
        'limits': [
            'Synthetic English corpus with fixed templates; not a measure of real conversations.',
            'Synonym paraphrases are expected to miss lexically; no embedding backend is installed. Most keep the '
            'entity name, so a hit there comes from the entity, not from understanding the synonym.',
            'Abbreviation queries that use the ranker abbreviation table measure in-vocabulary coverage.',
            'Contested labels come from an oracle with the W3 interface, not from contradiction detection.',
            'Latency is wall-clock on this machine with an in-memory database; it is excluded from the digest.',
            'Precision counts every selected memory item, including general preferences recalled as baseline; '
            'precision_excluding_baseline leaves those out. Noise reuses the target templates with other entities.',
        ],
    }
    report['deterministic_sha256'] = digest(report)
    return report
