"""Synthetic-only layout fixtures: no external OCR/models needed."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pytest
from PIL import Image

from navigator.ingestion import ocr as module
from navigator.ingestion.common import Unit, build_fragments
from navigator.ingestion.pdf_parser import parse_pdf, _broken_encoding
from navigator.ingestion.docx_parser import parse_docx, _W
from test_ingestion import make_pdf


def rewrite_pdf(raw, *, rotation=0, footer=False, crop=False):
    """Rebuild artificial objects/xref using only the standard library."""
    import re
    objects = re.findall(rb'\d+ 0 obj\n(.*?)\nendobj', raw, re.S)
    out, offsets = b'%PDF-1.4\n', []
    for index, body in enumerate(objects, 1):
        if b'/Type /Page ' in body:
            body = body.replace(b'/MediaBox', f'/Rotate {rotation} /MediaBox'.encode())
        if crop and b'/Type /Page ' in body:
            body = body.replace(b'/MediaBox', b'/CropBox [50 50 550 800] /MediaBox')
        if footer and b'\nstream\n' in body:
            stream = body.split(b'\nstream\n', 1)[1].rsplit(b'\nendstream', 1)[0]
            stream += b'\nBT /F1 8 Tf 72 30 Td (1 Synthetic footnote retained for review.) Tj ET'
            body = f'<< /Length {len(stream)} >>\nstream\n'.encode()+stream+b'\nendstream'
        offsets.append(len(out))
        out += f'{index} 0 obj\n'.encode()+body+b'\nendobj\n'
    xref = len(out)
    out += f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    out += ''.join(f'{n:010d} 00000 n \n' for n in offsets).encode()
    out += f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
    return out


@pytest.fixture
def fake_engine(monkeypatch):
    import pytesseract
    monkeypatch.setattr(module, 'ocr_status', lambda *a: {'available': True, 'tessdata': None})
    monkeypatch.setattr(module, 'find_tesseract', lambda *a: 'synthetic-tesseract')
    monkeypatch.setattr(module, '_deskew_angle', lambda image: 0.)
    monkeypatch.setattr(pytesseract, 'image_to_data', lambda *a, **kw: {
        'text': ['Synthetic readable rule for the test page.'], 'conf': ['92'],
        'left': [10], 'top': [20], 'width': [30], 'height': [40],
        'block_num': [1], 'par_num': [1], 'line_num': [1]})
    return pytesseract


@pytest.mark.parametrize('rotation,expected', [
    (0, [5, 10, 20, 30]), (90, [10, 80, 30, 95]),
    (180, [30, 70, 45, 90]), (270, [20, 5, 40, 20]),
])
def test_four_osd_rotations_restore_display_coordinates(fake_engine, monkeypatch, rotation, expected):
    monkeypatch.setattr(fake_engine, 'image_to_osd', lambda *a, **kw:
                        {'rotate': rotation, 'orientation_conf': 18.})
    image = Image.new('RGB', (100, 200), 'white')
    result = module.make_ocr()(image, 2, page_size=(50, 100))
    assert result[0]['bbox'] == expected
    assert result.diagnostics['rotation'] == rotation
    assert result.diagnostics['confidence'] == 92
    assert not result.diagnostics['needs_review']
    assert image.size == (100, 200)


def test_uncertain_orientation_preserves_text_and_requires_review(fake_engine, monkeypatch):
    monkeypatch.setattr(fake_engine, 'image_to_osd', lambda *a, **kw:
                        {'rotate': 90, 'orientation_conf': 2.})
    result = module.make_ocr()(Image.new('RGB', (100, 200)), 1)
    assert result and result.diagnostics['rotation'] is None
    assert result.diagnostics['needs_review'] and result.diagnostics['warnings']


def test_ocr_error_is_not_reported_as_blank(fake_engine, monkeypatch):
    def fail(*a, **kw):
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(fake_engine, 'image_to_osd', fail)
    monkeypatch.setattr(fake_engine, 'image_to_data', fail)
    result = module.make_ocr()(Image.new('RGB', (100, 200)), 1)
    assert not result and result.diagnostics['source'] == 'error'
    assert result.diagnostics['needs_review']


def test_low_confidence_is_kept_for_review(fake_engine, monkeypatch):
    monkeypatch.setattr(fake_engine, 'image_to_osd', lambda *a, **kw:
                        {'rotate': 0, 'orientation_conf': 18.})
    original = fake_engine.image_to_data
    def data(*a, **kw):
        result = original(*a, **kw)
        result['conf'] = ['12']
        return result
    monkeypatch.setattr(fake_engine, 'image_to_data', data)
    result = module.make_ocr()(Image.new('RGB', (100, 200)), 1)
    assert result and result.diagnostics['needs_review']
    assert result.diagnostics['confidence'] == 12


def test_native_pdf_does_not_call_ocr(tmp_path):
    path = tmp_path / 'native.pdf'
    path.write_bytes(make_pdf(['1. A synthetic native rule with sufficient readable characters.']))
    def forbidden(*a):
        pytest.fail('native text must not call OCR')
    parsed = parse_pdf(path, ocr=forbidden)
    assert parsed.diagnostics[0]['source'] == 'native'
    assert not parsed.needs_review


@pytest.mark.parametrize('text', ['(cid:42)' * 20, '\ue000' * 40, '12345+-' * 10])
def test_broken_encoding_detection(text):
    assert _broken_encoding(text)


@pytest.mark.parametrize('rotation', [0, 90, 180, 270])
@pytest.mark.parametrize('crop', [False, True])
def test_pdf_rotation_is_already_in_rendered_page(tmp_path, rotation, crop):
    path = tmp_path / 'rotated.pdf'
    path.write_bytes(rewrite_pdf(make_pdf([None]), rotation=rotation, crop=crop))
    sizes = []
    def hook(image, number, page_size=None):
        sizes.append((image.size, page_size))
        return module.OCRResult([{'text': 'Synthetic OCR rule.', 'bbox': [0, 0, 10, 10]}],
                                source='ocr', rotation=0, warnings=[], needs_review=False)
    hook.supports_page_size = True
    parsed = parse_pdf(path, ocr=hook)
    expected = (842., 595.) if rotation in (90, 270) else (595., 842.)
    assert sizes[0][1] == expected
    assert abs(sizes[0][0][0]/sizes[0][0][1] - expected[0]/expected[1]) < .002
    assert parsed.diagnostics[0]['pdf_rotation'] == rotation


def test_small_deskew_inverse_uses_all_corners():
    import math
    angle = 2.
    before, after = (100, 200), (108, 204)
    # Center is invariant under centered rotation; a box is conservatively expanded.
    box = module._inverse_box([49, 97, 59, 107], [(angle,before,after)], before, before)
    assert (box[0]+box[2])/2 == pytest.approx(50, abs=.01)
    assert (box[1]+box[3])/2 == pytest.approx(100, abs=.01)
    assert box[2]-box[0] == pytest.approx(10*(math.cos(math.radians(angle))+math.sin(math.radians(angle))), abs=.02)


def test_docx_grid_keeps_horizontal_vertical_merges_and_empty_cells(tmp_path):
    import docx
    document = docx.Document()
    table = document.add_table(rows=4, cols=4)
    table.cell(0, 0).text = 'Header'
    table.cell(0, 2).text = 'Other'
    table.cell(1, 0).merge(table.cell(1, 1)).text = 'Merged'
    table.cell(1, 3).text = 'Last'
    table.cell(2, 0).merge(table.cell(3, 0)).text = 'Vertical'
    path = tmp_path / 'grid.docx'
    document.save(path)
    rows = parse_docx(path).blocks[0]['rows']
    assert all(len(row) == 4 for row in rows)
    assert rows[1] == ['Merged', '', '', 'Last']
    assert rows[2][0] == 'Vertical' and rows[3][0] == ''
    assert rows[3] == ['', '', '', '']


def test_docx_grid_before_does_not_shift_columns(tmp_path):
    import docx
    from docx.oxml import OxmlElement
    document = docx.Document()
    table = document.add_table(rows=2, cols=3)
    table.cell(0, 0).text = 'Header'
    table.cell(1, 1).text = 'Second column'
    tr = table.rows[1]._tr
    tr.remove(tr.tc_lst[0])
    before = OxmlElement('w:gridBefore')
    before.set(_W+'val', '1')
    tr.get_or_add_trPr().append(before)
    path = tmp_path / 'gap.docx'
    document.save(path)
    assert parse_docx(path).blocks[0]['rows'][1] == ['', 'Second column', '']


def footnote_docx(tmp_path, *, missing=False, orphan=False):
    import docx
    from docx.oxml import OxmlElement
    document = docx.Document()
    para = document.add_paragraph('1. Synthetic rule')
    ref = OxmlElement('w:footnoteReference')
    ref.set(_W+'id', '1')
    para.add_run()._r.append(ref)
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = 'Header'
    table.cell(1, 0).text = 'Table ground'
    ref2 = OxmlElement('w:footnoteReference')
    ref2.set(_W+'id', '2')
    table.cell(1, 0).paragraphs[0].add_run()._r.append(ref2)
    path = tmp_path / 'notes.docx'
    document.save(path)
    with ZipFile(path) as src:
        entries = {n: src.read(n) for n in src.namelist()}
    if not missing:
        entries['word/footnotes.xml'] = (f'<w:footnotes xmlns:w="{_W[1:-1]}">'
            '<w:footnote w:id="-1" w:type="separator"><w:p/></w:footnote>'
            '<w:footnote w:id="1"><w:p><w:r><w:t>First synthetic note.</w:t></w:r></w:p></w:footnote>'
            '<w:footnote w:id="2"><w:p><w:r><w:t>Table synthetic note.</w:t></w:r></w:p></w:footnote>'
            + ('<w:footnote w:id="3"><w:p><w:r><w:t>Orphan note.</w:t></w:r></w:p></w:footnote>' if orphan else '')
            + '</w:footnotes>').encode()
    with ZipFile(path, 'w', ZIP_DEFLATED) as dest:
        for name, data in entries.items():
            dest.writestr(name, data)
    return path


def test_docx_explicit_notes_are_reviewable_and_travel_with_grounds(tmp_path):
    parsed = parse_docx(footnote_docx(tmp_path))
    assert not parsed.needs_review
    assert len([b for b in parsed.blocks if b['kind'] == 'note']) == 2
    drafts = parsed.build()
    assert any('Synthetic rule[сноска:1]' in d.text and 'First synthetic note' in d.text for d in drafts)
    assert any('Table ground[сноска:2]' in d.text and 'Table synthetic note' in d.text for d in drafts)


@pytest.mark.parametrize('kwargs', [{'missing': True}, {'orphan': True}])
def test_missing_or_orphan_docx_note_is_flagged(tmp_path, kwargs):
    parsed = parse_docx(footnote_docx(tmp_path, **kwargs))
    assert parsed.needs_review and parsed.warnings


def test_pdf_note_is_not_removed_as_repeated_footer(tmp_path):
    # Handwritten synthetic PDF with a small-font footnote at the bottom, repeated on 3 pages.
    raw = make_pdf(['1. Synthetic rule [1] with enough ordinary body text.'] * 3)
    path = tmp_path / 'notes.pdf'
    path.write_bytes(rewrite_pdf(raw, footer=True))
    parsed = parse_pdf(path)
    assert not parsed.needs_review
    assert len([u for u in parsed.units if u.kind == 'note']) == 3
    assert all('footnote retained' in d.text for d in parsed.build() if 'Synthetic rule' in d.text)


def test_table_note_follows_every_split_and_budget_is_respected():
    parent = Unit(text='table', locator={'kind': 'docx', 'block': 0}, kind='table',
                  table_header='Header', rows=['synthetic row '*30 for _ in range(12)], note_refs=('n',))
    note = Unit(text='* Synthetic condition.', locator={'kind':'docx','block':1}, kind='note', note_id='n')
    drafts = build_fragments([parent, note])
    assert len(drafts) > 2
    assert all(note.text in d.text for d in drafts)
    assert all(len(d.text) <= 2400 for d in drafts)

def test_runtime_paths_accept_path_objects_without_quote_or_cache_errors(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    import pytesseract
    exe = tmp_path / 'tesseract.exe'
    exe.touch()
    data = tmp_path / 'tessdata'
    data.mkdir()
    calls = []
    def probe(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout='List of available languages (3):\neng\nrus\nosd\n')
    monkeypatch.setattr(subprocess, 'run', probe)
    monkeypatch.setattr(pytesseract, 'get_tesseract_version', lambda: 'synthetic-version')
    status = module.ocr_status(exe, data)
    assert status['available'] and status['osd_available']
    assert calls == [[str(exe), '--list-langs', '--tessdata-dir', str(data)]]
    import shlex
    config = module._config(data)
    assert '"' not in config if module.os.name == 'nt' else True
    expected = str(data).replace('\\', '/') if module.os.name == 'nt' else str(data)
    assert shlex.split(config)[1] == expected


def test_broken_native_layer_uses_ocr_and_retains_review_diagnostics(monkeypatch):
    import pdfplumber
    from types import SimpleNamespace
    page = SimpleNamespace(width=100, height=200, rotation=90,
                           extract_text=lambda: '(cid:42)'*20,
                           to_image=lambda **kw: SimpleNamespace(original=Image.new('RGB',(100,200))))
    monkeypatch.setattr(pdfplumber, 'open', lambda path: SimpleNamespace(pages=[page], close=lambda: None))
    def hook(image, number):
        return module.OCRResult([{'text': 'Synthetic uncertain OCR rule.', 'bbox': [1,2,30,40]}],
                                page=number, source='ocr', rotation=None, confidence=12.,
                                warnings=['Synthetic uncertainty'], needs_review=True)
    parsed = parse_pdf(Path('unused-synthetic.pdf'), ocr=hook)
    assert parsed.needs_review
    assert parsed.diagnostics[0]['source'] == 'ocr'
    assert parsed.diagnostics[0]['rotation'] is None
    assert parsed.diagnostics[0]['pdf_rotation'] == 90
    assert parsed.blocks[0]['text'] == 'Synthetic uncertain OCR rule.'
    assert len(parsed.warnings) >= 2


def test_deskew_abstains_for_blank_and_sparse_image():
    image = Image.new('RGB',(1000,1000),'white')
    assert module._deskew_angle(image) == 0


def test_docx_table_paragraph_boundaries_are_not_concatenated(tmp_path):
    import docx
    document = docx.Document()
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).text = 'First'
    table.cell(0, 0).add_paragraph('Second')
    path = tmp_path / 'paragraphs.docx'
    document.save(path)
    assert parse_docx(path).blocks[0]['rows'] == [['First Second']]

def test_ocr_footnote_requires_small_font_not_just_numbered_bottom_paragraph(fake_engine, monkeypatch):
    monkeypatch.setattr(fake_engine, 'image_to_osd', lambda *a, **kw:
                        {'rotate': 0, 'orientation_conf': 18.})
    monkeypatch.setattr(fake_engine, 'image_to_data', lambda *a, **kw: {
        'text': ['Synthetic body with explicit [1] marker.', 'Ordinary body.',
                 '2. Bottom numbered rule is still a rule.', '1 Synthetic note.'],
        'conf': ['92']*4, 'left': [10]*4, 'top': [20, 50, 145, 180],
        'width': [70]*4, 'height': [20, 20, 20, 10],
        'block_num': [1,2,3,4], 'par_num': [1]*4, 'line_num': [1]*4})
    result = module.make_ocr()(Image.new('RGB', (100,200)),1)
    assert [item['note'] for item in result] == [False,False,False,True]


def test_pdf_unresolved_note_is_preserved_and_flagged(tmp_path):
    path = tmp_path / 'unlinked.pdf'
    path.write_bytes(rewrite_pdf(make_pdf(['Synthetic rule with no footnote marker in body.']), footer=True))
    parsed = parse_pdf(path)
    assert parsed.needs_review and parsed.diagnostics[0]['warnings']
    assert any('Synthetic footnote' in draft.text for draft in parsed.build())
