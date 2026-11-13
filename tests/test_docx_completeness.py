"""Artificial DOCX fixtures only: preservation, structural context and ambiguity."""
import pytest
from docx import Document
from docx.oxml import OxmlElement

from navigator.ingestion.common import MAX_CHARS, ParseError, Unit, build_fragments
from navigator.ingestion.docx_parser import parse_docx


def save(document, tmp_path):
    path = tmp_path / 'synthetic.docx'
    document.save(path)
    return path


def test_parenthesized_enumeration_is_content_and_every_item_survives(tmp_path):
    doc = Document()
    doc.add_paragraph('1. Synthetic directions')
    labels = [f'{i}) synthetic subject {i}' for i in range(1, 14)]
    for label in labels:
        doc.add_paragraph(label)
    parsed = parse_docx(save(doc, tmp_path))
    assert all(u.kind == 'paragraph' for u in parsed.units[1:])
    joined = '\n'.join(f.text for f in parsed.build())
    assert all(label in joined for label in labels)
    assert not parsed.needs_review


def test_consecutive_and_final_headings_are_not_dropped(tmp_path):
    doc = Document()
    for text in ('SYNTHETIC INTRODUCTION', 'SYNTHETIC SECOND TITLE', 'SYNTHETIC FINAL TITLE'):
        doc.add_heading(text, level=1)
    parsed = parse_docx(save(doc, tmp_path))
    drafts = parsed.build()
    assert [f.locator['block'] for f in drafts] == [0, 1, 2]
    assert all(u.text in '\n'.join(f.text for f in drafts) for u in parsed.units)


def test_multilevel_explicit_header_repeated_and_vertical_merges_explained(tmp_path):
    doc = Document()
    doc.add_heading('UNRELATED APPROVAL PAGE', level=1)
    table = doc.add_table(rows=62, cols=3)
    table.cell(0, 0).merge(table.cell(0, 2)).text = 'Synthetic register'
    for row in (0, 1):
        props = table.rows[row]._tr.get_or_add_trPr()
        props.append(OxmlElement('w:tblHeader'))
    for col, label in enumerate(('Synthetic owner', 'Synthetic category', 'Synthetic description')):
        table.cell(1, col).text = label
    table.cell(2, 0).merge(table.cell(61, 0)).text = 'Synthetic shared parent'
    for r in range(2, 62):
        table.cell(r, 1).text = f'Item-{r}'
        table.cell(r, 2).text = f'Unique artificial row {r} '+('description '*20)
    parsed = parse_docx(save(doc, tmp_path))
    unit = next(u for u in parsed.units if u.kind == 'table')
    drafts = [d for d in parsed.build() if unit.locator == d.locator]
    assert len(drafts) > 5
    assert all(all(label in d.text for label in ('Synthetic register', 'Synthetic owner',
                    'Synthetic category', 'Synthetic description', 'Synthetic shared parent')) for d in drafts)
    assert all('UNRELATED APPROVAL PAGE' not in d.text for d in drafts)
    assert all(len(d.text) <= MAX_CHARS for d in drafts)
    assert all(f'Unique artificial row {r}' in '\n'.join(d.text for d in drafts) for r in range(2, 62))
    block = next(b for b in parsed.blocks if b['kind'] == 'table')
    assert block['header_rows'] == 2 and block['rows'][3][0] == ''
    assert any(m.get('rowspan_from') == 2 for m in block['merges'])
    assert '[объединено с R3C1:' in '\n'.join(d.text for d in drafts)


def test_unmarked_title_blank_column_header_and_sparse_context(tmp_path):
    doc = Document()
    table = doc.add_table(rows=83, cols=4)
    table.cell(0, 0).merge(table.cell(0, 3)).text = 'Synthetic table caption'
    labels = ('Artificial parent', 'Artificial level', 'Artificial code', 'Artificial value')
    for col, label in enumerate(labels):
        table.cell(2, col).text = label
    table.cell(3, 0).text = 'Artificial group row'
    for r in range(4, 83):
        table.cell(r, 1).text = f'Child-{r}'
        table.cell(r, 2).text = f'Code-{r}'
        table.cell(r, 3).text = 'Artificial description '*9
    parsed = parse_docx(save(doc, tmp_path))
    drafts = parsed.build()
    assert len(drafts) > 5
    assert all('Synthetic table caption' in d.text and all(label in d.text for label in labels) for d in drafts)
    assert all('Artificial group row' in d.text for d in drafts)
    assert all('не значение пустых ячеек' in d.text for d in drafts)
    block = parsed.blocks[0]
    assert block['header_rows'] == 3 and block['rows'][4][0] == ''
    assert 'R5:  | Child-4' in '\n'.join(d.text for d in drafts)
    assert not parsed.needs_review


def add_superscript(paragraph, text):
    paragraph.add_run(text).font.superscript = True


