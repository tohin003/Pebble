"""Provider-specific reasoning controls, verified 2026-10-09.
Unknown models use only their default until their provider reports capabilities.
"""
import re
from urllib.parse import urlsplit


def capabilities(p):
    model = p.get('model', '')
    host = urlsplit(p.get('url', '')).hostname
    levels, default, parameter = [], 'default', None
    if p['kind'] == 'gguf':
        if re.search(r'FrogNano|qwen.?3[._-]?5', p.get('name', ''), re.I):
            levels, default, parameter = ['quick', 'think'], 'quick', 'local_thinking'
    elif host == 'api.openai.com' or p['kind'] == 'chatgpt':
        if model in {'gpt-6-luna','gpt-6-sol'}:
            levels, default = ['none', 'low', 'medium', 'high', 'xhigh', 'max'], 'medium'
            parameter = 'reasoning'
        elif model in {'gpt-6-astra','gpt-6.1-sol'}:
            levels, default, parameter = ['low','medium','high','xhigh','max'], 'medium', 'reasoning'
        elif model in {'o3', 'o3-mini', 'o4-mini', 'o1'}:
            levels, default, parameter = ['low', 'medium', 'high'], 'medium', 'reasoning'
        elif model in {'gpt-5', 'gpt-5-mini', 'gpt-5-nano'}:
            levels, default, parameter = ['minimal', 'low', 'medium', 'high'], 'medium', 'reasoning'
    elif host == 'generativelanguage.googleapis.com' and '/openai' in p.get('url', ''):
        core = re.sub(r'-preview(?:-.*)?$', '', model)
        if core in {'gemini-3.8-flash', 'gemini-3.7-flash'}:
            levels, default = ['low', 'medium', 'high'], 'medium'
        elif core in {'gemini-3.6-flash', 'gemini-3.5-flash'}:
            levels, default = ['minimal', 'low', 'medium', 'high'], 'medium'
        elif core == 'gemini-3.1-pro':
            levels, default = ['low', 'medium', 'high'], 'high'
        elif core in {'gemini-3-flash', 'gemini-3.1-flash-lite', 'gemini-3.5-flash-lite'}:
            levels, default = ['minimal', 'low', 'medium', 'high'], 'minimal' if 'lite' in core else 'high'
        elif core == 'gemini-2.5-pro':
            levels, default = ['low', 'medium', 'high'], 'default'
        elif core in {'gemini-2.5-flash', 'gemini-2.5-flash-lite'}:
            levels, default = ['none', 'low', 'medium', 'high'], 'default'
        if levels:
            parameter = 'reasoning_effort'
    elif p['kind'] == 'anthropic':
        core = re.sub(r'-\d{8}$', '', model)
        five = {'claude-fable-5-1','claude-mythos-5-1','claude-fable-5','claude-mythos-5', 'claude-opus-5-5','claude-opus-5','claude-opus-4-8','claude-opus-4-7','claude-sonnet-5-5','claude-sonnet-5','claude-haiku-5-5'}
        four = {'claude-mythos-preview','claude-opus-4-6','claude-sonnet-4-6'}
        if core in five:
            levels = ['low', 'medium', 'high', 'xhigh', 'max']
        elif core in four:
            levels = ['low', 'medium', 'high', 'max']
        elif core == 'claude-opus-4-5':
            levels = ['low', 'medium', 'high']
        if levels:
            default = 'medium' if core in {'claude-opus-5-5','claude-haiku-5-5'} else 'high'
            parameter = 'output_config.effort'
    # A fetched model catalogue may explicitly publish levels. This only changes
    # request formatting, never broker tools, filesystem scope or permissions.
    published = p.get('reasoning_capabilities', {})
    if published.get('parameter') in {'reasoning', 'reasoning_effort', 'output_config.effort'} and isinstance(published.get('levels'), list):
        approved = [v for v in published['levels'] if v in {'none','minimal','low','medium','high','xhigh','max'}]
        if approved and published.get('default') in approved:
            levels, default, parameter = approved, published['default'], published['parameter']
    chosen = p.get('effort', default)
    if chosen not in levels and chosen != 'default':
        chosen = default
    return {'efforts': ['default'] + levels if levels else ['default'], 'default_effort': default,
            'effort': chosen, 'effort_parameter': parameter}


def choose(p, effort):
    c = capabilities(p)
    if isinstance(effort, bool):
        effort = ('think' if effort else 'quick') if p['kind'] == 'gguf' and 'quick' in c['efforts'] else p.get('effort', c['default_effort'])
    effort = effort or p.get('effort', c['default_effort'])
    if effort not in c['efforts']:
        raise ValueError('This model does not support that effort level. Choose one of its available settings.')
    return effort


def output_budget(effort):
    # Reasoning tokens count toward output. Avoid a tiny hard cutoff hiding the
    # answer. These are per-request caps, not a promise to spend this many tokens.
    return {'none':4096,'minimal':4096,'low':8192,'medium':16384,'high':32768,'xhigh':64000,'max':64000}.get(effort,16384)
