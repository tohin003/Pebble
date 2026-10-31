"""Phase 46 (1.9.28, follow-up 2, D190; the person's decision: "test first, then turn on"): a reply checked against the
person's saved preferences that can be checked on its own text (a date format, a currency, decimals, units, the
24-hour clock), repaired once as a soft check. controller.soft_checks (w7's mechanism) calls soft_issues: the model is
asked once with the issue and the run is never held; whatever it replies next is checked by the gate alone. On unless
PEBBLE_1928_MEMORY_CHECK is "0" (read when a run ends): off until the lead measured it on the development split, in the
final 1.9.28 dev check (2026-10-30), as D190 said.

The preferences are exactly the SAVED PREFERENCES the run's model was given (its checkpoint), newest first: the newest
record that speaks of a slot decides it, and a newer "forget"/"stop using" statement of the person's (D176) leaves the
slot unchecked. A slot the request decides itself ("Give the date as YYYY-MM-DD", "in USD") is not checked either.
Only the reply's own words are read, never a fenced code block, inline code, a blockquote, a quotation, a link, a
path or file name, a timestamp or a "key = value" pair, so a date quoted from a file or from a command's output is never
"corrected"; and a value the reply also writes the preferred way is no issue ("2026-11-21 (21.11.2026)"). Bounded:
MAX_REPLY characters, every pattern one pass.

Phase 46 (1.9.29, w22): what the host can write itself (an unambiguous date, a 12-hour time on a saved 24-hour clock, too
few decimals of a conversion) is repaired by the host before the reply is shown, with no model request (repair,
repair_reply; the runner calls it each time a reply is checked, with a notice above the reply); a record that says
several things decides each slot one of its clauses says alone (own_slots).
"""

import json
import os
import re
from pathlib import Path

from . import memory, memory_facts

SWITCH = 'PEBBLE_1928_MEMORY_CHECK'
MAX_REPLY = 20000
MAX_RECORDS = 20
MAX_EXAMPLES = 3
NOTICE = 'Pebble checked the reply against your saved preferences.'

_FINAL = re.compile(r'^[ \t>*_`#-]*FINAL[ \t]*:(.*)$', re.I | re.M)  # one line each
_FENCED = re.compile(r'(?ms)^[ \t]*(```|~~~)[^\n]*$.*?(?:^[ \t]*\1[ \t]*$|\Z)')
_QUOTED_LINE = re.compile(r'(?m)^[ \t]*>.*$')
_LINK = re.compile(r'\b(?:https?|ftp|file)://\S+|\bwww\.\S+', re.I)
_PATHISH = re.compile(r'(?<!\S)(?=\S*[A-Za-z_~])\S*[/\\]\S*'
                      r'|\b[\w-]{1,80}(?:\.[\w-]{1,80}){0,4}\.[A-Za-z][A-Za-z0-9]{0,6}\b')
_STAMP = re.compile(r'\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?|[ ]\d{2}:\d{2}:\d{2}'
                    r'(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)')
_PAIR = re.compile(r'\b[A-Za-z_][\w.-]{0,63}[ \t]*=[ \t]*\S+|\b[a-z][a-z0-9]*_[\w]*:[ \t]*\S+')

_NUMERIC_DATE = re.compile(r'(?<![\w.:/-])(\d{1,4})([./-])(\d{1,2})\2(\d{1,4})(?![\w:/-]|\.\d)')
_MONTHS = (r'jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|'
           r'oct(?:ober)?|nov(?:ember)?|dec(?:ember)?')
_MONTH_NUMBER = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10,
                 'nov': 11, 'dec': 12}
_NAMED_DATE = re.compile(r'\b(?:(\d{1,2})(?:st|nd|rd|th)?\.? (' + _MONTHS + r')\.?,? (\d{4})|(' + _MONTHS
                         + r')\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}))\b', re.I)
_SYMBOLS = {'€': 'EUR', '£': 'GBP', '¥': 'JPY', '$': 'USD', 'fr.': 'CHF', 'franc': 'CHF', 'francs': 'CHF',
            'euro': 'EUR', 'euros': 'EUR', 'dollar': 'USD', 'dollars': 'USD'}  # "$" read as US dollars
