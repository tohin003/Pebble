"""Host activity classes for the companion's status line (Phase 19 B1, design study §2.3).

Pure tables, no I/O. The runner adds ``activity`` to each ``progress`` event from
``classify(tool, kind)`` (parent patch P1), so the words a person sees come from
the host's own record of what it is doing, never from model text or a label that
embeds a path. Every name in ``tool_catalogue.TOOLS`` has an explicit class here;
``backend/tests/test_activity_kinds.py`` fails when a new tool arrives unclassified.
The browser keeps the same table as a fallback (``src/statusWords.ts``), and the
test keeps the two equal.

A class is presentation only: it grants nothing and is never sent to a model.
"""

# Activity keys shown while a run is live (queued, running or retry_wait).
ACTIVITY_KEYS = ('thinking', 'writing', 'reading', 'searching', 'editing', 'testing',
                 'running', 'delegating', 'viewing', 'working', 'loading')
# Host keys: a human is needed, the run is held, or the service is unavailable.
HOST_KEYS = ('question', 'review', 'reconnecting', 'blocked', 'uncertain', 'offline',
             'paused', 'error', 'sending')
KEYS = ACTIVITY_KEYS + HOST_KEYS

_CLASSES = {
    'reading': ('Read', 'ReadAttachment', 'ReadURL', 'ReadSource', 'ReadProcess', 'ReadRunEvidence',
                'LoadSkill', 'LoadPluginTool', 'LoadPluginData', 'ReadPluginData', 'ReadPluginMedia',
                'ReadGitStatus', 'ReadGitDiff', 'ReadGitHistory', 'ReadGitPlanReceipt', 'ReadGitWorktrees',
                'ReadWorktreeExport', 'ListProjectRecipes', 'ScreenObserve', 'ResearchStatus'),
    'searching': ('Glob', 'Grep', 'SearchFiles', 'CodeReferences', 'WebSearch', 'Weather',
                  'SearchPluginTools', 'SearchPluginData', 'ResearchPlan'),
    'editing': ('Write', 'Append', 'Edit', 'ProposePatch', 'IntegrateAgentPatch', 'ProposeGitChange',
                'PreviewWorktreeExport', 'IntegrateWorktreeFile'),
    'testing': ('RunTests', 'RunRecipe'),
    'running': ('RunCommand', 'StartCommand', 'WaitProcess', 'StopProcess', 'WriteProcessInput', 'RunPlan',
                'StartPreview', 'PrepareEnvironment', 'RunCode'),
    'delegating': ('SpawnAgent', 'SpawnWorktreeAgent', 'ReadAgentTree', 'SendAgentMessage', 'WaitForAgents',
                   'DelegateAgentTask', 'StopAgent', 'RetryAgent', 'AskParentQuestion', 'AnswerAgentQuestion',
                   'PromoteAgentQuestion', 'ReviewAgentResult', 'ReadAgentMessage', 'ReadAgentPatch',
                   'RequestAgentEffect', 'ReadAgentEffect', 'ReadAgentEffects', 'ExecuteAgentEffect',
                   'DismissAgentEffect'),
    'viewing': ('UIRender', 'UIUpdate', 'RenderMod'),
    # Host bookkeeping between model turns: the model is still deciding.
    'thinking': ('AskUserQuestion', 'UpdatePlan', 'UpdateGoal', 'MemoryPropose'),
    # Real actions with no narrower honest word (an app, a connector, a screen).
    'working': ('OpenApp', 'PluginCall', 'FetchPluginData', 'ScreenAct'),
}
ACTIVITY = {name: key for key, names in _CLASSES.items() for name in names}

# Run states (runtime.STATES) that hold a status key outside live activity.
HOST = {
    'retry_wait': 'reconnecting',
    'waiting_input': 'question',
    'waiting_approval': 'review',
    'budget_exhausted': 'blocked',
    'verification_required': 'blocked',
    'needs_reconciliation': 'uncertain',
    'paused': 'paused',
    'failed': 'error',
}
# Run states with no status key: live work is classified by its progress events,
# and a finished, stopped or interrupted run says nothing more.
QUIET = frozenset({'queued', 'running', 'completed', 'stopped', 'interrupted'})


def classify(tool, kind):
    """Activity key for one ``progress`` event, or None when it is not an activity.

    Only ``kind='working'`` describes what the host is doing now. A working event
    without a tool is the model's own turn (``thinking``). An unknown or unexpected
    tool name, including one a model invented, is plain ``working``: the name is
    never shown, so a tool called "Approve" cannot borrow a meaning.
    """
    if kind != 'working':
        return None
    if not isinstance(tool, str) or not tool:
        return 'thinking'
    return ACTIVITY.get(tool, 'working')
