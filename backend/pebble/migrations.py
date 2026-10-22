"""Pre-upgrade SQLite safety: refusal before mutation, verified private backups, completion.

App calls this before Store opens the database, and again after every subsystem
constructor ran and before background workers start. The exact parent calls are
in docs/HARNESS_MIGRATIONS.md:

    upgrade = prepare(home)   # read-only checks; a verified backup when upgrading
    ...                       # Store and subsystem constructors with their recovery
    upgrade.complete()        # verify against the backup, then stamp the epoch
    upgrade.abort()           # any startup failure: nothing is marked complete

PRAGMA user_version is the schema epoch. Epoch 0 is every database written
before this layer; a newer epoch is refused before anything changes. A backup is
SQLite's online backup taken inside the same read snapshot as its checks, so
committed WAL frames are included and concurrent writers cannot tear it.
Manifests, receipts and inspection carry counts, hashes and versions, never row
values. Credentials live in the OS vault: nothing here reads, copies or restores
them, and no published backup is ever deleted implicitly.
"""

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import time
from collections import namedtuple
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .memory import KINDS


DATABASE = 'pebble.sqlite'
BACKUPS = 'upgrade-backups'
LOCK = 'upgrade-owner.lock'
SCHEMA_EPOCH = 9
MANIFEST_FORMAT = 'pebble-upgrade-backup/1'
RECEIPT_FORMAT = 'pebble-upgrade-receipt/1'
INSPECTION_FORMAT = 'pebble-upgrade-inspection/1'
BACKUP_ID = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{8}')
ENTRY = re.compile(r'(\d{8}T\d{6}Z-[0-9a-f]{8})\.(sqlite|manifest\.json|complete\.json)')
PARTIAL = re.compile(r'\.tmp-[0-9a-f]{16}\.(?:sqlite(?:-journal|-wal|-shm)?|json)')
SIDECARS = ('-wal', '-shm', '-journal')
MAX_RECORD = 1 << 20
CREDENTIALS = ('Provider and connector credentials live in the OS vault. None are stored in, '
               'copied with or restored from this database.')
SCOPE = ('pebble.sqlite only. Attachment files, media snapshots, AgentGuard data, worktrees '
         'and logs are not part of an upgrade backup.')

REASONS = {
    'corrupt': 'The database failed SQLite integrity checks.',
    'future_version': 'The database was written by a newer Pebble.',
    'unknown_version': 'The database schema version is not one this Pebble knows.',
    'incompatible_core': 'Conversation, settings, action or memory tables have an incompatible structure.',
    'incompatible_subsystem': 'A Pebble subsystem table has an incompatible structure.',
    'incompatible_object': 'A Pebble table name is used by an incompatible database object.',
    'unknown_trigger': 'The database contains a trigger this Pebble does not know.',
    'unknown_constraint': 'A Pebble table carries a uniqueness constraint this Pebble does not know.',
    'subsystem_version': 'A Pebble subsystem records a version this Pebble does not support.',
    'unknown_runtime_version': 'The database records a runtime version this Pebble does not know.',
    'missing_runtime_version': 'Subsystem data exists without its version record.',
    'busy': 'Another program held the database for too long.',
    'deadline': 'The upgrade check exceeded its time limit.',
    'unreadable': 'The database could not be opened read-only.',
    'io_error': 'A file operation failed.',
    'unsafe_path': 'A data or backup path is a link, shared, replaced or not owned by you.',
    'platform_unsupported': 'Verified upgrade backups need POSIX descriptor-relative file operations.',
    'upgrade_owned': 'Another Pebble process is preparing this data directory.',
    'database_too_large': 'The database exceeds the backup size limit.',
    'insufficient_space': 'There is not enough free disk space for a verified backup.',
    'backup_budget_exhausted': ('Retained upgrade backups reached their count or size limit. '
                                'Remove an old backup explicitly; none is deleted automatically.'),
    'backup_failed': 'A verified backup could not be written and published.',
    'backup_tampered': 'An upgrade backup or its manifest failed verification.',
    'pending_backup_invalid': 'The backup of an unfinished upgrade failed verification.',
    'pending_backup_ambiguous': 'More than one unfinished upgrade backup exists.',
    'pending_backup_mismatch': 'The unfinished upgrade backup does not belong to this database.',
    'schema_incomplete': 'Startup did not produce the complete current schema.',
    'preservation_failed': 'Startup changed or lost records that an upgrade must preserve. The backup is retained.',
    'receipt_failed': 'The upgrade completion receipt could not be published.',
    'backup_pending': 'This backup belongs to an unfinished upgrade.',
    'backup_missing': 'No such upgrade backup.',
    'restore_target': 'Restore needs a new directory outside the live data directory.',
    'not_prepared': 'This upgrade was already completed or abandoned.',
}


class MigrationRefused(ValueError):
    """Startup must stop. `reason` is a stable code; the text names no user values."""

    def __init__(self, reason, detail=None):
        self.reason, self.detail = reason, detail
        super().__init__('Pebble stopped this upgrade: ' + REASONS.get(reason, 'The upgrade check failed.')
                         + (f' ({detail})' if detail else ''))


@dataclass(frozen=True)
class Limits:
    deadline: float = 120.0               # seconds for prepare and again for complete
    busy: float = 5.0                     # SQLite busy timeout, never beyond the deadline
    lock_wait: float = 3.0                # wait for another process's upgrade ownership
    max_database_bytes: int = 2 << 30
    max_backups: int = 3                  # retained backups, including the new one
    max_backup_bytes: int = 4 << 30       # retained backup files, including the new one
    free_reserve: int = 256 << 20         # free space left after a new backup
    step_pages: int = 1024                # pages per online-backup step

    def __post_init__(self):
        for name in ('deadline', 'busy', 'lock_wait'):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f'Choose a finite, nonnegative {name}.')
        for name in ('max_database_bytes', 'max_backups', 'max_backup_bytes', 'free_reserve', 'step_pages'):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == 'step_pages' else 0):
                raise ValueError(f'Choose a nonnegative integer {name}.')