_CURRENCY_NAMES = '|'.join(memory_facts.CURRENCIES)
_AMOUNT = re.compile(r'(?<![\w.])(?:(?P<pre>' + _CURRENCY_NAMES + r'|[€£¥$]|Fr\.)[ \t]?(?P<a>\d[\d,\'’]{0,15}(?:\.\d{1,4})?)'
                     r'|(?P<b>\d[\d,\'’]{0,15}(?:\.\d{1,4})?)[ \t]?(?P<post>' + _CURRENCY_NAMES
                     + r'|[€£¥]|francs?|euros?|dollars?)\b)(?![\d.,]\d)')
# a currency or decimals preference covers what the reply works out for the request (a conversion), never a price or an
# amount it reports from a page or a file ("CHF 89" from a shop's page)
_MONEY_REQUEST = re.compile(r'\b(?:convert\w*|conversions?|exchange\w*|into my|in my (?:preferred |usual )?currency|'
                            r'my preferred currency)\b', re.I)
_IMPERIAL = re.compile(r'\b\d[\d,.]{0,20}[ \t]?(?:miles?|mi|mph|feet|foot|ft|inch(?:es)?|yards?|yd|lbs?|ounces?|oz|'
                       r'gallons?|gal|°F|ºF|degrees? fahrenheit|fahrenheit)(?!\w)', re.I)
_METRIC = re.compile(r'\b\d[\d,.]{0,20}[ \t]?(?:km|kilomet(?:er|re)s?|met(?:er|re)s?|m|cm|mm|kg|kilograms?|g|grams?|l|'
                     r'lit(?:er|re)s?|ml|km/h|kph|°C|ºC|degrees? celsius|celsius)(?!\w)', re.I)
_TWELVE = re.compile(r'\b(?:1[0-2]|0?[1-9])(?::[0-5]\d)?[ \t]?(?:am|pm|a\.m\.|p\.m\.)(?!\w)', re.I)
_TWENTY_FOUR = re.compile(r'\b(?:[01]\d|2[0-3]):[0-5]\d\b(?![ \t]?(?:am|pm|a\.m\.|p\.m\.))', re.I)
_REQUEST_UNITS = re.compile(r'\b(?:in|into|to) (?:miles|km|kilomet(?:er|re)s|feet|met(?:er|re)s|kg|kilograms|pounds|lbs|'
                            r'celsius|fahrenheit|°[CF])\b|\b(?:metric|imperial)\b', re.I)
_REQUEST_CURRENCY = re.compile(r'\b(?:in|into|to|as) (?:' + _CURRENCY_NAMES + r'|swiss francs?|francs?|euros?|us dollars?|'
                               r'dollars?|pounds? sterling|yen)\b', re.I)
_REQUEST_DATE = re.compile(r'\bISO\b|\b(?:US|American|European|British) (?:date )?format\b', re.I)

ISSUES = {
    'date': ("The person's saved preferences say dates are written as {value}; your reply writes {examples}. Write the "
             "dates of your own text as {value} (a date quoted from a file, code or a command's output stays as it is)."),
    'currency': ("The person's saved preferences give amounts in {value}; your reply's result is {examples}. Give it in "
                 "{value}, or say why you cannot."),
    'decimals': "The person's saved preferences give amounts with {value} decimals; your reply writes {examples}.",
    'units': "The person's saved preferences use {value} units; your reply gives {examples} only.",
    'clock': "The person's saved preferences use the {value} clock; your reply writes {examples}.",
}
SUFFIX = ' If the request asked for another form, keep it; keep any requested FINAL line.'


def enabled():
    """Whether the check is on: unless PEBBLE_1928_MEMORY_CHECK is "0" (read when a run ends, so it can be turned off
    without a restart)."""
    return os.environ.get(SWITCH, '1') != '0'


