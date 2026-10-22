"""Normalize actual runtime/provider statistics; missing values stay unknown."""

import math
import sys


def usage_metrics(usage, timings, first, end):
    # Statistics arrive after a completed stream. A malformed optional report
    # must not discard that generation or trigger another paid request.
    usage = usage if isinstance(usage, dict) else {}
    timings = timings if isinstance(timings, dict) else {}
    def count(value):
        return value if type(value) is int and 0 <= value <= 1_000_000_000 else None

    def finite(value):
        if type(value) is int:
            return 0 <= value <= sys.float_info.max
        return type(value) is float and math.isfinite(value) and value >= 0

    input_tokens = count(usage.get("prompt_tokens", usage.get("input_tokens")))
    if "prompt_tokens" not in usage and input_tokens is not None:
        extras = [count(usage[key]) if key in usage else 0
                  for key in ("cache_read_input_tokens", "cache_creation_input_tokens")]
        input_tokens = count(input_tokens + sum(extras)) if None not in extras else None
    output_tokens = count(usage.get("completion_tokens", usage.get("output_tokens")))
    details = usage.get("prompt_tokens_details", usage.get("input_tokens_details"))
    details = details if isinstance(details, dict) else {}
    cached = count(details.get("cached_tokens", usage.get("cache_read_input_tokens")))
    if cached is not None and input_tokens is not None and cached > input_tokens:
        cached = None
    rate = timings.get("predicted_per_second")
    if rate is None and output_tokens is not None and finite(first) and finite(end) and end >= first:
        rate = output_tokens / max(0.01, end - first)
    # Phase 22: reasoning tokens are part of the reported output (Responses output_tokens_details, chat-completions
    # completion_tokens_details); a count above the output total is malformed and stays unknown.
    produced = usage.get("output_tokens_details", usage.get("completion_tokens_details"))
    reasoning = count(produced.get("reasoning_tokens")) if isinstance(produced, dict) else None
    if reasoning is not None and (output_tokens is None or reasoning > output_tokens):
        reasoning = None

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached,
        "tokens_per_second": round(rate, 1)
        if finite(rate)
        else None,
        "rate_source": "runtime"
        if timings.get("predicted_per_second") is not None
        else "stream elapsed",
        "cache_write_tokens": count(usage.get("cache_creation_input_tokens")),
        "reasoning_tokens": reasoning,
    }


REPORTED = (("input", "input_tokens"), ("cached", "cached_tokens"), ("output", "output_tokens"),
            ("reasoning", "reasoning_tokens"))


def attempt_report(stats):
    """One attempt's provider-reported counts, in the activity contract's names, or None when it reported none.

    Only what the provider reported: a missing field stays None, nothing is estimated."""
    stats = stats if isinstance(stats, dict) else {}
    report = {short: stats.get(name) if type(stats.get(name)) is int and 0 <= stats.get(name) <= 1_000_000_000 else None
              for short, name in REPORTED}
    if report["input"] is None and report["output"] is None:
        return None
    rate = stats.get("tokens_per_second")
    report["tokens_per_second"] = rate if type(rate) in (int, float) and math.isfinite(rate) and rate > 0 else None
    return report


def add_report(total, report):
    """A run's sum over the attempts whose provider reported usage (Phase 22 `reported`).

    Beside the message's totals, which stay unknown once any attempt's usage is unknown (an abandoned attempt may
    still be billed), this says what the provider did report. `attempts` counts the summed attempts; a field is None
    until one of them reported it; tokens_per_second is output over generation time, as for the totals."""
    total = dict(total) if isinstance(total, dict) else {
        "input": None, "cached": None, "output": None, "reasoning": None, "tokens_per_second": None,
        "attempts": 0, "generation_seconds": 0.0}
    for short, _ in REPORTED:
        if report.get(short) is not None:
            total[short] = (total.get(short) or 0) + report[short]
    if report.get("tokens_per_second") and report.get("output"):
        total["generation_seconds"] = total.get("generation_seconds", 0.0) + report["output"] / report["tokens_per_second"]
    total["tokens_per_second"] = (round(total["output"] / total["generation_seconds"], 1)
                                  if total.get("output") and total.get("generation_seconds") else None)
    total["attempts"] = total.get("attempts", 0) + 1
    return total
