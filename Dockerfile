FROM python:3.11-slim

WORKDIR /app

# Native WeasyPrint/Pango PDF runtime. The package set is maintained in
# tools/pdf_native_runtime.sh — the single source of truth (it also handles
# newer-release package renames such as libgdk-pixbuf2.0-0 ->
# libgdk-pixbuf-4.0-0). --no-verify: WeasyPrint is pip-installed in a later
# layer, so the FULL-stack render gate below is what proves this image.
# System fonts (Noto core / Devanagari / color emoji) come from the same
# tool so Hindi renders in the WeasyPrint result PDF even without internet
# font fetching; a Hind subset is additionally @font-face bundled in-repo.
COPY tools/pdf_native_runtime.sh /app/tools/pdf_native_runtime.sh
RUN bash /app/tools/pdf_native_runtime.sh install --no-verify

COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev shared-mime-info \
    ffmpeg tesseract-ocr tesseract-ocr-eng tesseract-ocr-hin \
    && rm -rf /var/lib/apt/lists/*
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
