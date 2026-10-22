"""Phase 17 (Q4 F1): Pebble never downloads a tokenizer. The o200k_base ranks load only from the bundled
file, checked against its published SHA-256; anything else falls back to a local over-count."""
import os
import shutil
import socket
from pathlib import Path

import pytest

from pebble import context, token_budget
from pebble.token_budget import TOKENIZER_FILE, ByteEstimate, TokenBudget

RESOURCES = Path(__file__).resolve().parents[2] / 'src-tauri' / 'resources'
SAMPLES = [
    'Hello, world! Plain English prose with a few words.',
    '你好，世界。这是中文文本。', 'مرحبا بالعالم', 'Ünïcödé façade — naïve café',
    'def f(x):\n    return {"a": [1, 2, 3], "b": x ** 2}\n',
    'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0MTIzNDU2Nzg5MA==' * 3, '🙂🚀👩‍💻 emoji run 🎉',
    '​⁦hidden⁩ ' + 'x' * 40, '<|endoftext|> is plain text here',
]


@pytest.fixture
def offline(monkeypatch):
    """Every outbound connection attempt is recorded and refused."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args[1:] if len(args) > 1 else args)
        raise OSError('network refused by test')
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)
    return attempts


@pytest.fixture
def fresh(monkeypatch):
    """Isolate the module's folder list and load cache."""
    monkeypatch.setattr(token_budget, '_bundled', [])
    monkeypatch.setattr(token_budget, '_loaded', {})
    return token_budget


def test_bundled_ranks_match_tiktoken_exactly_without_any_network(offline, fresh, monkeypatch):
    budget = TokenBudget(RESOURCES)
    encoding = budget.encoding()
    assert encoding.name == 'o200k_base' and not isinstance(encoding, ByteEstimate)
    # Reference: tiktoken's own registry reading the same bundled cache entry (present, so no fetch).
    monkeypatch.setenv('TIKTOKEN_CACHE_DIR', str(RESOURCES / 'tokenizer'))
    import tiktoken
    reference = tiktoken.get_encoding('o200k_base')
    for text in SAMPLES:
        assert encoding.encode(text, disallowed_special=()) == reference.encode(text, disallowed_special=())
    assert budget.count([{'role': 'user', 'content': SAMPLES[0]}]) > 64
    assert offline == []


def test_missing_tokenizer_never_downloads_and_over_counts(offline, fresh, monkeypatch, tmp_path):
    monkeypatch.setattr(fresh, '_SOURCE', str(tmp_path / 'absent'))
    estimate = TokenBudget(tmp_path / 'no-resources').encoding()
    assert isinstance(estimate, ByteEstimate) and estimate.exact is False
    exact = token_budget._load(RESOURCES / 'tokenizer')
    for text in SAMPLES:
        assert len(estimate.encode(text, disallowed_special=())) >= len(exact.encode(text, disallowed_special=()))
    budget = TokenBudget(tmp_path / 'no-resources')
    assert budget.count([{'role': 'user', 'content': 'x' * 1000}]) >= 1000
    assert offline == []


def test_altered_tokenizer_is_refused_and_left_in_place(offline, fresh, monkeypatch, tmp_path):
    folder = tmp_path / 'resources' / 'tokenizer'
    folder.mkdir(parents=True)
    altered = folder / TOKENIZER_FILE
    shutil.copyfile(RESOURCES / 'tokenizer' / TOKENIZER_FILE, altered)
    data = bytearray(altered.read_bytes())
    data[100] ^= 1
    altered.write_bytes(bytes(data))
    monkeypatch.setattr(fresh, '_SOURCE', str(tmp_path / 'absent'))
    assert isinstance(TokenBudget(tmp_path / 'resources').encoding(), ByteEstimate)
    assert altered.read_bytes() == bytes(data)  # tiktoken's loader would delete it and fetch again
    assert offline == []


def test_byte_estimate_prefix_slices_keep_focused_excerpts_working(fresh, monkeypatch, tmp_path):
    monkeypatch.setattr(fresh, '_SOURCE', str(tmp_path / 'absent'))
    TokenBudget(tmp_path / 'none')
    text = 'é' * 50 + 'abc' * 50
    excerpt = context.focused_excerpt(text, 10_000, token_limit=10)
    assert text.startswith(excerpt) and len(excerpt.encode()) <= 10
    tokens = ByteEstimate().encode('abcdef')
    with pytest.raises(TypeError):
        tokens[1:3]


def test_no_tiktoken_cache_variable_is_set_by_pebble(fresh, monkeypatch):
    monkeypatch.delenv('TIKTOKEN_CACHE_DIR', raising=False)
    TokenBudget(RESOURCES).encoding()
    assert 'TIKTOKEN_CACHE_DIR' not in os.environ
