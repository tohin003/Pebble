"""Compact tool evidence without breaking JSON or turning it into authority."""

import copy
import json


def focused_excerpt(text, max_chars, token_limit=None):
    """Exact Unicode prefix; a token estimate is a bound, not provider proof."""
    excerpt = text[:max_chars]
    if token_limit is not None:
        from .token_budget import TokenBudget
        encoding = TokenBudget.encoding()
        tokens = encoding.encode(excerpt, disallowed_special=())
        if len(tokens) > token_limit:
            excerpt = encoding.decode_bytes(tokens[:token_limit]).decode('utf-8', errors='ignore')
    return excerpt

METADATA = {
    "id",
    "action_id",
    "path",
    "file_path",
    "root",
    "url",
    "source",
    "kind",
    "status",
    "file_hash",
    "hash",
    "tool",
    "tool_call_id",
    'receipt_id',
    "name",
}


def compact(value, string_limit=2200, list_limit=12):
    changed = False

    def visit(item, key=""):
        nonlocal changed
        if isinstance(item, dict):
            return {k: visit(v, k) for k, v in item.items()}
        if isinstance(item, list):
            if len(item) > list_limit:
                changed = True
            return [visit(v, key) for v in item[:list_limit]]
        if isinstance(item, str) and key not in METADATA and len(item) > string_limit:
            changed = True
            text = item[:string_limit]
            if "\n" in text:
                text = text.rsplit("\n", 1)[0]
            return (
                text
                + "\n[Excerpt shortened; request a focused read for omitted content.]"
            )
        return item

    result = visit(value)
    if changed and isinstance(result, dict):
        result["context_note"] = (
            "Evidence shortened to fit context; metadata and execution status are preserved."
        )
    return result


def encode_result(value, limit=6500):
    if isinstance(value, dict) and any(key in value for key in ('cursor', 'next_char_offset', 'next_offset')):
        # Dropping rows while retaining the next-page pointer would skip data.
        # Keep the page whole or explicitly ask for a smaller original query.
        page = value
        if isinstance(value.get('coverage'), dict) and any(k in value for k in
                ('untrusted_matches', 'files', 'untrusted_results', 'untrusted_symbols')):
            # Performance/cache internals remain in the durable receipt/native
            # inspector. Keep the fields needed to reason about search absence.
            critical = {'scope', 'inventoried', 'searched', 'skipped', 'skip_reasons',
                'inventory_truncated', 'inventory_errors', 'scan_errors', 'entry_limit',
                'scan_complete', 'complete', 'match_limit', 'matches_returned', 'matches_truncated', 'staged',
                'ranking', 'path_filter', 'outside_filter', 'in_scope', 'path_only', 'file_size_limit',
                'python_files', 'python_parsed', 'python_partial', 'python_failed', 'python_deferred',
                'parse_issues', 'parse_budget', 'parse_complete', 'candidates', 'rank_limit',
                'ranked_truncated', 'drafts_overlaid', 'depth_limit', 'prefiltered', 'page_char_clipped',
                'tree_sitter'}  # Phase 15: why a ts/js/go/rust scope reports parse_complete false.
            page = {**value, 'coverage': {k: v for k, v in value['coverage'].items() if k in critical}}
        encoded = json.dumps(page, ensure_ascii=False, separators=(',', ':'))
        if len(encoded) <= limit:
            return encoded
        return json.dumps({'status': 'evidence_too_large', 'page_not_shown': True,
            'context_note': 'No page rows were shown. Repeat the ORIGINAL cursor/offset with a smaller limit. Do not advance to the returned next page.',
            **{k: value[k] for k in ('receipt_id', 'action_id') if k in value}}, ensure_ascii=False)
    result = copy.deepcopy(value)
    for size, items in ((3000, 20), (1800, 10), (900, 5), (400, 2)):
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= limit:
            return encoded
        result = compact(result, size, items)
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) <= limit:
        return encoded
    return json.dumps(
        {
            "context_note": "Tool evidence exceeds the context budget. Request a narrower query or source excerpt.",
            **({k: value[k] for k in ('receipt_id', 'action_id') if k in value} if isinstance(value, dict) else {}),
            "status": value.get("status", "evidence_too_large")
            if isinstance(value, dict)
            else "evidence_too_large",
        }
    )