def given_preferences(run):
    """The SAVED PREFERENCES records the run's model was given ([{'content', 'saved', ...}], newest first), from the
    run's checkpoint; [] when it was given none (8K or less, memory off, or no saved preference)."""
    checkpoint = (run or {}).get('checkpoint') if isinstance(run, dict) else None
    messages = checkpoint.get('messages') if isinstance(checkpoint, dict) else None
    for message in messages if isinstance(messages, list) else ():
        if isinstance(message, dict) and message.get('name') == 'pebble_saved_preferences':
            try:
                records = json.loads(str(message.get('content', '')).split('\n', 1)[1])
            except (IndexError, ValueError):
                return []
            return [r for r in records if isinstance(r, dict) and isinstance(r.get('content'), str)][:MAX_RECORDS]
    return []


def preferred(records):
    """{slot: value} the person's saved preferences decide, newest first: the newest record speaking of a slot decides
    it; a "forget"/"stop using" statement of theirs (memory.revocation) leaves its slots unchecked. w17 (the follow-ups
    review's finding 5): only their own word on a slot (memory_facts.own_slot), never a fact about a third thing ("our
    invoices are in EUR") nor a wish for one ("amounts in EUR for invoices") saved beside it. Phase 46 (1.9.29): a
    clause of theirs that says only its slot decides it too (own_slots)."""
    decided, values = set(), {}
    for record in sorted(records, key=lambda r: str(r.get('saved') or ''), reverse=True):
        content = record.get('content') or ''
        if memory.revocation(content):
            decided |= memory_facts.slots_touched(content)
            continue
        own = own_slots(content)
        for slot, value in own.items():
            if slot not in decided:
                decided.add(slot)
                values[slot] = value
        # Phase 46 (1.9.29, the lead; private d-me-244 and the held-out memory counts: a preference changed in a later
        # session): the person's own newer word that changes a slot without deciding it by itself ("Update to my
        # preferences: dates should now be written as YYYY-MM-DD.", "I have switched to ISO dates, YYYY-MM-DD, for
        # everything now.") leaves that slot undecided for every older record, as a stop does, so the check never holds
        # the replaced value against a reply. Never a fact about a third thing ("the vendor API returns ISO dates") nor a
        # wish for one ("amounts in EUR for invoices"): they name no change of the person's own (w17's finding 5).
        unsettled = set(memory_facts.preference_slots(content[:memory_facts.SLOT_TEXT])) - set(own)
        if unsettled and CHANGED.search(content) and PERSONS_OWN.search(content) and not SCOPED.search(content) \
                and not _EXCEPTION.search(content):  # never a wish for one thing ("for the invoices") or an exception
            decided |= unsettled
    return values


CHANGED = re.compile(r"\b(?:now|instead|no\s+longer|any\s*more|moved|changed?|switch(?:ed)?|renamed|updated?|"
                     r"from\s+now\s+on|going\s+forward)\b", re.I)
SCOPED = re.compile(r"\bfor\s+(?:(?:the|my|our|your|these|those|this|that|its|their)\s+)?(?!(?:everything|everyone|all|now|me|good|future|later|next|every|each|any|anything|always)\b)[a-z][\w-]*|"
                    r"\bin\s+(?:the\s+)?[\w-]+\s+(?:files?|names?|folders?|sheets?|reports?)\b", re.I)
PERSONS_OWN = re.compile(r"\b(?:I|I'm|I've|I'd|my|me)\b|^(?:please\s+)?(?:change|switch|update|set|use|write)\b", re.I)


# Phase 46 (1.9.29, w22): a record that names an exception decides none of its slots ("dates as DD.MM.YYYY, except in
# file names, where you use YYYY-MM-DD"); the clauses of one that says several things are read one by one
_EXCEPTION = re.compile(r"\b(?:except|unless|but\s+not|other\s+than|apart\s+from|excluding|besides|only\s+(?:for|in|on))\b",
                        re.I)