# The schema this build creates, statement for statement. Comparison is by
# introspection, so formatting differences do not matter; a drift test keeps it
# equal to what a fresh App creates.
REFERENCE_DDL = '''
CREATE TABLE chats(id TEXT PRIMARY KEY,title TEXT,workspace TEXT,updated REAL);
CREATE TABLE messages(id TEXT PRIMARY KEY,chat_id TEXT,role TEXT,content TEXT,created REAL);
CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE actions(id TEXT PRIMARY KEY,chat_id TEXT,kind TEXT,status TEXT,payload TEXT,created REAL);
CREATE TABLE memories(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
  scope TEXT NOT NULL, chat_id TEXT, origin TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL, expires REAL);
CREATE INDEX memories_scope ON memories(scope,kind,updated);
CREATE VIRTUAL TABLE memory_search USING fts5(id UNINDEXED,content,tokenize='unicode61');
CREATE TRIGGER memory_insert AFTER INSERT ON memories BEGIN
  INSERT INTO memory_search(id,content) VALUES(new.id,new.content); END;
CREATE TRIGGER memory_delete AFTER DELETE ON memories BEGIN
  DELETE FROM memory_search WHERE id=old.id; END;
CREATE TRIGGER memory_update AFTER UPDATE OF content ON memories BEGIN
  DELETE FROM memory_search WHERE id=old.id;
  INSERT INTO memory_search(id,content) VALUES(new.id,new.content); END;
CREATE TABLE checkpoints(chat_id TEXT PRIMARY KEY,payload TEXT,updated REAL);
CREATE TABLE metrics(message_id TEXT PRIMARY KEY,chat_id TEXT,payload TEXT);
CREATE TABLE extension_tool_routes(run_id TEXT,plugin_id TEXT,tool TEXT,digest TEXT,snapshot TEXT,PRIMARY KEY(run_id,plugin_id,tool));
CREATE TABLE extensions(id TEXT PRIMARY KEY,kind TEXT,name TEXT,content TEXT,config TEXT,enabled INTEGER,created REAL);
CREATE TABLE extension_bundles(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
CREATE TABLE extension_hook_audit(id INTEGER PRIMARY KEY,run_id TEXT,extension_id TEXT,digest TEXT,event TEXT,decision TEXT,status TEXT,elapsed REAL,created REAL);
CREATE TABLE extension_hook_budget(run_id TEXT PRIMARY KEY,calls INTEGER,seconds REAL);
CREATE TABLE extension_data_routes(run_id TEXT,plugin_id TEXT,kind TEXT,identifier TEXT,digest TEXT,snapshot TEXT,PRIMARY KEY(run_id,plugin_id,kind,identifier));
CREATE TABLE extension_data_snapshots(id TEXT PRIMARY KEY,run_id TEXT,plugin_id TEXT,kind TEXT,identifier TEXT,digest TEXT,body TEXT,body_hash TEXT,created REAL);
CREATE TABLE image_attachments(id TEXT PRIMARY KEY, chat_id TEXT, message_id TEXT, name TEXT, data BLOB, preview TEXT, width INTEGER, height INTEGER);
CREATE TABLE file_attachments(id TEXT PRIMARY KEY, chat_id TEXT, message_id TEXT, name TEXT, kind TEXT, bytes INTEGER, text TEXT, notice TEXT, suffix TEXT, sha256 TEXT);
CREATE TABLE artifacts(id TEXT PRIMARY KEY, chat_id TEXT, title TEXT, kind TEXT, data TEXT, created REAL);
CREATE INDEX artifacts_chat ON artifacts(chat_id);
CREATE TABLE artifact_meta(version INTEGER NOT NULL);
CREATE TABLE artifact_revisions(
  artifact_id TEXT NOT NULL, chat_id TEXT NOT NULL, revision INTEGER NOT NULL,
  title TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
  owner_run_id TEXT, evidence_ids TEXT NOT NULL, status TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL, data_hash TEXT NOT NULL,
  previous_hash TEXT NOT NULL, record_hash TEXT NOT NULL,
  PRIMARY KEY(artifact_id,revision));
CREATE TABLE artifact_heads(artifact_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, record_hash TEXT NOT NULL);
CREATE TABLE artifact_registry(artifact_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX artifact_revisions_chat ON artifact_revisions(chat_id,artifact_id,revision);
CREATE TABLE runtime_meta(version INTEGER NOT NULL);
CREATE TABLE runs(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, goal TEXT NOT NULL,
  state TEXT NOT NULL, config TEXT NOT NULL, checkpoint TEXT,
  steps INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
  error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX runs_chat ON runs(chat_id,created);
CREATE UNIQUE INDEX runs_one_owner ON runs((1))
  WHERE state IN ('queued','running','retry_wait');
CREATE TABLE run_events(
  run_id TEXT NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL,
  payload TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(run_id,sequence));
CREATE TABLE run_tools(
  run_id TEXT NOT NULL, call_id TEXT NOT NULL, name TEXT NOT NULL,
  arguments TEXT NOT NULL, state TEXT NOT NULL, result TEXT,
  PRIMARY KEY(run_id,call_id));
CREATE TABLE run_action_owners(
  action_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,call_id TEXT NOT NULL,kind TEXT NOT NULL);
CREATE TABLE run_handoffs(
  run_id TEXT PRIMARY KEY, packet TEXT NOT NULL,
  manifest TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE run_compactions(
  run_id TEXT NOT NULL, generation INTEGER NOT NULL,
  archive TEXT NOT NULL, manifest TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(run_id,generation));
CREATE TABLE run_sources(
  run_id TEXT NOT NULL, receipt_id TEXT NOT NULL, path TEXT NOT NULL,
  file_hash TEXT NOT NULL, disk_hash TEXT, basis TEXT NOT NULL,
  status TEXT NOT NULL, checked REAL NOT NULL, current_hash TEXT,
  root_dev INTEGER NOT NULL, root_ino INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  PRIMARY KEY(run_id,receipt_id,path));
CREATE INDEX run_sources_path ON run_sources(run_id,path);
CREATE TABLE search_index_meta(key TEXT PRIMARY KEY, value);
CREATE TABLE search_index_roots(ns TEXT PRIMARY KEY, root BLOB NOT NULL,
  generation INTEGER NOT NULL, state TEXT NOT NULL, stats TEXT NOT NULL, used REAL NOT NULL);
CREATE TABLE search_index_files(ns TEXT NOT NULL, path TEXT NOT NULL,
  meta TEXT NOT NULL, eligible INTEGER NOT NULL, status TEXT, hash TEXT, checked INTEGER,
  body BLOB, PRIMARY KEY(ns, path));
CREATE TABLE run_inbox(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  kind TEXT NOT NULL, origin TEXT NOT NULL, payload TEXT NOT NULL,
  status TEXT NOT NULL, created REAL NOT NULL, delivered_step INTEGER,
  UNIQUE(run_id,sequence));
CREATE TABLE run_questions(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, call_id TEXT NOT NULL,
  revision INTEGER NOT NULL, required INTEGER NOT NULL, status TEXT NOT NULL,
  items TEXT NOT NULL, answer TEXT, created REAL NOT NULL, answered REAL,
  UNIQUE(run_id,call_id));
CREATE TABLE run_continuations(
  run_id TEXT PRIMARY KEY, reason TEXT NOT NULL, state TEXT NOT NULL,
  error TEXT, updated REAL NOT NULL);
CREATE TABLE run_plans(
  run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, steps TEXT NOT NULL,
  updated REAL NOT NULL);
CREATE INDEX run_inbox_pending ON run_inbox(run_id,status,sequence);
CREATE INDEX run_questions_owner ON run_questions(run_id,status);
CREATE TABLE artifact_form_links(
  artifact_id TEXT NOT NULL, revision INTEGER NOT NULL,
  question_id TEXT NOT NULL UNIQUE, question_revision INTEGER NOT NULL,
  PRIMARY KEY(artifact_id,revision));
CREATE TABLE mods_meta(version INTEGER NOT NULL);
CREATE TABLE mods(
  id TEXT PRIMARY KEY,name TEXT NOT NULL,manifest TEXT NOT NULL,digest TEXT NOT NULL,
  enabled INTEGER NOT NULL,grant_digest TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL,
  grant_revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE mod_reviews(digest TEXT PRIMARY KEY,manifest TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE mod_dispatches(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,chat_id TEXT NOT NULL,mod_id TEXT NOT NULL,
  digest TEXT NOT NULL,event TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,
  created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX mod_dispatches_run ON mod_dispatches(run_id,created);
CREATE TABLE mod_panes(
  mod_id TEXT NOT NULL,run_id TEXT NOT NULL,pane_id TEXT NOT NULL,chat_id TEXT NOT NULL,
  artifact_id TEXT NOT NULL,revision INTEGER NOT NULL,
  PRIMARY KEY(mod_id,run_id,pane_id));
CREATE TABLE memory_candidates(
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  kind TEXT NOT NULL, scope TEXT NOT NULL, slot TEXT NOT NULL,
  content TEXT NOT NULL, fingerprint TEXT NOT NULL, evidence TEXT NOT NULL,
  input_id TEXT, expires REAL, supersedes TEXT, status TEXT NOT NULL,
  reason TEXT, memory_id TEXT, created REAL NOT NULL, updated REAL NOT NULL,
  validated_proof TEXT);
CREATE INDEX memory_candidates_queue ON memory_candidates(status,created);
CREATE INDEX memory_candidates_run ON memory_candidates(run_id);
CREATE TABLE memory_evidence(
  memory_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, run_id TEXT NOT NULL,
  chat_id TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
  slot TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
  proof TEXT NOT NULL, superseded_by TEXT, updated REAL NOT NULL);
CREATE INDEX memory_evidence_slot ON memory_evidence(scope,kind,slot,status);
CREATE TABLE memory_tombstones(
  scope TEXT NOT NULL, kind TEXT NOT NULL, slot TEXT NOT NULL,
  fingerprint TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY(scope,kind,slot,fingerprint));
CREATE TABLE memory_watermarks(key TEXT PRIMARY KEY,through REAL NOT NULL);
CREATE TABLE memory_run_consent(
  run_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,enabled INTEGER NOT NULL,layers TEXT NOT NULL);
CREATE TABLE memory_recipe_failures(
  scope TEXT NOT NULL,recipe TEXT NOT NULL,through REAL NOT NULL,
  PRIMARY KEY(scope,recipe));
CREATE TABLE connector_grants(plugin_id TEXT PRIMARY KEY,grant_id TEXT,revision INTEGER,state TEXT,metadata TEXT,updated REAL);
CREATE TABLE connector_cleanup(plugin_id TEXT,grant_id TEXT PRIMARY KEY);
CREATE TABLE connector_registrations(registration_id TEXT PRIMARY KEY,plugin_id TEXT,issuer TEXT,resource TEXT,base_digest TEXT,plan_digest TEXT,state TEXT,grant_id TEXT,metadata TEXT,updated REAL);
CREATE TABLE plugin_packages(plugin_id TEXT PRIMARY KEY,package_id TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL);
CREATE TABLE plugin_package_revisions(plugin_id TEXT,revision INTEGER,bundle TEXT NOT NULL,snapshot TEXT NOT NULL,source TEXT NOT NULL,operation TEXT NOT NULL,source_revision INTEGER,created REAL,PRIMARY KEY(plugin_id,revision));
CREATE TABLE extension_media_snapshots(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,plugin_id TEXT NOT NULL,receipt_id TEXT NOT NULL,body TEXT NOT NULL,body_hash TEXT NOT NULL,data BLOB NOT NULL,created REAL NOT NULL);
CREATE TABLE agent_trees(
  run_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,config TEXT NOT NULL,
  tools TEXT NOT NULL,max_steps INTEGER NOT NULL,max_seconds REAL NOT NULL,
  max_tokens INTEGER NOT NULL,allocated_steps INTEGER NOT NULL DEFAULT 0,
  allocated_seconds REAL NOT NULL DEFAULT 0,allocated_tokens INTEGER NOT NULL DEFAULT 0,
  dispatches INTEGER NOT NULL DEFAULT 0,charged_tokens INTEGER NOT NULL DEFAULT 0,
  completed_steps INTEGER NOT NULL DEFAULT 0,child_seconds REAL NOT NULL DEFAULT 0,created REAL NOT NULL);
CREATE TABLE agent_nodes(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,parent_id TEXT,name TEXT NOT NULL,
  task TEXT NOT NULL,context TEXT NOT NULL,tools TEXT NOT NULL,limits TEXT NOT NULL,
  dependencies TEXT NOT NULL,write_paths TEXT NOT NULL,depth INTEGER NOT NULL,
  generation INTEGER NOT NULL,status TEXT NOT NULL,result TEXT,evidence_ids TEXT NOT NULL,
  review TEXT NOT NULL DEFAULT 'pending',dispatches INTEGER NOT NULL DEFAULT 0,
  charged_tokens INTEGER NOT NULL DEFAULT 0,elapsed REAL NOT NULL DEFAULT 0,
  created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX agent_nodes_tree ON agent_nodes(run_id,created);
CREATE TABLE agent_claims(
  agent_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,generation INTEGER NOT NULL,
  owner TEXT NOT NULL,expires REAL NOT NULL,heartbeat REAL NOT NULL);
CREATE TABLE agent_path_leases(
  run_id TEXT NOT NULL,path TEXT NOT NULL,agent_id TEXT NOT NULL,
  generation INTEGER NOT NULL,baseline TEXT,status TEXT NOT NULL,
  PRIMARY KEY(run_id,path));
CREATE TABLE agent_dispatches(
  run_id TEXT NOT NULL,id TEXT NOT NULL,actor TEXT NOT NULL,generation INTEGER NOT NULL,
  input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,status TEXT NOT NULL,
  started REAL NOT NULL,elapsed REAL NOT NULL DEFAULT 0,
  PRIMARY KEY(run_id,id));
CREATE TABLE agent_usage(
  run_id TEXT NOT NULL,dispatch_id TEXT NOT NULL,actor TEXT NOT NULL,generation INTEGER NOT NULL,
  attempt INTEGER NOT NULL,outcome TEXT NOT NULL,state TEXT NOT NULL,input_tokens INTEGER,
  output_tokens INTEGER,cached_tokens INTEGER,recorded REAL NOT NULL,
  PRIMARY KEY(run_id,dispatch_id));
CREATE TABLE agent_receipts(
  run_id TEXT NOT NULL,agent_id TEXT NOT NULL,generation INTEGER NOT NULL,
  call_id TEXT NOT NULL,name TEXT NOT NULL,arguments TEXT NOT NULL,
  state TEXT NOT NULL,result TEXT,PRIMARY KEY(agent_id,generation,call_id));
CREATE TABLE agent_messages(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,sequence INTEGER NOT NULL,
  sender TEXT NOT NULL,target TEXT NOT NULL,generation INTEGER NOT NULL,
  body TEXT NOT NULL,kind TEXT NOT NULL,correlation_id TEXT,idempotency_key TEXT NOT NULL,
  acked INTEGER NOT NULL DEFAULT 0,revoked INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,
  UNIQUE(run_id,sequence),UNIQUE(run_id,sender,idempotency_key));
CREATE TABLE agent_waits(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,actor TEXT NOT NULL,call_id TEXT NOT NULL,
  agent_ids TEXT NOT NULL,cursor INTEGER NOT NULL,status TEXT NOT NULL,
  result TEXT,notified INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,
  UNIQUE(run_id,actor,call_id));
CREATE TABLE agent_parent_questions(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,agent_id TEXT NOT NULL,
  generation INTEGER NOT NULL,parent_id TEXT NOT NULL,call_id TEXT NOT NULL,
  items TEXT NOT NULL,status TEXT NOT NULL,answer TEXT,created REAL NOT NULL,
  answer_origin TEXT,UNIQUE(agent_id,generation,call_id));
CREATE TABLE agent_mail_reads(
  message_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,cursor INTEGER NOT NULL,
  call_id TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE agent_inputs(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,agent_id TEXT NOT NULL,generation INTEGER NOT NULL,
  origin TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE agent_checkpoints(
  agent_id TEXT NOT NULL,generation INTEGER NOT NULL,messages TEXT NOT NULL,step INTEGER NOT NULL,
  PRIMARY KEY(agent_id,generation));
CREATE TABLE agent_contexts(
  agent_id TEXT NOT NULL,generation INTEGER NOT NULL,manifest TEXT NOT NULL,
  PRIMARY KEY(agent_id,generation));
CREATE TABLE agent_native_questions(
  question_id TEXT PRIMARY KEY,parent_question_id TEXT UNIQUE NOT NULL,run_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,generation INTEGER NOT NULL);
CREATE TABLE agent_question_cancellations(
  question_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,origin TEXT NOT NULL);
CREATE TABLE agent_patches(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,agent_id TEXT NOT NULL,generation INTEGER NOT NULL,
  path TEXT NOT NULL,baseline TEXT NOT NULL,content TEXT NOT NULL,receipt_id TEXT NOT NULL,
  status TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE agent_effects(
  id TEXT PRIMARY KEY,run_id TEXT NOT NULL,chat_id TEXT NOT NULL,agent_id TEXT NOT NULL,
  generation INTEGER NOT NULL,call_id TEXT NOT NULL,tool TEXT NOT NULL,arguments TEXT NOT NULL,
  args_hash TEXT NOT NULL,purpose TEXT,workspace TEXT,route_identity TEXT,binding TEXT NOT NULL,
  permission_mode TEXT NOT NULL,status TEXT NOT NULL,root_call_id TEXT,effect_call_id TEXT,
  action_id TEXT,outcome TEXT,reason TEXT,created REAL NOT NULL,updated REAL NOT NULL,
  workspace_identity TEXT,binding_version INTEGER,
  UNIQUE(agent_id,generation,call_id));
CREATE INDEX agent_effects_run ON agent_effects(run_id,status);
CREATE INDEX agent_effects_call ON agent_effects(run_id,effect_call_id);
CREATE INDEX agent_effects_action ON agent_effects(action_id);
CREATE TABLE git_worktrees(id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,
  workspace TEXT NOT NULL,root_identity TEXT NOT NULL,git_identity TEXT NOT NULL,path TEXT NOT NULL,
  base_commit TEXT NOT NULL,base_ref TEXT,path_identity TEXT,state TEXT NOT NULL,
  evidence TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX git_worktrees_chat ON git_worktrees(chat_id,state);
CREATE TABLE job_meta(version INTEGER NOT NULL);
CREATE TABLE jobs(
  id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin='human'),
  label TEXT NOT NULL, task TEXT NOT NULL, schedule TEXT NOT NULL, monitor INTEGER NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','paused','needs_review','finished')),
  revision INTEGER NOT NULL, pin TEXT NOT NULL, pin_fingerprint TEXT NOT NULL,
  anchor REAL NOT NULL, next_due REAL, last_scheduled REAL, last_fingerprint TEXT,
  review_reason TEXT, deferred_reason TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX jobs_due ON jobs(state,next_due);
CREATE INDEX jobs_chat ON jobs(chat_id,created);
CREATE TABLE job_fires(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, chat_id TEXT NOT NULL, revision INTEGER NOT NULL,
  scheduled REAL NOT NULL, missed INTEGER NOT NULL, origin TEXT NOT NULL CHECK(origin='host'),
  status TEXT NOT NULL CHECK(status IN ('claimed','bound','running','waiting','attention','completed',
    'failed','cancelled','needs_review','blocked','dismissed')),
  reason TEXT, run_id TEXT UNIQUE, run_state TEXT, pin_fingerprint TEXT NOT NULL,
  result_fingerprint TEXT, label_message_id TEXT, claimed REAL NOT NULL, updated REAL NOT NULL,
  UNIQUE(job_id,scheduled,revision));
CREATE UNIQUE INDEX job_fires_one_live ON job_fires(job_id)
  WHERE status IN ('claimed','bound','running','waiting');
CREATE INDEX job_fires_status ON job_fires(status);
CREATE TABLE job_notifications(
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, fire_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  run_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('completed','failed','action_needed')),
  status TEXT NOT NULL, fingerprint TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
  created REAL NOT NULL, acknowledged REAL, UNIQUE(fire_id,status,fingerprint));
CREATE INDEX job_notifications_pending ON job_notifications(acknowledged,created);
CREATE TABLE IF NOT EXISTS upgrade_history(id INTEGER PRIMARY KEY, backup_id TEXT,
  from_version INTEGER NOT NULL, to_version INTEGER NOT NULL, completed REAL NOT NULL);
'''
from .stream_journal import STREAM_DDL
from .process_jobs import PROCESS_DDL
from .process_reviews import REVIEW_DDL
_history_offset = REFERENCE_DDL.index('CREATE TABLE IF NOT EXISTS upgrade_history')
from .child_worktrees import WORKTREE_DDL
from .git_plans import GIT_PLAN_DDL
from .environments import ENVIRONMENT_DDL
# Epoch 4 (Phase 13): child worktree ledger/writes, Git plan receipts, managed environments.
REFERENCE_DDL = (REFERENCE_DDL[:_history_offset] + PROCESS_DDL + STREAM_DDL + REVIEW_DDL
                 + WORKTREE_DDL + GIT_PLAN_DDL + ENVIRONMENT_DDL + REFERENCE_DDL[_history_offset:])
