"""Pebble's own UI guidance skill: reviewed design text pinned by digest, never authority.

The bundle is built from the constants below through skill_bundle.make_bundle, so it has
the shape, limits and loader of an installed skill. Extensions delegates the reserved ID
here; nothing is stored in the extensions tables. The digest is checked at first use: a
mismatch fails closed (the guidance is neither offered nor loaded) and the app keeps running.

Size per window: never offered or admitted at an 8K window or below (parent decision at the Phase 19 merge: the
pinned SKILL.md costs about 1,000 tokens, over the design study's 520-token target for 8K). Above it, SKILL.md is
pinned whole on every later turn (MAIN_CHARS, PIN_TOKENS), and each reference loads in one
LoadSkill page (REFERENCE_CHARS). The text is written for Pebble; no third-party text is copied.
"""
import copy
import re
import threading

from .policy import PolicyError
from .skill_bundle import make_bundle

ID = 'pebble-ui-guidance'  # Not hex: an installed skill's uuid4 ID can never equal it.
NAME = 'pebble-ui-guidance'
MAIN = 'SKILL.md'
OFFER_ABOVE = 8192  # Nothing at an 8K window or below (Phase 19 merge; it was 4096): ~1,000 pinned tokens is too much there.
REFERENCES_ABOVE = 8192  # Reference pages load only above an 8K window (design study §5).
MAIN_CHARS = 2400
REFERENCE_CHARS = 3000  # app.py pages references at 3000 characters above 4K.
# TokenBudget counts added to an empty window. The runner's pinned message stays within an eighth of 8K.
PIN_TOKENS = 1024
MENU_TOKENS = 200
REFERENCE_TOKENS = 800
ATTRIBUTION = ("Attribution: Pebble's own wording, after the idea of the MIT-licensed taste-skill project; "
               'no text, image or code is copied from it.')

SKILL_MD = '''---
name: pebble-ui-guidance
description: Design guidance for Pebble views, from forms, tables and charts to quizzes, dashboards and app prototypes.
---
# Pebble UI guidance

Guidance only: it grants no permission, starts no tool and changes no host rule. Where the tool schema or a validator error differs from this text, follow them.

## Hard limits
- A view is data that Pebble's host draws. Never put links, web addresses, scripts, styles, HTML, event handlers or remote images in view data. Nothing in a view runs.
- Only host controls change task data, such as Submit, Grade, Share, Refresh and Send feedback. Prototype buttons only switch demo screens.
- Never draw what looks like an approval, a permission prompt, a sign-in box or a system dialog.
- Never ask for passwords, keys or tokens. Never invent figures, sources or results; label samples as samples.

## Which kind
- Form: answers you need before continuing. Table: records to compare. Chart: numbers over time (line) or across groups (bar).
- Plan: progress. Evidence: proof. Quiz: a learning check. Diagram: how parts connect. Dashboard: facts at a glance. Prototype: an app idea to click through.
- If a sentence answers it, skip the view.

## Taste
- One job per view; the title states the finding ("Signups doubled after launch"), not the format.
- Most important first. Seven columns or fewer, four chart series or fewer, no decoration.
- Plain words, sentence case, units in labels ("Revenue (USD)"), one number format per column.
- Colour never carries meaning alone; name every series and status.
- Captions name the source, its date or run, the sort order and any gaps.
- Empty states say what is missing. Buttons say what happens ("Save draft"); no filler copy.
- Forms ask only what blocks the work. Plans keep one step in progress. Diagrams stay at twelve nodes or fewer.

## References (LoadSkill with this digest and the path)
- references/dashboards.md
- references/prototypes.md
- references/quizzes.md

''' + ATTRIBUTION + '\n'

DASHBOARDS = '''# Dashboards

A dashboard answers one standing question at a glance, such as "Is this week's work on track?". Use that question, or its answer, as the title.

## Shape
- Three to six panels suit most dashboards. Never add a panel just to fill the grid; the validator allows at most eight.
- Use two columns by default, one when the view may open in the pet's narrow sidebar, and three only for a row of short metrics.
- Order panels from summary to detail: headline metrics, then the trend that explains them, then a table or evidence for the specifics.
- Each panel answers one smaller question, and its title says which.

## Metrics
- One number per metric panel, with its unit. Round to the precision a reader needs (1.2k rather than 1,204.37).
- A change needs its sign, its period and its baseline ("+12% against last week").
- Four metrics in a row at most.

## Where the numbers come from
- When the schema offers host bindings and one of the allowed host sources answers the question, prefer the binding. The host fills it, stamps when it was read, and the person refreshes it with a native control.
- Use inline data only for values this run produced and can point to, and name their origin in the caption.
- Never name a source the schema does not list.
- Values can change after you publish. Read the current revision before an update instead of assuming your earlier values still stand.

## Panels inside a dashboard
- Charts and tables follow the same rules as on their own: one idea, few series, units in labels.
- Choose a table when the person will look up a particular row, and a chart when the overall shape matters more than any single value.

## Avoid
- Gauges, icons or colour that carry no data.
- Panels covering different time ranges without saying so.
- The same number repeated in two panels.
- Verdicts such as "Healthy" unless the caption names the check behind them.
'''

