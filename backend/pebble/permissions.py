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
    },
}
OPTIONAL_TOOLS = frozenset({'screen'})
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
        value["tools"][name] = False  # Omitted optional groups stay off.
    return value


def automatic(policy, kind):
    # Full access means all supported actions may run without review. It does
    # not introduce an arbitrary shell, credential access or an OS bypass.
    # Child worktree allocation is an edit-class local effect outside the
    # project tree. Git plans run automatically only in Full mode; destructive,
    # publication and dependency-install payloads set requires_review, so they
    # always wait for native review.
    return (
        policy["mode"] == "full"
        or policy["mode"] == "auto"
        and kind in {"edit", "tests", "command", "worktree"}
    )
