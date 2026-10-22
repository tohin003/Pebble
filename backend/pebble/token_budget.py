"""Local token estimates for endpoints without an authorized counting API.

Estimates include schemas and framing headroom. Provider usage is authoritative;
this is deliberately not presented as an exact tokenizer for every provider.
``multiplier`` is a token_calibration factor in [1.0, 2.0]; it only enlarges.

The o200k_base ranks load only from the file bundled under ``<resources>/tokenizer``
and checked against its published SHA-256. Pebble never downloads a tokenizer: tiktoken's
own loader fetches a missing or altered file from the network (and deletes an altered
cache entry), so it is not used. Without a valid bundled file the estimate falls back to
one token per UTF-8 byte. Every o200k token covers at least one byte, so this never
under-counts; it only shrinks what is sent.
"""
import base64
import hashlib
import json
import math
from pathlib import Path

# tiktoken's cache key for the o200k_base blob (sha1 of its URL) and the file's published SHA-256.
TOKENIZER_FILE = 'fb374d419588a4632f3f557e76b4b70aebbca790'
TOKENIZER_SHA256 = '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d'
# The o200k_base split pattern and special tokens, as tiktoken_ext.openai_public defines them (MIT).
O200K_PATTERN = '|'.join([
    r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
    r"""[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?""",
    r"""\p{N}{1,3}""",
    r""" ?[^\s\p{L}\p{N}]+[\r\n/]*""",
    r"""\s*[\r\n]+""",
    r"""\s+(?!\S)""",
    r"""\s+""",
])
O200K_SPECIAL = {'<|endoftext|>': 199999, '<|endofprompt|>': 200018}
# Tokenizer folders named by TokenBudget(resources), first one first; a source checkout's own bundle is the
# last resort, so a module-level count before any App exists still uses the exact ranks.
_bundled = []
_SOURCE = str(Path(__file__).resolve().parents[2] / 'src-tauri' / 'resources' / 'tokenizer')
_loaded = {}  # folder -> Encoding or None; a failed folder is not retried


class _ByteTokens:
    """A prefix-sliceable stand-in for token IDs: one token per UTF-8 byte (an upper bound)."""
    __slots__ = ('data',)

    def __init__(self, data):
        self.data = data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, index):
        if not isinstance(index, slice) or index.start not in (None, 0) or index.step not in (None, 1):
            raise TypeError('Byte estimates support prefix slices only.')
        stop = len(self) if index.stop is None else max(0, index.stop)
        return _ByteTokens(self.data[:stop])


class ByteEstimate:
    """Used when the bundled tokenizer is missing or altered; never exact and never networked."""
    name = 'byte_estimate'
    exact = False

    def encode(self, text, disallowed_special=()):
        return _ByteTokens(text.encode('utf-8', errors='surrogatepass'))

    def decode_bytes(self, tokens):
        return tokens.data


def _load(folder):
    """The bundled o200k_base encoding, or None when the file is absent, unreadable or altered."""
    try:
        data = (Path(folder) / TOKENIZER_FILE).read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
        return None
    import tiktoken
    ranks = {}
    for line in data.splitlines():
        if line:
            token, rank = line.split()
            ranks[base64.b64decode(token)] = int(rank)
    return tiktoken.Encoding('o200k_base', pat_str=O200K_PATTERN, mergeable_ranks=ranks, special_tokens=O200K_SPECIAL)


class TokenBudget:
    def __init__(self, resources):
        folder = Path(resources) / 'tokenizer'
        if str(folder) not in _bundled:
            _bundled.append(str(folder))

    @staticmethod
    def encoding():
        for folder in (*_bundled, _SOURCE):
            if folder not in _loaded:
                _loaded[folder] = _load(folder)
            if _loaded[folder] is not None:
                return _loaded[folder]
        return _BYTE_ESTIMATE

    def count(self, messages, tools=(), multiplier=1.0):
        if type(multiplier) not in (int, float) or not 1.0 <= multiplier <= 2.0:
            raise ValueError('A calibration multiplier must be from 1.0 to 2.0.')
        image_cost=0
        def normalized(value):
            nonlocal image_cost
            if isinstance(value,str) and value.startswith('data:image/'):
                image_cost+=16000  # Bounded 3072px raster; base64 is not text tokens.
                return '[attached raster image]'
            if isinstance(value,dict):return {k:normalized(v) for k,v in value.items()}
            if isinstance(value,list):return [normalized(v) for v in value]
            return value
        payload = json.dumps(normalized({'messages':messages,'tools':tools}), ensure_ascii=False)
        count = len(self.encoding().encode(payload, disallowed_special=()))
        estimate = int(count * 1.03) + len(messages) * 12 + 64 + image_cost
        return estimate if multiplier == 1.0 else math.ceil(estimate * multiplier)


_BYTE_ESTIMATE = ByteEstimate()