PROTOTYPES = '''# App prototypes

A prototype is a mock-up the person can click through. Its buttons only move between its own screens, and its inputs and toggles hold throwaway demo state. Pebble labels it as a mock-up and supplies the feedback control.

## Plan the flow first
- Name the one task the demo shows ("Book a table for two") and build only the screens that task needs: usually three to six, never more than twelve.
- Set start to the screen a new user sees first.
- Every screen needs a way on and a way back, through a nav item or a button whose goto names another screen. Every goto must name a screen that exists; leave no dead ends.
- Include the moments people actually meet: an empty state, an error with a way to fix it, and a success screen. Show waiting as a short message.

## Each screen
- One heading that says where the person is.
- One primary button per screen; secondary choices sit below it as plain items.
- List components in the order the eye should travel: heading, content, then actions.
- Phone: a single column, the primary button last so it lands near the thumb, and a nav of three to five items.
- Tablet and desktop: still one clear primary action; group related content in cards.
- For any picture, use image_placeholder with a descriptive label and an aspect ratio. Never point to an image file or a web address.

## Words
- Use real, specific words from the product's domain, never filler text.
- Buttons name their result ("Confirm booking" rather than "Submit" or "OK").
- Input labels say what to enter, and placeholders show the expected format.
- Call the same thing by the same name on every screen.

## Theme
- Choose only from the offered theme tokens (accent, density, radius). One accent; cozy density unless the screen is dense with data.
- Pebble's host draws the type and colour. There is no custom styling.

## Never
- Imitate Pebble's own approvals, a system permission prompt, a sign-in page for a real service, a payment card form or a password field.
- Ask for real personal data. The demo keeps nothing and sends nothing.
- Present the demo as working software.
'''

QUIZZES = '''# Quizzes

A quiz checks understanding of something the person asked to learn. Pebble's host shows it, times it and handles grading; a result reaches you only when the person shares it.

## Before writing
- Put the topic and size in the title ("Python list basics: 8 questions").
- Test the material the person gave or asked about, not trivia around it.
- Five to ten questions suit most checks. Allow about a minute per question for the duration.

## Questions
- One idea per question, asked in a plain sentence.
- Avoid double negatives, and use "which is NOT" only when spotting the exception is the skill.
- Keep the answer out of the stem: no grammar, length or repeated-word clues.
- Go from easier to harder.

## Choices
- Three or four choices usually, similar in length and style.
- Make wrong choices believable by building them from common mistakes, never jokes.
- Skip "all of the above" and "none of the above".
- Move the correct answer around; it should not always be first or last.

## Explanations
- Say in one or two sentences why the right answer is right, and name the mistake behind the most tempting wrong choice.
- Explain kindly; an explanation teaches rather than scolds.

## After publishing
- Do not reveal answers in chat while the quiz is open.
- Never state a score or result yourself; only the host grades.
- Updating a quiz can end an attempt in progress, so tell the person before you replace it.
'''

REFERENCES = {
    'references/dashboards.md': DASHBOARDS,
    'references/prototypes.md': PROTOTYPES,
    'references/quizzes.md': QUIZZES,
}
# make_bundle's digest over SKILL_MD and REFERENCES. Changing any text needs a new review and this pin.
PINNED_DIGEST = 'dbae7b02863f6775c7bebea5a3f856e7080aafe263e44a3f7b4ae6c9d5fda68f'

# Requests that benefit from layout guidance; the runner also requires its own UIRender intent.
INTENT = re.compile(
    r'\b(?:dashboards?|prototypes?|mock(?:-| )?ups?|wireframes?|quiz(?:zes|zed)?|charts?|graphs?|diagrams?'
    r'|visuali[sz](?:e|es|ed|ing|ation|ations)|(?:app|ui|interface|screen) (?:demos?|designs?|flows?))\b', re.I)

_lock = threading.Lock()
_pinned = None


def files():
    return {MAIN: SKILL_MD.encode(), **{path: text.encode() for path, text in REFERENCES.items()}}


def bundle():
    """A private copy of the pinned bundle; PolicyError when the text no longer matches its pin."""
    global _pinned
    with _lock:
        if _pinned is None:
            try:
                built = make_bundle(SKILL_MD, NAME, files(), MAIN)
            except Exception:  # malformed text (YAML, encoding, limits) fails closed like a changed digest
                built = None
            if built is None or built['digest'] != PINNED_DIGEST or built['metadata']['name'] != NAME:
                raise PolicyError('Pebble UI guidance changed after review; it is unavailable until a reviewed build restores it.')
            _pinned = built
        return copy.deepcopy(_pinned)


def metadata():
    """The skill-menu entry, shaped like Extensions.skill_metadata."""
    pinned = bundle()
    return {'id': ID, 'name': pinned['metadata']['name'], 'digest': pinned['digest'],
            'description': pinned['metadata']['description'], 'files': len(pinned['files']), 'authorization': 'none'}


def offer(budget, text, tools):
    """Whether the runner lists the guidance: above 4K only, for layout intent, with views and files on."""
    return (type(budget) is int and budget > OFFER_ABOVE and isinstance(text, str) and INTENT.search(text) is not None
            and isinstance(tools, dict) and tools.get('views') is True and tools.get('files') is True)


def admit(budget, reference=None):
    """Refuse loading or selecting the guidance at a 4K window or below, where it would be new context.

    A reference page is model context too: design study §5 keeps references to windows above 8K.
    """
    if type(budget) is not int or budget <= OFFER_ABOVE:
        raise PolicyError('Pebble UI guidance needs a model window above 8,192 tokens.')
    if reference and budget <= REFERENCES_ABOVE:
        raise PolicyError('Pebble UI guidance references are not available at this window size; follow SKILL.md.')