_CLAUSE = re.compile(r"\s*(?:;|,\s*(?:and|but|while|plus)\b|\band\b|,)\s*", re.I)
# Phase 46 (1.9.29, w22-memory-r2): a record may keep several sentences (memory._spec_after): each is read as a clause,
# unless the record's lead says what they are about ("how I want CSV exports: ... Dates are written as DD.MM.YYYY."):
# then they are that thing's, and none decides a slot of every reply and file
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
# Phase 46 (1.9.29, the lead; private d-me-245): the words that tie a stop of a habit ("Please stop starting your replies
# with Ahoy.") to a saved record of the person's (memory.Memory.revoked_records), never the statement's own verbs,
# function words or the words every presentation preference shares ("reply", "line", "start")
_PART_COMMON = frozenset('''the and for you your yours with that this from now future session sessions later every each all
any please remember keep mind forget stop stopped using use want wants like longer more anymore don dont not never also
about what said told preference preferences rule rules thing things one ones would should could will can just its are was
were have has had been being into onto out off over under them they their there then than when whenever across reply
replies answer answers line lines end ending start starting'''.split())


def _part_words(text):
    return {word for word in re.findall(r"[a-z0-9]+", text.casefold()) if len(word) > 2 and word not in _PART_COMMON}


def own_slots(content):
    """{slot: value} one saved record decides (preferred): the whole record when it says only that
    (memory_facts.own_slot); Phase 46 (1.9.29, w22; the 1.9.28 dev cells of d-me-01 saved "I always want dates written
    as DD.MM.YYYY, and my staging server's settings are in deploy/staging.ini" as one record, which decided nothing) each
    clause that says only its slot, never a slot another clause speaks of with more words ("amounts in EUR for
    invoices") nor anything of a record with an exception."""
    if memory_facts.own_slot(content):
        return memory_facts.preference_slots(content)
    if _EXCEPTION.search(content[:memory_facts.SLOT_TEXT]):
        return {}
    found, spoken = {}, set()
    text = content[:memory_facts.SLOT_TEXT]
    lead = text.split(':', 1)[0] if ':' in text else ''
    parts = [text] if memory.SPEC_SUBJECT.search(lead) else _SENTENCE.split(text)
    for clause in [clause for part in parts for clause in _CLAUSE.split(part)][:12]:
        slots = memory_facts.preference_slots(clause)
        if slots and memory_facts.own_slot(clause):
            for slot, value in slots.items():
                found.setdefault(slot, value)
        else:
            spoken |= set(slots)
    return {slot: value for slot, value in found.items() if slot not in spoken}


def request_slots(request):
    """The slots the request decides itself (it wins over a saved preference): a date pattern or a named format, a
    target currency ("in USD"), decimals, units ("in miles"), a clock; and those a "forget"/"stop using" in it speaks
    of."""
    text = memory_facts.clean_text(request or '')[:memory_facts.SLOT_TEXT]
    found = set()
    if memory_facts.preference_slots(text).get('date') or _REQUEST_DATE.search(text):
        found.add('date')
    if _REQUEST_CURRENCY.search(text):
        found.add('currency')
    if 'decimals' in memory_facts.preference_slots(text):
        found.add('decimals')
    if _REQUEST_UNITS.search(text):
        found.add('units')
    if 'clock' in memory_facts.preference_slots(text):
        found.add('clock')
    for sentence in re.split(r'(?<=[.!?])\s+', text)[:20]:
        if memory.revocation(sentence):
            found |= memory_facts.slots_touched(sentence)
    return found


def own_text(reply):
    """The reply's own words (offsets kept): fenced code, blockquotes, quotations and inline code, links, paths and file
    names, timestamps and "key = value" pairs blanked; the FINAL line's value is added once more without its markup."""
    text, final = own_parts(reply)
    return text + ('\n' + final if final else '')


def own_parts(reply):
    """(own text, the last FINAL line's value without its markup) of a reply (own_text)."""
    text = reply[:MAX_REPLY] if isinstance(reply, str) else ''
    blank = lambda found: re.sub(r'[^\n]', ' ', found.group())  # noqa: E731
    final = _FINAL.findall(text)
    text = _QUOTED_LINE.sub(blank, _FENCED.sub(blank, text))
    text = memory_facts.cue_text(text).replace(memory_facts.PLACEHOLDER, ' ')
    for pattern in (_LINK, _STAMP, _PATHISH, _PAIR):
        text = pattern.sub(blank, text)
    return text, final[-1].strip().strip('`*_ ') if final else ''