from .time_ledger import LEDGER_DDL
# Epoch 5 (Phase 14): cross-crash active-time ledger and root-pool time reservations.
_history_offset = REFERENCE_DDL.index('CREATE TABLE IF NOT EXISTS upgrade_history')
REFERENCE_DDL = REFERENCE_DDL[:_history_offset] + LEDGER_DDL + REFERENCE_DDL[_history_offset:]
# Epoch 6 (Phase 15): memory relation graph, contradiction claims, tombstone Bloom filters and scope salts
# (memory_graph); delegation ping-pong rounds (agent_tree) and content-free delegation history.
from .memory_graph import GRAPH_DDL
from .agent_tree import ROUNDS_DDL
from .delegation_history import HISTORY_DDL as DELEGATION_HISTORY_DDL
# Also per-message route classes for the reviewed model handoff (C10) and provider token calibration (C08).
from .store import ROUTES_DDL
from .token_calibration import DDL as CALIBRATION_DDL
_history_offset = REFERENCE_DDL.index('CREATE TABLE IF NOT EXISTS upgrade_history')
REFERENCE_DDL = (REFERENCE_DDL[:_history_offset] + GRAPH_DDL + ROUNDS_DDL + DELEGATION_HISTORY_DDL
                 + ROUTES_DDL + CALIBRATION_DDL + REFERENCE_DDL[_history_offset:])
# Epoch 7 (Phase 16): the research source ledger (operational evidence, not a memory layer), scoped screen
# sessions and their acts, and the connector effect journal with native recipient confirmations.
from .source_ledger import RESEARCH_DDL, TABLES as RESEARCH_TABLES
from .research_store import DDL as RESEARCH_STORE_DDL, TABLES as RESEARCH_STORE_TABLES
from .screen_session import SCREEN_DDL, TABLES as SCREEN_TABLES
from .connector_effects import DDL as CONNECTOR_EFFECTS_DDL
_history_offset = REFERENCE_DDL.index('CREATE TABLE IF NOT EXISTS upgrade_history')
REFERENCE_DDL = (REFERENCE_DDL[:_history_offset] + RESEARCH_DDL + RESEARCH_STORE_DDL + SCREEN_DDL
                 + ';\n'.join(CONNECTOR_EFFECTS_DDL) + ';\n' + REFERENCE_DDL[_history_offset:])
# Epoch 8 (Phase 17): lifecycle observations, the native notification outbox, banners and quiet host notices,
# export records and saves, and human goals with criteria, a step DAG and append-only goal events.
from .lifecycle import LIFECYCLE_DDL
from .notifications import NOTIFY_DDL
from .exports import EXPORT_DDL
from .goals import GOAL_DDL
_history_offset = REFERENCE_DDL.index('CREATE TABLE IF NOT EXISTS upgrade_history')
REFERENCE_DDL = (REFERENCE_DDL[:_history_offset] + LIFECYCLE_DDL + NOTIFY_DDL + EXPORT_DDL.strip() + '\n'
                 + GOAL_DDL + REFERENCE_DDL[_history_offset:])
# Epoch 9 (Phase 19): host-held quiz answer keys and human quiz attempts. Dashboard reads and refreshes store nothing.
from .quiz_grading import DDL as QUIZ_DDL, TABLES as QUIZ_TABLES
_history_offset = REFERENCE_DDL.index('CREATE TABLE IF NOT EXISTS upgrade_history')
REFERENCE_DDL = REFERENCE_DDL[:_history_offset] + QUIZ_DDL.strip() + '\n' + REFERENCE_DDL[_history_offset:]
HISTORY_DDL = REFERENCE_DDL[REFERENCE_DDL.index('CREATE TABLE IF NOT EXISTS upgrade_history'):].strip()

# Exact column lists: positional INSERTs depend on them.
CORE = frozenset({'chats', 'messages', 'settings', 'actions', 'memories', 'checkpoints', 'metrics'})
# Columns an older database may lack; their owners add them with ALTER TABLE.
ADDITIVE = {
    'agent_trees': {'completed_steps'},
    'agent_parent_questions': {'answer_origin'},
    'agent_effects': {'workspace_identity', 'binding_version'},
    'memory_candidates': {'validated_proof'},
    'mods': {'grant_revision'},
    # Epoch 5 (Phase 14): supervised-job columns; no CHECK clauses so older tables stay compatible.
    'process_jobs': {'jobdir', 'plan_hash', 'contained', 'settled_by', 'reservation_id'},
}
# Single-row subsystem versions, and the data tables that must not exist without them.
VERSIONS = {'runtime_meta': 1, 'job_meta': 1, 'mods_meta': 1, 'artifact_meta': 1, 'run_stream_meta': 1, 'process_meta': 1}
GUARDED = {
    'runtime_meta': ('runs', 'run_events', 'run_tools', 'run_action_owners', 'run_handoffs', 'run_compactions'),
    'run_stream_meta': ('run_stream_attempts', 'run_stream_events'),
    'process_meta': ('process_jobs', 'process_output'),
    'job_meta': ('jobs', 'job_fires', 'job_notifications'),
    'mods_meta': ('mods', 'mod_reviews', 'mod_dispatches', 'mod_panes'),
    'artifact_meta': ('artifact_revisions', 'artifact_heads', 'artifact_registry'),
}
SEARCH_SCHEMA = 1  # search_index.SCHEMA; an older cache is rebuilt by its owner
OWN = frozenset({'upgrade_history'})  # created by completion itself
# An unknown table that looks like a version record means an unknown runtime wrote here.
MARKER = re.compile(r'(?:^|_)(?:meta|version|versions|schema)$')

# Rows of the pre-upgrade backup that startup must keep. `stable` columns stay
# identical (columns the backup lacks are additive and skipped). `exempt` rows
# are checked separately, `missing` rows may legitimately disappear, and
# `transition` admits only the host's own fail-closed recovery between the
# backup row `o` and the live row `n`. Nothing is re-pinned, enabled, claimed,
# started or applied by an upgrade. run_tools and agent_effects outcomes are
# verified in Python against the exact pre-existing records they derive from.
Keep = namedtuple('Keep', 'keys stable exempt missing transition', defaults=(None, None, None))
PRESERVED = {
    'chats': Keep(('id',), ('title', 'workspace', 'updated')),
    'messages': Keep(('id',), ('chat_id', 'role', 'content', 'created')),
    'settings': Keep(('key',), ('value',), exempt="o.key IN ('permissions','model_profiles')"),
    # Only AgentEffects revocation settles a pending action during startup, and only as rejected.
    'actions': Keep(('id',), ('chat_id', 'kind', 'payload', 'created'), transition=(
        "n.status IS o.status OR (o.status = 'pending' AND n.status = 'rejected' AND EXISTS("
        "SELECT 1 FROM main.agent_effects e WHERE e.action_id = o.id AND e.status = 'revoked'))")),
    # Expired rows, and automatic history beyond Memory.put's 1,500-row bound, may go; nothing user-authored.
    'memories': Keep(('id',), ('kind', 'content', 'scope', 'chat_id', 'origin', 'created', 'updated', 'expires'),
                     missing="o.expires IS NOT NULL AND o.expires <= :now OR o.origin = 'conversation' AND "
                             "(SELECT count(*) FROM b.memories c WHERE c.origin = 'conversation') > 1500"),
    'checkpoints': Keep(('chat_id',), ('payload', 'updated')),
    'metrics': Keep(('message_id',), ('chat_id', 'payload')),
    'memory_tombstones': Keep(('scope', 'kind', 'slot', 'fingerprint'), ('created',)),
    'memory_evidence': Keep(('memory_id',), ('candidate_id', 'run_id', 'chat_id', 'scope', 'kind', 'slot',
                                             'fingerprint', 'proof')),
    'connector_grants': Keep(('plugin_id',), ('grant_id', 'revision', 'state', 'metadata', 'updated')),
    'connector_registrations': Keep(('registration_id',), ('plugin_id', 'issuer', 'resource', 'base_digest',
                                                            'plan_digest', 'grant_id', 'metadata'),
                                    transition="n.state IS o.state OR (o.state = 'admitted' AND n.state = 'unknown')"),
    'extensions': Keep(('id',), ('kind', 'content', 'created'), transition='n.enabled IS o.enabled OR n.enabled = 0'),
    'mods': Keep(('id',), ('name', 'manifest', 'digest', 'grant_digest', 'grant_revision', 'created'),
                 transition='n.enabled IS o.enabled OR n.enabled = 0'),
    'plugin_packages': Keep(('plugin_id',), ('package_id', 'revision', 'digest')),
    'jobs': Keep(('id',), ('chat_id', 'origin', 'label', 'task', 'schedule', 'monitor', 'revision', 'pin',
                           'pin_fingerprint', 'anchor', 'last_fingerprint', 'created'),
                 transition="n.state IS o.state OR (o.state = 'active' AND n.state = 'needs_review')"),
    'job_fires': Keep(('id',), ('job_id', 'chat_id', 'revision', 'scheduled', 'missed', 'origin', 'run_id',
                                'pin_fingerprint', 'result_fingerprint', 'label_message_id', 'claimed'),
                      transition="n.status IS o.status OR (o.status = 'claimed' AND n.status = 'needs_review')"),
    'runs': Keep(('id',), ('chat_id', 'goal', 'config', 'checkpoint', 'steps', 'elapsed', 'created'), transition=(
        "n.state IS o.state OR (o.state = 'waiting_input' AND n.state = 'stopped') OR "
        "(n.state IN ('interrupted','needs_reconciliation') AND (o.state IN ('queued','running','retry_wait') "
        "OR EXISTS(SELECT 1 FROM b.run_tools t WHERE t.run_id = o.id AND t.state = 'running')))")),
    'run_tools': Keep(('run_id', 'call_id'), ('name', 'arguments')),
    'run_events': Keep(('run_id', 'sequence'), ('kind', 'payload', 'created')),
    'agent_effects': Keep(('id',), ('run_id', 'chat_id', 'agent_id', 'generation', 'call_id', 'tool', 'arguments',
                                    'args_hash', 'workspace', 'route_identity', 'binding', 'permission_mode',
                                    'workspace_identity', 'binding_version', 'created')),
    'upgrade_history': Keep(('id',), ('backup_id', 'from_version', 'to_version', 'completed')),
}
# Startup creates none of these records before completion: a new row is forged authority or effect.
NO_NEW = ('actions', 'runs', 'run_tools', 'jobs', 'job_fires', 'connector_grants', 'connector_registrations',
          'plugin_packages', 'extensions', 'mods', 'agent_effects', 'process_jobs', 'process_output',
          'process_inspections', 'run_stream_attempts', 'agent_worktrees', 'agent_worktree_writes',
          'git_plan_receipts', 'project_environments')
