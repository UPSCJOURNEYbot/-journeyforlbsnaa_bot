FROM python:3.11-slim

# System deps required by weasyprint (PDF generation) and PyMuPDF.
# fonts-noto-core/fonts-deva provide Noto Sans Devanagari so Hindi renders
# in the WeasyPrint result PDF even without internet font fetching.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libcairo2 \
    libffi-dev shared-mime-info fonts-liberation fonts-noto-core fonts-deva \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

# Fail the image build if the WeasyPrint stack cannot render (pins protect
# against transitive drift such as pydyf 0.12 removing Stream.transform,
# which broke every result PDF with "'super' object has no attribute
# 'transform'"). Runs after pango/cairo/Noto fonts are installed above.
RUN python -c "import pydyf; assert hasattr(pydyf.Stream, 'transform') and tuple(int(x) for x in pydyf.__version__.split('.')[:2]) < (0, 12), pydyf.__version__; \
from weasyprint import HTML; \
pdf = HTML(string=\"<h1>ok</h1><p>PDF smoke हिन्दी</p><div style='transform:rotate(3deg)'>r</div>\").write_pdf(); \
assert pdf[:5] == b'%PDF-'; \
import fitz; assert fitz.open(stream=pdf, filetype='pdf').page_count >= 1; \
print('Docker PDF stack render gate OK')"

COPY . .

RUN mkdir -p /app/data

# Mini App server port (only listens if MINI_APP_DOMAIN is configured / the
# miniapp component is actually started -- harmless to expose otherwise).
EXPOSE 8080

CMD ["python", "run.py"]