def _dates(text, named=True):
    """[(text, {(order, separator, year, month, day)})] of the full dates written in text (a four-digit year), each
    with the readings it allows ("05/06/2026": DMY or MDY); with `named`, also "21 November 2026"."""
    return [(text[start:end], readings) for start, end, readings in _date_spans(text, named)]


def _date_spans(text, named=True):
    """_dates with each date's place: [(start, end, readings)] (Phase 46, 1.9.29: repair rewrites them in place)."""
    found = []
    for match in _NUMERIC_DATE.finditer(text):
        first, separator, second, last = match[1], match[2], int(match[3]), match[4]
        readings = set()
        if len(first) == 4 and len(last) <= 2:
            readings.add(('YMD', separator, int(first), second, int(last)))
        elif len(last) == 4 and len(first) <= 2:
            readings |= {('DMY', separator, int(last), second, int(first)),
                         ('MDY', separator, int(last), int(first), second)}
        readings = {r for r in readings if 1900 <= r[2] <= 2100 and 1 <= r[3] <= 12 and 1 <= r[4] <= 31}
        if readings:
            found.append((match.start(), match.end(), readings))
    for match in _NAMED_DATE.finditer(text) if named else ():
        day, month, year = (match[1], match[2], match[3]) if match[1] else (match[5], match[4], match[6])
        if 1 <= int(day) <= 31:
            found.append((match.start(), match.end(),
                          {('named', ' ', int(year), _MONTH_NUMBER[month[:3].casefold()], int(day))}))
    return found


def _date_issue(text, final, value):
    """The dates written another way than `value`, in the reply's own text and its FINAL value: another numeric form
    ("2026-11-21" for DD.MM.YYYY) or a month name ("21 November 2026", "Nov 21 2026": D205, the person, 2026-10-30, so a
    date preference covers every date the reply writes; before, a month name counted only in the FINAL value); never one
    also written the preferred way. Quotes, code, file names, links and timestamps are blanked first (own_parts)."""
    order = ''.join(part[0] for part in re.split(r'[./-]', value))
    separator = re.search(r'[./-]', value).group()
    dates = _dates(text) + _dates(final)
    kept = {r[2:] for _, readings in dates for r in readings if r[0] == order and r[1] == separator}
    wrong = [shown for shown, readings in dates
             if not any(r[0] == order and r[1] == separator for r in readings)
             and not any(r[2:] in kept for r in readings)]
    return wrong


def _amounts(text):
    """[(text, currency, decimals)] of the amounts with a currency in text."""
    found = []
    for match in _AMOUNT.finditer(text):
        mark = match['pre'] or match['post'] or ''
        number = match['a'] or match['b'] or ''
        currency = mark.upper() if mark.upper() in memory_facts.CURRENCIES else _SYMBOLS.get(mark.casefold())
        if currency:
            found.append((match.group().strip(), currency, len(number.split('.', 1)[1]) if '.' in number else 0))
    return found


def reply_issues(answer, records, request=''):
    """[issue] of one reply against the person's saved preferences `records` (above). Pure: the replay of recorded runs
    and the tests call it as soft_issues does."""
    return [issue for _, issue in check(answer, records, request)]