PRESERVED.update({
    'process_jobs': Keep(('id',), ('action_id', 'chat_id', 'run_id', 'call_id', 'root', 'root_identity',
        'payload_hash', 'command', 'network', 'writable', 'owner', 'pid', 'stop_requested', 'output_bytes',
        'output_dropped', 'timeout', 'created', 'started', 'exit_code', 'isolation'),
        transition="(n.state IS o.state AND n.outcome_uncertain IS o.outcome_uncertain AND n.reason IS o.reason AND n.finished IS o.finished) OR (o.state IN ('admitted','running') AND n.state = 'unknown' AND n.outcome_uncertain=1)"),
    'process_output': Keep(('process_id', 'sequence'), ('byte_start', 'data')),
    'process_inspections': Keep(('process_id',), ('run_id', 'chat_id', 'observation', 'note', 'created')),
    'run_stream_attempts': Keep(('id',), ('run_id', 'ordinal', 'step', 'attempt', 'bytes', 'deltas', 'created'),
        transition="n.state IS o.state OR (o.state = 'open' AND n.state = 'interrupted')"),
    'run_stream_events': Keep(('run_id', 'sequence'), ('attempt_id', 'kind', 'state', 'origin', 'text',
                                                    'size', 'sha256', 'created')),
})
# Epoch 4: unobserved effects settle only to their fail-closed recovery states.
PRESERVED.update({
    'agent_worktrees': Keep(('worktree_id',), ('run_id', 'chat_id', 'agent_id', 'generation', 'action_id', 'workspace',
        'root_identity', 'base_commit', 'created'),
        transition="n.state IS o.state OR (o.state = 'allocating' AND n.state IN ('uncertain','failed')) "
                   "OR (o.state = 'retiring' AND n.state IN ('uncertain','retired'))"),
    'agent_worktree_writes': Keep(('id',), ('worktree_id', 'run_id', 'agent_id', 'generation', 'call_id', 'path',
        'before_hash', 'after_hash', 'bytes', 'created')),
    'git_plan_receipts': Keep(('id',), ('chat_id', 'run_id', 'action_id', 'operation', 'plan_hash', 'created'),
        transition="n.state IS o.state OR (o.state = 'executing' AND n.state = 'uncertain')"),
    'project_environments': Keep(('id',), ('chat_id', 'root', 'root_identity', 'kind', 'manifest_digest', 'lock_digest',
        'toolchain', 'registries', 'created'),
        transition="n.state IS o.state OR (o.state = 'installing' AND n.state = 'uncertain')"),
})
# Epoch 6: memory graph rows may be written by startup (the local scope salt, migrated supersedes edges);
# delegation rounds and history, message routes and calibration samples are never created by startup.
NO_NEW = NO_NEW + ('agent_rounds', 'delegation_history', 'delegation_salt', 'message_routes', 'token_calibration')
PRESERVED.update({
    'memory_relations': Keep(('from_id', 'to_id', 'relation'), ('evidence', 'created')),
    'memory_claims': Keep(('memory_id',), ('scope', 'kind', 'claim_key', 'label', 'value', 'created')),
    'memory_tombstone_blooms': Keep(('scope',), ('bits', 'items', 'updated')),
    'memory_scope_salts': Keep(('salt_id',), ('salt', 'origin', 'created')),
    'agent_rounds': Keep(('agent_id', 'generation', 'call_id'), ('run_id', 'kind', 'created')),
    'delegation_history': Keep(('scope', 'event'), ('run_key', 'kind', 'outcome', 'detail', 'child_steps',
                                                    'child_tokens', 'recorded')),
    'delegation_salt': Keep(('id',), ('salt',)),
    'message_routes': Keep(('message_id',), ('chat_id', 'route_class')),
    'token_calibration': Keep(('route', 'sequence'), ('estimated', 'reported', 'created')),
})
# Epoch 7: startup never creates research rows; screen and connector recovery only settle in-flight rows
# to their fail-closed states (admitted -> unknown, proposed -> expired, live sessions -> restarted).
NO_NEW = NO_NEW + RESEARCH_TABLES + RESEARCH_STORE_TABLES + SCREEN_TABLES + ('connector_effects', 'connector_effect_confirmations')
PRESERVED.update({
    'research_sources': Keep(('run_id', 'n'), ('chat_id', 'agent_id', 'kind', 'url', 'final_url', 'title', 'query_id',
        'query', 'plan_id', 'retrieved_at', 'http_date', 'last_modified', 'claimed_published', 'claimed_modified',
        'date_basis', 'content_type', 'encoding', 'redirects', 'content_sha256', 'body_sha256', 'characters', 'bytes',
        'truncated', 'hidden_removed', 'domain', 'primary_class', 'flags')),
    'research_texts': Keep(('sha256',), ('characters', 'data')),
    'research_claim_rows': Keep(('run_id', 'claim_id'), ('chat_id', 'origin', 'agent_id', 'anchor_id', 'row', 'created')),
    'research_plans': Keep(('id',), ('run_id', 'chat_id', 'questions', 'queries', 'budgets', 'created')),
    'research_claims': Keep(('id',), ('run_id', 'chat_id', 'sentence_sha256', 'token', 'claim_key', 'value', 'source_n',
        'span_start', 'span_end', 'span_sha256', 'match', 'support', 'created')),
    'research_conflicts': Keep(('id',), ('run_id', 'chat_id', 'claim_key', 'claim_ids', 'claim_values', 'state',
        'resolution', 'created', 'updated')),
    'screen_sessions': Keep(('session_id',), ('chat_id', 'run_id', 'kind', 'target', 'target_sha256', 'pin', 'actions',
        'created', 'expires'),
        transition="n.state IS o.state OR (o.state IN ('active','handoff','interrupted') AND n.state = 'restarted')"),
    'screen_actions': Keep(('action_id',), ('session_id', 'chat_id', 'run_id', 'observation_digest', 'element', 'action',
        'class', 'text_sha256', 'text_chars', 'review', 'postcondition', 'created'),
        transition="(n.state IS o.state AND n.verdict IS o.verdict) OR (o.state = 'admitted' AND n.state = 'unknown' "
                   "AND n.verdict = 'unknown') OR (o.state = 'proposed' AND n.state = 'expired')"),
    'connector_effects': Keep(('action_id',), ('chat_id', 'run_id', 'plugin_id', 'tool', 'effect_class', 'assigned',
        'catalogue_digest', 'request_digest', 'idempotency_key', 'recipients_sha256', 'facts', 'created'),
        transition="n.state IS o.state OR (o.state = 'admitted' AND n.state = 'unknown')"),
    'connector_effect_confirmations': Keep(('action_id',), ('chat_id', 'recipients_sha256', 'confirmed')),
})
# Epoch 8: startup creates no lifecycle, notification, export or goal record. Export recovery only settles an
# interrupted render to failed and an interrupted save to uncertain; nothing else changes before completion.
NO_NEW = NO_NEW + ('lifecycle_observations', 'native_deliveries', 'native_banners', 'host_notices', 'export_records',
                   'export_saves', 'goals', 'goal_criteria', 'goal_steps', 'goal_events')
PRESERVED.update({
    'lifecycle_observations': Keep(('id',), ('kind', 'source', 'observed', 'started', 'seconds', 'zone')),
    'native_deliveries': Keep(('notification_id',), ('job_id', 'chat_id', 'category', 'state', 'reason', 'banner_id',
        'banner_version', 'attempts', 'created', 'posted', 'updated')),
    'native_banners': Keep(('id',), ('scope', 'job_id', 'chat_id', 'members', 'version', 'state', 'delivered_version',
        'attempts', 'reason', 'opened', 'posted', 'updated')),
    'host_notices': Keep(('id',), ('chat_id', 'job_id', 'kind', 'fingerprint', 'title', 'body', 'created', 'acknowledged')),
    'export_records': Keep(('id',), ('chat_id', 'plan_id', 'format', 'source', 'created'),
        transition="(n.state IS o.state AND n.receipt IS o.receipt AND n.updated IS o.updated) "
                   "OR (o.state = 'rendering' AND n.state = 'failed')"),
    'export_saves': Keep(('id',), ('export_id', 'chat_id', 'path', 'sha256', 'verified', 'created'),
        transition="(n.state IS o.state AND n.updated IS o.updated) OR (o.state = 'writing' AND n.state = 'uncertain')"),
    'goals': Keep(('id',), ('chat_id', 'origin', 'title', 'revision', 'state', 'created', 'updated')),
    'goal_criteria': Keep(('goal_id', 'id'), ('ordinal', 'text', 'kind', 'status', 'origin', 'evidence_ids',
                                              'checked_by', 'proposed_run_id', 'updated')),
    'goal_steps': Keep(('goal_id', 'id'), ('ordinal', 'title', 'status', 'depends_on', 'criteria', 'evidence_ids',
                                           'blocker_kind', 'blocker_ref', 'run_id', 'updated')),
    'goal_events': Keep(('goal_id', 'sequence'), ('kind', 'origin', 'run_id', 'payload', 'created')),
})
# Epoch 5: only startup recovery may close an open slice (fully charged) or settle another boot's hold.
NO_NEW = NO_NEW + ('run_time_ledger', 'run_time_reservations')
PRESERVED.update({
    'run_time_ledger': Keep(('slice_id',), ('run_id', 'boot_uuid', 'opened', 'opened_mono', 'charged'),
        transition="(n.state IS o.state AND n.closed IS o.closed) OR "
                   "(o.state = 'open' AND n.state IN ('crash','reboot') AND n.closed IS NOT NULL)"),
    'run_time_reservations': Keep(('reservation_id',), ('run_id', 'kind', 'ref', 'seconds', 'boot_uuid', 'opened',
                                                         'opened_mono'),
        transition="(n.state IS o.state AND n.used IS o.used AND n.charged IS o.charged AND n.closed IS o.closed) OR "
                   "(o.state = 'open' AND n.state = 'uncertain' AND n.used IS NULL AND n.charged = o.seconds "
                   "AND n.closed IS NOT NULL)"),
})
# Epoch 9 (design study §3.5): startup creates no quiz key or attempt and changes none (an attempt expires lazily
# at its next native read, never at startup). The artifact chain also gains preservation with every column stable;
# it stays out of NO_NEW because startup legitimately materializes legacy six-column artifacts (Artifacts._legacy).
NO_NEW = NO_NEW + QUIZ_TABLES
PRESERVED.update({
    'quiz_answer_keys': Keep(('artifact_id', 'revision'), ('chat_id', 'key_json', 'key_hash', 'public_hash', 'created')),
    'quiz_attempts': Keep(('id',), ('chat_id', 'artifact_id', 'revision', 'origin', 'status', 'clock_id', 'started',
        'started_mono', 'limit_seconds', 'elapsed_mark', 'draft', 'saved', 'answers', 'result', 'reason', 'submitted',
        'share_input_id', 'shared', 'updated')),
    'artifact_revisions': Keep(('artifact_id', 'revision'), ('chat_id', 'title', 'kind', 'data', 'owner_run_id',
        'evidence_ids', 'status', 'created', 'updated', 'data_hash', 'previous_hash', 'record_hash')),
    'artifact_heads': Keep(('artifact_id',), ('revision', 'record_hash')),
    'artifact_registry': Keep(('artifact_id',), ('chat_id', 'created')),
    'artifact_form_links': Keep(('artifact_id', 'revision'), ('question_id', 'question_revision')),
})
# Settings App writes during construction when absent, with exactly these values.
ADDED_SETTINGS = {'memory_initialized': True}  # plus permissions == permissions.DEFAULT
# Agent effect statuses the host's own recovery can reach from each earlier status.
EFFECT_RECOVERY = {
    'proposed': {'invalidated'},
    'awaiting_approval': {'applied', 'rejected', 'failed', 'uncertain', 'revoked'},
    'executing': {'proposed', 'invalidated', 'uncertain', 'failed', 'user_reported', 'awaiting_approval',
                  'applied', 'rejected', 'revoked'},
    'uncertain': {'user_reported'},
}

_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_DIRECTORY = getattr(os, 'O_DIRECTORY', 0)
SUPPORTED = (os.name == 'posix' and bool(_NOFOLLOW and _DIRECTORY) and hasattr(os, 'fchmod')
             and hasattr(os, 'fstatvfs')
             and {os.open, os.stat, os.mkdir, os.unlink, os.link, os.rmdir} <= os.supports_dir_fd
             and os.listdir in os.supports_fd)


def supported():
    """Descriptor-relative, no-follow file operations exist (macOS, Linux)."""
    return SUPPORTED


def _platform():
    if not SUPPORTED:
        raise MigrationRefused('platform_unsupported')


# Small helpers

def _ident(info):
    return info.st_dev, info.st_ino


