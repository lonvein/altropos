"""Recognize an artificial image-only PDF, never a real corpus document."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tesseract", required=True)
    parser.add_argument("--tessdata", required=True)
    args = parser.parse_args()
    from PIL import Image, ImageDraw, ImageFont
    from navigator.ingestion.ocr import make_ocr, ocr_status
    from navigator.ingestion.pdf_parser import parse_pdf
    status = ocr_status(args.tesseract, args.tessdata)
    assert status["available"] and status["osd_available"], status
    image = Image.new("RGB", (2480, 3508), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 40)
    lines = ["ИСКУССТВЕННЫЙ УЧЕБНЫЙ ДОКУМЕНТ", "1. Порядок подачи заявки"]
    for i in range(1, 21):
        lines += [f"1.{i}. Участник направляет заявку координатору мероприятия.",
                  "Координатор проверяет сведения и подтверждает получение заявки."]
    for index, line in enumerate(lines):
        draw.text((160, 180 + index * 65), line, font=font, fill="black")
    with tempfile.TemporaryDirectory(prefix="altropos-artificial-ocr-") as temp:
        path = Path(temp) / "synthetic-scan.pdf"
        image.save(path, "PDF", resolution=300)
        parsed = parse_pdf(path, ocr=make_ocr(args.tesseract, args.tessdata))
        text = " ".join(unit.text for unit in parsed.units)
        assert "заявку" in text.lower() and "координатор" in text.lower(), text[:300]
        print(json.dumps({"ocr": status, "units": len(parsed.units), "characters": len(text),
                          "needs_review": parsed.needs_review, "warnings": parsed.warnings}, ensure_ascii=False))
        print("PASS: artificial image-only PDF rendered and recognized with rus+eng")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