def check(answer, records, request=''):
    """[(slot, issue)] of one reply against the person's saved preferences `records` (reply_issues)."""
    values = {slot: value for slot, value in preferred(records).items() if slot not in request_slots(request)}
    if not values or not isinstance(answer, str) or not answer.strip():
        return []
    prose, final = own_parts(answer)
    text = prose + ('\n' + final if final else '')
    asked = request if isinstance(request, str) else ''
    issues = []

    def issue(slot, value, examples):
        shown = ', '.join(json.dumps(example[:40], ensure_ascii=False) for example in examples[:MAX_EXAMPLES])
        issues.append((slot, ISSUES[slot].format(value=value, examples=shown) + SUFFIX))

    if 'date' in values:
        wrong = _date_issue(prose, final, values['date'])
        if wrong:
            issue('date', values['date'], wrong)
    amounts = [a for a in _amounts(text) if a[0] not in asked]  # never the request's own figures ("120 EUR")
    if 'currency' in values and amounts and _MONEY_REQUEST.search(asked or '') \
            and not any(currency == values['currency'] for _, currency, _ in amounts):
        issue('currency', values['currency'], [shown for shown, _, _ in amounts])
    if 'decimals' in values and _MONEY_REQUEST.search(asked or ''):
        mine = [a for a in amounts if 'currency' not in values or a[1] == values['currency']]
        wrong = [shown for shown, _, places in mine if places != values['decimals']]
        if wrong and not any(places == values['decimals'] for _, _, places in mine):
            issue('decimals', values['decimals'], wrong)
    if 'units' in values:
        other, own = (_IMPERIAL, _METRIC) if values['units'] == 'metric' else (_METRIC, _IMPERIAL)
        wrong = [found.group() for found in other.finditer(text) if found.group() not in asked]
        if wrong and not own.search(text):
            issue('units', values['units'], wrong)
    if 'clock' in values:
        other, own = (_TWELVE, _TWENTY_FOUR) if values['clock'] == '24h' else (_TWENTY_FOUR, _TWELVE)
        wrong = [found.group() for found in other.finditer(text) if found.group() not in asked]
        if values['clock'] == '12h':
            wrong = [shown for shown in wrong if int(shown.split(':')[0]) >= 13 or shown.startswith('0')]
        if wrong and not own.search(text):
            issue('clock', '24-hour' if values['clock'] == '24h' else '12-hour', wrong)
    return issues


def soft_issues(app, run, answer):
    """[(issue, notice)] for controller.soft_checks (w7): the reply against the SAVED PREFERENCES its run's model was
    given; [] unless PEBBLE_1928_MEMORY_CHECK is on. A `memory_check` run event records the slots that fired."""
    if not enabled():
        return []
    records = given_preferences(run)
    if not records:
        return []
    found = check(answer, records, (run or {}).get('goal') or '')
    if found:
        app.runs.note(run['id'], 'memory_check', {'count': len(found), 'slots': [slot for slot, _ in found]})
    return [(issue, NOTICE) for _, issue in found]


# Phase 46 (1.9.29, w22; the held-out failing memory cells wrote files in their later sessions): a prose file the run
# wrote is checked as a reply is, for dates; the model is asked once (controller.soft_checks), never a host rewrite of
# a file. Data files (CSV, JSON ...) keep their machine forms.
PROSE = ('.md', '.markdown', '.txt', '.rst')
MAX_FILES, MAX_FILE_CHARS = 8, 40000
FILE_ISSUE = ("The person's saved preferences say dates are written as {value}; the file {path} you wrote writes "
              "{examples}. Write the dates of the text you wrote there as {value} (a date quoted from a file, code or a "
              "command's output stays as it is).")