def _single(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()


def _kept(info):
    """A private, single-link regular file owned by this user."""
    return _single(info) and not info.st_mode & 0o077


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _seal(value, field):
    value = dict(value)
    value[field] = hashlib.sha256(_canonical(value).encode()).hexdigest()
    return value


def _unseal(raw, field, kind):
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get('format') != kind:
        return None
    seal = value.pop(field, None)
    if seal != hashlib.sha256(_canonical(value).encode()).hexdigest():
        return None
    value[field] = seal
    return value


def _text(raw):
    return raw.decode('utf-8', 'surrogateescape')


def _encode(value):
    if value is None:
        return b'N'
    if isinstance(value, int):
        return b'I%d;' % value
    if isinstance(value, float):
        return b'F' + value.hex().encode() + b';'
    if isinstance(value, str):
        value = value.encode('utf-8', 'surrogateescape')
        return b'T%d:' % len(value) + value
    return b'B%d:' % len(value) + bytes(value)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _label(key, known):
    """Known Pebble names stay readable; anything else is reported only by hash."""
    return key if key in known else 'unknown-' + hashlib.sha256(key.encode('utf-8', 'surrogateescape')).hexdigest()[:16]


def _uri(path, immutable=False):
    return Path(os.path.abspath(path)).as_uri() + ('?mode=ro&immutable=1' if immutable else '?mode=ro')


class _Timer:
    def __init__(self, seconds, clock):
        self.clock = clock
        self.end = clock() + seconds

    def left(self):
        return max(0.0, self.end - self.clock())

    def expired(self):
        return self.clock() >= self.end

    def check(self, *unused):
        if self.expired():
            raise MigrationRefused('deadline')

    def handler(self):
        # SQLite progress handler: nonzero interrupts the running statement.
        return 1 if self.expired() else 0


@contextmanager
def _errors(timer):
    try:
        yield
    except sqlite3.Error as exc:
        text = (str(getattr(exc, 'sqlite_errorname', '')) + ' ' + str(exc)).lower()
        if timer.expired() or 'interrupt' in text:
            reason = 'deadline'
        elif 'busy' in text or 'locked' in text:
            reason = 'busy'
        elif any(word in text for word in ('corrupt', 'malformed', 'notadb', 'not a database')):
            reason = 'corrupt'
        else:
            reason = 'unreadable'
        raise MigrationRefused(reason) from None


@contextmanager
def _reader(path, timer, limits, immutable=False):
    """A read-only SQLite connection bounded by the busy timeout and the deadline."""
    try:
        db = sqlite3.connect(_uri(path, immutable), uri=True, isolation_level=None, check_same_thread=False,
                             timeout=max(0.0, min(limits.busy, timer.left())))
    except sqlite3.Error:
        raise MigrationRefused('unreadable') from None
    db.text_factory = _text
    db.set_progress_handler(timer.handler, 1000)
    try:
        with _errors(timer):
            yield db
    finally:
        db.close()


class _Directory:
    """A directory pinned by descriptor; its entries are reached relative to it, never through links."""

    def __init__(self, path, fd, mask):
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & mask:
            os.close(fd)
            raise MigrationRefused('unsafe_path')
        self.path, self.fd, self.identity = Path(path), fd, _ident(info)

    @classmethod
    def open(cls, path, mask):
        """The existing directory at `path`, or None. A link or a shared directory is refused."""
        try:
            before = os.lstat(path)
            fd = os.open(path, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            raise MigrationRefused('unsafe_path') from None
        directory = cls(path, fd, mask)
        if _ident(before) != directory.identity:
            directory.close()
            raise MigrationRefused('unsafe_path')
        return directory

    def child(self, name, mask, create=False):
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
            except FileExistsError:
                pass
            except OSError:
                raise MigrationRefused('unsafe_path') from None
        try:
            before = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            fd = os.open(name, os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        except OSError:
            raise MigrationRefused('unsafe_path') from None
        directory = _Directory(self.path / name, fd, mask)
        if _ident(before) != directory.identity:
            directory.close()
            raise MigrationRefused('unsafe_path')
        return directory

    def check(self):
        """SQLite opens by path: the path must still name this directory."""
        try:
            info = os.lstat(self.path)
        except OSError:
            raise MigrationRefused('unsafe_path') from None
        if not stat.S_ISDIR(info.st_mode) or _ident(info) != self.identity:
            raise MigrationRefused('unsafe_path')

    def stat(self, name):
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def names(self):
        return sorted(os.listdir(self.fd))

    def create(self, name):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=self.fd)
        return fd, os.fstat(fd)

    def read(self, name, expected, timer=None, limit=None):
        """(sha256, size, bytes when limited) of a private file that is still `expected`."""
        try:
            fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=self.fd)
        except OSError:
            raise MigrationRefused('backup_tampered') from None
        try:
            info = os.fstat(fd)
            if not _kept(info) or _ident(info) != _ident(expected):
                raise MigrationRefused('backup_tampered')
            digest, size, kept = hashlib.sha256(), 0, []
            while chunk := os.read(fd, 1 << 20):
                digest.update(chunk)
                size += len(chunk)
                if limit is not None:
                    if size > limit:
                        raise MigrationRefused('backup_tampered')
                    kept.append(chunk)
                if timer is not None:
                    timer.check()
            return digest.hexdigest(), size, b''.join(kept)
        finally:
            os.close(fd)

    def remove(self, name, expected):
        """Unlink `name` only while it is still the regular file recorded in `expected`."""
        info = self.stat(name)
        if info is None or not stat.S_ISREG(info.st_mode) or _ident(info) != _ident(expected):
            return False
        os.unlink(name, dir_fd=self.fd)
        return True

    def link(self, source, target):
        # link(2) fails with EEXIST: publication never replaces an existing path.
        os.link(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def sync(self):
        os.fsync(self.fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _lock(root, limits):
    """Exclusive upgrade ownership of this data directory until completion or abort."""
    import fcntl
    try:
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600, dir_fd=root.fd)
    except OSError:
        raise MigrationRefused('unsafe_path') from None
    try:
        if not _single(os.fstat(fd)):
            raise MigrationRefused('unsafe_path')
        end = time.monotonic() + limits.lock_wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise MigrationRefused('upgrade_owned') from None
                time.sleep(0.05)
            except OSError:
                raise MigrationRefused('unsafe_path') from None
        os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _database(root):
    """Identity of pebble.sqlite (None when absent). Links and extra hard links are refused."""
    info = root.stat(DATABASE)
    if info is not None and not _single(info):
        raise MigrationRefused('unsafe_path')
    for suffix in SIDECARS:
        side = root.stat(DATABASE + suffix)
        if side is not None and not _single(side):
            raise MigrationRefused('unsafe_path')
        # A journal without its database would be replayed into a new one.
        if info is None and side is not None and suffix != '-shm':
            raise MigrationRefused('unsafe_path', 'journal without database')
    return info


def _same(root, info):
    now = root.stat(DATABASE)
    if now is None or _ident(now) != _ident(info):
        raise MigrationRefused('unsafe_path', 'database replaced')


# Schema introspection and compatibility

def _normal(sql):
    """SQL text without case or whitespace outside quoted text, for structural comparison."""
    out, quote = [], None
    for ch in sql or '':
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in '\'"`[':
            quote = ']' if ch == '[' else ch
            out.append(ch)
        elif not ch.isspace():
            out.append(ch.lower())
    return ''.join(out)


def _clauses(normal):
    """(sorted CHECK expressions, text after the column list) of a normalized CREATE TABLE."""
    checks, quote, depth, last, i = [], None, 0, -1, 0
    while i < len(normal):
        ch = normal[i]
        if quote:
            quote = None if ch == quote else quote
        elif ch in '\'"`[':
            quote = ']' if ch == '[' else ch
        elif normal.startswith('check(', i):
            start = j = i + 6
            level, inner = 1, None
            while j < len(normal) and level:
                c = normal[j]
                if inner:
                    inner = None if c == inner else inner
                elif c in '\'"`[':
                    inner = ']' if c == '[' else c
                elif c == '(':
                    level += 1
                elif c == ')':
                    level -= 1
                j += 1
            checks.append(normal[start:j - 1])
            i = j
            continue
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if not depth:
                last = i
        i += 1
    return sorted(checks), normal[last + 1:] if last >= 0 else ''


def _schema(db, prefix='main'):
    tables, indexes, triggers, views = {}, {}, {}, set()
    rows = db.execute(f"SELECT type,name,tbl_name,sql FROM {prefix}.sqlite_master "
                      "WHERE name NOT LIKE 'sqlite~_%' ESCAPE '~'").fetchall()
    for kind, name, owner, sql in rows:
        key, normal = name.lower(), _normal(sql)
        if kind == 'table':
            virtual = normal.startswith('createvirtualtable')
            columns, uniques = (), frozenset()
            if not virtual:
                columns = tuple((r[0].lower(), r[1], r[2], r[3], r[4], r[5]) for r in db.execute(
                    'SELECT name,upper(type),"notnull",dflt_value,pk,hidden FROM pragma_table_xinfo(?,?) ORDER BY cid',
                    (name, prefix)))
                found = set()
                for index, unique, origin in db.execute(
                        'SELECT name,"unique",origin FROM pragma_index_list(?,?)', (name, prefix)).fetchall():
                    if unique and origin == 'c':
                        found.add(('c', index.lower()))
                    elif unique:
                        found.add((origin, tuple(str(r[0]).lower() for r in db.execute(
                            'SELECT name FROM pragma_index_info(?,?) ORDER BY seqno', (index, prefix)))))
                uniques = frozenset(found)
            checks, tail = _clauses(normal)
            tables[key] = {'name': name, 'virtual': virtual, 'sql': normal, 'columns': columns,
                           'uniques': uniques, 'checks': checks, 'tail': tail}
        elif kind == 'index' and sql:
            indexes[key] = (owner.lower(), normal.startswith('createunique'), normal)
        elif kind == 'trigger':
            triggers[key] = (owner.lower(), normal)
        elif kind == 'view':
            views.add(key)
    return {'tables': tables, 'indexes': indexes, 'triggers': triggers, 'views': views}


@lru_cache(maxsize=1)
def _reference():
    db = sqlite3.connect(':memory:')
    try:
        db.executescript(REFERENCE_DDL)
        schema = _schema(db)
    finally:
        db.close()
    virtual = [key for key, table in schema['tables'].items() if table['virtual']]
    schema['shadows'] = frozenset(key for key, table in schema['tables'].items()
                                  if not table['virtual'] and any(key.startswith(v + '_') for v in virtual))
    return schema


def _primary(table):
    return tuple(c[0] for c in sorted((c for c in table['columns'] if c[4]), key=lambda c: c[4]))


def _table_problem(key, table, ref, schema, reference):
    if table['virtual'] or ref['virtual']:
        return None if table['sql'] == ref['sql'] else 'incompatible_object'
    reason = 'incompatible_core' if key in CORE else 'incompatible_subsystem'
    if table['tail'] != ref['tail'] or table['checks'] != ref['checks']:
        return reason
    columns, expected = {c[0]: c for c in table['columns']}, {c[0]: c for c in ref['columns']}
    if key in CORE:
        if table['columns'] != ref['columns']:
            return reason
    else:
        if set(columns) - set(expected) or set(expected) - set(columns) - ADDITIVE.get(key, set()):
            return reason
        for name, (_, _, notnull, default, _, hidden) in columns.items():
            want = expected[name]
            # A stricter column than this build writes would refuse its inserts.
            if hidden != want[5] or notnull and not want[2] or notnull and default is None and want[3] is not None:
                return reason
    if _primary(table) != _primary(ref):
        return reason
    if table['uniques'] - ref['uniques']:
        return 'unknown_constraint'
    if {u for u in ref['uniques'] if u[0] != 'c'} - table['uniques']:
        return reason
    for kind, name in table['uniques']:
        if kind == 'c' and schema['indexes'].get(name, (None, None, None))[2] != reference['indexes'][name][2]:
            return 'unknown_constraint'
    return None


def _compatibility(schema, reference):
    """Refusals plus what startup would add; names of unknown objects are never returned in clear."""
    refusals, missing, partial, unknown = [], [], {}, []
    have, known = schema['tables'], reference['tables']
    for key, ref in known.items():
        if key in reference['shadows']:
            continue
        table = have.get(key)
        if table is None:
            if key in schema['views'] or key in schema['indexes'] or key in schema['triggers']:
                refusals.append(('incompatible_object', key))
            elif key not in OWN:
                missing.append(key)
            continue
        problem = _table_problem(key, table, ref, schema, reference)
        if problem:
            refusals.append((problem, key))
        elif not table['virtual']:
            names = {c[0] for c in table['columns']}
            absent = [c[0] for c in ref['columns'] if c[0] not in names]
            if absent:
                partial[key] = absent
    for key, table in have.items():
        if key not in known:
            unknown.append(_label(key, known))
            if MARKER.search(key) and {c[0] for c in table['columns']} & {'version', 'schema', 'schema_version', 'key'}:
                refusals.append(('unknown_runtime_version', _label(key, known)))
    for key, value in schema['triggers'].items():
        if reference['triggers'].get(key) != value:
            refusals.append(('unknown_trigger', _label(key, reference['triggers'])))
    return {'refusals': refusals, 'missing_tables': sorted(missing), 'missing_columns': partial,
            'unknown_tables': sorted(unknown), 'views': len(schema['views']),
            'unknown_indexes': sum(key not in reference['indexes'] for key in schema['indexes'])}


def _versions(db, schema, prefix='main'):
    found, refusals = {}, []
    tables = schema['tables']
    for key, expected in VERSIONS.items():
        if key in tables:
            try:
                rows = [r[0] for r in db.execute(f'SELECT version FROM {prefix}.{_quote(tables[key]["name"])} LIMIT 2')]
            except sqlite3.OperationalError:
                rows = None  # no version column: refused structurally as well
            found[key] = [r for r in rows or () if type(r) is int]
            if rows != [expected]:
                refusals.append(('subsystem_version', key))
        elif any(t in tables for t in GUARDED[key]):
            refusals.append(('missing_runtime_version', key))
    if 'search_index_meta' in tables:
        try:
            row = db.execute(f"SELECT value FROM {prefix}.search_index_meta WHERE key='schema'").fetchone()
        except sqlite3.OperationalError:
            row = None
        value = row[0] if row else None
        found['search_index_meta'] = [value] if type(value) is int else []
        if type(value) is int and value > SEARCH_SCHEMA:
            refusals.append(('subsystem_version', 'search_index_meta'))
    return found, refusals


def _history(db, schema):
    if 'upgrade_history' not in schema['tables']:
        return set()
    try:
        return {r[0] for r in db.execute('SELECT backup_id FROM main.upgrade_history WHERE backup_id IS NOT NULL')}
    except sqlite3.OperationalError:
        return set()


# Logical summaries: counts and digests, never values

def _digest(db, prefix, name, timer):
    source = f'{prefix}.{_quote(name)}'
    try:
        cursor = db.execute(f'SELECT * FROM {source} ORDER BY rowid')
    except sqlite3.OperationalError as exc:
        if 'rowid' not in str(exc).lower():
            raise
        cursor = db.execute(f'SELECT * FROM {source}')  # WITHOUT ROWID scans in key order
    digest, rows = hashlib.sha256(), 0
    for row in cursor:
        rows += 1
        digest.update(b'R')
        for value in row:
            digest.update(_encode(value))
        if not rows % 4096:
            timer.check()
    return {'rows': rows, 'sha256': digest.hexdigest()}


def _layers(db, schema, prefix='main'):
    if 'memories' not in schema['tables']:
        return {}
    counts, other = dict.fromkeys(KINDS, 0), 0
    for kind, count in db.execute(f'SELECT kind,count(*) FROM {prefix}.memories GROUP BY kind'):
        if kind in counts:
            counts[kind] = count
        else:
            other += count
    if other:
        counts['other'] = other
    return counts


def _logical(db, schema, reference, timer, prefix='main'):
    tables = {}
    for key in sorted(schema['tables']):
        table = schema['tables'][key]
        if table['virtual'] and key not in reference['tables']:
            continue  # its module may be unavailable; its shadow tables are ordinary tables
        tables[_label(key, reference['tables'])] = _digest(db, prefix, table['name'], timer)
    master = hashlib.sha256()
    for row in db.execute(f'SELECT type,name,tbl_name,sql FROM {prefix}.sqlite_master ORDER BY type,name'):
        for value in row:
            master.update(_encode(value))
    return {'tables': tables, 'memory_layers': _layers(db, schema, prefix), 'schema_sha256': master.hexdigest()}


# Backup files

def _publish(directory, name, value):
    """Durably create `name` holding `value`; an existing path is never replaced."""
    data = _canonical(value).encode()
    temp = '.tmp-' + secrets.token_hex(8) + '.json'
    fd, made = directory.create(temp)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
            os.fchmod(fd, 0o400)
        finally:
            os.close(fd)
        directory.link(temp, name)
    finally:
        directory.remove(temp, made)
    directory.sync()
    now = directory.stat(name)
    if now is None or _ident(now) != _ident(made) or now.st_nlink != 1:
        raise MigrationRefused('backup_failed')
    return hashlib.sha256(data).hexdigest()


def _partial(info):
    # A crash between link(2) and removing the temporary name leaves a second link.
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def _inventory(backups):
    """Published names must be private single-link regular files; other names are only counted."""
    entries, partials, unknown = {}, [], 0
    for name in backups.names():
        info = backups.stat(name)
        if info is None:
            continue
        match = ENTRY.fullmatch(name)
        if match:
            if not _kept(info):
                raise MigrationRefused('backup_tampered')
            entries.setdefault(match[1], {})[match[2]] = info
        elif PARTIAL.fullmatch(name):
            if not _partial(info):
                raise MigrationRefused('backup_tampered')
            partials.append((name, info))
        else:
            unknown += 1
    return entries, partials, unknown


def _sweep(backups):
    """Under the upgrade lock, unpublished partial files can only belong to an interrupted run.

    Removing them first also restores the single link of a file published just
    before the interruption.
    """
    removed = 0
    for name in backups.names():
        info = backups.stat(name) if PARTIAL.fullmatch(name) else None
        if info is not None:
            if not _partial(info):
                raise MigrationRefused('backup_tampered')
            removed += backups.remove(name, info)
    if removed:
        backups.sync()
    return removed


def _shape(manifest):
    try:
        return (type(manifest['from_version']) is int and type(manifest['to_version']) is int
                and type(manifest['source']['device']) is int and type(manifest['source']['inode']) is int
                and type(manifest['backup']['bytes']) is int and isinstance(manifest['backup']['sha256'], str)
                and isinstance(manifest['logical'], dict))
    except (KeyError, TypeError):
        return False


def _backups(backups, entries):
    """Per-backup state. pending: verified manifest and data, no receipt; completed: valid receipt."""
    items = {}
    for bid, files in sorted(entries.items()):
        item = {'id': bid, 'files': files, 'manifest': None, 'receipt': None, 'digests': {},
                'state': 'invalid' if 'manifest.json' in files else 'orphan' if 'sqlite' in files else 'stray'}
        raw = {}
        for part in ('manifest.json', 'complete.json'):
            if part in files:
                item['digests'][part], _, raw[part] = backups.read(f'{bid}.{part}', files[part], limit=MAX_RECORD)
        manifest = _unseal(raw.get('manifest.json', b''), 'manifest_sha256', MANIFEST_FORMAT)
        receipt = _unseal(raw.get('complete.json', b''), 'receipt_sha256', RECEIPT_FORMAT)
        if manifest and manifest.get('id') == bid and _shape(manifest):
            item['manifest'] = manifest
            if 'complete.json' in files:
                if (receipt and receipt.get('id') == bid
                        and receipt.get('manifest_sha256') == item['digests']['manifest.json']):
                    item.update(receipt=receipt, state='completed')
            elif 'sqlite' in files and files['sqlite'].st_size == manifest['backup']['bytes']:
                item['state'] = 'pending'
        items[bid] = item
    return items


def _unsettled(items):
    return [i for i in items.values() if i['state'] == 'pending' or i['state'] == 'invalid'
            and 'manifest.json' in i['files']]


def _verify_data(backups, item, timer):
    files, expected = item['files'], item['manifest']['backup']
    if 'sqlite' not in files:
        raise MigrationRefused('backup_tampered')
    digest, size, _ = backups.read(item['id'] + '.sqlite', files['sqlite'], timer)
    if size != expected['bytes'] or digest != expected['sha256']:
        raise MigrationRefused('backup_tampered')


def _receipt(backups, item, verification):
    receipt = _seal({'format': RECEIPT_FORMAT, 'id': item['id'], 'manifest_sha256': item['digests']['manifest.json'],
                     'completed': _now(), 'from_version': item['manifest']['from_version'],
                     'to_version': SCHEMA_EPOCH, 'verification': verification}, 'receipt_sha256')
    try:
        _publish(backups, item['id'] + '.complete.json', receipt)
    except (OSError, MigrationRefused):
        raise MigrationRefused('receipt_failed') from None


# Preflight

class Upgrade:
    """Ownership of one startup's upgrade, from preflight until completion or abort.

    state: fresh (no existing data), current (already this epoch), backed_up (a
    new verified backup) or resumed (the backup of an interrupted startup).
    """

    def __init__(self, root, lock, limits, clock, state, version, identity, backup=None, cleaned=0, recovered=0):
        self._root, self._lock = root, lock
        self.limits, self.clock = limits, clock
        self.state, self.version, self.identity = state, version, identity
        self.backup, self.cleaned, self.recovered = backup, cleaned, recovered
        self.completed = False

    def summary(self):
        return {'state': self.state, 'schema_epoch': SCHEMA_EPOCH, 'from_version': self.version,
                'backup_id': self.backup, 'completed': self.completed,
                'removed_partials': self.cleaned, 'recovered_receipts': self.recovered}

    def complete(self):
        """After every subsystem constructor, before workers start. Refuses instead of stamping."""
        if self._root is None:
            raise MigrationRefused('not_prepared')
        try:
            verification = _complete(self)
        except OSError:
            raise MigrationRefused('io_error') from None
        finally:
            self.abort()
        self.completed = True
        return {**self.summary(), 'verification': verification}

    def abort(self):
        """Release ownership without marking anything; safe to repeat."""
        lock, root, self._lock, self._root = self._lock, self._root, None, None
        if lock is not None:
            os.close(lock)
        if root is not None:
            root.close()


def prepare(home, limits=None, *, clock=time.monotonic):
    """Preflight before Store opens `home`. Returns the Upgrade to complete or abort.

    The database is only read. Every refusal happens here, before any mutation;
    only an existing database that needs an upgrade receives a backup.
    """
    limits = limits or Limits()
    _platform()
    timer = _Timer(limits.deadline, clock)
    timer.check()
    home = Path(home)
    if not os.path.lexists(home):
        home.mkdir(parents=True, exist_ok=True, mode=0o700)  # what Store does next anyway
    root = _Directory.open(home, 0o022)
    if root is None:
        raise MigrationRefused('unsafe_path')
    lock = None
    try:
        lock = _lock(root, limits)
        upgrade = _preflight(root, lock, limits, clock, timer)
        root = lock = None
        return upgrade
    except OSError:
        raise MigrationRefused('io_error') from None
    finally:
        if lock is not None:
            os.close(lock)
        if root is not None:
            root.close()


def _preflight(root, lock, limits, clock, timer):
    info = _database(root)
    backups = root.child(BACKUPS, 0o077)
    try:
        cleaned = _sweep(backups) if backups else 0
        if info is None:
            return Upgrade(root, lock, limits, clock, 'fresh', 0, None, cleaned=cleaned)
        root.check()
        with _reader(root.path / DATABASE, timer, limits) as db:
            db.execute('BEGIN')  # one read snapshot for every check and the backup
            version = db.execute('PRAGMA main.user_version').fetchone()[0]
            if version > SCHEMA_EPOCH:
                raise MigrationRefused('future_version')
            if version < 0:
                raise MigrationRefused('unknown_version')
            schema = _schema(db)
            if not version and not schema['tables'] and not schema['views']:
                state, backup, recovered = 'fresh', None, 0
            else:
                state, backup, recovered = _decide(db, root, backups, info, version, schema, limits, timer)
            db.execute('ROLLBACK')
        _same(root, info)
        return Upgrade(root, lock, limits, clock, state, version, _ident(info), backup, cleaned, recovered)
    finally:
        if backups is not None:
            backups.close()


def _decide(db, root, backups, info, version, schema, limits, timer):
    reference = _reference()
    compat = _compatibility(schema, reference)
    versions, problems = _versions(db, schema)
    refusals = compat['refusals'] + problems
    if refusals:
        raise MigrationRefused(*refusals[0])
    current = not compat['missing_tables'] and not compat['missing_columns'] and 'upgrade_history' in schema['tables']
    items, recovered = {}, 0
    if backups:
        items = _backups(backups, _inventory(backups)[0])
        history = _history(db, schema)
        for item in items.values():
            # Stamped in the database but the receipt was lost: completion already verified it.
            if item['state'] == 'pending' and item['id'] in history:
                _receipt(backups, item, {'recovered': 'upgrade_history'})
                item['state'], recovered = 'completed', recovered + 1
    unsettled = _unsettled(items)
    if any(item['state'] == 'invalid' for item in unsettled):
        raise MigrationRefused('pending_backup_invalid')
    if len(unsettled) > 1:
        raise MigrationRefused('pending_backup_ambiguous')
    if unsettled:
        item = unsettled[0]
        manifest = item['manifest']
        if ((manifest['source']['device'], manifest['source']['inode']) != _ident(info)
                or manifest['from_version'] != version or manifest['to_version'] > SCHEMA_EPOCH):
            raise MigrationRefused('pending_backup_mismatch')
        try:
            _verify_data(backups, item, timer)
        except MigrationRefused:
            raise MigrationRefused('pending_backup_invalid') from None
        # An interrupted startup reuses its pristine backup; nothing is copied again.
        return 'resumed', item['id'], recovered
    if version == SCHEMA_EPOCH and current:
        return 'current', None, recovered
    backup = _backup(db, root, backups, info, version, schema, reference, compat, versions, items, limits, timer)
    return 'backed_up', backup, recovered


def _backup(db, root, backups, info, version, schema, reference, compat, versions, items, limits, timer):
    page_size = db.execute('PRAGMA main.page_size').fetchone()[0]
    pages = db.execute('PRAGMA main.page_count').fetchone()[0]
    size = page_size * pages
    if size > limits.max_database_bytes:
        raise MigrationRefused('database_too_large')
    count = sum('sqlite' in item['files'] for item in items.values())
    used = sum(f.st_size for item in items.values() for f in item['files'].values())
    if count + 1 > limits.max_backups or used + size > limits.max_backup_bytes:
        raise MigrationRefused('backup_budget_exhausted')
    space = os.fstatvfs((backups or root).fd)
    if space.f_bavail * space.f_frsize < size + limits.free_reserve:
        raise MigrationRefused('insufficient_space')
    logical = _logical(db, schema, reference, timer)
    created = backups is None
    target = root.child(BACKUPS, 0o077, create=True) if created else backups
    try:
        source = {'device': info.st_dev, 'inode': info.st_ino, 'page_size': page_size, 'page_count': pages}
        summary = {'missing_tables': len(compat['missing_tables']),
                   'missing_columns': sum(map(len, compat['missing_columns'].values())),
                   'unknown_tables': len(compat['unknown_tables']), 'unknown_indexes': compat['unknown_indexes'],
                   'views': compat['views']}
        return _write(db, target, version, source, logical, summary, versions, reference, limits, timer)
    except BaseException:
        if created:
            try:
                os.rmdir(BACKUPS, dir_fd=root.fd)  # only ever removes our own empty directory
            except OSError:
                pass
        raise
    finally:
        if created:
            target.close()


def _write(db, backups, version, source, logical, summary, versions, reference, limits, timer):
    bid = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + secrets.token_hex(4)
    temp = '.tmp-' + secrets.token_hex(8) + '.sqlite'
    fd = made = published = None
    try:
        fd, made = backups.create(temp)
        backups.check()
        copy = sqlite3.connect(backups.path / temp, isolation_level=None)
        try:
            with _errors(timer):
                # The source holds one read snapshot, so every step copies the same state.
                db.backup(copy, pages=limits.step_pages, progress=timer.check)
                # A self-contained rollback-journal file: no WAL sidecar is needed to read it.
                mode = copy.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
        finally:
            copy.close()
        for suffix in SIDECARS:
            side = backups.stat(temp + suffix)
            if side is not None:
                backups.remove(temp + suffix, side)
        now = backups.stat(temp)
        if str(mode).lower() != 'delete' or now is None or _ident(now) != _ident(made) or now.st_nlink != 1:
            raise MigrationRefused('backup_failed')
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        os.close(fd)
        fd, made = None, backups.stat(temp)
        with _reader(backups.path / temp, timer, limits, immutable=True) as copy:
            if copy.execute('PRAGMA integrity_check(1)').fetchone()[0] != 'ok':
                raise MigrationRefused('corrupt')
            if _logical(copy, _schema(copy), reference, timer) != logical:
                raise MigrationRefused('backup_failed')
        digest, size, _ = backups.read(temp, made, timer)
        manifest = _seal({
            'format': MANIFEST_FORMAT, 'id': bid, 'created': _now(), 'purpose': 'pre_upgrade',
            'from_version': version, 'to_version': SCHEMA_EPOCH, 'source': source,
            'backup': {'file': bid + '.sqlite', 'bytes': size, 'sha256': digest, 'journal_mode': 'delete'},
            'checks': {'integrity': 'ok', 'snapshot_match': True, 'method': 'sqlite_online_backup'},
            'logical': logical, 'compatibility': summary, 'versions': versions,
            'credentials': CREDENTIALS}, 'manifest_sha256')
        backups.link(temp, bid + '.sqlite')
        published = bid + '.sqlite'
        backups.remove(temp, made)
        _publish(backups, bid + '.manifest.json', manifest)
        return bid
    except BaseException as exc:
        # Only this call's own files are removed, and only while they are what it created.
        for name in (published, temp):
            if name and made is not None:
                try:
                    backups.remove(name, made)
                except OSError:
                    pass
        for suffix in SIDECARS:
            side = backups.stat(temp + suffix)
            if side is not None:
                try:
                    backups.remove(temp + suffix, side)
                except OSError:
                    pass
        if isinstance(exc, (OSError, sqlite3.Error)):
            raise MigrationRefused('backup_failed') from None
        raise
    finally:
        if fd is not None:
            os.close(fd)


# Completion

def _complete(upgrade):
    root, limits = upgrade._root, upgrade.limits
    timer = _Timer(limits.deadline, upgrade.clock)
    timer.check()
    root.check()
    info = _database(root)
    if info is None:
        raise MigrationRefused('schema_incomplete')
    if upgrade.identity is not None and _ident(info) != upgrade.identity:
        raise MigrationRefused('unsafe_path', 'database replaced')
    backups = item = None
    try:
        if upgrade.backup:
            backups = root.child(BACKUPS, 0o077)
            item = _backups(backups, _inventory(backups)[0]).get(upgrade.backup) if backups else None
            if not item or item['state'] != 'pending':
                raise MigrationRefused('backup_tampered')
            _verify_data(backups, item, timer)
            backups.check()
        with _reader(root.path / DATABASE, timer, limits) as db:
            if item:
                db.execute('ATTACH DATABASE ? AS b', (_uri(backups.path / (item['id'] + '.sqlite'), immutable=True),))
            db.execute('BEGIN')
            version = db.execute('PRAGMA main.user_version').fetchone()[0]
            if version not in (upgrade.version, SCHEMA_EPOCH):
                raise MigrationRefused('unknown_version')
            schema = _schema(db)
            reference = _reference()
            compat = _compatibility(schema, reference)
            refusals = compat['refusals'] + _versions(db, schema)[1]
            if refusals:
                raise MigrationRefused(*refusals[0])
            if compat['missing_tables'] or compat['missing_columns']:
                raise MigrationRefused('schema_incomplete')
            verification = {'schema': 'current'}
            if upgrade.state != 'current':
                if db.execute('PRAGMA main.integrity_check(1)').fetchone()[0] != 'ok':
                    raise MigrationRefused('corrupt')
                verification['integrity'] = 'ok'
            if item:
                verification.update(_preserved(db, schema, reference, timer))
            db.execute('ROLLBACK')
        _same(root, info)
        if upgrade.state != 'current':
            _stamp(root, info, timer, limits, upgrade.backup, upgrade.version)
            if item:
                _receipt(backups, item, verification)
        upgrade.identity = _ident(info)
        return verification
    finally:
        if backups is not None:
            backups.close()


def _preserved(db, schema, reference, timer):
    """Compare the live database with the attached backup `b`; returns counts only."""
    old = {r[0].lower(): (r[0], _normal(r[1])) for r in db.execute(
        "SELECT name,sql FROM b.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~'")}
    known, now = reference['tables'], time.time()
    lost = {_label(key, known) for key in old if key not in schema['tables']}
    missing, changed, rows, checked = {}, {}, 0, 0
    for key, keep in PRESERVED.items():
        if key not in old or key not in schema['tables']:
            continue
        columns = {r[0].lower() for r in db.execute('SELECT name FROM pragma_table_xinfo(?,?)', (old[key][0], 'b'))}
        match = ' AND '.join(f'n.{c} = o.{c}' for c in keep.keys)
        same = ' AND '.join(f'n.{c} IS o.{c}' for c in keep.stable if c in columns) or '1'
        if keep.exempt:
            same = f'({keep.exempt}) OR ({same})'
        condition = f'({same})' + (f' AND ({keep.transition})' if keep.transition else '')
        gone = f'SELECT count(*) FROM b.{key} o WHERE NOT EXISTS(SELECT 1 FROM main.{key} n WHERE {match})'
        if keep.missing:
            gone += f' AND NOT ({keep.missing})'
        rows += db.execute(f'SELECT count(*) FROM b.{key}').fetchone()[0]
        checked += 1
        count = db.execute(gone, {'now': now} if keep.missing else ()).fetchone()[0]
        if count:
            missing[key] = count
        count = db.execute(f'SELECT count(*) FROM b.{key} o JOIN main.{key} n ON {match} WHERE NOT ({condition})'
                           ).fetchone()[0]
        if count:
            changed[key] = count
    verifiers = (('settings', _settings), ('run_tools', _tool_changes), ('agent_effects', _effect_changes))
    for key, verify in verifiers:
        if key in old and key in schema['tables']:
            count = verify(db)
            if count:
                changed[key] = changed.get(key, 0) + count
    for key in NO_NEW:
        if key not in schema['tables']:
            continue
        match = ' AND '.join(f'n.{c} = o.{c}' for c in PRESERVED[key].keys)
        count = db.execute(f'SELECT count(*) FROM main.{key} n' + (
            f' WHERE NOT EXISTS(SELECT 1 FROM b.{key} o WHERE {match})' if key in old else '')).fetchone()[0]
        if count:
            changed[key] = changed.get(key, 0) + count
    retained = 0
    for key, (name, normal) in old.items():
        if key in known or key not in schema['tables'] or normal.startswith('createvirtualtable'):
            continue
        # Unknown data is retained untouched, row for row.
        if _digest(db, 'b', name, timer) != _digest(db, 'main', schema['tables'][key]['name'], timer):
            changed[_label(key, known)] = 1
        else:
            retained += 1
    if lost or missing or changed:
        raise MigrationRefused('preservation_failed', ', '.join(sorted(lost | set(missing) | set(changed))))
    return {'preserved_tables': checked, 'preserved_rows': rows, 'unknown_tables_retained': retained}


def _json(raw):
    try:
        return json.loads(raw) if isinstance(raw, (str, bytes)) else None
    except ValueError:
        return None


def _settings(db):
    """Settings rows startup rewrote or added beyond App's own exact normalization; a count."""
    from .permissions import DEFAULT
    bad = 0
    for key, old, new in db.execute("SELECT o.key,o.value,n.value FROM b.settings o JOIN main.settings n "
                                    "ON n.key=o.key WHERE o.key IN ('permissions','model_profiles')"):
        if old != new:
            bad += not (_permissions(_json(old), _json(new), DEFAULT) if key == 'permissions'
                        else _profiles(_json(old), _json(new)))
    for key, value in db.execute('SELECT n.key,n.value FROM main.settings n '
                                 'WHERE NOT EXISTS(SELECT 1 FROM b.settings o WHERE o.key = n.key)'):
        # App writes the default policy and the memory marker when absent; nothing else appears.
        allowed = dict(ADDED_SETTINGS, permissions=DEFAULT)
        bad += key not in allowed or _json(value) != allowed[key] or type(_json(value)) is not type(allowed[key])
    return bad


def _permissions(old, new, default):
    """App's merge only: the earlier mode and every earlier tool flag, new required tools at their defaults
    (an omitted optional group already means off and is never written)."""
    from .permissions import OPTIONAL_TOOLS
    if not isinstance(old, dict) or 'mode' not in old or not isinstance(old.get('tools'), dict):
        return False
    required = {k: v for k, v in default['tools'].items() if k not in OPTIONAL_TOOLS}
    return new == {'mode': old['mode'], 'tools': {**required, **old['tools']}}


def _profiles(old, new):
    """Same profiles in the same order, each unchanged or exactly as Providers.profiles migrates it."""
    if not isinstance(old, list) or not isinstance(new, list) or len(old) != len(new):
        return False
    if not all(isinstance(p, dict) for p in old + new):
        return False
    ids = [_canonical(p.get('id')) for p in new]
    if len(set(ids)) != len(ids):
        return False
    return all(n == o or n == _profile_migration(o) for o, n in zip(old, new))


def _profile_migration(profile):
    """Replays Providers.profiles' in-place migration of one stored profile; None if it would fail.

    Only context budget fields and the derived connection identity (the old id and
    name) may change. Route, account, credentials state and capabilities never do.
    """
    from .providers import model_capacity
    try:
        p = json.loads(_canonical(profile))
        if p.get('kind') != 'gguf' and not p.get('budget_version'):
            p['context_mode'], p['budget'], p['budget_version'] = 'full', p['context'], 2
        if p.get('kind') != 'gguf' and not p.get('connection_id'):
            p['connection_id'], p['provider_name'] = p['id'], p['name']
        capacity = model_capacity(p.get('url', ''), p.get('model', ''))
        if capacity and not p.get('context_version'):
            if p.get('context') == 131072:
                p['context'] = capacity
            p['context_version'] = 1
        if p.get('context_mode') == 'full' and p.get('budget') != p['context']:
            p['budget'] = p['context']
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return p


def _tool_changes(db):
    """Run receipts startup changed without an exact pre-existing basis; a count."""
    from .runtime import encoded
    bad = 0
    for old_state, old_result, new_state, new_result in db.execute(
            'SELECT o.state,o.result,n.state,n.result FROM b.run_tools o JOIN main.run_tools n '
            'ON n.run_id=o.run_id AND n.call_id=o.call_id WHERE n.state IS NOT o.state OR n.result IS NOT o.result'
            ).fetchall():
        bad += not _tool_recovered(db, encoded, old_state, old_result, new_state, new_result)
    return bad


def _tool_recovered(db, encoded, old_state, old_result, new_state, new_result):
    if old_state == 'running' and new_state == 'uncertain' and new_result == old_result:
        return True  # RunJournal.recover: an unacknowledged effect stays unknown, never replayed
    old, new = _json(old_result), _json(new_result)
    action = old.get('action_id') if isinstance(old, dict) else None
    before = action and db.execute('SELECT status,payload,kind FROM b.actions WHERE id=?', (action,)).fetchone()
    if not before or not isinstance(new, dict):
        return False
    if old_state == 'running' and new_state == 'completed':
        # RunJournal.recover reads back a broker action that was already applied before the upgrade.
        payload = _json(before[1])
        result = payload.get('result') if isinstance(payload, dict) else None
        return before[0] == 'applied' and new_result == encoded({'action_id': action, 'status': 'applied', 'result': result})
    if old_state == new_state == 'completed':
        # AgentEffects revokes a still-pending action of a closed requester: receipt fields only.
        fields = {'action_id', 'kind', 'status', 'revoked', 'request_id', 'reason'}
        after = db.execute('SELECT status FROM main.actions WHERE id=?', (action,)).fetchone()
        return (before[0] == 'pending' and after == ('rejected',) and new.get('status') == 'rejected'
                and new.get('revoked') is True and new.get('action_id') == action and new.get('kind') == before[2]
                and {k: v for k, v in new.items() if k not in fields} == {k: v for k, v in old.items() if k not in fields})
    return False


def _effect_changes(db):
    """Agent effect outcomes startup changed beyond AgentEffects' own recovery; a count."""
    from .agent_effects import AgentEffects
    from .runtime import encoded

    def outcome(result):
        return encoded(AgentEffects._bounded(result, 16000), 64000)

    bad = 0
    for row in db.execute(
            'SELECT o.status,o.action_id,o.outcome,o.chat_id,o.run_id,o.root_call_id,o.effect_call_id,'
            'n.status,n.action_id,n.outcome FROM b.agent_effects o JOIN main.agent_effects n ON n.id=o.id '
            'WHERE n.status IS NOT o.status OR n.action_id IS NOT o.action_id OR n.outcome IS NOT o.outcome').fetchall():
        bad += not _effect_recovered(db, outcome, *row)
    return bad


def _effect_recovered(db, outcome, status, action, result, chat, run, root_call, effect_call,
                      new_status, new_action, new_result):
    if new_status == status or new_status not in EFFECT_RECOVERY.get(status, ()):
        return False  # an outcome never changes without its own recovery transition
    if action is not None and new_action != action:
        return False
    if new_action is not None and new_action != action or new_status in ('applied', 'rejected'):
        before = db.execute('SELECT status,payload,chat_id FROM b.actions WHERE id=?', (new_action,)).fetchone()
        if before is None or before[2] != chat:
            return False
        if new_status == 'applied':
            # Only an action applied before the upgrade, with exactly its recorded result.
            payload = _json(before[1])
            return before[0] == 'applied' and new_result == outcome(payload.get('result') if isinstance(payload, dict) else None)
        if new_status == 'rejected':
            return before[0] in ('rejected', 'superseded')
    if new_status == 'user_reported':
        # Still unverified: it must restate an existing root receipt of this request exactly.
        receipts = db.execute("SELECT result FROM b.run_tools WHERE run_id=? AND call_id IN (?,?) AND state='completed'",
                              (run, root_call, effect_call)).fetchall()
        return any(isinstance(r, dict) and r.get('status') == 'user_reported' and new_result == outcome(r)
                   for r in (_json(row[0]) for row in receipts))
    return True  # proposed, invalidated, uncertain, failed, revoked and awaiting_approval assert no success


def _stamp(root, info, timer, limits, backup, version):
    """One transaction: the history row and the epoch become visible together."""
    root.check()
    _same(root, info)
    db = sqlite3.connect(root.path / DATABASE, isolation_level=None, timeout=max(0.0, min(limits.busy, timer.left())))
    try:
        db.set_progress_handler(timer.handler, 1000)
        with _errors(timer):
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute(HISTORY_DDL)
                db.execute('INSERT INTO upgrade_history(backup_id,from_version,to_version,completed) VALUES(?,?,?,?)',
                           (backup, version, SCHEMA_EPOCH, time.time()))
                db.execute(f'PRAGMA user_version={int(SCHEMA_EPOCH)}')
                db.execute('COMMIT')
            except BaseException:
                if db.in_transaction:
                    db.execute('ROLLBACK')
                raise
    finally:
        db.close()
    _same(root, info)


# Validation API: counts, hashes, versions and states only

def inspect(home, limits=None, *, deep=False, clock=time.monotonic):
    """Read-only view of a data directory. Creates nothing and takes no ownership."""
    limits = limits or Limits()
    timer = _Timer(limits.deadline, clock)
    report = {'format': INSPECTION_FORMAT, 'schema_epoch': SCHEMA_EPOCH, 'state': 'absent',
              'database': {'exists': False}, 'backups': [], 'budget': None}
    if not SUPPORTED:
        report['state'] = 'unsupported'
        return report
    try:
        root = _Directory.open(Path(home), 0o022)
    except MigrationRefused as exc:
        report.update(state='refused', refusal=exc.reason)
        return report
    if root is None:
        return report
    items = {}
    try:
        backups = root.child(BACKUPS, 0o077)
        if backups is not None:
            try:
                entries, partials, unknown = _inventory(backups)
                items = _backups(backups, entries)
                report['backups'] = [_describe(backups, item, timer, deep) for item in items.values()]
                report['partials'], report['unrecognized_entries'] = len(partials), unknown
            finally:
                backups.close()
        count = sum('sqlite' in item['files'] for item in items.values())
        used = sum(f.st_size for item in items.values() for f in item['files'].values())
        report['budget'] = {'backups': count, 'bytes': used, 'max_backups': limits.max_backups,
                            'max_bytes': limits.max_backup_bytes,
                            'exhausted': count >= limits.max_backups or used >= limits.max_backup_bytes}
        info = _database(root)
        if info is not None:
            report['database'] = _describe_database(root, info, limits, timer, deep)
    except MigrationRefused as exc:
        report.update(state='refused', refusal=exc.reason)
        return report
    finally:
        root.close()
    database = report['database']
    report['state'] = ('absent' if not database['exists'] else 'refused' if database.get('refusal')
                       else 'upgrade_pending' if _unsettled(items) else 'fresh' if database.get('fresh')
                       else 'current' if database.get('current') else 'upgrade_required')
    return report


def _describe(backups, item, timer, deep):
    manifest = item['manifest'] or {}
    result = {'id': item['id'], 'state': item['state'], 'data_present': 'sqlite' in item['files'],
              'bytes': sum(f.st_size for f in item['files'].values()),
              'from_version': manifest.get('from_version'), 'to_version': manifest.get('to_version'),
              'created': manifest.get('created'), 'sha256': (manifest.get('backup') or {}).get('sha256'),
              'manifest_sha256': item['digests'].get('manifest.json')}
    if deep and 'sqlite' in item['files']:
        if item['manifest']:
            try:
                _verify_data(backups, item, timer)
                result['verified'] = True
            except MigrationRefused:
                result['verified'] = False
        else:
            result['data_sha256'] = backups.read(item['id'] + '.sqlite', item['files']['sqlite'], timer)[0]
    return result


def _describe_database(root, info, limits, timer, deep):
    wal = root.stat(DATABASE + '-wal')
    result = {'exists': True, 'bytes': info.st_size + (wal.st_size if wal else 0)}
    try:
        root.check()
        with _reader(root.path / DATABASE, timer, limits) as db:
            db.execute('BEGIN')
            version = db.execute('PRAGMA main.user_version').fetchone()[0]
            schema = _schema(db)
            reference = _reference()
            compat = _compatibility(schema, reference)
            versions, problems = _versions(db, schema)
            refusals = ([('future_version', None)] if version > SCHEMA_EPOCH else
                        [('unknown_version', None)] if version < 0 else []) + compat['refusals'] + problems
            rows = {}
            for key in sorted(schema['tables']):
                table = schema['tables'][key]
                if key in reference['shadows'] or table['virtual'] and key not in reference['tables']:
                    continue
                rows[_label(key, reference['tables'])] = db.execute(
                    f'SELECT count(*) FROM main.{_quote(table["name"])}').fetchone()[0]
            result.update(
                user_version=version, versions=versions, rows=rows,
                page_size=db.execute('PRAGMA main.page_size').fetchone()[0],
                page_count=db.execute('PRAGMA main.page_count').fetchone()[0],
                tables={'known': sum(k in reference['tables'] for k in schema['tables']),
                        'unknown': len(compat['unknown_tables']), 'missing': len(compat['missing_tables']),
                        'missing_columns': sum(map(len, compat['missing_columns'].values())),
                        'unknown_indexes': compat['unknown_indexes'], 'views': compat['views']},
                memory_layers=None if refusals else _layers(db, schema),
                upgrade_history=len(_history(db, schema)) if not refusals else None,
                fresh=not version and not schema['tables'] and not schema['views'],
                current=(not refusals and version == SCHEMA_EPOCH and not compat['missing_tables']
                         and not compat['missing_columns'] and 'upgrade_history' in schema['tables']),
                refusal=refusals[0][0] if refusals else None,
                refusal_object=refusals[0][1] if refusals else None)
            if deep:
                result['integrity'] = 'ok' if db.execute('PRAGMA main.integrity_check(1)').fetchone()[0] == 'ok' else 'failed'
            db.execute('ROLLBACK')
    except MigrationRefused as exc:
        result['refusal'] = exc.reason
    return result


def verify_backup(home, backup_id, limits=None, *, clock=time.monotonic):
    """Deep check of one backup: file hash, SQLite integrity and its recorded logical summary."""
    limits = limits or Limits()
    timer = _Timer(limits.deadline, clock)
    try:
        _platform()
        root, backups, item = _locate(home, backup_id)
        try:
            if not item['manifest']:
                raise MigrationRefused('backup_tampered')
            _verify_data(backups, item, timer)
            backups.check()
            with _reader(backups.path / (backup_id + '.sqlite'), timer, limits, immutable=True) as copy:
                if copy.execute('PRAGMA integrity_check(1)').fetchone()[0] != 'ok':
                    raise MigrationRefused('corrupt')
                logical = _logical(copy, _schema(copy), _reference(), timer)
        finally:
            backups.close()
            root.close()
    except MigrationRefused as exc:
        return {'id': backup_id, 'valid': False, 'reason': exc.reason}
    match = logical == item['manifest']['logical']
    return {'id': backup_id, 'valid': match, 'reason': None if match else 'backup_tampered', 'state': item['state'],
            'integrity': 'ok', 'logical_match': match, 'tables': len(logical['tables']),
            'rows': sum(t['rows'] for t in logical['tables'].values()), 'memory_layers': logical['memory_layers'],
            'sha256': item['manifest']['backup']['sha256']}


def _locate(home, backup_id):
    if not isinstance(backup_id, str) or not BACKUP_ID.fullmatch(backup_id):
        raise MigrationRefused('backup_missing')
    root = _Directory.open(Path(home), 0o022)
    if root is None:
        raise MigrationRefused('backup_missing')
    try:
        backups = root.child(BACKUPS, 0o077)
        if backups is None:
            raise MigrationRefused('backup_missing')
        try:
            item = _backups(backups, _inventory(backups)[0]).get(backup_id)
            if item is None:
                raise MigrationRefused('backup_missing')
        except BaseException:
            backups.close()
            raise
    except BaseException:
        root.close()
        raise
    return root, backups, item


def restore_copy(home, backup_id, target, limits=None, *, clock=time.monotonic):
    """Offline verification: restore one backup into a NEW directory and check the copy.

    The live home is only read. There is no hot restore and no credential is
    involved. The copy is a disposable home: starting Pebble on it upgrades it
    like any other legacy database.
    """
    limits = limits or Limits()
    _platform()
    timer = _Timer(limits.deadline, clock)
    target = Path(target)
    live = os.path.realpath(home)
    if target.name in ('', '.', '..') or os.path.lexists(target):
        raise MigrationRefused('restore_target')
    if os.path.commonpath([live, os.path.realpath(target.parent)]) == live:
        raise MigrationRefused('restore_target')
    root, backups, item = _locate(home, backup_id)
    parent = destination = None
    try:
        if not item['manifest']:
            raise MigrationRefused('backup_tampered')
        _verify_data(backups, item, timer)
        parent = _Directory.open(target.parent, 0o022)
        if parent is None:
            raise MigrationRefused('restore_target')
        try:
            os.mkdir(target.name, 0o700, dir_fd=parent.fd)  # EEXIST: never an existing directory
        except OSError:
            raise MigrationRefused('restore_target') from None
        destination = parent.child(target.name, 0o077)
        fd, made = destination.create(DATABASE)
        os.close(fd)
        backups.check()
        destination.check()
        with _reader(backups.path / (backup_id + '.sqlite'), timer, limits, immutable=True) as source:
            copy = sqlite3.connect(destination.path / DATABASE, isolation_level=None)
            try:
                source.backup(copy, pages=limits.step_pages, progress=timer.check)
            finally:
                copy.close()
        now = destination.stat(DATABASE)
        if now is None or _ident(now) != _ident(made):
            raise MigrationRefused('restore_target')
        with _reader(destination.path / DATABASE, timer, limits) as copy:
            if copy.execute('PRAGMA integrity_check(1)').fetchone()[0] != 'ok':
                raise MigrationRefused('corrupt')
            version = copy.execute('PRAGMA main.user_version').fetchone()[0]
            schema = _schema(copy)
            logical = _logical(copy, schema, _reference(), timer)
            refusals = _compatibility(schema, _reference())['refusals'] + _versions(copy, schema)[1]
        if logical != item['manifest']['logical']:
            raise MigrationRefused('backup_tampered')
    finally:
        for directory in (destination, parent, backups, root):
            if directory is not None:
                directory.close()
    return {'restored': True, 'backup_id': backup_id, 'target_created': True, 'integrity': 'ok',
            'logical_match': True, 'tables': len(logical['tables']),
            'rows': sum(t['rows'] for t in logical['tables'].values()), 'memory_layers': logical['memory_layers'],
            'user_version': version, 'compatible': not refusals, 'credentials_restored': False,
            'credentials': CREDENTIALS, 'scope': SCOPE}


def discard_backup(home, backup_id, sha256, limits=None):
    """Explicit native removal of one settled backup, confirmed by its manifest hash.

    A pending upgrade keeps its backup. An orphan data file is confirmed by its
    own hash. Nothing else ever deletes a published backup.
    """
    limits = limits or Limits()
    _platform()
    if not isinstance(sha256, str):
        raise MigrationRefused('backup_tampered')
    timer = _Timer(limits.deadline, time.monotonic)
    root, backups, item = _locate(home, backup_id)
    lock = None
    try:
        lock = _lock(root, limits)
        item = _backups(backups, _inventory(backups)[0]).get(backup_id)  # again, under ownership
        if item is None:
            raise MigrationRefused('backup_missing')
        if item['state'] == 'pending':
            raise MigrationRefused('backup_pending')
        files = item['files']
        expected = (item['digests'].get('manifest.json')
                    or ('sqlite' in files and backups.read(backup_id + '.sqlite', files['sqlite'], timer)[0])
                    or item['digests'].get('complete.json') or '')
        if sha256 != expected:  # a confirmation, not a secret: a stale or wrong hash removes nothing
            raise MigrationRefused('backup_tampered')
        freed = 0
        # Data first: an interruption leaves a settled record, never a pending-looking one.
        for part in ('sqlite', 'manifest.json', 'complete.json'):
            if part in files and backups.remove(f'{backup_id}.{part}', files[part]):
                freed += files[part].st_size
        backups.sync()
        return {'discarded': backup_id, 'bytes': freed}
    finally:
        if lock is not None:
            os.close(lock)
        backups.close()
        root.close()