def test_unmarked_multilevel_header_with_vertical_parent_and_horizontal_group(tmp_path):
    doc = Document()
    table = doc.add_table(rows=32, cols=3)
    table.cell(0, 0).merge(table.cell(1, 0)).text = 'Artificial key'
    table.cell(0, 1).merge(table.cell(0, 2)).text = 'Artificial grouped values'
    table.cell(1, 1).text = 'Artificial left value'
    table.cell(1, 2).text = 'Artificial right value'
    for row in range(2, 32):
        for col in range(3):
            table.cell(row, col).text = f'Synthetic data-{row}-{col} '+('text '*20)
    parsed = parse_docx(save(doc, tmp_path))
    assert parsed.blocks[0]['header_rows'] == 2
    assert parsed.blocks[0]['rows'][1][0] == ''
    assert all(all(label in d.text for label in ('Artificial key', 'Artificial grouped values',
                  'Artificial left value', 'Artificial right value')) for d in parsed.build())


def test_manual_superscripts_marked_linked_and_always_require_review(tmp_path):
    doc = Document()
    heading = doc.add_heading('Synthetic calculation', level=1)
    add_superscript(heading, '1')
    doc.add_paragraph('Artificial content. '*800)
    note = doc.add_paragraph()
    add_superscript(note, '1')
    note.add_run(' Artificial explanatory condition.')
    parsed = parse_docx(save(doc, tmp_path))
    drafts = parsed.build()
    grounds = [d for d in drafts if 'Synthetic calculation' in d.text]
    assert grounds and all('Artificial explanatory condition.' in d.text for d in grounds)
    assert all('[надстрочный:1]' in d.text for d in grounds)
    assert parsed.needs_review
    assert any(d.get('source') == 'manual_superscript' for d in parsed.diagnostics)
    assert any('ручные надстрочные' in w for w in parsed.warnings)


def test_ambiguous_repeated_manual_definitions_not_silently_associated(tmp_path):
    doc = Document()
    p = doc.add_paragraph('Synthetic ground')
    add_superscript(p, '2')
    for text in ('First artificial explanation', 'Second artificial explanation'):
        p = doc.add_paragraph()
        add_superscript(p, '2')
        p.add_run(text)
    parsed = parse_docx(save(doc, tmp_path))
    assert parsed.needs_review and any('неоднозначные' in w for w in parsed.warnings)
    ground = parsed.build()[0]
    assert 'First artificial explanation' not in ground.text
    assert 'Second artificial explanation' not in ground.text
    assert all(not u.note_id for u in parsed.units)


def test_missing_manual_explanation_blocks_and_preserves_marker(tmp_path):
    doc = Document()
    p = doc.add_paragraph('Synthetic quantity')
    add_superscript(p, '7')
    parsed = parse_docx(save(doc, tmp_path))
    assert parsed.needs_review
    assert '[надстрочный:7]' in parsed.build()[0].text


def test_table_header_survives_note_budget_resplit():
    header = 'Synthetic full header\nSynthetic second header line'
    table = Unit(text='table', locator={'kind': 'docx', 'block': 0}, kind='table',
                 table_header=header, rows=['Artificial row '+('word '*430)]*4, note_refs=('1',))
    note = Unit(text='Synthetic note '+('condition '*45), locator={'kind':'docx','block':1}, kind='note', note_id='1')
    drafts = [d for d in build_fragments([table, note]) if d.locator['block'] == 0]
    assert len(drafts) > 4
    assert all(d.text.startswith(header) and note.text in d.text for d in drafts)
    assert all(len(d.text) <= MAX_CHARS for d in drafts)


def test_table_mismatched_locators_are_rejected_instead_of_truncated():
    unit = Unit('synthetic', {'kind':'docx','block':0}, kind='table', rows=['First', 'Second'],
                row_locators=[{'kind':'docx','block':0}])
    with pytest.raises(ParseError, match='локаторов'):
        build_fragments([unit])


def test_oversized_header_flags_review_without_dropping_it():
    from navigator.ingestion.model import Parsed
    header = 'Artificial header '*200
    parsed = Parsed(media_type='docx', units=[Unit('table', {'kind':'docx','block':0},
                    kind='table', table_header=header, rows=['Artificial row'])])
    drafts = parsed.build()
    assert all(header in d.text for d in drafts)
    assert parsed.needs_review


def test_symbol_font_markers_survive_as_readable_unicode_in_text_and_table(tmp_path):
    doc = Document()
    doc.add_paragraph('\uf02d Synthetic supplies')
    paragraph = doc.add_paragraph()
    marker = OxmlElement('w:sym')
    from docx.oxml.ns import qn
    marker.set(qn('w:font'), 'Symbol')
    marker.set(qn('w:char'), 'F0B7')
    paragraph.add_run()._r.append(marker)
    paragraph.add_run(' Synthetic condition')
    table = doc.add_table(rows=2, cols=1)
    table.cell(0, 0).text = 'Synthetic header'
    table.cell(1, 0).text = '\uf02d Synthetic table item'
    parsed = parse_docx(save(doc, tmp_path))
    text = '\n'.join(d.text for d in parsed.build())
    assert '– Synthetic supplies' in text
    assert '• Synthetic condition' in text
    assert '– Synthetic table item' in text
    assert '\uf02d' not in text and '\uf0b7' not in text
    assert not parsed.needs_review


def test_unknown_private_font_symbol_is_preserved_and_blocks_readiness(tmp_path):
    doc = Document()
    doc.add_paragraph('Synthetic unknown symbol \uf123')
    parsed = parse_docx(save(doc, tmp_path))
    assert '\uf123' in parsed.units[0].text
    assert parsed.needs_review and any('символы шрифта' in w for w in parsed.warnings)
