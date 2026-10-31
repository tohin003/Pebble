"""UI-owned permissions. Never inferred from prompts, documents or memory."""

import copy

DEFAULT = {
    "mode": "ask",
    "tools": {
        "files": True,
        "edits": True,
        "tests": True,
        "apps": True,
        "web": True,
        "terminal": True,
        "plugins": True,
        'views': True,
        # Phase 16 D04: scoped app and browser control. Off by default, and optional in a saved value:
        # an omitted key means off, so a client that predates it can only keep it off.
        'screen': False,
        # Phase 40 (1.6.0, D93): screenshots when a task needs them; macOS Screen Recording is the person's own gate.
        # Optional like 'screen': an omitted key takes its default here (on), so permissions saved by 1.5.x keep it.
        'capture': True,
    },
}
OPTIONAL_TOOLS = frozenset({'screen', 'capture'})
GROUP = {
    'ReadGitStatus': 'files', 'ReadGitDiff': 'files', 'ReadGitHistory': 'files',
    'ReadGitWorktrees': 'files', 'PreviewWorktreeExport': 'files', 'ReadWorktreeExport': 'files', 'IntegrateWorktreeFile': 'edits',
    'ProposeGitChange': 'edits', 'ReadGitPlanReceipt': 'files',
    'ListProjectRecipes': 'files', 'PrepareEnvironment': 'tests',
    'ReadPluginMedia':'plugins',
    'SearchPluginData':'plugins','LoadPluginData':'plugins','FetchPluginData':'plugins','ReadPluginData':'plugins',
    'UIRender': 'views', 'UIUpdate': 'views', 'RenderMod': 'views',
    "Glob": "files",
    "Read": "files",
    "ReadAttachment": "files",
    "Grep": "files",
    'SearchFiles': 'files',
    'CodeReferences': 'files',
    'RunCode': 'files',  # Phase 37: each call a script makes is checked against its own group as well
    "Write": "edits",
    "Append": "edits",
    "Edit": "edits",
    "RunTests": "tests",
    'RunRecipe': 'tests',
    "StartPreview": "tests",
    "OpenApp": "apps",
    "WebSearch": "web",
    "ReadURL": "web",
    "Weather": "web",
    "RunCommand": "terminal",
    'StartCommand': 'terminal', 'ReadProcess': 'terminal',
    'WaitProcess': 'terminal', 'StopProcess': 'terminal',
    'RunPlan': 'terminal', 'WriteProcessInput': 'terminal',
    "LoadSkill": "files",
    "PluginCall": "plugins",
    "SearchPluginTools": "plugins",
    "LoadPluginTool": "plugins",
    'ResearchPlan': 'web',
    'ResearchStatus': 'web',
    'ReadSource': 'web',
    'ScreenObserve': 'screen',
    'ScreenAct': 'screen',
    'ScreenRequest': 'screen',  # Phase 42 (1.8.0)
    'BrowserRequest': 'screen',  # Phase 43 (1.9.0): sites in the person's browser, as a window
    'OpenURL': 'apps',  # Phase 43 (1.9.0): as opening an app
    'AppAction': 'apps',  # Phase 43 (1.9.0): fixed scripting verbs, as opening an app
    # Phase 40 (1.6.0): the Companion's general tools
    'Shell': 'terminal', 'ReadFile': 'files', 'WriteFile': 'edits', 'EditFile': 'edits', 'Screenshot': 'capture',
    'FindTool': 'files',
}


def validate(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"mode", "tools"}
        or value["mode"] not in {"ask", "auto", "full"}
    ):
        raise ValueError("Choose Ask, Auto, or Full access.")
    tools = value["tools"]
    if (
        not isinstance(tools, dict)
        or not set(DEFAULT["tools"]) - OPTIONAL_TOOLS <= set(tools) <= set(DEFAULT["tools"])
        or any(type(v) is not bool for v in tools.values())
    ):
        raise ValueError("Invalid tool permissions.")
    value = copy.deepcopy(value)
    for name in OPTIONAL_TOOLS - set(tools):
        value["tools"][name] = DEFAULT["tools"][name]  # Omitted optional groups take their default (screen stays off).
    return value


def automatic(policy, kind, payload=None):
    # Full access means all supported actions may run without review. It does
    # not introduce an arbitrary shell, credential access or an OS bypass.
    # Phase 40 (D91): the Companion's Shell is a separate, sandboxed tool; its
    # widening asks (kind 'shell') always wait for the person, in every mode.
    # Child worktree allocation is an edit-class local effect outside the
    # project tree. Destructive, publication and dependency-install payloads
    # set requires_review, so they always wait for native review.
    # Phase 46 (1.9.28, D184, the person: "Ask only for risky steps"): in Auto a
    # Git plan whose Undo the plans keep runs too (git_plans.undo_kept); push,
    # deletion and history rewrites keep their card. PEBBLE_AUTO_RISKY_ONLY=0
    # turns it off. A confined command's card is decided where it is inspected.
    return (
        policy["mode"] == "full"
        or policy["mode"] == "auto"
        and (kind in {"edit", "tests", "command", "worktree"} or kind == "git" and undoable(payload))
    )


def undoable(payload):
    from .confined import enabled
    from .git_plans import undo_kept
    return enabled() and undo_kept(payload)
