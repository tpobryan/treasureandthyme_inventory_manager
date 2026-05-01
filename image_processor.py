import logging
from pathlib import Path
from PIL import Image, ImageOps, ImageEnhance

try:
    from pi_heif import register_heif_opener
    HEIF_BACKEND = "pi-heif"
except ImportError:
    try:
        from pillow_heif import register_heif_opener
        HEIF_BACKEND = "pillow-heif"
    except ImportError:
        register_heif_opener = None
        HEIF_BACKEND = ""

HEIF_SUPPORT_ENABLED = register_heif_opener is not None
if HEIF_SUPPORT_ENABLED:
    register_heif_opener()

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
if HEIF_SUPPORT_ENABLED:
    ALLOWED_EXTENSIONS.update({".heic", ".heif"})

MAX_IMAGE_DIMENSION = 1800
JPEG_QUALITY = 85

logger = logging.getLogger(__name__)

def optimize_image(source_path: Path, destination_path: Path) -> Path:
    """
    Open an uploaded image, auto-rotate it, convert to RGB if needed,
    resize to a sane max dimension, and save as optimized JPEG.
    Returns the final saved path.
    """
    with Image.open(source_path) as img:
        img = ImageOps.exif_transpose(img)

        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")

        width, height = img.size
        longest_side = max(width, height)

        if longest_side > MAX_IMAGE_DIMENSION:
            scale = MAX_IMAGE_DIMENSION / float(longest_side)
            new_size = (int(width * scale), int(height * scale))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        final_path = destination_path.with_suffix(".jpg")
        img.save(final_path, format="JPEG", quality=JPEG_QUALITY, optimize=True)

    return final_path

def apply_auto_enhance(source_path: Path, destination_path: Path) -> Path:
    """
    Open an image, apply autocontrast, slight color saturation boost,
    and slight sharpening. Saves back to destination_path.
    """
    with Image.open(source_path) as img:
        # 1. Autocontrast (removes haze/washes out by stretching histogram)
        img = ImageOps.autocontrast(img, cutoff=0.5)
        
        # 2. Color Vibrancy
        color_enhancer = ImageEnhance.Color(img)
        img = color_enhancer.enhance(1.15)
        
        # 3. Sharpness
        sharpness_enhancer = ImageEnhance.Sharpness(img)
        img = sharpness_enhancer.enhance(1.2)
        
        final_path = destination_path.with_suffix(".jpg")
        img.save(final_path, format="JPEG", quality=JPEG_QUALITY, optimize=True)

    return final_path