def file_issues(app, run, tools, workspace):
    """[(issue, notice)] for controller.soft_checks: the prose files this project run wrote (Write, Edit or Append
    applied; PROSE) whose own text writes a date otherwise than the saved preference says (own_parts and _date_issue,
    as for a reply); [] unless PEBBLE_1928_MEMORY_CHECK is on and the run was given a date preference the request does
    not decide. Bounded: MAX_FILES files, MAX_FILE_CHARS characters each, inside the project only."""
    from .soft_checks import applied_edit
    if not enabled() or not workspace:
        return []
    records = given_preferences(run)
    values = {slot: value for slot, value in preferred(records).items()
              if slot not in request_slots((run or {}).get('goal') or '')}
    if 'date' not in values:
        return []
    # the runner's policy.Workspace (its resolved root), or a path
    root, seen, issues = Path(getattr(workspace, 'root', workspace)).resolve(), [], []
    for tool in tools or ():
        arguments = tool.get('arguments') if isinstance(tool.get('arguments'), dict) else {}
        rel = arguments.get('file_path')
        if not applied_edit(tool) or not isinstance(rel, str) or rel in seen or not rel.lower().endswith(PROSE):
            continue
        seen.append(rel)
        candidate = root / rel
        if candidate.is_symlink() or root not in candidate.resolve().parents or not candidate.is_file():
            continue
        prose, _ = own_parts(candidate.read_text(encoding='utf-8', errors='replace')[:MAX_FILE_CHARS])
        wrong = _date_issue(prose, '', values['date'])
        if wrong:
            shown = ', '.join(json.dumps(example[:40], ensure_ascii=False) for example in wrong[:MAX_EXAMPLES])
            issues.append((FILE_ISSUE.format(value=values['date'], path=json.dumps(rel), examples=shown) + SUFFIX,
                           NOTICE))
        if len(seen) >= MAX_FILES:
            break
    if issues:
        app.runs.note(run['id'], 'memory_check', {'count': len(issues), 'slots': ['date_file']})
    return issues


# Phase 46 (1.9.29, w22; the round brief's item 4: a new session's reply that says it followed a saved preference while
# its own text does not is repaired once host-side, before the reply, with no model request): what the host can write
# itself. A currency, units, extra decimals or an ambiguous date still go to the model once (soft_issues).
REPAIRED = {'date': 'Pebble wrote the dates the way your saved preferences give them ({value}).',
            'decimals': 'Pebble wrote the amounts with {value} decimals, as your saved preferences say.',
            'clock': 'Pebble wrote the times on the 24-hour clock, as your saved preferences say.'}
_TWELVE_PARTS = re.compile(r'\b(1[0-2]|0?[1-9])(?::([0-5]\d))?[ \t]?(am|pm|a\.m\.|p\.m\.)(?!\w)', re.I)


def _written(value, year, month, day):
    """A date in a saved numeric pattern ('DD.MM.YYYY', 'YYYY-MM-DD', 'D/M/YY' ...)."""
    separator = re.search(r'[./-]', value).group()
    parts = []
    for part in value.split(separator):
        letter = part[0].upper()
        number = {'Y': year, 'M': month, 'D': day}[letter]
        if letter == 'Y':
            parts.append('%04d' % number if len(part) == 4 else '%02d' % (number % 100))
        else:
            parts.append('%02d' % number if len(part) == 2 else str(number))
    return separator.join(parts)


def _unsettled(records):
    """The slots a saved record speaks of without deciding them by its own words (own_slots): a wish for one thing ("Use
    YYYY-MM-DD for the invoices instead.") or a record with an exception. The host never rewrites them (repair), since
    the person may have meant this very reply; a "forget"/"stop using" statement is preferred()'s."""
    found = set()
    for record in (records or [])[:MAX_RECORDS]:
        content = record.get('content') if isinstance(record, dict) else None
        if isinstance(content, str) and not memory.revocation(content):
            found |= set(memory_facts.preference_slots(content[:memory_facts.SLOT_TEXT])) - set(own_slots(content))
    return found


