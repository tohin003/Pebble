"""Corpus 24a.1: the Pebble 1.0 claim corpus (docs/PEBBLE_V1_INSPECTION.md §7.1, from finding i7-agent-10).

60 tasks in 12 families, each with a canonical prompt and two paraphrases (terse, conversational); 15 tasks have
fixed follow-up turns. Graders read a product-neutral Output (graders.Output). Corpus 18a.3, its graders and its
records are unchanged; ``format_only`` exists only here.

Fingerprints: ``task_fingerprint`` covers every task field (prompts, paraphrases, follow-ups, fixtures, grants), the
task's oracle and grader name; ``corpus_fingerprint`` adds the graders' and oracles' source digests, so a changed
prompt, paraphrase, fixture, oracle or grader yields a new corpus fingerprint, and a registered plan refuses it.
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .oracles import ORACLES
from .tasks import BY_ID, FAMILIES, PHRASINGS, PRODUCTS, TASKS

CORPUS_VERSION = '24a.1'
HERE = Path(__file__).resolve().parent


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else str(value).encode()).hexdigest()


def source_digest(name):
    return sha((HERE / name).read_bytes())


def task_fingerprint(task):
    data = asdict(task)
    data['corpus'] = CORPUS_VERSION
    data['oracle_sha256'] = sha(canonical(ORACLES[task.id]))
    return sha(canonical(data))


def digests():
    """The parts a plan digest names separately: prompts, paraphrases, follow-ups, fixtures, oracles and graders."""
    return {
        'prompts_sha256': sha(canonical([[t.id, t.prompts[0]] for t in TASKS])),
        'paraphrases_sha256': sha(canonical([[t.id, list(t.prompts[1:])] for t in TASKS])),
        'turns_sha256': sha(canonical([[t.id, [asdict(turn) for turn in t.turns]] for t in TASKS])),
        'fixtures_sha256': sha(canonical([[t.id, [list(f) for f in t.files]] for t in TASKS])),
        'oracles_sha256': sha(canonical(ORACLES)),
        'graders_source_sha256': source_digest('graders.py'),
        'oracles_source_sha256': source_digest('oracles.py'),
    }


def corpus_fingerprint():
    return sha(canonical({'version': CORPUS_VERSION, 'tasks': [[t.id, task_fingerprint(t)] for t in TASKS],
                          **digests()}))


def manifest():
    """Public and content-free: ids, families, products, traps and fingerprints; never prompts or oracles."""
    return {'version': CORPUS_VERSION, 'fingerprint': corpus_fingerprint(), 'families': list(FAMILIES),
            'phrasings': list(PHRASINGS), 'tasks': [
                {'id': t.id, 'version': t.version, 'family': t.family, 'products': t.products, 'trap': t.trap,
                 'turns': 1 + len(t.turns), 'multi_turn': t.multi_turn, 'blocked': t.blocked, 'hostile': t.hostile,
                 'grader': t.grader, 'fingerprint': task_fingerprint(t)} for t in TASKS]}


def prompt_for(task, phrasing):
    return task.prompts[PHRASINGS.index(phrasing)]


def oracle_markers(task_id):
    """Answer strings that must never appear in the task's prompt, paraphrases or follow-ups (the tests check). Research
    and memory answers do appear in their sources, by design; computed answers appear nowhere (also checked)."""
    if BY_ID[task_id].family == 'memory':
        return []  # the person states the remembered values in the conversation itself
    oracle = ORACLES[task_id]
    values = [oracle.get('value')] + [t.get('value') for t in oracle.get('turns', ()) if isinstance(t, dict)]
    return ([v for v in values if isinstance(v, str) and len(v) >= 3 and any(c.isdigit() for c in v)]
            + list(oracle.get('items', ())))


__all__ = ['BY_ID', 'CORPUS_VERSION', 'FAMILIES', 'PHRASINGS', 'PRODUCTS', 'TASKS', 'corpus_fingerprint', 'digests',
           'manifest', 'oracle_markers', 'prompt_for', 'task_fingerprint']
