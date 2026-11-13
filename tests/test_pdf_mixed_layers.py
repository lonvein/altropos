"""Artificial PDFs with native headers and large raster bodies; no real OCR."""
import pytest

from navigator.ingestion.pdf_parser import parse_pdf


HEADER = ('ARTIFICIAL NATIVE HEADER ' + 'approval signatures and dates ' * 4).rstrip()


def mixed_pdf(image_rectangles):
    # A one-pixel gray image is stretched to each rectangle; native text remains
    # readable to pdfplumber. This reproduces partial text overlays on scans.
    commands = ' '.join(f'q {w} 0 0 {h} {x} {y} cm /Im1 Do Q' for x, y, w, h in image_rectangles)
    stream = (commands + f' BT /F1 12 Tf 30 770 Td ({HEADER}) Tj ET').encode('ascii')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Contents 4 0 R '
        b'/Resources << /Font << /F1 5 0 R >> /XObject << /Im1 6 0 R >> >> >>',
        f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray '
        b'/BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream',
    ]
    data = b'%PDF-1.4\n'
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    data += b''.join(f'{offset:010} 00000 n \n'.encode() for offset in offsets)
    data += f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return data


@pytest.mark.parametrize('rectangles', [[(0, 0, 600, 800)], [(0, 0, 300, 400), (300, 0, 300, 400)]])
def test_large_image_with_native_header_uses_whole_page_ocr(tmp_path, rectangles):
    path = tmp_path / 'mixed.pdf'
    path.write_bytes(mixed_pdf(rectangles))
    calls = []
    def ocr(image, number):
        calls.append(number)
        return [{'text': HEADER, 'bbox': [30., 20., 560., 40.]},
                {'text': 'ARTIFICIAL RASTER BODY: omitted conditions now recognized.',
                 'bbox': [30., 100., 560., 700.]}]
    parsed = parse_pdf(path, ocr=ocr)
    assert calls == [1] and not parsed.needs_review
    text = '\n'.join(unit.text for unit in parsed.units)
    assert text.count(HEADER) == 1
    assert 'ARTIFICIAL RASTER BODY' in text
    assert parsed.diagnostics[0]['source'] == 'ocr'
    assert not any('изображением' in warning for warning in parsed.warnings)


@pytest.mark.parametrize('ocr', [None, lambda *_: [], lambda *_: (_ for _ in ()).throw(RuntimeError('OCR failed'))])
def test_mixed_page_without_successful_ocr_never_accepts_header_only(tmp_path, ocr):
    path = tmp_path / 'mixed.pdf'
    path.write_bytes(mixed_pdf([(0, 0, 600, 800)]))
    parsed = parse_pdf(path, ocr=ocr)
    assert parsed.needs_review
    assert parsed.units == []


def test_small_or_repeated_logo_does_not_force_ocr(tmp_path):
    path = tmp_path / 'logo.pdf'
    path.write_bytes(mixed_pdf([(20, 20, 40, 40)] * 150))
    def ocr(*_):
        pytest.fail('Small repeated logo must not be mistaken for raster body')
    parsed = parse_pdf(path, ocr=ocr)
    assert not parsed.needs_review
    assert parsed.diagnostics[0]['source'] == 'native'
    assert HEADER in '\n'.join(unit.text for unit in parsed.units)
