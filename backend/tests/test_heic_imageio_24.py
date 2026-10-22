"""Phase 24 (t24-build, V1-045): HEIC and HEIF are decoded by macOS (ImageIO through /usr/bin/sips) inside the
confined parser; pillow-heif is not part of the app.

Workers never run the real sips (track rule): the decode path is exercised with an injected sips that behaves like
the real one (it reads the file's properties, then writes a PNG to --out). The tests whose names contain
`real_sips` drive the real tool through the real sandbox, as validate_phase21's h1 and h2 do, and are the parent's
to run.
"""
import ast
import base64
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from pebble import attachment_worker, images
from pebble.documents import parse_document

ROOT = Path(__file__).resolve().parents[2]
SIZE, LEFT, RIGHT, TOLERANCE = (160, 96), (204, 64, 40), (36, 96, 208), 32  # validate_phase21's fixture
HEIC_HEAD = (b'\x00\x00\x00\x1cftypheic\x00\x00\x00\x00mif1heicmiaf') + b'\x00\x00\x00\x08mdat'


def phase21_pattern(name):
    """A regex constant of scripts/validate_phase21.py, read from its source (importing it starts nothing, but it
    pulls in the whole validator chain)."""
    tree = ast.parse((ROOT / 'scripts/validate_phase21.py').read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', None) == name:
            return re.compile(node.value.args[0].value)
    raise AssertionError(name)


HONEST, FALSE_LIMIT = phase21_pattern('HONEST'), phase21_pattern('FALSE_LIMIT')


def fixture_image(size=SIZE):
    image = Image.new('RGB', size, LEFT)
    image.paste(RIGHT, (size[0] // 2, 0, size[0], size[1]))
    return image


class FakeSips:
    """Behaves like /usr/bin/sips for `-g ...` and `-s format png IN --out OUT`; nothing else is accepted."""

    def __init__(self, image=None, orientation=None, properties=None, decode_exit=0):
        self.image, self.orientation, self.properties, self.decode_exit = image, orientation, properties, decode_exit
        self.calls = []

    def __call__(self, argv, env):
        self.calls.append(argv)
        assert argv[0] == images.SIPS and set(env) == {'PATH', 'HOME', 'TMPDIR', 'LANG'}
        if argv[1] == '-g':
            if self.properties is not None:
                return 0, self.properties
            width, height = self.image.size
            lines = [argv[-1], '  pixelWidth: %d' % width, '  pixelHeight: %d' % height]
            if self.orientation:
                lines.append('  orientation: %d' % self.orientation)
            return 0, '\n'.join(lines) + '\n'
        assert argv[1:4] == ['-s', 'format', 'png'] and argv[-2] == '--out'
        if self.decode_exit:
            return self.decode_exit, ''
        self.image.save(argv[-1], format='PNG')
        return 0, argv[-1] + '\n'


def describe(data_url):
    head, _, data = data_url.partition(';base64,')
    with Image.open(io.BytesIO(base64.b64decode(data))) as image:
        rgb = image.convert('RGB')
    w, h = rgb.size
    sides = {}
    for side, box in (('left', (w // 10, h // 5, w * 7 // 20, h * 4 // 5)), ('right', (w * 13 // 20, h // 5, w * 9 // 10, h * 4 // 5))):
        sides[side] = [round(v) for v in ImageStat.Stat(rgb.crop(box)).mean]
    return (w, h), sides


def matches(sides):
    return all(abs(a - b) <= TOLERANCE for side, colour in (('left', LEFT), ('right', RIGHT))
               for a, b in zip(sides[side], colour))


@pytest.fixture
def heic(tmp_path):
    path = tmp_path / 'phase24-photo.heic'
    path.write_bytes(HEIC_HEAD + bytes(256))
    return path


def test_heif_brands_go_to_macos_and_avif_stays_with_pillow():
    assert images.heif_file(HEIC_HEAD)
    assert images.heif_file(b'\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic')
    assert not images.heif_file(b'\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf')
    assert not images.heif_file(b'\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1avif')
    assert not images.heif_file(b'\x89PNG\r\n\x1a\n' + bytes(24)) and not images.heif_file(b'')
    assert 'HEIF' not in images.FORMATS and 'AVIF' in images.FORMATS


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS decodes HEIC')
def test_a_heic_photo_is_decoded_by_sips_to_its_size_and_colours(heic, tmp_path):
    sips = FakeSips(fixture_image())
    result = images.parse_image(heic, runner=sips, scratch=tmp_path)
    assert (result['width'], result['height']) == SIZE == (result['original_width'], result['original_height'])
    size, sides = describe('data:image/png;base64,' + result['data'])
    assert size == SIZE and matches(sides)
    assert matches(describe(result['preview'])[1])
    # Properties first (the size is checked before anything is decoded), then one decode into the scratch folder,
    # whose PNG is removed once read.
    assert [call[1] for call in sips.calls] == ['-g', '-s']
    out = Path(sips.calls[1][-1])
    assert out.parent == tmp_path and out.name.startswith('decoded-') and not out.exists()


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS decodes HEIC')
def test_the_source_orientation_is_applied_once(heic, tmp_path):
    # The PNG sips writes holds the photo's stored pixels; orientation 6 (rotate 90° clockwise) is applied here.
    result = images.parse_image(heic, runner=FakeSips(fixture_image(), orientation=6), scratch=tmp_path)
    assert (result['width'], result['height']) == (SIZE[1], SIZE[0])
    plain = images.parse_image(heic, runner=FakeSips(fixture_image(), orientation=1), scratch=tmp_path)
    assert (plain['width'], plain['height']) == SIZE


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS decodes HEIC')
def test_an_oversized_heic_is_refused_before_it_is_decoded(heic, tmp_path):
    sips = FakeSips(properties='%s\n  pixelWidth: 9000\n  pixelHeight: 9000\n' % heic)
    with pytest.raises(ValueError, match='40 megapixels'):
        images.parse_image(heic, runner=sips, scratch=tmp_path)
    assert [call[1] for call in sips.calls] == ['-g']


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS decodes HEIC')
@pytest.mark.parametrize('case', ['truncated', 'damaged', 'decode_fails'])
def test_a_broken_heic_is_refused_honestly(heic, tmp_path, case):
    """validate_phase21 h2's rule: an honest decode message, no traceback, path or false size limit."""
    sips = {'truncated': FakeSips(properties='%s\n  pixelWidth: <nil>\n' % heic),
            'damaged': FakeSips(properties='Error: Unable to read image.\n'),
            'decode_fails': FakeSips(fixture_image(), decode_exit=13)}[case]
    with pytest.raises(ValueError) as error:
        images.parse_image(heic, runner=sips, scratch=tmp_path)
    text = str(error.value)
    assert HONEST.search(text) and not FALSE_LIMIT.search(text) and str(tmp_path) not in text and 'Traceback' not in text
    assert not list(tmp_path.glob('decoded-*'))


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS decodes HEIC')
def test_the_worker_reports_a_heic_refusal_as_its_json_error_without_a_traceback(heic, tmp_path, monkeypatch, capsys):
    import resource
    monkeypatch.setenv(images.SCRATCH_ENV, str(tmp_path))
    monkeypatch.setattr(images, 'run_sips', FakeSips(properties='Error: Unable to read image.\n'))
    limits = []
    monkeypatch.setattr(resource, 'setrlimit', lambda *args: limits.append(args))  # the worker's own, not pytest's
    with pytest.raises(SystemExit) as done:
        attachment_worker.extract('--extract-image', str(heic))
    out, err = capsys.readouterr()
    assert done.value.code == 1 and 'Traceback' not in err
    message = json.loads(out)['error']
    assert HONEST.search(message) and not FALSE_LIMIT.search(message)
    # sips inherits a file-size limit that fits the largest PNG it may write, and nothing larger.
    assert limits == [(resource.RLIMIT_FSIZE, (images.DECODED_LIMIT, images.DECODED_LIMIT))]


def test_without_macos_or_a_scratch_folder_heic_is_refused_honestly(heic, monkeypatch):
    monkeypatch.delenv(images.SCRATCH_ENV, raising=False)
    with pytest.raises(ValueError) as error:
        images.parse_image(heic)
    assert HONEST.search(str(error.value)) and not FALSE_LIMIT.search(str(error.value))


def test_no_pillow_heif_anywhere_in_the_app(tmp_path):
    for path in sorted((ROOT / 'backend/pebble').glob('*.py')):
        text = path.read_text()
        assert 'pillow_heif' not in text and 'register_heif_opener' not in text, path.name
    pinned = re.compile(r'(?im)^\s*pillow[-_]heif\s*==')
    assert not pinned.search((ROOT / 'backend/requirements.txt').read_text())
    assert not pinned.search((ROOT / 'backend/requirements.lock').read_text())
    assert pinned.search((ROOT / 'backend/requirements-dev.txt').read_text())  # validate_phase21's encoder only
    # Decoding an ordinary image never loads it, even in an environment that has it installed.
    png = tmp_path / 'plain.png'
    fixture_image().save(png)
    code =('import sys; sys.path.insert(0, %r); from pebble.images import parse_image; parse_image(%r); '
            'print(sorted(m for m in sys.modules if "heif" in m))' % (str(ROOT / 'backend'), str(png)))
    loaded = subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, text=True, timeout=60)
    assert loaded.returncode == 0 and loaded.stdout.strip() == '[]', loaded.stderr[-2000:]


# The parent's real-tool checks ---------------------------------------------------------------------------------

def phase21_fixture(folder):
    """validate_phase21's photo: pillow-heif (developer environment) encodes the two-colour image."""
    pillow_heif = pytest.importorskip('pillow_heif')
    encoded = io.BytesIO()
    pillow_heif.from_pillow(fixture_image()).save(encoded, quality=90)
    data = encoded.getvalue()
    photo, truncated, damaged = folder / 'photo.heic', folder / 'truncated.heic', folder / 'damaged.heic'
    photo.write_bytes(data)
    truncated.write_bytes(data[:len(data) * 2 // 3])
    ftyp = int.from_bytes(data[:4], 'big')
    damaged.write_bytes(data[:ftyp] + os.urandom(len(data) - ftyp))
    return photo, truncated, damaged


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS decodes HEIC')
def test_the_confined_worker_decodes_the_phase21_photo_with_real_sips(tmp_path):
    photo, _, _ = phase21_fixture(tmp_path)
    result = json.loads(parse_document(photo, '--extract-image'))
    size, sides = describe('data:image/png;base64,' + result['data'])
    assert (result['width'], result['height']) == size == SIZE and matches(sides)


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS decodes HEIC')
def test_the_confined_worker_refuses_broken_heic_honestly_with_real_sips(tmp_path):
    _, truncated, damaged = phase21_fixture(tmp_path)
    for path in (truncated, damaged):
        with pytest.raises(ValueError) as error:
            parse_document(path, '--extract-image')
        text = str(error.value)
        assert HONEST.search(text) and not FALSE_LIMIT.search(text) and 'Traceback' not in text, text
