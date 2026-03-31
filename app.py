import base64
import csv
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import zipfile
import zlib
from datetime import datetime, timedelta
from functools import wraps
from html import escape as html_escape
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from flask import Flask, jsonify, make_response, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent


def runtime_path(env_name: str, default_path: Path) -> Path:
    configured = (os.environ.get(env_name) or "").strip()
    if not configured:
        return default_path
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else (BASE_DIR / candidate).resolve()


DATA_DIR = runtime_path("TPA_DATA_DIR", BASE_DIR)
DATABASE_PATH = runtime_path("TPA_DATABASE_PATH", DATA_DIR / "database.db")
UPLOADS_DIR = runtime_path("TPA_UPLOADS_DIR", DATA_DIR / "uploads")
STUDENT_PHOTOS_DIR = UPLOADS_DIR / "student_photos"
FACULTY_PHOTOS_DIR = UPLOADS_DIR / "faculty_photos"
RESULTS_DIR = UPLOADS_DIR / "results"
GALLERY_IMAGES_DIR = UPLOADS_DIR / "gallery_images"
GENERATED_FORMS_DIR = DATA_DIR / "generated_forms"

PRIMARY_COLOR = "#0a1929"
ACCENT_COLOR = "#f0b90b"
ADMIN_SESSION_LIFETIME_DAYS = 30
ADMIN_REMEMBER_LIFETIME_DAYS = 180
ADMIN_REMEMBER_COOKIE_NAME = "tpa_admin_remember"
MAX_STUDENT_PHOTO_SIZE = 300 * 1024  # 300 KB
MAX_FACULTY_PHOTO_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_RESULT_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_GALLERY_IMAGE_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_BACKUP_FILE_SIZE = 60 * 1024 * 1024  # 60 MB
MAX_VISITOR_MESSAGE_WORDS = 250
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png"}
ALLOWED_RESULT_EXTENSIONS = {"pdf"}
ALLOWED_GENDERS = {"Male", "Female", "Other"}
VALID_CLASS_CHOICES = {"IX", "X", "XI", "XII", "MDCAT Prep", "ECAT Prep"}
ENROLLMENT_CLASS_CHOICES = ["IX", "X", "XI", "XII", "MDCAT Prep", "ECAT Prep"]
FACULTY_SECTION_CHOICES = ["Class IX-X", "XI-XII | Pre-Med", "XI-XII | Pre-Eng", "MDCAT", "ECAT"]
TEXT_SANITIZE_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
PDF_ACTIVE_CONTENT_MARKERS = (
    b"/javascript",
    b"/js",
    b"/launch",
    b"/openaction",
    b"/submitform",
    b"/richmedia",
    b"/embeddedfile",
    b"/xfa",
    b"/aa",
)
EXECUTABLE_SIGNATURES = (
    b"MZ",
    b"\x7fELF",
    b"PK\x03\x04",
    b"#!",
)
RATE_LIMIT_RULES = {
    "admin_login": {"limit": 8, "window_seconds": 10 * 60, "message": "Too many login attempts. Please wait a few minutes and try again."},
    "enrollment_submit": {"limit": 6, "window_seconds": 15 * 60, "message": "Too many enrollment submissions from this connection. Please try again later."},
    "enrollment_status": {"limit": 20, "window_seconds": 5 * 60, "message": "Too many status checks right now. Please wait a few minutes and try again."},
    "enrollment_form_download": {"limit": 10, "window_seconds": 10 * 60, "message": "Too many form download attempts right now. Please try again later."},
    "visitor_message_submit": {"limit": 6, "window_seconds": 15 * 60, "message": "Too many messages from this connection. Please try again later."},
    "analytics_visit": {"limit": 240, "window_seconds": 10 * 60, "message": "Too many analytics events right now. Please wait a moment and try again."},
    "analytics_presence": {"limit": 1200, "window_seconds": 10 * 60, "message": "Too many live visitor updates right now. Please wait a moment and try again."},
}
RATE_LIMIT_STATE: Dict[str, List[float]] = {}
CHROMIUM_PDF_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]
PDF_BROWSER_BASE_ARGS = [
    "--disable-gpu",
    "--allow-file-access-from-files",
    "--disable-background-networking",
    "--disable-default-apps",
    "--disable-sync",
    "--metrics-recording-only",
    "--mute-audio",
    "--no-default-browser-check",
    "--no-first-run",
]
ADMISSION_FORM_CACHE_VERSION = "v7"

LEGACY_DEFAULT_ADMIN_USERNAME = "thepamirpurkhas"
LEGACY_DEFAULT_ADMIN_PASSWORD = "tpamirpurkhas"
DEFAULT_ADMIN_USERNAME = "admintpa0109"
DEFAULT_ADMIN_PASSWORD = "9010admintpa"
DEFAULT_MAP_EMBED_URL = "https://www.google.com/maps?q=25.5093141,69.0190305&z=18&hl=en&output=embed"
LEGACY_MAP_EMBED_URL = "https://www.google.com/maps?q=Mirpur%20Khas%20Sindh%20Pakistan&output=embed"
LEGACY_ADMIN_PANEL_PATH = "/adminpanel1010tpa"
ADMIN_PANEL_PATH = "/adminpanel0109tpa2026"
ADMIN_PATH_PLACEHOLDER = "__ADMIN_PATH__"
POPUP_SECTION_CHOICES = {"", "home", "enrollment", "faculty", "results", "announcements", "about", "status-check"}
ANALYTICS_SECTION_CHOICES = {"home", "enrollment", "status-check", "faculty", "results", "announcements", "about"}
LIVE_VISITOR_WINDOW_MINUTES = 3
LIVE_VISITOR_RETENTION_HOURS = 24
HOSTED_RUNTIME_ENV_KEYS = (
    "RAILWAY_PROJECT_ID",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_SERVICE_ID",
    "RENDER",
    "RENDER_SERVICE_ID",
    "VERCEL",
    "DYNO",
)

SECONDARY_SUBJECTS = ["English", "Maths", "Biology", "Physics", "Chemistry"]
HIGHER_SECONDARY_GROUPS = {
    "Pre-Medical": ["English", "Botany", "Zoology", "Physics", "Chemistry"],
    "Pre-Engineering": ["Maths", "English", "Physics", "Chemistry"],
}

DEFAULT_SETTINGS = {
    "contact_primary": "+92 300 1234567",
    "contact_secondary": "+92 333 7654321",
    "office_timing": "10:00 am to 08:00 pm",
    "address": "The Professors Academy, Near Satellite Town, Mirpur Khas, Sindh, Pakistan",
    "email": "info@theprofessorsacademy.edu.pk",
    "facebook_url": "",
    "whatsapp_enabled": "1",
    "whatsapp_number": "923001234567",
    "whatsapp_message": "Assalam o Alaikum, I would like to get admission information from The Professors Academy.",
    "hero_badge": "Premium Academic Coaching",
    "hero_heading": "Shape stronger futures with discipline, mentorship, and results.",
    "hero_description": "The Professors Academy is built for students who want focused preparation, supportive faculty, and a polished learning environment from Class IX through Class XII.",
    "hero_overlay_title": "Admissions are open for the 2026-27 session.",
    "hero_overlay_description": "Submit your enrollment online and our team will follow up with the next academic steps.",
    "enrollment_info_badge": "Why Families Choose Us",
    "enrollment_info_heading": "A polished academic environment, built around consistency.",
    "enrollment_info_description": "We blend strong classroom discipline with approachable faculty support so students grow with confidence.",
    "enrollment_card_1_label": "Focused Streams",
    "enrollment_card_1_title": "Science Ready",
    "enrollment_card_1_description": "Clear pathways for Pre-Medical and Pre-Engineering students with guided subject selection.",
    "enrollment_card_2_label": "Fast Processing",
    "enrollment_card_2_title": "Digital Admission",
    "enrollment_card_2_description": "Your form reaches the administration instantly through the secure enrollment system.",
    "enrollment_card_3_label": "Parent Confidence",
    "enrollment_card_3_title": "Transparent Communication",
    "enrollment_card_3_description": "Announcements, contact details, and result uploads remain easy to access in one place.",
    "motion_enabled": "1",
    "dark_mode_enabled": "0",
    "home_stats_enabled": "1",
    "home_announcements_enabled": "1",
    "home_message_enabled": "1",
    "home_gallery_enabled": "1",
    "home_faq_enabled": "1",
    "message_badge": "Message From The Academy",
    "message_heading": "A professional academic message for students and parents.",
    "message_description": "We remain committed to disciplined teaching, transparent guidance, and a learning environment where students can grow with confidence and consistency.",
    "message_author_name": "Admin Office",
    "message_author_title": "The Professors Academy",
    "gallery_badge": "Academy Gallery",
    "gallery_heading": "Campus, classroom, event, and result day highlights",
    "gallery_description": "A quick visual glimpse of the learning environment, academic activity, and celebratory moments at The Professors Academy.",
    "gallery_item_1_label": "Campus",
    "gallery_item_1_title": "Welcoming academy environment",
    "gallery_item_1_description": "A clean and focused academy atmosphere that supports disciplined study and daily academic routine.",
    "gallery_item_1_image": "https://images.unsplash.com/photo-1498243691581-b145c3f54a5a?auto=format&fit=crop&w=1000&q=80",
    "gallery_item_2_label": "Classrooms",
    "gallery_item_2_title": "Structured classroom learning",
    "gallery_item_2_description": "Subject-focused teaching spaces designed for clarity, attention, and consistent classroom engagement.",
    "gallery_item_2_image": "https://images.unsplash.com/photo-1523240795612-9a054b0db644?auto=format&fit=crop&w=1000&q=80",
    "gallery_item_3_label": "Events",
    "gallery_item_3_title": "Seminars and academic gatherings",
    "gallery_item_3_description": "Important academy moments, student briefings, and educational events that build confidence beyond the classroom.",
    "gallery_item_3_image": "https://images.unsplash.com/photo-1513258496099-48168024aec0?auto=format&fit=crop&w=1000&q=80",
    "gallery_item_4_label": "Result Day",
    "gallery_item_4_title": "Achievements worth celebrating",
    "gallery_item_4_description": "Academic results, recognition, and progress updates shared with students and families in a proud setting.",
    "gallery_item_4_image": "https://images.unsplash.com/photo-1522202176988-66273c2fd55f?auto=format&fit=crop&w=1000&q=80",
    "faq_badge": "Helpful Answers",
    "faq_heading": "Frequently asked questions",
    "faq_description": "Quick answers for parents and students who want admission clarity before visiting the academy office.",
    "faq_item_1_question": "How do I apply for admission online?",
    "faq_item_1_answer": "Fill in the enrollment form, upload the required passport size picture, and submit the form online. The academy team will review it and contact you with the next step.",
    "faq_item_2_question": "How can I check my enrollment status?",
    "faq_item_2_answer": "Use the status check section with your CNIC number and date of birth in DD/MM/YYYY format to view whether your application is pending, confirmed, or rejected.",
    "faq_item_3_question": "When should I visit the academy office?",
    "faq_item_3_answer": "Once your admission is confirmed, download the form and visit the admin office with the printed copy and the required fee during office timing.",
    "faq_item_4_question": "Can I view results and notices online?",
    "faq_item_4_answer": "Yes. Results and announcements are published on the website, so students and parents can stay informed without waiting for manual updates.",
    "map_embed_url": DEFAULT_MAP_EMBED_URL,
    "enrollment_enabled": "1",
    "enrollment_class_allowlist": json.dumps(ENROLLMENT_CLASS_CHOICES),
    "enrollment_closed_message": "Admissions are currently closed. They will open again from 24th Mar 2027.",
    "marquee_enabled": "1",
    "marquee_text": "Admissions are open now. Contact The Professors Academy for the current opening and closing dates.",
    "status_check_enabled": "1",
    "status_check_disabled_message": "Enrollment status checking is currently unavailable. Please contact the academy office for assistance.",
    "status_message_pending": "Your enrollment is currently under review. We will contact you after verification.",
    "status_message_confirmed": "Your enrollment has been confirmed. Please stay connected with the academy for the next admission steps.",
    "status_message_rejected": "Your enrollment could not be approved at this time. Please contact the admin or visit the academy office for guidance.",
    "status_message_not_found": "No enrollment record matched the provided CNIC number and date of birth. Please check the details and try again.",
    "admission_form_note": "Please submit a printed copy of this form with Rs. 5000 at the admin office to complete the admission process.",
    "homepage_popup_enabled": "1",
    "homepage_popup_title": "Admissions & Results Update",
    "homepage_popup_message": "Admissions, announcements, and latest results are available on the website. Please review the latest update by clicking the button below.",
    "homepage_popup_button_label": "See",
    "homepage_popup_target_section": "results",
    "homepage_popup_result_id": "",
}

SAMPLE_ANNOUNCEMENTS = [
    ("Summer Vacations 2026", "Academy will remain closed for summer break from June 1, 2026, to June 15, 2026.", "2026-06-01"),
    ("Admissions Open 2026-27", "Admissions are now open for the 2026-27 academic session. Visit the enrollment section to apply online.", "2026-03-15"),
    ("Scholarship Interviews", "Merit scholarship interviews for high achievers will be conducted on April 10, 2026, in the main seminar hall.", "2026-04-10"),
]

SAMPLE_FACULTY = [
    ("Prof. Ahmed Ali", None, "XI-XII | Pre-Eng", "Mathematics", "M.Phil Mathematics", "8 Years+", 1),
    ("Dr. Fatima Khan", None, "Class IX-X", "Biology", "PhD Biological Sciences", "6 Years+", 2),
]

SAMPLE_RESULT_FILE = "annual_exam_2025_result_sample.pdf"
SAMPLE_RESULTS = [
    ("Annual Exam 2025 Result", "Class X", "2025", SAMPLE_RESULT_FILE, "2026-01-15 10:00:00"),
]


def is_hosted_runtime() -> bool:
    return any(str(os.environ.get(key) or "").strip() for key in HOSTED_RUNTIME_ENV_KEYS)


def configured_admin_seed_credentials() -> Optional[Tuple[str, str]]:
    username = str(os.environ.get("TPA_ADMIN_USERNAME") or "").strip()
    password = os.environ.get("TPA_ADMIN_PASSWORD") or ""
    if username and password:
        return username, password
    return None


def create_app() -> Flask:
    secure_cookies_enabled = str(os.environ.get("TPA_SECURE_COOKIES") or "").strip().lower() in {"1", "true", "yes", "on"}
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        SESSION_COOKIE_NAME="tpa_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies_enabled,
        PERMANENT_SESSION_LIFETIME=timedelta(days=ADMIN_SESSION_LIFETIME_DAYS),
        JSON_SORT_KEYS=False,
    )
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    return app


app = create_app()


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA trusted_schema = OFF")
    except sqlite3.DatabaseError:
        pass
    return connection


def current_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def normalize_admin_flag(value: Any, default: bool = False) -> str:
    if value is None:
        return "1" if default else "0"
    return "1" if str(value).strip() in {"1", "true", "True", "on", "yes"} else "0"


def clear_generated_form_cache(student_id: Optional[Any] = None) -> None:
    try:
        if student_id is None:
            for cached_path in GENERATED_FORMS_DIR.glob("admission_form_*.pdf"):
                try:
                    cached_path.unlink()
                except OSError:
                    continue
            return
        safe_id = safe_filename_fragment(str(student_id or "record"), "record")
        for cached_path in GENERATED_FORMS_DIR.glob(f"admission_form_{safe_id}_*.pdf"):
            try:
                cached_path.unlink()
            except OSError:
                continue
    except Exception:
        pass


def parse_dateish_year(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y")
        except ValueError:
            continue
    return datetime.now().strftime("%Y")


def current_roll_year_prefix(date_value: Optional[str] = None) -> str:
    return parse_dateish_year(date_value)


def roll_year_display_prefix(date_value: Optional[str] = None) -> str:
    return f"2k{current_roll_year_prefix(date_value)[-2:]}"


def roll_group_code(class_name: str, student_group: Optional[str] = None) -> Optional[str]:
    normalized_class = str(class_name or "").strip()
    normalized_group = str(student_group or "").strip().upper()
    if normalized_class in {"XI", "XII"}:
        if normalized_group in {"PRE-MEDICAL", "P.M"}:
            return "P.M"
        if normalized_group in {"PRE-ENGINEERING", "P.E"}:
            return "P.E"
        return "GEN"
    if normalized_class == "MDCAT Prep":
        return "MDCAT"
    if normalized_class == "ECAT Prep":
        return "ECAT"
    return None


def roll_number_counter_key(class_name: str, student_group: Optional[str] = None) -> str:
    normalized = (class_name or "").strip()
    if normalized in {"XI", "XII"}:
        return f"{normalized}:{roll_group_code(normalized, student_group)}"
    if normalized == "MDCAT Prep":
        return "MDCAT"
    if normalized == "ECAT Prep":
        return "ECAT"
    return normalized or "GENERAL"


def roll_sequence_bucket(class_name: str, date_value: Optional[str] = None, student_group: Optional[str] = None) -> str:
    return f"{current_roll_year_prefix(date_value)}:{roll_number_counter_key(class_name, student_group)}"


def build_roll_prefix(class_name: str, date_value: Optional[str] = None, student_group: Optional[str] = None) -> str:
    year_prefix = roll_year_display_prefix(date_value)
    normalized_class = (class_name or "").strip()
    if normalized_class in {"IX", "X"}:
        return f"{year_prefix}/{normalized_class}/"
    if normalized_class in {"XI", "XII"}:
        return f"{year_prefix}/{normalized_class}/{roll_group_code(normalized_class, student_group)}/"
    if normalized_class == "MDCAT Prep":
        return f"{year_prefix}/MDCAT/"
    if normalized_class == "ECAT Prep":
        return f"{year_prefix}/ECAT/"
    safe_class = re.sub(r"[^A-Za-z0-9]+", "", normalized_class).upper() or "GENERAL"
    return f"{year_prefix}/{safe_class}/"


def build_legacy_roll_prefix(class_name: str, date_value: Optional[str] = None, student_group: Optional[str] = None) -> str:
    legacy_year = f"2k{current_roll_year_prefix(date_value)[-2:]}"
    normalized_class = (class_name or "").strip()
    if normalized_class in {"IX", "X"}:
        return f"{legacy_year}/{normalized_class}/"
    if normalized_class in {"XI", "XII"}:
        return f"{legacy_year}/{normalized_class}/{roll_group_code(normalized_class, student_group)}/"
    if normalized_class == "MDCAT Prep":
        return f"{legacy_year}/MDCAT/"
    if normalized_class == "ECAT Prep":
        return f"{legacy_year}/ECAT/"
    return f"{legacy_year}/"


def build_roll_number(class_name: str, sequence: int, date_value: Optional[str] = None, student_group: Optional[str] = None) -> str:
    return f"{build_roll_prefix(class_name, date_value, student_group)}{sequence:03d}"


def extract_roll_sequence(
    roll_number: Any,
    class_name: str,
    date_value: Optional[str] = None,
    student_group: Optional[str] = None,
) -> Optional[int]:
    normalized_roll = str(roll_number or "").strip()
    prefix = build_roll_prefix(class_name, date_value, student_group)
    if not normalized_roll.startswith(prefix):
        return None
    suffix = normalized_roll[len(prefix):].strip()
    if not suffix.isdigit():
        return None
    return int(suffix)


def upgrade_legacy_roll_numbers(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT id, class_name, student_group, roll_number, created_at
        FROM students
        WHERE roll_number IS NOT NULL AND TRIM(roll_number) <> ''
        """
    ).fetchall()
    for row in rows:
        legacy_prefix = build_legacy_roll_prefix(row["class_name"], row["created_at"], row["student_group"])
        normalized_roll = str(row["roll_number"] or "").strip()
        if not normalized_roll.startswith(legacy_prefix):
            continue
        suffix = normalized_roll[len(legacy_prefix):].strip()
        if not suffix.isdigit():
            continue
        updated_roll = build_roll_number(row["class_name"], int(suffix), row["created_at"], row["student_group"])
        if updated_roll != normalized_roll:
            connection.execute("UPDATE students SET roll_number = ? WHERE id = ?", (updated_roll, row["id"]))


def next_roll_number_for_class(
    connection: sqlite3.Connection,
    class_name: str,
    created_at: Optional[str] = None,
    student_group: Optional[str] = None,
) -> str:
    target_bucket = roll_sequence_bucket(class_name, created_at, student_group)
    rows = connection.execute("SELECT class_name, student_group, roll_number, created_at FROM students").fetchall()
    max_sequence = 0
    for row in rows:
        if roll_sequence_bucket(row["class_name"], row["created_at"], row["student_group"]) != target_bucket:
            continue
        sequence = extract_roll_sequence(row["roll_number"], row["class_name"], row["created_at"], row["student_group"])
        if sequence is not None:
            max_sequence = max(max_sequence, sequence)
    return build_roll_number(class_name, max_sequence + 1, created_at, student_group)


def assign_missing_roll_numbers(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT id, class_name, student_group, roll_number, created_at
        FROM students
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()
    if not rows:
        return

    sequences_by_bucket: Dict[str, int] = {}
    for row in rows:
        bucket = roll_sequence_bucket(row["class_name"], row["created_at"], row["student_group"])
        next_sequence = sequences_by_bucket.get(bucket, 0) + 1
        sequences_by_bucket[bucket] = next_sequence
        normalized_roll_number = build_roll_number(
            row["class_name"],
            next_sequence,
            row["created_at"],
            row["student_group"],
        )
        if str(row["roll_number"] or "").strip() != normalized_roll_number:
            connection.execute(
                "UPDATE students SET roll_number = ? WHERE id = ?",
                (normalized_roll_number, row["id"]),
            )


def ensure_directories() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    for directory in (DATA_DIR, UPLOADS_DIR, STUDENT_PHOTOS_DIR, FACULTY_PHOTOS_DIR, RESULTS_DIR, GALLERY_IMAGES_DIR, GENERATED_FORMS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def generate_simple_pdf(text: str) -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT\n/F1 18 Tf\n72 720 Td\n({escaped}) Tj\nET"
    stream_bytes = stream.encode("latin-1")
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream_bytes)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    chunks = ["%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part.encode("latin-1")) for part in chunks))
        chunks.append(f"{index} 0 obj\n{obj}\nendobj\n")

    startxref = sum(len(part.encode("latin-1")) for part in chunks)
    chunks.append("xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        chunks.append(f"{offset:010d} 00000 n \n")
    chunks.append(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF")
    return "".join(chunks).encode("latin-1")


PDF_INK_COLOR = "0.071 0.141 0.227"
PDF_PRIMARY_COLOR = "0.039 0.098 0.161"
PDF_PRIMARY_DARK = "0.027 0.078 0.141"
PDF_GOLD_COLOR = "0.941 0.725 0.043"
PDF_MUTED_COLOR = "0.365 0.427 0.510"
PDF_LINE_COLOR = "0.831 0.863 0.902"
PDF_SOFT_FILL = "0.978 0.984 0.996"
PDF_SOFT_GOLD = "0.996 0.969 0.878"
PDF_SOFT_BLUE = "0.933 0.961 0.996"
PDF_WHITE = "1 1 1"


class PDFDocumentBuilder:
    def __init__(self) -> None:
        self.objects: List[bytes] = []

    def reserve_object(self) -> int:
        self.objects.append(b"")
        return len(self.objects)

    def set_object(self, object_id: int, payload: str | bytes) -> None:
        self.objects[object_id - 1] = payload if isinstance(payload, bytes) else payload.encode("latin-1")

    def add_object(self, payload: str | bytes) -> int:
        self.objects.append(payload if isinstance(payload, bytes) else payload.encode("latin-1"))
        return len(self.objects)

    def add_stream_object(self, dictionary_entries: str, stream_bytes: bytes) -> int:
        payload = (
            f"<< {dictionary_entries} /Length {len(stream_bytes)} >>\nstream\n".encode("latin-1")
            + stream_bytes
            + b"\nendstream"
        )
        return self.add_object(payload)

    def build(self, root_object_id: int) -> bytes:
        document = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for object_id, payload in enumerate(self.objects, start=1):
            offsets.append(len(document))
            document.extend(f"{object_id} 0 obj\n".encode("latin-1"))
            document.extend(payload)
            document.extend(b"\nendobj\n")

        xref_offset = len(document)
        document.extend(f"xref\n0 {len(self.objects) + 1}\n".encode("latin-1"))
        document.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            document.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
        document.extend(
            f"trailer\n<< /Size {len(self.objects) + 1} /Root {root_object_id} 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode(
                "latin-1"
            )
        )
        return bytes(document)


def pdf_escape_text(value: Any) -> str:
    normalized = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    safe_text = normalized.encode("latin-1", "replace").decode("latin-1")
    return safe_text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def wrap_pdf_text(value: Any, width: float, font_size: float, max_lines: Optional[int] = None) -> List[str]:
    normalized = re.sub(r"\s+", " ", str(value or "").strip()) or "N/A"
    approx_char_width = max(font_size * 0.52, 1.0)
    max_chars = max(8, int(width / approx_char_width))
    words = normalized.split(" ")
    lines: List[str] = []
    current = ""

    for word in words:
        if len(word) > max_chars:
            if current:
                lines.append(current)
                current = ""
            for start in range(0, len(word), max_chars):
                lines.append(word[start : start + max_chars])
            continue
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        last_line = lines[-1]
        lines[-1] = f"{last_line[: max(1, max_chars - 3)].rstrip()}..."
    return lines or ["N/A"]


def add_pdf_text_block(
    commands: List[str],
    lines: List[str],
    x: float,
    y: float,
    *,
    font: str = "F1",
    size: float = 12,
    color: str = PDF_INK_COLOR,
    leading: Optional[float] = None,
) -> None:
    safe_lines = [pdf_escape_text(line) for line in lines if str(line).strip()]
    if not safe_lines:
        return
    line_height = leading or max(size * 1.35, size + 2)
    buffer = [
        "BT",
        f"{color} rg",
        f"/{font} {size:.2f} Tf",
        f"{line_height:.2f} TL",
        f"1 0 0 1 {x:.2f} {y:.2f} Tm",
    ]
    for index, line in enumerate(safe_lines):
        if index:
            buffer.append("T*")
        buffer.append(f"({line}) Tj")
    buffer.append("ET")
    commands.append("\n".join(buffer))


def add_pdf_rectangle(
    commands: List[str],
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    stroke_color: Optional[str] = PDF_LINE_COLOR,
    fill_color: Optional[str] = None,
    line_width: float = 1,
) -> None:
    buffer = ["q"]
    if fill_color:
        buffer.append(f"{fill_color} rg")
    if stroke_color:
        buffer.append(f"{stroke_color} RG")
        buffer.append(f"{line_width:.2f} w")
    paint_operator = "B" if fill_color and stroke_color else "f" if fill_color else "S"
    buffer.append(f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re {paint_operator}")
    buffer.append("Q")
    commands.append("\n".join(buffer))


def add_pdf_detail_box(
    commands: List[str],
    label: str,
    value: Any,
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    max_lines: int = 2,
    fill_color: str = PDF_SOFT_FILL,
    stroke_color: Optional[str] = PDF_LINE_COLOR,
    label_color: str = PDF_MUTED_COLOR,
    value_color: str = PDF_INK_COLOR,
    label_size: float = 8.6,
    value_size: float = 11.6,
    padding_x: float = 14,
    label_top_offset: float = 18,
    value_top_offset: float = 38,
) -> None:
    add_pdf_rectangle(commands, x, y, width, height, fill_color=fill_color, stroke_color=stroke_color)
    add_pdf_text_block(commands, [label], x + padding_x, y + height - label_top_offset, font="F2", size=label_size, color=label_color)
    value_lines = wrap_pdf_text(value, width - (padding_x * 2), value_size, max_lines=max_lines)
    add_pdf_text_block(commands, value_lines, x + padding_x, y + height - value_top_offset, font="F1", size=value_size, color=value_color)


def format_display_datetime(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "N/A"
    for format_string in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, format_string)
            if format_string == "%Y-%m-%d":
                return parsed.strftime("%d %b %Y")
            return parsed.strftime("%d %b %Y, %I:%M %p")
        except ValueError:
            continue
    return raw


def parse_jpeg_for_pdf(file_path: Path) -> Optional[Dict[str, Any]]:
    payload = file_path.read_bytes()
    if not payload.startswith(b"\xff\xd8"):
        return None

    offset = 2
    while offset < len(payload):
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset : offset + 2], "big")
        offset += 2
        if segment_length < 2 or offset + segment_length - 2 > len(payload):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(payload[offset + 1 : offset + 3], "big")
            width = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            components = payload[offset + 5]
            color_space = {1: "/DeviceGray", 3: "/DeviceRGB"}.get(components)
            if not color_space:
                return None
            return {
                "width": width,
                "height": height,
                "bits_per_component": 8,
                "color_space": color_space,
                "filter_name": "/DCTDecode",
                "stream": payload,
            }
        offset += segment_length - 2
    return None


def png_paeth_predictor(left: int, up: int, upper_left: int) -> int:
    prediction = left + up - upper_left
    left_distance = abs(prediction - left)
    up_distance = abs(prediction - up)
    upper_left_distance = abs(prediction - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left


def decode_png_scanline(filter_type: int, row: bytes, previous_row: bytes, bytes_per_pixel: int) -> bytes:
    decoded = bytearray(row)
    if filter_type == 0:
        return bytes(decoded)
    if filter_type == 1:
        for index in range(len(decoded)):
            left = decoded[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            decoded[index] = (decoded[index] + left) & 0xFF
        return bytes(decoded)
    if filter_type == 2:
        for index in range(len(decoded)):
            up = previous_row[index] if previous_row else 0
            decoded[index] = (decoded[index] + up) & 0xFF
        return bytes(decoded)
    if filter_type == 3:
        for index in range(len(decoded)):
            left = decoded[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            up = previous_row[index] if previous_row else 0
            decoded[index] = (decoded[index] + ((left + up) // 2)) & 0xFF
        return bytes(decoded)
    if filter_type == 4:
        for index in range(len(decoded)):
            left = decoded[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            up = previous_row[index] if previous_row else 0
            upper_left = previous_row[index - bytes_per_pixel] if previous_row and index >= bytes_per_pixel else 0
            decoded[index] = (decoded[index] + png_paeth_predictor(left, up, upper_left)) & 0xFF
        return bytes(decoded)
    raise ValueError("Unsupported PNG filter type.")


def composite_channel(channel: int, alpha: int) -> int:
    return (channel * alpha + 255 * (255 - alpha)) // 255


def parse_png_for_pdf(file_path: Path) -> Optional[Dict[str, Any]]:
    payload = file_path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        return None

    offset = 8
    width = height = 0
    bit_depth = color_type = interlace_method = 0
    idat_parts: List[bytes] = []
    palette = b""
    palette_alpha = b""

    while offset + 8 <= len(payload):
        chunk_length = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + chunk_length
        chunk_data = payload[chunk_data_start:chunk_data_end]
        offset = chunk_data_end + 4
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, interlace_method = struct.unpack(
                ">IIBBBBB", chunk_data
            )
        elif chunk_type == b"PLTE":
            palette = chunk_data
        elif chunk_type == b"tRNS":
            palette_alpha = chunk_data
        elif chunk_type == b"IDAT":
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    if not width or not height or not idat_parts or bit_depth != 8 or interlace_method != 0:
        return None

    samples_per_pixel = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if not samples_per_pixel:
        return None

    row_length = width * samples_per_pixel
    inflated = zlib.decompress(b"".join(idat_parts))
    expected_length = height * (row_length + 1)
    if len(inflated) < expected_length:
        return None

    reconstructed_rows: List[bytes] = []
    previous_row = b""
    cursor = 0
    for _ in range(height):
        filter_type = inflated[cursor]
        row = inflated[cursor + 1 : cursor + 1 + row_length]
        cursor += row_length + 1
        decoded_row = decode_png_scanline(filter_type, row, previous_row, samples_per_pixel)
        reconstructed_rows.append(decoded_row)
        previous_row = decoded_row

    rgb_bytes = bytearray()
    if color_type == 0:
        for row in reconstructed_rows:
            for gray in row:
                rgb_bytes.extend((gray, gray, gray))
    elif color_type == 2:
        for row in reconstructed_rows:
            rgb_bytes.extend(row)
    elif color_type == 3:
        if not palette:
            return None
        palette_entries = [palette[index : index + 3] for index in range(0, len(palette), 3)]
        for row in reconstructed_rows:
            for index_value in row:
                if index_value >= len(palette_entries):
                    rgb_bytes.extend((255, 255, 255))
                    continue
                red, green, blue = palette_entries[index_value]
                alpha = palette_alpha[index_value] if index_value < len(palette_alpha) else 255
                rgb_bytes.extend(
                    (
                        composite_channel(red, alpha),
                        composite_channel(green, alpha),
                        composite_channel(blue, alpha),
                    )
                )
    elif color_type == 4:
        for row in reconstructed_rows:
            for index in range(0, len(row), 2):
                gray = row[index]
                alpha = row[index + 1]
                composited = composite_channel(gray, alpha)
                rgb_bytes.extend((composited, composited, composited))
    elif color_type == 6:
        for row in reconstructed_rows:
            for index in range(0, len(row), 4):
                red = row[index]
                green = row[index + 1]
                blue = row[index + 2]
                alpha = row[index + 3]
                rgb_bytes.extend(
                    (
                        composite_channel(red, alpha),
                        composite_channel(green, alpha),
                        composite_channel(blue, alpha),
                    )
                )

    return {
        "width": width,
        "height": height,
        "bits_per_component": 8,
        "color_space": "/DeviceRGB",
        "filter_name": "/FlateDecode",
        "stream": zlib.compress(bytes(rgb_bytes)),
    }


def load_image_for_pdf(file_path: Path) -> Optional[Dict[str, Any]]:
    if not file_path.exists() or not file_path.is_file():
        return None
    extension = file_path.suffix.lower()
    try:
        if extension in {".jpg", ".jpeg"}:
            return parse_jpeg_for_pdf(file_path)
        if extension == ".png":
            return parse_png_for_pdf(file_path)
    except Exception:
        return None
    return None


def find_chromium_pdf_browser() -> Optional[Path]:
    for browser_path in CHROMIUM_PDF_CANDIDATES:
        if browser_path.exists():
            return browser_path
    for browser_name in ("msedge", "msedge.exe", "chrome", "chrome.exe", "chromium", "chromium-browser"):
        browser_path = shutil.which(browser_name)
        if browser_path:
            return Path(browser_path)
    return None


def build_admission_form_cache_key(student: Dict[str, Any]) -> str:
    settings = get_settings()
    cache_payload: Dict[str, Any] = {
        "version": ADMISSION_FORM_CACHE_VERSION,
        "student_id": student.get("id"),
        "roll_number": student.get("roll_number"),
        "name": student.get("name"),
        "father_name": student.get("father_name"),
        "father_contact": student.get("father_contact"),
        "gender": student.get("gender"),
        "email": student.get("email"),
        "date_of_birth": student.get("date_of_birth"),
        "mobile": student.get("mobile"),
        "cnic": student.get("cnic"),
        "class": student.get("class"),
        "group": student.get("group"),
        "subjects": student.get("subjects") or [],
        "address": student.get("address"),
        "date": student.get("date"),
        "confirmed_at": student.get("confirmed_at"),
        "settings": {
            "address": settings.get("address"),
            "contact_primary": settings.get("contact_primary"),
            "email": settings.get("email"),
            "office_timing": settings.get("office_timing"),
            "admission_form_note": settings.get("admission_form_note"),
        },
    }
    photo_name = str(student.get("photo") or "").strip()
    if photo_name:
        photo_path = STUDENT_PHOTOS_DIR / photo_name
        if photo_path.exists():
            stat = photo_path.stat()
            cache_payload["photo"] = {
                "name": photo_name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        else:
            cache_payload["photo"] = {"name": photo_name, "missing": True}
    serialized = json.dumps(cache_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def admission_form_cache_path(student: Dict[str, Any]) -> Path:
    student_id = safe_filename_fragment(str(student.get("id") or "record"), "record")
    cache_key = build_admission_form_cache_key(student)
    return GENERATED_FORMS_DIR / f"admission_form_{student_id}_{cache_key}.pdf"


def cleanup_old_admission_form_cache(student: Dict[str, Any], keep_path: Path) -> None:
    student_id = safe_filename_fragment(str(student.get("id") or "record"), "record")
    pattern = f"admission_form_{student_id}_*.pdf"
    for existing_path in GENERATED_FORMS_DIR.glob(pattern):
        if existing_path == keep_path:
            continue
        existing_path.unlink(missing_ok=True)


def build_browser_admission_pdf(students: List[Dict[str, Any]], title: str) -> Optional[bytes]:
    browser_path = find_chromium_pdf_browser()
    if not browser_path:
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="tpa_admission_pdf_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            html_path = temp_dir / "admission_form.html"
            pdf_path = temp_dir / "admission_form.pdf"
            html_path.write_text(build_admission_form_document(students, title, render_mode="pdf"), encoding="utf-8")

            command_variants = [
                [
                    str(browser_path),
                    "--headless=new",
                    "--run-all-compositor-stages-before-draw",
                    *PDF_BROWSER_BASE_ARGS,
                    "--virtual-time-budget=1200",
                    "--print-to-pdf-no-header",
                    f"--print-to-pdf={pdf_path}",
                    html_path.as_uri(),
                ],
                [
                    str(browser_path),
                    "--headless",
                    "--run-all-compositor-stages-before-draw",
                    *PDF_BROWSER_BASE_ARGS,
                    "--virtual-time-budget=1200",
                    "--print-to-pdf-no-header",
                    f"--print-to-pdf={pdf_path}",
                    html_path.as_uri(),
                ],
            ]

            for command in command_variants:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if completed.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0:
                    return pdf_path.read_bytes()
                if pdf_path.exists():
                    pdf_path.unlink(missing_ok=True)
    except Exception:
        return None

    return None


def build_browser_admission_screenshot_pdf(students: List[Dict[str, Any]], title: str) -> Optional[bytes]:
    browser_path = find_chromium_pdf_browser()
    if not browser_path or not students:
        return None

    try:
        with tempfile.TemporaryDirectory(prefix="tpa_admission_screen_pdf_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            document = PDFDocumentBuilder()
            pages_object_id = document.reserve_object()
            page_ids: List[int] = []

            for index, student in enumerate(students):
                html_path = temp_dir / f"admission_form_{index}.html"
                screenshot_path = temp_dir / f"admission_form_{index}.png"
                html_path.write_text(build_admission_form_document([student], title, render_mode="capture"), encoding="utf-8")

                command_variants = [
                    [
                        str(browser_path),
                        "--headless=new",
                        "--run-all-compositor-stages-before-draw",
                        "--hide-scrollbars",
                        *PDF_BROWSER_BASE_ARGS,
                        "--force-device-scale-factor=2",
                        "--window-size=980,1390",
                        "--virtual-time-budget=1200",
                        f"--screenshot={screenshot_path}",
                        html_path.as_uri(),
                    ],
                    [
                        str(browser_path),
                        "--headless",
                        "--run-all-compositor-stages-before-draw",
                        "--hide-scrollbars",
                        *PDF_BROWSER_BASE_ARGS,
                        "--force-device-scale-factor=2",
                        "--window-size=980,1390",
                        "--virtual-time-budget=1200",
                        f"--screenshot={screenshot_path}",
                        html_path.as_uri(),
                    ],
                ]

                for command in command_variants:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    if completed.returncode == 0 and screenshot_path.exists() and screenshot_path.stat().st_size > 0:
                        break
                    if screenshot_path.exists():
                        screenshot_path.unlink(missing_ok=True)

                if not screenshot_path.exists() or screenshot_path.stat().st_size == 0:
                    return None

                image_info = parse_png_for_pdf(screenshot_path)
                if not image_info:
                    return None

                image_object_id = document.add_stream_object(
                    f"/Type /XObject /Subtype /Image /Width {image_info['width']} /Height {image_info['height']} /ColorSpace {image_info['color_space']} /BitsPerComponent {image_info['bits_per_component']} /Filter {image_info['filter_name']}",
                    image_info["stream"],
                )
                image_name = "Im1"
                page_width = 595.0
                page_height = 842.0
                scale = min(page_width / image_info["width"], page_height / image_info["height"])
                draw_width = image_info["width"] * scale
                draw_height = image_info["height"] * scale
                draw_x = (page_width - draw_width) / 2
                draw_y = max(6.0, page_height - draw_height - 6.0)
                content_bytes = "\n".join(
                    [
                        "q",
                        f"{PDF_WHITE} rg",
                        f"{PDF_WHITE} RG",
                        "0 0 595 842 re B",
                        "Q",
                        "q",
                        f"{draw_width:.2f} 0 0 {draw_height:.2f} {draw_x:.2f} {draw_y:.2f} cm",
                        f"/{image_name} Do",
                        "Q",
                    ]
                ).encode("latin-1")
                content_object_id = document.add_stream_object("", content_bytes)
                resources = f"<< /XObject << /{image_name} {image_object_id} 0 R >> >>"
                page_ids.append(
                    document.add_object(
                        f"<< /Type /Page /Parent {pages_object_id} 0 R /MediaBox [0 0 595 842] /Resources {resources} /Contents {content_object_id} 0 R >>"
                    )
                )

            kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
            document.set_object(pages_object_id, f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>")
            catalog_object_id = document.add_object(f"<< /Type /Catalog /Pages {pages_object_id} 0 R >>")
            return document.build(catalog_object_id)
    except Exception:
        return None

    return None


def build_summary_fallback_admission_pdf(students: List[Dict[str, Any]], title: str) -> bytes:
    document = PDFDocumentBuilder()
    pages_object_id = document.reserve_object()
    regular_font_id = document.add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    bold_font_id = document.add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    page_ids: List[int] = []
    settings = get_settings()
    academy_contact = str(settings.get("contact_primary") or "N/A").strip() or "N/A"
    academy_email = str(settings.get("email") or "N/A").strip() or "N/A"
    academy_address = str(settings.get("address") or "The Professors Academy").strip() or "The Professors Academy"
    payment_note = render_settings_text_template(
        settings.get("admission_form_note") or DEFAULT_SETTINGS["admission_form_note"],
        settings,
    ) or DEFAULT_SETTINGS["admission_form_note"]

    def normalized_value(value: Any) -> str:
        normalized = str(value or "").strip()
        if not normalized or normalized.upper() == "N/A":
            return ""
        return normalized

    def visible_rows(rows: List[Tuple[str, Any]]) -> List[Tuple[str, str]]:
        normalized_rows: List[Tuple[str, str]] = []
        for label, value in rows:
            display_value = normalized_value(value)
            if display_value:
                normalized_rows.append((label, display_value))
        return normalized_rows

    def draw_panel(commands: List[str], title_text: str, x: float, y: float, width: float, height: float) -> None:
        add_pdf_rectangle(commands, x, y, width, height, fill_color=PDF_WHITE, stroke_color=PDF_LINE_COLOR, line_width=0.9)
        title_width = min(max(96, (len(title_text) * 4.8) + 22), width - 32)
        title_height = 14
        title_y = y + height - 7
        add_pdf_rectangle(commands, x + 12, title_y, title_width, title_height, fill_color=PDF_WHITE, stroke_color=PDF_GOLD_COLOR, line_width=0.6)
        add_pdf_text_block(commands, [title_text], x + 18, title_y + 4, font="F2", size=7.4, color=PDF_PRIMARY_COLOR)

    def add_signature_box(commands: List[str], label: str, x: float, y: float, width: float, height: float) -> None:
        add_pdf_rectangle(commands, x, y, width, height, fill_color=PDF_WHITE, stroke_color=PDF_LINE_COLOR, line_width=0.9)
        add_pdf_rectangle(commands, x + 14, y + height - 16, width - 28, 0.7, fill_color=PDF_LINE_COLOR, stroke_color=PDF_LINE_COLOR, line_width=0.7)
        add_pdf_text_block(commands, [label], x + 40, y + 7, font="F2", size=7.4, color=PDF_MUTED_COLOR)

    def build_student_page_commands(student: Dict[str, Any], image_name: Optional[str], image_info: Optional[Dict[str, Any]]) -> bytes:
        commands: List[str] = []
        roll_number = str(student.get("roll_number") or "").strip() or "N/A"
        class_name = str(student.get("class") or "").strip() or "N/A"
        student_group = str(student.get("group") or "").strip() or "N/A"
        class_display = class_name if student_group == "N/A" else f"{class_name} | {student_group}"
        submitted_on = format_display_datetime(student.get("date"))
        date_of_birth = format_display_datetime(student.get("date_of_birth"))
        subjects_text = ", ".join(student.get("subjects") or []) or "N/A"

        page_x = 20
        page_y = 20
        page_width = 555
        page_height = 802
        content_x = 36
        content_width = 523
        page_top = page_y + page_height
        photo_width = 112
        photo_height = 134
        photo_x = content_x + content_width - photo_width
        photo_y = page_top - 166
        detail_style = {
            "fill_color": PDF_SOFT_FILL,
            "stroke_color": PDF_LINE_COLOR,
            "label_color": PDF_MUTED_COLOR,
            "value_color": PDF_INK_COLOR,
            "label_size": 6.8,
            "value_size": 9.2,
            "padding_x": 10,
            "label_top_offset": 12,
            "value_top_offset": 22,
        }

        add_pdf_rectangle(commands, page_x, page_y, page_width, page_height, fill_color=PDF_WHITE, stroke_color=PDF_LINE_COLOR, line_width=1.0)
        add_pdf_rectangle(commands, page_x + 8, page_y + 8, page_width - 16, page_height - 16, fill_color=None, stroke_color=PDF_GOLD_COLOR, line_width=0.8)
        add_pdf_rectangle(commands, content_x, page_top - 42, 182, 18, fill_color=PDF_SOFT_GOLD, stroke_color=PDF_GOLD_COLOR, line_width=0.8)
        add_pdf_text_block(commands, ["Official Admission Record"], content_x + 10, page_top - 36, font="F2", size=7.6, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(commands, ["The Professors Academy"], content_x, page_top - 74, font="F2", size=17.6, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(commands, ["Professional Admission Summary"], content_x, page_top - 92, font="F2", size=10.0, color=PDF_MUTED_COLOR)
        add_pdf_text_block(
            commands,
            wrap_pdf_text(academy_address, 330, 8.4, max_lines=2),
            content_x,
            page_top - 108,
            font="F1",
            size=8.4,
            color=PDF_MUTED_COLOR,
            leading=10,
        )
        add_pdf_text_block(
            commands,
            [f"Phone: {academy_contact} | Email: {academy_email}"],
            content_x,
            page_top - 132,
            font="F1",
            size=8.2,
            color=PDF_MUTED_COLOR,
        )
        add_pdf_rectangle(commands, content_x, page_top - 160, content_width - photo_width - 12, 24, fill_color=PDF_SOFT_FILL, stroke_color=PDF_LINE_COLOR, line_width=0.8)
        add_pdf_rectangle(commands, content_x, page_top - 160, 4, 24, fill_color=PDF_GOLD_COLOR, stroke_color=PDF_GOLD_COLOR, line_width=0.8)
        add_pdf_text_block(
            commands,
            ["This formal record contains the student's submitted details for academy verification and printed submission."],
            content_x + 10,
            page_top - 151,
            font="F1",
            size=7.4,
            color=PDF_INK_COLOR,
            leading=8.2,
        )

        add_pdf_rectangle(commands, photo_x, photo_y, photo_width, photo_height, fill_color=PDF_SOFT_FILL, stroke_color=PDF_GOLD_COLOR, line_width=0.9)
        add_pdf_rectangle(commands, photo_x + 8, photo_y + 30, photo_width - 16, photo_height - 36, fill_color=PDF_WHITE, stroke_color=PDF_LINE_COLOR, line_width=0.7)
        add_pdf_rectangle(commands, photo_x + 8, photo_y + 8, photo_width - 16, 16, fill_color=PDF_WHITE, stroke_color=PDF_LINE_COLOR, line_width=0.6)
        if image_name and image_info:
            inner_photo_width = photo_width - 16
            inner_photo_height = photo_height - 36
            scale = min(inner_photo_width / image_info["width"], inner_photo_height / image_info["height"])
            draw_width = image_info["width"] * scale
            draw_height = image_info["height"] * scale
            draw_x = photo_x + 8 + (inner_photo_width - draw_width) / 2
            draw_y = photo_y + 30 + (inner_photo_height - draw_height) / 2
            commands.append(
                "\n".join(
                    [
                        "q",
                        f"{draw_width:.2f} 0 0 {draw_height:.2f} {draw_x:.2f} {draw_y:.2f} cm",
                        f"/{image_name} Do",
                        "Q",
                    ]
                )
            )
        else:
            add_pdf_text_block(commands, ["No Photo"], photo_x + 30, photo_y + 73, font="F2", size=12, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(commands, ["Student Photograph"], photo_x + 18, photo_y + 14, font="F2", size=7.0, color=PDF_PRIMARY_COLOR)

        meta_y = 588
        meta_height = 50
        meta_gap = 8
        meta_width = (content_width - (meta_gap * 3)) / 4
        meta_rows = [
            ("Student", student.get("name") or "N/A"),
            ("Roll No", roll_number),
            ("Class / Track", class_display),
            ("Application Date", submitted_on),
        ]
        for index, (label, value) in enumerate(meta_rows):
            add_pdf_detail_box(
                commands,
                label,
                value,
                content_x + (index * (meta_width + meta_gap)),
                meta_y,
                meta_width,
                meta_height,
                max_lines=2,
                fill_color=PDF_WHITE,
                stroke_color=PDF_LINE_COLOR,
                label_color=PDF_MUTED_COLOR,
                value_color=PDF_PRIMARY_COLOR,
                label_size=7.4,
                value_size=9.6,
                padding_x=11,
                label_top_offset=13,
                value_top_offset=28,
            )

        left_panel_x = content_x
        left_panel_y = 394
        left_panel_width = 242
        left_panel_height = 188
        right_panel_x = left_panel_x + left_panel_width + 14
        right_panel_y = left_panel_y
        right_panel_width = content_width - left_panel_width - 14
        right_panel_height = left_panel_height
        draw_panel(commands, "Student Information", left_panel_x, left_panel_y, left_panel_width, left_panel_height)
        draw_panel(commands, "Contact & Address", right_panel_x, right_panel_y, right_panel_width, right_panel_height)

        identity_rows = visible_rows(
            [
                ("Full Name", student.get("name")),
                ("Father Name", student.get("father_name")),
                ("Gender", student.get("gender")),
                ("Date of Birth", date_of_birth),
                ("CNIC Number", student.get("cnic")),
            ]
        )
        identity_box_y = left_panel_y + left_panel_height - 46
        for label, value in identity_rows[:5]:
            add_pdf_detail_box(commands, label, value, left_panel_x + 10, identity_box_y, left_panel_width - 20, 30, max_lines=2, **detail_style)
            identity_box_y -= 34

        contact_rows = visible_rows(
            [
                ("Student Contact", student.get("mobile")),
                ("Father Contact", student.get("father_contact")),
                ("Email Address", student.get("email")),
            ]
        )
        contact_box_y = right_panel_y + right_panel_height - 46
        for label, value in contact_rows:
            add_pdf_detail_box(commands, label, value, right_panel_x + 10, contact_box_y, right_panel_width - 20, 30, max_lines=2, **detail_style)
            contact_box_y -= 34

        address_value = normalized_value(student.get("address")) or "N/A"
        add_pdf_detail_box(
            commands,
            "Address",
            address_value,
            right_panel_x + 10,
            right_panel_y + 10,
            right_panel_width - 20,
            52,
            max_lines=3,
            **detail_style,
        )

        academic_x = content_x
        academic_y = 252
        academic_width = content_width
        academic_height = 116
        draw_panel(commands, "Academic Selection", academic_x, academic_y, academic_width, academic_height)
        half_width = (academic_width - 30) / 2
        add_pdf_detail_box(commands, "Class / Program", class_name, academic_x + 10, academic_y + 66, half_width, 30, max_lines=1, **detail_style)
        add_pdf_detail_box(commands, "Academic Group", student_group, academic_x + 20 + half_width, academic_y + 66, half_width, 30, max_lines=1, **detail_style)
        add_pdf_detail_box(commands, "Application Date", submitted_on, academic_x + 10, academic_y + 30, academic_width - 20, 28, max_lines=2, **detail_style)
        add_pdf_detail_box(
            commands,
            "Selected Subjects",
            subjects_text,
            academic_x + 10,
            academic_y + 4,
            academic_width - 20,
            22,
            max_lines=2,
            **detail_style,
        )

        note_y = 192
        add_pdf_rectangle(commands, content_x, note_y, content_width, 48, fill_color=PDF_SOFT_GOLD, stroke_color=PDF_GOLD_COLOR, line_width=0.9)
        add_pdf_text_block(commands, ["Important Note"], content_x + 12, note_y + 27, font="F2", size=7.6, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(
            commands,
            wrap_pdf_text(payment_note, content_width - 24, 8.9, max_lines=2),
            content_x + 12,
            note_y + 12,
            font="F2",
            size=8.9,
            color=PDF_PRIMARY_COLOR,
            leading=9.4,
        )

        signature_y = 136
        signature_height = 36
        signature_gap = 10
        signature_width = (content_width - (signature_gap * 2)) / 3
        add_signature_box(commands, "Student Signature", content_x, signature_y, signature_width, signature_height)
        add_signature_box(commands, "Parent / Guardian", content_x + signature_width + signature_gap, signature_y, signature_width, signature_height)
        add_signature_box(commands, "Admin Office", content_x + ((signature_width + signature_gap) * 2), signature_y, signature_width, signature_height)
        add_pdf_text_block(commands, ["Official Student Copy"], content_x, 116, font="F2", size=7.0, color=PDF_MUTED_COLOR)
        add_pdf_text_block(commands, ["The Professors Academy | Printed Submission Record"], content_x + 320, 116, font="F1", size=6.8, color=PDF_MUTED_COLOR)
        return "\n".join(commands).encode("latin-1")

    if not students:
        content_bytes = "\n".join(
            [
                "q",
                f"{PDF_WHITE} rg",
                f"{PDF_LINE_COLOR} RG",
                "1.00 w",
                "20.00 20.00 555.00 802.00 re B",
                "Q",
            ]
        ).encode("latin-1")
        add_placeholder = [
            "BT",
            f"{PDF_PRIMARY_COLOR} rg",
            "/F2 22 Tf",
            "26 TL",
            "1 0 0 1 44 760 Tm",
            "(The Professors Academy) Tj",
            "T*",
            "(Admission Forms) Tj",
            "ET",
            "BT",
            f"{PDF_MUTED_COLOR} rg",
            "/F1 11 Tf",
            "16 TL",
            "1 0 0 1 44 716 Tm",
            "(No confirmed admissions are available right now.) Tj",
            "T*",
            "(Once an admission is confirmed, its PDF form will appear here.) Tj",
            "ET",
        ]
        content_bytes += ("\n".join(add_placeholder)).encode("latin-1")
        content_object_id = document.add_stream_object("", content_bytes)
        page_ids.append(
            document.add_object(
                f"<< /Type /Page /Parent {pages_object_id} 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {regular_font_id} 0 R /F2 {bold_font_id} 0 R >> >> /Contents {content_object_id} 0 R >>"
            )
        )
    else:
        for student in students:
            image_info = None
            image_name = None
            image_object_id = None
            if student.get("photo"):
                image_path = STUDENT_PHOTOS_DIR / str(student["photo"])
                image_info = load_image_for_pdf(image_path)
                if image_info:
                    image_object_id = document.add_stream_object(
                        f"/Type /XObject /Subtype /Image /Width {image_info['width']} /Height {image_info['height']} /ColorSpace {image_info['color_space']} /BitsPerComponent {image_info['bits_per_component']} /Filter {image_info['filter_name']}",
                        image_info["stream"],
                    )
                    image_name = "Im1"

            content_bytes = build_student_page_commands(student, image_name, image_info)
            content_object_id = document.add_stream_object("", content_bytes)
            resources = f"<< /Font << /F1 {regular_font_id} 0 R /F2 {bold_font_id} 0 R >>"
            if image_object_id:
                resources += f" /XObject << /{image_name} {image_object_id} 0 R >>"
            resources += " >>"
            page_ids.append(
                document.add_object(
                    f"<< /Type /Page /Parent {pages_object_id} 0 R /MediaBox [0 0 595 842] /Resources {resources} /Contents {content_object_id} 0 R >>"
                )
            )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    document.set_object(pages_object_id, f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>")
    catalog_object_id = document.add_object(f"<< /Type /Catalog /Pages {pages_object_id} 0 R >>")
    return document.build(catalog_object_id)


def build_admission_form_pdf(students: List[Dict[str, Any]], title: str) -> bytes:
    if len(students) == 1:
        cached_pdf_path = admission_form_cache_path(students[0])
        if cached_pdf_path.exists() and cached_pdf_path.stat().st_size > 0:
            return cached_pdf_path.read_bytes()

    # Keep downloaded admission forms deterministic and single-page.
    pdf_bytes = build_summary_fallback_admission_pdf(students, title)

    if len(students) == 1:
        cached_pdf_path = admission_form_cache_path(students[0])
        try:
            cached_pdf_path.write_bytes(pdf_bytes)
            cleanup_old_admission_form_cache(students[0], cached_pdf_path)
        except Exception:
            pass

    return pdf_bytes

    document = PDFDocumentBuilder()
    pages_object_id = document.reserve_object()
    regular_font_id = document.add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    bold_font_id = document.add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    page_ids: List[int] = []
    settings = get_settings()
    academy_contact = settings.get("contact_primary") or "N/A"
    academy_email = settings.get("email") or "N/A"
    academy_address = settings.get("address") or "N/A"
    academy_office_timing = settings.get("office_timing") or "N/A"
    payment_note = "Please submit Rs. 5000 at the admin office with a printed copy of this form."

    def build_student_page_commands(student: Dict[str, Any], image_name: Optional[str], image_info: Optional[Dict[str, Any]]) -> bytes:
        commands: List[str] = []
        student_group = student.get("group") or "N/A"
        date_of_birth = format_display_datetime(student.get("date_of_birth"))
        roll_number = student.get("roll_number") or "N/A"
        student_id = str(student.get("id") or "N/A")
        confirmed_on = format_display_datetime(student.get("confirmed_at"))
        application_date = format_display_datetime(student.get("date"))
        issue_date = confirmed_on if confirmed_on != "N/A" else application_date
        class_display = f"{student.get('class') or 'N/A'}{f' | {student_group}' if student_group != 'N/A' else ''}"
        document_code = f"TPA-{roll_number}"
        subjects_text = ", ".join(student.get("subjects") or []) or "N/A"
        instruction_lines = [
            "1. Bring a printed copy of this form when visiting the academy office.",
            "2. Submit Rs. 5000 at the admin office to complete the admission process.",
            "3. Keep your roll number and CNIC available for future academy communication.",
        ]
        page_x = 18
        page_y = 18
        page_width = 559
        page_height = 806
        content_x = 38
        content_width = 519
        badge_width = 170
        badge_height = 18
        green_color = "0.067 0.478 0.290"
        photo_x = content_x
        photo_y = 510
        photo_width = 145
        photo_height = 126
        summary_x = 195
        summary_width = 205
        approval_x = 412
        approval_width = 145
        status_y = 650
        status_height = 18
        status_gap = 9
        fact_y = 458
        fact_height = 36
        fact_gap = 9
        fact_width = (content_width - (fact_gap * 3)) / 4
        left_x = content_x
        right_x = 304
        box_width = 253
        row_height = 30
        row_gap = 6

        add_pdf_rectangle(commands, page_x, page_y, page_width, page_height, stroke_color=PDF_LINE_COLOR, fill_color=PDF_WHITE, line_width=1.0)
        add_pdf_rectangle(commands, page_x + 8, page_y + 8, page_width - 16, page_height - 16, stroke_color=PDF_GOLD_COLOR, fill_color=None, line_width=0.8)
        add_pdf_text_block(commands, ["TPA"], 408, 612, font="F2", size=92, color=PDF_SOFT_BLUE)

        add_pdf_rectangle(commands, content_x, 778, badge_width, badge_height, fill_color=PDF_SOFT_GOLD, stroke_color=PDF_GOLD_COLOR, line_width=0.8)
        add_pdf_text_block(commands, ["Confirmed Admission Record"], content_x + 11, 784, font="F2", size=8.3, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(commands, ["The Professors Academy"], content_x, 748, font="F2", size=24, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(commands, ["Admission Confirmation Sheet"], content_x, 738, font="F1", size=10.4, color=PDF_MUTED_COLOR)
        add_pdf_text_block(
            commands,
            wrap_pdf_text(academy_address, 280, 9.5, max_lines=2),
            content_x,
            718,
            font="F1",
            size=9.5,
            color=PDF_MUTED_COLOR,
            leading=11.5,
        )
        add_pdf_text_block(commands, [f"Phone: {academy_contact} | Email: {academy_email}"], content_x, 690, font="F1", size=9.4, color=PDF_MUTED_COLOR)

        meta_x = 370
        meta_y = 694
        meta_width = 173
        meta_height = 96
        add_pdf_rectangle(commands, meta_x, meta_y, meta_width, meta_height, fill_color=PDF_SOFT_GOLD, stroke_color=PDF_GOLD_COLOR, line_width=0.9)
        add_pdf_text_block(commands, ["ACADEMY RECORD"], meta_x + 12, meta_y + 78, font="F2", size=7.6, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(
            commands,
            [
                f"Roll Number: {roll_number}",
                f"Student ID: {student_id}",
                f"Confirmed On: {confirmed_on}",
                f"Office Timing: {academy_office_timing}",
            ],
            meta_x + 12,
            meta_y + 62,
            font="F1",
            size=8.5,
            color=PDF_INK_COLOR,
            leading=12,
        )
        add_pdf_rectangle(commands, content_x, 680, content_width, 2, fill_color=PDF_GOLD_COLOR, stroke_color=PDF_GOLD_COLOR)

        chip_widths = [154, 158, 189]
        chip_x = content_x
        for chip_width, chip_text, chip_fill, chip_stroke, chip_color in [
            (chip_widths[0], "Approved By Admin Office", "0.933 0.973 0.949", green_color, green_color),
            (chip_widths[1], f"Document Code: {document_code}", PDF_SOFT_BLUE, PDF_LINE_COLOR, PDF_PRIMARY_COLOR),
            (chip_widths[2], "Student Copy / Academy Record", PDF_SOFT_FILL, PDF_LINE_COLOR, PDF_PRIMARY_COLOR),
        ]:
            add_pdf_rectangle(
                commands,
                chip_x,
                status_y,
                chip_width,
                status_height,
                fill_color=chip_fill,
                stroke_color=chip_stroke,
                line_width=0.7,
            )
            add_pdf_text_block(commands, [chip_text], chip_x + 9, status_y + 6, font="F2", size=6.9, color=chip_color)
            chip_x += chip_width + status_gap

        ribbon_y = 620
        ribbon_height = 22
        ribbon_gap = 10
        ribbon_widths = [156, 156, 187]
        ribbon_items = [
            ("Document Type", "Official Confirmed Admission"),
            ("Document Code", document_code),
            ("Issue Date", issue_date),
        ]
        ribbon_x = content_x
        for index, (label, value) in enumerate(ribbon_items):
            ribbon_width = ribbon_widths[index]
            add_pdf_rectangle(
                commands,
                ribbon_x,
                ribbon_y,
                ribbon_width,
                ribbon_height,
                fill_color=PDF_SOFT_GOLD,
                stroke_color=PDF_GOLD_COLOR,
                line_width=0.7,
            )
            add_pdf_text_block(commands, [label], ribbon_x + 9, ribbon_y + 13, font="F2", size=6.3, color=PDF_MUTED_COLOR)
            add_pdf_text_block(
                commands,
                wrap_pdf_text(value, ribbon_width - 18, 8.6, max_lines=1),
                ribbon_x + 9,
                ribbon_y + 4,
                font="F1",
                size=8.6,
                color=PDF_PRIMARY_COLOR,
            )
            ribbon_x += ribbon_width + ribbon_gap

        add_pdf_rectangle(commands, photo_x, photo_y, photo_width, photo_height, fill_color=PDF_SOFT_FILL, stroke_color=PDF_LINE_COLOR, line_width=1.0)
        add_pdf_rectangle(commands, photo_x + 8, photo_y + 20, photo_width - 16, 96, fill_color=PDF_WHITE, stroke_color=PDF_LINE_COLOR, line_width=0.6)
        add_pdf_rectangle(commands, summary_x, photo_y, summary_width, photo_height, fill_color=PDF_PRIMARY_DARK, stroke_color=PDF_PRIMARY_DARK, line_width=1.0)
        add_pdf_rectangle(commands, summary_x + 12, photo_y + 100, 112, 14, fill_color=PDF_SOFT_BLUE, stroke_color=None)
        add_pdf_text_block(commands, ["Official Student Record"], summary_x + 19, photo_y + 105, font="F2", size=7.1, color=PDF_PRIMARY_COLOR)
        add_pdf_rectangle(commands, approval_x, photo_y, approval_width, photo_height, fill_color=PDF_SOFT_GOLD, stroke_color=PDF_GOLD_COLOR, line_width=0.9)

        if image_name and image_info:
            inner_photo_width = photo_width - 16
            inner_photo_height = 96
            scale = min(inner_photo_width / image_info["width"], inner_photo_height / image_info["height"])
            draw_width = image_info["width"] * scale
            draw_height = image_info["height"] * scale
            draw_x = photo_x + 8 + (inner_photo_width - draw_width) / 2
            draw_y = photo_y + 20 + (inner_photo_height - draw_height) / 2
            commands.append(
                "\n".join(
                    [
                        "q",
                        f"{draw_width:.2f} 0 0 {draw_height:.2f} {draw_x:.2f} {draw_y:.2f} cm",
                        f"/{image_name} Do",
                        "Q",
                    ]
                )
            )
        else:
            add_pdf_text_block(commands, ["Student Photo"], photo_x + 35, photo_y + 69, font="F2", size=12, color=PDF_PRIMARY_COLOR)
            add_pdf_text_block(commands, ["Photo not available"], photo_x + 24, photo_y + 49, font="F1", size=9.2, color=PDF_MUTED_COLOR)

        add_pdf_text_block(commands, ["Official Passport Photograph"], photo_x + 18, photo_y + 8, font="F2", size=7.2, color=PDF_PRIMARY_COLOR)

        add_pdf_text_block(
            commands,
            wrap_pdf_text(student.get("name") or "N/A", summary_width - 28, 18.5, max_lines=2),
            summary_x + 14,
            photo_y + 76,
            font="F2",
            size=18.5,
            color=PDF_WHITE,
            leading=20,
        )
        add_pdf_text_block(commands, [f"Roll Number: {roll_number}"], summary_x + 14, photo_y + 44, font="F1", size=10.2, color="0.816 0.855 0.894")
        add_pdf_text_block(commands, [class_display], summary_x + 14, photo_y + 28, font="F1", size=10.2, color="0.816 0.855 0.894")
        add_pdf_text_block(
            commands,
            wrap_pdf_text(f"Subjects: {subjects_text}", summary_width - 28, 8.8, max_lines=3),
            summary_x + 14,
            photo_y + 12,
            font="F1",
            size=8.8,
            color="0.816 0.855 0.894",
            leading=9.8,
        )

        compact_box = {
            "fill_color": PDF_SOFT_FILL,
            "stroke_color": PDF_LINE_COLOR,
            "label_color": PDF_MUTED_COLOR,
            "value_color": PDF_INK_COLOR,
            "label_size": 7.2,
            "value_size": 9.7,
            "padding_x": 11,
            "label_top_offset": 13,
            "value_top_offset": 23,
        }
        top_rows = [
            (414, "Roll Number", roll_number, "Full Name", student.get("name") or "N/A"),
            (414 - (row_height + row_gap), "Father Name", student.get("father_name") or "N/A", "Father Contact No", student.get("father_contact") or "N/A"),
            (414 - ((row_height + row_gap) * 2), "Gender", student.get("gender") or "N/A", "Email Address", student.get("email") or "N/A"),
            (414 - ((row_height + row_gap) * 3), "Date of Birth", date_of_birth, "Mobile Number", student.get("mobile") or "N/A"),
            (414 - ((row_height + row_gap) * 4), "CNIC Number", student.get("cnic") or "N/A", "Class / Group", class_display),
            (414 - ((row_height + row_gap) * 5), "Application Date", application_date, "Confirmed On", confirmed_on),
        ]
        for box_y, left_label, left_value, right_label, right_value in top_rows:
            add_pdf_detail_box(commands, left_label, left_value, left_x, box_y, box_width, row_height, max_lines=1, **compact_box)
            add_pdf_detail_box(commands, right_label, right_value, right_x, box_y, box_width, row_height, max_lines=1, **compact_box)

        add_pdf_detail_box(
            commands,
            "Subjects",
            subjects_text,
            content_x,
            188,
            content_width,
            34,
            max_lines=2,
            fill_color=PDF_SOFT_FILL,
            stroke_color=PDF_LINE_COLOR,
            label_color=PDF_MUTED_COLOR,
            value_color=PDF_INK_COLOR,
            label_size=7.8,
            value_size=9.8,
            padding_x=12,
            label_top_offset=14,
            value_top_offset=25,
        )
        add_pdf_detail_box(
            commands,
            "Address",
            student.get("address") or "N/A",
            content_x,
            142,
            content_width,
            36,
            max_lines=2,
            fill_color=PDF_SOFT_FILL,
            stroke_color=PDF_LINE_COLOR,
            label_color=PDF_MUTED_COLOR,
            value_color=PDF_INK_COLOR,
            label_size=7.8,
            value_size=9.6,
            padding_x=12,
            label_top_offset=14,
            value_top_offset=25,
        )

        add_pdf_text_block(commands, ["Admission Status"], approval_x + 12, photo_y + 98, font="F2", size=7.6, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(commands, ["Confirmed"], approval_x + 12, photo_y + 70, font="F2", size=20, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(
            commands,
            wrap_pdf_text("This student has been approved by The Professors Academy administration office.", approval_width - 24, 8.2, max_lines=4),
            approval_x + 12,
            photo_y + 52,
            font="F1",
            size=8.2,
            color=PDF_INK_COLOR,
            leading=9.1,
        )
        add_pdf_rectangle(commands, approval_x + 12, photo_y + 14, 74, 16, fill_color="0.933 0.973 0.949", stroke_color=green_color, line_width=0.8)
        add_pdf_text_block(commands, ["VERIFIED"], approval_x + 25, photo_y + 20, font="F2", size=8.1, color=green_color)

        fact_items = [
            ("Roll Number", roll_number),
            ("Class / Group", class_display),
            ("Confirmed On", confirmed_on),
            ("Office Timing", academy_office_timing),
        ]
        add_pdf_rectangle(commands, content_x, 446, content_width, 8, fill_color=PDF_PRIMARY_DARK, stroke_color=PDF_PRIMARY_DARK)
        add_pdf_text_block(commands, ["SECTION 01  |  STUDENT INFORMATION"], content_x + 12, 448, font="F2", size=7.2, color=PDF_WHITE)
        for index, (label, value) in enumerate(fact_items):
            fact_x = content_x + (index * (fact_width + fact_gap))
            add_pdf_rectangle(commands, fact_x, fact_y, fact_width, fact_height, fill_color=PDF_SOFT_FILL, stroke_color=PDF_LINE_COLOR, line_width=0.8)
            add_pdf_text_block(commands, [label], fact_x + 10, fact_y + 23, font="F2", size=6.8, color=PDF_MUTED_COLOR)
            add_pdf_text_block(
                commands,
                wrap_pdf_text(value, fact_width - 20, 9.5, max_lines=2),
                fact_x + 10,
                fact_y + 10,
                font="F1",
                size=9.5,
                color=PDF_PRIMARY_COLOR,
                leading=10.4,
            )

        add_pdf_rectangle(commands, content_x, 224, content_width, 8, fill_color=PDF_PRIMARY_DARK, stroke_color=PDF_PRIMARY_DARK)
        add_pdf_text_block(commands, ["SECTION 02  |  ACADEMIC & ADMISSION RECORD"], content_x + 12, 226, font="F2", size=7.2, color=PDF_WHITE)
        panel_y = 86
        panel_height = 42
        panel_gap = 12
        left_panel_width = 320
        right_panel_width = content_width - left_panel_width - panel_gap
        add_pdf_rectangle(commands, content_x, 130, content_width, 8, fill_color=PDF_PRIMARY_DARK, stroke_color=PDF_PRIMARY_DARK)
        add_pdf_text_block(commands, ["SECTION 03  |  INSTRUCTIONS & OFFICE VERIFICATION"], content_x + 12, 132, font="F2", size=7.2, color=PDF_WHITE)
        add_pdf_rectangle(commands, content_x, panel_y, left_panel_width, panel_height, fill_color=PDF_SOFT_FILL, stroke_color=PDF_LINE_COLOR, line_width=0.8)
        add_pdf_rectangle(commands, content_x + left_panel_width + panel_gap, panel_y, right_panel_width, panel_height, fill_color=PDF_SOFT_FILL, stroke_color=PDF_LINE_COLOR, line_width=0.8)
        add_pdf_text_block(commands, ["Important Instructions"], content_x + 12, panel_y + 30, font="F2", size=7.4, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(
            commands,
            instruction_lines,
            content_x + 12,
            panel_y + 19,
            font="F1",
            size=7.2,
            color=PDF_MUTED_COLOR,
            leading=8.2,
        )
        office_x = content_x + left_panel_width + panel_gap
        add_pdf_text_block(commands, ["Office Use Only"], office_x + 12, panel_y + 30, font="F2", size=7.4, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(
            commands,
            [
                f"Document Code: {document_code}",
                f"Issued For: {student.get('name') or 'N/A'}",
                "Fee Status: To be deposited at admin office",
            ],
            office_x + 12,
            panel_y + 19,
            font="F1",
            size=7.2,
            color=PDF_MUTED_COLOR,
            leading=8.2,
        )

        payment_y = 52
        payment_height = 24
        add_pdf_rectangle(commands, content_x, payment_y, content_width, payment_height, fill_color=PDF_SOFT_GOLD, stroke_color=PDF_GOLD_COLOR, line_width=0.9)
        add_pdf_text_block(commands, ["Declaration & Fee Note"], content_x + 12, payment_y + 14, font="F2", size=7.4, color=PDF_PRIMARY_COLOR)
        add_pdf_text_block(
            commands,
            wrap_pdf_text(payment_note, content_width - 140, 7.9, max_lines=2),
            content_x + 120,
            payment_y + 14,
            font="F1",
            size=7.9,
            color=PDF_INK_COLOR,
            leading=8.6,
        )

        signature_y = 22
        signature_width = 162
        signature_gap = 16
        signature_labels = ["Student Signature", "Parent / Guardian", "Admin Office"]
        for index, signature_label in enumerate(signature_labels):
            signature_x = content_x + (index * (signature_width + signature_gap))
            add_pdf_rectangle(commands, signature_x, signature_y, signature_width, 22, stroke_color=PDF_LINE_COLOR, fill_color=PDF_WHITE, line_width=0.8)
            add_pdf_text_block(commands, [signature_label], signature_x + 27, signature_y + 8, font="F1", size=7.6, color=PDF_MUTED_COLOR)
        return "\n".join(commands).encode("latin-1")

    if not students:
        content_bytes = "\n".join(
            [
                "q",
                f"{PDF_PRIMARY_COLOR} RG",
                "1.20 w",
                "24.00 24.00 547.00 794.00 re S",
                "Q",
            ]
        ).encode("latin-1")
        add_placeholder = [
            "BT",
            f"{PDF_PRIMARY_COLOR} rg",
            "/F2 24 Tf",
            "28 TL",
            "1 0 0 1 42 770 Tm",
            "(The Professors Academy) Tj",
            "T*",
            "(Confirmed Admission Forms) Tj",
            "ET",
            "BT",
            f"{PDF_MUTED_COLOR} rg",
            "/F1 13 Tf",
            "18 TL",
            "1 0 0 1 42 720 Tm",
            "(No confirmed admissions are available right now.) Tj",
            "T*",
            "(Once an admission is confirmed, its PDF form will appear here.) Tj",
            "ET",
        ]
        content_bytes += ("\n".join(add_placeholder)).encode("latin-1")
        content_object_id = document.add_stream_object("", content_bytes)
        page_ids.append(
            document.add_object(
                f"<< /Type /Page /Parent {pages_object_id} 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {regular_font_id} 0 R /F2 {bold_font_id} 0 R >> >> /Contents {content_object_id} 0 R >>"
            )
        )
    else:
        for student in students:
            image_info = None
            image_name = None
            image_object_id = None
            if student.get("photo"):
                image_path = STUDENT_PHOTOS_DIR / str(student["photo"])
                image_info = load_image_for_pdf(image_path)
                if image_info:
                    image_object_id = document.add_stream_object(
                        f"/Type /XObject /Subtype /Image /Width {image_info['width']} /Height {image_info['height']} /ColorSpace {image_info['color_space']} /BitsPerComponent {image_info['bits_per_component']} /Filter {image_info['filter_name']}",
                        image_info["stream"],
                    )
                    image_name = "Im1"

            content_bytes = build_student_page_commands(student, image_name, image_info)
            content_object_id = document.add_stream_object("", content_bytes)
            resources = f"<< /Font << /F1 {regular_font_id} 0 R /F2 {bold_font_id} 0 R >>"
            if image_object_id:
                resources += f" /XObject << /{image_name} {image_object_id} 0 R >>"
            resources += " >>"
            page_ids.append(
                document.add_object(
                    f"<< /Type /Page /Parent {pages_object_id} 0 R /MediaBox [0 0 595 842] /Resources {resources} /Contents {content_object_id} 0 R >>"
                )
            )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    document.set_object(pages_object_id, f"<< /Type /Pages /Count {len(page_ids)} /Kids [{kids}] >>")
    catalog_object_id = document.add_object(f"<< /Type /Catalog /Pages {pages_object_id} 0 R >>")
    return document.build(catalog_object_id)


def ensure_sample_pdf() -> None:
    sample_path = RESULTS_DIR / SAMPLE_RESULT_FILE
    if not sample_path.exists():
        sample_path.write_bytes(generate_simple_pdf("The Professors Academy - Annual Exam 2025 Result"))


def initialize_database() -> None:
    try:
        ensure_directories()
        ensure_sample_pdf()
        with get_connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS students (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    roll_number TEXT,
                    name TEXT NOT NULL,
                    father_name TEXT NOT NULL,
                    father_contact TEXT,
                    gender TEXT,
                    email TEXT,
                    date_of_birth TEXT,
                    mobile TEXT NOT NULL,
                    cnic TEXT NOT NULL,
                    photo TEXT NOT NULL,
                    class_name TEXT NOT NULL,
                    student_group TEXT,
                    subjects TEXT NOT NULL,
                    address TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            student_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(students)").fetchall()}
            if "roll_number" not in student_columns:
                cursor.execute("ALTER TABLE students ADD COLUMN roll_number INTEGER")
            if "father_contact" not in student_columns:
                cursor.execute("ALTER TABLE students ADD COLUMN father_contact TEXT")
            if "gender" not in student_columns:
                cursor.execute("ALTER TABLE students ADD COLUMN gender TEXT")
            if "email" not in student_columns:
                cursor.execute("ALTER TABLE students ADD COLUMN email TEXT")
            if "date_of_birth" not in student_columns:
                cursor.execute("ALTER TABLE students ADD COLUMN date_of_birth TEXT")
            if "confirmed_at" not in student_columns:
                cursor.execute("ALTER TABLE students ADD COLUMN confirmed_at TEXT")
            if "rejected_at" not in student_columns:
                cursor.execute("ALTER TABLE students ADD COLUMN rejected_at TEXT")
            assign_missing_roll_numbers(connection)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS announcements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    date TEXT NOT NULL,
                    is_new INTEGER NOT NULL DEFAULT 0,
                    is_published INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            announcement_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(announcements)").fetchall()}
            if "is_new" not in announcement_columns:
                cursor.execute("ALTER TABLE announcements ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0")
            if "is_published" not in announcement_columns:
                cursor.execute("ALTER TABLE announcements ADD COLUMN is_published INTEGER NOT NULL DEFAULT 1")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    class_name TEXT NOT NULL,
                    year TEXT NOT NULL,
                    pdf_filename TEXT NOT NULL,
                    upload_date TEXT NOT NULL,
                    is_new INTEGER NOT NULL DEFAULT 0,
                    is_published INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            result_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(results)").fetchall()}
            if "is_new" not in result_columns:
                cursor.execute("ALTER TABLE results ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0")
            if "is_published" not in result_columns:
                cursor.execute("ALTER TABLE results ADD COLUMN is_published INTEGER NOT NULL DEFAULT 1")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS faculty (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    photo TEXT,
                    class_assigned TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    qualification TEXT NOT NULL,
                    experience_years TEXT NOT NULL DEFAULT '5 Years+',
                    display_order INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            faculty_columns = {row["name"] for row in cursor.execute("PRAGMA table_info(faculty)").fetchall()}
            if "experience_years" not in faculty_columns:
                cursor.execute("ALTER TABLE faculty ADD COLUMN experience_years TEXT NOT NULL DEFAULT '5 Years+'")
            cursor.execute(
                """
                UPDATE faculty
                SET experience_years = '5 Years+'
                WHERE IFNULL(TRIM(experience_years), '') = ''
                """
            )
            faculty_rows = cursor.execute("SELECT id, class_assigned, subject FROM faculty").fetchall()
            for faculty_row in faculty_rows:
                normalized_sections = parse_faculty_section_assignments(
                    {
                        "class_assigned": faculty_row["class_assigned"],
                        "subject": faculty_row["subject"],
                    }
                )
                if not normalized_sections:
                    continue
                normalized_value = json.dumps(normalized_sections)
                if str(faculty_row["class_assigned"] or "").strip() != normalized_value:
                    cursor.execute(
                        "UPDATE faculty SET class_assigned = ? WHERE id = ?",
                        (normalized_value, faculty_row["id"]),
                    )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admin (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS site_visits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    page_path TEXT NOT NULL,
                    section_name TEXT NOT NULL,
                    visitor_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_created_at ON site_visits(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_section_name ON site_visits(section_name)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_visitor_hash ON site_visits(visitor_hash)")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS site_presence (
                    visitor_key TEXT PRIMARY KEY,
                    page_path TEXT NOT NULL,
                    section_name TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_presence_last_seen ON site_presence(last_seen)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_presence_section_name ON site_presence(section_name)")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_username TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_summary TEXT NOT NULL,
                    target_type TEXT,
                    target_id TEXT,
                    details_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON activity_log(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_activity_log_action_type ON activity_log(action_type)")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS visitor_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    full_name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    mobile TEXT NOT NULL,
                    message TEXT NOT NULL,
                    is_read INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_visitor_messages_created_at ON visitor_messages(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_visitor_messages_is_read ON visitor_messages(is_read)")
            seeded_admin = configured_admin_seed_credentials()
            admin_count = cursor.execute("SELECT COUNT(*) AS total FROM admin").fetchone()["total"]
            default_admin_row = cursor.execute(
                "SELECT id, username, password_hash FROM admin WHERE username = ?",
                (DEFAULT_ADMIN_USERNAME,),
            ).fetchone()
            legacy_admin_row = cursor.execute(
                "SELECT id, username, password_hash FROM admin WHERE username = ?",
                (LEGACY_DEFAULT_ADMIN_USERNAME,),
            ).fetchone()
            if seeded_admin:
                seeded_username, seeded_password = seeded_admin
                seeded_hash = generate_password_hash(seeded_password)
                cursor.execute(
                    """
                    INSERT INTO admin (username, password_hash)
                    VALUES (?, ?)
                    ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash
                    """,
                    (seeded_username, seeded_hash),
                )
                migratable_admin_row = None
                if default_admin_row and check_password_hash(default_admin_row["password_hash"], DEFAULT_ADMIN_PASSWORD):
                    migratable_admin_row = default_admin_row
                elif legacy_admin_row and check_password_hash(legacy_admin_row["password_hash"], LEGACY_DEFAULT_ADMIN_PASSWORD):
                    migratable_admin_row = legacy_admin_row
                if migratable_admin_row:
                    cursor.execute(
                        "UPDATE admin SET username = ?, password_hash = ? WHERE id = ?",
                        (seeded_username, seeded_hash, migratable_admin_row["id"]),
                    )
            else:
                if legacy_admin_row and check_password_hash(legacy_admin_row["password_hash"], LEGACY_DEFAULT_ADMIN_PASSWORD):
                    cursor.execute(
                        "UPDATE admin SET username = ?, password_hash = ? WHERE id = ?",
                        (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD), legacy_admin_row["id"]),
                    )
                elif admin_count == 0:
                    cursor.execute(
                        "INSERT INTO admin (username, password_hash) VALUES (?, ?)",
                        (DEFAULT_ADMIN_USERNAME, generate_password_hash(DEFAULT_ADMIN_PASSWORD)),
                    )
            announcements_count = cursor.execute("SELECT COUNT(*) AS total FROM announcements").fetchone()["total"]
            if announcements_count == 0:
                cursor.executemany(
                    "INSERT INTO announcements (title, description, date) VALUES (?, ?, ?)",
                    SAMPLE_ANNOUNCEMENTS,
                )
            faculty_count = cursor.execute("SELECT COUNT(*) AS total FROM faculty").fetchone()["total"]
            if faculty_count == 0:
                cursor.executemany(
                    """
                    INSERT INTO faculty (name, photo, class_assigned, subject, qualification, experience_years, display_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    SAMPLE_FACULTY,
                )
            results_count = cursor.execute("SELECT COUNT(*) AS total FROM results").fetchone()["total"]
            if results_count == 0:
                cursor.executemany(
                    """
                    INSERT INTO results (title, class_name, year, pdf_filename, upload_date)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    SAMPLE_RESULTS,
                )
            for key, value in DEFAULT_SETTINGS.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            map_setting = cursor.execute(
                "SELECT value FROM settings WHERE key = ?",
                ("map_embed_url",),
            ).fetchone()
            if map_setting and map_setting["value"] == LEGACY_MAP_EMBED_URL:
                cursor.execute(
                    "UPDATE settings SET value = ? WHERE key = ?",
                    (DEFAULT_SETTINGS["map_embed_url"], "map_embed_url"),
                )
            cursor.execute(
                "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                (
                    DEFAULT_SETTINGS["status_message_not_found"],
                    "status_message_not_found",
                    "No enrollment record matched the provided CNIC and date of birth. Please check the details and try again.",
                ),
            )
            cursor.execute(
                "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                (
                    DEFAULT_SETTINGS["status_message_not_found"],
                    "status_message_not_found",
                    "No enrollment record matched the provided CNIC number. Please check the details and try again.",
                ),
            )
            cursor.execute(
                "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                (
                    DEFAULT_SETTINGS["faq_item_2_answer"],
                    "faq_item_2_answer",
                    "Use the status check section with your CNIC number to view whether your application is pending, confirmed, or rejected.",
                ),
            )
            cursor.execute(
                "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                (
                    DEFAULT_SETTINGS["faq_item_2_answer"],
                    "faq_item_2_answer",
                    "Use the status check section with the same CNIC and full name entered in your enrollment form. The status popup will show whether your application is pending, confirmed, or rejected.",
                ),
            )
            cursor.execute(
                """
                UPDATE announcements
                SET description = REPLACE(REPLACE(description, 'Campus', 'Academy'), 'campus', 'academy')
                WHERE description LIKE '%Campus%' OR description LIKE '%campus%'
                """
            )
            cursor.execute(
                "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                (
                    DEFAULT_SETTINGS["hero_overlay_title"],
                    "hero_overlay_title",
                    "Admissions are open for the 2024-25 session.",
                ),
            )
            cursor.execute(
                """
                UPDATE announcements
                SET title = ?, description = ?, date = ?
                WHERE title = ? AND date = ?
                """,
                (
                    "Summer Vacations 2026",
                    "Academy will remain closed for summer break from June 1, 2026, to June 15, 2026.",
                    "2026-06-01",
                    "Summer Vacations 2024",
                    "2024-06-01",
                ),
            )
            cursor.execute(
                """
                UPDATE announcements
                SET title = ?, description = ?, date = ?
                WHERE title = ? AND date = ?
                """,
                (
                    "Admissions Open 2026-27",
                    "Admissions are now open for the 2026-27 academic session. Visit the enrollment section to apply online.",
                    "2026-03-15",
                    "Admissions Open 2024-25",
                    "2024-03-15",
                ),
            )
            cursor.execute(
                """
                UPDATE announcements
                SET description = ?, date = ?
                WHERE title = ? AND date = ?
                """,
                (
                    "Merit scholarship interviews for high achievers will be conducted on April 10, 2026, in the main seminar hall.",
                    "2026-04-10",
                    "Scholarship Interviews",
                    "2024-04-10",
                ),
            )
            cursor.execute(
                """
                UPDATE results
                SET title = ?, year = ?, pdf_filename = ?, upload_date = ?
                WHERE title = ? AND year = ?
                """,
                (
                    "Annual Exam 2025 Result",
                    "2025",
                    SAMPLE_RESULT_FILE,
                    "2026-01-15 10:00:00",
                    "Annual Exam 2023 Result",
                    "2023",
                ),
            )
            cursor.execute(
                """
                UPDATE results
                SET pdf_filename = ?, upload_date = ?
                WHERE pdf_filename = ? AND upload_date = ?
                """,
                (
                    SAMPLE_RESULT_FILE,
                    "2026-01-15 10:00:00",
                    "annual_exam_2023_result_sample.pdf",
                    "2024-01-15 10:00:00",
                ),
            )
            connection.commit()
    except Exception:
        logging.exception("Database initialization error")
        import sys
        sys.exit(1)


def is_allowed_extension(filename: str, allowed_extensions: Set[str]) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


def clean_limited_text(value: Any, field_name: str, max_length: int, *, allow_newlines: bool = False) -> str:
    raw = str(value or "")
    if TEXT_SANITIZE_PATTERN.search(raw):
        raise ValueError(f"{field_name} contains invalid characters.")
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not allow_newlines:
        normalized = re.sub(r"\s+", " ", normalized)
    else:
        normalized = re.sub(r"[ \t]+", " ", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} is too long.")
    return normalized


def client_ip_address() -> str:
    forwarded_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.remote_addr or "unknown"


def request_user_agent_hash() -> str:
    user_agent = (request.headers.get("User-Agent") or "").strip()
    if not user_agent:
        return ""
    return hashlib.sha256(user_agent.encode("utf-8", "ignore")).hexdigest()


def admin_remember_cookie_value(username: str, issued_at: Optional[int] = None) -> str:
    normalized_username = str(username or "").strip()
    if not normalized_username:
        return ""
    timestamp = int(issued_at or time.time())
    payload = f"{normalized_username}|{timestamp}"
    secret_key = str(app.config.get("SECRET_KEY") or "")
    signature = hashlib.sha256(f"{payload}|{secret_key}".encode("utf-8", "ignore")).hexdigest()
    return f"{payload}|{signature}"


def parse_admin_remember_cookie(raw_value: str) -> Optional[str]:
    token = str(raw_value or "").strip()
    if not token:
        return None
    parts = token.split("|")
    if len(parts) != 3:
        return None
    username, issued_at_text, signature = parts
    if not username or not issued_at_text or not signature:
        return None
    if not issued_at_text.isdigit():
        return None
    issued_at = int(issued_at_text)
    if issued_at <= 0:
        return None
    if time.time() - issued_at > ADMIN_REMEMBER_LIFETIME_DAYS * 24 * 60 * 60:
        return None
    expected = admin_remember_cookie_value(username, issued_at)
    if not expected:
        return None
    expected_signature = expected.rsplit("|", 1)[-1]
    if not secrets.compare_digest(signature, expected_signature):
        return None
    with get_connection() as connection:
        admin_row = connection.execute("SELECT username FROM admin WHERE username = ?", (username,)).fetchone()
    return admin_row["username"] if admin_row else None


def write_admin_session(username: str) -> None:
    session.clear()
    session["admin_username"] = username
    session["last_activity"] = datetime.utcnow().isoformat()
    session["admin_ua_hash"] = request_user_agent_hash()
    session.permanent = True


def apply_admin_remember_cookie(response, username: str):
    cookie_value = admin_remember_cookie_value(username)
    response.set_cookie(
        ADMIN_REMEMBER_COOKIE_NAME,
        cookie_value,
        max_age=ADMIN_REMEMBER_LIFETIME_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=bool(app.config.get("SESSION_COOKIE_SECURE")),
        samesite=str(app.config.get("SESSION_COOKIE_SAMESITE") or "Lax"),
        path=ADMIN_PANEL_PATH,
    )
    return response


def clear_admin_remember_cookie(response):
    response.delete_cookie(
        ADMIN_REMEMBER_COOKIE_NAME,
        path=ADMIN_PANEL_PATH,
        httponly=True,
        secure=bool(app.config.get("SESSION_COOKIE_SECURE")),
        samesite=str(app.config.get("SESSION_COOKIE_SAMESITE") or "Lax"),
    )
    return response


def restore_admin_session_from_cookie() -> Optional[str]:
    restored_username = parse_admin_remember_cookie(request.cookies.get(ADMIN_REMEMBER_COOKIE_NAME) or "")
    if not restored_username:
        return None
    write_admin_session(restored_username)
    return restored_username


def request_visitor_hash() -> str:
    user_agent = (request.headers.get("User-Agent") or "").strip()
    raw_value = f"{client_ip_address()}|{user_agent}"
    return hashlib.sha256(raw_value.encode("utf-8", "ignore")).hexdigest()


def is_probable_bot_request() -> bool:
    user_agent = (request.headers.get("User-Agent") or "").lower()
    return any(marker in user_agent for marker in ("bot", "crawl", "spider", "preview", "slurp"))


def normalize_analytics_section(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ANALYTICS_SECTION_CHOICES else "home"


def record_site_visit(section_name: str, page_path: str = "/") -> None:
    if is_probable_bot_request():
        return
    normalized_section = normalize_analytics_section(section_name)
    safe_page_path = str(page_path or "/").strip() or "/"
    if len(safe_page_path) > 160:
        safe_page_path = "/"
    with get_connection() as connection:
        connection.execute(
            "INSERT INTO site_visits (page_path, section_name, visitor_hash, created_at) VALUES (?, ?, ?, ?)",
            (safe_page_path, normalized_section, request_visitor_hash(), current_timestamp()),
        )
        connection.commit()


def normalize_presence_id(value: Any) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{12,120}", candidate):
        return ""
    return candidate


def build_presence_visitor_key(presence_id: str = "") -> str:
    base_value = request_visitor_hash()
    if presence_id:
        raw_value = f"{base_value}|{presence_id}"
    else:
        raw_value = base_value
    return hashlib.sha256(raw_value.encode("utf-8", "ignore")).hexdigest()


def record_site_presence(section_name: str, page_path: str = "/", presence_id: str = "") -> None:
    if is_probable_bot_request():
        return
    normalized_section = normalize_analytics_section(section_name)
    safe_page_path = str(page_path or "/").strip() or "/"
    if len(safe_page_path) > 160 or not safe_page_path.startswith("/"):
        safe_page_path = "/"
    normalized_presence_id = normalize_presence_id(presence_id)
    visitor_key = build_presence_visitor_key(normalized_presence_id)
    now_timestamp = current_timestamp()
    stale_before = (datetime.now() - timedelta(hours=LIVE_VISITOR_RETENTION_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as connection:
        connection.execute("DELETE FROM site_presence WHERE last_seen < ?", (stale_before,))
        connection.execute(
            """
            INSERT INTO site_presence (visitor_key, page_path, section_name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(visitor_key) DO UPDATE SET
                page_path = excluded.page_path,
                section_name = excluded.section_name,
                last_seen = excluded.last_seen
            """,
            (visitor_key, safe_page_path, normalized_section, now_timestamp, now_timestamp),
        )
        connection.commit()


def is_same_origin_request() -> bool:
    origin = (request.headers.get("Origin") or "").strip()
    referer = (request.headers.get("Referer") or "").strip()
    if not origin and not referer:
        sec_fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sec_fetch_site in {"same-origin", "same-site", "none"}:
            return True
        return not is_hosted_runtime()

    expected_host = (request.host or "").strip().lower()
    if not expected_host:
        return False

    def host_matches(candidate_url: str) -> bool:
        try:
            parsed = urlparse(candidate_url)
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        return (parsed.netloc or "").strip().lower() == expected_host

    if origin:
        return host_matches(origin)
    return host_matches(referer)


def validate_same_origin_request() -> Optional[Any]:
    if is_same_origin_request():
        return None
    return json_error("Security validation failed. Please refresh the page and try again.", 403)


def enforce_rate_limit(rule_name: str) -> Optional[Any]:
    rule = RATE_LIMIT_RULES.get(rule_name)
    if not rule:
        return None
    ip_address = client_ip_address()
    key = f"{rule_name}:{ip_address}"
    now = time.time()
    active_hits = [value for value in RATE_LIMIT_STATE.get(key, []) if now - value < rule["window_seconds"]]
    if len(active_hits) >= rule["limit"]:
        return json_error(rule["message"], 429)
    active_hits.append(now)
    RATE_LIMIT_STATE[key] = active_hits
    return None


def generate_admin_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_admin_csrf() -> Optional[Any]:
    expected = session.get("csrf_token")
    provided = (request.headers.get("X-CSRF-Token") or "").strip()
    if not expected or not provided or not secrets.compare_digest(str(expected), provided):
        return json_error("Security validation failed. Please refresh the admin panel and try again.", 403)
    return None


def require_public_post_security() -> Optional[Any]:
    if request.method == "POST":
        return validate_same_origin_request()
    return None


def reject_executable_payload(payload: bytes) -> None:
    payload_start = payload[:8]
    for signature in EXECUTABLE_SIGNATURES:
        if payload_start.startswith(signature):
            raise ValueError("Executable, archive, or script files are not allowed.")


def validate_image_payload(payload: bytes, extension: str) -> None:
    reject_executable_payload(payload)
    expected_extension = extension.lower()
    if expected_extension in {"jpg", "jpeg"} and not payload.startswith(b"\xff\xd8"):
        raise ValueError("Only valid JPG images are allowed.")
    if expected_extension == "png" and not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Only valid PNG images are allowed.")
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{expected_extension}") as temp_file:
            temp_file.write(payload)
            temp_path = Path(temp_file.name)
        try:
            parsed = (
                parse_jpeg_for_pdf(temp_path)
                if expected_extension in {"jpg", "jpeg"}
                else parse_png_for_pdf(temp_path)
            )
        except (OSError, ValueError, struct.error, zlib.error):
            parsed = None
    finally:
        if "temp_path" in locals():
            temp_path.unlink(missing_ok=True)
    if not parsed:
        raise ValueError("The uploaded image is invalid or unsafe.")
    if int(parsed.get("width") or 0) <= 0 or int(parsed.get("height") or 0) <= 0:
        raise ValueError("The uploaded image is invalid or unsafe.")
    if int(parsed["width"]) > 6000 or int(parsed["height"]) > 6000:
        raise ValueError("The uploaded image dimensions are too large.")


def validate_pdf_payload(payload: bytes) -> None:
    reject_executable_payload(payload)
    stripped = payload.lstrip()
    if not stripped.startswith(b"%PDF-"):
        raise ValueError("Only valid PDF files are allowed.")
    payload_lower = payload.lower()
    if b"%%eof" not in payload_lower[-2048:]:
        raise ValueError("The uploaded PDF appears incomplete or corrupted.")
    for marker in PDF_ACTIVE_CONTENT_MARKERS:
        if marker in payload_lower:
            raise ValueError("The uploaded PDF contains blocked active content and was rejected for security reasons.")


def validate_upload_payload(payload: bytes, extension: str, allowed_extensions: Set[str]) -> None:
    normalized_extension = extension.lower()
    if normalized_extension in ALLOWED_IMAGE_EXTENSIONS:
        validate_image_payload(payload, normalized_extension)
        return
    if normalized_extension in ALLOWED_RESULT_EXTENSIONS:
        validate_pdf_payload(payload)
        return
    raise ValueError(f"Allowed file types: {', '.join(sorted(allowed_extensions))}.")


def save_uploaded_file(file_storage, target_directory: Path, allowed_extensions: Set[str], max_size: int, prefix: str) -> str:
    original_name = secure_filename(file_storage.filename or "")
    if not original_name:
        raise ValueError("Please choose a valid file.")

    if not is_allowed_extension(original_name, allowed_extensions):
        allowed = ", ".join(sorted(allowed_extensions))
        raise ValueError(f"Allowed file types: {allowed}.")

    payload = file_storage.read()
    if not payload:
        raise ValueError("Uploaded file is empty.")

    if len(payload) > max_size:
        raise ValueError(f"File is too large. Maximum allowed size is {max_size // 1024} KB.")

    extension = original_name.rsplit(".", 1)[1].lower()
    validate_upload_payload(payload, extension, allowed_extensions)
    unique_name = f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}.{extension}"
    (target_directory / unique_name).write_bytes(payload)
    return unique_name


def delete_file(directory: Path, filename: Optional[str]) -> None:
    if not filename:
        return
    file_path = directory / filename
    if file_path.exists() and file_path.is_file():
        file_path.unlink()


def gallery_image_filename_from_value(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw.startswith("/uploads/gallery_images/"):
        return None
    filename = raw.split("/uploads/gallery_images/", 1)[1].strip().lstrip("/")
    if not filename or "/" in filename or "\\" in filename:
        return None
    return filename


def normalize_cnic(value: str) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 13:
        return None
    return f"{digits[:5]}-{digits[5:12]}-{digits[12]}"


def normalize_date_only(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_mobile(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or not re.fullmatch(r"[+\d\s]+", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0092"):
        digits = digits[4:]
    elif digits.startswith("92") and len(digits) > 10:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) > 10:
        digits = digits[1:]
    if re.fullmatch(r"\d{10}", digits):
        return f"+92 {digits}"
    return None


def normalize_email(value: str) -> Optional[str]:
    normalized = (value or "").strip().lower()
    if not normalized or len(normalized) > 254:
        return None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}", normalized):
        return None
    return normalized


def sanitize_visitor_message_payload(raw_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    try:
        full_name = clean_limited_text(raw_data.get("full_name") or raw_data.get("fullName") or "", "Full name", 120)
        email = normalize_email(raw_data.get("email") or "")
        mobile = normalize_mobile(raw_data.get("mobile") or raw_data.get("phone") or "")
        message = clean_limited_text(raw_data.get("message") or "", "Message", 1800, allow_newlines=True)
    except ValueError as error:
        return str(error), None

    if not full_name:
        return "Please enter your full name.", None
    if not email:
        return "Please enter a valid email address.", None
    if not mobile:
        return "Please enter a valid mobile number in +92 1234567890 format.", None
    if not message:
        return "Please enter your message.", None

    message_word_count = len(re.findall(r"\S+", message))
    if message_word_count > MAX_VISITOR_MESSAGE_WORDS:
        return f"Please keep your message within {MAX_VISITOR_MESSAGE_WORDS} words.", None

    return None, {
        "full_name": full_name,
        "email": email,
        "mobile": mobile,
        "message": message,
    }


def normalize_whatsapp_number(value: str) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return None
    if digits.startswith("0"):
        digits = f"92{digits[1:]}"
    elif not digits.startswith("92") and len(digits) == 10:
        digits = f"92{digits}"
    if len(digits) < 11 or len(digits) > 15:
        return None
    return digits


def normalize_facebook_url(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 320:
        return None
    if not re.match(r"^[a-z]+://", raw, flags=re.IGNORECASE):
        raw = f"https://{raw.lstrip('/')}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.netloc or "").strip()
    if not host:
        return None
    return raw


def normalize_public_image_url(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > 600:
        return None
    if raw.startswith("/uploads/"):
        return raw
    if not re.match(r"^[a-z]+://", raw, flags=re.IGNORECASE):
        raw = f"https://{raw.lstrip('/')}"
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    if not (parsed.netloc or "").strip():
        return None
    return raw


def normalize_date_of_birth(value: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        digits_only = re.sub(r"\D", "", raw)
        if len(digits_only) == 8:
            compact_value = f"{digits_only[:2]}/{digits_only[2:4]}/{digits_only[4:8]}"
            try:
                parsed = datetime.strptime(compact_value, "%d/%m/%Y").date()
            except ValueError:
                parsed = None
    if parsed is None:
        return None
    today = datetime.now().date()
    if parsed > today or parsed.year < 1900:
        return None
    return parsed.isoformat()


def normalize_calendar_date(value: str) -> Optional[str]:
    raw = (value or "").strip()
    if not raw:
        return current_date()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize_year_value(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not re.fullmatch(r"\d{4}", raw):
        return None
    year_value = int(raw)
    current_year_value = datetime.now().year
    if year_value < 2000 or year_value > current_year_value + 2:
        return None
    return raw


def normalize_map_embed_url(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if len(raw) > 600:
        return None
    if not raw.startswith("https://www.google.com/maps"):
        return None
    if "output=embed" not in raw:
        return None
    return raw


def parse_enrollment_class_allowlist(value: Any) -> List[str]:
    raw_choices: List[str] = []
    if isinstance(value, list):
        raw_choices = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, list):
                    raw_choices = [str(item).strip() for item in decoded if str(item).strip()]
                else:
                    raw_choices = [part.strip() for part in stripped.split(",") if part.strip()]
            except json.JSONDecodeError:
                raw_choices = [part.strip() for part in stripped.split(",") if part.strip()]
    allowed = [choice for choice in ENROLLMENT_CLASS_CHOICES if choice in raw_choices]
    return allowed or ENROLLMENT_CLASS_CHOICES.copy()


def parse_faculty_section_assignments(value: Any) -> List[str]:
    def normalize_choice(choice_value: Any, subject_value: Any = "") -> List[str]:
        choice = str(choice_value or "").strip()
        if not choice:
            return []
        if choice in FACULTY_SECTION_CHOICES:
            return [choice]

        compact = choice.upper().replace(" ", "")
        subject_normalized = str(subject_value or "").strip().upper()
        has_pre_med = any(token in compact for token in ("PRE-MED", "PREMED", "PRE-MEDICAL", "PREMEDICAL", "P.M"))
        has_pre_eng = any(token in compact for token in ("PRE-ENG", "PREENG", "PRE-ENGINEERING", "PREENGINEERING", "P.E"))
        has_mdcat = "MDCAT" in compact
        has_ecat = "ECAT" in compact
        is_ix_x = "IX-X" in compact or compact.endswith("IX") or compact.endswith("X")
        is_xi_xii = "XI-XII" in compact or compact.endswith("XI") or compact.endswith("XII")

        if is_ix_x:
            return ["Class IX-X"]
        if is_xi_xii and has_pre_med:
            return ["XI-XII | Pre-Med"]
        if is_xi_xii and has_pre_eng:
            return ["XI-XII | Pre-Eng"]
        if is_xi_xii and (has_mdcat or has_ecat):
            if re.search(r"MATH", subject_normalized):
                return ["XI-XII | Pre-Eng"]
            if re.search(r"BIO|BOTANY|ZOOLOGY", subject_normalized):
                return ["XI-XII | Pre-Med"]
            return ["XI-XII | Pre-Med", "XI-XII | Pre-Eng"]
        if is_xi_xii:
            if re.search(r"MATH", subject_normalized):
                return ["XI-XII | Pre-Eng"]
            if re.search(r"BIO|BOTANY|ZOOLOGY", subject_normalized):
                return ["XI-XII | Pre-Med"]
            return ["XI-XII | Pre-Med", "XI-XII | Pre-Eng"]
        if has_mdcat and has_ecat:
            return ["MDCAT", "ECAT"]
        if has_mdcat:
            return ["MDCAT"]
        if has_ecat:
            return ["ECAT"]
        return []

    raw_choices: List[str] = []
    subject_value = ""
    if isinstance(value, dict):
        subject_value = value.get("subject") or ""
        value = value.get("class_assigned") or value.get("faculty_sections") or ""
    if isinstance(value, list):
        raw_choices = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                decoded = json.loads(stripped)
                if isinstance(decoded, list):
                    raw_choices = [str(item).strip() for item in decoded if str(item).strip()]
                else:
                    raw_choices = [part.strip() for part in stripped.split(",") if part.strip()]
            except json.JSONDecodeError:
                raw_choices = [part.strip() for part in stripped.split(",") if part.strip()]
    normalized_choices: List[str] = []
    for item in raw_choices:
        for normalized_choice in normalize_choice(item, subject_value):
            if normalized_choice not in normalized_choices:
                normalized_choices.append(normalized_choice)
    return normalized_choices


def parse_subjects(raw_value) -> List[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped:
            return []
        try:
            decoded = json.loads(stripped)
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        except json.JSONDecodeError:
            pass
        return [part.strip() for part in stripped.split(",") if part.strip()]
    return []


def resolve_subjects(class_name: str, student_group: Optional[str], subjects: List[str]) -> Tuple[Optional[str], Optional[List[str]]]:
    if class_name in {"IX", "X"}:
        cleaned = [subject for subject in subjects if subject in SECONDARY_SUBJECTS]
        if not cleaned:
            return "Please choose at least one subject.", None
        return None, cleaned

    if class_name == "MDCAT Prep":
        allowed_subjects = HIGHER_SECONDARY_GROUPS["Pre-Medical"]
        cleaned = [subject for subject in subjects if subject in allowed_subjects]
        if not cleaned:
            return "Please choose at least one subject for MDCAT Prep.", None
        return None, cleaned

    if class_name == "ECAT Prep":
        allowed_subjects = HIGHER_SECONDARY_GROUPS["Pre-Engineering"]
        cleaned = [subject for subject in subjects if subject in allowed_subjects]
        if not cleaned:
            return "Please choose at least one subject for ECAT Prep.", None
        return None, cleaned

    if class_name in {"XI", "XII"}:
        if student_group not in HIGHER_SECONDARY_GROUPS:
            return "Please choose either Pre-Medical or Pre-Engineering.", None
        allowed_subjects = HIGHER_SECONDARY_GROUPS[student_group]
        cleaned = [subject for subject in subjects if subject in allowed_subjects]
        if not cleaned:
            return "Please choose at least one subject for the selected group.", None
        return None, cleaned

    return "Please select a valid class.", None


def normalize_student_enrollment_payload(raw_form) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    try:
        name = clean_limited_text(raw_form.get("full_name") or raw_form.get("name") or "", "Full name", 120)
        father_name = clean_limited_text(raw_form.get("father_name") or "", "Father name", 120)
        address = clean_limited_text(raw_form.get("address") or "", "Address", 400, allow_newlines=True)
    except ValueError as error:
        return str(error), None
    father_contact = normalize_mobile(raw_form.get("father_contact") or "")
    gender = (raw_form.get("gender") or "").strip().title()
    email = normalize_email(raw_form.get("email") or "")
    date_of_birth = normalize_date_of_birth(raw_form.get("date_of_birth") or "")
    mobile = normalize_mobile(raw_form.get("mobile") or "")
    cnic = normalize_cnic(raw_form.get("cnic") or "")
    class_name = str(raw_form.get("class") or "").strip()
    student_group = (raw_form.get("group") or "").strip() or None
    subjects = parse_subjects(raw_form.get("subjects"))

    if class_name == "MDCAT Prep":
        student_group = "Pre-Medical"
    elif class_name == "ECAT Prep":
        student_group = "Pre-Engineering"

    if not name:
        return "Full name is required.", None
    if not father_name:
        return "Father name is required.", None
    if not father_contact:
        return "Please enter a valid father contact number in +92 1234567890 format.", None
    if gender not in ALLOWED_GENDERS:
        return "Please select a valid gender.", None
    if not email:
        return "Please enter a valid email address.", None
    if not date_of_birth:
        return "Please enter a valid date of birth in DD/MM/YYYY format.", None
    if not mobile:
        return "Please enter a valid mobile number in +92 1234567890 format.", None
    if not cnic:
        return "Please enter a valid CNIC number.", None
    if class_name not in VALID_CLASS_CHOICES:
        return "Please select a valid class.", None
    if not address:
        return "Address is required.", None

    subject_error, resolved_subjects = resolve_subjects(class_name, student_group, subjects)
    if subject_error:
        return subject_error, None

    return (
        None,
        {
            "name": name,
            "father_name": father_name,
            "father_contact": father_contact,
            "gender": gender,
            "email": email,
            "date_of_birth": date_of_birth,
            "mobile": mobile,
            "cnic": cnic,
            "class_name": class_name,
            "student_group": student_group,
            "subjects": resolved_subjects or [],
            "subjects_json": json.dumps(resolved_subjects or []),
            "address": address,
        },
    )


def sanitize_announcement_payload(raw_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    try:
        title = clean_limited_text(raw_data.get("title") or "", "Title", 160)
        description = clean_limited_text(raw_data.get("description") or "", "Description", 900, allow_newlines=True)
    except ValueError as error:
        return str(error), None
    announcement_date = normalize_calendar_date(raw_data.get("date") or "")
    is_new = normalize_admin_flag(raw_data.get("is_new"))
    is_published = normalize_admin_flag(raw_data.get("is_published"), default=True)
    if not title or not description:
        return "Title and description are required.", None
    if not announcement_date:
        return "Please enter a valid announcement date.", None
    return None, {"title": title, "description": description, "date": announcement_date, "is_new": is_new, "is_published": is_published}


def sanitize_result_payload(raw_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    try:
        title = clean_limited_text(raw_data.get("title") or "", "Title", 160)
        class_name = clean_limited_text(raw_data.get("class") or raw_data.get("class_name") or "", "Class", 80)
    except ValueError as error:
        return str(error), None
    year = normalize_year_value(raw_data.get("year") or "")
    is_new = normalize_admin_flag(raw_data.get("is_new"))
    is_published = normalize_admin_flag(raw_data.get("is_published"), default=True)
    if not title or not class_name or not year:
        return "Title, class, and year are required.", None
    return None, {"title": title, "class_name": class_name, "year": year, "is_new": is_new, "is_published": is_published}


def sanitize_faculty_payload(raw_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    try:
        name = clean_limited_text(raw_data.get("name") or "", "Name", 120)
        subject = clean_limited_text(raw_data.get("subject") or "", "Subject", 120)
        qualification = clean_limited_text(raw_data.get("qualification") or "", "Qualification", 160)
        experience_years = clean_limited_text(raw_data.get("experience_years") or "", "Experience", 40)
    except ValueError as error:
        return str(error), None
    faculty_sections = parse_faculty_section_assignments(
        {
            "class_assigned": raw_data.get("class_assigned") or raw_data.get("faculty_sections"),
            "subject": subject,
        }
    )
    faculty_level = str(raw_data.get("faculty_level") or "").strip()
    faculty_track = str(raw_data.get("faculty_track") or "").strip()
    if not faculty_sections and faculty_level:
        if faculty_level not in {"Class IX-X", "XI-XII", "MDCAT", "ECAT"}:
            return "Please select a valid faculty section.", None
        if faculty_level == "XI-XII":
            if faculty_track not in {"Pre-Med", "Pre-Eng"}:
                return "Please choose Pre-Med or Pre-Eng for XI-XII faculty.", None
            faculty_sections = [f"XI-XII | {faculty_track}"]
        else:
            faculty_sections = [faculty_level]
    if not faculty_sections:
        try:
            legacy_class_assigned = clean_limited_text(raw_data.get("class_assigned") or "", "Class assigned", 160)
        except ValueError as error:
            return str(error), None
        faculty_sections = parse_faculty_section_assignments({"class_assigned": legacy_class_assigned, "subject": subject})
    if not name or not faculty_sections or not subject or not qualification or not experience_years:
        return "Name, faculty teaching section, subject, qualification, and experience are required.", None
    return None, {
        "name": name,
        "class_assigned": json.dumps(faculty_sections),
        "subject": subject,
        "qualification": qualification,
        "experience_years": experience_years,
    }


def sanitize_settings_payload(raw_data: Dict[str, Any]) -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    try:
        contact_primary = clean_limited_text(raw_data.get("contact_primary") or "", "Primary contact", 40)
        contact_secondary = clean_limited_text(raw_data.get("contact_secondary") or "", "Secondary contact", 40)
        office_timing = clean_limited_text(raw_data.get("office_timing") or "", "Office timing", 80)
        address = clean_limited_text(raw_data.get("address") or "", "Address", 260, allow_newlines=True)
        hero_badge = clean_limited_text(raw_data.get("hero_badge") or "", "Hero badge", 60)
        hero_heading = clean_limited_text(raw_data.get("hero_heading") or "", "Hero heading", 180)
        hero_description = clean_limited_text(
            raw_data.get("hero_description") or "",
            "Hero description",
            320,
            allow_newlines=True,
        )
        hero_overlay_title = clean_limited_text(raw_data.get("hero_overlay_title") or "", "Hero overlay title", 120)
        hero_overlay_description = clean_limited_text(
            raw_data.get("hero_overlay_description") or "",
            "Hero overlay description",
            220,
            allow_newlines=True,
        )
        enrollment_info_badge = clean_limited_text(raw_data.get("enrollment_info_badge") or "", "Enrollment info badge", 80)
        enrollment_info_heading = clean_limited_text(raw_data.get("enrollment_info_heading") or "", "Enrollment info heading", 180)
        enrollment_info_description = clean_limited_text(
            raw_data.get("enrollment_info_description") or "",
            "Enrollment info description",
            320,
            allow_newlines=True,
        )
        enrollment_card_1_label = clean_limited_text(raw_data.get("enrollment_card_1_label") or "", "Enrollment card 1 label", 80)
        enrollment_card_1_title = clean_limited_text(raw_data.get("enrollment_card_1_title") or "", "Enrollment card 1 title", 80)
        enrollment_card_1_description = clean_limited_text(
            raw_data.get("enrollment_card_1_description") or "",
            "Enrollment card 1 description",
            220,
            allow_newlines=True,
        )
        enrollment_card_2_label = clean_limited_text(raw_data.get("enrollment_card_2_label") or "", "Enrollment card 2 label", 80)
        enrollment_card_2_title = clean_limited_text(raw_data.get("enrollment_card_2_title") or "", "Enrollment card 2 title", 80)
        enrollment_card_2_description = clean_limited_text(
            raw_data.get("enrollment_card_2_description") or "",
            "Enrollment card 2 description",
            220,
            allow_newlines=True,
        )
        enrollment_card_3_label = clean_limited_text(raw_data.get("enrollment_card_3_label") or "", "Enrollment card 3 label", 80)
        enrollment_card_3_title = clean_limited_text(raw_data.get("enrollment_card_3_title") or "", "Enrollment card 3 title", 80)
        enrollment_card_3_description = clean_limited_text(
            raw_data.get("enrollment_card_3_description") or "",
            "Enrollment card 3 description",
            220,
            allow_newlines=True,
        )
        message_badge = clean_limited_text(raw_data.get("message_badge") or "", "Message badge", 80)
        message_heading = clean_limited_text(raw_data.get("message_heading") or "", "Message heading", 180)
        message_description = clean_limited_text(
            raw_data.get("message_description") or "",
            "Message body",
            420,
            allow_newlines=True,
        )
        message_author_name = clean_limited_text(raw_data.get("message_author_name") or "", "Message author name", 100)
        message_author_title = clean_limited_text(raw_data.get("message_author_title") or "", "Message author title", 120)
        gallery_badge = clean_limited_text(raw_data.get("gallery_badge") or "", "Gallery badge", 80)
        gallery_heading = clean_limited_text(raw_data.get("gallery_heading") or "", "Gallery heading", 180)
        gallery_description = clean_limited_text(
            raw_data.get("gallery_description") or "",
            "Gallery description",
            320,
            allow_newlines=True,
        )
        gallery_item_1_label = clean_limited_text(raw_data.get("gallery_item_1_label") or "", "Gallery card 1 label", 60)
        gallery_item_1_title = clean_limited_text(raw_data.get("gallery_item_1_title") or "", "Gallery card 1 title", 100)
        gallery_item_1_description = clean_limited_text(
            raw_data.get("gallery_item_1_description") or "",
            "Gallery card 1 description",
            220,
            allow_newlines=True,
        )
        gallery_item_2_label = clean_limited_text(raw_data.get("gallery_item_2_label") or "", "Gallery card 2 label", 60)
        gallery_item_2_title = clean_limited_text(raw_data.get("gallery_item_2_title") or "", "Gallery card 2 title", 100)
        gallery_item_2_description = clean_limited_text(
            raw_data.get("gallery_item_2_description") or "",
            "Gallery card 2 description",
            220,
            allow_newlines=True,
        )
        gallery_item_3_label = clean_limited_text(raw_data.get("gallery_item_3_label") or "", "Gallery card 3 label", 60)
        gallery_item_3_title = clean_limited_text(raw_data.get("gallery_item_3_title") or "", "Gallery card 3 title", 100)
        gallery_item_3_description = clean_limited_text(
            raw_data.get("gallery_item_3_description") or "",
            "Gallery card 3 description",
            220,
            allow_newlines=True,
        )
        gallery_item_4_label = clean_limited_text(raw_data.get("gallery_item_4_label") or "", "Gallery card 4 label", 60)
        gallery_item_4_title = clean_limited_text(raw_data.get("gallery_item_4_title") or "", "Gallery card 4 title", 100)
        gallery_item_4_description = clean_limited_text(
            raw_data.get("gallery_item_4_description") or "",
            "Gallery card 4 description",
            220,
            allow_newlines=True,
        )
        faq_badge = clean_limited_text(raw_data.get("faq_badge") or "", "FAQ badge", 80)
        faq_heading = clean_limited_text(raw_data.get("faq_heading") or "", "FAQ heading", 180)
        faq_description = clean_limited_text(
            raw_data.get("faq_description") or "",
            "FAQ description",
            320,
            allow_newlines=True,
        )
        faq_item_1_question = clean_limited_text(raw_data.get("faq_item_1_question") or "", "FAQ question 1", 180)
        faq_item_1_answer = clean_limited_text(
            raw_data.get("faq_item_1_answer") or "",
            "FAQ answer 1",
            320,
            allow_newlines=True,
        )
        faq_item_2_question = clean_limited_text(raw_data.get("faq_item_2_question") or "", "FAQ question 2", 180)
        faq_item_2_answer = clean_limited_text(
            raw_data.get("faq_item_2_answer") or "",
            "FAQ answer 2",
            320,
            allow_newlines=True,
        )
        faq_item_3_question = clean_limited_text(raw_data.get("faq_item_3_question") or "", "FAQ question 3", 180)
        faq_item_3_answer = clean_limited_text(
            raw_data.get("faq_item_3_answer") or "",
            "FAQ answer 3",
            320,
            allow_newlines=True,
        )
        faq_item_4_question = clean_limited_text(raw_data.get("faq_item_4_question") or "", "FAQ question 4", 180)
        faq_item_4_answer = clean_limited_text(
            raw_data.get("faq_item_4_answer") or "",
            "FAQ answer 4",
            320,
            allow_newlines=True,
        )
        home_stats_enabled = "1" if str(raw_data.get("home_stats_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0"
        home_announcements_enabled = "1" if str(raw_data.get("home_announcements_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0"
        home_message_enabled = "1" if str(raw_data.get("home_message_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0"
        home_gallery_enabled = "1" if str(raw_data.get("home_gallery_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0"
        home_faq_enabled = "1" if str(raw_data.get("home_faq_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0"
        whatsapp_message = clean_limited_text(
            raw_data.get("whatsapp_message") or "",
            "WhatsApp message",
            220,
            allow_newlines=True,
        )
        enrollment_closed_message = clean_limited_text(
            raw_data.get("enrollment_closed_message") or "",
            "Enrollment closed message",
            220,
            allow_newlines=True,
        )
        marquee_text = clean_limited_text(raw_data.get("marquee_text") or "", "Marquee text", 240)
        homepage_popup_title = clean_limited_text(raw_data.get("homepage_popup_title") or "", "Homepage popup title", 120)
        homepage_popup_message = clean_limited_text(
            raw_data.get("homepage_popup_message") or "",
            "Homepage popup message",
            320,
            allow_newlines=True,
        )
        homepage_popup_button_label = clean_limited_text(
            raw_data.get("homepage_popup_button_label") or "",
            "Homepage popup button label",
            40,
        )
        status_message_pending = clean_limited_text(
            raw_data.get("status_message_pending") or "",
            "Pending status message",
            260,
            allow_newlines=True,
        )
        status_message_confirmed = clean_limited_text(
            raw_data.get("status_message_confirmed") or "",
            "Confirmed status message",
            260,
            allow_newlines=True,
        )
        status_message_rejected = clean_limited_text(
            raw_data.get("status_message_rejected") or "",
            "Rejected status message",
            260,
            allow_newlines=True,
        )
        status_message_not_found = clean_limited_text(
            raw_data.get("status_message_not_found") or "",
            "Record not found status message",
            260,
            allow_newlines=True,
        )
        status_check_disabled_message = clean_limited_text(
            raw_data.get("status_check_disabled_message") or "",
            "Status check disabled message",
            260,
            allow_newlines=True,
        )
        admission_form_note = clean_limited_text(
            raw_data.get("admission_form_note") or "",
            "Admission form important note",
            320,
            allow_newlines=True,
        )
        enrollment_class_allowlist = parse_enrollment_class_allowlist(raw_data.get("enrollment_class_allowlist") or ENROLLMENT_CLASS_CHOICES)
    except ValueError as error:
        return str(error), None

    email = normalize_email(raw_data.get("email") or "")
    facebook_url = normalize_facebook_url(raw_data.get("facebook_url") or "")
    whatsapp_enabled = "1" if str(raw_data.get("whatsapp_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0"
    whatsapp_number = normalize_whatsapp_number(raw_data.get("whatsapp_number") or "")
    map_embed_url = normalize_map_embed_url(raw_data.get("map_embed_url") or "")
    gallery_item_1_image = normalize_public_image_url(raw_data.get("gallery_item_1_image") or "")
    gallery_item_2_image = normalize_public_image_url(raw_data.get("gallery_item_2_image") or "")
    gallery_item_3_image = normalize_public_image_url(raw_data.get("gallery_item_3_image") or "")
    gallery_item_4_image = normalize_public_image_url(raw_data.get("gallery_item_4_image") or "")
    if not contact_primary or not address or not email:
        return "Primary contact, address, and email are required.", None
    if str(raw_data.get("facebook_url") or "").strip() and facebook_url is None:
        return "Please enter a valid website link for the Facebook button.", None
    if not office_timing:
        return "Office timing is required.", None
    if whatsapp_enabled == "1" and not whatsapp_number:
        return "Please enter a valid WhatsApp number for the public button.", None
    if not map_embed_url:
        return "Please enter a valid Google Maps embed URL.", None
    if str(raw_data.get("gallery_item_1_image") or "").strip() and gallery_item_1_image is None:
        return "Please enter a valid image URL for gallery card 1.", None
    if str(raw_data.get("gallery_item_2_image") or "").strip() and gallery_item_2_image is None:
        return "Please enter a valid image URL for gallery card 2.", None
    if str(raw_data.get("gallery_item_3_image") or "").strip() and gallery_item_3_image is None:
        return "Please enter a valid image URL for gallery card 3.", None
    if str(raw_data.get("gallery_item_4_image") or "").strip() and gallery_item_4_image is None:
        return "Please enter a valid image URL for gallery card 4.", None

    return None, {
        "contact_primary": contact_primary,
        "contact_secondary": contact_secondary,
        "office_timing": office_timing,
        "address": address,
        "email": email,
        "facebook_url": facebook_url or "",
        "hero_badge": hero_badge,
        "hero_heading": hero_heading,
        "hero_description": hero_description,
        "hero_overlay_title": hero_overlay_title,
        "hero_overlay_description": hero_overlay_description,
        "enrollment_info_badge": enrollment_info_badge,
        "enrollment_info_heading": enrollment_info_heading,
        "enrollment_info_description": enrollment_info_description,
        "enrollment_card_1_label": enrollment_card_1_label,
        "enrollment_card_1_title": enrollment_card_1_title,
        "enrollment_card_1_description": enrollment_card_1_description,
        "enrollment_card_2_label": enrollment_card_2_label,
        "enrollment_card_2_title": enrollment_card_2_title,
        "enrollment_card_2_description": enrollment_card_2_description,
        "enrollment_card_3_label": enrollment_card_3_label,
        "enrollment_card_3_title": enrollment_card_3_title,
        "enrollment_card_3_description": enrollment_card_3_description,
        "message_badge": message_badge,
        "message_heading": message_heading,
        "message_description": message_description,
        "message_author_name": message_author_name,
        "message_author_title": message_author_title,
        "gallery_badge": gallery_badge,
        "gallery_heading": gallery_heading,
        "gallery_description": gallery_description,
        "gallery_item_1_label": gallery_item_1_label,
        "gallery_item_1_title": gallery_item_1_title,
        "gallery_item_1_description": gallery_item_1_description,
        "gallery_item_1_image": gallery_item_1_image or "",
        "gallery_item_2_label": gallery_item_2_label,
        "gallery_item_2_title": gallery_item_2_title,
        "gallery_item_2_description": gallery_item_2_description,
        "gallery_item_2_image": gallery_item_2_image or "",
        "gallery_item_3_label": gallery_item_3_label,
        "gallery_item_3_title": gallery_item_3_title,
        "gallery_item_3_description": gallery_item_3_description,
        "gallery_item_3_image": gallery_item_3_image or "",
        "gallery_item_4_label": gallery_item_4_label,
        "gallery_item_4_title": gallery_item_4_title,
        "gallery_item_4_description": gallery_item_4_description,
        "gallery_item_4_image": gallery_item_4_image or "",
        "faq_badge": faq_badge,
        "faq_heading": faq_heading,
        "faq_description": faq_description,
        "faq_item_1_question": faq_item_1_question,
        "faq_item_1_answer": faq_item_1_answer,
        "faq_item_2_question": faq_item_2_question,
        "faq_item_2_answer": faq_item_2_answer,
        "faq_item_3_question": faq_item_3_question,
        "faq_item_3_answer": faq_item_3_answer,
        "faq_item_4_question": faq_item_4_question,
        "faq_item_4_answer": faq_item_4_answer,
        "motion_enabled": "1" if str(raw_data.get("motion_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0",
        "dark_mode_enabled": "1" if str(raw_data.get("dark_mode_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0",
        "home_stats_enabled": home_stats_enabled,
        "home_announcements_enabled": home_announcements_enabled,
        "home_message_enabled": home_message_enabled,
        "home_gallery_enabled": home_gallery_enabled,
        "home_faq_enabled": home_faq_enabled,
        "whatsapp_enabled": whatsapp_enabled,
        "whatsapp_number": whatsapp_number or "",
        "whatsapp_message": whatsapp_message,
        "map_embed_url": map_embed_url,
        "enrollment_enabled": "1" if str(raw_data.get("enrollment_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0",
        "enrollment_class_allowlist": json.dumps(enrollment_class_allowlist),
        "enrollment_closed_message": enrollment_closed_message,
        "marquee_enabled": "1" if str(raw_data.get("marquee_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0",
        "marquee_text": marquee_text,
        "status_check_enabled": "1" if str(raw_data.get("status_check_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0",
        "status_check_disabled_message": status_check_disabled_message,
        "status_message_pending": status_message_pending,
        "status_message_confirmed": status_message_confirmed,
        "status_message_rejected": status_message_rejected,
        "status_message_not_found": status_message_not_found,
        "admission_form_note": admission_form_note,
        "homepage_popup_enabled": "1" if str(raw_data.get("homepage_popup_enabled", "")).strip() in {"1", "true", "True", "on", "yes"} else "0",
        "homepage_popup_title": homepage_popup_title,
        "homepage_popup_message": homepage_popup_message,
        "homepage_popup_button_label": homepage_popup_button_label,
        "homepage_popup_target_section": str(raw_data.get("homepage_popup_target_section") or "").strip(),
        "homepage_popup_result_id": str(raw_data.get("homepage_popup_result_id") or "").strip(),
    }


def row_to_settings(rows: List[sqlite3.Row]) -> Dict[str, str]:
    values = DEFAULT_SETTINGS.copy()
    for row in rows:
        values[row["key"]] = row["value"]
    return values


def get_settings() -> Dict[str, str]:
    with get_connection() as connection:
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    return row_to_settings(rows)


def public_base_url() -> str:
    configured_base_url = (os.environ.get("PUBLIC_BASE_URL") or "").strip()
    if configured_base_url.startswith(("http://", "https://")):
        return configured_base_url.rstrip("/")
    return (request.url_root or "").rstrip("/")


def public_seo_title() -> str:
    return "The Professors Academy | Admissions, Faculty & Results"


def public_seo_description() -> str:
    return (
        "The Professors Academy offers admissions, faculty information, academic results, "
        "announcements, and MDCAT/ECAT preparation in Mirpur Khas."
    )


def build_public_structured_data(base_url: str, settings: Dict[str, str], description: str) -> str:
    canonical = f"{base_url}/"
    website_id = f"{canonical}#website"
    organization_id = f"{canonical}#organization"
    payload: Dict[str, Any] = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": website_id,
                "url": canonical,
                "name": "The Professors Academy",
                "alternateName": ["theprofessorsacademy", "TPA"],
                "inLanguage": "en-PK",
                "publisher": {"@id": organization_id},
            },
            {
                "@type": "EducationalOrganization",
                "@id": organization_id,
                "name": "The Professors Academy",
                "alternateName": ["theprofessorsacademy", "TPA"],
                "description": description,
                "url": canonical,
                "logo": f"{base_url}/static/icons/icon-512.png",
                "image": f"{base_url}/static/icons/icon-512.png",
                "telephone": str(settings.get("contact_primary") or DEFAULT_SETTINGS["contact_primary"]).strip(),
                "email": str(settings.get("email") or DEFAULT_SETTINGS["email"]).strip(),
                "address": {
                    "@type": "PostalAddress",
                    "streetAddress": str(settings.get("address") or DEFAULT_SETTINGS["address"]).strip(),
                    "addressLocality": "Mirpur Khas",
                    "addressRegion": "Sindh",
                    "addressCountry": "PK",
                },
                "contactPoint": [
                    {
                        "@type": "ContactPoint",
                        "telephone": str(settings.get("contact_primary") or DEFAULT_SETTINGS["contact_primary"]).strip(),
                        "contactType": "admissions support",
                        "areaServed": "PK",
                        "availableLanguage": ["en", "ur"],
                    }
                ],
                "geo": {
                    "@type": "GeoCoordinates",
                    "latitude": 25.5093141,
                    "longitude": 69.0190305,
                },
            },
        ],
    }
    facebook_url = str(settings.get("facebook_url") or "").strip()
    if facebook_url:
        payload["@graph"][1]["sameAs"] = [facebook_url]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def compute_public_last_modified() -> str:
    with get_connection() as connection:
        values = [
            connection.execute("SELECT MAX(date) AS value FROM announcements").fetchone()["value"],
            connection.execute("SELECT MAX(upload_date) AS value FROM results").fetchone()["value"],
        ]
    cleaned_values = [str(value).strip() for value in values if str(value or "").strip()]
    if not cleaned_values:
        return datetime.now().date().isoformat()
    latest = max(cleaned_values)
    date_part = latest[:10]
    try:
        return datetime.strptime(date_part, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return datetime.now().date().isoformat()


def build_sitemap_urlset(entries: List[Tuple[str, str, str, str]]) -> str:
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for location, lastmod, changefreq, priority in entries:
        body.extend(
            [
                "  <url>",
                f"    <loc>{html_escape(location)}</loc>",
                f"    <lastmod>{html_escape(lastmod)}</lastmod>",
                f"    <changefreq>{changefreq}</changefreq>",
                f"    <priority>{priority}</priority>",
                "  </url>",
            ]
        )
    body.append("</urlset>")
    return "\n".join(body)


def xml_response(body: str):
    response = make_response(body)
    response.headers["Content-Type"] = "application/xml; charset=utf-8"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


def render_settings_text_template(template: str, settings: Dict[str, str]) -> str:
    rendered = str(template or "").strip()
    replacements = {
        "{contact_primary}": str(settings.get("contact_primary") or "").strip(),
        "{office_timing}": str(settings.get("office_timing") or "").strip(),
        "{academy_name}": "The Professors Academy",
    }
    for placeholder, replacement in replacements.items():
        rendered = rendered.replace(placeholder, replacement)
    rendered = re.sub(r"[ \t]+\n", "\n", rendered)
    rendered = re.sub(r"\n{3,}", "\n\n", rendered)
    return rendered.strip()


def render_status_message_template(template: str, settings: Dict[str, str]) -> str:
    return render_settings_text_template(template, settings)


def log_admin_activity(
    action_type: str,
    action_summary: str,
    target_type: str = "",
    target_id: Any = "",
    details: Optional[Dict[str, Any]] = None,
    username: Optional[str] = None,
) -> None:
    try:
        actor = str(username or active_admin_username() or "admin").strip() or "admin"
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO activity_log (admin_username, action_type, action_summary, target_type, target_id, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor,
                    str(action_type or "general").strip() or "general",
                    clean_limited_text(action_summary or "Admin activity", "Activity summary", 220),
                    str(target_type or "").strip(),
                    str(target_id or "").strip(),
                    json.dumps(details or {}, ensure_ascii=True, separators=(",", ":")),
                    current_timestamp(),
                ),
            )
            connection.commit()
    except Exception:
        logging.exception("Unable to write admin activity log")


def fetch_activity_log(limit: int = 50) -> List[Dict[str, Any]]:
    safe_limit = min(max(int(limit or 50), 1), 200)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, admin_username, action_type, action_summary, target_type, target_id, created_at
            FROM activity_log
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "admin_username": row["admin_username"],
            "action_type": row["action_type"],
            "action_summary": row["action_summary"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def fetch_visitor_messages(limit: int = 200) -> List[Dict[str, Any]]:
    safe_limit = min(max(int(limit or 200), 1), 500)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, full_name, email, mobile, message, is_read, created_at
            FROM visitor_messages
            ORDER BY is_read ASC, created_at DESC, id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "full_name": row["full_name"],
            "email": row["email"],
            "mobile": row["mobile"],
            "message": row["message"],
            "is_read": bool(row["is_read"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def build_backup_archive_bytes() -> bytes:
    archive_buffer = BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if DATABASE_PATH.exists():
            archive.write(DATABASE_PATH, arcname="database.db")
        for root_path, archive_root in (
            (STUDENT_PHOTOS_DIR, "uploads/student_photos"),
            (FACULTY_PHOTOS_DIR, "uploads/faculty_photos"),
            (RESULTS_DIR, "uploads/results"),
            (GALLERY_IMAGES_DIR, "uploads/gallery_images"),
        ):
            if not root_path.exists():
                continue
            for file_path in root_path.rglob("*"):
                if file_path.is_file():
                    relative_part = file_path.relative_to(root_path).as_posix()
                    archive.write(file_path, arcname=f"{archive_root}/{relative_part}")
    return archive_buffer.getvalue()


def restore_backup_archive(upload_name: str, upload_bytes: bytes) -> None:
    filename = secure_filename(upload_name or "backup.zip").lower()
    if not upload_bytes:
        raise ValueError("Please choose a backup file first.")
    if len(upload_bytes) > MAX_BACKUP_FILE_SIZE:
        raise ValueError("Backup file is too large.")

    ensure_runtime_directories()
    with tempfile.TemporaryDirectory(prefix="tpa_restore_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        backup_db = temp_dir / "database.db"
        if filename.endswith(".db"):
            backup_db.write_bytes(upload_bytes)
        elif filename.endswith(".zip"):
            try:
                with zipfile.ZipFile(BytesIO(upload_bytes)) as archive:
                    for member in archive.infolist():
                        member_name = member.filename.replace("\\", "/").strip("/")
                        if not member_name or member.is_dir():
                            continue
                        if ".." in member_name.split("/"):
                            raise ValueError("Backup archive contains an invalid file path.")
                        target_path = temp_dir / member_name
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source_file, open(target_path, "wb") as output_file:
                            shutil.copyfileobj(source_file, output_file)
            except zipfile.BadZipFile as error:
                raise ValueError("Backup file is not a valid ZIP archive.") from error
        else:
            raise ValueError("Please upload a .zip or .db backup file.")

        if not backup_db.exists():
            nested_backup = next(temp_dir.rglob("database.db"), None)
            if nested_backup and nested_backup.is_file():
                backup_db = nested_backup
        if not backup_db.exists():
            raise ValueError("Backup file does not contain database.db.")

        with sqlite3.connect(backup_db) as test_connection:
            test_connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()

        shutil.copyfile(backup_db, DATABASE_PATH)

        uploaded_paths = {
            "uploads/student_photos": STUDENT_PHOTOS_DIR,
            "uploads/faculty_photos": FACULTY_PHOTOS_DIR,
            "uploads/results": RESULTS_DIR,
            "uploads/gallery_images": GALLERY_IMAGES_DIR,
        }
        for archive_root, destination in uploaded_paths.items():
            extracted_root = temp_dir / archive_root
            if not extracted_root.exists():
                continue
            destination.mkdir(parents=True, exist_ok=True)
            for file_path in extracted_root.rglob("*"):
                if file_path.is_file():
                    relative_part = file_path.relative_to(extracted_root)
                    final_path = destination / relative_part
                    final_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(file_path, final_path)

    initialize_database()
    clear_generated_form_cache()


def serialize_announcement(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "date": row["date"],
        "is_new": bool(row["is_new"]),
        "is_published": bool(row["is_published"]),
    }


def serialize_result(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "class": row["class_name"],
        "year": row["year"],
        "pdf_filename": row["pdf_filename"],
        "upload_date": row["upload_date"],
        "is_new": bool(row["is_new"]),
        "is_published": bool(row["is_published"]),
        "download_url": f"/uploads/results/{row['pdf_filename']}",
    }


def serialize_faculty(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "photo": row["photo"],
        "photo_url": f"/uploads/faculty_photos/{row['photo']}" if row["photo"] else None,
        "class_assigned": row["class_assigned"],
        "subject": row["subject"],
        "qualification": row["qualification"],
        "experience_years": row["experience_years"],
        "order": row["display_order"],
    }


def serialize_student(row: sqlite3.Row) -> Dict[str, Any]:
    status = "pending"
    if row["rejected_at"]:
        status = "rejected"
    elif row["confirmed_at"]:
        status = "confirmed"
    return {
        "id": row["id"],
        "roll_number": row["roll_number"],
        "name": row["name"],
        "father_name": row["father_name"],
        "father_contact": row["father_contact"],
        "gender": row["gender"],
        "email": row["email"],
        "date_of_birth": row["date_of_birth"],
        "mobile": row["mobile"],
        "cnic": row["cnic"],
        "photo": row["photo"],
        "photo_url": f"/uploads/student_photos/{row['photo']}" if row["photo"] else None,
        "class": row["class_name"],
        "group": row["student_group"],
        "subjects": parse_subjects(row["subjects"]),
        "address": row["address"],
        "date": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "rejected_at": row["rejected_at"],
        "status": status,
    }


def fetch_announcements(public_only: bool = False) -> List[Dict[str, Any]]:
    with get_connection() as connection:
        query = "SELECT id, title, description, date, IFNULL(is_new, 0) AS is_new, IFNULL(is_published, 1) AS is_published FROM announcements"
        if public_only:
            query += " WHERE IFNULL(is_published, 1) = 1"
        query += " ORDER BY date DESC, id DESC"
        rows = connection.execute(query).fetchall()
    return [serialize_announcement(row) for row in rows]


def fetch_results(public_only: bool = False) -> List[Dict[str, Any]]:
    with get_connection() as connection:
        query = "SELECT id, title, class_name, year, pdf_filename, upload_date, IFNULL(is_new, 0) AS is_new, IFNULL(is_published, 1) AS is_published FROM results"
        if public_only:
            query += " WHERE IFNULL(is_published, 1) = 1"
        query += " ORDER BY upload_date DESC, id DESC"
        rows = connection.execute(query).fetchall()
    return [serialize_result(row) for row in rows]


def fetch_faculty() -> List[Dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, name, photo, class_assigned, subject, qualification, experience_years, display_order
            FROM faculty
            ORDER BY display_order ASC, id ASC
            """
        ).fetchall()
    return [serialize_faculty(row) for row in rows]


def fetch_enrollments(search_term: str = "", status: Optional[str] = None, class_name: Optional[str] = None) -> List[Dict[str, Any]]:
    query = """
        SELECT id, roll_number, name, father_name, father_contact, gender, email, date_of_birth, mobile, cnic, photo, class_name, student_group, subjects, address, created_at, confirmed_at, rejected_at
        FROM students
    """
    params: List[str] = []
    conditions: List[str] = []

    if status == "confirmed":
        conditions.append("confirmed_at IS NOT NULL AND rejected_at IS NULL")
    elif status == "rejected":
        conditions.append("rejected_at IS NOT NULL")
    elif status == "pending":
        conditions.append("confirmed_at IS NULL AND rejected_at IS NULL")

    if class_name:
        conditions.append("class_name = ?")
        params.append(class_name)

    if search_term:
        pattern = f"%{search_term}%"
        conditions.append(
            "(CAST(IFNULL(roll_number, '') AS TEXT) LIKE ? OR name LIKE ? OR father_name LIKE ? OR IFNULL(father_contact, '') LIKE ? OR IFNULL(gender, '') LIKE ? OR IFNULL(email, '') LIKE ? OR IFNULL(date_of_birth, '') LIKE ? OR mobile LIKE ? OR cnic LIKE ? OR class_name LIKE ? OR IFNULL(student_group, '') LIKE ?)"
        )
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern, pattern])

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    if status == "confirmed":
        query += " ORDER BY confirmed_at DESC, id DESC"
    elif status == "rejected":
        query += " ORDER BY rejected_at DESC, id DESC"
    else:
        query += " ORDER BY created_at DESC, id DESC"
    with get_connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return [serialize_student(row) for row in rows]


def build_enrollments_csv(students: List[Dict[str, Any]], title: str) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([title])
    writer.writerow(["Generated At", current_timestamp()])
    writer.writerow([])
    writer.writerow([
        "ID",
        "Roll Number",
        "Name",
        "Father Name",
        "Father Contact No",
        "Gender",
        "Email",
        "Date of Birth",
        "Mobile",
        "CNIC",
        "Class",
        "Group",
        "Subjects",
        "Address",
        "Application Date",
        "Confirmed At",
        "Rejected At",
        "Status",
    ])
    for item in students:
        writer.writerow([
            item["id"],
            item.get("roll_number") or "",
            item.get("name") or "",
            item.get("father_name") or "",
            item.get("father_contact") or "",
            item.get("gender") or "",
            item.get("email") or "",
            item.get("date_of_birth") or "",
            item.get("mobile") or "",
            item.get("cnic") or "",
            item.get("class") or "",
            item.get("group") or "",
            ", ".join(item.get("subjects") or []),
            item.get("address") or "",
            item.get("date") or "",
            item.get("confirmed_at") or "",
            item.get("rejected_at") or "",
            item.get("status") or "",
        ])
    return output.getvalue()


def fetch_last_7_days_insights() -> Dict[str, Any]:
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = today_start - timedelta(days=6)
    since_timestamp = since.strftime("%Y-%m-%d %H:%M:%S")
    since_date = since.strftime("%Y-%m-%d")
    live_since_timestamp = (datetime.now() - timedelta(minutes=LIVE_VISITOR_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")

    with get_connection() as connection:
        enrollments_last_7_days = connection.execute(
            "SELECT COUNT(*) AS total FROM students WHERE created_at >= ?",
            (since_timestamp,),
        ).fetchone()["total"]
        confirmed_last_7_days = connection.execute(
            "SELECT COUNT(*) AS total FROM students WHERE confirmed_at IS NOT NULL AND confirmed_at >= ?",
            (since_timestamp,),
        ).fetchone()["total"]
        rejected_last_7_days = connection.execute(
            "SELECT COUNT(*) AS total FROM students WHERE rejected_at IS NOT NULL AND rejected_at >= ?",
            (since_timestamp,),
        ).fetchone()["total"]
        announcements_last_7_days = connection.execute(
            "SELECT COUNT(*) AS total FROM announcements WHERE date >= ?",
            (since_date,),
        ).fetchone()["total"]
        results_last_7_days = connection.execute(
            "SELECT COUNT(*) AS total FROM results WHERE upload_date >= ?",
            (since_timestamp,),
        ).fetchone()["total"]
        page_views_last_7_days = connection.execute(
            "SELECT COUNT(*) AS total FROM site_visits WHERE created_at >= ?",
            (since_timestamp,),
        ).fetchone()["total"]
        unique_visitors_last_7_days = connection.execute(
            "SELECT COUNT(DISTINCT visitor_hash) AS total FROM site_visits WHERE created_at >= ?",
            (since_timestamp,),
        ).fetchone()["total"]
        live_visitors_now = connection.execute(
            "SELECT COUNT(*) AS total FROM site_presence WHERE last_seen >= ?",
            (live_since_timestamp,),
        ).fetchone()["total"]

        enrollment_rows = connection.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS total
            FROM students
            WHERE created_at >= ?
            GROUP BY substr(created_at, 1, 10)
            """,
            (since_timestamp,),
        ).fetchall()
        confirmation_rows = connection.execute(
            """
            SELECT substr(confirmed_at, 1, 10) AS day, COUNT(*) AS total
            FROM students
            WHERE confirmed_at IS NOT NULL AND confirmed_at >= ?
            GROUP BY substr(confirmed_at, 1, 10)
            """,
            (since_timestamp,),
        ).fetchall()
        rejection_rows = connection.execute(
            """
            SELECT substr(rejected_at, 1, 10) AS day, COUNT(*) AS total
            FROM students
            WHERE rejected_at IS NOT NULL AND rejected_at >= ?
            GROUP BY substr(rejected_at, 1, 10)
            """,
            (since_timestamp,),
        ).fetchall()
        visit_rows = connection.execute(
            """
            SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS total
            FROM site_visits
            WHERE created_at >= ?
            GROUP BY substr(created_at, 1, 10)
            """,
            (since_timestamp,),
        ).fetchall()
        section_rows = connection.execute(
            """
            SELECT section_name, COUNT(*) AS total
            FROM site_visits
            WHERE created_at >= ?
            GROUP BY section_name
            ORDER BY total DESC, section_name ASC
            LIMIT 6
            """,
            (since_timestamp,),
        ).fetchall()
        live_section_rows = connection.execute(
            """
            SELECT section_name, COUNT(*) AS total
            FROM site_presence
            WHERE last_seen >= ?
            GROUP BY section_name
            ORDER BY total DESC, section_name ASC
            LIMIT 4
            """,
            (live_since_timestamp,),
        ).fetchall()

    enrollment_map = {row["day"]: row["total"] for row in enrollment_rows}
    confirmation_map = {row["day"]: row["total"] for row in confirmation_rows}
    rejection_map = {row["day"]: row["total"] for row in rejection_rows}
    visit_map = {row["day"]: row["total"] for row in visit_rows}
    daily_breakdown = []
    for offset in range(7):
        day = since + timedelta(days=offset)
        day_key = day.strftime("%Y-%m-%d")
        daily_breakdown.append(
            {
                "day": day_key,
                "enrollments": int(enrollment_map.get(day_key, 0)),
                "confirmed": int(confirmation_map.get(day_key, 0)),
                "rejected": int(rejection_map.get(day_key, 0)),
                "visits": int(visit_map.get(day_key, 0)),
            }
        )

    return {
        "range_label": f"{since.strftime('%b %d')} - {today_start.strftime('%b %d')}",
        "enrollments_last_7_days": int(enrollments_last_7_days),
        "confirmed_last_7_days": int(confirmed_last_7_days),
        "rejected_last_7_days": int(rejected_last_7_days),
        "announcements_last_7_days": int(announcements_last_7_days),
        "results_last_7_days": int(results_last_7_days),
        "live_visitors_now": int(live_visitors_now),
        "live_window_minutes": LIVE_VISITOR_WINDOW_MINUTES,
        "page_views_last_7_days": int(page_views_last_7_days),
        "unique_visitors_last_7_days": int(unique_visitors_last_7_days),
        "live_sections": [
            {"section": str(row["section_name"] or "home"), "visitors": int(row["total"])}
            for row in live_section_rows
        ],
        "top_sections": [
            {"section": str(row["section_name"] or "home"), "views": int(row["total"])}
            for row in section_rows
        ],
        "daily_breakdown": daily_breakdown,
    }


def report_since(days: Optional[int]) -> Tuple[Optional[str], str]:
    if days is None:
        return None, "All Time"
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    since = today_start - timedelta(days=days - 1)
    return since.strftime("%Y-%m-%d %H:%M:%S"), f"Last {days} Days ({since.strftime('%b %d')} - {today_start.strftime('%b %d')})"


def fetch_admission_report(days: Optional[int]) -> Dict[str, Any]:
    since_timestamp, range_label = report_since(days)

    def count_query(column_name: str) -> Tuple[str, Tuple[str, ...]]:
        if since_timestamp is None:
            return f"SELECT COUNT(*) AS total FROM students WHERE {column_name} IS NOT NULL", ()
        return (
            f"SELECT COUNT(*) AS total FROM students WHERE {column_name} IS NOT NULL AND {column_name} >= ?",
            (since_timestamp,),
        )

    with get_connection() as connection:
        if since_timestamp is None:
            total_admissions = connection.execute("SELECT COUNT(*) AS total FROM students").fetchone()["total"]
        else:
            total_admissions = connection.execute(
                "SELECT COUNT(*) AS total FROM students WHERE created_at >= ?",
                (since_timestamp,),
            ).fetchone()["total"]

        confirmed_query, confirmed_params = count_query("confirmed_at")
        confirmed_admissions = connection.execute(confirmed_query, confirmed_params).fetchone()["total"]

        rejected_query, rejected_params = count_query("rejected_at")
        rejected_admissions = connection.execute(rejected_query, rejected_params).fetchone()["total"]

        if since_timestamp is None:
            pending_admissions = connection.execute(
                "SELECT COUNT(*) AS total FROM students WHERE confirmed_at IS NULL AND rejected_at IS NULL"
            ).fetchone()["total"]
        else:
            pending_admissions = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM students
                WHERE created_at >= ? AND confirmed_at IS NULL AND rejected_at IS NULL
                """,
                (since_timestamp,),
            ).fetchone()["total"]

    return {
        "range_label": range_label,
        "days": days,
        "generated_at": current_timestamp(),
        "total_admissions": int(total_admissions),
        "confirmed_admissions": int(confirmed_admissions),
        "rejected_admissions": int(rejected_admissions),
        "pending_admissions": int(pending_admissions),
    }


def build_report_csv(report_data: Dict[str, Any]) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["The Professors Academy Admission Report"])
    writer.writerow(["Generated At", report_data["generated_at"]])
    writer.writerow(["Range", report_data["range_label"]])
    writer.writerow([])
    writer.writerow(["Metric", "Count"])
    writer.writerow(["Total Admissions", report_data["total_admissions"]])
    writer.writerow(["Confirmed Admissions", report_data["confirmed_admissions"]])
    writer.writerow(["Rejected Admissions", report_data["rejected_admissions"]])
    writer.writerow(["Pending Admissions", report_data["pending_admissions"]])
    return output.getvalue()


def fetch_student(student_id: int) -> Optional[Dict[str, Any]]:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT id, roll_number, name, father_name, father_contact, gender, email, date_of_birth, mobile, cnic, photo, class_name, student_group, subjects, address, created_at, confirmed_at, rejected_at
            FROM students
            WHERE id = ?
            """,
            (student_id,),
        ).fetchone()
    if not row:
        return None
    return serialize_student(row)


def normalize_lookup_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def normalize_status_lookup_date_of_birth(value: str) -> Optional[str]:
    return normalize_date_of_birth(value or "")


def find_student_for_status_lookup(cnic: str, date_of_birth: str) -> Optional[Dict[str, Any]]:
    normalized_cnic = normalize_cnic(cnic or "")
    raw_date_of_birth = str(date_of_birth or "").strip()
    normalized_date_of_birth = normalize_status_lookup_date_of_birth(raw_date_of_birth) if raw_date_of_birth else None
    if not normalized_cnic or not normalized_date_of_birth:
        return None

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT id, roll_number, name, father_name, father_contact, gender, email, date_of_birth, mobile, cnic, photo, class_name, student_group, subjects, address, created_at, confirmed_at, rejected_at
            FROM students
            WHERE cnic = ?
            ORDER BY created_at DESC, id DESC
            """,
            (normalized_cnic,),
        ).fetchall()
    if not rows:
        return None
    for row in rows:
        if normalize_date_of_birth(row["date_of_birth"] or "") == normalized_date_of_birth:
            return serialize_student(row)
    return None


def filename_date_fragment(value: Any) -> str:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return datetime.now().strftime("%Y-%m-%d")


def admission_form_filename(student: Dict[str, Any], extension: str) -> str:
    safe_name = safe_filename_fragment(student.get("name") or "student", "student")
    safe_roll = safe_filename_fragment(student.get("roll_number") or "roll", "roll")
    safe_date = safe_filename_fragment(filename_date_fragment(student.get("date") or student.get("confirmed_at")), "date")
    version_tag = safe_filename_fragment(ADMISSION_FORM_CACHE_VERSION, "v1")
    return f"{safe_name}_{safe_roll}_{safe_date}_{version_tag}.{extension}"


def safe_filename_fragment(value: Optional[str], fallback: str = "records") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip()).strip("_")
    return cleaned or fallback


def embedded_student_photo_src(student: Dict[str, Any]) -> Optional[str]:
    photo_name = str(student.get("photo") or "").strip()
    if not photo_name:
        return None
    file_path = STUDENT_PHOTOS_DIR / photo_name
    if not file_path.exists() or not file_path.is_file():
        return student.get("photo_url")
    extension = file_path.suffix.lower()
    mime_type = "image/png" if extension == ".png" else "image/jpeg"
    try:
        encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    except OSError:
        return student.get("photo_url")
    return f"data:{mime_type};base64,{encoded}"


def make_admission_pdf_response(student: Dict[str, Any]):
    pdf_bytes = build_admission_form_pdf([student], f"{student['name']} Admission Form")
    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{admission_form_filename(student, "pdf")}"'
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def make_admission_document_response(student: Dict[str, Any], *, download: bool):
    document = build_admission_form_document([student], f"{student['name']} Admission Form")
    response = make_response(document)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    disposition = "attachment" if download else "inline"
    response.headers["Content-Disposition"] = (
        f'{disposition}; filename="{admission_form_filename(student, "html")}"'
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def build_admission_form_document(students: List[Dict[str, Any]], title: str, render_mode: str = "screen") -> str:
    settings = get_settings()
    payment_note = render_settings_text_template(
        settings.get("admission_form_note") or DEFAULT_SETTINGS["admission_form_note"],
        settings,
    ) or DEFAULT_SETTINGS["admission_form_note"]
    sheets = []
    is_capture_mode = render_mode == "capture"
    is_pdf_mode = render_mode == "pdf"

    def build_data_row(label: str, value: Any, extra_class: str = "") -> str:
        normalized = str(value or "").strip()
        if not normalized or normalized == "N/A":
            return ""
        class_name = "data-row"
        if extra_class:
            class_name = f"{class_name} {extra_class}"
        return (
            f'<div class="{class_name}">'
            f"<span>{html_escape(label)}</span>"
            f"<strong>{html_escape(normalized)}</strong>"
            f"</div>"
        )

    for student in students:
        subjects = ", ".join(student.get("subjects") or []) or "N/A"
        student_group = str(student.get("group") or "").strip() or "N/A"
        gender = str(student.get("gender") or "").strip() or "N/A"
        email = str(student.get("email") or "").strip() or "N/A"
        date_of_birth = format_display_datetime(student.get("date_of_birth"))
        father_contact = str(student.get("father_contact") or "").strip() or "N/A"
        class_name = str(student.get("class") or "").strip() or "N/A"
        class_display = class_name if student_group == "N/A" else f"{class_name} | {student_group}"
        cnic_value = str(student.get("cnic") or "").strip() or "N/A"
        mobile_value = str(student.get("mobile") or "").strip() or "N/A"
        address_value = str(student.get("address") or "").strip() or "N/A"
        roll_number = str(student.get("roll_number") or "").strip() or "N/A"
        submitted_on = format_display_datetime(student.get("date"))
        document_reference = f"TPA/{roll_number.replace('/', '-')}" if roll_number != "N/A" else "TPA/ACADEMY-RECORD"
        support_contact = f"{settings.get('contact_primary') or 'N/A'} | {settings.get('email') or 'N/A'}"
        submission_checklist = "Bring this printed form, the admission fee, and the required documents to the admin office."
        identity_rows = "".join(
            [
                build_data_row("Full Name", student.get("name")),
                build_data_row("Father Name", student.get("father_name")),
                build_data_row("Gender", gender),
                build_data_row("Date of Birth", date_of_birth),
                build_data_row("CNIC Number", cnic_value),
            ]
        )
        contact_rows = "".join(
            [
                build_data_row("Student Contact", mobile_value),
                build_data_row("Father Contact", father_contact),
                build_data_row("Email Address", email),
                build_data_row("Address", address_value, "data-row-wide"),
            ]
        )
        academic_rows = "".join(
            [
                build_data_row("Class / Program", class_name),
                build_data_row("Academic Group", student_group),
                build_data_row("Application Date", submitted_on),
                build_data_row("Selected Subjects", subjects, "data-row-wide"),
            ]
        )
        embedded_photo_src = embedded_student_photo_src(student)
        photo_html = (
            f'<img src="{html_escape(embedded_photo_src)}" alt="{html_escape(student["name"])} photo">'
            if embedded_photo_src
            else '<div class="photo-fallback">No Photo</div>'
        )
        sheets.append(
            f"""
            <section class="sheet">
                <div class="sheet-topline"></div>
                <div class="watermark">TPA</div>
                <header class="sheet-header">
                    <div class="academy-copy">
                        <div class="header-flags">
                            <span class="badge">Official Admission Record</span>
                            <span class="document-chip">Student Copy</span>
                        </div>
                        <h1>The Professors Academy</h1>
                        <h2>Professional Admission Summary</h2>
                        <p class="contact-line">{html_escape(settings.get("address") or "The Professors Academy")}</p>
                        <p class="contact-line">Phone: {html_escape(settings.get("contact_primary") or "N/A")} | Email: {html_escape(settings.get("email") or "N/A")}</p>
                        <div class="intro-note">
                            <span>Issued For Printed Submission</span>
                            <strong>This formal record contains the student's submitted details for academy verification and office submission.</strong>
                        </div>
                    </div>
                    <div class="photo-panel">
                        <div class="photo-frame">{photo_html}</div>
                        <div class="photo-caption">Student Photograph</div>
                    </div>
                </header>
                <div class="formal-strip">
                    <div class="formal-item">
                        <span>Document Reference</span>
                        <strong>{html_escape(document_reference)}</strong>
                    </div>
                    <div class="formal-item">
                        <span>Prepared For</span>
                        <strong>Student Submission File</strong>
                    </div>
                    <div class="formal-item">
                        <span>Academy Status</span>
                        <strong>Official Summary Record</strong>
                    </div>
                </div>
                <div class="meta-strip">
                    <div class="meta-card"><span>Student</span><strong>{html_escape(student["name"])}</strong></div>
                    <div class="meta-card"><span>Roll No</span><strong>{html_escape(roll_number)}</strong></div>
                    <div class="meta-card"><span>Class / Track</span><strong>{html_escape(class_display)}</strong></div>
                    <div class="meta-card"><span>Application Date</span><strong>{html_escape(submitted_on)}</strong></div>
                </div>
                <section class="summary-layout">
                    <article class="summary-panel">
                        <div class="panel-title">Student Information</div>
                        <div class="data-list">{identity_rows}</div>
                    </article>
                    <article class="summary-panel">
                        <div class="panel-title">Contact & Address</div>
                        <div class="data-list">{contact_rows}</div>
                    </article>
                    <article class="summary-panel summary-panel-wide">
                        <div class="panel-title">Academic Selection</div>
                        <div class="data-list data-list-compact">{academic_rows}</div>
                    </article>
                </section>
                <div class="sheet-closing">
                    <div class="important-note">
                        <div class="note-copy">
                            <span>Important Note</span>
                            <strong>{html_escape(payment_note)}</strong>
                        </div>
                        <div class="note-seal">Office Submission</div>
                    </div>
                    <div class="completion-strip">
                        <div class="completion-card">
                            <span>Office Timing</span>
                            <strong>{html_escape(settings.get("office_timing") or "N/A")}</strong>
                        </div>
                        <div class="completion-card">
                            <span>Contact & Support</span>
                            <strong>{html_escape(support_contact)}</strong>
                        </div>
                        <div class="completion-card">
                            <span>Submission Checklist</span>
                            <strong>{html_escape(submission_checklist)}</strong>
                        </div>
                    </div>
                    <div class="signature-grid">
                        <div class="signature-box"><span>Student Signature</span></div>
                        <div class="signature-box"><span>Parent / Guardian</span></div>
                        <div class="signature-box"><span>Admin Office</span></div>
                    </div>
                    <div class="sheet-footer">
                        <span>Official Student Copy</span>
                        <span>The Professors Academy | Printed Submission Record</span>
                    </div>
                </div>
            </section>
            """
        )

    if not sheets:
        sheets.append(
            """
            <section class="sheet empty-sheet">
                <header class="sheet-header">
                    <div>
                        <span class="badge">Official Admission Form</span>
                        <h1>The Professors Academy</h1>
                        <p>No confirmed admissions are available right now.</p>
                    </div>
                </header>
            </section>
            """
        )

    body_class = "capture-mode" if is_capture_mode else ("pdf-mode" if is_pdf_mode else "")
    return f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html_escape(title)}</title>
    <style>
        :root{{--navy:{PRIMARY_COLOR};--gold:{ACCENT_COLOR};--line:rgba(10,25,41,.12);--ink:#12243a;--muted:#5d6d82;--page-width:198mm;--page-height:287mm;--page-padding:5.2mm}}
        @page{{size:A4;margin:0}}
        *{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
        body{{margin:0;padding:4.5mm;font-family:"Segoe UI",Arial,sans-serif;color:var(--ink);background:radial-gradient(circle at top left,rgba(240,185,11,.12),transparent 30%),linear-gradient(180deg,#edf3fb 0,#f8fafc 100%)}}
        .sheet{{position:relative;display:flex;flex-direction:column;width:min(100%,var(--page-width));min-height:var(--page-height);margin:0 auto 4.5mm;background:linear-gradient(180deg,#ffffff 0,#fcfdff 100%);border:1px solid rgba(10,25,41,.11);border-radius:18px;padding:4.8mm;box-shadow:0 24px 48px rgba(10,25,41,.08);page-break-after:always;page-break-inside:avoid;overflow:hidden}}
        .sheet::before{{content:"";position:absolute;inset:3mm;border:1px solid rgba(240,185,11,.26);border-radius:12px;pointer-events:none}}
        .sheet:last-child{{page-break-after:auto}}
        body.capture-mode{{padding:0;background:#fff;width:980px;min-height:1380px;overflow:hidden}}
        body.capture-mode .sheet{{width:980px;min-height:1380px;height:1380px;margin:0;background:#fff;border-radius:0;box-shadow:none;padding-bottom:12px}}
        body.capture-mode .sheet::before{{inset:3.2mm;border-radius:0}}
        body.pdf-mode{{padding:0;background:#fff}}
        body.pdf-mode .sheet{{width:198mm;min-height:287mm;height:287mm;margin:0 auto;background:#fff;border-radius:0;box-shadow:none;padding:4.2mm 4.4mm;overflow:hidden}}
        body.pdf-mode .sheet::before{{inset:2.6mm;border-radius:0}}
        body.pdf-mode .sheet-topline{{margin-bottom:5px}}
        body.pdf-mode .watermark{{font-size:54px;right:10mm;top:22mm}}
        body.pdf-mode .sheet-header{{grid-template-columns:minmax(0,1fr) 34mm;gap:8px;padding-bottom:6px}}
        body.pdf-mode .sheet-header h1{{font-size:20px}}
        body.pdf-mode .sheet-header h2{{font-size:8.2px}}
        body.pdf-mode .sheet-header p{{font-size:8.5px;line-height:1.18}}
        body.pdf-mode .intro-note{{margin-top:6px;padding:7px 9px}}
        body.pdf-mode .intro-note span{{font-size:6.8px}}
        body.pdf-mode .intro-note strong{{font-size:8.6px;line-height:1.24}}
        body.pdf-mode .photo-frame{{width:30mm;height:34mm}}
        body.pdf-mode .photo-caption{{width:29mm;padding:3px 5px;font-size:6.8px}}
        body.pdf-mode .formal-strip{{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin-top:6px}}
        body.pdf-mode .formal-item{{padding:7px 9px}}
        body.pdf-mode .formal-item span{{font-size:6.4px}}
        body.pdf-mode .formal-item strong{{font-size:8.4px;line-height:1.16}}
        body.pdf-mode .meta-strip{{grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-top:6px}}
        body.pdf-mode .meta-card{{padding:7px 9px}}
        body.pdf-mode .meta-card span{{font-size:6.8px}}
        body.pdf-mode .meta-card strong{{font-size:8.9px;line-height:1.18}}
        body.pdf-mode .summary-layout{{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;margin-top:6px}}
        body.pdf-mode .summary-panel{{padding:8px 9px;border-radius:12px}}
        body.pdf-mode .panel-title{{margin:0 0 6px;padding:3px 10px;font-size:9.2px}}
        body.pdf-mode .data-list{{gap:4px}}
        body.pdf-mode .data-list-compact{{grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 6px}}
        body.pdf-mode .data-row{{padding:6px 8px;border-radius:10px}}
        body.pdf-mode .data-row span{{font-size:6.5px}}
        body.pdf-mode .data-row strong{{font-size:8.8px;line-height:1.2;margin-top:2px}}
        body.pdf-mode .sheet-closing{{gap:6px;margin-top:6px}}
        body.pdf-mode .important-note{{margin-top:0;padding:8px 10px;border-radius:12px;gap:8px}}
        body.pdf-mode .important-note span{{font-size:6.6px}}
        body.pdf-mode .important-note strong{{font-size:8.6px;line-height:1.2;margin-top:3px}}
        body.pdf-mode .note-seal{{min-width:100px;padding:7px 10px;font-size:6.8px}}
        body.pdf-mode .completion-strip{{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}}
        body.pdf-mode .completion-card{{padding:8px 9px;border-radius:11px}}
        body.pdf-mode .completion-card span{{font-size:6.4px}}
        body.pdf-mode .completion-card strong{{font-size:8.2px;line-height:1.2;margin-top:3px}}
        body.pdf-mode .signature-grid{{grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin-top:0}}
        body.pdf-mode .signature-box{{min-height:41px;padding:6px}}
        body.pdf-mode .signature-box::before{{top:12px}}
        body.pdf-mode .signature-box::after{{top:16px;font-size:5.8px}}
        body.pdf-mode .signature-box span{{font-size:6.3px}}
        body.pdf-mode .sheet-footer{{margin-top:3px;padding-top:4px;font-size:6.6px}}
        @media print{{body{{padding:0!important;background:#fff!important}}.sheet{{width:198mm!important;min-height:287mm!important;height:287mm!important;margin:0 auto!important;background:#fff!important;border-radius:0!important;box-shadow:none!important;padding:4.2mm 4.4mm!important;overflow:hidden!important}}.sheet::before{{inset:2.6mm!important;border-radius:0!important}}.sheet-topline{{margin-bottom:5px!important}}.watermark{{font-size:54px!important;right:10mm!important;top:22mm!important}}.sheet-header{{grid-template-columns:minmax(0,1fr) 34mm!important;gap:8px!important;padding-bottom:6px!important}}.sheet-header h1{{font-size:20px!important}}.sheet-header h2{{font-size:8.2px!important}}.sheet-header p{{font-size:8.5px!important;line-height:1.18!important}}.intro-note{{margin-top:6px!important;padding:7px 9px!important}}.intro-note span{{font-size:6.8px!important}}.intro-note strong{{font-size:8.6px!important;line-height:1.24!important}}.photo-frame{{width:30mm!important;height:34mm!important}}.photo-caption{{width:29mm!important;padding:3px 5px!important;font-size:6.8px!important}}.formal-strip{{grid-template-columns:repeat(3,minmax(0,1fr))!important;gap:6px!important;margin-top:6px!important}}.formal-item{{padding:7px 9px!important}}.formal-item span{{font-size:6.4px!important}}.formal-item strong{{font-size:8.4px!important;line-height:1.16!important}}.meta-strip{{grid-template-columns:repeat(4,minmax(0,1fr))!important;gap:6px!important;margin-top:6px!important}}.meta-card{{padding:7px 9px!important}}.meta-card span{{font-size:6.8px!important}}.meta-card strong{{font-size:8.9px!important;line-height:1.18!important}}.summary-layout{{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:6px!important;margin-top:6px!important}}.summary-panel{{padding:8px 9px!important;border-radius:12px!important}}.panel-title{{margin:0 0 6px!important;padding:3px 10px!important;font-size:9.2px!important}}.data-list{{gap:4px!important}}.data-list-compact{{grid-template-columns:repeat(2,minmax(0,1fr))!important;gap:4px 6px!important}}.data-row{{padding:6px 8px!important;border-radius:10px!important}}.data-row span{{font-size:6.5px!important}}.data-row strong{{font-size:8.8px!important;line-height:1.2!important;margin-top:2px!important}}.sheet-closing{{gap:6px!important;margin-top:6px!important}}.important-note{{margin-top:0!important;padding:8px 10px!important;border-radius:12px!important;gap:8px!important}}.important-note span{{font-size:6.6px!important}}.important-note strong{{font-size:8.6px!important;line-height:1.2!important;margin-top:3px!important}}.note-seal{{min-width:100px!important;padding:7px 10px!important;font-size:6.8px!important}}.completion-strip{{grid-template-columns:repeat(3,minmax(0,1fr))!important;gap:6px!important}}.completion-card{{padding:8px 9px!important;border-radius:11px!important}}.completion-card span{{font-size:6.4px!important}}.completion-card strong{{font-size:8.2px!important;line-height:1.2!important;margin-top:3px!important}}.signature-grid{{grid-template-columns:repeat(3,minmax(0,1fr))!important;gap:6px!important;margin-top:0!important}}.signature-box{{min-height:41px!important;padding:6px!important}}.signature-box::before{{top:12px!important}}.signature-box::after{{top:16px!important;font-size:5.8px!important}}.signature-box span{{font-size:6.3px!important}}.sheet-footer{{margin-top:3px!important;padding-top:4px!important;font-size:6.6px!important}}}}
        .sheet-topline{{height:4px;border-radius:999px;background:linear-gradient(90deg,rgba(240,185,11,.95),rgba(10,25,41,.22));margin-bottom:7px}}
        .watermark{{position:absolute;right:11mm;top:23mm;font-size:56px;font-weight:800;letter-spacing:.08em;color:rgba(10,25,41,.03);pointer-events:none}}
        .sheet-header{{display:grid;grid-template-columns:minmax(0,1fr) 35mm;gap:10px;align-items:start;border-bottom:1.6px solid rgba(240,185,11,.26);padding-bottom:7px}}
        .academy-copy{{min-width:0;overflow-wrap:anywhere}}
        .header-flags{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
        .badge{{display:inline-block;background:linear-gradient(135deg,rgba(240,185,11,.22),rgba(240,185,11,.08));color:var(--navy);font-weight:800;border-radius:999px;padding:4px 10px;font-size:7.8px;text-transform:uppercase;letter-spacing:.1em;border:1px solid rgba(240,185,11,.26)}}
        .document-chip{{display:inline-flex;align-items:center;padding:4px 10px;border-radius:999px;background:rgba(10,25,41,.05);border:1px solid rgba(10,25,41,.08);font-size:7.6px;font-weight:800;letter-spacing:.1em;text-transform:uppercase;color:var(--navy)}}
        .sheet-header h1,.sheet-header h2,.panel-title,.important-note strong{{font-family:Georgia,"Times New Roman",serif}}
        .sheet-header h1{{margin:5px 0 2px;font-size:23px;color:var(--navy);line-height:1.04}}
        .sheet-header h2{{margin:0;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:#7b8798}}
        .sheet-header p{{margin:2px 0 0;color:var(--muted);line-height:1.26;font-size:9.5px;overflow-wrap:anywhere}}
        .contact-line{{max-width:92%}}
        .intro-note{{margin-top:7px;padding:8px 10px;border-radius:12px;background:linear-gradient(135deg,rgba(10,25,41,.045),rgba(255,255,255,.94));border:1px solid rgba(10,25,41,.08);border-left:4px solid rgba(240,185,11,.82);box-shadow:inset 0 1px 0 rgba(255,255,255,.65)}}
        .intro-note span{{display:block;font-size:7.2px;font-weight:800;letter-spacing:.09em;text-transform:uppercase;color:var(--navy)}}
        .intro-note strong{{display:block;margin-top:3px;font-size:9.4px;line-height:1.36;color:var(--ink);font-family:"Segoe UI",Arial,sans-serif}}
        .photo-panel{{display:grid;gap:6px;justify-items:end}}
        .photo-frame{{width:31mm;height:36mm;border-radius:13px;overflow:hidden;border:2px solid rgba(240,185,11,.26);background:linear-gradient(180deg,rgba(10,25,41,.045),rgba(255,255,255,.9));display:grid;place-items:center;box-shadow:0 12px 24px rgba(10,25,41,.08)}}
        .photo-frame img{{width:100%;height:100%;object-fit:cover}}
        .photo-caption{{width:30mm;padding:4px 6px;border-radius:10px;background:rgba(10,25,41,.05);font-size:7.2px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:var(--navy);text-align:center}}
        .photo-fallback{{font-weight:700;color:var(--muted)}}
        .formal-strip{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin-top:8px}}
        .formal-item{{padding:8px 10px;border-radius:12px;background:linear-gradient(180deg,rgba(10,25,41,.96),rgba(16,38,64,.94));border:1px solid rgba(240,185,11,.18);box-shadow:0 10px 18px rgba(10,25,41,.08);min-width:0}}
        .formal-item span{{display:block;font-size:6.8px;font-weight:800;text-transform:uppercase;letter-spacing:.1em;color:rgba(255,255,255,.68)}}
        .formal-item strong{{display:block;margin-top:3px;font-size:9.1px;line-height:1.28;color:#fff;overflow-wrap:anywhere}}
        .meta-strip{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:8px}}
        .meta-card{{padding:8px 10px;border-radius:13px;background:linear-gradient(180deg,#fff,rgba(10,25,41,.03));border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.85);position:relative;overflow:hidden}}
        .meta-card::before{{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,rgba(240,185,11,.92),rgba(10,25,41,.18))}}
        .meta-card span{{display:block;font-size:7.2px;font-weight:800;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
        .meta-card strong{{display:block;margin-top:3px;font-size:10.4px;line-height:1.3;color:var(--navy);overflow-wrap:anywhere}}
        .summary-layout{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;margin-top:8px;flex:1 1 auto;align-content:stretch}}
        .summary-panel{{display:flex;flex-direction:column;padding:10px 11px;border-radius:15px;background:linear-gradient(180deg,#fff,rgba(10,25,41,.02));border:1px solid var(--line);min-width:0;box-shadow:0 8px 18px rgba(10,25,41,.035);position:relative;overflow:hidden}}
        .summary-panel::before{{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,rgba(240,185,11,.55),rgba(10,25,41,.08))}}
        .summary-panel-wide{{grid-column:1 / -1}}
        .panel-title{{display:inline-flex;align-items:center;margin:0 0 7px;padding:4px 11px;border-radius:999px;background:linear-gradient(135deg,rgba(240,185,11,.16),rgba(240,185,11,.06));border:1px solid rgba(240,185,11,.2);font-size:10.8px;letter-spacing:.04em;color:var(--navy)}}
        .data-list{{display:grid;gap:5px;align-content:start}}
        .data-list-compact{{grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 7px}}
        .data-row{{padding:8px 9px;border:1px solid rgba(10,25,41,.08);border-radius:11px;background:linear-gradient(180deg,rgba(255,255,255,.96),rgba(249,251,254,.96));min-width:0;box-shadow:inset 0 1px 0 rgba(255,255,255,.82)}}
        .data-row-wide{{grid-column:1 / -1}}
        .data-row span{{display:block;font-size:7.2px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:800}}
        .data-row strong{{display:block;margin-top:3px;font-size:10.5px;line-height:1.34;overflow-wrap:anywhere}}
        .sheet-closing{{display:grid;gap:8px;margin-top:10px}}
        .important-note{{margin-top:9px;padding:10px 12px;border-radius:15px;background:linear-gradient(135deg,rgba(240,185,11,.16),rgba(10,25,41,.05));border:1px solid rgba(240,185,11,.26);box-shadow:0 8px 18px rgba(240,185,11,.08);display:flex;align-items:flex-start;justify-content:space-between;gap:10px}}
        .note-copy{{min-width:0;flex:1 1 auto}}
        .important-note span{{display:block;font-size:7.2px;text-transform:uppercase;letter-spacing:.08em;color:var(--navy);font-weight:800}}
        .important-note strong{{display:block;margin-top:4px;font-size:10px;line-height:1.34;color:var(--ink);white-space:pre-line}}
        .note-seal{{display:inline-flex;align-items:center;justify-content:center;align-self:center;min-width:118px;padding:8px 12px;border-radius:999px;background:rgba(255,255,255,.78);border:1px solid rgba(10,25,41,.1);font-size:7.4px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--navy);box-shadow:inset 0 1px 0 rgba(255,255,255,.72)}}
        .completion-strip{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}}
        .completion-card{{padding:10px 11px;border-radius:13px;background:linear-gradient(180deg,#fff,rgba(10,25,41,.03));border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.82)}}
        .completion-card span{{display:block;font-size:7px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);font-weight:800}}
        .completion-card strong{{display:block;margin-top:4px;font-size:9.4px;line-height:1.36;color:var(--navy);font-weight:700;overflow-wrap:anywhere}}
        .signature-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:2px}}
        body.capture-mode .signature-grid{{position:static;left:auto;right:auto;bottom:auto;margin-top:2px;padding-top:0}}
        .signature-box{{min-height:48px;border:1px dashed rgba(10,25,41,.24);border-radius:13px;background:linear-gradient(180deg,#fff,rgba(249,251,254,.96));display:flex;align-items:flex-end;justify-content:center;padding:7px;position:relative;box-shadow:inset 0 1px 0 rgba(255,255,255,.82)}}
        .signature-box::before{{content:"";position:absolute;left:14px;right:14px;top:15px;height:1px;background:rgba(10,25,41,.15)}}
        .signature-box::after{{content:"Authorized Signature";position:absolute;top:19px;left:0;right:0;text-align:center;font-size:6.2px;letter-spacing:.08em;text-transform:uppercase;color:rgba(10,25,41,.34);font-weight:700}}
        .signature-box span{{font-size:6.9px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:700}}
        .sheet-footer{{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-top:4px;padding-top:6px;border-top:1px solid rgba(10,25,41,.08);font-size:7.2px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}}
        .empty-sheet{{text-align:center}}
        @media (max-width:900px){{body{{padding:16px}}.sheet{{padding:18px}}.sheet-header,.formal-strip,.meta-strip,.summary-layout,.data-list-compact,.completion-strip,.signature-grid{{grid-template-columns:1fr}}.photo-panel{{justify-items:start}}.photo-caption,.photo-frame{{width:100%;max-width:220px}}.watermark{{font-size:50px;top:110px}}.important-note{{flex-direction:column;align-items:flex-start}}.sheet-footer{{flex-direction:column;align-items:flex-start}}}}
    </style>
</head>
<body class="{body_class}">
    {"".join(sheets)}
</body>
</html>
"""


def active_admin_username() -> Optional[str]:
    username = session.get("admin_username")
    if not username:
        return restore_admin_session_from_cookie()

    expected_user_agent = str(session.get("admin_ua_hash") or "")
    current_user_agent = request_user_agent_hash()
    if expected_user_agent and current_user_agent and not secrets.compare_digest(expected_user_agent, current_user_agent):
        session["admin_ua_hash"] = current_user_agent

    session["last_activity"] = datetime.utcnow().isoformat()
    session.permanent = True
    return username


def login_required(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        if not active_admin_username():
            return jsonify({"success": False, "message": "Please log in again."}), 401
        return view_function(*args, **kwargs)

    return wrapped


def json_error(message: str, status: int = 400):
    return jsonify({"success": False, "message": message}), status


def parse_request_payload() -> dict:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def require_admin_write_security() -> Optional[Any]:
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        same_origin_error = validate_same_origin_request()
        if same_origin_error:
            return same_origin_error
        return validate_admin_csrf()
    return None


@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'self'; "
        "object-src 'none'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' blob: data: https://images.unsplash.com https://www.google.com https://*.google.com; "
        "connect-src 'self'; "
        "frame-src 'self' https://www.google.com https://maps.google.com https://www.google.com/maps; "
        "manifest-src 'self'; "
        "worker-src 'self';"
    )
    if request.is_secure or app.config.get("SESSION_COOKIE_SECURE"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith(ADMIN_PANEL_PATH):
        response.headers["Cache-Control"] = "no-store"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.errorhandler(413)
def handle_file_too_large(_error):
    return json_error("Uploaded file is too large for the server limit.", 413)


@app.get("/health")
def health_check():
    with get_connection() as connection:
        connection.execute("SELECT 1").fetchone()
    return jsonify(
        {
            "success": True,
            "status": "ok",
            "timestamp": current_timestamp(),
        }
    )


@app.get("/robots.txt")
def robots_txt():
    base_url = public_base_url()
    lines = [
        "User-agent: *",
        "Allow: /",
        f"Disallow: {LEGACY_ADMIN_PANEL_PATH}",
        f"Disallow: {ADMIN_PANEL_PATH}",
        "Disallow: /api/",
        f"Sitemap: {base_url}/sitemap.xml",
        "",
    ]
    response = make_response("\n".join(lines))
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.get("/sitemap.xml")
def sitemap_xml():
    base_url = public_base_url()
    last_modified = compute_public_last_modified()
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        "  <sitemap>",
        f"    <loc>{html_escape(base_url)}/sitemap-pages.xml</loc>",
        f"    <lastmod>{html_escape(last_modified)}</lastmod>",
        "  </sitemap>",
    ]
    if fetch_results():
        body.extend(
            [
                "  <sitemap>",
                f"    <loc>{html_escape(base_url)}/sitemap-results.xml</loc>",
                f"    <lastmod>{html_escape(last_modified)}</lastmod>",
                "  </sitemap>",
            ]
        )
    body.append("</sitemapindex>")
    return xml_response("\n".join(body))


@app.get("/sitemap-pages.xml")
def sitemap_pages_xml():
    base_url = public_base_url()
    last_modified = compute_public_last_modified()
    page_entries = [
        (f"{base_url}/", last_modified, "daily", "1.0"),
    ]
    return xml_response(build_sitemap_urlset(page_entries))


@app.get("/sitemap-results.xml")
def sitemap_results_xml():
    base_url = public_base_url()
    fallback_last_modified = compute_public_last_modified()
    result_entries: List[Tuple[str, str, str, str]] = []
    for item in fetch_results():
        download_url = str(item.get("download_url") or "").strip()
        if not download_url:
            continue
        upload_date = str(item.get("upload_date") or "").strip()
        result_lastmod = upload_date[:10] if upload_date else fallback_last_modified
        result_entries.append((f"{base_url}{download_url}", result_lastmod, "monthly", "0.7"))
    return xml_response(build_sitemap_urlset(result_entries))


@app.get("/favicon.svg")
def public_favicon_svg():
    return send_from_directory(BASE_DIR, "favicon.svg")


@app.get("/favicon.ico")
def public_favicon_ico():
    return send_from_directory(BASE_DIR, "favicon.ico")


@app.get("/site.webmanifest")
def site_manifest():
    response = send_from_directory(BASE_DIR, "site.webmanifest", mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/service-worker.js")
def service_worker():
    response = send_from_directory(BASE_DIR, "service-worker.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/")
def home():
    return public_index()


@app.get("/index.html")
def public_index():
    index_path = BASE_DIR / "index.html"
    base_url = public_base_url().rstrip("/")
    settings = get_settings()
    replacements = {
        "__PUBLIC_BASE_URL__": base_url,
        "__SEO_TITLE__": html_escape(public_seo_title()),
        "__SEO_DESCRIPTION__": html_escape(public_seo_description()),
        "__STRUCTURED_DATA__": build_public_structured_data(base_url, settings, public_seo_description()),
    }
    html = index_path.read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    response = make_response(html)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Last-Modified"] = compute_public_last_modified()
    return response


@app.get("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOADS_DIR, filename)


@app.get("/api/announcements")
def api_announcements():
    return jsonify(fetch_announcements(public_only=True))


@app.get("/api/results")
def api_results():
    return jsonify(fetch_results(public_only=True))


@app.get("/api/faculty")
def api_faculty():
    return jsonify(fetch_faculty())


@app.get("/api/settings")
def api_settings():
    return jsonify(get_settings())


@app.get("/api/site-summary")
def api_site_summary():
    with get_connection() as connection:
        students = connection.execute("SELECT COUNT(*) AS total FROM students").fetchone()["total"]
        faculty = connection.execute("SELECT COUNT(*) AS total FROM faculty").fetchone()["total"]
        results = connection.execute("SELECT COUNT(*) AS total FROM results").fetchone()["total"]
        announcements = connection.execute("SELECT COUNT(*) AS total FROM announcements").fetchone()["total"]
    return jsonify(
        {
            "students": students,
            "faculty": faculty,
            "results": results,
            "announcements": announcements,
        }
    )


@app.post("/api/message")
def api_message():
    rate_limit_response = enforce_rate_limit("visitor_message_submit")
    if rate_limit_response:
        return rate_limit_response
    security_error = require_public_post_security()
    if security_error:
        return security_error

    payload = parse_request_payload()
    if str(payload.get("website") or "").strip():
        return jsonify({"success": True, "message": "Your message has been sent successfully."})

    validation_error, clean_payload = sanitize_visitor_message_payload(payload)
    if validation_error or not clean_payload:
        return json_error(validation_error or "Unable to send your message right now.")

    created_at = current_timestamp()
    try:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO visitor_messages (full_name, email, mobile, message, is_read, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (
                    clean_payload["full_name"],
                    clean_payload["email"],
                    clean_payload["mobile"],
                    clean_payload["message"],
                    created_at,
                ),
            )
            connection.commit()
    except sqlite3.Error:
        return json_error("Unable to send your message right now. Please try again shortly.", 500)

    return jsonify({"success": True, "message": "Your message has been sent successfully. The academy team will review it shortly."})


@app.post("/api/analytics/visit")
def api_analytics_visit():
    rate_limit_response = enforce_rate_limit("analytics_visit")
    if rate_limit_response:
        return rate_limit_response
    security_error = require_public_post_security()
    if security_error:
        return security_error
    payload = parse_request_payload()
    section_name = normalize_analytics_section(payload.get("section") or payload.get("section_name") or "home")
    page_path = str(payload.get("page_path") or payload.get("path") or "/").strip() or "/"
    if not page_path.startswith("/"):
        page_path = "/"
    record_site_visit(section_name, page_path)
    return jsonify({"success": True})


@app.post("/api/analytics/presence")
def api_analytics_presence():
    rate_limit_response = enforce_rate_limit("analytics_presence")
    if rate_limit_response:
        return rate_limit_response
    security_error = require_public_post_security()
    if security_error:
        return security_error
    payload = parse_request_payload()
    section_name = normalize_analytics_section(payload.get("section") or payload.get("section_name") or "home")
    page_path = str(payload.get("page_path") or payload.get("path") or "/").strip() or "/"
    if not page_path.startswith("/"):
        page_path = "/"
    presence_id = payload.get("presence_id") or payload.get("visitor_id") or ""
    record_site_presence(section_name, page_path, presence_id)
    return jsonify({"success": True})


@app.post("/api/enrollment-status")
def api_enrollment_status():
    rate_limit_response = enforce_rate_limit("enrollment_status")
    if rate_limit_response:
        return rate_limit_response
    security_error = require_public_post_security()
    if security_error:
        return security_error
    payload = parse_request_payload()
    cnic = normalize_cnic(payload.get("cnic") or "")
    settings = get_settings()
    if str(settings.get("status_check_enabled", "1")) != "1":
        disabled_message = render_status_message_template(
            settings.get("status_check_disabled_message") or DEFAULT_SETTINGS["status_check_disabled_message"],
            settings,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "status": "disabled",
                    "message": disabled_message,
                    "action_note": "Please contact the academy office or try again later.",
                    "can_download_form": False,
                    "contact_primary": settings["contact_primary"],
                    "office_timing": settings["office_timing"],
                }
            ),
            403,
        )

    date_of_birth = str(payload.get("date_of_birth") or payload.get("dateOfBirth") or "").strip()

    if not cnic:
        return json_error("Please enter a valid CNIC number.")
    if not normalize_status_lookup_date_of_birth(date_of_birth):
        return json_error("Please enter date of birth in DD/MM/YYYY format.")

    student = find_student_for_status_lookup(cnic, date_of_birth)
    if not student:
        return (
            jsonify(
                {
                    "success": False,
                    "status": "not_found",
                    "message": render_status_message_template(
                        settings.get("status_message_not_found") or DEFAULT_SETTINGS["status_message_not_found"],
                        settings,
                    ),
                }
            ),
            404,
        )

    status = student["status"]
    if status == "confirmed":
        message = render_status_message_template(
            settings.get("status_message_confirmed") or DEFAULT_SETTINGS["status_message_confirmed"],
            settings,
        )
        action_note = (
            f"For further guidance, contact the admin on {settings['contact_primary']} or visit the academy office during {settings['office_timing']}."
        )
    elif status == "rejected":
        message = render_status_message_template(
            settings.get("status_message_rejected") or DEFAULT_SETTINGS["status_message_rejected"],
            settings,
        )
        action_note = (
            f"Please contact the admin on {settings['contact_primary']} or visit the academy office during {settings['office_timing']}."
        )
    else:
        message = render_status_message_template(
            settings.get("status_message_pending") or DEFAULT_SETTINGS["status_message_pending"],
            settings,
        )
        action_note = "Please wait for the academy team to complete the review process."

    return jsonify(
        {
            "success": True,
            "status": status,
            "message": message,
            "action_note": action_note,
            "student_name": student.get("name") or "",
            "class": student.get("class") or "",
            "group": student.get("group") or "",
            "submitted_on": student.get("date") or "",
            "contact_primary": settings["contact_primary"],
            "office_timing": settings["office_timing"],
            "can_download_form": status == "confirmed",
        }
    )


@app.post("/api/enrollment-form-download")
def api_enrollment_form_download():
    rate_limit_response = enforce_rate_limit("enrollment_form_download")
    if rate_limit_response:
        return rate_limit_response
    security_error = require_public_post_security()
    if security_error:
        return security_error
    payload = parse_request_payload()
    cnic = normalize_cnic(payload.get("cnic") or "")
    date_of_birth = str(payload.get("date_of_birth") or payload.get("dateOfBirth") or "").strip()

    if not cnic:
        return json_error("Please enter a valid CNIC number.")
    if not normalize_status_lookup_date_of_birth(date_of_birth):
        return json_error("Please enter date of birth in DD/MM/YYYY format.")

    settings = get_settings()
    if str(settings.get("status_check_enabled", "1")) != "1":
        disabled_message = render_status_message_template(
            settings.get("status_check_disabled_message") or DEFAULT_SETTINGS["status_check_disabled_message"],
            settings,
        )
        return json_error(disabled_message, 403)

    student = find_student_for_status_lookup(cnic, date_of_birth)
    if not student:
        return json_error("No enrollment record matched the provided CNIC number and date of birth.", 404)
    if student["status"] != "confirmed":
        return json_error("Admission form download is available only after admission confirmation.", 400)
    return make_admission_pdf_response(student)


@app.post("/api/enroll")
def api_enroll():
    rate_limit_response = enforce_rate_limit("enrollment_submit")
    if rate_limit_response:
        return rate_limit_response
    security_error = require_public_post_security()
    if security_error:
        return security_error
    form = request.form
    photo_file = request.files.get("photo")
    settings = get_settings()

    if str(settings.get("enrollment_enabled", "1")) != "1":
        return json_error(settings.get("enrollment_closed_message") or "Admissions are currently closed.", 403)

    # Quietly drop obvious bot submissions without revealing the honeypot field.
    if str(form.get("website") or "").strip():
        return jsonify(
            {
                "success": True,
                "message": "Your enrollment form was submitted successfully. We will contact you within 48 hours. Thank you.",
            }
        )

    validation_error, payload = normalize_student_enrollment_payload(form)
    if validation_error:
        return json_error(validation_error)
    allowed_classes = parse_enrollment_class_allowlist(settings.get("enrollment_class_allowlist") or ENROLLMENT_CLASS_CHOICES)
    if payload["class_name"] not in allowed_classes:
        return json_error("Admissions for the selected class are currently closed right now.", 403)
    if photo_file is None or not photo_file.filename:
        return json_error("Passport picture is required.")

    saved_photo = None
    try:
        saved_photo = save_uploaded_file(
            photo_file,
            STUDENT_PHOTOS_DIR,
            ALLOWED_IMAGE_EXTENSIONS,
            MAX_STUDENT_PHOTO_SIZE,
            "student",
        )
        with get_connection() as connection:
            created_at = current_timestamp()
            next_roll_number = next_roll_number_for_class(
                connection,
                payload["class_name"],
                created_at,
                payload["student_group"],
            )
            connection.execute(
                """
                INSERT INTO students
                (roll_number, name, father_name, father_contact, gender, email, date_of_birth, mobile, cnic, photo, class_name, student_group, subjects, address, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_roll_number,
                    payload["name"],
                    payload["father_name"],
                    payload["father_contact"],
                    payload["gender"],
                    payload["email"],
                    payload["date_of_birth"],
                    payload["mobile"],
                    payload["cnic"],
                    saved_photo,
                    payload["class_name"],
                    payload["student_group"],
                    payload["subjects_json"],
                    payload["address"],
                    created_at,
                ),
            )
            connection.commit()
    except ValueError as error:
        if "File is too large" in str(error):
            return json_error("Passport picture must be 300 KB or smaller.")
        return json_error(str(error))
    except sqlite3.Error:
        if saved_photo:
            delete_file(STUDENT_PHOTOS_DIR, saved_photo)
        return json_error("Unable to save the enrollment right now. Please try again.", 500)

    return jsonify(
        {
            "success": True,
            "roll_number": next_roll_number,
            "message": "Your enrollment form was submitted successfully. We will contact you within 48 hours. Thank you.",
        }
    )


@app.route(ADMIN_PANEL_PATH, methods=["GET"])
@app.route(f"{ADMIN_PANEL_PATH}/", methods=["GET"])
def admin_page():
    response = make_response(ADMIN_HTML)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route(LEGACY_ADMIN_PANEL_PATH, defaults={"subpath": ""}, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@app.route(f"{LEGACY_ADMIN_PANEL_PATH}/<path:subpath>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def legacy_admin_proxy(subpath: str):
    target_path = ADMIN_PANEL_PATH if not subpath else f"{ADMIN_PANEL_PATH}/{subpath}"
    response = redirect(target_path, code=308 if request.method != "GET" else 302)
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.get(f"{ADMIN_PANEL_PATH}/session")
def admin_session():
    username = active_admin_username()
    if username:
        return jsonify({"authenticated": True, "username": username, "csrf_token": generate_admin_csrf_token()})
    return jsonify({"authenticated": False, "username": None})


@app.post(f"{ADMIN_PANEL_PATH}/login")
def admin_login():
    rate_limit_response = enforce_rate_limit("admin_login")
    if rate_limit_response:
        return rate_limit_response
    same_origin_error = validate_same_origin_request()
    if same_origin_error:
        return same_origin_error
    data = parse_request_payload()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return json_error("Username and password are required.")
    seeded_admin = configured_admin_seed_credentials()
    if seeded_admin and username == seeded_admin[0] and secrets.compare_digest(password, seeded_admin[1]):
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO admin (username, password_hash)
                VALUES (?, ?)
                ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash
                """,
                (username, generate_password_hash(password)),
            )
            connection.commit()
        write_admin_session(username)
        if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
            response = jsonify({"success": True, "message": "Login successful.", "csrf_token": generate_admin_csrf_token()})
            return apply_admin_remember_cookie(response, username)
        response = redirect(ADMIN_PANEL_PATH)
        return apply_admin_remember_cookie(response, username)
    if is_hosted_runtime() and username == LEGACY_DEFAULT_ADMIN_USERNAME and password == LEGACY_DEFAULT_ADMIN_PASSWORD:
        return json_error("Legacy default admin credentials are disabled on hosted deployments. Set TPA_ADMIN_USERNAME and TPA_ADMIN_PASSWORD in your hosting environment.", 403)

    with get_connection() as connection:
        admin_row = connection.execute(
            "SELECT id, username, password_hash FROM admin WHERE username = ?",
            (username,),
        ).fetchone()

    if not admin_row or not check_password_hash(admin_row["password_hash"], password):
        return json_error("Invalid username or password.", 401)

    write_admin_session(admin_row["username"])
    if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
        response = jsonify({"success": True, "message": "Login successful.", "csrf_token": generate_admin_csrf_token()})
        return apply_admin_remember_cookie(response, admin_row["username"])
    response = redirect(ADMIN_PANEL_PATH)
    return apply_admin_remember_cookie(response, admin_row["username"])


@app.post(f"{ADMIN_PANEL_PATH}/logout")
@login_required
def admin_logout():
    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error
    session.clear()
    response = jsonify({"success": True, "message": "Logged out successfully."})
    return clear_admin_remember_cookie(response)


@app.route(f"{ADMIN_PANEL_PATH}/announcements", methods=["GET", "POST", "PUT", "DELETE"])
@login_required
def admin_announcements():
    if request.method == "GET":
        return jsonify(fetch_announcements())

    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error
    data = parse_request_payload()
    validation_error, payload = sanitize_announcement_payload(data)
    if validation_error and request.method != "DELETE":
        return json_error(validation_error)

    if request.method == "POST":
        with get_connection() as connection:
            cursor = connection.execute(
                "INSERT INTO announcements (title, description, date, is_new, is_published) VALUES (?, ?, ?, ?, ?)",
                (payload["title"], payload["description"], payload["date"], payload["is_new"], payload["is_published"]),
            )
            connection.commit()
        log_admin_activity(
            "announcement_add",
            f"Saved notice: {payload['title']}",
            "announcement",
            cursor.lastrowid,
            {"published": payload["is_published"] == "1"},
        )
        return jsonify({"success": True, "message": "Notice saved successfully."})

    if request.method == "PUT":
        announcement_id = data.get("id")
        if not announcement_id:
            return json_error("Announcement ID is required.")
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE announcements
                SET title = ?, description = ?, date = ?, is_new = ?, is_published = ?
                WHERE id = ?
                """,
                (payload["title"], payload["description"], payload["date"], payload["is_new"], payload["is_published"], announcement_id),
            )
            connection.commit()
        log_admin_activity(
            "announcement_edit",
            f"Updated notice: {payload['title']}",
            "announcement",
            announcement_id,
            {"published": payload["is_published"] == "1"},
        )
        return jsonify({"success": True, "message": "Notice updated successfully."})

    announcement_id = data.get("id")
    if not announcement_id:
        return json_error("Announcement ID is required.")
    with get_connection() as connection:
        deleted_row = connection.execute("SELECT title FROM announcements WHERE id = ?", (announcement_id,)).fetchone()
        connection.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
        connection.commit()
    log_admin_activity(
        "announcement_delete",
        f"Deleted notice: {deleted_row['title'] if deleted_row else 'Notice'}",
        "announcement",
        announcement_id,
    )
    return jsonify({"success": True, "message": "Announcement deleted successfully."})


@app.route(f"{ADMIN_PANEL_PATH}/results", methods=["GET", "POST", "PUT", "DELETE"])
@login_required
def admin_results():
    if request.method == "GET":
        return jsonify(fetch_results())

    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error

    if request.method == "POST":
        validation_error, payload = sanitize_result_payload(request.form)
        if validation_error:
            return json_error(validation_error)
        pdf_file = request.files.get("pdf")

        if pdf_file is None or not pdf_file.filename:
            return json_error("Please upload a PDF file.")

        saved_pdf = None
        try:
            saved_pdf = save_uploaded_file(
                pdf_file,
                RESULTS_DIR,
                ALLOWED_RESULT_EXTENSIONS,
                MAX_RESULT_SIZE,
                "result",
            )
            with get_connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO results (title, class_name, year, pdf_filename, upload_date, is_new, is_published)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (payload["title"], payload["class_name"], payload["year"], saved_pdf, current_timestamp(), payload["is_new"], payload["is_published"]),
                )
                connection.commit()
        except ValueError as error:
            return json_error(str(error))
        except sqlite3.Error:
            if saved_pdf:
                delete_file(RESULTS_DIR, saved_pdf)
            return json_error("Unable to save the result right now.", 500)

        log_admin_activity(
            "result_add",
            f"Saved result: {payload['title']}",
            "result",
            cursor.lastrowid,
            {"published": payload["is_published"] == "1"},
        )
        return jsonify({"success": True, "message": "Result saved successfully."})

    if request.method == "PUT":
        result_id = (request.form.get("id") or "").strip()
        validation_error, payload = sanitize_result_payload(request.form)
        if validation_error:
            return json_error(validation_error)
        pdf_file = request.files.get("pdf")

        if not result_id:
            return json_error("Result ID is required.")

        with get_connection() as connection:
            result_row = connection.execute(
                "SELECT id, pdf_filename FROM results WHERE id = ?",
                (result_id,),
            ).fetchone()
        if not result_row:
            return json_error("Result not found.", 404)

        old_pdf = result_row["pdf_filename"]
        saved_pdf = old_pdf
        replacement_pdf = None
        try:
            if pdf_file is not None and pdf_file.filename:
                replacement_pdf = save_uploaded_file(
                    pdf_file,
                    RESULTS_DIR,
                    ALLOWED_RESULT_EXTENSIONS,
                    MAX_RESULT_SIZE,
                    "result",
                )
                saved_pdf = replacement_pdf

            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE results
                    SET title = ?, class_name = ?, year = ?, pdf_filename = ?, is_new = ?, is_published = ?
                    WHERE id = ?
                    """,
                    (payload["title"], payload["class_name"], payload["year"], saved_pdf, payload["is_new"], payload["is_published"], result_id),
                )
                connection.commit()
        except ValueError as error:
            if replacement_pdf:
                delete_file(RESULTS_DIR, replacement_pdf)
            return json_error(str(error))
        except sqlite3.Error:
            if replacement_pdf:
                delete_file(RESULTS_DIR, replacement_pdf)
            return json_error("Unable to update the result right now.", 500)

        if replacement_pdf and old_pdf and old_pdf != replacement_pdf:
            delete_file(RESULTS_DIR, old_pdf)
        log_admin_activity(
            "result_edit",
            f"Updated result: {payload['title']}",
            "result",
            result_id,
            {"published": payload["is_published"] == "1", "pdf_replaced": bool(replacement_pdf)},
        )
        return jsonify({"success": True, "message": "Result updated successfully."})

    data = parse_request_payload()
    result_id = data.get("id")
    if not result_id:
        return json_error("Result ID is required.")

    with get_connection() as connection:
        result_row = connection.execute(
            "SELECT id, title, pdf_filename FROM results WHERE id = ?",
            (result_id,),
        ).fetchone()
        if not result_row:
            return json_error("Result not found.", 404)
        connection.execute("DELETE FROM results WHERE id = ?", (result_id,))
        connection.commit()

    delete_file(RESULTS_DIR, result_row["pdf_filename"])
    log_admin_activity(
        "result_delete",
        f"Deleted result: {result_row['title']}",
        "result",
        result_id,
    )
    return jsonify({"success": True, "message": "Result deleted successfully."})


def move_faculty_member(faculty_id: int, direction: str) -> None:
    with get_connection() as connection:
        current_row = connection.execute(
            "SELECT id, display_order FROM faculty WHERE id = ?",
            (faculty_id,),
        ).fetchone()
        if not current_row:
            raise ValueError("Faculty member not found.")

        if direction == "up":
            neighbor = connection.execute(
                """
                SELECT id, display_order FROM faculty
                WHERE display_order < ?
                ORDER BY display_order DESC, id DESC
                LIMIT 1
                """,
                (current_row["display_order"],),
            ).fetchone()
        else:
            neighbor = connection.execute(
                """
                SELECT id, display_order FROM faculty
                WHERE display_order > ?
                ORDER BY display_order ASC, id ASC
                LIMIT 1
                """,
                (current_row["display_order"],),
            ).fetchone()

        if not neighbor:
            return

        connection.execute(
            "UPDATE faculty SET display_order = ? WHERE id = ?",
            (neighbor["display_order"], current_row["id"]),
        )
        connection.execute(
            "UPDATE faculty SET display_order = ? WHERE id = ?",
            (current_row["display_order"], neighbor["id"]),
        )
        connection.commit()


@app.route(f"{ADMIN_PANEL_PATH}/faculty", methods=["GET", "POST", "PUT", "DELETE"])
@login_required
def admin_faculty():
    if request.method == "GET":
        return jsonify(fetch_faculty())

    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error

    if request.method == "POST":
        validation_error, payload = sanitize_faculty_payload(request.form)
        if validation_error:
            return json_error(validation_error)
        photo_file = request.files.get("photo")

        saved_photo = None
        try:
            if photo_file and photo_file.filename:
                saved_photo = save_uploaded_file(
                    photo_file,
                    FACULTY_PHOTOS_DIR,
                    ALLOWED_IMAGE_EXTENSIONS,
                    MAX_FACULTY_PHOTO_SIZE,
                    "faculty",
                )

            with get_connection() as connection:
                max_order_row = connection.execute("SELECT COALESCE(MAX(display_order), 0) AS value FROM faculty").fetchone()
                next_order = int(max_order_row["value"]) + 1
                connection.execute(
                    """
                    INSERT INTO faculty (name, photo, class_assigned, subject, qualification, experience_years, display_order)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["name"],
                        saved_photo,
                        payload["class_assigned"],
                        payload["subject"],
                        payload["qualification"],
                        payload["experience_years"],
                        next_order,
                    ),
                )
                connection.commit()
        except ValueError as error:
            return json_error(str(error))
        except sqlite3.Error:
            if saved_photo:
                delete_file(FACULTY_PHOTOS_DIR, saved_photo)
            return json_error("Unable to save the faculty record right now.", 500)

        return jsonify({"success": True, "message": "Faculty member added successfully."})

    if request.method == "PUT":
        if request.is_json:
            data = request.get_json(silent=True) or {}
            if data.get("action") == "move":
                faculty_id = data.get("id")
                direction = (data.get("direction") or "").strip().lower()
                if not faculty_id or direction not in {"up", "down"}:
                    return json_error("Valid faculty ID and direction are required.")
                try:
                    move_faculty_member(int(faculty_id), direction)
                except ValueError as error:
                    return json_error(str(error), 404)
                return jsonify({"success": True, "message": "Faculty order updated successfully."})

            faculty_id = data.get("id")
            if not faculty_id:
                return json_error("Faculty ID is required.")
            validation_error, payload = sanitize_faculty_payload(data)
            if validation_error:
                return json_error(validation_error)

            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE faculty
                    SET name = ?, class_assigned = ?, subject = ?, qualification = ?, experience_years = ?
                    WHERE id = ?
                    """,
                    (
                        payload["name"],
                        payload["class_assigned"],
                        payload["subject"],
                        payload["qualification"],
                        payload["experience_years"],
                        faculty_id,
                    ),
                )
                connection.commit()
            return jsonify({"success": True, "message": "Faculty member updated successfully."})

        faculty_id = request.form.get("id")
        validation_error, payload = sanitize_faculty_payload(request.form)
        if validation_error:
            return json_error(validation_error)
        photo_file = request.files.get("photo")
        remove_existing_photo = str(request.form.get("remove_photo") or "").strip().lower() in {"1", "true", "on", "yes"}

        if not faculty_id:
            return json_error("Faculty ID is required.")

        with get_connection() as connection:
            existing_row = connection.execute(
                "SELECT id, photo FROM faculty WHERE id = ?",
                (faculty_id,),
            ).fetchone()
            if not existing_row:
                return json_error("Faculty member not found.", 404)

        saved_photo = existing_row["photo"]
        new_photo = None
        try:
            if photo_file and photo_file.filename:
                new_photo = save_uploaded_file(
                    photo_file,
                    FACULTY_PHOTOS_DIR,
                    ALLOWED_IMAGE_EXTENSIONS,
                    MAX_FACULTY_PHOTO_SIZE,
                    "faculty",
                )
                saved_photo = new_photo
            elif remove_existing_photo:
                saved_photo = None

            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE faculty
                    SET name = ?, photo = ?, class_assigned = ?, subject = ?, qualification = ?, experience_years = ?
                    WHERE id = ?
                    """,
                    (
                        payload["name"],
                        saved_photo,
                        payload["class_assigned"],
                        payload["subject"],
                        payload["qualification"],
                        payload["experience_years"],
                        faculty_id,
                    ),
                )
                connection.commit()
        except ValueError as error:
            return json_error(str(error))
        except sqlite3.Error:
            if new_photo:
                delete_file(FACULTY_PHOTOS_DIR, new_photo)
            return json_error("Unable to update the faculty member right now.", 500)

        if (new_photo or remove_existing_photo) and existing_row["photo"]:
            delete_file(FACULTY_PHOTOS_DIR, existing_row["photo"])

        return jsonify({"success": True, "message": "Faculty member updated successfully."})

    data = parse_request_payload()
    faculty_id = data.get("id")
    if not faculty_id:
        return json_error("Faculty ID is required.")

    with get_connection() as connection:
        faculty_row = connection.execute(
            "SELECT id, photo FROM faculty WHERE id = ?",
            (faculty_id,),
        ).fetchone()
        if not faculty_row:
            return json_error("Faculty member not found.", 404)
        connection.execute("DELETE FROM faculty WHERE id = ?", (faculty_id,))
        connection.commit()

    delete_file(FACULTY_PHOTOS_DIR, faculty_row["photo"])
    return jsonify({"success": True, "message": "Faculty member deleted successfully."})


@app.get(f"{ADMIN_PANEL_PATH}/enrollments")
@login_required
def admin_enrollments():
    search_term = (request.args.get("q") or "").strip()
    return jsonify(
        {
            "pending": fetch_enrollments(search_term, status="pending"),
            "confirmed": fetch_enrollments(search_term, status="confirmed"),
            "rejected": fetch_enrollments(search_term, status="rejected"),
        }
    )


@app.post(f"{ADMIN_PANEL_PATH}/enrollments/bulk")
@login_required
def admin_bulk_enrollments():
    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error
    data = parse_request_payload()
    raw_ids = data.get("ids") or []
    if not isinstance(raw_ids, list):
        return json_error("Please choose at least one student record.")
    ids: List[int] = []
    for item in raw_ids:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    ids = sorted({student_id for student_id in ids if student_id > 0})
    if not ids:
        return json_error("Please choose at least one student record.")
    action = str(data.get("action") or "").strip().lower()
    if action not in {"confirm", "reject", "delete"}:
        return json_error("Please choose a valid bulk action.")

    placeholders = ",".join("?" for _ in ids)
    with get_connection() as connection:
        rows = connection.execute(
            f"SELECT id, name, confirmed_at, rejected_at, photo FROM students WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        if not rows:
            return json_error("Selected student records were not found.", 404)

        processed_names: List[str] = []
        removed_photos: List[str] = []
        now_value = current_timestamp()
        for row in rows:
            processed_names.append(str(row["name"] or "Student"))
            if action == "confirm":
                connection.execute(
                    "UPDATE students SET confirmed_at = ?, rejected_at = NULL WHERE id = ?",
                    (now_value, row["id"]),
                )
            elif action == "reject":
                connection.execute(
                    "UPDATE students SET rejected_at = ?, confirmed_at = NULL WHERE id = ?",
                    (now_value, row["id"]),
                )
            else:
                connection.execute("DELETE FROM students WHERE id = ?", (row["id"],))
                if row["photo"]:
                    removed_photos.append(row["photo"])
            clear_generated_form_cache(row["id"])
        connection.commit()

    if action == "delete":
        for photo_name in removed_photos:
            delete_file(STUDENT_PHOTOS_DIR, photo_name)

    action_labels = {"confirm": "Confirmed", "reject": "Rejected", "delete": "Deleted"}
    log_admin_activity(
        f"bulk_{action}",
        f"{action_labels[action]} {len(processed_names)} student record(s)",
        "student_bulk",
        ",".join(str(student_id) for student_id in ids),
        {"names": processed_names[:12], "count": len(processed_names)},
    )
    return jsonify({"success": True, "message": f"{action_labels[action]} {len(processed_names)} student record(s) successfully."})


@app.get(f"{ADMIN_PANEL_PATH}/insights")
@login_required
def admin_insights():
    return jsonify(fetch_last_7_days_insights())


@app.get(f"{ADMIN_PANEL_PATH}/activity-log")
@login_required
def admin_activity_log():
    limit_raw = (request.args.get("limit") or "50").strip()
    try:
        limit = int(limit_raw)
    except ValueError:
        limit = 50
    return jsonify(fetch_activity_log(limit))


@app.route(f"{ADMIN_PANEL_PATH}/messages", methods=["GET", "PUT", "DELETE"])
@login_required
def admin_messages():
    if request.method == "GET":
        return jsonify(fetch_visitor_messages())

    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error

    data = parse_request_payload()
    try:
        message_id = int(data.get("id") or 0)
    except (TypeError, ValueError):
        message_id = 0
    if message_id <= 0:
        return json_error("Please choose a valid message record.")

    with get_connection() as connection:
        existing_row = connection.execute(
            "SELECT id, full_name, email, is_read FROM visitor_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if not existing_row:
            return json_error("Selected message record was not found.", 404)

        if request.method == "PUT":
            is_read = 1 if str(data.get("is_read", "")).strip() in {"1", "true", "True", "on", "yes"} else 0
            connection.execute(
                "UPDATE visitor_messages SET is_read = ? WHERE id = ?",
                (is_read, message_id),
            )
            connection.commit()
            log_admin_activity(
                "visitor_message_update",
                f"{'Marked' if is_read else 'Reopened'} website message from {existing_row['full_name']}",
                "visitor_message",
                str(message_id),
                {"email": existing_row["email"], "is_read": bool(is_read)},
            )
            return jsonify(
                {
                    "success": True,
                    "message": "Message marked handled successfully." if is_read else "Message marked unread successfully.",
                }
            )

        connection.execute("DELETE FROM visitor_messages WHERE id = ?", (message_id,))
        connection.commit()
        log_admin_activity(
            "visitor_message_delete",
            f"Deleted website message from {existing_row['full_name']}",
            "visitor_message",
            str(message_id),
            {"email": existing_row["email"], "was_read": bool(existing_row["is_read"])},
        )
        return jsonify({"success": True, "message": "Message deleted successfully."})


@app.get(f"{ADMIN_PANEL_PATH}/backup")
@login_required
def admin_backup():
    backup_bytes = build_backup_archive_bytes()
    filename = f"the_professors_academy_backup_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.zip"
    response = make_response(backup_bytes)
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    log_admin_activity("backup_download", "Downloaded a full system backup", "backup", filename)
    return response


@app.post(f"{ADMIN_PANEL_PATH}/restore")
@login_required
def admin_restore():
    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error
    backup_file = request.files.get("backup_file")
    if backup_file is None or not backup_file.filename:
        return json_error("Please choose a backup file first.")
    try:
        restore_backup_archive(backup_file.filename, backup_file.read())
    except ValueError as error:
        return json_error(str(error))
    except sqlite3.DatabaseError:
        return json_error("Backup file database is not valid.", 400)
    except Exception:
        logging.exception("Backup restore failed")
        return json_error("Unable to restore the backup right now.", 500)
    log_admin_activity("backup_restore", f"Restored backup file: {backup_file.filename}", "backup", backup_file.filename)
    return jsonify({"success": True, "message": "Backup restored successfully."})


@app.get(f"{ADMIN_PANEL_PATH}/reports/summary")
@login_required
def admin_summary_report():
    range_key = (request.args.get("range") or "7").strip().lower()
    if range_key not in {"7", "30", "all"}:
        return json_error("Valid report range values are 7, 30, or all.")

    days = None if range_key == "all" else int(range_key)
    report_data = fetch_admission_report(days)
    csv_text = build_report_csv(report_data)
    filename_suffix = "all_time" if days is None else f"last_{days}_days"

    response = make_response(csv_text)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="the_professors_academy_admission_report_{filename_suffix}.csv"'
    )
    return response


@app.get(f"{ADMIN_PANEL_PATH}/reports/enrollments-export")
@login_required
def admin_enrollments_export():
    status = (request.args.get("status") or "pending").strip().lower()
    class_name = (request.args.get("class_name") or "").strip()
    if status not in {"pending", "confirmed", "rejected"}:
        return json_error("Valid enrollment export status values are pending, confirmed, or rejected.")

    students = fetch_enrollments(status=status, class_name=class_name or None)
    title_status = status.title()
    title = f"The Professors Academy {title_status} Enrollments"
    if class_name:
        title = f"{title} - {class_name}"
    csv_text = build_enrollments_csv(students, title)
    suffix = safe_filename_fragment(class_name or "all_classes")
    filename = f"the_professors_academy_{status}_enrollments_{suffix}.csv"

    response = make_response(csv_text)
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@app.get(f"{ADMIN_PANEL_PATH}/reports/confirmed-forms")
@login_required
def admin_confirmed_forms_report():
    class_name = (request.args.get("class_name") or "").strip()
    students = fetch_enrollments(status="confirmed", class_name=class_name or None)
    title = "The Professors Academy Confirmed Admission Forms"
    if class_name:
        title = f"{title} - {class_name}"
    pdf_bytes = build_admission_form_pdf(students, title)
    suffix = safe_filename_fragment(class_name or "all_classes")
    response = make_response(pdf_bytes)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = (
        f'attachment; filename="the_professors_academy_confirmed_admission_forms_{suffix}.pdf"'
    )
    return response


@app.post(f"{ADMIN_PANEL_PATH}/enrollment/<int:student_id>/confirm")
@login_required
def admin_confirm_enrollment(student_id: int):
    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error
    with get_connection() as connection:
        student_row = connection.execute(
            "SELECT id, name, confirmed_at, rejected_at FROM students WHERE id = ?",
            (student_id,),
        ).fetchone()
        if not student_row:
            return json_error("Enrollment not found.", 404)
        if student_row["confirmed_at"]:
            return json_error("Admission is already confirmed.")
        if student_row["rejected_at"]:
            return json_error("This admission has already been rejected.")

        connection.execute(
            "UPDATE students SET confirmed_at = ?, rejected_at = NULL WHERE id = ?",
            (current_timestamp(), student_id),
        )
        connection.commit()
    clear_generated_form_cache(student_id)
    log_admin_activity("enrollment_confirm", f"Confirmed admission: {student_row['name']}", "student", student_id)

    confirmed_student = fetch_student(student_id)
    if confirmed_student:
        try:
            build_admission_form_pdf([confirmed_student], f"{confirmed_student['name']} Admission Form")
        except Exception:
            pass

    return jsonify({"success": True, "message": "Admission confirmed successfully."})


@app.post(f"{ADMIN_PANEL_PATH}/enrollment/<int:student_id>/reject")
@login_required
def admin_reject_enrollment(student_id: int):
    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error
    with get_connection() as connection:
        student_row = connection.execute(
            "SELECT id, name, confirmed_at, rejected_at FROM students WHERE id = ?",
            (student_id,),
        ).fetchone()
        if not student_row:
            return json_error("Enrollment not found.", 404)
        if student_row["rejected_at"]:
            return json_error("Admission is already rejected.")
        if student_row["confirmed_at"]:
            return json_error("This admission is already confirmed.")

        connection.execute(
            "UPDATE students SET rejected_at = ?, confirmed_at = NULL WHERE id = ?",
            (current_timestamp(), student_id),
        )
        connection.commit()
    clear_generated_form_cache(student_id)
    log_admin_activity("enrollment_reject", f"Rejected admission: {student_row['name']}", "student", student_id)
    return jsonify({"success": True, "message": "Admission rejected successfully."})


@app.get(f"{ADMIN_PANEL_PATH}/enrollment/<int:student_id>/form")
@login_required
def admin_enrollment_form(student_id: int):
    student = fetch_student(student_id)
    if not student:
        response = make_response("<h1>Enrollment not found.</h1>", 404)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response
    if student["status"] != "confirmed":
        response = make_response("<h1>This form is only available for confirmed admissions.</h1>", 400)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        return response

    download_mode = (request.args.get("download") or "").strip().lower()
    if download_mode in {"1", "pdf"}:
        return make_admission_pdf_response(student)
    if download_mode in {"html", "editable"}:
        return make_admission_document_response(student, download=True)

    return make_admission_document_response(student, download=False)


@app.route(f"{ADMIN_PANEL_PATH}/enrollment/<int:student_id>", methods=["PUT", "DELETE"])
@login_required
def admin_enrollment_record(student_id: int):
    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error
    if request.method == "PUT":
        form = request.form
        photo_file = request.files.get("photo")
        validation_error, payload = normalize_student_enrollment_payload(form)
        if validation_error:
            return json_error(validation_error)

        with get_connection() as connection:
            student_row = connection.execute(
                "SELECT id, photo, class_name, student_group, created_at, roll_number FROM students WHERE id = ?",
                (student_id,),
            ).fetchone()
        if not student_row:
            return json_error("Enrollment not found.", 404)

        old_photo = student_row["photo"]
        saved_photo = old_photo
        new_photo = None
        try:
            if photo_file is not None and photo_file.filename:
                new_photo = save_uploaded_file(
                    photo_file,
                    STUDENT_PHOTOS_DIR,
                    ALLOWED_IMAGE_EXTENSIONS,
                    MAX_STUDENT_PHOTO_SIZE,
                    "student",
                )
                saved_photo = new_photo

            with get_connection() as connection:
                updated_roll_number = student_row["roll_number"]
                current_class_name = str(student_row["class_name"] or "").strip()
                current_group = str(student_row["student_group"] or "").strip()
                new_group = str(payload["student_group"] or "").strip()
                if current_class_name != payload["class_name"] or current_group != new_group:
                    updated_roll_number = next_roll_number_for_class(
                        connection,
                        payload["class_name"],
                        student_row["created_at"],
                        payload["student_group"],
                    )
                connection.execute(
                    """
                    UPDATE students
                    SET roll_number = ?, name = ?, father_name = ?, father_contact = ?, gender = ?, email = ?,
                        date_of_birth = ?, mobile = ?, cnic = ?, photo = ?, class_name = ?, student_group = ?,
                        subjects = ?, address = ?
                    WHERE id = ?
                    """,
                    (
                        updated_roll_number,
                        payload["name"],
                        payload["father_name"],
                        payload["father_contact"],
                        payload["gender"],
                        payload["email"],
                        payload["date_of_birth"],
                        payload["mobile"],
                        payload["cnic"],
                        saved_photo,
                        payload["class_name"],
                        payload["student_group"],
                        payload["subjects_json"],
                        payload["address"],
                        student_id,
                    ),
                )
                connection.commit()
        except ValueError as error:
            if new_photo:
                delete_file(STUDENT_PHOTOS_DIR, new_photo)
            if "File is too large" in str(error):
                return json_error("Passport picture must be 300 KB or smaller.")
            return json_error(str(error))
        except sqlite3.Error:
            if new_photo:
                delete_file(STUDENT_PHOTOS_DIR, new_photo)
            return json_error("Unable to update the enrollment right now.", 500)

        if new_photo and old_photo and old_photo != new_photo:
            delete_file(STUDENT_PHOTOS_DIR, old_photo)
        clear_generated_form_cache(student_id)
        log_admin_activity("enrollment_edit", f"Updated admission form: {payload['name']}", "student", student_id)
        return jsonify({"success": True, "message": "Enrollment updated successfully."})

    with get_connection() as connection:
        student_row = connection.execute(
            "SELECT id, name, photo FROM students WHERE id = ?",
            (student_id,),
        ).fetchone()
        if not student_row:
            return json_error("Enrollment not found.", 404)
        connection.execute("DELETE FROM students WHERE id = ?", (student_id,))
        connection.commit()

    delete_file(STUDENT_PHOTOS_DIR, student_row["photo"])
    clear_generated_form_cache(student_id)
    log_admin_activity("enrollment_delete", f"Deleted admission record: {student_row['name']}", "student", student_id)
    return jsonify({"success": True, "message": "Enrollment deleted successfully."})


@app.route(f"{ADMIN_PANEL_PATH}/settings", methods=["GET", "PUT"])
@login_required
def admin_settings():
    if request.method == "GET":
        return jsonify(get_settings())

    csrf_error = require_admin_write_security()
    if csrf_error:
        return csrf_error
    data = parse_request_payload()
    validation_error, values_to_save = sanitize_settings_payload(data)
    if validation_error:
        return json_error(validation_error)
    if values_to_save["homepage_popup_target_section"] not in POPUP_SECTION_CHOICES:
        values_to_save["homepage_popup_target_section"] = ""
    popup_result_id = values_to_save["homepage_popup_result_id"]
    values_to_save["homepage_popup_result_id"] = popup_result_id if popup_result_id.isdigit() else ""
    current_settings = get_settings()
    saved_gallery_images: List[str] = []
    replaced_gallery_files: List[str] = []

    try:
        for index in range(1, 5):
            file_storage = request.files.get(f"gallery_item_{index}_file")
            if file_storage is None or not (file_storage.filename or "").strip():
                continue
            saved_name = save_uploaded_file(
                file_storage,
                GALLERY_IMAGES_DIR,
                ALLOWED_IMAGE_EXTENSIONS,
                MAX_GALLERY_IMAGE_SIZE,
                "gallery",
            )
            saved_gallery_images.append(saved_name)
            values_to_save[f"gallery_item_{index}_image"] = f"/uploads/gallery_images/{saved_name}"
            old_filename = gallery_image_filename_from_value(current_settings.get(f"gallery_item_{index}_image") or "")
            if old_filename and old_filename != saved_name:
                replaced_gallery_files.append(old_filename)
    except ValueError as error:
        for saved_name in saved_gallery_images:
            delete_file(GALLERY_IMAGES_DIR, saved_name)
        return json_error(str(error))

    try:
        with get_connection() as connection:
            for key, value in values_to_save.items():
                connection.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            connection.commit()
    except sqlite3.Error:
        for saved_name in saved_gallery_images:
            delete_file(GALLERY_IMAGES_DIR, saved_name)
        return json_error("Unable to save settings right now.", 500)

    for old_filename in replaced_gallery_files:
        delete_file(GALLERY_IMAGES_DIR, old_filename)

    log_admin_activity("settings_update", "Updated website settings", "settings", "public_site")
    return jsonify({"success": True, "message": "Settings updated successfully."})


ADMIN_HTML_PARTS = [
    r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>The Professors Academy Admin</title>
    <meta name="theme-color" content="#0a1929">
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/icons/icon-32.png">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Poppins:wght@600;700;800&display=swap" rel="stylesheet">
    <style>
        :root{--navy:#0a1929;--navy2:#102640;--navy3:#1a3655;--gold:#f0b90b;--gold-soft:#fff4cf;--muted:#64748b;--line:rgba(10,25,41,.1);--line-strong:rgba(10,25,41,.18);--card:rgba(255,255,255,.94);--card-strong:rgba(255,255,255,.985);--danger:#b42318;--success:#12715b;--shadow:0 28px 72px rgba(10,25,41,.14);--shadow-soft:0 18px 42px rgba(10,25,41,.08);--shadow-hover:0 34px 86px rgba(10,25,41,.18);--radius-xl:30px;--radius-lg:24px;--radius-md:20px;--radius-sm:16px}
        *{box-sizing:border-box}html,body{max-width:100%;overflow-x:hidden}body{margin:0;font-family:Inter,sans-serif;color:#12243a;background:radial-gradient(circle at top left,rgba(240,185,11,.16),transparent 28%),radial-gradient(circle at top right,rgba(16,38,64,.08),transparent 24%),radial-gradient(circle at bottom left,rgba(10,25,41,.08),transparent 25%),linear-gradient(180deg,#f4f7fb 0,#eef3fb 45%,#f6f8fc 100%)}button,input,textarea,select{font:inherit}a{color:inherit}.hidden{display:none!important}
        .shell{min-height:100vh;padding:22px;overflow-x:hidden}.login{max-width:1160px;margin:0 auto;min-height:calc(100vh - 44px);display:grid;grid-template-columns:1.05fr .95fr;gap:26px;align-items:center}.glass,.panel,.card,.stat{background:linear-gradient(180deg,var(--card-strong),rgba(255,255,255,.92));border:1px solid rgba(255,255,255,.88);border-radius:var(--radius-xl);box-shadow:var(--shadow);backdrop-filter:blur(16px)}
        .copy,.login-card,.sidebar,.topbar,.panel,.card,.stat{position:relative;overflow:hidden}
        .copy{padding:40px}.copy:before,.panel:before,.topbar:before{content:"";position:absolute;top:0;left:24px;right:24px;height:4px;border-radius:999px;background:linear-gradient(90deg,rgba(240,185,11,.96),rgba(10,25,41,.16))}.copy:after{content:"";position:absolute;right:-60px;bottom:-60px;width:220px;height:220px;border-radius:50%;background:radial-gradient(circle,rgba(240,185,11,.32),transparent 70%)}.eyebrow{display:inline-flex;padding:8px 14px;border-radius:999px;background:rgba(240,185,11,.12);color:var(--navy);font-weight:800;text-transform:uppercase;letter-spacing:.08em;font-size:.74rem;box-shadow:inset 0 1px 0 rgba(255,255,255,.6)}
        h1,h2,h3,h4{margin:0;font-family:Poppins,sans-serif;color:var(--navy);letter-spacing:-.03em}.copy h1{margin-top:18px;font-size:clamp(2.3rem,6vw,4.4rem);line-height:1}.copy p,.muted{color:var(--muted);line-height:1.8}.mini{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:24px}.mini .card{padding:18px}.mini strong{display:block;font-size:1.4rem}
        .login-card{padding:34px}.field{display:grid;gap:8px;margin-top:14px}.field label{font-weight:700;color:var(--navy);font-size:.93rem}.input,textarea,select{width:100%;padding:15px 16px;border-radius:18px;border:1px solid var(--line);background:#fff;color:#12243a;outline:none;box-shadow:inset 0 1px 0 rgba(255,255,255,.72)}textarea{min-height:110px;resize:vertical}.input:focus,textarea:focus,select:focus{border-color:rgba(240,185,11,.85);box-shadow:0 0 0 4px rgba(240,185,11,.14)}
        .btn{border:none;border-radius:18px;padding:13px 18px;font-weight:800;cursor:pointer;transition:transform .22s ease,box-shadow .22s ease,background .22s ease}.btn:hover{transform:translateY(-1px)}.btn-primary{background:linear-gradient(135deg,var(--gold),#ffd75e);color:var(--navy);box-shadow:0 16px 30px rgba(240,185,11,.24)}.btn-secondary{background:linear-gradient(135deg,var(--navy),#193553);color:#fff;box-shadow:0 16px 28px rgba(10,25,41,.18)}.btn-soft{background:rgba(10,25,41,.05);color:var(--navy);border:1px solid rgba(10,25,41,.08)}.btn-danger{background:rgba(180,35,24,.12);color:var(--danger);border:1px solid rgba(180,35,24,.12)}
        .app{max-width:1500px;margin:0 auto;display:grid;grid-template-columns:320px minmax(0,1fr);gap:22px}.sidebar{padding:24px;border-radius:28px;background:linear-gradient(180deg,#091523 0,#102640 42%,#153250 100%);border:1px solid rgba(240,185,11,.14);color:#fff;display:flex;flex-direction:column;gap:18px;box-shadow:var(--shadow-hover);position:sticky;top:20px;align-self:start;counter-reset:admin-nav}.sidebar:before{content:"";position:absolute;top:-80px;right:-30px;width:240px;height:240px;border-radius:50%;background:radial-gradient(circle,rgba(240,185,11,.22),transparent 72%)}.sidebar:after{content:"";position:absolute;left:20px;right:20px;bottom:82px;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.16),transparent)}.sidebar small{color:rgba(255,255,255,.74);position:relative}.sidebar-nav-title{display:flex;align-items:center;gap:10px;color:rgba(255,255,255,.78);font-size:.75rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.sidebar-nav-title:before{content:"";width:22px;height:2px;border-radius:999px;background:rgba(240,185,11,.85)}.sidebar-cluster{display:grid;gap:12px}.tab-list{display:grid;gap:12px;position:relative}.tab{display:grid;gap:6px;border:1px solid rgba(255,255,255,.08);background:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.04));color:#fff;border-radius:20px;padding:16px 18px 16px 56px;text-align:left;cursor:pointer;font-weight:700;transition:transform .22s ease,background .22s ease,border-color .22s ease,box-shadow .22s ease}.tab:before{counter-increment:admin-nav;content:counter(admin-nav, decimal-leading-zero);position:absolute;margin-left:-38px;align-self:start;width:28px;height:28px;display:inline-grid;place-items:center;border-radius:999px;background:rgba(255,255,255,.1);color:rgba(255,255,255,.88);font-size:.72rem;font-weight:800;letter-spacing:.08em}.tab span{display:block;font-weight:800;font-size:.97rem}.tab small{display:block;color:rgba(255,255,255,.68);font-size:.72rem;line-height:1.48}.tab.active,.tab:hover{background:linear-gradient(135deg,rgba(240,185,11,.18),rgba(255,255,255,.08));border-color:rgba(240,185,11,.34);transform:translateX(3px);box-shadow:0 16px 28px rgba(4,12,20,.18)}.tab.active:before,.tab:hover:before{background:rgba(240,185,11,.2);color:#fff}.tab.active small,.tab:hover small{color:rgba(255,255,255,.92)}
        .main{display:grid;gap:22px}.topbar,.panel{padding:24px}.topbar{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;background:linear-gradient(135deg,rgba(255,255,255,.98),rgba(247,250,255,.94))}.topbar>div:first-child{display:grid;gap:10px}.topbar h1{font-size:clamp(1.85rem,2vw,2.5rem)}.topbar-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:flex-end;max-width:480px}.status-badge{display:inline-flex;align-items:center;justify-content:center;padding:10px 13px;border-radius:999px;background:rgba(10,25,41,.06);color:var(--navy);font-size:.78rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em;border:1px solid rgba(10,25,41,.04)}.status-badge.soft{background:rgba(240,185,11,.14)}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px}.compact-stats{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}.stat{padding:24px;transition:transform .22s ease,box-shadow .22s ease,border-color .22s ease;border-radius:24px}.stat:hover{transform:translateY(-3px);box-shadow:var(--shadow-hover);border-color:rgba(240,185,11,.22)}.compact-stats .stat{padding:18px}.stat span{display:block;color:var(--muted);font-size:.86rem;text-transform:uppercase;letter-spacing:.08em;font-weight:800}.stat strong{display:block;margin-top:10px;font-size:2rem;color:var(--navy)}.compact-stats .stat strong{font-size:1.55rem}
        .panel{display:grid;gap:20px;background:linear-gradient(180deg,rgba(255,255,255,.98),rgba(246,249,255,.94));border:1px solid rgba(255,255,255,.92)}.panel[data-panel="overview"]{--panel-accent:#f0b90b}.panel[data-panel="enrollments"]{--panel-accent:#12715b}.panel[data-panel="messages"]{--panel-accent:#6a4ecb}.panel[data-panel="records"]{--panel-accent:#0a1929}.panel[data-panel="announcements"]{--panel-accent:#b58a06}.panel[data-panel="results"]{--panel-accent:#215f9a}.panel[data-panel="faculty"]{--panel-accent:#8a5a15}.panel[data-panel="settings"]{--panel-accent:#5a6678}.panel:before{background:linear-gradient(90deg,var(--panel-accent),rgba(10,25,41,.12))}.panel:after{content:"";position:absolute;right:-80px;top:40px;width:260px;height:260px;border-radius:50%;background:radial-gradient(circle,rgba(240,185,11,.08),transparent 70%);pointer-events:none}.panel-head{display:flex;justify-content:space-between;gap:14px;align-items:flex-start;margin-bottom:0;padding-bottom:16px;border-bottom:1px solid rgba(10,25,41,.08)}section.panel>.panel-head{padding:22px 22px 20px;border-radius:24px;border:1px solid rgba(10,25,41,.06);background:linear-gradient(135deg,rgba(255,255,255,.99),rgba(246,249,255,.96));box-shadow:var(--shadow-soft)}.panel-head h2{font-size:1.7rem}.panel-head p{margin:8px 0 0;color:var(--muted);max-width:900px}.workspace-divider{display:flex;align-items:center;gap:14px;color:var(--navy);font-size:.82rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.workspace-divider:before,.workspace-divider:after{content:"";flex:1;height:1px;background:linear-gradient(90deg,rgba(10,25,41,.06),rgba(240,185,11,.36),rgba(10,25,41,.06))}.workspace-divider span{display:inline-flex;align-items:center;justify-content:center;padding:8px 12px;border-radius:999px;background:rgba(255,255,255,.92);border:1px solid rgba(10,25,41,.08);box-shadow:var(--shadow-soft)}.split{display:grid;grid-template-columns:1.08fr .92fr;gap:18px}.stack{display:grid;gap:14px}.card{padding:20px;transition:transform .22s ease,box-shadow .22s ease,border-color .22s ease;border-radius:24px;background:linear-gradient(180deg,#fff,rgba(248,250,255,.96))}.card:hover{transform:translateY(-2px);box-shadow:var(--shadow-hover);border-color:rgba(240,185,11,.24)}.card h4{margin-bottom:6px}.card p{margin:0;color:var(--muted);line-height:1.72}.card small{display:block;margin-top:8px;color:var(--muted)}
        .section-kicker{display:inline-flex;align-items:center;padding:9px 14px;border-radius:999px;background:linear-gradient(135deg,rgba(240,185,11,.16),rgba(240,185,11,.08));color:var(--navy);font-size:.74rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em;border:1px solid rgba(240,185,11,.18)}.section-lead{display:grid;gap:14px;margin-bottom:2px;padding:18px 20px;border-radius:24px;background:linear-gradient(135deg,rgba(10,25,41,.035),rgba(240,185,11,.045));border:1px solid rgba(10,25,41,.06)}.quick-actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}.quick-action{display:grid;gap:9px;padding:18px 18px;border-radius:22px;border:1px solid rgba(10,25,41,.08);background:linear-gradient(180deg,#fff,rgba(248,250,255,.96));box-shadow:var(--shadow-soft);cursor:pointer;text-align:left;transition:transform .22s ease,box-shadow .22s ease,border-color .22s ease}.quick-action:hover{transform:translateY(-2px);box-shadow:var(--shadow-hover);border-color:rgba(240,185,11,.28)}.quick-action strong{display:block;color:var(--navy);font-size:1rem}.quick-action span{display:block;color:var(--muted);line-height:1.62;font-size:.9rem}.toolbar{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:16px}.toolbar>*{min-width:0}.toolbar .input{flex:1 1 240px}.toolbar-card{display:grid;gap:16px;padding:22px;border-radius:26px;background:linear-gradient(180deg,#fff,rgba(247,250,255,.98));border:1px solid rgba(10,25,41,.08);box-shadow:var(--shadow-soft)}.filter-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}.field-stack{display:grid;gap:8px}.field-stack label{font-size:.78rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.toolbar-actions{display:flex;gap:10px;flex-wrap:wrap;justify-content:flex-end}.toolbar-actions .btn{flex:0 1 auto}.selection-bar{display:flex;gap:12px;flex-wrap:wrap;align-items:center;justify-content:space-between;padding:15px 16px;border-radius:18px;background:linear-gradient(135deg,rgba(10,25,41,.04),rgba(240,185,11,.05));border:1px solid rgba(10,25,41,.08)}.selection-controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.selection-count{display:inline-flex;align-items:center;justify-content:center;padding:9px 12px;border-radius:999px;background:rgba(240,185,11,.16);color:var(--navy);font-size:.78rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase}.helper-steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.helper-step{padding:16px 16px;border-radius:20px;background:linear-gradient(180deg,#fff,rgba(248,250,255,.96));border:1px solid rgba(10,25,41,.08);box-shadow:var(--shadow-soft)}.helper-step strong{display:block;margin-bottom:6px;color:var(--navy);font-size:.92rem}.helper-step span{display:block;color:var(--muted);line-height:1.66}.workspace-grid{display:grid;grid-template-columns:minmax(0,1.08fr) minmax(350px,.92fr);gap:18px}.pane-shell{display:grid;gap:14px;padding:22px;border-radius:26px;background:linear-gradient(180deg,rgba(255,255,255,.99),rgba(247,250,255,.95));border:1px solid rgba(10,25,41,.08);box-shadow:var(--shadow-soft)}.list-pane{position:relative}.list-pane:after,.form-pane:after{content:"";position:absolute;left:22px;right:22px;top:0;height:4px;border-radius:999px;background:linear-gradient(90deg,rgba(10,25,41,.1),rgba(240,185,11,.7))}.form-pane:after{background:linear-gradient(90deg,rgba(240,185,11,.95),rgba(10,25,41,.12))}.pane-label{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;padding-bottom:14px;border-bottom:1px solid rgba(10,25,41,.08)}.pane-label p{margin:6px 0 0;color:var(--muted)}.table-wrap{overflow:auto;border:1px solid rgba(10,25,41,.08);border-radius:18px;background:#fff;box-shadow:inset 0 1px 0 rgba(255,255,255,.8)}table{width:100%;min-width:1320px;border-collapse:collapse}th,td{padding:14px;text-align:left;border-bottom:1px solid rgba(10,25,41,.08);vertical-align:top}th{position:sticky;top:0;background:rgba(10,25,41,.05);font-size:.92rem;color:var(--navy);z-index:1}tbody tr:nth-child(even){background:rgba(10,25,41,.018)}tbody tr:hover{background:rgba(240,185,11,.08)}td{font-size:.95rem}.row-actions{display:flex;gap:10px;flex-wrap:wrap}.row-actions .btn{padding:10px 14px;white-space:normal;text-align:center}
        .inline-toggle{display:flex;align-items:center;gap:12px;padding:14px 16px;border-radius:18px;border:1px solid var(--line);background:#fff;font-weight:700;box-shadow:inset 0 1px 0 rgba(255,255,255,.7)}.inline-toggle input{width:18px;height:18px;margin:0}
        .grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.option-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.choice{display:flex;align-items:center;gap:10px;padding:12px 14px;border-radius:18px;border:1px solid rgba(10,25,41,.08);background:rgba(10,25,41,.03);font-weight:700;transition:border-color .22s ease,background .22s ease,transform .22s ease}.choice:hover{border-color:rgba(240,185,11,.32);background:rgba(240,185,11,.08);transform:translateY(-1px)}.choice input{width:18px;height:18px;margin:0}.compact-choice{display:inline-flex;padding:10px 12px;border-radius:14px;font-size:.84rem}.slim-choice{margin-bottom:10px}.subjects-wrap,.group-wrap{display:grid;gap:10px}.dialog-scroll{max-height:min(82vh,920px);overflow:auto;padding-right:4px}.faculty{display:grid;grid-template-columns:82px 1fr;gap:16px;align-items:center}.avatar{width:82px;height:82px;border-radius:24px;overflow:hidden;display:grid;place-items:center;background:linear-gradient(135deg,rgba(240,185,11,.26),rgba(10,25,41,.14));font-weight:800;color:var(--navy);box-shadow:var(--shadow-soft)}.avatar img{width:100%;height:100%;object-fit:cover}.record-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.record-line{padding:12px 14px;border-radius:16px;background:rgba(10,25,41,.04);border:1px solid rgba(10,25,41,.06);min-width:0}.record-line strong{display:block;margin-bottom:4px;color:var(--navy);font-size:.82rem;text-transform:uppercase;letter-spacing:.08em}.record-line span,.record-line a{overflow-wrap:anywhere;word-break:break-word}.pending-card-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}.pending-card{padding:20px;border-radius:24px;background:linear-gradient(180deg,rgba(255,255,255,.99),rgba(246,249,253,.96));border:1px solid rgba(10,25,41,.08);box-shadow:var(--shadow-soft);display:grid;gap:14px}.pending-card h5{margin:0;font-family:Poppins,sans-serif;font-size:1.12rem;color:var(--navy)}.pending-card .row-actions .btn,.record-actions .btn{flex:1 1 150px}.class-tab-shell{display:grid;gap:14px;margin-bottom:18px;padding:18px 18px 14px;border-radius:24px;background:linear-gradient(135deg,rgba(10,25,41,.03),rgba(240,185,11,.04));border:1px solid rgba(10,25,41,.06)}.class-tab-shell h3{margin:0}.class-tab-shell p{margin:6px 0 0;color:var(--muted)}.class-tab-bar{display:flex;flex-wrap:wrap;gap:10px}.class-tab{display:inline-flex;align-items:center;gap:10px;padding:11px 14px;border-radius:18px;border:1px solid rgba(10,25,41,.08);background:rgba(255,255,255,.86);color:var(--navy);font-weight:800;cursor:pointer;transition:transform .22s ease,background .22s ease,border-color .22s ease,box-shadow .22s ease}.class-tab:hover{transform:translateY(-1px);border-color:rgba(240,185,11,.28);background:rgba(240,185,11,.08)}.class-tab.active{background:linear-gradient(135deg,var(--navy),#14324f);color:#fff;border-color:rgba(240,185,11,.24);box-shadow:var(--shadow-soft)}.class-tab-count{display:inline-flex;align-items:center;justify-content:center;min-width:28px;height:28px;padding:0 9px;border-radius:999px;background:rgba(10,25,41,.08);font-size:.76rem;font-weight:800}.class-tab.active .class-tab-count{background:rgba(255,255,255,.14)}.class-tab-mark{display:none;font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;color:rgba(255,255,255,.82)}.class-tab.active .class-tab-mark{display:inline-flex}
        .empty{padding:18px;border-radius:18px;background:linear-gradient(180deg,rgba(10,25,41,.03),rgba(10,25,41,.06));border:1px dashed rgba(10,25,41,.12);color:var(--muted)}.report-actions{margin-top:14px}.report-actions .btn{flex:1 1 220px}.crop-preview{display:grid;gap:12px;padding:16px;border-radius:20px;background:rgba(10,25,41,.04);margin-top:14px}.crop-preview img{width:150px;height:150px;border-radius:22px;object-fit:cover;border:1px solid var(--line)}body.modal-open{overflow:hidden}.crop-modal,.admin-confirm-modal{position:fixed;inset:0;z-index:120;background:rgba(10,25,41,.72);display:grid;place-items:center;padding:20px}.crop-dialog,.admin-confirm-dialog{width:min(960px,100%);max-height:calc(100vh - 40px);overflow:auto;background:#fff;border-radius:28px;padding:24px;box-shadow:var(--shadow)}.crop-layout{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:20px;align-items:start}.crop-stage{padding:18px;border-radius:24px;background:rgba(10,25,41,.04);display:grid;place-items:center}.crop-stage canvas{width:min(100%,420px);aspect-ratio:1/1;border-radius:24px;border:1px solid var(--line);background:#fff;box-shadow:0 18px 36px rgba(10,25,41,.1)}.crop-controls{display:grid;gap:14px}.note-box{padding:14px;border-radius:16px;background:rgba(240,185,11,.12);color:var(--navy);font-weight:600;line-height:1.7;border:1px solid rgba(240,185,11,.2)}.toast{position:fixed;right:24px;bottom:24px;padding:14px 16px;border-radius:16px;color:#fff;background:var(--navy);box-shadow:var(--shadow);opacity:0;pointer-events:none;transform:translateY(14px);transition:opacity .2s ease,transform .2s ease;max-width:min(92vw,420px)}.toast.show{opacity:1;transform:translateY(0)}.toast.success{background:var(--success)}.toast.error{background:var(--danger)}
        .admin-confirm-modal.hidden{display:none!important}.admin-confirm-dialog{width:min(520px,100%);display:grid;gap:16px;background:linear-gradient(180deg,#fffdf8,#fff9ee);border:1px solid rgba(240,185,11,.24)}.admin-confirm-mark{width:68px;height:68px;border-radius:22px;display:grid;place-items:center;background:linear-gradient(135deg,rgba(240,185,11,.2),rgba(10,25,41,.08));color:var(--navy);font-family:Poppins,sans-serif;font-size:1.4rem;font-weight:800;box-shadow:var(--shadow-soft)}.admin-confirm-actions{display:flex;gap:12px;flex-wrap:wrap}.admin-confirm-actions .btn{flex:1 1 180px}
        body.theme-dark-admin{color:#edf4ff;background:radial-gradient(circle at top left,rgba(240,185,11,.1),transparent 28%),radial-gradient(circle at top right,rgba(31,79,136,.16),transparent 24%),radial-gradient(circle at bottom left,rgba(13,29,47,.36),transparent 28%),linear-gradient(180deg,#07111c 0,#0b1624 50%,#101d2d 100%)}
        body.theme-dark-admin .glass,body.theme-dark-admin .panel,body.theme-dark-admin .card,body.theme-dark-admin .stat,body.theme-dark-admin .topbar,body.theme-dark-admin .toolbar-card,body.theme-dark-admin .pane-shell,body.theme-dark-admin .manage-card,body.theme-dark-admin .timeline-card,body.theme-dark-admin .history-item,body.theme-dark-admin .settings-block,body.theme-dark-admin .section-lead,body.theme-dark-admin .selection-bar,body.theme-dark-admin .class-tab-shell,body.theme-dark-admin .pending-card,body.theme-dark-admin .record-line,body.theme-dark-admin .inline-toggle,body.theme-dark-admin .choice,body.theme-dark-admin .table-wrap,body.theme-dark-admin .crop-dialog,body.theme-dark-admin .admin-confirm-dialog{background:linear-gradient(180deg,rgba(13,24,38,.98),rgba(17,30,47,.96));border-color:rgba(201,221,247,.12);box-shadow:0 28px 72px rgba(0,0,0,.32)}
        body.theme-dark-admin .topbar,body.theme-dark-admin section.panel>.panel-head{background:linear-gradient(135deg,rgba(13,24,38,.99),rgba(20,34,52,.97));border-color:rgba(201,221,247,.12)}
        body.theme-dark-admin .list-pane,body.theme-dark-admin .form-pane,body.theme-dark-admin .panel{background:linear-gradient(180deg,rgba(10,20,32,.99),rgba(16,29,45,.96))}
        body.theme-dark-admin h1,body.theme-dark-admin h2,body.theme-dark-admin h3,body.theme-dark-admin h4,body.theme-dark-admin .field label,body.theme-dark-admin .record-line strong,body.theme-dark-admin .manage-card-copy h4,body.theme-dark-admin .history-item strong,body.theme-dark-admin .settings-block h4,body.theme-dark-admin .section-kicker,body.theme-dark-admin .workspace-divider,body.theme-dark-admin .workspace-divider span,body.theme-dark-admin .panel-tag,body.theme-dark-admin .class-tab,body.theme-dark-admin .manage-chip,body.theme-dark-admin .sidebar-stat strong,body.theme-dark-admin .quick-action strong,body.theme-dark-admin .helper-step strong,body.theme-dark-admin .status-badge,body.theme-dark-admin .eyebrow,body.theme-dark-admin .selection-count,body.theme-dark-admin .stat strong,body.theme-dark-admin .field-stack label,body.theme-dark-admin .inline-toggle,body.theme-dark-admin .note-box{color:#edf4ff}
        body.theme-dark-admin .copy p,body.theme-dark-admin .muted,body.theme-dark-admin .panel-head p,body.theme-dark-admin .pane-label p,body.theme-dark-admin .manage-card-copy p,body.theme-dark-admin .history-item p,body.theme-dark-admin .settings-section-head p,body.theme-dark-admin .settings-block p,body.theme-dark-admin .record-line span,body.theme-dark-admin td,body.theme-dark-admin th,body.theme-dark-admin .timeline-card p,body.theme-dark-admin .helper-step span,body.theme-dark-admin .sidebar-stat span,body.theme-dark-admin .manage-note,body.theme-dark-admin small,body.theme-dark-admin .quick-action span,body.theme-dark-admin .card p,body.theme-dark-admin .stat span,body.theme-dark-admin .class-tab-shell p,body.theme-dark-admin .section-lead p{color:#a9bdd5}
        body.theme-dark-admin .input,body.theme-dark-admin textarea,body.theme-dark-admin select{background:#0f1c2d;color:#edf4ff;border-color:rgba(201,221,247,.16);box-shadow:inset 0 1px 0 rgba(255,255,255,.02)}
        body.theme-dark-admin .input:focus,body.theme-dark-admin textarea:focus,body.theme-dark-admin select:focus{border-color:rgba(240,185,11,.76);box-shadow:0 0 0 4px rgba(240,185,11,.12)}
        body.theme-dark-admin tbody tr:nth-child(even){background:rgba(255,255,255,.02)}body.theme-dark-admin tbody tr:hover{background:rgba(240,185,11,.08)}body.theme-dark-admin th{background:rgba(255,255,255,.04)}
        body.theme-dark-admin .btn-soft{background:rgba(201,221,247,.08);color:#edf4ff;border:1px solid rgba(201,221,247,.12)}body.theme-dark-admin .btn-secondary{background:linear-gradient(135deg,#173250,#24456a)}body.theme-dark-admin .btn-danger{background:rgba(180,35,24,.18);color:#ffd3cd;border-color:rgba(255,145,124,.14)}
        body.theme-dark-admin .manage-chip.gold{background:rgba(240,185,11,.18);color:#fff3cb}body.theme-dark-admin .manage-chip.navy{background:rgba(201,221,247,.08);color:#d9e7f7}body.theme-dark-admin .manage-chip.success{background:rgba(18,113,91,.22);color:#c9f1e4}body.theme-dark-admin .manage-chip.draft{background:rgba(133,151,176,.18);color:#cfdae6}
        body.theme-dark-admin .empty{background:linear-gradient(180deg,rgba(255,255,255,.02),rgba(255,255,255,.04));border-color:rgba(201,221,247,.14);color:#a9bdd5}
        body.theme-dark-admin .note-box,body.theme-dark-admin .manage-note{background:linear-gradient(135deg,rgba(240,185,11,.14),rgba(201,221,247,.06));color:#edf4ff;border-color:rgba(240,185,11,.16)}
        body.theme-dark-admin .toast{background:#173250}body.theme-dark-admin .toast.success{background:#12715b}body.theme-dark-admin .toast.error{background:#8f2318}
        body.theme-dark-admin .admin-confirm-dialog{background:linear-gradient(180deg,rgba(22,34,51,.99),rgba(15,26,40,.97));border-color:rgba(240,185,11,.18)}body.theme-dark-admin .admin-confirm-mark{background:linear-gradient(135deg,rgba(240,185,11,.16),rgba(201,221,247,.08));color:#fff3cb}
        body.theme-dark-admin .panel-tag,body.theme-dark-admin .eyebrow,body.theme-dark-admin .status-badge.soft,body.theme-dark-admin .selection-count{background:rgba(240,185,11,.16);border-color:rgba(240,185,11,.2);color:#fff3cb}
        body.theme-dark-admin .status-badge{background:rgba(201,221,247,.08);border-color:rgba(201,221,247,.12);color:#edf4ff}
        body.theme-dark-admin .workspace-divider:before,body.theme-dark-admin .workspace-divider:after{background:linear-gradient(90deg,rgba(201,221,247,.12),rgba(240,185,11,.4),rgba(201,221,247,.12))}
        body.theme-dark-admin .class-tab-count{background:rgba(201,221,247,.12);color:#edf4ff}
        body.theme-dark-admin .record-line a,body.theme-dark-admin a:not(.btn){color:#d9e7f7}
        body.theme-dark-admin code{color:#fff3cb;background:rgba(255,255,255,.05);padding:2px 6px;border-radius:8px}
        .admin-brand{display:grid;grid-template-columns:64px minmax(0,1fr);gap:14px;align-items:center;position:relative}.copy .admin-brand{margin-bottom:4px}.sidebar .admin-brand{padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,.08)}.sidebar .admin-brand h2{color:#fff;font-size:1.55rem}.admin-mark{display:grid;place-items:center;width:64px;height:64px;border-radius:20px;background:linear-gradient(135deg,#102640,#08111b);border:1px solid rgba(240,185,11,.24);box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 14px 28px rgba(0,0,0,.18);color:var(--gold);font-family:Poppins,sans-serif;font-size:1.02rem;font-weight:800;letter-spacing:.08em}.sidebar-summary{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;position:relative}.sidebar-stat{padding:12px 14px;border-radius:18px;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.08);box-shadow:inset 0 1px 0 rgba(255,255,255,.05)}.sidebar-stat strong{display:block;font-family:Poppins,sans-serif;font-size:1.3rem;color:#fff}.sidebar-stat span{display:block;margin-top:4px;color:rgba(255,255,255,.72);font-size:.72rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.sidebar-note{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.08);color:rgba(255,255,255,.84);font-weight:600;line-height:1.68}.panel-tag{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;background:rgba(240,185,11,.12);color:var(--navy);font-size:.74rem;font-weight:800;text-transform:uppercase;letter-spacing:.08em}.list-pane,.form-pane{background:linear-gradient(180deg,rgba(255,255,255,.99),rgba(247,250,255,.95))}.form-pane form{display:grid}.manage-card{padding:18px;border-radius:24px;background:linear-gradient(180deg,#fff,rgba(247,250,255,.96));border:1px solid rgba(10,25,41,.08);box-shadow:var(--shadow-soft);display:grid;gap:14px}.manage-card-header{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.manage-card-copy h4{margin:0}.manage-card-copy p{margin:6px 0 0}.manage-chip-row{display:flex;gap:8px;flex-wrap:wrap}.manage-chip{display:inline-flex;align-items:center;justify-content:center;padding:8px 11px;border-radius:999px;background:rgba(10,25,41,.05);color:var(--navy);font-size:.76rem;font-weight:800;letter-spacing:.05em;text-transform:uppercase}.manage-chip.gold{background:rgba(240,185,11,.16)}.manage-chip.navy{background:rgba(10,25,41,.08)}.manage-chip.success{background:rgba(18,113,91,.14);color:var(--success)}.manage-chip.draft{background:rgba(93,109,130,.16);color:#516174}.manage-actions{display:flex;gap:10px;flex-wrap:wrap}.manage-actions .btn{flex:1 1 140px}.manage-note{padding:16px 18px;border-radius:20px;background:linear-gradient(135deg,rgba(240,185,11,.1),rgba(10,25,41,.03));border:1px solid rgba(240,185,11,.18);color:var(--navy);font-weight:600;line-height:1.75}.history-list{display:grid;gap:12px}.history-item{padding:14px 16px;border-radius:18px;background:linear-gradient(180deg,#fff,rgba(248,250,255,.96));border:1px solid rgba(10,25,41,.08);box-shadow:var(--shadow-soft)}.history-item strong{display:block;color:var(--navy)}.history-item p{margin:6px 0 0;color:var(--muted);line-height:1.65}.settings-grid{display:grid;grid-template-columns:1.2fr .8fr;gap:18px;align-items:start}.settings-stack{display:grid;gap:18px}.settings-section{display:grid;gap:16px}.settings-section-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;padding-bottom:12px;border-bottom:1px solid rgba(10,25,41,.08)}.settings-section-head p{margin:6px 0 0;color:var(--muted)}.settings-block{padding:20px;border-radius:24px;background:linear-gradient(180deg,#fff,rgba(247,250,255,.96));border:1px solid rgba(10,25,41,.08);box-shadow:var(--shadow-soft);display:grid;gap:14px;scroll-margin-top:90px}.settings-block h4{margin:0}.settings-block p{margin:0;color:var(--muted);line-height:1.72}.settings-jump-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.timeline-card{padding:16px;border-radius:18px;background:linear-gradient(180deg,#fff,rgba(248,250,255,.96));border:1px solid rgba(10,25,41,.06);box-shadow:var(--shadow-soft)}.timeline-card p{margin:6px 0 0;color:var(--muted)}
        .mt-12{margin-top:12px}.mt-14{margin-top:14px}.mt-16{margin-top:16px}.mt-18{margin-top:18px}.mb-18{margin-bottom:18px}.mb-12{margin-bottom:12px}.boxed{padding:16px;border:1px solid var(--line);border-radius:18px;background:rgba(10,25,41,.02)}
        @media (max-width:1280px){.app{grid-template-columns:290px minmax(0,1fr)}}
        @media (max-width:1160px){.login,.app,.split,.stats,.settings-grid,.workspace-grid{grid-template-columns:1fr}.sidebar{position:relative;top:0}.tab-list{grid-template-columns:repeat(2,minmax(0,1fr))}.topbar{flex-direction:column;align-items:flex-start}.topbar-actions{width:100%;justify-content:flex-start}}
        @media (max-width:760px){.shell{padding:14px}.mini,.grid2,.crop-layout,.option-grid,.record-grid,.sidebar-summary,.settings-grid,.filter-grid,.workspace-grid{grid-template-columns:1fr}.copy,.login-card,.panel,.topbar,.stat,.crop-dialog,.pane-shell,.toolbar-card{padding:18px}.toolbar>*{flex:1 1 100%}.toolbar-actions{justify-content:stretch}.toolbar-actions .btn,.row-actions .btn,.report-actions .btn,.record-actions .btn,.pending-card .row-actions .btn,.manage-actions .btn{flex:1 1 100%}.tab-list{grid-template-columns:1fr}.admin-brand{grid-template-columns:52px minmax(0,1fr)}.admin-mark{width:52px;height:52px;border-radius:16px;font-size:.92rem}.pending-card-grid{grid-template-columns:1fr}.class-tab-bar{display:grid;grid-template-columns:1fr}.class-tab{justify-content:space-between;width:100%}.toast{right:14px;left:14px;bottom:14px;max-width:none}}
    </style>
</head>
<body>
    <script>
        (async () => {
            try {
                if ('serviceWorker' in navigator) {
                    const registrations = await navigator.serviceWorker.getRegistrations();
                    await Promise.all(registrations.map((registration) => registration.unregister()));
                }
                if (window.caches && caches.keys) {
                    const keys = await caches.keys();
                    await Promise.all(keys.map((key) => caches.delete(key)));
                }
            } catch (_error) {
                // Admin panel should still load even if cache cleanup fails.
            }
        })();
    </script>
    <div class="shell">
        <section id="loginView" class="login">
            <div class="glass copy">
                <div class="admin-brand">
                    <div class="admin-mark">TPA</div>
                    <div>
                        <span class="eyebrow">Admin Portal</span>
                        <h1 class="mt-14">Run the academy with clarity and control.</h1>
                    </div>
                </div>
                <p class="mt-16">Manage admissions, faculty, results, public communication, and website content from one secure administrative workspace.</p>
                <div class="mini">
                    <div class="card"><strong>Secure</strong><span class="muted">Protected admin session with manual logout</span></div>
                    <div class="card"><strong>Simple</strong><span class="muted">Single-file Flask backend and SQLite storage</span></div>
                    <div class="card"><strong>Fast</strong><span class="muted">Everything updates instantly through fetch API</span></div>
                </div>
            </div>
            <div class="glass login-card">
                <h2>Administrator Login</h2>
                <p class="muted">Use the authorised academy credentials to access the administrative control centre.</p>
                <form id="loginForm" action="__ADMIN_PATH__/login" method="post">
                    <div class="field"><label for="username">Username</label><input class="input" id="username" name="username" type="text" autocomplete="username" required></div>
                    <div class="field"><label for="password">Password</label><input class="input" id="password" name="password" type="password" autocomplete="current-password" required></div>
                    <button class="btn btn-primary mt-16" type="submit">Log In</button>
                </form>
            </div>
        </section>

        <section id="dashboardView" class="app hidden">
            <aside class="sidebar">
                <div class="admin-brand">
                    <div class="admin-mark">TPA</div>
                    <div>
                        <span class="eyebrow">The Professors Academy</span>
                        <h2 class="mt-14">Administrative Control Centre</h2>
                        <small id="adminUsername">Session Active</small>
                    </div>
                </div>
                <div id="sidebarSnapshot" class="sidebar-summary"></div>
                    <div class="sidebar-cluster">
                        <div class="sidebar-nav-title">Admissions & Operations</div>
                        <div class="tab-list">
                            <button class="tab active" data-tab="overview"><span>Executive Overview</span><small>Review key metrics, recent activity, and live public website updates</small></button>
                            <button class="tab" data-tab="enrollments"><span>Pending Admission Review</span><small>Review new applications and record admission decisions</small></button>
                            <button class="tab" data-tab="messages"><span>Website Correspondence</span><small>Review visitor enquiries and mark them resolved</small></button>
                            <button class="tab" data-tab="records"><span>Final Admission Records</span><small>Access confirmed and declined records with exports</small></button>
                        </div>
                    </div>
                <div class="sidebar-cluster">
                    <div class="sidebar-nav-title">Public Website Administration</div>
                    <div class="tab-list">
                        <button class="tab" data-tab="announcements"><span>Announcements & Marquee</span><small>Manage public notices and top-line updates</small></button>
                        <button class="tab" data-tab="results"><span>Results Library</span><small>Publish, update, or archive result documents</small></button>
                        <button class="tab" data-tab="faculty"><span>Faculty Directory</span><small>Maintain faculty profiles, photographs, and display order</small></button>
                        <button class="tab" data-tab="settings"><span>Website Settings & Public Information</span><small>Edit public content, contact details, and administrative tools</small></button>
                    </div>
                </div>
                <div class="manage-note sidebar-note"><strong>Recommended workflow:</strong> first review <strong>Pending Admission Review</strong>, then move completed cases into <strong>Final Admission Records</strong>, and use <strong>Website Settings & Public Information</strong> for public-facing updates.</div>
                <button id="logoutButton" class="btn btn-primary" type="button">Log Out</button>
            </aside>

            <div class="main">
                    <div class="glass topbar">
                        <div>
                            <span class="panel-tag">Administrative Workspace</span>
                            <h1>Academy Administrative Control Centre</h1>
                            <p class="muted">All operational records, public website controls, and communication tools are organised in clear professional sections for daily academy management.</p>
                        </div>
                        <div class="topbar-actions">
                            <span class="status-badge">Live Administrative Session</span>
                            <span class="status-badge">Integrated Workspaces</span>
                            <span class="status-badge soft">Secure Session</span>
                            <a class="btn btn-soft" href="/" target="_blank" rel="noreferrer">Open Public Website</a>
                    </div>
                </div>

                <section class="glass panel" data-panel="overview">
                    <div class="panel-head">
                        <div>
                            <h2>Executive Overview</h2>
                            <p>Review the primary operational metrics, recent administrative activity, and current public website performance from this summary workspace.</p>
                        </div>
                    </div>
                    <div class="section-lead">
                        <span class="section-kicker">Executive Summary</span>
                        <div class="manage-note">Begin with this summary to review current activity, then continue to the relevant workspace for admissions, faculty, website content, or communication tasks.</div>
                    </div>
                    <div class="quick-actions mb-18">
                        <button class="quick-action" type="button" data-open-tab="enrollments"><strong>Review Pending Applications</strong><span>Open newly submitted student applications and record admission decisions.</span></button>
                        <button class="quick-action" type="button" data-open-tab="messages"><strong>Review Website Correspondence</strong><span>Open visitor enquiries and mark them resolved after follow-up.</span></button>
                        <button class="quick-action" type="button" data-open-tab="records"><strong>Open Final Admission Records</strong><span>Access confirmed and declined records with exports and downloads.</span></button>
                        <button class="quick-action" type="button" data-open-tab="announcements"><strong>Manage Announcements</strong><span>Update public notices and the homepage marquee line.</span></button>
                        <button class="quick-action" type="button" data-open-tab="results"><strong>Maintain Results Library</strong><span>Publish or replace academic result documents for the public website.</span></button>
                        <button class="quick-action" type="button" data-open-tab="faculty"><strong>Update Faculty Directory</strong><span>Maintain faculty profiles, photographs, and display order.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="contactAccessBlock"><strong>Edit Public Website Settings</strong><span>Update contact details, homepage content, and public configuration.</span></button>
                    </div>
                    <div class="workspace-divider"><span>Main Numbers</span></div>
                    <div id="statsGrid" class="stats"></div>
                    <div class="workspace-divider"><span>Downloads & Reports</span></div>
                    <div class="card mt-18">
                        <div class="panel-head">
                            <div>
                            <h3>Admission Reports</h3>
                            <p>Download formal admission reports for the last 7 days, last 30 days, or the complete record history.</p>
                            </div>
                        </div>
                        <div class="row-actions report-actions">
                            <button class="btn btn-secondary report-download" type="button" data-range="7">Download 7-Day Report</button>
                            <button class="btn btn-secondary report-download" type="button" data-range="30">Download 30-Day Report</button>
                            <button class="btn btn-secondary report-download" type="button" data-range="all">Download Full Report</button>
                            <button id="downloadConfirmedForms" class="btn btn-soft" type="button">Download All Confirmed Forms</button>
                        </div>
                    </div>
                    <div class="workspace-divider"><span>Live Website Monitoring</span></div>
                    <div class="split mt-18">
                        <div class="card">
                            <div class="panel-head">
                                <div>
                                    <h3>Recent Announcements</h3>
                                    <p>Latest notices that are showing now or saved for later.</p>
                                </div>
                            </div>
                            <div id="recentAnnouncements" class="stack"></div>
                        </div>
                        <div class="card">
                            <div class="panel-head">
                                <div>
                            <h3>Website Insights</h3>
                            <p>Review the last 7 days of visitor activity together with the current live visitor count from the public website.</p>
                                </div>
                            </div>
                            <div id="insightsGrid" class="stats compact-stats"></div>
                            <div id="insightsTimeline" class="stack mt-14"></div>
                            <div id="insightsSectionList" class="stack mt-14"></div>
                        </div>
                    </div>
                    <div class="workspace-divider"><span>Office Activity</span></div>
                    <div class="card mt-18">
                        <div class="panel-head">
                            <div>
                            <h3>Recent Administrative Activity</h3>
                            <p>Review the latest confirmations, edits, uploads, deletions, and backup actions recorded by the system.</p>
                            </div>
                        </div>
                        <div id="activityLogList" class="history-list"></div>
                    </div>
                    <div class="workspace-divider"><span>Daily Reminders</span></div>
                    <div class="card mt-18">
                        <div class="panel-head">
                            <div>
                            <h3>Operational Guidance</h3>
                            <p>Important reminders to keep admissions, records, and public updates accurate and consistent.</p>
                            </div>
                        </div>
                        <div class="stack">
                            <div class="manage-note">Use Confirm or Reject so the saved student records and reports always stay correct.</div>
                            <div class="manage-note">Download 7-day, 30-day, and all-time reports directly from the dashboard whenever you need a quick admission summary.</div>
                            <div class="manage-note">Student passport photos must be JPG or PNG and 300 KB or smaller.</div>
                            <div class="manage-note">Search indexing helpers are now available through <code>/robots.txt</code> and <code>/sitemap.xml</code> for better discoverability.</div>
                            <div class="manage-note">Result uploads accept PDF files only. Keep the publish toggle on if you want them to appear on the public site immediately.</div>
                            <div class="manage-note">Use the faculty up and down controls to reorder the public faculty section.</div>
                        </div>
                    </div>
                </section>

                <section class="glass panel hidden" data-panel="enrollments">
                    <div class="panel-head">
                        <div>
                            <h2>Pending Admission Review</h2>
                            <p>Review newly submitted applications here, then confirm, decline, edit, delete, or export them from one controlled workspace.</p>
                        </div>
                    </div>
                    <div class="section-lead">
                        <span class="section-kicker">Review Workflow</span>
                        <div class="manage-note">Use the filters to narrow the list, select the required applications, and then complete the relevant admission action from the controls below.</div>
                    </div>
                    <div class="helper-steps mb-18">
                        <div class="helper-step"><strong>1. Filter</strong><span>Locate applications by student name, roll number, CNIC, class, or date range.</span></div>
                        <div class="helper-step"><strong>2. Select</strong><span>Select one or multiple applications from the filtered view.</span></div>
                        <div class="helper-step"><strong>3. Process</strong><span>Confirm, decline, delete, or export the selected applications from a single action bar.</span></div>
                    </div>
                    <div class="workspace-divider"><span>Search, Filter, and Bulk Actions</span></div>
                    <div class="card toolbar-card">
                        <div class="class-tab-shell">
                            <div>
                                <h3>All Enrollments</h3>
                                <p>Click one class tab below. Only that class section will show in the list.</p>
                            </div>
                            <div id="enrollmentClassTabs" class="class-tab-bar"></div>
                        </div>
                        <div class="filter-grid">
                            <div class="field-stack">
                                <label for="enrollmentNameSearch">Student Name</label>
                                <input id="enrollmentNameSearch" class="input" type="search" placeholder="Faiz Hussain">
                            </div>
                            <div class="field-stack">
                                <label for="enrollmentRollSearch">Roll No</label>
                                <input id="enrollmentRollSearch" class="input" type="search" placeholder="2k26/IX/001">
                            </div>
                            <div class="field-stack">
                                <label for="enrollmentCnicSearch">CNIC No</label>
                                <input id="enrollmentCnicSearch" class="input" type="search" placeholder="12345-1234567-1">
                            </div>
                            <div class="field-stack">
                                <label for="enrollmentSearch">Quick Search</label>
                                <input id="enrollmentSearch" class="input" type="search" placeholder="Search any detail like father name, email, mobile, or subjects">
                            </div>
                            <div class="field-stack">
                                <label for="enrollmentClassFilter">Class Section</label>
                                <select id="enrollmentClassFilter" class="input">
                                    <option value="">All Pending Classes</option>
                                </select>
                            </div>
                            <div class="field-stack">
                                <label for="enrollmentFrom">From Date & Time</label>
                                <input id="enrollmentFrom" class="input" type="datetime-local" aria-label="Pending admissions from date and time">
                            </div>
                            <div class="field-stack">
                                <label for="enrollmentTo">To Date & Time</label>
                                <input id="enrollmentTo" class="input" type="datetime-local" aria-label="Pending admissions to date and time">
                            </div>
                            <div class="field-stack">
                                <label for="enrollmentSort">Record Order</label>
                                <select id="enrollmentSort" class="input">
                                    <option value="new_to_old">New to Old</option>
                                    <option value="old_to_new">Old to New</option>
                                </select>
                            </div>
                        </div>
                        <div class="selection-bar">
                            <div class="selection-controls">
                                <label class="choice compact-choice" for="selectAllPending">
                                    <input id="selectAllPending" type="checkbox">
                                    <span>Select all students shown right now</span>
                                </label>
                                <span id="pendingSelectionCount" class="selection-count">0 selected</span>
                            </div>
                            <div class="toolbar-actions">
                                <button id="bulkConfirmPending" class="btn btn-primary" type="button">Confirm Selected</button>
                                <button id="bulkRejectPending" class="btn btn-soft" type="button">Mark Rejected</button>
                                <button id="bulkDeletePending" class="btn btn-danger" type="button">Delete Selected</button>
                                <button id="exportSelectedPending" class="btn btn-secondary" type="button">Download Selected List</button>
                            </div>
                        </div>
                        <div class="toolbar-actions">
                            <button id="clearPendingFilters" class="btn btn-soft" type="button">Reset Search</button>
                            <button id="exportEnrollments" class="btn btn-secondary" type="button">Download All Pending List</button>
                        </div>
                        <div class="workspace-divider"><span>Pending Student Cards</span></div>
                        <div id="enrollmentClassSections" class="stack"></div>
                    </div>
                </section>
                <section class="glass panel hidden" data-panel="messages">
                    <div class="panel-head">
                        <div>
                            <h2>Website Messages</h2>
                            <p>Read the messages visitors send from the public website, then mark them handled or delete them when they are no longer needed.</p>
                        </div>
                    </div>
                    <div class="helper-steps mb-18">
                        <div class="helper-step"><strong>1. Open a message</strong><span>Read the visitor name, email, mobile number, and full message first.</span></div>
                        <div class="helper-step"><strong>2. Follow up</strong><span>Call, email, or reply from your office process after checking the message.</span></div>
                        <div class="helper-step"><strong>3. Mark handled</strong><span>Once reviewed, mark the message handled so your inbox stays clean.</span></div>
                    </div>
                    <div class="workspace-divider"><span>Search and Review Inbox</span></div>
                    <div class="card toolbar-card">
                        <div class="filter-grid">
                            <div class="field-stack">
                                <label for="messageSearch">Search Message Inbox</label>
                                <input id="messageSearch" class="input" type="search" placeholder="Search by name, email, mobile, or message text">
                            </div>
                            <div class="field-stack">
                                <label for="messageStatusFilter">Message Status</label>
                                <select id="messageStatusFilter" class="input">
                                    <option value="">All Messages</option>
                                    <option value="unread">Unread Only</option>
                                    <option value="read">Handled Only</option>
                                </select>
                            </div>
                        </div>
                        <div class="selection-bar">
                            <div class="selection-controls">
                                <span id="messageSummaryCount" class="selection-count">0 messages</span>
                            </div>
                            <div class="toolbar-actions">
                                <button id="clearMessageFilters" class="btn btn-soft" type="button">Reset Search</button>
                            </div>
                        </div>
                        <div class="workspace-divider"><span>Visitor Message Inbox</span></div>
                        <div id="messageList" class="stack"></div>
                    </div>
                </section>
""",
]

ADMIN_HTML_PARTS.append(
    r"""
                <section class="glass panel hidden" data-panel="announcements">
                    <div class="panel-head">
                        <div>
                            <h2>Announcements & Marquee</h2>
                            <p>Create, update, and publish website announcements while also controlling the public marquee line from the same workspace.</p>
                        </div>
                    </div>
                    <div class="helper-steps mb-18">
                        <div class="helper-step"><strong>1. Select a notice</strong><span>Review both published and hidden notices from the library panel.</span></div>
                        <div class="helper-step"><strong>2. Edit the content</strong><span>Update the title, date, description, and public visibility settings.</span></div>
                        <div class="helper-step"><strong>3. Publish changes</strong><span>Save the record and the public website will reflect the update shortly.</span></div>
                    </div>
                    <div class="card mb-18">
                        <h3>Public Marquee Line</h3>
                        <p class="muted">Edit the moving text line that appears near the top of the public website.</p>
                        <form id="marqueeForm">
                            <div class="field">
                                <label class="inline-toggle" for="marqueeEnabled">
                                    <input id="marqueeEnabled" type="checkbox">
                                        <span>Show This Moving Line On Website</span>
                                </label>
                            </div>
                            <div class="field">
                                <label for="marqueeText">Moving Line Text</label>
                                <textarea id="marqueeText" placeholder="Admissions are open from ... to ..."></textarea>
                                <small class="muted">Update this text and click save to change the top moving line on the public website.</small>
                            </div>
                            <button class="btn btn-primary mt-16" type="submit">Save Moving Line</button>
                        </form>
                    </div>
                    <div class="workspace-divider"><span>Notice Library and Editor</span></div>
                    <div class="workspace-grid">
                        <div class="card list-pane pane-shell">
                            <div class="pane-label">
                                <div>
                                    <h3>Saved Notices</h3>
                                    <p>Review notices that are showing now or hidden for later.</p>
                                </div>
                                <span class="manage-chip">Library</span>
                            </div>
                            <div id="announcementList" class="stack"></div>
                        </div>
                        <div class="card form-pane pane-shell">
                            <div class="pane-label">
                                <div>
                                    <h3 id="announcementFormTitle">Create Announcement</h3>
                                    <p>Create a new announcement or update the selected record from this editor.</p>
                                </div>
                                <span class="manage-chip gold">Editor</span>
                            </div>
                            <form id="announcementForm">
                                <input id="announcementId" type="hidden">
                                <div class="field"><label for="announcementTitle">Title</label><input class="input" id="announcementTitle" type="text" required></div>
                                <div class="field"><label for="announcementDate">Date</label><input class="input" id="announcementDate" type="date"></div>
                                <div class="field">
                                    <label class="inline-toggle" for="announcementIsNew">
                                        <input id="announcementIsNew" type="checkbox">
                                        <span>Show "New" badge on public website</span>
                                    </label>
                                </div>
                                <div class="field">
                                    <label class="inline-toggle" for="announcementIsPublished">
                                        <input id="announcementIsPublished" type="checkbox" checked>
                                        <span>Show this notice on the website now</span>
                                    </label>
                                </div>
                                <div class="field"><label for="announcementDescription">Description</label><textarea id="announcementDescription" required></textarea></div>
                                <div class="row-actions mt-16">
                                    <button class="btn btn-primary" type="submit">Save Notice</button>
                                    <button class="btn btn-soft" id="announcementReset" type="button">Clear Form</button>
                                </div>
                            </form>
                        </div>
                    </div>
                </section>

                <section class="glass panel hidden" data-panel="results">
                    <div class="panel-head">
                        <div>
                            <h2>Results Library</h2>
                            <p>Upload, edit, replace, and organise academic result documents for the public website.</p>
                        </div>
                    </div>
                    <div class="helper-steps mb-18">
                        <div class="helper-step"><strong>1. Review files</strong><span>Check the current result documents in the library panel.</span></div>
                        <div class="helper-step"><strong>2. Update the record</strong><span>Enter the title, class, year, and select a PDF file if required.</span></div>
                        <div class="helper-step"><strong>3. Publish the result</strong><span>The document will appear publicly when the visibility toggle is enabled.</span></div>
                    </div>
                    <div class="workspace-divider"><span>Results Library and Upload Panel</span></div>
                    <div class="workspace-grid">
                        <div class="card list-pane pane-shell">
                            <div class="pane-label">
                                <div>
                                    <h3>Saved Result Files</h3>
                                    <p>See current result files here before replacing or editing any public file.</p>
                                </div>
                                <span class="manage-chip">Library</span>
                            </div>
                            <div id="resultList" class="stack"></div>
                        </div>
                        <div class="card form-pane pane-shell">
                            <div class="pane-label">
                                <div>
                                    <h3 id="resultFormTitle">Create Result Record</h3>
                                    <p>Use this form to add a new result document or replace the selected PDF file cleanly.</p>
                                </div>
                                <span class="manage-chip gold">Upload Desk</span>
                            </div>
                            <form id="resultForm" enctype="multipart/form-data">
                                <input id="resultId" name="id" type="hidden">
                                <div class="field"><label for="resultTitle">Title</label><input class="input" id="resultTitle" name="title" type="text" required></div>
                                <div class="grid2">
                                    <div class="field"><label for="resultClass">Class</label><input class="input" id="resultClass" name="class" type="text" placeholder="Class X" required></div>
                                    <div class="field"><label for="resultYear">Year</label><input class="input" id="resultYear" name="year" type="text" placeholder="2026" required></div>
                                </div>
                                <div class="field">
                                    <label class="inline-toggle" for="resultIsNew">
                                        <input id="resultIsNew" name="is_new" type="checkbox" value="1">
                                        <span>Show "New" badge on public website</span>
                                    </label>
                                </div>
                                <div class="field">
                                    <label class="inline-toggle" for="resultIsPublished">
                                        <input id="resultIsPublished" name="is_published" type="checkbox" value="1" checked>
                                        <span>Show this result on the website now</span>
                                    </label>
                                </div>
                                <div class="field">
                                    <label for="resultPdf">PDF File</label>
                                    <input class="input" id="resultPdf" name="pdf" type="file" accept="application/pdf">
                                    <small class="muted">When editing, leave this empty to keep the current PDF file.</small>
                                </div>
                                <div class="row-actions mt-16">
                                    <button class="btn btn-primary" type="submit">Save Result</button>
                                    <button class="btn btn-soft" id="resultReset" type="button">Clear Form</button>
                                </div>
                            </form>
                        </div>
                    </div>
                </section>

                <section class="glass panel hidden" data-panel="faculty">
                    <div class="panel-head">
                        <div>
                            <h2>Faculty Directory</h2>
                            <p>Maintain faculty profiles, photographs, subject details, and the public display order from this directory workspace.</p>
                        </div>
                    </div>
                    <div class="helper-steps mb-18">
                        <div class="helper-step"><strong>1. Review faculty</strong><span>Check the current faculty profiles in the directory panel first.</span></div>
                        <div class="helper-step"><strong>2. Update profile details</strong><span>Use the editor to set classes, subject, qualification, experience, and photograph.</span></div>
                        <div class="helper-step"><strong>3. Save the profile</strong><span>Once saved, the public faculty section updates automatically.</span></div>
                    </div>
                    <div class="workspace-divider"><span>Faculty Directory and Editor</span></div>
                    <div class="workspace-grid">
                        <div class="card list-pane pane-shell">
                            <div class="pane-label">
                                <div>
                                    <h3>Saved Faculty Profiles</h3>
                                    <p>Review existing faculty cards here before changing display order, photographs, or teaching assignments.</p>
                                </div>
                                <span class="manage-chip">Directory</span>
                            </div>
                            <div id="facultyList" class="stack"></div>
                        </div>
                        <div class="card form-pane pane-shell">
                            <div class="pane-label">
                                <div>
                                    <h3 id="facultyFormTitle">Create Faculty Profile</h3>
                                    <p>Create a new faculty profile or update the selected record from this form panel.</p>
                                </div>
                                <span class="manage-chip gold">Faculty Editor</span>
                            </div>
                            <form id="facultyForm" enctype="multipart/form-data">
                                <input id="facultyId" name="id" type="hidden">
                                <input id="facultyClass" name="class_assigned" type="hidden">
                                <div class="field"><label for="facultyName">Name</label><input class="input" id="facultyName" name="name" type="text" required></div>
                                <div class="field">
                                    <label>Faculty Teaching Sections</label>
                                    <div id="facultySectionOptions" class="option-grid">
                                        <label class="choice compact-choice slim-choice"><input type="checkbox" data-faculty-section value="Class IX-X"><span>Class IX-X</span></label>
                                        <label class="choice compact-choice slim-choice"><input type="checkbox" data-faculty-section value="XI-XII | Pre-Med"><span>XI-XII Pre-Med</span></label>
                                        <label class="choice compact-choice slim-choice"><input type="checkbox" data-faculty-section value="XI-XII | Pre-Eng"><span>XI-XII Pre-Eng</span></label>
                                        <label class="choice compact-choice slim-choice"><input type="checkbox" data-faculty-section value="MDCAT"><span>MDCAT</span></label>
                                        <label class="choice compact-choice slim-choice"><input type="checkbox" data-faculty-section value="ECAT"><span>ECAT</span></label>
                                    </div>
                                    <small class="muted">You can assign the same faculty member to multiple sections, so duplicate records are not required.</small>
                                </div>
                                <div class="grid2">
                                    <div class="field"><label for="facultySubject">Subject</label><input class="input" id="facultySubject" name="subject" type="text" placeholder="Biology" required></div>
                                    <div class="field"><label for="facultyQualification">Qualification</label><input class="input" id="facultyQualification" name="qualification" type="text" required></div>
                                </div>
                                <div class="field"><label for="facultyExperienceYears">Experience</label><input class="input" id="facultyExperienceYears" name="experience_years" type="text" placeholder="5 Years+" required></div>
                                <div class="field">
                                    <label for="facultyPhoto">Photo</label>
                                    <input class="input" id="facultyPhoto" name="photo" type="file" accept="image/png,image/jpeg">
                                    <small class="muted">After selecting an image, you can crop it before saving the faculty member. While editing, you can also remove the old picture.</small>
                                </div>
                                <div id="facultyCurrentPhotoWrap" class="crop-preview hidden">
                                    <div>
                                        <h4 class="mb-12">Current Teacher Photo</h4>
                                        <p>Keep this picture, replace it with a new one, or remove the old one before saving.</p>
                                    </div>
                                    <img id="facultyCurrentPhoto" alt="Current teacher photo">
                                    <label class="choice compact-choice slim-choice">
                                        <input id="facultyRemovePhoto" name="remove_photo" type="checkbox" value="1">
                                        <span>Remove old picture when saving</span>
                                    </label>
                                </div>
                                <div id="facultyPhotoPreviewWrap" class="crop-preview hidden">
                                    <div>
                                        <h4 class="mb-12">Faculty Photo Preview</h4>
                                        <p>Use the crop option to center the image before upload.</p>
                                    </div>
                                    <img id="facultyPhotoPreview" alt="Faculty photo preview">
                                    <div class="row-actions">
                                        <button id="recropFacultyPhoto" class="btn btn-soft" type="button">Crop Again</button>
                                    </div>
                                </div>
                                <div class="row-actions mt-16">
                                        <button class="btn btn-primary" type="submit">Save Faculty Profile</button>
                                        <button class="btn btn-soft" id="facultyReset" type="button">Clear Form</button>
                                </div>
                            </form>
                        </div>
                    </div>
                </section>

                <section class="glass panel hidden" data-panel="records">
                    <div class="panel-head">
                        <div>
                            <h2>Final Admission Records</h2>
                            <p>Review confirmed and declined student records by class, search them, and download the required lists whenever needed.</p>
                        </div>
                    </div>
                    <div class="section-lead">
                        <span class="section-kicker">Archived Admission Records</span>
                        <div class="manage-note">Use this area only for applications that have already been confirmed or declined. Search by name, roll number, CNIC, class, status, or a custom time range.</div>
                    </div>
                    <div class="helper-steps mb-18">
                        <div class="helper-step"><strong>1. Filter</strong><span>Choose the required class, status, or reporting period.</span></div>
                        <div class="helper-step"><strong>2. Review</strong><span>Open each class section to review confirmed and declined totals.</span></div>
                        <div class="helper-step"><strong>3. Export</strong><span>Download class-wise lists, selected records, or confirmed forms.</span></div>
                    </div>
                    <div class="workspace-divider"><span>Search and Status Filters</span></div>
                    <div class="card toolbar-card mb-18">
                        <div class="class-tab-shell">
                            <div>
                                <h3>All Final Records</h3>
                                <p>Click one class tab below. Only that class record section will show in the list.</p>
                            </div>
                            <div id="admissionRecordsClassTabs" class="class-tab-bar"></div>
                        </div>
                        <div class="filter-grid">
                            <div class="field-stack">
                                <label for="admissionRecordsNameSearch">Student Name</label>
                                <input id="admissionRecordsNameSearch" class="input" type="search" placeholder="Faiz Hussain">
                            </div>
                            <div class="field-stack">
                                <label for="admissionRecordsRollSearch">Roll No</label>
                                <input id="admissionRecordsRollSearch" class="input" type="search" placeholder="2k26/XI/P.M/001">
                            </div>
                            <div class="field-stack">
                                <label for="admissionRecordsCnicSearch">CNIC No</label>
                                <input id="admissionRecordsCnicSearch" class="input" type="search" placeholder="12345-1234567-1">
                            </div>
                            <div class="field-stack">
                                <label for="admissionRecordsSearch">Quick Search</label>
                                <input id="admissionRecordsSearch" class="input" type="search" placeholder="Search any extra detail like father name, email, mobile, or subjects">
                            </div>
                            <div class="field-stack">
                                <label for="admissionRecordsClassFilter">Class Section</label>
                                <select id="admissionRecordsClassFilter" class="input">
                                    <option value="">All Class Sections</option>
                                </select>
                            </div>
                            <div class="field-stack">
                                <label for="admissionRecordsStatusFilter">Status</label>
                                <select id="admissionRecordsStatusFilter" class="input">
                                        <option value="">Show All</option>
                                        <option value="confirmed">Confirmed Only</option>
                                        <option value="rejected">Rejected Only</option>
                                </select>
                            </div>
                            <div class="field-stack">
                                <label for="admissionRecordsFrom">From Date & Time</label>
                                <input id="admissionRecordsFrom" class="input" type="datetime-local" aria-label="Admission records from date and time">
                            </div>
                            <div class="field-stack">
                                <label for="admissionRecordsTo">To Date & Time</label>
                                <input id="admissionRecordsTo" class="input" type="datetime-local" aria-label="Admission records to date and time">
                            </div>
                            <div class="field-stack">
                                <label for="admissionRecordsSort">Record Order</label>
                                <select id="admissionRecordsSort" class="input">
                                    <option value="new_to_old">New to Old</option>
                                    <option value="old_to_new">Old to New</option>
                                </select>
                            </div>
                        </div>
                        <div class="selection-bar">
                            <div class="selection-controls">
                                <label class="choice compact-choice" for="selectAllRecords">
                                    <input id="selectAllRecords" type="checkbox">
                                    <span>Select all records shown right now</span>
                                </label>
                                <span id="recordSelectionCount" class="selection-count">0 selected</span>
                            </div>
                            <div class="toolbar-actions">
                                <button id="exportSelectedRecords" class="btn btn-secondary" type="button">Download Selected List</button>
                                <button id="deleteSelectedRecords" class="btn btn-danger" type="button">Delete Selected</button>
                            </div>
                        </div>
                        <div class="toolbar-actions">
                            <button id="admissionRecordsClearRange" class="btn btn-soft" type="button">Reset Search</button>
                        </div>
                    </div>
                    <div class="manage-note mb-18">Use the class dropdown with custom <strong>From</strong> and <strong>To</strong> date-time fields to review separate class sections below. This works for hours, days, or any custom period you need.</div>
                    <div class="workspace-divider"><span>Final Student Records</span></div>
                    <div id="admissionRecordsSections" class="stack">
                        <div class="card">
                            <div class="panel-head">
                                <div>
                                    <h3>Download Full Lists</h3>
                                    <p>Download full confirmed or rejected student lists, or use the class sections below for class-wise downloads.</p>
                                </div>
                                <div class="row-actions">
                                    <button class="btn btn-secondary record-export-all" type="button" data-status="confirmed">All Confirmed List</button>
                                    <button class="btn btn-soft record-confirmed-forms-all" type="button">All Confirmed Forms</button>
                                    <button class="btn btn-soft record-export-all" type="button" data-status="rejected">All Rejected List</button>
                                </div>
                            </div>
                        </div>
                        <div id="admissionRecordsClassSections" class="stack"></div>
                    </div>
                </section>

                <section class="glass panel hidden" data-panel="settings">
                    <div class="panel-head">
                        <div>
                            <h2>Website Settings & Public Information</h2>
                            <p>Use this settings workspace for contact details, homepage content, enrollment text, pop-up notices, and public communication controls.</p>
                        </div>
                    </div>
                    <div class="section-lead">
                        <span class="section-kicker">Most Used Website Changes</span>
                        <div class="manage-note">If you only need a quick public update, use the shortcut buttons below to move directly to the correct settings block.</div>
                    </div>
                    <div class="settings-jump-grid mb-18">
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="contactAccessBlock"><strong>Main Contact Details</strong><span>Change phone numbers, address, office timing, and map link.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="homeHeroBlock"><strong>Homepage Main Text</strong><span>Edit the big heading and side card on the homepage.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="homeSectionVisibilityBlock"><strong>Homepage Sections</strong><span>Show or hide the homepage blocks like stats, gallery, and FAQ.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="galleryContentBlock"><strong>Gallery Section</strong><span>Change gallery titles, card text, and image links from one place.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="faqContentBlock"><strong>FAQ Section</strong><span>Edit common questions and answers shown on the homepage.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="enrollmentContentBlock"><strong>Enrollment Page Text</strong><span>Update the feature cards shown near the form.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="classScopeBlock"><strong>Which Classes Can Apply?</strong><span>Choose all classes or only selected classes.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="statusMessagingBlock"><strong>WhatsApp & Status Messages</strong><span>Change the contact button and status-check popup text.</span></button>
                        <button class="quick-action" type="button" data-open-tab="settings" data-scroll-target="backupSectionCard"><strong>Backup & Restore</strong><span>Download a backup or restore an old full copy.</span></button>
                    </div>
                    <div class="helper-steps mb-18">
                        <div class="helper-step"><strong>1. Open a block</strong><span>Use the shortcut buttons or scroll to the section you need.</span></div>
                        <div class="helper-step"><strong>2. Change text</strong><span>Update the field values like a normal form and review them once.</span></div>
                        <div class="helper-step"><strong>3. Save once</strong><span>Press save and the public website updates automatically.</span></div>
                    </div>
                    <div class="workspace-divider"><span>Public Website Controls</span></div>
                    <div class="settings-grid">
                    <div class="card settings-section">
                        <div class="settings-section-head">
                            <div>
                                <h3>Easy Website Changes</h3>
                                <p>Keep homepage text, enrollment messaging, public contact details, and communication controls in one clear area.</p>
                            </div>
                            <span class="manage-chip gold">Public Facing</span>
                        </div>
                        <form id="settingsForm">
                            <div id="contactAccessBlock" class="settings-block">
                                <h4>Main Contact Details</h4>
                                <p>Change phone numbers, office timing, Facebook link, map, and enrollment open or closed status here.</p>
                                <div class="grid2">
                                    <div class="field"><label for="contactPrimary">Primary Phone</label><input class="input" id="contactPrimary" type="text" required></div>
                                    <div class="field"><label for="contactSecondary">Secondary Phone</label><input class="input" id="contactSecondary" type="text"></div>
                                </div>
                                <div class="field"><label for="officeTiming">Admin Office Timing</label><input class="input" id="officeTiming" type="text" placeholder="10:00 am to 08:00 pm" required></div>
                                <div class="field"><label for="settingsEmail">Email</label><input class="input" id="settingsEmail" type="email" required></div>
                                <div class="field"><label for="facebookUrl">Facebook Button Link</label><input class="input" id="facebookUrl" type="text" placeholder="https://www.facebook.com/theprofessorsacademy"></div>
                                <div class="field"><label for="settingsAddress">Address</label><textarea id="settingsAddress" required></textarea></div>
                                <div class="field"><label for="mapEmbedUrl">Google Map Embed URL</label><input class="input" id="mapEmbedUrl" type="url" placeholder="https://www.google.com/maps?q=...&output=embed"></div>
                                <div class="field">
                                    <label class="inline-toggle" for="enrollmentEnabled">
                                        <input id="enrollmentEnabled" type="checkbox">
                                        <span>Enable Public Enrollments</span>
                                    </label>
                                </div>
                                <div class="field"><label for="enrollmentClosedMessage">Closed Enrollment Message</label><textarea id="enrollmentClosedMessage" placeholder="Admissions are currently closed. They will open again from 24th Mar 2027."></textarea></div>
                                <div class="manage-note">For better Google indexing, keep the academy name, contacts, and public content updated. The site now serves a sitemap and robots file automatically.</div>
                            </div>
                            <div id="homeHeroBlock" class="settings-block">
                                <h4>Homepage Main Text</h4>
                                <p>Control the big heading area shown first on the public homepage.</p>
                                <div class="field"><label for="heroBadge">Hero Badge</label><input class="input" id="heroBadge" type="text" placeholder="Premium Academic Coaching"></div>
                                <div class="field"><label for="heroHeading">Hero Heading</label><textarea id="heroHeading" placeholder="Shape stronger futures with discipline, mentorship, and results."></textarea></div>
                                <div class="field"><label for="heroDescription">Hero Description</label><textarea id="heroDescription" placeholder="The Professors Academy is built for students who want focused preparation, supportive faculty, and a polished learning environment from Class IX through Class XII."></textarea></div>
                                <div class="grid2">
                                    <div class="field"><label for="heroOverlayTitle">Hero Side Card Title</label><input class="input" id="heroOverlayTitle" type="text" placeholder="Admissions are open for the 2026-27 session."></div>
                                    <div class="field"><label for="heroOverlayDescription">Hero Side Card Description</label><textarea id="heroOverlayDescription" placeholder="Submit your enrollment online and our team will follow up with the next academic steps."></textarea></div>
                                </div>
                            </div>
                            <div id="motionEffectsBlock" class="settings-block">
                                <h4>Website Motion & Effects</h4>
                                <p>Control visual feel for the public website. Animations and dark mode can both be turned on or off from here.</p>
                                <div class="field">
                                    <label class="inline-toggle" for="motionEnabled">
                                        <input id="motionEnabled" type="checkbox">
                                        <span>Enable Formal Website Animations</span>
                                    </label>
                                </div>
                                <div class="field">
                                    <label class="inline-toggle" for="darkModeEnabled">
                                        <input id="darkModeEnabled" type="checkbox">
                                        <span>Enable Public Website Dark Mode</span>
                                    </label>
                                </div>
                                <div class="manage-note">This controls the homepage entry motion, section reveal effects, and dark color theme on the public website only. Dark mode is off by default until you enable it here.</div>
                            </div>
                            <div id="homeSectionVisibilityBlock" class="settings-block">
                                <h4>Homepage Sections On / Off</h4>
                                <p>Use these switches to show or hide the homepage blocks without changing the rest of the website.</p>
                                <div class="grid2">
                                    <div class="field">
                                        <label class="inline-toggle" for="homeStatsEnabled">
                                            <input id="homeStatsEnabled" type="checkbox">
                                            <span>Show Quick Stats Block</span>
                                        </label>
                                    </div>
                                    <div class="field">
                                        <label class="inline-toggle" for="homeAnnouncementsEnabled">
                                            <input id="homeAnnouncementsEnabled" type="checkbox">
                                            <span>Show Latest Announcements Block</span>
                                        </label>
                                    </div>
                                    <div class="field">
                                        <label class="inline-toggle" for="homeGalleryEnabled">
                                            <input id="homeGalleryEnabled" type="checkbox">
                                            <span>Show Gallery Block</span>
                                        </label>
                                    </div>
                                    <div class="field">
                                        <label class="inline-toggle" for="homeFaqEnabled">
                                            <input id="homeFaqEnabled" type="checkbox">
                                            <span>Show FAQ Block</span>
                                        </label>
                                    </div>
                                    <div class="field">
                                        <label class="inline-toggle" for="homeMessageEnabled">
                                            <input id="homeMessageEnabled" type="checkbox">
                                            <span>Show Message Us Form</span>
                                        </label>
                                    </div>
                                </div>
                                <div class="manage-note">These controls affect the homepage blocks and the website message form area. The main pages and navigation stay available.</div>
                            </div>
                            <div id="galleryContentBlock" class="settings-block">
                                <h4>Gallery Section</h4>
                                <p>Change the gallery title, supporting text, and image cards shown on the homepage. Paste direct image links for each card.</p>
                                <div class="grid2">
                                    <div class="field"><label for="galleryBadge">Gallery Badge</label><input class="input" id="galleryBadge" type="text" placeholder="Academy Gallery"></div>
                                    <div class="field"><label for="galleryHeading">Gallery Heading</label><input class="input" id="galleryHeading" type="text" placeholder="Campus, classroom, event, and result day highlights"></div>
                                </div>
                                <div class="field"><label for="galleryDescription">Gallery Description</label><textarea id="galleryDescription" placeholder="A quick visual glimpse of the learning environment, academic activity, and celebratory moments at The Professors Academy."></textarea></div>
                                <div class="manage-card">
                                    <div class="manage-card-header">
                                        <div class="manage-card-copy"><h4>Gallery Card 1</h4><p>Use this for campus, building, or entry view.</p></div>
                                        <div class="manage-chip-row"><span class="manage-chip gold">Card 1</span></div>
                                    </div>
                                    <div class="grid2">
                                        <div class="field"><label for="galleryItem1Label">Card 1 Label</label><input class="input" id="galleryItem1Label" type="text" placeholder="Campus"></div>
                                        <div class="field"><label for="galleryItem1Title">Card 1 Title</label><input class="input" id="galleryItem1Title" type="text" placeholder="Welcoming academy environment"></div>
                                    </div>
                                    <div class="field"><label for="galleryItem1Description">Card 1 Description</label><textarea id="galleryItem1Description" placeholder="A clean and focused academy atmosphere that supports disciplined study and daily academic routine."></textarea></div>
                                    <div class="field"><label for="galleryItem1Image">Card 1 Image URL</label><input class="input" id="galleryItem1Image" type="text" placeholder="https://example.com/academy-campus.jpg"></div>
                                    <div class="field"><label for="galleryItem1ImageFile">Or Upload Card 1 Picture From PC</label><input id="galleryItem1ImageFile" class="input" type="file" accept=".jpg,.jpeg,.png,image/jpeg,image/png"><small class="muted">If you choose a file here, it will be used instead of the image link above.</small></div>
                                </div>
                                <div class="manage-card">
                                    <div class="manage-card-header">
                                        <div class="manage-card-copy"><h4>Gallery Card 2</h4><p>Use this for classroom activity or teaching space.</p></div>
                                        <div class="manage-chip-row"><span class="manage-chip gold">Card 2</span></div>
                                    </div>
                                    <div class="grid2">
                                        <div class="field"><label for="galleryItem2Label">Card 2 Label</label><input class="input" id="galleryItem2Label" type="text" placeholder="Classrooms"></div>
                                        <div class="field"><label for="galleryItem2Title">Card 2 Title</label><input class="input" id="galleryItem2Title" type="text" placeholder="Structured classroom learning"></div>
                                    </div>
                                    <div class="field"><label for="galleryItem2Description">Card 2 Description</label><textarea id="galleryItem2Description" placeholder="Subject-focused teaching spaces designed for clarity, attention, and consistent classroom engagement."></textarea></div>
                                    <div class="field"><label for="galleryItem2Image">Card 2 Image URL</label><input class="input" id="galleryItem2Image" type="text" placeholder="https://example.com/classroom.jpg"></div>
                                    <div class="field"><label for="galleryItem2ImageFile">Or Upload Card 2 Picture From PC</label><input id="galleryItem2ImageFile" class="input" type="file" accept=".jpg,.jpeg,.png,image/jpeg,image/png"><small class="muted">If you choose a file here, it will be used instead of the image link above.</small></div>
                                </div>
                                <div class="manage-card">
                                    <div class="manage-card-header">
                                        <div class="manage-card-copy"><h4>Gallery Card 3</h4><p>Use this for seminars, trips, or academy events.</p></div>
                                        <div class="manage-chip-row"><span class="manage-chip gold">Card 3</span></div>
                                    </div>
                                    <div class="grid2">
                                        <div class="field"><label for="galleryItem3Label">Card 3 Label</label><input class="input" id="galleryItem3Label" type="text" placeholder="Events"></div>
                                        <div class="field"><label for="galleryItem3Title">Card 3 Title</label><input class="input" id="galleryItem3Title" type="text" placeholder="Seminars and academic gatherings"></div>
                                    </div>
                                    <div class="field"><label for="galleryItem3Description">Card 3 Description</label><textarea id="galleryItem3Description" placeholder="Important academy moments, student briefings, and educational events that build confidence beyond the classroom."></textarea></div>
                                    <div class="field"><label for="galleryItem3Image">Card 3 Image URL</label><input class="input" id="galleryItem3Image" type="text" placeholder="https://example.com/event.jpg"></div>
                                    <div class="field"><label for="galleryItem3ImageFile">Or Upload Card 3 Picture From PC</label><input id="galleryItem3ImageFile" class="input" type="file" accept=".jpg,.jpeg,.png,image/jpeg,image/png"><small class="muted">If you choose a file here, it will be used instead of the image link above.</small></div>
                                </div>
                                <div class="manage-card">
                                    <div class="manage-card-header">
                                        <div class="manage-card-copy"><h4>Gallery Card 4</h4><p>Use this for result day, awards, or student success moments.</p></div>
                                        <div class="manage-chip-row"><span class="manage-chip gold">Card 4</span></div>
                                    </div>
                                    <div class="grid2">
                                        <div class="field"><label for="galleryItem4Label">Card 4 Label</label><input class="input" id="galleryItem4Label" type="text" placeholder="Result Day"></div>
                                        <div class="field"><label for="galleryItem4Title">Card 4 Title</label><input class="input" id="galleryItem4Title" type="text" placeholder="Achievements worth celebrating"></div>
                                    </div>
                                    <div class="field"><label for="galleryItem4Description">Card 4 Description</label><textarea id="galleryItem4Description" placeholder="Academic results, recognition, and progress updates shared with students and families in a proud setting."></textarea></div>
                                    <div class="field"><label for="galleryItem4Image">Card 4 Image URL</label><input class="input" id="galleryItem4Image" type="text" placeholder="https://example.com/result-day.jpg"></div>
                                    <div class="field"><label for="galleryItem4ImageFile">Or Upload Card 4 Picture From PC</label><input id="galleryItem4ImageFile" class="input" type="file" accept=".jpg,.jpeg,.png,image/jpeg,image/png"><small class="muted">If you choose a file here, it will be used instead of the image link above.</small></div>
                                </div>
                                <div class="manage-note">Tip: use direct JPG or PNG image links. After saving, the public gallery updates automatically.</div>
                            </div>
                            <div id="faqContentBlock" class="settings-block">
                                <h4>FAQ Section</h4>
                                <p>Change the frequently asked questions shown to parents and students on the homepage.</p>
                                <div class="grid2">
                                    <div class="field"><label for="faqBadge">FAQ Badge</label><input class="input" id="faqBadge" type="text" placeholder="Helpful Answers"></div>
                                    <div class="field"><label for="faqHeading">FAQ Heading</label><input class="input" id="faqHeading" type="text" placeholder="Frequently asked questions"></div>
                                </div>
                                <div class="field"><label for="faqDescription">FAQ Description</label><textarea id="faqDescription" placeholder="Quick answers for parents and students who want admission clarity before visiting the academy office."></textarea></div>
                                <div class="manage-card">
                                    <div class="manage-card-header">
                                        <div class="manage-card-copy"><h4>Question 1</h4><p>The first question opens first on the public website, so keep it the most important one.</p></div>
                                        <div class="manage-chip-row"><span class="manage-chip navy">FAQ 1</span></div>
                                    </div>
                                    <div class="field"><label for="faqItem1Question">Question 1</label><input class="input" id="faqItem1Question" type="text" placeholder="How do I apply for admission online?"></div>
                                    <div class="field"><label for="faqItem1Answer">Answer 1</label><textarea id="faqItem1Answer" placeholder="Fill in the enrollment form, upload the required passport size picture, and submit the form online."></textarea></div>
                                </div>
                                <div class="manage-card">
                                    <div class="manage-card-header">
                                        <div class="manage-card-copy"><h4>Question 2</h4><p>Use this for status-check or admission review related help.</p></div>
                                        <div class="manage-chip-row"><span class="manage-chip navy">FAQ 2</span></div>
                                    </div>
                                    <div class="field"><label for="faqItem2Question">Question 2</label><input class="input" id="faqItem2Question" type="text" placeholder="How can I check my enrollment status?"></div>
                                    <div class="field"><label for="faqItem2Answer">Answer 2</label><textarea id="faqItem2Answer" placeholder="Use the status check section with the same CNIC and date of birth entered in your enrollment form."></textarea></div>
                                </div>
                                <div class="manage-card">
                                    <div class="manage-card-header">
                                        <div class="manage-card-copy"><h4>Question 3</h4><p>Use this for office visit, fee, or follow-up guidance.</p></div>
                                        <div class="manage-chip-row"><span class="manage-chip navy">FAQ 3</span></div>
                                    </div>
                                    <div class="field"><label for="faqItem3Question">Question 3</label><input class="input" id="faqItem3Question" type="text" placeholder="When should I visit the academy office?"></div>
                                    <div class="field"><label for="faqItem3Answer">Answer 3</label><textarea id="faqItem3Answer" placeholder="Once your admission is confirmed, download the form and visit the admin office with the printed copy and the required fee during office timing."></textarea></div>
                                </div>
                                <div class="manage-card">
                                    <div class="manage-card-header">
                                        <div class="manage-card-copy"><h4>Question 4</h4><p>Use this for results, notices, or website information.</p></div>
                                        <div class="manage-chip-row"><span class="manage-chip navy">FAQ 4</span></div>
                                    </div>
                                    <div class="field"><label for="faqItem4Question">Question 4</label><input class="input" id="faqItem4Question" type="text" placeholder="Can I view results and notices online?"></div>
                                    <div class="field"><label for="faqItem4Answer">Answer 4</label><textarea id="faqItem4Answer" placeholder="Yes. Results and announcements are published on the website, so students and parents can stay informed without waiting for manual updates."></textarea></div>
                                </div>
                            </div>
                            <div id="enrollmentContentBlock" class="settings-block">
                                <h4>Enrollment Page Text</h4>
                                <p>Control the supporting text and feature cards shown near the public enrollment form.</p>
                                <div class="field"><label for="enrollmentInfoBadge">Enrollment Info Badge</label><input class="input" id="enrollmentInfoBadge" type="text" placeholder="Why Families Choose Us"></div>
                                <div class="field"><label for="enrollmentInfoHeading">Enrollment Info Heading</label><textarea id="enrollmentInfoHeading" placeholder="A polished academic environment, built around consistency."></textarea></div>
                                <div class="field"><label for="enrollmentInfoDescription">Enrollment Info Description</label><textarea id="enrollmentInfoDescription" placeholder="We blend strong classroom discipline with approachable faculty support so students grow with confidence."></textarea></div>
                                <div class="grid2">
                                    <div class="field"><label for="enrollmentCard1Label">Card 1 Label</label><input class="input" id="enrollmentCard1Label" type="text" placeholder="Focused Streams"></div>
                                    <div class="field"><label for="enrollmentCard1Title">Card 1 Title</label><input class="input" id="enrollmentCard1Title" type="text" placeholder="Science Ready"></div>
                                </div>
                                <div class="field"><label for="enrollmentCard1Description">Card 1 Description</label><textarea id="enrollmentCard1Description" placeholder="Clear pathways for Pre-Medical and Pre-Engineering students with guided subject selection."></textarea></div>
                                <div class="grid2">
                                    <div class="field"><label for="enrollmentCard2Label">Card 2 Label</label><input class="input" id="enrollmentCard2Label" type="text" placeholder="Fast Processing"></div>
                                    <div class="field"><label for="enrollmentCard2Title">Card 2 Title</label><input class="input" id="enrollmentCard2Title" type="text" placeholder="Digital Admission"></div>
                                </div>
                                <div class="field"><label for="enrollmentCard2Description">Card 2 Description</label><textarea id="enrollmentCard2Description" placeholder="Your form reaches the administration instantly through the secure enrollment system."></textarea></div>
                                <div class="grid2">
                                    <div class="field"><label for="enrollmentCard3Label">Card 3 Label</label><input class="input" id="enrollmentCard3Label" type="text" placeholder="Parent Confidence"></div>
                                    <div class="field"><label for="enrollmentCard3Title">Card 3 Title</label><input class="input" id="enrollmentCard3Title" type="text" placeholder="Transparent Communication"></div>
                                </div>
                                <div class="field"><label for="enrollmentCard3Description">Card 3 Description</label><textarea id="enrollmentCard3Description" placeholder="Announcements, contact details, and result uploads remain easy to access in one place."></textarea></div>
                            </div>
                            <div id="classScopeBlock" class="settings-block">
                                <h4>Which Classes Can Apply?</h4>
                                <p>Choose which classes appear in the public enrollment form. Students will only see the checked classes.</p>
                                <div class="field">
                                    <label class="inline-toggle" for="enrollmentClassAll">
                                        <input id="enrollmentClassAll" type="checkbox">
                                        <span>Allow All Classes</span>
                                    </label>
                                </div>
                                <div id="enrollmentClassScopeOptions" class="option-grid">
                                    <label class="choice"><input type="checkbox" data-enrollment-class-choice value="IX"><span>IX</span></label>
                                    <label class="choice"><input type="checkbox" data-enrollment-class-choice value="X"><span>X</span></label>
                                    <label class="choice"><input type="checkbox" data-enrollment-class-choice value="XI"><span>XI</span></label>
                                    <label class="choice"><input type="checkbox" data-enrollment-class-choice value="XII"><span>XII</span></label>
                                    <label class="choice"><input type="checkbox" data-enrollment-class-choice value="MDCAT Prep"><span>MDCAT Prep</span></label>
                                    <label class="choice"><input type="checkbox" data-enrollment-class-choice value="ECAT Prep"><span>ECAT Prep</span></label>
                                </div>
                                <small class="muted">If you leave all boxes checked, the public form will show all classes. If you choose a few classes, only those will appear.</small>
                            </div>
                            <div id="formDownloadBlock" class="settings-block">
                                <h4>Download Form Note</h4>
                                <p>Control the important note shown on the formal admission form that students and staff download.</p>
                                <div class="manage-note">You can also use <code>{contact_primary}</code>, <code>{office_timing}</code>, and <code>{academy_name}</code> inside the note if you want dynamic text.</div>
                                <div class="field"><label for="admissionFormNote">Important Note On Form</label><textarea id="admissionFormNote" placeholder="Please submit a printed copy of this form with Rs. 5000 at the admin office to complete the admission process."></textarea></div>
                            </div>
                            <div id="statusMessagingBlock" class="settings-block">
                                <h4>WhatsApp & Status Messages</h4>
                                <p>Configure the floating WhatsApp contact button and the messages shown in the status checker popup.</p>
                                <div class="field">
                                    <label class="inline-toggle" for="whatsappEnabled">
                                        <input id="whatsappEnabled" type="checkbox">
                                        <span>Enable Floating WhatsApp Button</span>
                                    </label>
                                </div>
                                <div class="field">
                                    <label class="inline-toggle" for="statusCheckEnabled">
                                        <input id="statusCheckEnabled" type="checkbox">
                                        <span>Enable Public Status Check</span>
                                    </label>
                                </div>
                                <div class="grid2">
                                    <div class="field"><label for="whatsappNumber">WhatsApp Number</label><input class="input" id="whatsappNumber" type="text" placeholder="923001234567"></div>
                                    <div class="field"><label for="whatsappMessage">WhatsApp Opening Message</label><input class="input" id="whatsappMessage" type="text" placeholder="Assalam o Alaikum, I would like to get admission information from The Professors Academy."></div>
                                </div>
                                <div class="manage-note">These messages appear in the public enrollment status popup. You can also use <code>{contact_primary}</code>, <code>{office_timing}</code>, and <code>{academy_name}</code> inside the message text if you want.</div>
                                <div class="field"><label for="statusCheckDisabledMessage">Status Check Disabled Message</label><textarea id="statusCheckDisabledMessage" placeholder="Enrollment status checking is currently unavailable. Please contact the academy office for assistance."></textarea></div>
                                <div class="field"><label for="statusMessagePending">Pending Message</label><textarea id="statusMessagePending" placeholder="Your enrollment is currently under review. We will contact you after verification."></textarea></div>
                                <div class="field"><label for="statusMessageConfirmed">Confirmed Message</label><textarea id="statusMessageConfirmed" placeholder="Your enrollment has been confirmed. Please stay connected with the academy for the next admission steps."></textarea></div>
                                <div class="field"><label for="statusMessageRejected">Rejected Message</label><textarea id="statusMessageRejected" placeholder="Your enrollment could not be approved at this time. Please contact the admin or visit the academy office for guidance."></textarea></div>
                                <div class="field"><label for="statusMessageNotFound">Record Not Found Message</label><textarea id="statusMessageNotFound" placeholder="No enrollment record matched the provided CNIC number. Please check the details and try again."></textarea></div>
                            </div>
                            <button class="btn btn-primary mt-16" type="submit">Save All Website Changes</button>
                        </form>
                    </div>
                    <div class="settings-stack">
                    <div id="backupSectionCard" class="card settings-section">
                        <div class="settings-section-head">
                            <div>
                                <h3>Save / Bring Back Backup</h3>
                                <p>Keep one safe backup before major changes. Use restore only when you need to bring back old data and uploads.</p>
                            </div>
                            <span class="manage-chip gold">Safety</span>
                        </div>
                        <div class="manage-note">Download a full backup before deleting many records, restoring old data, or doing large edits. A restore will replace the current database and upload files.</div>
                        <div class="row-actions">
                            <button id="downloadBackupButton" class="btn btn-secondary" type="button">Download Safety Backup</button>
                        </div>
                        <form id="restoreBackupForm" enctype="multipart/form-data">
                            <div class="field">
                                <label for="restoreBackupFile">Choose Old Backup File</label>
                                <input id="restoreBackupFile" class="input" name="backup_file" type="file" accept=".zip,.db" required>
                                <small class="muted">Choose a full backup ZIP or a database.db file.</small>
                            </div>
                            <button class="btn btn-danger mt-16" type="submit">Bring Back Backup</button>
                        </form>
                    </div>
                    <div id="popupNoticeSectionCard" class="card settings-section">
                        <div class="settings-section-head">
                            <div>
                                <h3>Home Popup Message</h3>
                                <p>Control the first-visit popup shown on the public website and connect its button to a section or result.</p>
                            </div>
                            <span class="manage-chip navy">Homepage</span>
                        </div>
                        <form id="popupNoticeForm">
                            <div class="field">
                                <label class="inline-toggle" for="homepagePopupEnabled">
                                    <input id="homepagePopupEnabled" type="checkbox">
                                    <span>Enable Homepage Popup Notice</span>
                                </label>
                            </div>
                            <div class="grid2">
                                <div class="field"><label for="homepagePopupTitle">Popup Title</label><input class="input" id="homepagePopupTitle" type="text" placeholder="Admissions & Results Update"></div>
                                <div class="field"><label for="homepagePopupButtonLabel">Button Label</label><input class="input" id="homepagePopupButtonLabel" type="text" placeholder="See"></div>
                            </div>
                            <div class="field"><label for="homepagePopupMessage">Popup Message</label><textarea id="homepagePopupMessage" placeholder="Admissions are open from ... to ... and latest results have been uploaded."></textarea></div>
                            <div class="grid2">
                                <div class="field">
                                    <label for="homepagePopupTargetSection">Button Target Section</label>
                                    <select id="homepagePopupTargetSection" class="input">
                                        <option value="">No target</option>
                                        <option value="home">Home</option>
                                        <option value="enrollment">Enrollment</option>
                                        <option value="faculty">Faculty</option>
                                        <option value="results">Results</option>
                                        <option value="announcements">Announcements</option>
                                        <option value="about">About Us</option>
                                        <option value="status-check">Status Check</option>
                                    </select>
                                </div>
                                <div class="field" id="homepagePopupResultField">
                                    <label for="homepagePopupResultId">Highlight Result</label>
                                    <select id="homepagePopupResultId" class="input">
                                        <option value="">No specific result</option>
                                    </select>
                                </div>
                            </div>
                            <button class="btn btn-primary mt-16" type="submit">Save Popup Notice</button>
                        </form>
                    </div>
                    <div class="card settings-section">
                        <div class="settings-section-head">
                            <div>
                                <h3>Where To Use Each Section</h3>
                                <p>Use these areas consistently so the dashboard stays clear and easy for future staff.</p>
                            </div>
                            <span class="manage-chip">Workflow</span>
                        </div>
                        <div class="stack">
                            <div class="record-line"><strong>New Admissions</strong><span>Use this section to review new forms, update details, and confirm or reject applications.</span></div>
                            <div class="record-line"><strong>Approved / Rejected</strong><span>Use this section for confirmed and rejected students, record keeping, list downloads, and form downloads.</span></div>
                            <div class="record-line"><strong>Notices & Top Line</strong><span>Keep announcements, hidden drafts, and the public moving top line together in one place.</span></div>
                            <div class="record-line"><strong>Website Text & Contact</strong><span>Keep homepage text, enrollment content, public contact details, popup content, and communication controls here.</span></div>
                            <div class="record-line"><strong>Save / Bring Back Backup</strong><span>Use this tool only when you want to save a safety copy or bring back an older full backup.</span></div>
                        </div>
                    </div>
                    </div>
                    </div>
                </section>
            </div>
        </section>
    </div>

    <div id="toast" class="toast"></div>
    <div id="adminConfirmModal" class="admin-confirm-modal hidden" aria-hidden="true">
        <div class="admin-confirm-dialog">
            <div id="adminConfirmMark" class="admin-confirm-mark">!</div>
            <span id="adminConfirmBadge" class="eyebrow">Please Confirm</span>
            <h3 id="adminConfirmTitle">Continue with this action?</h3>
            <p id="adminConfirmMessage" class="muted">This change will be applied right away.</p>
            <div class="admin-confirm-actions">
                <button id="adminConfirmCancel" class="btn btn-soft" type="button">Cancel</button>
                <button id="adminConfirmApprove" class="btn btn-primary" type="button">Yes, Continue</button>
            </div>
        </div>
    </div>
    <div id="enrollmentEditModal" class="crop-modal hidden">
        <div class="crop-dialog">
            <div class="panel-head">
                <div>
                    <h3 id="enrollmentEditTitle">Edit Enrollment</h3>
                    <p>Update student details, replace the passport photo if needed, and save the changes without removing the record.</p>
                </div>
            </div>
            <form id="enrollmentEditForm" class="dialog-scroll" enctype="multipart/form-data">
                <input id="editEnrollmentId" name="id" type="hidden">
                <div class="grid2">
                    <div class="field"><label for="editFullName">Full Name</label><input class="input" id="editFullName" name="full_name" type="text" required></div>
                    <div class="field"><label for="editFatherName">Father Name</label><input class="input" id="editFatherName" name="father_name" type="text" required></div>
                </div>
                <div class="grid2">
                    <div class="field"><label for="editMobile">Mobile No</label><input class="input" id="editMobile" name="mobile" type="tel" maxlength="14" placeholder="+92 1234567890" required></div>
                    <div class="field"><label for="editCnic">CNIC No</label><input class="input" id="editCnic" name="cnic" type="text" maxlength="15" placeholder="12345-1234567-1" required></div>
                </div>
                <div class="grid2">
                    <div class="field"><label for="editFatherContact">Father Contact No</label><input class="input" id="editFatherContact" name="father_contact" type="tel" maxlength="14" placeholder="+92 1234567890" required></div>
                    <div class="field">
                        <label for="editGender">Gender</label>
                        <select id="editGender" name="gender" class="input" required>
                            <option value="">Select Gender</option>
                            <option value="Male">Male</option>
                            <option value="Female">Female</option>
                            <option value="Other">Other</option>
                        </select>
                    </div>
                </div>
                <div class="grid2">
                    <div class="field"><label for="editEmail">Email Address</label><input class="input" id="editEmail" name="email" type="email" required></div>
                    <div class="field"><label for="editDateOfBirth">Date of Birth (DD/MM/YYYY)</label><input class="input" id="editDateOfBirth" name="date_of_birth" type="text" inputmode="numeric" maxlength="10" placeholder="DD/MM/YYYY" required><small class="muted">Example: 05/03/2008</small></div>
                </div>
                <div class="grid2">
                    <div class="field">
                        <label for="editEnrollmentClass">Class</label>
                        <select id="editEnrollmentClass" name="class" class="input" required>
                            <option value="">Select Class</option>
                            <option value="IX">IX</option>
                            <option value="X">X</option>
                            <option value="XI">XI</option>
                            <option value="XII">XII</option>
                            <option value="MDCAT Prep">MDCAT Prep</option>
                            <option value="ECAT Prep">ECAT Prep</option>
                        </select>
                    </div>
                    <div class="field">
                        <label for="editEnrollmentPhoto">Passport Picture</label>
                        <input class="input" id="editEnrollmentPhoto" name="photo" type="file" accept="image/png,image/jpeg">
                                <small class="muted">Leave empty to keep the current photo. JPG or PNG only, 300 KB or smaller.</small>
                    </div>
                </div>
                <div class="field hidden" id="editEnrollmentGroupField">
                    <label>Group Selection</label>
                    <div class="group-wrap" id="editEnrollmentGroupOptions"></div>
                </div>
                <div class="field">
                    <label>Subjects</label>
                    <div class="subjects-wrap" id="editEnrollmentSubjectOptions">
                        <div class="empty">Select a class to view available subjects.</div>
                    </div>
                </div>
                <div class="field"><label for="editEnrollmentAddress">Address</label><textarea id="editEnrollmentAddress" name="address" required></textarea></div>
                <div id="editEnrollmentPhotoPreviewWrap" class="crop-preview hidden">
                    <div>
                        <h4 class="mb-12">Student Photo Preview</h4>
                        <p>Use this preview to confirm the passport picture before saving the record.</p>
                    </div>
                    <img id="editEnrollmentPhotoPreview" alt="Student photo preview">
                </div>
                <div class="row-actions mt-16">
                    <button class="btn btn-primary" type="submit">Save Enrollment</button>
                    <button class="btn btn-soft" id="enrollmentEditCancel" type="button">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    <div id="facultyCropModal" class="crop-modal hidden">
        <div class="crop-dialog">
            <div class="panel-head">
                <div>
                    <h3>Crop Faculty Photo</h3>
                    <p>Adjust the image so the teacher photo looks clean and centered on the website.</p>
                </div>
            </div>
            <div class="crop-layout">
                <div class="crop-stage">
                    <canvas id="facultyCropCanvas" width="420" height="420"></canvas>
                </div>
                <div class="crop-controls">
                    <div class="field"><label for="facultyCropZoom">Zoom</label><input id="facultyCropZoom" class="input" type="range" min="100" max="240" step="1" value="100"></div>
                    <div class="field"><label for="facultyCropX">Horizontal Position</label><input id="facultyCropX" class="input" type="range" min="0" max="0" step="1" value="0"></div>
                    <div class="field"><label for="facultyCropY">Vertical Position</label><input id="facultyCropY" class="input" type="range" min="0" max="0" step="1" value="0"></div>
                    <div class="note-box">Move the sliders until the face is centered, then click Apply Crop. You can also keep the original photo if you do not want to crop it.</div>
                    <div class="row-actions">
                        <button id="applyFacultyCrop" class="btn btn-primary" type="button">Apply Crop</button>
                        <button id="keepOriginalFacultyPhoto" class="btn btn-soft" type="button">Keep Original</button>
                        <button id="cancelFacultyCrop" class="btn btn-danger" type="button">Cancel</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const state={username:'',csrfToken:'',announcements:[],results:[],faculty:[],pendingEnrollments:[],confirmedEnrollments:[],rejectedEnrollments:[],messages:[],activityLog:[],insights:{},settings:{}};
        const selectionState={pending:new Set(),records:new Set()};
        let insightsRefreshTimer=null;
        const loginView=document.getElementById('loginView');
        const dashboardView=document.getElementById('dashboardView');
        const toast=document.getElementById('toast');
        const themeColorMeta=document.querySelector('meta[name="theme-color"]');
        const adminConfirmModal=document.getElementById('adminConfirmModal');
        const adminConfirmMark=document.getElementById('adminConfirmMark');
        const adminConfirmBadge=document.getElementById('adminConfirmBadge');
        const adminConfirmTitle=document.getElementById('adminConfirmTitle');
        const adminConfirmMessage=document.getElementById('adminConfirmMessage');
        const adminConfirmCancel=document.getElementById('adminConfirmCancel');
        const adminConfirmApprove=document.getElementById('adminConfirmApprove');
        const adminUsername=document.getElementById('adminUsername');
        const sidebarSnapshot=document.getElementById('sidebarSnapshot');
        const statsGrid=document.getElementById('statsGrid');
        const recentAnnouncements=document.getElementById('recentAnnouncements');
        const activityLogList=document.getElementById('activityLogList');
        const insightsGrid=document.getElementById('insightsGrid');
        const insightsTimeline=document.getElementById('insightsTimeline');
        const insightsSectionList=document.getElementById('insightsSectionList');
        const enrollmentClassTabs=document.getElementById('enrollmentClassTabs');
        const enrollmentClassSections=document.getElementById('enrollmentClassSections');
        const enrollmentNameSearch=document.getElementById('enrollmentNameSearch');
        const enrollmentRollSearch=document.getElementById('enrollmentRollSearch');
        const enrollmentCnicSearch=document.getElementById('enrollmentCnicSearch');
        const enrollmentSearch=document.getElementById('enrollmentSearch');
        const enrollmentClassFilter=document.getElementById('enrollmentClassFilter');
        const enrollmentFrom=document.getElementById('enrollmentFrom');
        const enrollmentTo=document.getElementById('enrollmentTo');
        const enrollmentSort=document.getElementById('enrollmentSort');
        const selectAllPending=document.getElementById('selectAllPending');
        const pendingSelectionCount=document.getElementById('pendingSelectionCount');
        const bulkConfirmPendingButton=document.getElementById('bulkConfirmPending');
        const bulkRejectPendingButton=document.getElementById('bulkRejectPending');
        const bulkDeletePendingButton=document.getElementById('bulkDeletePending');
        const exportSelectedPendingButton=document.getElementById('exportSelectedPending');
        const clearPendingFiltersButton=document.getElementById('clearPendingFilters');
        const admissionRecordsSections=document.getElementById('admissionRecordsSections');
        const admissionRecordsClassTabs=document.getElementById('admissionRecordsClassTabs');
        const admissionRecordsClassSections=document.getElementById('admissionRecordsClassSections');
        const admissionRecordsNameSearch=document.getElementById('admissionRecordsNameSearch');
        const admissionRecordsRollSearch=document.getElementById('admissionRecordsRollSearch');
        const admissionRecordsCnicSearch=document.getElementById('admissionRecordsCnicSearch');
        const admissionRecordsSearch=document.getElementById('admissionRecordsSearch');
        const admissionRecordsClassFilter=document.getElementById('admissionRecordsClassFilter');
        const admissionRecordsStatusFilter=document.getElementById('admissionRecordsStatusFilter');
        const admissionRecordsFrom=document.getElementById('admissionRecordsFrom');
        const admissionRecordsTo=document.getElementById('admissionRecordsTo');
        const admissionRecordsClearRange=document.getElementById('admissionRecordsClearRange');
        const admissionRecordsSort=document.getElementById('admissionRecordsSort');
        const selectAllRecords=document.getElementById('selectAllRecords');
        const recordSelectionCount=document.getElementById('recordSelectionCount');
        const exportSelectedRecordsButton=document.getElementById('exportSelectedRecords');
        const deleteSelectedRecordsButton=document.getElementById('deleteSelectedRecords');
        const messageList=document.getElementById('messageList');
        const messageSearch=document.getElementById('messageSearch');
        const messageStatusFilter=document.getElementById('messageStatusFilter');
        const messageSummaryCount=document.getElementById('messageSummaryCount');
        const clearMessageFiltersButton=document.getElementById('clearMessageFilters');
        const announcementList=document.getElementById('announcementList');
        const resultList=document.getElementById('resultList');
        const facultyList=document.getElementById('facultyList');
        const announcementForm=document.getElementById('announcementForm');
        const resultForm=document.getElementById('resultForm');
        const resultFormTitle=document.getElementById('resultFormTitle');
        const resultIdInput=document.getElementById('resultId');
        const resultTitleInput=document.getElementById('resultTitle');
        const resultClassInput=document.getElementById('resultClass');
        const resultYearInput=document.getElementById('resultYear');
        const resultPdfInput=document.getElementById('resultPdf');
        const marqueeForm=document.getElementById('marqueeForm');
        const facultyForm=document.getElementById('facultyForm');
        const settingsForm=document.getElementById('settingsForm');
        const popupNoticeForm=document.getElementById('popupNoticeForm');
        const downloadBackupButton=document.getElementById('downloadBackupButton');
        const restoreBackupForm=document.getElementById('restoreBackupForm');
        const restoreBackupFile=document.getElementById('restoreBackupFile');
        const facultyPhotoInput=document.getElementById('facultyPhoto');
        const facultyCurrentPhotoWrap=document.getElementById('facultyCurrentPhotoWrap');
        const facultyCurrentPhoto=document.getElementById('facultyCurrentPhoto');
        const facultyRemovePhoto=document.getElementById('facultyRemovePhoto');
        const facultyPhotoPreviewWrap=document.getElementById('facultyPhotoPreviewWrap');
        const facultyPhotoPreview=document.getElementById('facultyPhotoPreview');
        const facultyCropModal=document.getElementById('facultyCropModal');
        const facultyCropCanvas=document.getElementById('facultyCropCanvas');
        const facultyCropZoom=document.getElementById('facultyCropZoom');
        const facultyCropX=document.getElementById('facultyCropX');
        const facultyCropY=document.getElementById('facultyCropY');
        const enrollmentEditModal=document.getElementById('enrollmentEditModal');
        const enrollmentEditForm=document.getElementById('enrollmentEditForm');
        const enrollmentEditTitle=document.getElementById('enrollmentEditTitle');
        const editEnrollmentId=document.getElementById('editEnrollmentId');
        const editFullName=document.getElementById('editFullName');
        const editFatherName=document.getElementById('editFatherName');
        const editMobile=document.getElementById('editMobile');
        const editCnic=document.getElementById('editCnic');
        const editFatherContact=document.getElementById('editFatherContact');
        const editGender=document.getElementById('editGender');
        const editEmail=document.getElementById('editEmail');
        const editDateOfBirth=document.getElementById('editDateOfBirth');
        const editEnrollmentClass=document.getElementById('editEnrollmentClass');
        const editEnrollmentPhoto=document.getElementById('editEnrollmentPhoto');
        const editEnrollmentAddress=document.getElementById('editEnrollmentAddress');
        const editEnrollmentGroupField=document.getElementById('editEnrollmentGroupField');
        const editEnrollmentGroupOptions=document.getElementById('editEnrollmentGroupOptions');
        const editEnrollmentSubjectOptions=document.getElementById('editEnrollmentSubjectOptions');
        const editEnrollmentPhotoPreviewWrap=document.getElementById('editEnrollmentPhotoPreviewWrap');
        const editEnrollmentPhotoPreview=document.getElementById('editEnrollmentPhotoPreview');
        const exportEnrollmentsButton=document.getElementById('exportEnrollments');
        const downloadConfirmedFormsButton=document.getElementById('downloadConfirmedForms');
        const enrollmentClassAll=document.getElementById('enrollmentClassAll');
        const enrollmentClassScopeOptions=document.getElementById('enrollmentClassScopeOptions');
        const facultyCropState={file:null,image:null,sourceUrl:'',previewUrl:'',zoom:1,offsetX:0,offsetY:0,baseWidth:0,baseHeight:0};
        const enrollmentEditState={selectedSubjects:[],group:'',previewUrl:'',originalPhotoUrl:'',studentId:''};
        const enrollmentSubjectCatalog={IX:['English','Maths','Biology','Physics','Chemistry'],X:['English','Maths','Biology','Physics','Chemistry'],XI:{'Pre-Medical':['English','Botany','Zoology','Physics','Chemistry'],'Pre-Engineering':['Maths','English','Physics','Chemistry']},XII:{'Pre-Medical':['English','Botany','Zoology','Physics','Chemistry'],'Pre-Engineering':['Maths','English','Physics','Chemistry']},'MDCAT Prep':['English','Botany','Zoology','Physics','Chemistry'],'ECAT Prep':['Maths','English','Physics','Chemistry']};
        const enrollmentClassChoices=['IX','X','XI','XII','MDCAT Prep','ECAT Prep'];
        const publicSyncKey='tpa-public-sync';
        const adminSyncKey='tpa-admin-sync';
        const crossPageSyncChannel=typeof BroadcastChannel==='function'?new BroadcastChannel('tpa-cross-page-sync'):null;
        let adminRefreshListenersBound=false;
        let adminConfirmResolver=null;

        function toastMessage(message,type='success'){toast.textContent=message;toast.className=`toast show ${type}`;clearTimeout(toastMessage.timer);toastMessage.timer=setTimeout(()=>toast.className='toast',2800)}
        function renderAdminTheme(forceValue=null){
            const enabled=typeof forceValue==='boolean'?forceValue:String(state.settings.dark_mode_enabled||'0')==='1';
            document.body.classList.toggle('theme-dark-admin',enabled);
            if(themeColorMeta)themeColorMeta.setAttribute('content',enabled?'#08111b':'#0a1929');
        }
        function syncAdminModalState(){
            const hasVisibleConfirm=adminConfirmModal&&!adminConfirmModal.classList.contains('hidden');
            const hasVisibleCrop=facultyCropModal&&!facultyCropModal.classList.contains('hidden');
            const hasVisibleEnrollmentEdit=enrollmentEditModal&&!enrollmentEditModal.classList.contains('hidden');
            document.body.classList.toggle('modal-open',Boolean(hasVisibleConfirm||hasVisibleCrop||hasVisibleEnrollmentEdit));
        }
        function closeAdminConfirmModal(result=false){
            if(!adminConfirmModal)return;
            adminConfirmModal.classList.add('hidden');
            adminConfirmModal.setAttribute('aria-hidden','true');
            syncAdminModalState();
            const resolver=adminConfirmResolver;
            adminConfirmResolver=null;
            if(resolver)resolver(result);
        }
        function confirmAction(message,{title='Please confirm this action',badge='Admin Confirmation',mark='!',tone='primary',confirmLabel='Yes, Continue',cancelLabel='Cancel'}={}){
            if(!adminConfirmModal)return Promise.resolve(window.confirm(message));
            adminConfirmMark.textContent=mark;
            adminConfirmBadge.textContent=badge;
            adminConfirmTitle.textContent=title;
            adminConfirmMessage.textContent=message;
            adminConfirmApprove.textContent=confirmLabel;
            adminConfirmCancel.textContent=cancelLabel;
            adminConfirmApprove.className=tone==='danger'?'btn btn-danger':'btn btn-primary';
            adminConfirmModal.classList.remove('hidden');
            adminConfirmModal.setAttribute('aria-hidden','false');
            syncAdminModalState();
            return new Promise((resolve)=>{adminConfirmResolver=resolve;});
        }
        function buildAdminApiUrl(url){try{const parsed=new URL(url,window.location.origin);parsed.searchParams.set('_ts',String(Date.now()));return parsed.toString()}catch(_error){return url}}
        async function api(url,options={}){const requestOptions={credentials:'same-origin',...options};const headers=new Headers(options.headers||{});const method=String(requestOptions.method||'GET').toUpperCase();if(!headers.has('X-Requested-With'))headers.set('X-Requested-With','fetch');if(state.csrfToken&&!['GET','HEAD','OPTIONS'].includes(method))headers.set('X-CSRF-Token',state.csrfToken);requestOptions.headers=headers;if(['GET','HEAD'].includes(method))requestOptions.cache='no-store';const requestUrl=['GET','HEAD'].includes(method)?buildAdminApiUrl(url):url;const response=await fetch(requestUrl,requestOptions);const type=response.headers.get('content-type')||'';const data=type.includes('application/json')?await response.json():{};if(data&&data.csrf_token)state.csrfToken=data.csrf_token;if(!response.ok)throw new Error(data.message||'Request failed.');return data}
        function writeSyncKey(key,payload){try{window.localStorage.setItem(key,JSON.stringify(payload))}catch(_error){}}
        function notifyCrossPageSync(target){const payload={target,ts:Date.now()};writeSyncKey(target==='public'?publicSyncKey:adminSyncKey,payload);if(crossPageSyncChannel)crossPageSyncChannel.postMessage(payload)}
        function handleAdminSyncMessage(payload){if(!payload||payload.target!=='admin'||dashboardView.classList.contains('hidden'))return;refreshAdminLiveData(true)}
        function escapeHtml(value){return String(value??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
        function extractEnrollmentDateDigits(value){return String(value||'').replace(/\D/g,'').slice(0,8)}
        function formatEnrollmentDateValue(value){const raw=String(value||'').trim();if(!raw)return'';if(/^\d{4}-\d{2}-\d{2}$/.test(raw)){const[year,month,day]=raw.split('-');return`${day}/${month}/${year}`}if(/^\d{2}\/\d{2}\/\d{4}$/.test(raw))return raw;const digits=extractEnrollmentDateDigits(raw);const parts=[];if(digits.length>0)parts.push(digits.slice(0,2));if(digits.length>2)parts.push(digits.slice(2,4));if(digits.length>4)parts.push(digits.slice(4,8));return parts.join('/')}
        function parseEnrollmentDateValue(value){const formatted=/^\d{8}$/.test(String(value||'').trim())?formatEnrollmentDateValue(value):String(value||'').trim();const match=formatted.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);if(match){const day=Number(match[1]);const month=Number(match[2]);const year=Number(match[3]);const parsed=new Date(year,month-1,day);if(Number.isNaN(parsed.getTime())||parsed.getFullYear()!==year||parsed.getMonth()!==month-1||parsed.getDate()!==day)return null;return parsed}if(/^\d{4}-\d{2}-\d{2}$/.test(formatted)){const[year,month,day]=formatted.split('-').map((item)=>Number(item));const parsed=new Date(year,month-1,day);if(Number.isNaN(parsed.getTime())||parsed.getFullYear()!==year||parsed.getMonth()!==month-1||parsed.getDate()!==day)return null;return parsed}return null}
        function validateEnrollmentDateValue(value){const parsed=parseEnrollmentDateValue(value);if(!parsed)return false;const today=new Date();today.setHours(0,0,0,0);parsed.setHours(0,0,0,0);return parsed<=today&&parsed.getFullYear()>=1900}
        function formatEnrollmentDateInput(input){if(!input)return;input.value=formatEnrollmentDateValue(input.value)}
        function handleEnrollmentDateKeydown(event){if(event.ctrlKey||event.metaKey||event.altKey)return;const allowedKeys=['Backspace','Delete','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Tab','Home','End','Enter'];if(allowedKeys.includes(event.key))return;if(!/^\d$/.test(event.key))event.preventDefault()}
        function attachEnrollmentDateFormatting(input){if(!input)return;input.addEventListener('keydown',handleEnrollmentDateKeydown);input.addEventListener('input',()=>formatEnrollmentDateInput(input));input.addEventListener('paste',()=>setTimeout(()=>formatEnrollmentDateInput(input),0));input.addEventListener('blur',()=>formatEnrollmentDateInput(input))}
        function formatDate(value){if(!value)return'N/A';const raw=String(value||'').trim();if(/^\d{4}-\d{2}-\d{2}$/.test(raw)){const[year,month,day]=raw.split('-');return`${day}/${month}/${year}`}if(/^\d{2}\/\d{2}\/\d{4}$/.test(raw))return raw;const date=new Date(raw);if(Number.isNaN(date.getTime()))return raw;return date.toLocaleDateString('en-PK',{year:'numeric',month:'short',day:'numeric'})}
        function hasInvalidPhoneChars(value){return /[^+\d\s]/.test(String(value||''))}
        function extractPhoneDigits(value){const raw=String(value||'').trim();let digits=raw.replace(/[^\d]/g,'');if(raw.startsWith('+92')){if(digits.startsWith('92'))digits=digits.slice(2)}else if(digits.startsWith('0092'))digits=digits.slice(4);else if(digits.startsWith('92'))digits=digits.slice(2);else if(digits.startsWith('0')&&digits.length>10)digits=digits.slice(1);return digits.slice(0,10)}
        function normalizePhoneDigits(value){if(hasInvalidPhoneChars(value))return '';const digits=extractPhoneDigits(value);return /^\d{10}$/.test(digits)?digits:''}
        function formatPhoneValue(value){return `+92 ${extractPhoneDigits(value)}`}
        function formatEnrollmentPhoneInput(input){if(!input)return;input.value=formatPhoneValue(input.value)}
        function handleEnrollmentPhoneKeydown(event){if(event.ctrlKey||event.metaKey||event.altKey)return;const allowedKeys=['Backspace','Delete','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Tab','Home','End','Enter'];if(allowedKeys.includes(event.key))return;if(!/^\d$/.test(event.key))event.preventDefault()}
        function attachEnrollmentPhoneFormatting(input){if(!input)return;input.addEventListener('keydown',handleEnrollmentPhoneKeydown);input.addEventListener('input',()=>formatEnrollmentPhoneInput(input));input.addEventListener('paste',()=>setTimeout(()=>formatEnrollmentPhoneInput(input),0));input.addEventListener('focus',()=>{if(!String(input.value||'').trim())input.value='+92 '});input.addEventListener('blur',()=>formatEnrollmentPhoneInput(input))}
        function validateEnrollmentPhoneValue(value){return /^\d{10}$/.test(normalizePhoneDigits(value))}
        function setAuthenticated(flag){loginView.classList.toggle('hidden',flag);dashboardView.classList.toggle('hidden',!flag)}
        function switchTab(tabName){document.querySelectorAll('.tab').forEach((button)=>button.classList.toggle('active',button.dataset.tab===tabName));document.querySelectorAll('[data-panel]').forEach((panel)=>panel.classList.toggle('hidden',panel.dataset.panel!==tabName));if(tabName==='overview')refreshInsights(true)}
        function openAdminWorkspace(tabName,targetId=''){
            switchTab(tabName);
            const runScroll=()=>{
                const target=targetId?document.getElementById(targetId):document.querySelector(`[data-panel="${tabName}"]`);
                if(target)target.scrollIntoView({behavior:'smooth',block:'start'});
            };
            window.setTimeout(runScroll,40);
        }
        function initials(name){return String(name||'').split(' ').filter(Boolean).slice(0,2).map((item)=>item[0].toUpperCase()).join('')||'TPA'}
        function renderSidebarSnapshot(){if(!sidebarSnapshot)return;const unreadMessages=state.messages.filter((item)=>!item.is_read).length;const stats=[['Pending',state.pendingEnrollments.length],['Confirmed',state.confirmedEnrollments.length],['Messages',unreadMessages],['Rejected',state.rejectedEnrollments.length],['Faculty',state.faculty.length]];sidebarSnapshot.innerHTML=stats.map(([label,value])=>`<div class="sidebar-stat"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join('')}
        function downloadBlob(blob,filename){const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=filename;document.body.appendChild(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),400)}
        function filenameFromDisposition(value,fallback){const match=String(value||'').match(/filename=\"?([^\";]+)\"?/i);return match&&match[1]?match[1]:fallback}
        function formatDateTime(value){if(!value)return'N/A';const date=new Date(value);if(Number.isNaN(date.getTime()))return String(value);return date.toLocaleString('en-PK',{year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}
        function normalizeSearchValue(value){return String(value||'').trim().toLowerCase()}
        function matchesContains(source,query){return !query||normalizeSearchValue(source).includes(query)}
        function rowMatchesSpecificFields(item,filters={}){return matchesContains(item.name,filters.name)&&matchesContains(item.roll_number,filters.roll)&&matchesContains(item.cnic,filters.cnic)}
        function pruneSelection(selectionSet,rows){const validIds=new Set((rows||[]).map((item)=>String(item.id)));Array.from(selectionSet).forEach((id)=>{if(!validIds.has(String(id)))selectionSet.delete(String(id))})}
        function setSelectionCount(element,selectionSet,label){if(!element)return;const count=selectionSet.size;element.textContent=`${count} selected`;element.setAttribute('aria-label',`${count} ${label} selected`)}
        function syncSelectAllState(input,rows,selectionSet){if(!input)return;const ids=(rows||[]).map((item)=>String(item.id));input.checked=Boolean(ids.length)&&ids.every((id)=>selectionSet.has(id));input.indeterminate=Boolean(ids.length)&&!input.checked&&ids.some((id)=>selectionSet.has(id))}
        function csvEscape(value){const text=String(value??'');return /[",\n]/.test(text)?`"${text.replace(/"/g,'""')}"`:text}
        function buildEnrollmentCsvContent(rows,statusLabel=''){const header=['Roll No','Full Name','Father Name','Father Contact','Gender','Email','Date Of Birth','Student Contact','CNIC','Class','Group','Subjects','Address','Status','Application Date','Confirmed On','Rejected On'];const lines=[header.map(csvEscape).join(',')];(rows||[]).forEach((item)=>{lines.push([item.roll_number||'',item.name||'',item.father_name||'',item.father_contact||'',item.gender||'',item.email||'',item.date_of_birth||'',item.mobile||'',item.cnic||'',item.class||'',item.group||'',(item.subjects||[]).join(', '),item.address||'',statusLabel||'',item.date||'',item.confirmed_at||'',item.rejected_at||''].map(csvEscape).join(','))});return lines.join('\n')}
        function downloadRowsAsCsv(rows,statusLabel,filename){const csvContent=buildEnrollmentCsvContent(rows,statusLabel);downloadBlob(new Blob([csvContent],{type:'text/csv;charset=utf-8'}),filename)}
        async function downloadBackupArchive(){const response=await fetch('__ADMIN_PATH__/backup',{credentials:'same-origin'});if(!response.ok){let message='Unable to download backup.';try{const data=await response.json();message=data.message||message}catch(_error){}throw new Error(message)}const blob=await response.blob();downloadBlob(blob,filenameFromDisposition(response.headers.get('content-disposition'),'the_professors_academy_backup.zip'))}
        async function downloadReport(range){const response=await fetch(`__ADMIN_PATH__/reports/summary?range=${encodeURIComponent(range)}`);if(!response.ok){let message='Unable to download the report.';try{const data=await response.json();message=data.message||message}catch(_error){}throw new Error(message)}const blob=await response.blob();const filename=range==='all'?'the_professors_academy_admission_report_all_time.csv':`the_professors_academy_admission_report_last_${range}_days.csv`;downloadBlob(blob,filename)}
        async function downloadConfirmedForms(className=''){const query=className?`?class_name=${encodeURIComponent(className)}`:'';const response=await fetch(`__ADMIN_PATH__/reports/confirmed-forms${query}`);if(!response.ok){let message='Unable to download the confirmed admission forms.';try{const data=await response.json();message=data.message||message}catch(_error){}throw new Error(message)}const blob=await response.blob();const fallback=className?`the_professors_academy_confirmed_admission_forms_${className.replace(/[^A-Za-z0-9_-]+/g,'_')}.pdf`:'the_professors_academy_confirmed_admission_forms_all_classes.pdf';downloadBlob(blob,filenameFromDisposition(response.headers.get('content-disposition'),fallback))}
        async function downloadEnrollmentCsv(status,className=''){const query=new URLSearchParams({status});if(className)query.set('class_name',className);const response=await fetch(`__ADMIN_PATH__/reports/enrollments-export?${query.toString()}`);if(!response.ok){let message='Unable to download enrollment export.';try{const data=await response.json();message=data.message||message}catch(_error){}throw new Error(message)}const blob=await response.blob();const fallback=`the_professors_academy_${status}_enrollments_${(className||'all_classes').replace(/[^A-Za-z0-9_-]+/g,'_')}.csv`;downloadBlob(blob,filenameFromDisposition(response.headers.get('content-disposition'),fallback))}
        function classOrderList(){return ['IX','X','XI','XII','MDCAT Prep','ECAT Prep']}
        function groupEnrollmentsByClass(rows){const grouped=new Map();rows.forEach((item)=>{const classKey=item.class||'Other';if(!grouped.has(classKey))grouped.set(classKey,[]);grouped.get(classKey).push(item)});return grouped}
        function orderedClassKeys(grouped){const order=classOrderList();return [...order.filter((className)=>grouped.has(className)),...Array.from(grouped.keys()).filter((className)=>!order.includes(className))]}
        function enrollmentSearchText(item){return [item.roll_number,item.name,item.father_name,item.father_contact,item.gender,item.email,item.date_of_birth,item.mobile,item.cnic,item.class,item.group,(item.subjects||[]).join(', ')].join(' ').toLowerCase()}
        function filterEnrollmentRows(rows,query,className=''){return rows.filter((item)=>(!query||enrollmentSearchText(item).includes(query))&&(!className||(item.class||'')===className))}
        function renderEnrollmentClassTabs(){
            if(!enrollmentClassTabs)return;
            const activeClass=String(enrollmentClassFilter.value||'').trim();
            const counts=new Map();
            state.pendingEnrollments.forEach((item)=>{const className=String(item.class||'Other').trim()||'Other';counts.set(className,(counts.get(className)||0)+1)});
            const tabItems=[{value:'',label:'All Enrollments',count:state.pendingEnrollments.length},...classOrderList().map((className)=>({value:className,label:className,count:counts.get(className)||0}))];
            enrollmentClassTabs.innerHTML=tabItems.map((item)=>`
                <button class="class-tab ${item.value===activeClass?'active':''}" type="button" data-class-tab="${escapeHtml(item.value)}" aria-pressed="${item.value===activeClass?'true':'false'}">
                    <span>${escapeHtml(item.label)}</span>
                    <span class="class-tab-count">${escapeHtml(item.count)}</span>
                    <span class="class-tab-mark">Selected</span>
                </button>
            `).join('');
        }
        function renderAdmissionRecordTabs(){
            if(!admissionRecordsClassTabs)return;
            const activeClass=String(admissionRecordsClassFilter.value||'').trim();
            const counts=new Map();
            [...state.confirmedEnrollments,...state.rejectedEnrollments].forEach((item)=>{const className=String(item.class||'Other').trim()||'Other';counts.set(className,(counts.get(className)||0)+1)});
            const tabItems=[{value:'',label:'All Records',count:[...state.confirmedEnrollments,...state.rejectedEnrollments].length},...classOrderList().map((className)=>({value:className,label:className,count:counts.get(className)||0}))];
            admissionRecordsClassTabs.innerHTML=tabItems.map((item)=>`
                <button class="class-tab ${item.value===activeClass?'active':''}" type="button" data-record-class-tab="${escapeHtml(item.value)}" aria-pressed="${item.value===activeClass?'true':'false'}">
                    <span>${escapeHtml(item.label)}</span>
                    <span class="class-tab-count">${escapeHtml(item.count)}</span>
                    <span class="class-tab-mark">Selected</span>
                </button>
            `).join('');
        }
        function getPendingFilteredRows(){const quickQuery=normalizeSearchValue(enrollmentSearch.value);const specificFilters={name:normalizeSearchValue(enrollmentNameSearch.value),roll:normalizeSearchValue(enrollmentRollSearch.value),cnic:normalizeSearchValue(enrollmentCnicSearch.value)};let rows=filterEnrollmentRows(state.pendingEnrollments,quickQuery,enrollmentClassFilter.value);rows=rows.filter((item)=>rowMatchesSpecificFields(item,specificFilters));rows=filterEnrollmentRowsByCustomRange(rows,enrollmentFrom.value||'',enrollmentTo.value||'','date');return sortEnrollmentRows(rows,enrollmentSort.value||'new_to_old','date')}
        function getAdmissionRecordFilteredRows(){const quickQuery=normalizeSearchValue(admissionRecordsSearch.value);const specificFilters={name:normalizeSearchValue(admissionRecordsNameSearch.value),roll:normalizeSearchValue(admissionRecordsRollSearch.value),cnic:normalizeSearchValue(admissionRecordsCnicSearch.value)};const classValue=admissionRecordsClassFilter.value;const statusValue=admissionRecordsStatusFilter.value;let confirmedRows=filterEnrollmentRows(state.confirmedEnrollments,quickQuery,classValue).filter((item)=>rowMatchesSpecificFields(item,specificFilters));let rejectedRows=filterEnrollmentRows(state.rejectedEnrollments,quickQuery,classValue).filter((item)=>rowMatchesSpecificFields(item,specificFilters));confirmedRows=filterEnrollmentRowsByCustomRange(confirmedRows,admissionRecordsFrom.value||'',admissionRecordsTo.value||'','confirmed_at');rejectedRows=filterEnrollmentRowsByCustomRange(rejectedRows,admissionRecordsFrom.value||'',admissionRecordsTo.value||'','rejected_at');confirmedRows=sortEnrollmentRows(confirmedRows,admissionRecordsSort.value||'new_to_old','confirmed_at');rejectedRows=sortEnrollmentRows(rejectedRows,admissionRecordsSort.value||'new_to_old','rejected_at');if(statusValue==='confirmed')rejectedRows=[];if(statusValue==='rejected')confirmedRows=[];return {confirmedRows,rejectedRows,allRows:[...confirmedRows,...rejectedRows]}}
        function getSelectedRows(rows,selectionSet){const ids=new Set(Array.from(selectionSet).map(String));return (rows||[]).filter((item)=>ids.has(String(item.id)))}
        async function runBulkEnrollmentAction(action,selectionSet,label){
            const ids=Array.from(selectionSet);
            if(!ids.length){toastMessage(`Please select at least one ${label}.`,'error');return false}
            const actionLabels={confirm:'approve',reject:'reject',delete:'delete'};
            const approved=await confirmAction(`Do you want to ${actionLabels[action]||action} ${ids.length} selected ${label}?`,{
                title:'Please confirm this bulk action',
                badge:'Bulk Update',
                mark:action==='delete'?'DEL':action==='reject'?'RJ':'OK',
                tone:action==='delete'||action==='reject'?'danger':'primary',
                confirmLabel:action==='delete'?'Yes, Delete':'Yes, Continue'
            });
            if(!approved)return false;
            await api('__ADMIN_PATH__/enrollments/bulk',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids,action})});
            selectionSet.clear();
            return true
        }
        function clearPendingFilters(){enrollmentNameSearch.value='';enrollmentRollSearch.value='';enrollmentCnicSearch.value='';enrollmentSearch.value='';enrollmentClassFilter.value='';enrollmentFrom.value='';enrollmentTo.value='';enrollmentSort.value='new_to_old';selectionState.pending.clear();renderEnrollments()}
        function clearRecordFilters(){admissionRecordsNameSearch.value='';admissionRecordsRollSearch.value='';admissionRecordsCnicSearch.value='';admissionRecordsSearch.value='';admissionRecordsClassFilter.value='';admissionRecordsStatusFilter.value='';admissionRecordsFrom.value='';admissionRecordsTo.value='';admissionRecordsSort.value='new_to_old';selectionState.records.clear();renderAdmissionRecords()}
        function sortableEnrollmentTime(value){const timestamp=new Date(value||'').getTime();return Number.isNaN(timestamp)?0:timestamp}
        function sortEnrollmentRows(rows,sortOrder='new_to_old',primaryField='date'){return [...rows].sort((left,right)=>{const leftTime=sortableEnrollmentTime(left[primaryField]||left.date);const rightTime=sortableEnrollmentTime(right[primaryField]||right.date);if(leftTime!==rightTime)return sortOrder==='old_to_new'?leftTime-rightTime:rightTime-leftTime;return String(left.name||'').localeCompare(String(right.name||''),'en',{sensitivity:'base'})})}
        function parseAdminDateTimeValue(value){
            const normalized=String(value||'').trim();
            if(!normalized)return null;
            const timestamp=new Date(normalized).getTime();
            return Number.isNaN(timestamp)?null:timestamp
        }
        function filterEnrollmentRowsByCustomRange(rows,fromValue='',toValue='',primaryField='date'){
            let startTime=parseAdminDateTimeValue(fromValue);
            let endTime=parseAdminDateTimeValue(toValue);
            if(startTime!==null&&endTime!==null&&endTime<startTime){
                const temp=startTime;
                startTime=endTime;
                endTime=temp;
            }
            if(startTime===null&&endTime===null)return rows;
            return rows.filter((item)=>{
                const itemTime=sortableEnrollmentTime(item[primaryField]||item.date);
                if(startTime!==null&&itemTime<startTime)return false;
                if(endTime!==null&&itemTime>endTime)return false;
                return true
            })
        }
        function syncClassFilterOptions(selectElement,rows,allLabel='All Classes'){if(!selectElement)return;const currentValue=selectElement.value;const options=orderedClassKeys(groupEnrollmentsByClass(rows));if(currentValue&&!options.includes(currentValue))options.push(currentValue);selectElement.innerHTML=[`<option value="">${escapeHtml(allLabel)}</option>`,...options.map((className)=>`<option value="${escapeHtml(className)}">${escapeHtml(className)}</option>`)].join('');selectElement.value=currentValue||''}
        function resetFacultyPreview(){if(facultyCropState.previewUrl)URL.revokeObjectURL(facultyCropState.previewUrl);facultyCropState.previewUrl='';facultyPhotoPreview.removeAttribute('src');facultyPhotoPreviewWrap.classList.add('hidden')}
        function resetFacultyCurrentPhoto(){if(!facultyCurrentPhotoWrap)return;facultyCurrentPhoto.removeAttribute('src');facultyCurrentPhotoWrap.classList.add('hidden');facultyRemovePhoto.checked=false}
        function showFacultyCurrentPhoto(source){if(!facultyCurrentPhotoWrap)return;if(!source){resetFacultyCurrentPhoto();return}facultyCurrentPhoto.src=source;facultyCurrentPhotoWrap.classList.remove('hidden');facultyRemovePhoto.checked=false}
        function closeFacultyCropModal(){facultyCropModal.classList.add('hidden');syncAdminModalState()}
        function clearFacultyCropSource(){if(facultyCropState.sourceUrl)URL.revokeObjectURL(facultyCropState.sourceUrl);facultyCropState.sourceUrl='';facultyCropState.file=null;facultyCropState.image=null}
        function updateFacultyPreviewFromFile(file){resetFacultyPreview();facultyCropState.previewUrl=URL.createObjectURL(file);facultyPhotoPreview.src=facultyCropState.previewUrl;facultyPhotoPreviewWrap.classList.remove('hidden')}
        function assignFacultyFile(file){const transfer=new DataTransfer();transfer.items.add(file);facultyPhotoInput.files=transfer.files}
        function updateCropRanges(){if(!facultyCropState.image)return;const minDimension=Math.min(facultyCropState.image.naturalWidth,facultyCropState.image.naturalHeight);const cropSize=Math.max(140,Math.round(minDimension/facultyCropState.zoom));const maxX=Math.max(0,facultyCropState.image.naturalWidth-cropSize);const maxY=Math.max(0,facultyCropState.image.naturalHeight-cropSize);facultyCropX.max=String(maxX);facultyCropY.max=String(maxY);facultyCropState.offsetX=Math.min(facultyCropState.offsetX,maxX);facultyCropState.offsetY=Math.min(facultyCropState.offsetY,maxY);facultyCropX.value=String(facultyCropState.offsetX);facultyCropY.value=String(facultyCropState.offsetY);facultyCropState.baseWidth=cropSize;facultyCropState.baseHeight=cropSize}
        function drawFacultyCropCanvas(){if(!facultyCropState.image)return;updateCropRanges();const context=facultyCropCanvas.getContext('2d');context.clearRect(0,0,facultyCropCanvas.width,facultyCropCanvas.height);context.fillStyle='#ffffff';context.fillRect(0,0,facultyCropCanvas.width,facultyCropCanvas.height);context.drawImage(facultyCropState.image,facultyCropState.offsetX,facultyCropState.offsetY,facultyCropState.baseWidth,facultyCropState.baseHeight,0,0,facultyCropCanvas.width,facultyCropCanvas.height)}
        function openFacultyCropper(file){if(!file)return;if(!['image/jpeg','image/png'].includes(file.type)){toastMessage('Faculty photo must be a JPG or PNG image.','error');facultyPhotoInput.value='';resetFacultyPreview();return}clearFacultyCropSource();facultyCropState.file=file;facultyCropState.sourceUrl=URL.createObjectURL(file);const image=new Image();image.onload=()=>{facultyCropState.image=image;facultyCropState.zoom=1;facultyCropState.offsetX=0;facultyCropState.offsetY=0;facultyCropZoom.value='100';drawFacultyCropCanvas();facultyCropModal.classList.remove('hidden');syncAdminModalState()};image.onerror=()=>{toastMessage('Unable to open the selected image for cropping.','error');clearFacultyCropSource();facultyPhotoInput.value='';resetFacultyPreview()};image.src=facultyCropState.sourceUrl}
        function findEnrollmentRecordById(id){return [...state.pendingEnrollments,...state.confirmedEnrollments,...state.rejectedEnrollments].find((item)=>String(item.id)===String(id))}
        function resetEnrollmentEditPreview(){if(enrollmentEditState.previewUrl)URL.revokeObjectURL(enrollmentEditState.previewUrl);enrollmentEditState.previewUrl='';editEnrollmentPhotoPreview.removeAttribute('src');editEnrollmentPhotoPreviewWrap.classList.add('hidden')}
        function showEnrollmentEditPreview(source,isObjectUrl=false){resetEnrollmentEditPreview();if(!source)return;if(isObjectUrl)enrollmentEditState.previewUrl=source;editEnrollmentPhotoPreview.src=source;editEnrollmentPhotoPreviewWrap.classList.remove('hidden')}
        function currentEnrollmentEditGroupValue(){const selected=editEnrollmentGroupOptions.querySelector('input[name="editEnrollmentGroupChoice"]:checked');return selected?selected.value:''}
        function collectEnrollmentEditSubjects(){return Array.from(editEnrollmentSubjectOptions.querySelectorAll('[data-enrollment-subject]:not([data-all-subjects])')).filter((checkbox)=>checkbox.checked).map((checkbox)=>checkbox.value)}
        function buildEnrollmentEditChoices(subjects){const selected=new Set(enrollmentEditState.selectedSubjects||[]);const allChecked=subjects.length&&subjects.every((subject)=>selected.has(subject));return `<div class="option-grid"><label class="choice"><input type="checkbox" data-enrollment-subject data-all-subjects value="All Subjects" ${allChecked?'checked':''}><span>All Subjects</span></label>${subjects.map((subject)=>`<label class="choice"><input type="checkbox" data-enrollment-subject value="${escapeHtml(subject)}" ${selected.has(subject)?'checked':''}><span>${escapeHtml(subject)}</span></label>`).join('')}</div>`}
        function attachEnrollmentEditSubjectEvents(){const allBox=editEnrollmentSubjectOptions.querySelector('[data-all-subjects]');const subjectBoxes=Array.from(editEnrollmentSubjectOptions.querySelectorAll('[data-enrollment-subject]:not([data-all-subjects])'));if(!allBox)return;allBox.addEventListener('change',()=>{subjectBoxes.forEach((box)=>{box.checked=allBox.checked});enrollmentEditState.selectedSubjects=collectEnrollmentEditSubjects()});subjectBoxes.forEach((box)=>box.addEventListener('change',()=>{allBox.checked=subjectBoxes.every((item)=>item.checked);enrollmentEditState.selectedSubjects=collectEnrollmentEditSubjects()}));enrollmentEditState.selectedSubjects=collectEnrollmentEditSubjects()}
        function renderEnrollmentEditSubjects(){const classValue=editEnrollmentClass.value;if(!classValue){editEnrollmentGroupField.classList.add('hidden');editEnrollmentGroupOptions.innerHTML='';editEnrollmentSubjectOptions.innerHTML='<div class="empty">Select a class to view available subjects.</div>';return}if(classValue==='IX'||classValue==='X'){editEnrollmentGroupField.classList.add('hidden');editEnrollmentGroupOptions.innerHTML='';editEnrollmentSubjectOptions.innerHTML=buildEnrollmentEditChoices(enrollmentSubjectCatalog[classValue]);attachEnrollmentEditSubjectEvents();return}if(classValue==='MDCAT Prep'||classValue==='ECAT Prep'){editEnrollmentGroupField.classList.add('hidden');editEnrollmentGroupOptions.innerHTML='';enrollmentEditState.group=classValue==='MDCAT Prep'?'Pre-Medical':'Pre-Engineering';editEnrollmentSubjectOptions.innerHTML=buildEnrollmentEditChoices(enrollmentSubjectCatalog[classValue]);attachEnrollmentEditSubjectEvents();return}editEnrollmentGroupField.classList.remove('hidden');editEnrollmentGroupOptions.innerHTML=`<div class="option-grid"><label class="choice"><input type="radio" name="editEnrollmentGroupChoice" value="Pre-Medical" ${enrollmentEditState.group==='Pre-Medical'?'checked':''}><span>Pre-Medical</span></label><label class="choice"><input type="radio" name="editEnrollmentGroupChoice" value="Pre-Engineering" ${enrollmentEditState.group==='Pre-Engineering'?'checked':''}><span>Pre-Engineering</span></label></div>`;const activeGroup=currentEnrollmentEditGroupValue();if(!activeGroup){editEnrollmentSubjectOptions.innerHTML='<div class="empty">Select a group to load the subject list.</div>'}else{enrollmentEditState.group=activeGroup;editEnrollmentSubjectOptions.innerHTML=buildEnrollmentEditChoices(enrollmentSubjectCatalog[classValue][activeGroup]||[]);attachEnrollmentEditSubjectEvents()}editEnrollmentGroupOptions.querySelectorAll('input[name="editEnrollmentGroupChoice"]').forEach((radio)=>radio.addEventListener('change',()=>{enrollmentEditState.group=radio.value;enrollmentEditState.selectedSubjects=[];editEnrollmentSubjectOptions.innerHTML=buildEnrollmentEditChoices(enrollmentSubjectCatalog[classValue][radio.value]||[]);attachEnrollmentEditSubjectEvents()}))}
        function resetEnrollmentEditForm(){enrollmentEditForm.reset();editEnrollmentId.value='';enrollmentEditTitle.textContent='Edit Enrollment';enrollmentEditState.selectedSubjects=[];enrollmentEditState.group='';enrollmentEditState.originalPhotoUrl='';enrollmentEditState.studentId='';resetEnrollmentEditPreview();editEnrollmentGroupField.classList.add('hidden');editEnrollmentGroupOptions.innerHTML='';editEnrollmentSubjectOptions.innerHTML='<div class="empty">Select a class to view available subjects.</div>';enrollmentEditModal.classList.add('hidden');syncAdminModalState()}
        function openEnrollmentEditModal(item){if(!item)return;resetEnrollmentEditForm();editEnrollmentId.value=item.id;enrollmentEditState.studentId=String(item.id);enrollmentEditTitle.textContent=`Edit Enrollment - ${item.name}`;editFullName.value=item.name||'';editFatherName.value=item.father_name||'';editMobile.value=item.mobile||'';editCnic.value=item.cnic||'';editFatherContact.value=item.father_contact||'';editGender.value=item.gender||'';editEmail.value=item.email||'';editDateOfBirth.value=formatEnrollmentDateValue(item.date_of_birth||'');editEnrollmentClass.value=item.class||'';editEnrollmentAddress.value=item.address||'';editEnrollmentPhoto.value='';enrollmentEditState.group=item.group||'';enrollmentEditState.selectedSubjects=[...(item.subjects||[])];enrollmentEditState.originalPhotoUrl=item.photo_url||'';if(enrollmentEditState.originalPhotoUrl)showEnrollmentEditPreview(enrollmentEditState.originalPhotoUrl);renderEnrollmentEditSubjects();enrollmentEditModal.classList.remove('hidden');syncAdminModalState()}

        function formatAnalyticsSectionLabel(value){
            const labels={home:'Home',enrollment:'Enrollment',results:'Results',faculty:'Faculty',announcements:'Announcements',about:'About Us','status-check':'Status Check'};
            return labels[String(value||'').trim()]||'Home';
        }

        function renderActivityLog(){
            if(!activityLogList)return;
            activityLogList.innerHTML=state.activityLog.length?state.activityLog.map((item)=>`
                <div class="history-item">
                    <div class="manage-chip-row">
                        <span class="manage-chip gold">${escapeHtml(formatDateTime(item.created_at))}</span>
                        <span class="manage-chip">${escapeHtml(item.admin_username||'Admin')}</span>
                        <span class="manage-chip navy">${escapeHtml(String(item.action_type||'general').replace(/_/g,' '))}</span>
                    </div>
                    <strong>${escapeHtml(item.action_summary||'Admin activity')}</strong>
                    <p>${escapeHtml(item.target_type?`${item.target_type} ${item.target_id||''}`.trim():'System action')}</p>
                </div>
            `).join(''):'<div class="empty">No admin activity has been recorded yet.</div>';
        }

        function getFilteredMessages(){
            const query=String(messageSearch&&messageSearch.value||'').trim().toLowerCase();
            const statusValue=String(messageStatusFilter&&messageStatusFilter.value||'').trim();
            return (state.messages||[]).filter((item)=>{
                if(statusValue==='unread'&&item.is_read)return false;
                if(statusValue==='read'&&!item.is_read)return false;
                if(!query)return true;
                const searchable=[item.full_name,item.email,item.mobile,item.message].join(' ').toLowerCase();
                return searchable.includes(query);
            });
        }

        function renderMessages(){
            if(!messageList)return;
            const filteredMessages=getFilteredMessages();
            const unreadCount=(state.messages||[]).filter((item)=>!item.is_read).length;
            if(messageSummaryCount)messageSummaryCount.textContent=`${filteredMessages.length} shown | ${unreadCount} unread`;
            messageList.innerHTML=filteredMessages.length?filteredMessages.map((item)=>`
                <div class="manage-card">
                    <div class="manage-card-header">
                        <div class="manage-card-copy">
                            <h4>${escapeHtml(item.full_name)}</h4>
                            <p>${escapeHtml(item.message)}</p>
                        </div>
                        <div class="manage-chip-row">
                            <span class="manage-chip ${item.is_read?'draft':'gold'}">${item.is_read?'Handled':'Unread'}</span>
                            <span class="manage-chip navy">${escapeHtml(formatDateTime(item.created_at))}</span>
                        </div>
                    </div>
                    <div class="record-grid mb-12">
                        <div class="record-line"><strong>Email</strong><span>${escapeHtml(item.email)}</span></div>
                        <div class="record-line"><strong>Mobile No</strong><span>${escapeHtml(item.mobile)}</span></div>
                    </div>
                    <div class="manage-actions">
                        <button class="btn btn-soft message-toggle-read" type="button" data-id="${escapeHtml(item.id)}" data-read="${item.is_read?'0':'1'}">${item.is_read?'Mark Unread':'Mark Handled'}</button>
                        <button class="btn btn-danger message-delete" type="button" data-id="${escapeHtml(item.id)}">Delete Message</button>
                    </div>
                </div>
            `).join(''):'<div class="empty">No website messages match the current search or status filter.</div>';
        }

        function renderOverview(){
            const unreadMessages=state.messages.filter((item)=>!item.is_read).length;
            const stats=[['Pending Enrollments',state.pendingEnrollments.length],['Confirmed Admissions',state.confirmedEnrollments.length],['Unread Messages',unreadMessages],['Rejected Admissions',state.rejectedEnrollments.length],['Faculty Members',state.faculty.length],['Results',state.results.length]];
            statsGrid.innerHTML=stats.map(([label,value])=>`<div class="stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
            renderSidebarSnapshot();
            recentAnnouncements.innerHTML=state.announcements.length?state.announcements.slice(0,4).map((item)=>`<div class="manage-card"><div class="manage-card-header"><div class="manage-card-copy"><h4>${escapeHtml(item.title)}</h4><p>${escapeHtml(item.description)}</p></div><div class="manage-chip-row"><span class="manage-chip ${item.is_published?'gold':'draft'}">${item.is_published?'Live':'Draft'}</span></div></div><div class="manage-chip-row"><span class="manage-chip">${escapeHtml(formatDate(item.date))}</span></div></div>`).join(''):'<div class="empty">No announcements available yet.</div>';
            const liveWindowMinutes=Number(state.insights.live_window_minutes||3);
            const liveVisitorsNow=Number(state.insights.live_visitors_now||0);
            const insightStats=[['Live Now',liveVisitorsNow],['New Enrollments',state.insights.enrollments_last_7_days||0],['Confirmed',state.insights.confirmed_last_7_days||0],['Rejected',state.insights.rejected_last_7_days||0],['Page Views',state.insights.page_views_last_7_days||0],['Unique Visitors',state.insights.unique_visitors_last_7_days||0],['Announcements',state.insights.announcements_last_7_days||0],['Results Uploaded',state.insights.results_last_7_days||0]];
            insightsGrid.innerHTML=insightStats.map(([label,value])=>`<div class="stat"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
            insightsTimeline.innerHTML=(state.insights.daily_breakdown||[]).length?(state.insights.daily_breakdown||[]).map((item)=>`<div class="timeline-card"><div class="manage-chip-row"><span class="manage-chip navy">${escapeHtml(formatDate(item.day))}</span></div><p>Visits: ${escapeHtml(item.visits||0)} | Enrollments: ${escapeHtml(item.enrollments)} | Confirmed: ${escapeHtml(item.confirmed)} | Rejected: ${escapeHtml(item.rejected||0)}</p></div>`).join(''):'<div class="empty">No activity recorded during the last 7 days.</div>';
            const liveSectionsCard=`<div class="manage-card"><div class="panel-head"><div><h4>Currently Live On Website</h4><p>${escapeHtml(liveVisitorsNow)} visitor(s) active during the last ${escapeHtml(liveWindowMinutes)} minute(s).</p></div></div><div class="stack">${(state.insights.live_sections||[]).length?(state.insights.live_sections||[]).map((item)=>`<div class="record-line"><strong>${escapeHtml(formatAnalyticsSectionLabel(item.section))}</strong><span>${escapeHtml(item.visitors||0)} live visitor(s)</span></div>`).join(''):'<div class="empty">No public visitors are active right now.</div>'}</div></div>`;
            const mostViewedCard=(state.insights.top_sections||[]).length?`<div class="manage-card"><div class="panel-head"><div><h4>Most Viewed Sections</h4><p>Public visitors moved most frequently through these sections during the last 7 days.</p></div></div><div class="stack">${(state.insights.top_sections||[]).map((item)=>`<div class="record-line"><strong>${escapeHtml(formatAnalyticsSectionLabel(item.section))}</strong><span>${escapeHtml(item.views||0)} view(s)</span></div>`).join('')}</div></div>`:'<div class="empty">No visitor activity recorded for section analytics yet.</div>';
            insightsSectionList.innerHTML=`${liveSectionsCard}${mostViewedCard}`;
            renderActivityLog();
            renderMessages();
        }

        async function refreshInsights(silent=true){
            try{
                state.insights=await api('__ADMIN_PATH__/insights');
                renderOverview();
            }catch(error){
                if(!silent)toastMessage(error.message,'error');
            }
        }

        async function refreshAdminLiveData(silent=true){
            try{
                const [enrollmentData,insights,activityLog,messages]=await Promise.all([
                    api('__ADMIN_PATH__/enrollments'),
                    api('__ADMIN_PATH__/insights'),
                    api('__ADMIN_PATH__/activity-log'),
                    api('__ADMIN_PATH__/messages')
                ]);
                state.pendingEnrollments=enrollmentData.pending||[];
                state.confirmedEnrollments=enrollmentData.confirmed||[];
                state.rejectedEnrollments=enrollmentData.rejected||[];
                state.insights=insights||{};
                state.activityLog=activityLog||[];
                state.messages=messages||[];
                renderOverview();
                renderEnrollments();
                renderAdmissionRecords();
            }catch(error){
                if(!silent)toastMessage(error.message,'error');
            }
        }

        function startInsightsRefresh(){
            if(insightsRefreshTimer)clearInterval(insightsRefreshTimer);
            insightsRefreshTimer=setInterval(()=>{if(!document.hidden)refreshAdminLiveData(true)},5000);
            if(adminRefreshListenersBound)return;
            adminRefreshListenersBound=true;
            document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refreshAdminLiveData(true)});
            window.addEventListener('focus',()=>refreshAdminLiveData(true));
            window.addEventListener('storage',(event)=>{
                if(event.key!==adminSyncKey||!event.newValue)return;
                try{handleAdminSyncMessage(JSON.parse(event.newValue))}catch(_error){}
            });
            if(crossPageSyncChannel)crossPageSyncChannel.addEventListener('message',(event)=>handleAdminSyncMessage(event.data));
        }

        function stopInsightsRefresh(){
            if(!insightsRefreshTimer)return;
            clearInterval(insightsRefreshTimer);
            insightsRefreshTimer=null;
        }

        function renderEnrollments(){
            syncClassFilterOptions(enrollmentClassFilter,state.pendingEnrollments,'All Pending Classes');
            renderEnrollmentClassTabs();
            const pendingRows=getPendingFilteredRows();
            pruneSelection(selectionState.pending,pendingRows);
            setSelectionCount(pendingSelectionCount,selectionState.pending,'pending students');
            syncSelectAllState(selectAllPending,pendingRows,selectionState.pending);
            const groupedPendingRows=groupEnrollmentsByClass(pendingRows);
            const classKeys=orderedClassKeys(groupedPendingRows);
            enrollmentClassSections.innerHTML=classKeys.length?classKeys.map((className)=>{
                const classRows=groupedPendingRows.get(className)||[];
                return `
                    <div class="boxed">
                        <div class="panel-head mb-12">
                            <div>
                                <h4>${escapeHtml(className)}</h4>
                                <p>${escapeHtml(classRows.length)} pending enrollment(s) in this class section.</p>
                            </div>
                            <div class="row-actions">
                                <button class="btn btn-soft class-export" type="button" data-class="${escapeHtml(className)}">Export Class CSV</button>
                            </div>
                        </div>
                        <div class="pending-card-grid">
                            ${classRows.map((item)=>`
                                <div class="pending-card manage-card">
                                    <div class="panel-head mb-12">
                                        <div>
                                            <label class="choice compact-choice slim-choice">
                                                <input class="pending-select" type="checkbox" data-id="${escapeHtml(item.id)}" ${selectionState.pending.has(String(item.id))?'checked':''}>
                                                <span>Select Student</span>
                                            </label>
                                            <h5>${escapeHtml(item.name)}</h5>
                                            <p>Roll No: ${escapeHtml(item.roll_number||'N/A')}</p>
                                        </div>
                                        ${item.photo_url?`<a class="btn btn-soft" href="${escapeHtml(item.photo_url)}" target="_blank" rel="noreferrer">View Photo</a>`:''}
                                    </div>
                                    <div class="record-grid">
                                        <div class="record-line"><strong>Father Name</strong><span>${escapeHtml(item.father_name)}</span></div>
                                        <div class="record-line"><strong>Father Contact</strong><span>${escapeHtml(item.father_contact||'N/A')}</span></div>
                                        <div class="record-line"><strong>Gender</strong><span>${escapeHtml(item.gender||'N/A')}</span></div>
                                        <div class="record-line"><strong>Email</strong><span>${escapeHtml(item.email||'N/A')}</span></div>
                                        <div class="record-line"><strong>Date of Birth</strong><span>${formatDate(item.date_of_birth)}</span></div>
                                        <div class="record-line"><strong>Student Contact</strong><span>${escapeHtml(item.mobile||'N/A')}</span></div>
                                        <div class="record-line"><strong>Class</strong><span>${escapeHtml(item.class)}${item.group?` | ${escapeHtml(item.group)}`:''}</span></div>
                                        <div class="record-line"><strong>CNIC</strong><span>${escapeHtml(item.cnic||'N/A')}</span></div>
                                        <div class="record-line"><strong>Subjects</strong><span>${escapeHtml((item.subjects||[]).join(', ')||'N/A')}</span></div>
                                        <div class="record-line"><strong>Submitted On</strong><span>${formatDateTime(item.date)}</span></div>
                                    </div>
                                    <div class="row-actions record-actions">
                                        <button class="btn btn-soft enrollment-edit" type="button" data-id="${escapeHtml(item.id)}">Edit</button>
                                        <button class="btn btn-primary enrollment-confirm" type="button" data-id="${escapeHtml(item.id)}">Approve</button>
                                        <button class="btn btn-soft enrollment-reject" type="button" data-id="${escapeHtml(item.id)}">Reject</button>
                                        <button class="btn btn-danger enrollment-delete" type="button" data-id="${escapeHtml(item.id)}">Delete</button>
                                    </div>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                `;
            }).join(''):'<div class="empty">No pending enrollments found for the current filters.</div>';
        }

        function buildAdmissionRecordCard(item,status){
            const isConfirmed=status==='confirmed';
            const statusLabel=isConfirmed?'Confirmed':'Rejected';
            const statusDate=isConfirmed?item.confirmed_at:item.rejected_at;
            const deleteClass=isConfirmed?'confirmed-enrollment-delete':'rejected-enrollment-delete';
            const formActions=isConfirmed?`
                <a class="btn btn-soft" href="__ADMIN_PATH__/enrollment/${escapeHtml(item.id)}/form" target="_blank" rel="noreferrer">Open Form</a>
                <a class="btn btn-soft" href="__ADMIN_PATH__/enrollment/${escapeHtml(item.id)}/form?download=pdf">Download Form PDF</a>
            `:'';
            return `
                <div class="card manage-card">
                    <div class="panel-head mb-12">
                        <div>
                            <label class="choice compact-choice slim-choice">
                                <input class="record-select" type="checkbox" data-id="${escapeHtml(item.id)}" ${selectionState.records.has(String(item.id))?'checked':''}>
                                <span>Select Record</span>
                            </label>
                            <h4>${escapeHtml(item.name)}</h4>
                            <p>Roll No: ${escapeHtml(item.roll_number||'N/A')}</p>
                        </div>
                        <div class="record-line"><strong>${statusLabel}</strong><span>${formatDateTime(statusDate)}</span></div>
                    </div>
                    <div class="record-grid">
                        <div class="record-line"><strong>Father Name</strong><span>${escapeHtml(item.father_name)}</span></div>
                        <div class="record-line"><strong>Father Contact</strong><span>${escapeHtml(item.father_contact||'N/A')}</span></div>
                        <div class="record-line"><strong>Gender</strong><span>${escapeHtml(item.gender||'N/A')}</span></div>
                        <div class="record-line"><strong>Email</strong><span>${escapeHtml(item.email||'N/A')}</span></div>
                        <div class="record-line"><strong>Date of Birth</strong><span>${formatDate(item.date_of_birth)}</span></div>
                        <div class="record-line"><strong>Class</strong><span>${escapeHtml(item.class)}${item.group?` | ${escapeHtml(item.group)}`:''}</span></div>
                        <div class="record-line"><strong>Subjects</strong><span>${escapeHtml((item.subjects||[]).join(', ')||'N/A')}</span></div>
                        <div class="record-line"><strong>Student Contact</strong><span>${escapeHtml(item.mobile||'N/A')}</span></div>
                    </div>
                    <div class="row-actions record-actions mt-12">
                        ${item.photo_url?`<a class="btn btn-soft" href="${escapeHtml(item.photo_url)}" target="_blank" rel="noreferrer">View Photo</a>`:''}
                        <button class="btn btn-soft enrollment-edit" type="button" data-id="${escapeHtml(item.id)}">Edit</button>
                        ${formActions}
                        <button class="btn btn-danger ${deleteClass}" type="button" data-id="${escapeHtml(item.id)}">Delete</button>
                    </div>
                </div>
            `;
        }

        function renderAdmissionRecordGroup(title,description,rows,status,className){
            const exportLabel=status==='confirmed'?'Confirmed List':'Rejected List';
            const formButton=status==='confirmed'?`<button class="btn btn-soft record-class-confirmed-forms" type="button" data-class="${escapeHtml(className)}">Class Forms PDF</button>`:'';
            return `
                <div class="manage-card">
                    <div class="panel-head mb-12">
                        <div>
                            <h4>${escapeHtml(title)}</h4>
                            <p>${escapeHtml(description)}</p>
                        </div>
                        <div class="row-actions">
                            <button class="btn btn-soft record-class-export" type="button" data-status="${escapeHtml(status)}" data-class="${escapeHtml(className)}">${exportLabel}</button>
                            ${formButton}
                        </div>
                    </div>
                    <div class="stack">
                        ${rows.length?rows.map((item)=>buildAdmissionRecordCard(item,status)).join(''):`<div class="empty">No ${escapeHtml(status)} admissions are available in this class section for the current filters.</div>`}
                    </div>
                </div>
            `;
        }

        function renderAdmissionRecords(){
            syncClassFilterOptions(admissionRecordsClassFilter,[...state.confirmedEnrollments,...state.rejectedEnrollments],'All Class Sections');
            renderAdmissionRecordTabs();
            const {confirmedRows,rejectedRows,allRows}=getAdmissionRecordFilteredRows();
            pruneSelection(selectionState.records,allRows);
            setSelectionCount(recordSelectionCount,selectionState.records,'final records');
            syncSelectAllState(selectAllRecords,allRows,selectionState.records);
            const confirmedGrouped=groupEnrollmentsByClass(confirmedRows);
            const rejectedGrouped=groupEnrollmentsByClass(rejectedRows);
            const unionGrouped=new Map();
            allRows.forEach((item)=>{
                const classKey=item.class||'Other';
                if(!unionGrouped.has(classKey))unionGrouped.set(classKey,[]);
            });
            const classKeys=orderedClassKeys(unionGrouped);
            const statusValue=admissionRecordsStatusFilter.value;
            admissionRecordsClassSections.innerHTML=classKeys.length?classKeys.map((className)=>{
                const classConfirmedRows=confirmedGrouped.get(className)||[];
                const classRejectedRows=rejectedGrouped.get(className)||[];
                const totalConfirmed=classConfirmedRows.length;
                const totalRejected=classRejectedRows.length;
                const totalFinalized=totalConfirmed+totalRejected;
                return `
                    <div class="boxed">
                        <div class="panel-head mb-12">
                            <div>
                                <h3>${escapeHtml(className)}</h3>
                                <p>${escapeHtml(totalFinalized)} finalized admission record(s) in this class section.</p>
                            </div>
                            <div class="manage-chip-row">
                                <span class="manage-chip gold">Confirmed ${escapeHtml(totalConfirmed)}</span>
                                <span class="manage-chip navy">Rejected ${escapeHtml(totalRejected)}</span>
                            </div>
                        </div>
                        <div class="record-grid mb-12">
                            <div class="record-line"><strong>Total Confirmed</strong><span>${escapeHtml(totalConfirmed)}</span></div>
                            <div class="record-line"><strong>Total Rejected</strong><span>${escapeHtml(totalRejected)}</span></div>
                        </div>
                        <div class="stack">
                            ${statusValue!=='rejected'?renderAdmissionRecordGroup('Confirmed Admissions','Confirmed students for this class stay available here for forms and record keeping.',classConfirmedRows,'confirmed',className):''}
                            ${statusValue!=='confirmed'?renderAdmissionRecordGroup('Rejected Admissions','Rejected students for this class remain archived here for review and reporting.',classRejectedRows,'rejected',className):''}
                        </div>
                    </div>
                `;
            }).join(''):'<div class="empty">No admission records found for the current filters.</div>';
        }
""")

ADMIN_HTML_PARTS.append(
    r"""
        function renderAnnouncements(){
            announcementList.innerHTML=state.announcements.length?state.announcements.map((item)=>`
                <div class="manage-card">
                    <div class="manage-card-header">
                        <div class="manage-card-copy">
                            <h4>${escapeHtml(item.title)}</h4>
                            <p>${escapeHtml(item.description)}</p>
                        </div>
                        <div class="manage-chip-row">
                            ${item.is_new?'<span class="manage-chip success">New Badge On</span>':''}
                            <span class="manage-chip ${item.is_published?'gold':'draft'}">${item.is_published?'Showing On Website':'Hidden Draft'}</span>
                            <span class="manage-chip gold">${escapeHtml(formatDate(item.date))}</span>
                        </div>
                    </div>
                    <div class="manage-actions">
                        <button class="btn btn-soft announcement-edit" type="button" data-id="${escapeHtml(item.id)}">Edit</button>
                        <button class="btn btn-danger announcement-delete" type="button" data-id="${escapeHtml(item.id)}">Delete</button>
                    </div>
                </div>
            `).join(''):'<div class="empty">No notices added yet.</div>';
        }

        function renderResults(){
            resultList.innerHTML=state.results.length?state.results.map((item)=>`
                <div class="manage-card">
                    <div class="manage-card-header">
                        <div class="manage-card-copy">
                            <h4>${escapeHtml(item.title)}</h4>
                            <p>Uploaded academic result file ready for public download.</p>
                        </div>
                        <div class="manage-chip-row">
                            ${item.is_new?'<span class="manage-chip success">New Badge On</span>':''}
                            <span class="manage-chip ${item.is_published?'gold':'draft'}">${item.is_published?'Showing On Website':'Hidden Draft'}</span>
                            <span class="manage-chip">${escapeHtml(item.class)}</span>
                            <span class="manage-chip navy">${escapeHtml(item.year)}</span>
                        </div>
                    </div>
                    <div class="manage-chip-row">
                        <span class="manage-chip navy">Uploaded ${escapeHtml(formatDate(item.upload_date))}</span>
                    </div>
                    <div class="manage-actions">
                        <button class="btn btn-soft result-edit" type="button" data-id="${escapeHtml(item.id)}">Edit</button>
                        <a class="btn btn-soft" href="${escapeHtml(item.download_url)}" target="_blank" rel="noreferrer">Open PDF</a>
                        <button class="btn btn-danger result-delete" type="button" data-id="${escapeHtml(item.id)}">Delete</button>
                    </div>
                </div>
            `).join(''):'<div class="empty">No result files uploaded yet.</div>';
        }

        function formatFacultyExperience(value){
            const raw=String(value||'').trim();
            if(!raw)return 'Experienced Faculty';
            return /experience/i.test(raw)?raw:`${raw} Experience`;
        }
        function decodeFacultySectionList(classAssigned){
            const raw=String(classAssigned||'').trim();
            if(!raw)return [];
            try{
                const parsed=JSON.parse(raw);
                if(Array.isArray(parsed))return parsed.map((item)=>String(item||'').trim()).filter(Boolean);
            }catch(_error){}
            return raw.split(',').map((item)=>String(item||'').trim()).filter(Boolean);
        }
        function parseSingleFacultyClassConfig(classAssigned,subject=''){
            const raw=String(classAssigned||'').trim();
            const normalized=raw.replace(/\s+/g,'').toUpperCase();
            const subjectNormalized=String(subject||'').trim().toUpperCase();
            const hasPreMed=['PRE-MED','PREMED','PRE-MEDICAL','PREMEDICAL','P.M'].some((token)=>normalized.includes(token));
            const hasPreEng=['PRE-ENG','PREENG','PRE-ENGINEERING','PREENGINEERING','P.E'].some((token)=>normalized.includes(token));
            const hasMdcat=normalized.includes('MDCAT');
            const hasEcat=normalized.includes('ECAT');
            const isXiXii=normalized.includes('XI-XII')||normalized.endsWith('XI')||normalized.endsWith('XII');
            if(hasPreMed)return{level:'XI-XII',track:'Pre-Med',value:'XI-XII | Pre-Med',label:'XI-XII | Pre-Med'};
            if(hasPreEng)return{level:'XI-XII',track:'Pre-Eng',value:'XI-XII | Pre-Eng',label:'XI-XII | Pre-Eng'};
            if(isXiXii){
                if(/MATH/.test(subjectNormalized))return{level:'XI-XII',track:'Pre-Eng',value:'XI-XII | Pre-Eng',label:'XI-XII | Pre-Eng'};
                if(/BIO|BOTANY|ZOOLOGY/.test(subjectNormalized))return{level:'XI-XII',track:'Pre-Med',value:'XI-XII | Pre-Med',label:'XI-XII | Pre-Med'};
                return{level:'XI-XII',track:'',value:'XI-XII | Pre-Med',label:'XI-XII | Pre-Med / Pre-Eng'};
            }
            if(hasMdcat&&hasEcat)return{level:'MDCAT',track:'',value:'MDCAT',label:'MDCAT / ECAT'};
            if(hasMdcat)return{level:'MDCAT',track:'',value:'MDCAT',label:'MDCAT'};
            if(hasEcat)return{level:'ECAT',track:'',value:'ECAT',label:'ECAT'};
            if(normalized.includes('IX-X')||normalized.endsWith('IX')||normalized.endsWith('X'))return{level:'Class IX-X',track:'',value:'Class IX-X',label:'Class IX-X'};
            return{level:'Class IX-X',track:'',value:'Class IX-X',label:raw||'Class IX-X'};
        }
        function parseFacultyClassConfig(classAssigned,subject=''){
            const sections=decodeFacultySectionList(classAssigned);
            const parsedSections=(sections.length?sections:['Class IX-X']).map((section)=>parseSingleFacultyClassConfig(section,subject));
            const values=[];
            const labels=[];
            parsedSections.forEach((entry)=>{
                if(entry.value&&!values.includes(entry.value))values.push(entry.value);
                if(entry.label&&!labels.includes(entry.label))labels.push(entry.label);
            });
            return {sections:values.length?values:['Class IX-X'],label:labels.join(' | ')||'Class IX-X'};
        }
        function collectFacultySections(){
            return Array.from(document.querySelectorAll('[data-faculty-section]')).filter((checkbox)=>checkbox.checked).map((checkbox)=>checkbox.value);
        }
        function setFacultySectionSelections(values){
            const selected=new Set((Array.isArray(values)&&values.length?values:['Class IX-X']).map((item)=>String(item||'').trim()).filter(Boolean));
            document.querySelectorAll('[data-faculty-section]').forEach((checkbox)=>{checkbox.checked=selected.has(checkbox.value)});
        }
        function syncFacultyClassSelection(){
            const selected=collectFacultySections();
            document.getElementById('facultyClass').value=JSON.stringify(selected.length?selected:['Class IX-X']);
        }
        function renderFaculty(){
            facultyList.innerHTML=state.faculty.length?state.faculty.map((item)=>`
                <div class="manage-card">
                    <div class="faculty">
                        <div class="avatar">${item.photo_url?`<img src="${escapeHtml(item.photo_url)}" alt="${escapeHtml(item.name)}">`:`<span>${escapeHtml(initials(item.name))}</span>`}</div>
                        <div class="manage-card-copy">
                            <h4>${escapeHtml(item.name)}</h4>
                            <p>${escapeHtml(item.qualification)}</p>
                            <div class="manage-chip-row mt-12">
                                <span class="manage-chip">${escapeHtml(item.subject)}</span>
                                <span class="manage-chip gold">${escapeHtml(parseFacultyClassConfig(item.class_assigned,item.subject).label)}</span>
                                <span class="manage-chip navy">${escapeHtml(formatFacultyExperience(item.experience_years))}</span>
                            </div>
                            <small class="muted">Assigned sections: ${escapeHtml(parseFacultyClassConfig(item.class_assigned,item.subject).label)}</small>
                        </div>
                    </div>
                    <div class="manage-actions mt-14">
                        <button class="btn btn-soft faculty-move" type="button" data-id="${escapeHtml(item.id)}" data-direction="up">Up</button>
                        <button class="btn btn-soft faculty-move" type="button" data-id="${escapeHtml(item.id)}" data-direction="down">Down</button>
                        <button class="btn btn-soft faculty-edit" type="button" data-id="${escapeHtml(item.id)}">Edit</button>
                        <button class="btn btn-danger faculty-delete" type="button" data-id="${escapeHtml(item.id)}">Delete</button>
                    </div>
                </div>
            `).join(''):'<div class="empty">No faculty members added yet.</div>';
        }

        function parseEnrollmentClassAllowlist(value){
            const raw=String(value||'').trim();
            if(!raw)return enrollmentClassChoices.slice();
            let parsed=[];
            try{
                const decoded=JSON.parse(raw);
                if(Array.isArray(decoded))parsed=decoded.map((item)=>String(item||'').trim()).filter(Boolean);
            }catch(_error){
                parsed=raw.split(',').map((item)=>String(item||'').trim()).filter(Boolean);
            }
            const allowed=enrollmentClassChoices.filter((item)=>parsed.includes(item));
            return allowed.length?allowed:enrollmentClassChoices.slice();
        }

        function collectEnrollmentClassAllowlist(){
            if(!enrollmentClassScopeOptions)return enrollmentClassChoices.slice();
            const selected=Array.from(enrollmentClassScopeOptions.querySelectorAll('[data-enrollment-class-choice]')).filter((checkbox)=>checkbox.checked).map((checkbox)=>checkbox.value);
            return selected.length?selected:enrollmentClassChoices.slice();
        }

        function syncEnrollmentClassScopeFromSettings(){
            if(!enrollmentClassScopeOptions||!enrollmentClassAll)return;
            const active=new Set(parseEnrollmentClassAllowlist(state.settings.enrollment_class_allowlist||''));
            const boxes=Array.from(enrollmentClassScopeOptions.querySelectorAll('[data-enrollment-class-choice]'));
            boxes.forEach((checkbox)=>{checkbox.checked=active.has(checkbox.value)});
            enrollmentClassAll.checked=boxes.length>0&&boxes.every((checkbox)=>checkbox.checked);
        }

        function wireEnrollmentClassScopeControls(){
            if(!enrollmentClassScopeOptions||!enrollmentClassAll)return;
            const boxes=Array.from(enrollmentClassScopeOptions.querySelectorAll('[data-enrollment-class-choice]'));
            enrollmentClassAll.addEventListener('change',()=>{
                boxes.forEach((checkbox)=>{checkbox.checked=enrollmentClassAll.checked});
            });
            boxes.forEach((checkbox)=>checkbox.addEventListener('change',()=>{
                enrollmentClassAll.checked=boxes.length>0&&boxes.every((item)=>item.checked);
            }));
        }

        function fillSettings(){
            document.getElementById('contactPrimary').value=state.settings.contact_primary||'';
            document.getElementById('contactSecondary').value=state.settings.contact_secondary||'';
            document.getElementById('heroBadge').value=state.settings.hero_badge||'';
            document.getElementById('heroHeading').value=state.settings.hero_heading||'';
            document.getElementById('heroDescription').value=state.settings.hero_description||'';
            document.getElementById('heroOverlayTitle').value=state.settings.hero_overlay_title||'';
            document.getElementById('heroOverlayDescription').value=state.settings.hero_overlay_description||'';
            document.getElementById('motionEnabled').checked=String(state.settings.motion_enabled||'1')==='1';
            document.getElementById('darkModeEnabled').checked=String(state.settings.dark_mode_enabled||'0')==='1';
            document.getElementById('homeStatsEnabled').checked=String(state.settings.home_stats_enabled||'1')==='1';
            document.getElementById('homeAnnouncementsEnabled').checked=String(state.settings.home_announcements_enabled||'1')==='1';
            document.getElementById('homeMessageEnabled').checked=String(state.settings.home_message_enabled||'1')==='1';
            document.getElementById('homeGalleryEnabled').checked=String(state.settings.home_gallery_enabled||'1')==='1';
            document.getElementById('homeFaqEnabled').checked=String(state.settings.home_faq_enabled||'1')==='1';
            document.getElementById('galleryBadge').value=state.settings.gallery_badge||'';
            document.getElementById('galleryHeading').value=state.settings.gallery_heading||'';
            document.getElementById('galleryDescription').value=state.settings.gallery_description||'';
            document.getElementById('galleryItem1Label').value=state.settings.gallery_item_1_label||'';
            document.getElementById('galleryItem1Title').value=state.settings.gallery_item_1_title||'';
            document.getElementById('galleryItem1Description').value=state.settings.gallery_item_1_description||'';
            document.getElementById('galleryItem1Image').value=state.settings.gallery_item_1_image||'';
            document.getElementById('galleryItem2Label').value=state.settings.gallery_item_2_label||'';
            document.getElementById('galleryItem2Title').value=state.settings.gallery_item_2_title||'';
            document.getElementById('galleryItem2Description').value=state.settings.gallery_item_2_description||'';
            document.getElementById('galleryItem2Image').value=state.settings.gallery_item_2_image||'';
            document.getElementById('galleryItem3Label').value=state.settings.gallery_item_3_label||'';
            document.getElementById('galleryItem3Title').value=state.settings.gallery_item_3_title||'';
            document.getElementById('galleryItem3Description').value=state.settings.gallery_item_3_description||'';
            document.getElementById('galleryItem3Image').value=state.settings.gallery_item_3_image||'';
            document.getElementById('galleryItem4Label').value=state.settings.gallery_item_4_label||'';
            document.getElementById('galleryItem4Title').value=state.settings.gallery_item_4_title||'';
            document.getElementById('galleryItem4Description').value=state.settings.gallery_item_4_description||'';
            document.getElementById('galleryItem4Image').value=state.settings.gallery_item_4_image||'';
            document.getElementById('faqBadge').value=state.settings.faq_badge||'';
            document.getElementById('faqHeading').value=state.settings.faq_heading||'';
            document.getElementById('faqDescription').value=state.settings.faq_description||'';
            document.getElementById('faqItem1Question').value=state.settings.faq_item_1_question||'';
            document.getElementById('faqItem1Answer').value=state.settings.faq_item_1_answer||'';
            document.getElementById('faqItem2Question').value=state.settings.faq_item_2_question||'';
            document.getElementById('faqItem2Answer').value=state.settings.faq_item_2_answer||'';
            document.getElementById('faqItem3Question').value=state.settings.faq_item_3_question||'';
            document.getElementById('faqItem3Answer').value=state.settings.faq_item_3_answer||'';
            document.getElementById('faqItem4Question').value=state.settings.faq_item_4_question||'';
            document.getElementById('faqItem4Answer').value=state.settings.faq_item_4_answer||'';
            document.getElementById('enrollmentInfoBadge').value=state.settings.enrollment_info_badge||'';
            document.getElementById('enrollmentInfoHeading').value=state.settings.enrollment_info_heading||'';
            document.getElementById('enrollmentInfoDescription').value=state.settings.enrollment_info_description||'';
            document.getElementById('enrollmentCard1Label').value=state.settings.enrollment_card_1_label||'';
            document.getElementById('enrollmentCard1Title').value=state.settings.enrollment_card_1_title||'';
            document.getElementById('enrollmentCard1Description').value=state.settings.enrollment_card_1_description||'';
            document.getElementById('enrollmentCard2Label').value=state.settings.enrollment_card_2_label||'';
            document.getElementById('enrollmentCard2Title').value=state.settings.enrollment_card_2_title||'';
            document.getElementById('enrollmentCard2Description').value=state.settings.enrollment_card_2_description||'';
            document.getElementById('enrollmentCard3Label').value=state.settings.enrollment_card_3_label||'';
            document.getElementById('enrollmentCard3Title').value=state.settings.enrollment_card_3_title||'';
            document.getElementById('enrollmentCard3Description').value=state.settings.enrollment_card_3_description||'';
            document.getElementById('admissionFormNote').value=state.settings.admission_form_note||'';
            document.getElementById('whatsappEnabled').checked=String(state.settings.whatsapp_enabled||'1')==='1';
            document.getElementById('whatsappNumber').value=state.settings.whatsapp_number||'';
            document.getElementById('whatsappMessage').value=state.settings.whatsapp_message||'';
            document.getElementById('statusCheckEnabled').checked=String(state.settings.status_check_enabled||'1')==='1';
            document.getElementById('statusCheckDisabledMessage').value=state.settings.status_check_disabled_message||'';
            document.getElementById('enrollmentEnabled').checked=String(state.settings.enrollment_enabled||'1')==='1';
            document.getElementById('enrollmentClosedMessage').value=state.settings.enrollment_closed_message||'';
            document.getElementById('officeTiming').value=state.settings.office_timing||'';
            document.getElementById('settingsEmail').value=state.settings.email||'';
            document.getElementById('facebookUrl').value=state.settings.facebook_url||'';
            document.getElementById('settingsAddress').value=state.settings.address||'';
            document.getElementById('mapEmbedUrl').value=state.settings.map_embed_url||'';
            document.getElementById('statusMessagePending').value=state.settings.status_message_pending||'';
            document.getElementById('statusMessageConfirmed').value=state.settings.status_message_confirmed||'';
            document.getElementById('statusMessageRejected').value=state.settings.status_message_rejected||'';
            document.getElementById('statusMessageNotFound').value=state.settings.status_message_not_found||'';
            syncEnrollmentClassScopeFromSettings();
            renderAdminTheme();
        }

        function fillMarqueeSettings(){
            document.getElementById('marqueeEnabled').checked=String(state.settings.marquee_enabled||'0')==='1';
            document.getElementById('marqueeText').value=state.settings.marquee_text||'';
        }

        function renderPopupResultOptions(){
            const select=document.getElementById('homepagePopupResultId');
            const savedValue=String(state.settings.homepage_popup_result_id||'');
            select.innerHTML=['<option value="">No specific result</option>'].concat(state.results.map((item)=>`<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)} | ${escapeHtml(item.class)} | ${escapeHtml(item.year)}</option>`)).join('');
            select.value=state.results.some((item)=>String(item.id)===savedValue)?savedValue:'';
        }

        function togglePopupResultField(){
            const show=document.getElementById('homepagePopupTargetSection').value==='results';
            document.getElementById('homepagePopupResultField').style.display=show?'grid':'none';
        }

        function fillHomepagePopupSettings(){
            document.getElementById('homepagePopupEnabled').checked=String(state.settings.homepage_popup_enabled||'0')==='1';
            document.getElementById('homepagePopupTitle').value=state.settings.homepage_popup_title||'';
            document.getElementById('homepagePopupMessage').value=state.settings.homepage_popup_message||'';
            document.getElementById('homepagePopupButtonLabel').value=state.settings.homepage_popup_button_label||'';
            document.getElementById('homepagePopupTargetSection').value=state.settings.homepage_popup_target_section||'';
            renderPopupResultOptions();
            togglePopupResultField();
        }

        function buildFullSettingsPayload(overrides={}){
            return {
                contact_primary:state.settings.contact_primary||'',
                contact_secondary:state.settings.contact_secondary||'',
                hero_badge:state.settings.hero_badge||'',
                hero_heading:state.settings.hero_heading||'',
                hero_description:state.settings.hero_description||'',
                hero_overlay_title:state.settings.hero_overlay_title||'',
                hero_overlay_description:state.settings.hero_overlay_description||'',
                motion_enabled:String(state.settings.motion_enabled||'1'),
                dark_mode_enabled:String(state.settings.dark_mode_enabled||'0'),
                home_stats_enabled:String(state.settings.home_stats_enabled||'1'),
                home_announcements_enabled:String(state.settings.home_announcements_enabled||'1'),
                home_message_enabled:String(state.settings.home_message_enabled||'1'),
                home_gallery_enabled:String(state.settings.home_gallery_enabled||'1'),
                home_faq_enabled:String(state.settings.home_faq_enabled||'1'),
                message_badge:state.settings.message_badge||'',
                message_heading:state.settings.message_heading||'',
                message_description:state.settings.message_description||'',
                message_author_name:state.settings.message_author_name||'',
                message_author_title:state.settings.message_author_title||'',
                gallery_badge:state.settings.gallery_badge||'',
                gallery_heading:state.settings.gallery_heading||'',
                gallery_description:state.settings.gallery_description||'',
                gallery_item_1_label:state.settings.gallery_item_1_label||'',
                gallery_item_1_title:state.settings.gallery_item_1_title||'',
                gallery_item_1_description:state.settings.gallery_item_1_description||'',
                gallery_item_1_image:state.settings.gallery_item_1_image||'',
                gallery_item_2_label:state.settings.gallery_item_2_label||'',
                gallery_item_2_title:state.settings.gallery_item_2_title||'',
                gallery_item_2_description:state.settings.gallery_item_2_description||'',
                gallery_item_2_image:state.settings.gallery_item_2_image||'',
                gallery_item_3_label:state.settings.gallery_item_3_label||'',
                gallery_item_3_title:state.settings.gallery_item_3_title||'',
                gallery_item_3_description:state.settings.gallery_item_3_description||'',
                gallery_item_3_image:state.settings.gallery_item_3_image||'',
                gallery_item_4_label:state.settings.gallery_item_4_label||'',
                gallery_item_4_title:state.settings.gallery_item_4_title||'',
                gallery_item_4_description:state.settings.gallery_item_4_description||'',
                gallery_item_4_image:state.settings.gallery_item_4_image||'',
                faq_badge:state.settings.faq_badge||'',
                faq_heading:state.settings.faq_heading||'',
                faq_description:state.settings.faq_description||'',
                faq_item_1_question:state.settings.faq_item_1_question||'',
                faq_item_1_answer:state.settings.faq_item_1_answer||'',
                faq_item_2_question:state.settings.faq_item_2_question||'',
                faq_item_2_answer:state.settings.faq_item_2_answer||'',
                faq_item_3_question:state.settings.faq_item_3_question||'',
                faq_item_3_answer:state.settings.faq_item_3_answer||'',
                faq_item_4_question:state.settings.faq_item_4_question||'',
                faq_item_4_answer:state.settings.faq_item_4_answer||'',
                enrollment_info_badge:state.settings.enrollment_info_badge||'',
                enrollment_info_heading:state.settings.enrollment_info_heading||'',
                enrollment_info_description:state.settings.enrollment_info_description||'',
                enrollment_card_1_label:state.settings.enrollment_card_1_label||'',
                enrollment_card_1_title:state.settings.enrollment_card_1_title||'',
                enrollment_card_1_description:state.settings.enrollment_card_1_description||'',
                enrollment_card_2_label:state.settings.enrollment_card_2_label||'',
                enrollment_card_2_title:state.settings.enrollment_card_2_title||'',
                enrollment_card_2_description:state.settings.enrollment_card_2_description||'',
                enrollment_card_3_label:state.settings.enrollment_card_3_label||'',
                enrollment_card_3_title:state.settings.enrollment_card_3_title||'',
                enrollment_card_3_description:state.settings.enrollment_card_3_description||'',
                admission_form_note:state.settings.admission_form_note||'',
                enrollment_class_allowlist:collectEnrollmentClassAllowlist(),
                whatsapp_enabled:String(state.settings.whatsapp_enabled||'1'),
                whatsapp_number:state.settings.whatsapp_number||'',
                whatsapp_message:state.settings.whatsapp_message||'',
                status_check_enabled:String(state.settings.status_check_enabled||'1'),
                status_check_disabled_message:state.settings.status_check_disabled_message||'',
                enrollment_enabled:String(state.settings.enrollment_enabled||'1'),
                enrollment_closed_message:state.settings.enrollment_closed_message||'',
                office_timing:state.settings.office_timing||'',
                email:state.settings.email||'',
                facebook_url:state.settings.facebook_url||'',
                address:state.settings.address||'',
                map_embed_url:state.settings.map_embed_url||'',
                marquee_enabled:String(state.settings.marquee_enabled||'0'),
                marquee_text:state.settings.marquee_text||'',
                status_message_pending:state.settings.status_message_pending||'',
                status_message_confirmed:state.settings.status_message_confirmed||'',
                status_message_rejected:state.settings.status_message_rejected||'',
                status_message_not_found:state.settings.status_message_not_found||'',
                homepage_popup_enabled:String(state.settings.homepage_popup_enabled||'0'),
                homepage_popup_title:state.settings.homepage_popup_title||'',
                homepage_popup_message:state.settings.homepage_popup_message||'',
                homepage_popup_button_label:state.settings.homepage_popup_button_label||'',
                homepage_popup_target_section:state.settings.homepage_popup_target_section||'',
                homepage_popup_result_id:String(state.settings.homepage_popup_result_id||''),
                ...overrides
            };
        }

        function resetAnnouncementForm(){
            announcementForm.reset();
            document.getElementById('announcementId').value='';
            document.getElementById('announcementDate').value=new Date().toISOString().slice(0,10);
            document.getElementById('announcementIsNew').checked=false;
            document.getElementById('announcementIsPublished').checked=true;
            document.getElementById('announcementFormTitle').textContent='Add Announcement';
        }

        function resetResultForm(){
            resultForm.reset();
            resultIdInput.value='';
            document.getElementById('resultIsNew').checked=false;
            document.getElementById('resultIsPublished').checked=true;
            resultFormTitle.textContent='Upload New Result';
        }

        function resetFacultyForm(){
            facultyForm.reset();
            document.getElementById('facultyId').value='';
            setFacultySectionSelections(['Class IX-X']);
            syncFacultyClassSelection();
            document.getElementById('facultyFormTitle').textContent='Add Faculty Member';
            resetFacultyCurrentPhoto();
            resetFacultyPreview();
            clearFacultyCropSource();
            closeFacultyCropModal();
        }

        function formatEnrollmentEditCnic(){
            const digits=editCnic.value.replace(/\D/g,'').slice(0,13);
            const parts=[];
            if(digits.length>0)parts.push(digits.slice(0,5));
            if(digits.length>5)parts.push(digits.slice(5,12));
            if(digits.length>12)parts.push(digits.slice(12,13));
            editCnic.value=parts.join('-');
        }

        function formatEnrollmentEditMobile(){formatEnrollmentPhoneInput(editMobile)}

        function formatEnrollmentEditFatherContact(){formatEnrollmentPhoneInput(editFatherContact)}

        async function loadData(){
            const [enrollmentData,announcements,results,faculty,settings,insights,activityLog,messages]=await Promise.all([
                api('__ADMIN_PATH__/enrollments'),
                api('__ADMIN_PATH__/announcements'),
                api('__ADMIN_PATH__/results'),
                api('__ADMIN_PATH__/faculty'),
                api('__ADMIN_PATH__/settings'),
                api('__ADMIN_PATH__/insights'),
                api('__ADMIN_PATH__/activity-log'),
                api('__ADMIN_PATH__/messages')
            ]);
            state.pendingEnrollments=enrollmentData.pending||[];
            state.confirmedEnrollments=enrollmentData.confirmed||[];
            state.rejectedEnrollments=enrollmentData.rejected||[];
            state.announcements=announcements;
            state.results=results;
            state.faculty=faculty;
            state.settings=settings;
            state.insights=insights||{};
            state.activityLog=activityLog||[];
            state.messages=messages||[];
            renderAdminTheme();
            renderOverview();renderEnrollments();renderAdmissionRecords();renderAnnouncements();renderResults();renderFaculty();renderMessages();fillSettings();fillMarqueeSettings();fillHomepagePopupSettings();
        }

        async function checkSession(){
            try{
                const data=await api('__ADMIN_PATH__/session');
                if(data.authenticated){
                    state.username=data.username||'';
                    state.csrfToken=data.csrf_token||state.csrfToken||'';
                    adminUsername.textContent=state.username?`Signed in as ${state.username}`:'Signed in';
                    setAuthenticated(true);
                    resetAnnouncementForm();resetResultForm();resetFacultyForm();switchTab('overview');
                    await loadData();
                    startInsightsRefresh();
                }else{state.username='';state.csrfToken='';setAuthenticated(false);stopInsightsRefresh();renderAdminTheme(false)}
            }catch(_error){state.username='';state.csrfToken='';setAuthenticated(false);stopInsightsRefresh();renderAdminTheme(false)}
        }

        document.getElementById('loginForm').addEventListener('submit',async(event)=>{
            event.preventDefault();
            try{
                const loginResponse=await api('__ADMIN_PATH__/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('username').value.trim(),password:document.getElementById('password').value})});
                state.csrfToken=loginResponse.csrf_token||state.csrfToken||'';
                toastMessage('Login successful.');
                await checkSession();
            }catch(error){toastMessage(error.message,'error')}
        });

        document.getElementById('logoutButton').addEventListener('click',async()=>{
                await api('__ADMIN_PATH__/logout',{method:'POST'});
            state.username='';
            state.csrfToken='';
            stopInsightsRefresh();
            setAuthenticated(false);
            renderAdminTheme(false);
            toastMessage('Logged out successfully.');
        });

        if(adminConfirmCancel)adminConfirmCancel.addEventListener('click',()=>closeAdminConfirmModal(false));
        if(adminConfirmApprove)adminConfirmApprove.addEventListener('click',()=>closeAdminConfirmModal(true));
        if(adminConfirmModal)adminConfirmModal.addEventListener('click',(event)=>{if(event.target===adminConfirmModal)closeAdminConfirmModal(false)});
        document.addEventListener('keydown',(event)=>{if(event.key==='Escape'&&adminConfirmModal&&!adminConfirmModal.classList.contains('hidden'))closeAdminConfirmModal(false)});
        const darkModeToggle=document.getElementById('darkModeEnabled');
        if(darkModeToggle)darkModeToggle.addEventListener('change',()=>renderAdminTheme(darkModeToggle.checked));

        document.querySelectorAll('.tab').forEach((button)=>button.addEventListener('click',()=>switchTab(button.dataset.tab)));
        document.querySelectorAll('[data-open-tab]').forEach((button)=>button.addEventListener('click',()=>openAdminWorkspace(button.dataset.openTab||'overview',button.dataset.scrollTarget||'')));
        wireEnrollmentClassScopeControls();
        enrollmentNameSearch.addEventListener('input',renderEnrollments);
        enrollmentRollSearch.addEventListener('input',renderEnrollments);
        enrollmentCnicSearch.addEventListener('input',renderEnrollments);
        enrollmentSearch.addEventListener('input',renderEnrollments);
        enrollmentClassFilter.addEventListener('change',renderEnrollments);
        enrollmentFrom.addEventListener('change',renderEnrollments);
        enrollmentTo.addEventListener('change',renderEnrollments);
        enrollmentFrom.addEventListener('input',renderEnrollments);
        enrollmentTo.addEventListener('input',renderEnrollments);
        enrollmentSort.addEventListener('change',renderEnrollments);
        if(clearPendingFiltersButton)clearPendingFiltersButton.addEventListener('click',clearPendingFilters);
        if(selectAllPending)selectAllPending.addEventListener('change',()=>{const rows=getPendingFilteredRows();selectionState.pending.clear();if(selectAllPending.checked)rows.forEach((item)=>selectionState.pending.add(String(item.id)));renderEnrollments()});
        if(bulkConfirmPendingButton)bulkConfirmPendingButton.addEventListener('click',async()=>{try{if(await runBulkEnrollmentAction('confirm',selectionState.pending,'pending students')){toastMessage('Selected students confirmed successfully.');await loadData();notifyCrossPageSync('admin')}}catch(error){toastMessage(error.message,'error')}});
        if(bulkRejectPendingButton)bulkRejectPendingButton.addEventListener('click',async()=>{try{if(await runBulkEnrollmentAction('reject',selectionState.pending,'pending students')){toastMessage('Selected students marked rejected successfully.');await loadData();notifyCrossPageSync('admin')}}catch(error){toastMessage(error.message,'error')}});
        if(bulkDeletePendingButton)bulkDeletePendingButton.addEventListener('click',async()=>{try{if(await runBulkEnrollmentAction('delete',selectionState.pending,'pending students')){toastMessage('Selected students deleted successfully.');await loadData();notifyCrossPageSync('admin')}}catch(error){toastMessage(error.message,'error')}});
        if(exportSelectedPendingButton)exportSelectedPendingButton.addEventListener('click',()=>{const selectedRows=getSelectedRows(getPendingFilteredRows(),selectionState.pending);if(!selectedRows.length){toastMessage('Please select at least one pending student.','error');return}downloadRowsAsCsv(selectedRows,'Pending','the_professors_academy_selected_pending_students.csv');toastMessage('Selected pending student list downloaded successfully.')});
        admissionRecordsNameSearch.addEventListener('input',renderAdmissionRecords);
        admissionRecordsRollSearch.addEventListener('input',renderAdmissionRecords);
        admissionRecordsCnicSearch.addEventListener('input',renderAdmissionRecords);
        admissionRecordsSearch.addEventListener('input',renderAdmissionRecords);
        admissionRecordsClassFilter.addEventListener('change',renderAdmissionRecords);
        admissionRecordsStatusFilter.addEventListener('change',renderAdmissionRecords);
        admissionRecordsFrom.addEventListener('change',renderAdmissionRecords);
        admissionRecordsTo.addEventListener('change',renderAdmissionRecords);
        admissionRecordsFrom.addEventListener('input',renderAdmissionRecords);
        admissionRecordsTo.addEventListener('input',renderAdmissionRecords);
        admissionRecordsClearRange.addEventListener('click',clearRecordFilters);
        admissionRecordsSort.addEventListener('change',renderAdmissionRecords);
        if(selectAllRecords)selectAllRecords.addEventListener('change',()=>{const rows=getAdmissionRecordFilteredRows().allRows;selectionState.records.clear();if(selectAllRecords.checked)rows.forEach((item)=>selectionState.records.add(String(item.id)));renderAdmissionRecords()});
        if(exportSelectedRecordsButton)exportSelectedRecordsButton.addEventListener('click',()=>{const selectedRows=getSelectedRows(getAdmissionRecordFilteredRows().allRows,selectionState.records);if(!selectedRows.length){toastMessage('Please select at least one saved student record.','error');return}const statusLabel=admissionRecordsStatusFilter.value==='confirmed'?'Confirmed':admissionRecordsStatusFilter.value==='rejected'?'Rejected':'Final Record';downloadRowsAsCsv(selectedRows,statusLabel,'the_professors_academy_selected_final_records.csv');toastMessage('Selected student list downloaded successfully.')});
        if(deleteSelectedRecordsButton)deleteSelectedRecordsButton.addEventListener('click',async()=>{try{if(await runBulkEnrollmentAction('delete',selectionState.records,'final records')){toastMessage('Selected final records deleted successfully.');await loadData();notifyCrossPageSync('admin')}}catch(error){toastMessage(error.message,'error')}});
        if(messageSearch)messageSearch.addEventListener('input',renderMessages);
        if(messageStatusFilter)messageStatusFilter.addEventListener('change',renderMessages);
        if(clearMessageFiltersButton)clearMessageFiltersButton.addEventListener('click',()=>{if(messageSearch)messageSearch.value='';if(messageStatusFilter)messageStatusFilter.value='';renderMessages()});
        if(messageList)messageList.addEventListener('click',async(event)=>{
            const toggleButton=event.target.closest('.message-toggle-read');
            const deleteButton=event.target.closest('.message-delete');
            if(toggleButton){
                try{
                    await api('__ADMIN_PATH__/messages',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:toggleButton.dataset.id,is_read:toggleButton.dataset.read==='1'})});
                    toastMessage(toggleButton.dataset.read==='1'?'Message marked handled.':'Message marked unread.');
                    await loadData();
                    notifyCrossPageSync('admin');
                }catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(!deleteButton)return;
            const approved=await confirmAction('Delete this website message?',{title:'Delete this visitor message?',badge:'Delete Message',mark:'DEL',tone:'danger',confirmLabel:'Yes, Delete'});
            if(!approved)return;
            try{
                await api('__ADMIN_PATH__/messages',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:deleteButton.dataset.id})});
                toastMessage('Message deleted successfully.');
                await loadData();
                notifyCrossPageSync('admin');
            }catch(error){toastMessage(error.message,'error')}
        });
        document.querySelectorAll('.report-download').forEach((button)=>button.addEventListener('click',async()=>{try{await downloadReport(button.dataset.range);toastMessage('Report downloaded successfully.')}catch(error){toastMessage(error.message,'error')}}));
        if(downloadConfirmedFormsButton)downloadConfirmedFormsButton.addEventListener('click',async()=>{try{await downloadConfirmedForms();toastMessage('Confirmed admission forms downloaded successfully.')}catch(error){toastMessage(error.message,'error')}});
        if(exportEnrollmentsButton)exportEnrollmentsButton.addEventListener('click',async()=>{try{await downloadEnrollmentCsv('pending');toastMessage('Pending student list downloaded successfully.')}catch(error){toastMessage(error.message,'error')}});
        if(downloadBackupButton)downloadBackupButton.addEventListener('click',async()=>{try{await downloadBackupArchive();toastMessage('Backup downloaded successfully.')}catch(error){toastMessage(error.message,'error')}});
        if(restoreBackupForm)restoreBackupForm.addEventListener('submit',async(event)=>{event.preventDefault();if(!restoreBackupFile.files||!restoreBackupFile.files[0]){toastMessage('Please choose a backup file first.','error');return}const approved=await confirmAction('Restore backup now? This will replace the current website data and upload files.',{title:'Restore saved backup?',badge:'Safety Check',mark:'RST',tone:'danger',confirmLabel:'Yes, Restore'});if(!approved)return;try{await api('__ADMIN_PATH__/restore',{method:'POST',body:new FormData(restoreBackupForm)});toastMessage('Backup restored successfully.');restoreBackupForm.reset();await loadData();notifyCrossPageSync('public');notifyCrossPageSync('admin')}catch(error){toastMessage(error.message,'error')}});

        attachEnrollmentPhoneFormatting(editMobile);
        attachEnrollmentPhoneFormatting(editFatherContact);
        attachEnrollmentDateFormatting(editDateOfBirth);
        editCnic.addEventListener('input',formatEnrollmentEditCnic);
        editMobile.addEventListener('change',formatEnrollmentEditMobile);
        editFatherContact.addEventListener('change',formatEnrollmentEditFatherContact);
        editEnrollmentClass.addEventListener('change',()=>{enrollmentEditState.selectedSubjects=[];enrollmentEditState.group='';renderEnrollmentEditSubjects()});
        editEnrollmentPhoto.addEventListener('change',()=>{const file=editEnrollmentPhoto.files[0];if(!file){if(enrollmentEditState.originalPhotoUrl)showEnrollmentEditPreview(enrollmentEditState.originalPhotoUrl);else resetEnrollmentEditPreview();return}if(!['image/jpeg','image/png'].includes(file.type)){editEnrollmentPhoto.value='';toastMessage('Student photo must be a JPG or PNG image.','error');if(enrollmentEditState.originalPhotoUrl)showEnrollmentEditPreview(enrollmentEditState.originalPhotoUrl);else resetEnrollmentEditPreview();return}if(file.size>300*1024){editEnrollmentPhoto.value='';toastMessage('Passport picture must be 300 KB or smaller.','error');if(enrollmentEditState.originalPhotoUrl)showEnrollmentEditPreview(enrollmentEditState.originalPhotoUrl);else resetEnrollmentEditPreview();return}showEnrollmentEditPreview(URL.createObjectURL(file),true)});
        enrollmentEditForm.addEventListener('submit',async(event)=>{event.preventDefault();const studentId=editEnrollmentId.value;if(!studentId)return;formatEnrollmentEditMobile();formatEnrollmentEditFatherContact();formatEnrollmentDateInput(editDateOfBirth);if(!validateEnrollmentPhoneValue(editMobile.value)){toastMessage('Please enter a valid mobile number in +92 1234567890 format.','error');return}if(!validateEnrollmentPhoneValue(editFatherContact.value)){toastMessage('Please enter a valid father contact number in +92 1234567890 format.','error');return}if(!validateEnrollmentDateValue(editDateOfBirth.value)){toastMessage('Please enter date of birth in DD/MM/YYYY format.','error');return}const selectedClass=editEnrollmentClass.value.trim();let selectedGroup=currentEnrollmentEditGroupValue();if(selectedClass==='MDCAT Prep')selectedGroup='Pre-Medical';if(selectedClass==='ECAT Prep')selectedGroup='Pre-Engineering';if((selectedClass==='XI'||selectedClass==='XII')&&!selectedGroup){toastMessage('Please choose either Pre-Medical or Pre-Engineering.','error');return}const selectedSubjects=collectEnrollmentEditSubjects();if(!selectedSubjects.length){toastMessage('Please choose at least one subject.','error');return}const file=editEnrollmentPhoto.files[0];if(file&&file.size>300*1024){toastMessage('Passport picture must be 300 KB or smaller.','error');return}const formData=new FormData(enrollmentEditForm);formData.set('subjects',JSON.stringify(selectedSubjects));formData.set('group',selectedGroup);formData.set('date_of_birth',editDateOfBirth.value.trim());try{await api(`__ADMIN_PATH__/enrollment/${studentId}`,{method:'PUT',body:formData});toastMessage('Enrollment updated successfully.');resetEnrollmentEditForm();await loadData();notifyCrossPageSync('admin')}catch(error){toastMessage(error.message,'error')}});
        document.getElementById('enrollmentEditCancel').addEventListener('click',resetEnrollmentEditForm);
        enrollmentEditModal.addEventListener('click',(event)=>{if(event.target===enrollmentEditModal)resetEnrollmentEditForm()});

        async function deleteEnrollment(id){
                await api(`__ADMIN_PATH__/enrollment/${id}`,{method:'DELETE'});
            toastMessage('Enrollment deleted.');
            await loadData();
            notifyCrossPageSync('admin');
        }

        enrollmentClassSections.addEventListener('change',(event)=>{
            const pendingCheckbox=event.target.closest('.pending-select');
            if(!pendingCheckbox)return;
            const id=String(pendingCheckbox.dataset.id||'').trim();
            if(!id)return;
            if(pendingCheckbox.checked)selectionState.pending.add(id);else selectionState.pending.delete(id);
            renderEnrollments();
        });

        enrollmentClassTabs.addEventListener('click',(event)=>{
            const tabButton=event.target.closest('[data-class-tab]');
            if(!tabButton)return;
            enrollmentClassFilter.value=tabButton.dataset.classTab||'';
            renderEnrollments();
        });
        admissionRecordsClassTabs.addEventListener('click',(event)=>{
            const tabButton=event.target.closest('[data-record-class-tab]');
            if(!tabButton)return;
            admissionRecordsClassFilter.value=tabButton.dataset.recordClassTab||'';
            renderAdmissionRecords();
        });

        enrollmentClassSections.addEventListener('click',async(event)=>{
            const classExportButton=event.target.closest('.class-export');
            const editButton=event.target.closest('.enrollment-edit');
            const confirmButton=event.target.closest('.enrollment-confirm');
            const rejectButton=event.target.closest('.enrollment-reject');
            const button=event.target.closest('.enrollment-delete');
            if(classExportButton){
                try{await downloadEnrollmentCsv('pending',classExportButton.dataset.class||'');toastMessage('Class student list downloaded successfully.')}catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(editButton){
                const item=findEnrollmentRecordById(editButton.dataset.id);
                if(item)openEnrollmentEditModal(item);
                return;
            }
            if(confirmButton){
                const approved=await confirmAction('Confirm admission for this student?',{title:'Approve this admission?',badge:'Admission Approval',mark:'OK',confirmLabel:'Yes, Approve'});
                if(!approved)return;
            try{await api(`__ADMIN_PATH__/enrollment/${confirmButton.dataset.id}/confirm`,{method:'POST'});toastMessage('Admission confirmed successfully.');await loadData();notifyCrossPageSync('admin')}catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(rejectButton){
                const approved=await confirmAction('Reject admission for this student?',{title:'Reject this admission?',badge:'Admission Decision',mark:'RJ',tone:'danger',confirmLabel:'Yes, Reject'});
                if(!approved)return;
            try{await api(`__ADMIN_PATH__/enrollment/${rejectButton.dataset.id}/reject`,{method:'POST'});toastMessage('Admission rejected successfully.');await loadData();notifyCrossPageSync('admin')}catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(!button)return;
            const approved=await confirmAction('Delete this enrollment?',{title:'Delete this student record?',badge:'Delete Record',mark:'DEL',tone:'danger',confirmLabel:'Yes, Delete'});
            if(!approved)return;
            try{await deleteEnrollment(button.dataset.id)}catch(error){toastMessage(error.message,'error')}
        });

        admissionRecordsSections.addEventListener('change',(event)=>{
            const recordCheckbox=event.target.closest('.record-select');
            if(!recordCheckbox)return;
            const id=String(recordCheckbox.dataset.id||'').trim();
            if(!id)return;
            if(recordCheckbox.checked)selectionState.records.add(id);else selectionState.records.delete(id);
            renderAdmissionRecords();
        });

        admissionRecordsSections.addEventListener('click',async(event)=>{
            const exportAllButton=event.target.closest('.record-export-all');
            const allConfirmedFormsButton=event.target.closest('.record-confirmed-forms-all');
            const classExportButton=event.target.closest('.record-class-export');
            const classConfirmedFormsButton=event.target.closest('.record-class-confirmed-forms');
            const editButton=event.target.closest('.enrollment-edit');
            const button=event.target.closest('.confirmed-enrollment-delete, .rejected-enrollment-delete');
            if(exportAllButton){
                try{await downloadEnrollmentCsv(exportAllButton.dataset.status||'confirmed');toastMessage('Student list downloaded successfully.')}catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(allConfirmedFormsButton){
                try{await downloadConfirmedForms();toastMessage('Confirmed admission forms downloaded successfully.')}catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(classExportButton){
                try{await downloadEnrollmentCsv(classExportButton.dataset.status||'confirmed',classExportButton.dataset.class||'');toastMessage('Class student list downloaded successfully.')}catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(classConfirmedFormsButton){
                try{await downloadConfirmedForms(classConfirmedFormsButton.dataset.class||'');toastMessage('Class confirmed forms downloaded successfully.')}catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(editButton){
                const item=findEnrollmentRecordById(editButton.dataset.id);
                if(item)openEnrollmentEditModal(item);
                return;
            }
            const deleteLabel=button&&button.classList.contains('confirmed-enrollment-delete')?'confirmed admission record':'rejected admission record';
            if(!button)return;
            const approved=await confirmAction(`Delete this ${deleteLabel}?`,{title:'Delete saved admission record?',badge:'Delete Record',mark:'DEL',tone:'danger',confirmLabel:'Yes, Delete'});
            if(!approved)return;
            try{await deleteEnrollment(button.dataset.id)}catch(error){toastMessage(error.message,'error')}
        });
""")

ADMIN_HTML_PARTS.append(
    r"""
        announcementForm.addEventListener('submit',async(event)=>{
            event.preventDefault();
            const payload={id:document.getElementById('announcementId').value,title:document.getElementById('announcementTitle').value.trim(),description:document.getElementById('announcementDescription').value.trim(),date:document.getElementById('announcementDate').value||new Date().toISOString().slice(0,10),is_new:document.getElementById('announcementIsNew').checked?'1':'0',is_published:document.getElementById('announcementIsPublished').checked?'1':'0'};
            try{
                await api('__ADMIN_PATH__/announcements',{method:payload.id?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
                toastMessage(payload.id?'Notice updated.':'Notice saved.');
                resetAnnouncementForm();
                await loadData();
                notifyCrossPageSync('public');
            }catch(error){toastMessage(error.message,'error')}
        });

        document.getElementById('announcementReset').addEventListener('click',resetAnnouncementForm);
        announcementList.addEventListener('click',async(event)=>{
            const editButton=event.target.closest('.announcement-edit');
            const deleteButton=event.target.closest('.announcement-delete');
            if(editButton){
                const item=state.announcements.find((entry)=>String(entry.id)===editButton.dataset.id);
                if(!item)return;
                document.getElementById('announcementId').value=item.id;
                document.getElementById('announcementTitle').value=item.title;
                document.getElementById('announcementDescription').value=item.description;
                document.getElementById('announcementDate').value=item.date||'';
                document.getElementById('announcementIsNew').checked=Boolean(item.is_new);
                document.getElementById('announcementIsPublished').checked=Boolean(item.is_published);
                document.getElementById('announcementFormTitle').textContent='Edit Notice';
                switchTab('announcements');
                window.scrollTo({top:0,behavior:'smooth'});
                return;
            }
            if(deleteButton){
                const approved=await confirmAction('Delete this announcement?',{title:'Delete this notice?',badge:'Delete Notice',mark:'DEL',tone:'danger',confirmLabel:'Yes, Delete'});
                if(!approved)return;
            try{await api('__ADMIN_PATH__/announcements',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:deleteButton.dataset.id})});toastMessage('Announcement deleted.');await loadData();notifyCrossPageSync('public')}catch(error){toastMessage(error.message,'error')}
            }
        });

        resultForm.addEventListener('submit',async(event)=>{
            event.preventDefault();
            const editing=Boolean(resultIdInput.value);
            if(!editing&&(!resultPdfInput.files||!resultPdfInput.files[0])){toastMessage('Please upload a PDF file.','error');return}
            const formData=new FormData(event.currentTarget);
            formData.set('is_new',document.getElementById('resultIsNew').checked?'1':'0');
            formData.set('is_published',document.getElementById('resultIsPublished').checked?'1':'0');
            try{await api('__ADMIN_PATH__/results',{method:editing?'PUT':'POST',body:formData});toastMessage(editing?'Result updated.':'Result saved.');resetResultForm();await loadData();notifyCrossPageSync('public')}catch(error){toastMessage(error.message,'error')}
        });
        document.getElementById('resultReset').addEventListener('click',resetResultForm);
        resultList.addEventListener('click',async(event)=>{
            const editButton=event.target.closest('.result-edit');
            const deleteButton=event.target.closest('.result-delete');
            if(editButton){
                const item=state.results.find((entry)=>String(entry.id)===editButton.dataset.id);
                if(!item)return;
                resultIdInput.value=item.id;
                resultTitleInput.value=item.title||'';
                resultClassInput.value=item.class||'';
                resultYearInput.value=item.year||'';
                document.getElementById('resultIsNew').checked=Boolean(item.is_new);
                document.getElementById('resultIsPublished').checked=Boolean(item.is_published);
                resultPdfInput.value='';
                resultFormTitle.textContent='Edit Result';
                switchTab('results');
                window.scrollTo({top:0,behavior:'smooth'});
                return;
            }
            if(!deleteButton)return;
            const approved=await confirmAction('Delete this result PDF?',{title:'Delete this result file?',badge:'Delete Result',mark:'DEL',tone:'danger',confirmLabel:'Yes, Delete'});
            if(!approved)return;
            try{await api('__ADMIN_PATH__/results',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:deleteButton.dataset.id})});toastMessage('Result deleted.');await loadData();notifyCrossPageSync('public')}catch(error){toastMessage(error.message,'error')}
        });

        facultyPhotoInput.addEventListener('change',()=>{const file=facultyPhotoInput.files[0];if(file){facultyRemovePhoto.checked=false;openFacultyCropper(file)}else{resetFacultyPreview();clearFacultyCropSource();closeFacultyCropModal()}});
        facultyCropZoom.addEventListener('input',()=>{facultyCropState.zoom=Math.max(1,Number(facultyCropZoom.value||100)/100);drawFacultyCropCanvas()});
        facultyCropX.addEventListener('input',()=>{facultyCropState.offsetX=Number(facultyCropX.value||0);drawFacultyCropCanvas()});
        facultyCropY.addEventListener('input',()=>{facultyCropState.offsetY=Number(facultyCropY.value||0);drawFacultyCropCanvas()});
        document.getElementById('keepOriginalFacultyPhoto').addEventListener('click',()=>{if(facultyCropState.file)updateFacultyPreviewFromFile(facultyCropState.file);closeFacultyCropModal()});
        document.getElementById('cancelFacultyCrop').addEventListener('click',()=>{facultyPhotoInput.value='';resetFacultyPreview();clearFacultyCropSource();closeFacultyCropModal()});
        document.getElementById('recropFacultyPhoto').addEventListener('click',()=>{const file=facultyPhotoInput.files[0]||facultyCropState.file;if(file)openFacultyCropper(file)});
        facultyCropModal.addEventListener('click',(event)=>{if(event.target===facultyCropModal){closeFacultyCropModal()}});
        document.getElementById('applyFacultyCrop').addEventListener('click',()=>{if(!facultyCropState.image)return;facultyCropCanvas.toBlob((blob)=>{if(!blob){toastMessage('Unable to apply crop right now.','error');return}const originalName=(facultyCropState.file&&facultyCropState.file.name)||'faculty-photo.jpg';const extension=originalName.toLowerCase().endsWith('.png')?'png':'jpeg';const safeName=originalName.replace(/\.[^.]+$/,'')+`.${extension==='png'?'png':'jpg'}`;const croppedFile=new File([blob],safeName,{type:extension==='png'?'image/png':'image/jpeg'});assignFacultyFile(croppedFile);facultyCropState.file=croppedFile;updateFacultyPreviewFromFile(croppedFile);closeFacultyCropModal()},'image/jpeg',0.92)});

        facultyForm.addEventListener('submit',async(event)=>{
            event.preventDefault();
            const editing=Boolean(document.getElementById('facultyId').value);
            syncFacultyClassSelection();
            if(!collectFacultySections().length){toastMessage('Please choose at least one faculty teaching section.','error');return}
            try{await api('__ADMIN_PATH__/faculty',{method:editing?'PUT':'POST',body:new FormData(event.currentTarget)});toastMessage(editing?'Faculty profile updated successfully.':'Faculty profile saved successfully.');resetFacultyForm();await loadData();notifyCrossPageSync('public')}catch(error){toastMessage(error.message,'error')}
        });
        document.getElementById('facultyReset').addEventListener('click',resetFacultyForm);
        document.querySelectorAll('[data-faculty-section]').forEach((checkbox)=>checkbox.addEventListener('change',()=>{if(!collectFacultySections().length)checkbox.checked=true;syncFacultyClassSelection()}));
        facultyList.addEventListener('click',async(event)=>{
            const editButton=event.target.closest('.faculty-edit');
            const deleteButton=event.target.closest('.faculty-delete');
            const moveButton=event.target.closest('.faculty-move');
            if(editButton){
                const item=state.faculty.find((entry)=>String(entry.id)===editButton.dataset.id);
                if(!item)return;
                const parsedClass=parseFacultyClassConfig(item.class_assigned,item.subject);
                document.getElementById('facultyId').value=item.id;
                document.getElementById('facultyName').value=item.name;
                setFacultySectionSelections(parsedClass.sections||['Class IX-X']);
                syncFacultyClassSelection();
                document.getElementById('facultySubject').value=item.subject;
                document.getElementById('facultyQualification').value=item.qualification;
                document.getElementById('facultyExperienceYears').value=item.experience_years||'';
                document.getElementById('facultyFormTitle').textContent='Edit Teacher';
                showFacultyCurrentPhoto(item.photo_url||'');
                resetFacultyPreview();
                switchTab('faculty');
                window.scrollTo({top:0,behavior:'smooth'});
                return;
            }
            if(deleteButton){
                const approved=await confirmAction('Delete this faculty record?',{title:'Delete this teacher profile?',badge:'Delete Teacher',mark:'DEL',tone:'danger',confirmLabel:'Yes, Delete'});
                if(!approved)return;
            try{await api('__ADMIN_PATH__/faculty',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:deleteButton.dataset.id})});toastMessage('Faculty member deleted.');await loadData();notifyCrossPageSync('public')}catch(error){toastMessage(error.message,'error')}
                return;
            }
            if(moveButton){
            try{await api('__ADMIN_PATH__/faculty',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:moveButton.dataset.id,action:'move',direction:moveButton.dataset.direction})});await loadData();notifyCrossPageSync('public')}catch(error){toastMessage(error.message,'error')}
            }
        });

        marqueeForm.addEventListener('submit',async(event)=>{
            event.preventDefault();
            try{
                await api('__ADMIN_PATH__/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(buildFullSettingsPayload({
                    marquee_enabled:document.getElementById('marqueeEnabled').checked?'1':'0',
                    marquee_text:document.getElementById('marqueeText').value.trim()
                }))});
                toastMessage('Top moving line saved.');
                await loadData();
                notifyCrossPageSync('public');
            }catch(error){toastMessage(error.message,'error')}
        });
        document.getElementById('homepagePopupTargetSection').addEventListener('change',togglePopupResultField);

        popupNoticeForm.addEventListener('submit',async(event)=>{
            event.preventDefault();
            try{
                await api('__ADMIN_PATH__/settings',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(buildFullSettingsPayload({
                    homepage_popup_enabled:document.getElementById('homepagePopupEnabled').checked?'1':'0',
                    homepage_popup_title:document.getElementById('homepagePopupTitle').value.trim(),
                    homepage_popup_message:document.getElementById('homepagePopupMessage').value.trim(),
                    homepage_popup_button_label:document.getElementById('homepagePopupButtonLabel').value.trim(),
                    homepage_popup_target_section:document.getElementById('homepagePopupTargetSection').value,
                    homepage_popup_result_id:document.getElementById('homepagePopupTargetSection').value==='results'?document.getElementById('homepagePopupResultId').value:''
                }))});
                toastMessage('Home popup message saved.');
                await loadData();
                notifyCrossPageSync('public');
            }catch(error){toastMessage(error.message,'error')}
        });

        settingsForm.addEventListener('submit',async(event)=>{
            event.preventDefault();
            try{
                const settingsPayload=buildFullSettingsPayload({
                    contact_primary:document.getElementById('contactPrimary').value.trim(),
                    contact_secondary:document.getElementById('contactSecondary').value.trim(),
                    hero_badge:document.getElementById('heroBadge').value.trim(),
                    hero_heading:document.getElementById('heroHeading').value.trim(),
                    hero_description:document.getElementById('heroDescription').value.trim(),
                    hero_overlay_title:document.getElementById('heroOverlayTitle').value.trim(),
                    hero_overlay_description:document.getElementById('heroOverlayDescription').value.trim(),
                    motion_enabled:document.getElementById('motionEnabled').checked?'1':'0',
                    dark_mode_enabled:document.getElementById('darkModeEnabled').checked?'1':'0',
                    home_stats_enabled:document.getElementById('homeStatsEnabled').checked?'1':'0',
                    home_announcements_enabled:document.getElementById('homeAnnouncementsEnabled').checked?'1':'0',
                    home_message_enabled:document.getElementById('homeMessageEnabled').checked?'1':'0',
                    home_gallery_enabled:document.getElementById('homeGalleryEnabled').checked?'1':'0',
                    home_faq_enabled:document.getElementById('homeFaqEnabled').checked?'1':'0',
                    gallery_badge:document.getElementById('galleryBadge').value.trim(),
                    gallery_heading:document.getElementById('galleryHeading').value.trim(),
                    gallery_description:document.getElementById('galleryDescription').value.trim(),
                    gallery_item_1_label:document.getElementById('galleryItem1Label').value.trim(),
                    gallery_item_1_title:document.getElementById('galleryItem1Title').value.trim(),
                    gallery_item_1_description:document.getElementById('galleryItem1Description').value.trim(),
                    gallery_item_1_image:document.getElementById('galleryItem1Image').value.trim(),
                    gallery_item_2_label:document.getElementById('galleryItem2Label').value.trim(),
                    gallery_item_2_title:document.getElementById('galleryItem2Title').value.trim(),
                    gallery_item_2_description:document.getElementById('galleryItem2Description').value.trim(),
                    gallery_item_2_image:document.getElementById('galleryItem2Image').value.trim(),
                    gallery_item_3_label:document.getElementById('galleryItem3Label').value.trim(),
                    gallery_item_3_title:document.getElementById('galleryItem3Title').value.trim(),
                    gallery_item_3_description:document.getElementById('galleryItem3Description').value.trim(),
                    gallery_item_3_image:document.getElementById('galleryItem3Image').value.trim(),
                    gallery_item_4_label:document.getElementById('galleryItem4Label').value.trim(),
                    gallery_item_4_title:document.getElementById('galleryItem4Title').value.trim(),
                    gallery_item_4_description:document.getElementById('galleryItem4Description').value.trim(),
                    gallery_item_4_image:document.getElementById('galleryItem4Image').value.trim(),
                    faq_badge:document.getElementById('faqBadge').value.trim(),
                    faq_heading:document.getElementById('faqHeading').value.trim(),
                    faq_description:document.getElementById('faqDescription').value.trim(),
                    faq_item_1_question:document.getElementById('faqItem1Question').value.trim(),
                    faq_item_1_answer:document.getElementById('faqItem1Answer').value.trim(),
                    faq_item_2_question:document.getElementById('faqItem2Question').value.trim(),
                    faq_item_2_answer:document.getElementById('faqItem2Answer').value.trim(),
                    faq_item_3_question:document.getElementById('faqItem3Question').value.trim(),
                    faq_item_3_answer:document.getElementById('faqItem3Answer').value.trim(),
                    faq_item_4_question:document.getElementById('faqItem4Question').value.trim(),
                    faq_item_4_answer:document.getElementById('faqItem4Answer').value.trim(),
                    enrollment_info_badge:document.getElementById('enrollmentInfoBadge').value.trim(),
                    enrollment_info_heading:document.getElementById('enrollmentInfoHeading').value.trim(),
                    enrollment_info_description:document.getElementById('enrollmentInfoDescription').value.trim(),
                    enrollment_card_1_label:document.getElementById('enrollmentCard1Label').value.trim(),
                    enrollment_card_1_title:document.getElementById('enrollmentCard1Title').value.trim(),
                    enrollment_card_1_description:document.getElementById('enrollmentCard1Description').value.trim(),
                    enrollment_card_2_label:document.getElementById('enrollmentCard2Label').value.trim(),
                    enrollment_card_2_title:document.getElementById('enrollmentCard2Title').value.trim(),
                    enrollment_card_2_description:document.getElementById('enrollmentCard2Description').value.trim(),
                    enrollment_card_3_label:document.getElementById('enrollmentCard3Label').value.trim(),
                    enrollment_card_3_title:document.getElementById('enrollmentCard3Title').value.trim(),
                    enrollment_card_3_description:document.getElementById('enrollmentCard3Description').value.trim(),
                    admission_form_note:document.getElementById('admissionFormNote').value.trim(),
                    whatsapp_enabled:document.getElementById('whatsappEnabled').checked?'1':'0',
                    whatsapp_number:document.getElementById('whatsappNumber').value.trim(),
                    whatsapp_message:document.getElementById('whatsappMessage').value.trim(),
                    status_check_enabled:document.getElementById('statusCheckEnabled').checked?'1':'0',
                    status_check_disabled_message:document.getElementById('statusCheckDisabledMessage').value.trim(),
                    enrollment_enabled:document.getElementById('enrollmentEnabled').checked?'1':'0',
                    enrollment_closed_message:document.getElementById('enrollmentClosedMessage').value.trim(),
                    office_timing:document.getElementById('officeTiming').value.trim(),
                    email:document.getElementById('settingsEmail').value.trim(),
                    facebook_url:document.getElementById('facebookUrl').value.trim(),
                    address:document.getElementById('settingsAddress').value.trim(),
                    map_embed_url:document.getElementById('mapEmbedUrl').value.trim(),
                    status_message_pending:document.getElementById('statusMessagePending').value.trim(),
                    status_message_confirmed:document.getElementById('statusMessageConfirmed').value.trim(),
                    status_message_rejected:document.getElementById('statusMessageRejected').value.trim(),
                    status_message_not_found:document.getElementById('statusMessageNotFound').value.trim()
                });
                const settingsFormData=new FormData();
                Object.entries(settingsPayload).forEach(([key,value])=>settingsFormData.append(key,String(value??'')));
                [1,2,3,4].forEach((index)=>{
                    const fileInput=document.getElementById(`galleryItem${index}ImageFile`);
                    if(fileInput&&fileInput.files&&fileInput.files[0])settingsFormData.append(`gallery_item_${index}_file`,fileInput.files[0]);
                });
                await api('__ADMIN_PATH__/settings',{method:'PUT',body:settingsFormData});
                toastMessage('Website changes saved.');
                [1,2,3,4].forEach((index)=>{const fileInput=document.getElementById(`galleryItem${index}ImageFile`);if(fileInput)fileInput.value=''});
                await loadData();
                notifyCrossPageSync('public');
            }catch(error){toastMessage(error.message,'error')}
        });

        document.getElementById('announcementDate').value=new Date().toISOString().slice(0,10);
        checkSession();
    </script>
</body>
</html>
""")

ADMIN_HTML = "".join(ADMIN_HTML_PARTS)
ADMIN_HTML = ADMIN_HTML.replace(ADMIN_PATH_PLACEHOLDER, ADMIN_PANEL_PATH)


initialize_database()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