def repair(answer, records, request=''):
    """(answer, slots): `answer` with what the host can write itself the person's saved way (preferred; never a slot the
    request decides), and the slots it repaired: the dates of the reply's own text and of its FINAL value, each
    unambiguous (a four-digit year first, a month's name, or a day above 12: "05/06/2026" stays), never one the reply
    also writes the saved way nor one in code, a quotation, a link, a path, a timestamp or a "key = value" pair
    (own_parts); 12-hour times of its own text on a saved 24-hour clock; and, for a conversion, too few decimals of an
    amount padded with zeros. Never a slot a saved record speaks of without deciding it (unsettled: the model weighs
    those, soft_issues). Pure: repair_reply calls it with a run's records."""
    unsettled = _unsettled(records)
    values = {slot: value for slot, value in preferred(records).items()
              if slot not in request_slots(request) and slot not in unsettled}
    if not values or not isinstance(answer, str) or not answer.strip() or len(answer) > MAX_REPLY:
        return answer, []
    prose, _ = own_parts(answer)
    finals = list(_FINAL.finditer(answer))
    edits, slots = [], []
    if 'date' in values:
        value = values['date']
        order = ''.join(part[0] for part in re.split(r'[./-]', value))
        separator = re.search(r'[./-]', value).group()
        spans = _date_spans(prose)
        if finals:  # the FINAL value whatever its markup (own_parts blanks inline code, as "FINAL: `2026-11-21`" is)
            at = finals[-1].start(1)
            spans += [(at + s, at + e, r) for s, e, r in _date_spans(answer[at:finals[-1].end(1)])
                      if not any(s0 <= at + s < e0 for s0, e0, _ in spans)]
        kept = {r[2:] for _, _, readings in spans for r in readings if r[0] == order and r[1] == separator}
        for start, end, readings in spans:
            days = {r[2:] for r in readings}
            if any(r[0] == order and r[1] == separator for r in readings) or days & kept or len(days) != 1 \
                    or any(r[0] == 'YMD' and r[1] == '.' for r in readings):
                continue  # written the saved way, written so elsewhere too, ambiguous ("05/06/2026"), or a version
                # number's form ("2026.11.21", which the model check below still asks about)
            (year, month, day), = days
            edits.append((start, end, _written(value, year, month, day)))
        if edits:
            slots.append('date')
    if values.get('clock') == '24h':
        found = [m for m in _TWELVE_PARTS.finditer(prose) if m.group() not in (request or '')]
        for match in found:
            hour = int(match[1]) % 12 + (12 if match[3][0] in 'pP' else 0)
            edits.append((match.start(), match.end(), '%02d:%s' % (hour, match[2] or '00')))
        if found:
            slots.append('clock')
    if 'decimals' in values and _MONEY_REQUEST.search(request or ''):
        before = len(edits)
        for match in _AMOUNT.finditer(prose):
            mark = match['pre'] or match['post'] or ''
            currency = mark.upper() if mark.upper() in memory_facts.CURRENCIES else _SYMBOLS.get(mark.casefold())
            group = 'a' if match['a'] else 'b'
            number = match[group]
            places = len(number.split('.', 1)[1]) if '.' in number else 0
            if 'currency' in values and currency != values['currency'] or match.group().strip() in (request or '') \
                    or places >= values['decimals']:
                continue  # another currency, the request's own figure, or as many decimals (more: the model's)
            edits.append((match.start(group), match.end(group),
                          number + ('' if '.' in number else '.') + '0' * (values['decimals'] - places)))
        if len(edits) > before:
            slots.append('decimals')
    if not edits:
        return answer, []
    out, last = [], 0
    for start, end, text in sorted(edits):
        if start >= last:
            out += [answer[last:start], text]
            last = end
    return ''.join(out) + answer[last:], slots


def repair_reply(app, run_id, reply):
    """(reply, notice or None) for the runner's host repair of each checked reply before it is shown (Phase 46, 1.9.29):
    repair() against the SAVED PREFERENCES the run's model was given; the reply unchanged unless PEBBLE_1928_MEMORY_CHECK
    is on and it claims its task done (soft_checks.claims_done). A `memory_repair` run event records the slots."""
    from .soft_checks import claims_done
    if not enabled() or not isinstance(reply, str) or not claims_done(reply):
        return reply, None
    try:
        run = app.runs.get(run_id)
        records = given_preferences(run)
        if not records:
            return reply, None
        fixed, slots = repair(reply, records, run.get('goal') or '')
        if not slots or fixed == reply:
            return reply, None
        values = preferred(records)
        app.runs.note(run_id, 'memory_repair', {'slots': slots})
    except Exception as exc:  # a check that cannot run leaves the reply as written
        try:
            app.runs.note(run_id, 'memory_repair_failed', {'type': type(exc).__name__})
        except Exception:
            pass
        return reply, None
    return fixed, ' '.join(REPAIRED[slot].format(value=values.get(slot)) for slot in slots)
