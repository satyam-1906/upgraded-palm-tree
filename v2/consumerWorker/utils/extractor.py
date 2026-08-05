import re, hashlib, io, os
from dotenv import load_dotenv
load_dotenv()
import fitz
from PIL import Image
import pytesseract
def clean(text):
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()

def extract(file_bytes):
    text= ""
    doc= fitz.open(stream=file_bytes, filetype="pdf")
    for page in doc:
        text += f'{page.get_text()}'
    return clean(text)

def extract_ocr(file_bytes):
    text = ""
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    for page in doc:
        pix = page.get_pixmap()
        img_bytes = pix.tobytes("png")
        image = Image.open(io.BytesIO(img_bytes))
        page_text = pytesseract.image_to_string(image)
        text += page_text
    return clean(text)

def extract_csv(file_bytes):
    pass

def hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


