"""Artificial numbering and marker fixtures; no corpus content or OCR executable."""
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from navigator.ingestion.ocr import _filled_bullet, _layout_groups, _reread_number, OCRResult
from navigator.ingestion.outline import Outline
from navigator.ingestion.pdf_parser import parse_pdf


def word(text, x, y, width, height=30, line=1, conf=92):
    return {'text':text,'box':[x,y,x+width,y+height],'line':line,'conf':conf}


def paragraph(words):
    return {'words':words,'heights':[w['box'][3]-w['box'][1] for w in words]}


@pytest.fixture
def layout():
    image = Image.new('RGB',(1200,1400),'white')
    draw = ImageDraw.Draw(image)
    paragraphs = {
        (1,1):paragraph([word('4.]',80,80,45,conf=80),word('Eligibility requires',190,80,300)]),
        (1,2):paragraph([word('all conditions:',80,130,300)]),
    }
    for index, label in enumerate(('a','@','o','9')):
        y = 220+index*70
        draw.ellipse((80,y+6,99,y+25),fill='black')
        paragraphs[(1,index+3)] = paragraph([word(label,80,y+6,20,20,conf=30),
                                             word(f'Synthetic condition {index+1}',190,y,380)])
    paragraphs[(2,1)] = paragraph([word('with an additional requirement',190,480,500)])
    paragraphs[(2,2)] = paragraph([word('and its final continuation.',190,520,500),
                                    word('4.2',80,590,45,line=2),word('Next independent point.',190,590,500,line=2)])
    return image,paragraphs


def test_number_consensus_ignores_non_number_and_does_not_set_review(layout):
    image, paragraphs = layout
    answers = iter([('4.',0),('4.1',89),('4.1',87)])
    diag = {'needs_review':False,'warnings':[]}
    groups = _layout_groups(paragraphs,image,lambda *args: next(answers),diag)
    assert groups[0]['text'] == '4.1 Eligibility requires all conditions:'
    assert groups[0]['raw_text'].startswith('4.]')
    assert groups[0]['layout_corrections'][0]['after'] == '4.1'
    assert not diag['needs_review']
    assert diag['warnings']
    bullets = [g for g in groups if g['list_item']]
    assert len(bullets) == 4
    assert all(g['text'].startswith('• ') for g in bullets)
    assert 'with an additional requirement and its final continuation.' in bullets[-1]['text']
    assert groups[-1]['text'] == '4.2 Next independent point.'
    assert not groups[-1]['list_item']


def test_conflicting_or_unreadable_local_numbers_abstain(layout):
    image,_ = layout
    for values in ([('7.3',90),('7.8',93),('7.4',90)], [('7.3',40)]*3):
        answers = iter(values)
        assert _reread_number(image,[80,80,130,110],lambda *args: next(answers)) is None


def test_real_zero_is_kept_when_local_readings_confirm_it(layout):
    image,_ = layout
    answers = iter([('7.0',82),('7.0',88)])
    assert _reread_number(image,[80,80,130,110],lambda *args: next(answers)) == '7.0'


def test_unconfirmed_number_is_retained_and_marked_for_review(layout):
    image, paragraphs = layout
    diag = {'needs_review':False,'warnings':[]}
    groups = _layout_groups(paragraphs,image,lambda *args: ('',0),diag)
    assert groups[0]['text'].startswith('4.] ')
    assert not groups[0]['layout_corrections']
    assert diag['needs_review'] and diag['warnings']


def test_numeric_column_gap_ignores_intervening_separator_noise(layout):
    image,_ = layout
    paragraphs = {(1,1):paragraph([word('7.0',80,80,45,conf=73),word('—',150,80,10),
                                    word('Synthetic body.',210,80,400)])}
    diag = {'needs_review':False,'warnings':[]}
    result = _layout_groups(paragraphs,image,lambda *args: ('7.9',82),diag)
    assert result[0]['text'].startswith('7.9 — ')
    assert not diag['needs_review']


def test_body_decimal_is_not_a_numbered_section(layout):
    image,_ = layout
    paragraphs = {(1,1):paragraph([word('1.0',80,80,45,conf=50),word('units are required.',135,80,400)])}
    def forbidden(*args):
        pytest.fail('body decimal must not be re-read as a point')
    result = _layout_groups(paragraphs,image,forbidden,{'needs_review':False,'warnings':[]})
    assert result[0]['text'].startswith('1.0 ')
    assert not result[0]['numbered']


def test_hollow_letter_or_digit_and_close_gap_are_not_bullets():
    image = Image.new('RGB',(1200,1400),'white')
    draw = ImageDraw.Draw(image)
    draw.ellipse((80,100,99,119),outline='black',width=3)
    marker = word('o',80,100,20,20)
    following = word('Synthetic text',190,100,300)
    assert not _filled_bullet(image,marker,following,30)
    draw.ellipse((80,100,99,119),fill='black')
    assert _filled_bullet(image,marker,following,30)
    assert not _filled_bullet(image,marker,word('Adjacent',105,100,300),30)


def test_list_hint_and_explicit_bullet_never_become_heading():
    outline = Outline()
    outline.feed('4. Synthetic section')
    path = outline.path
    for text, hint in [('9 Synthetic condition',True),('• SYNTHETIC CONDITION',False)]:
        info = outline.feed(text,style_heading=True,list_item=hint)
        assert info.kind == 'paragraph' and info.path == path and not info.section


def test_intro_and_all_four_complete_conditions_reach_same_fragment(layout,monkeypatch):
    image,paragraphs = layout
    groups = _layout_groups(paragraphs,image,lambda *args: ('4.1',90),{'needs_review':False,'warnings':[]})
    items = [{'text':g['text'],'bbox':g['box'],'list_item':g['list_item'],
              'raw_text':g['raw_text'],'layout_corrections':g['layout_corrections']} for g in groups]
    page = SimpleNamespace(width=1200,height=1400,rotation=0,extract_text=lambda: '',
                           to_image=lambda **kw: SimpleNamespace(original=image))
    import pdfplumber
    monkeypatch.setattr(pdfplumber,'open',lambda *args: SimpleNamespace(pages=[page],close=lambda: None))
    parsed = parse_pdf('unused.pdf',ocr=lambda *args: OCRResult(items,source='ocr',warnings=[],needs_review=False))
    bullets = [b for b in parsed.blocks if b.get('list_item')]
    assert len(bullets) == 4 and all(b['kind']=='paragraph' for b in bullets)
    fragments = parsed.build()
    draft = next(d for d in fragments if 'Eligibility' in d.text)
    assert draft.section == '4.1'
    assert all(f'Synthetic condition {i}' in draft.text for i in range(1,5))
    assert 'and its final continuation.' in draft.text
    assert not parsed.needs_review

def test_one_local_read_can_confirm_but_cannot_change_full_page_number(layout):
    image,_ = layout
    assert _reread_number(image,[80,80,130,110],lambda *a: ('7.6',89),'7.6',83) == '7.6'
    values = iter([('7.9',90),('',0),('',0)])
    assert _reread_number(image,[80,80,130,110],lambda *a: next(values),'7.6',83) is None


def test_uncertain_hollow_marker_is_preserved_and_requires_review(layout):
    image,paragraphs = layout
    ImageDraw.Draw(image).ellipse((80,226,99,245),fill='white',outline='black',width=2)
    groups = _layout_groups(paragraphs,image,lambda *a: ('4.1',90),diag := {'needs_review':False,'warnings':[]})
    assert any(g['text'].startswith('a Synthetic condition 1') for g in groups)
    assert diag['needs_review']
    assert any('маркер' in warning for warning in diag['warnings'])
