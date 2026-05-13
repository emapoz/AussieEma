import os
import sqlite3
import secrets
import calendar
import smtplib
from email.message import EmailMessage
from urllib.parse import quote_plus
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st


# ============================================================
# CONFIGURAZIONE BASE
# ============================================================

APP_TITLE = "Aussie Ema"
APP_SUBTITLE = "Australia, viaggi e scelte vere"
SERVICE_NAME = "Videochiamata Australia & viaggio"
SERVICE_DURATION_MINUTES = 45
BREAK_BETWEEN_CALLS_MINUTES = 15

# Giorni lavorativi: lunedì=0, martedì=1, ..., domenica=6
WORKING_DAYS = [0, 1, 2, 3, 4]

WORK_START = time(9, 0)
WORK_END = time(18, 0)

# Metti None se non vuoi pausa pranzo
LUNCH_BREAK = (time(13, 0), time(14, 0))

BOOKING_MONTHS_AHEAD = 6

DB_PATH = Path("bookings.db")
ASSETS_DIR = Path("assets")

INSTAGRAM_URL = "https://www.instagram.com/_aussie_ema_/"

SOCIAL_LINKS = {
    "Instagram": INSTAGRAM_URL,
}

# ============================================================
# EMAIL / CALENDAR CONFIG
# ============================================================
# Automatic email works when SMTP settings are added in .streamlit/secrets.toml.
# Calendar saving works immediately through Google Calendar links and .ics files.
SITE_URL = "http://localhost:8501"
VIDEO_CALL_LINK = ""  # Example: "https://meet.google.com/xxx-yyyy-zzz"
ADMIN_EMAIL = ""      # Optional: receive a notification when someone books.
REVIEW_FORM_URL = ""  # Optional: Google Form / Typeform / external review form.


REVIEWS: list[dict[str, str]] = [
    {
        "name": "Marco",
        "tag": "Partenza Australia",
        "text": "Mi ha aiutato tanto a capire i primi passi senza vendermi il solito sogno.",
    },
]

DEFAULT_ADMIN_PASSWORD = "admin123"


# ============================================================
# ASSET HELPERS
# ============================================================

def find_asset(stem: str) -> Path | None:
    """
    Cerca immagini in assets/ senza costringerti a usare per forza .jpg.
    Funziona con:
    hero.jpg, hero.png, hero.jpeg, hero.webp
    oppure anche file chiamati semplicemente 'hero'.
    """
    candidates = [
        ASSETS_DIR / stem,
        ASSETS_DIR / f"{stem}.jpg",
        ASSETS_DIR / f"{stem}.jpeg",
        ASSETS_DIR / f"{stem}.png",
        ASSETS_DIR / f"{stem}.webp",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    # fallback case-insensitive
    if ASSETS_DIR.exists():
        for p in ASSETS_DIR.iterdir():
            if p.is_file() and p.stem.lower() == stem.lower():
                return p

    return None


def show_asset(stem: str, caption: str | None = None, use_container_width: bool = True) -> bool:
    path = find_asset(stem)
    if path is None:
        return False

    st.image(path, caption=caption, use_container_width=use_container_width)
    return True


# ============================================================
# DATABASE
# ============================================================

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_start TEXT NOT NULL,
            slot_end TEXT NOT NULL,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            notes TEXT,
            status TEXT NOT NULL DEFAULT 'booked',
            cancel_token TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        """
    )

    # Migration-safe index: both booked and confirmed calls must block the slot.
    conn.execute("DROP INDEX IF EXISTS idx_unique_active_booking_per_slot;")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_booking_per_slot
        ON bookings(slot_start)
        WHERE status IN ('booked', 'confirmed');
        """
    )
    conn.commit()
    conn.close()


def fetch_bookings(
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
    only_active: bool = True,
) -> pd.DataFrame:
    conn = get_connection()

    query = "SELECT * FROM bookings WHERE 1=1"
    params: list[str] = []

    if only_active:
        query += " AND status IN ('booked', 'confirmed')"

    if start_dt is not None:
        query += " AND slot_start >= ?"
        params.append(start_dt.isoformat())

    if end_dt is not None:
        query += " AND slot_start < ?"
        params.append(end_dt.isoformat())

    query += " ORDER BY slot_start ASC"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        return pd.DataFrame(
            columns=[
                "id",
                "slot_start",
                "slot_end",
                "name",
                "email",
                "phone",
                "notes",
                "status",
                "cancel_token",
                "created_at",
            ]
        )

    return pd.DataFrame([dict(row) for row in rows])


def create_booking(
    slot_start: datetime,
    slot_end: datetime,
    name: str,
    email: str,
    phone: str,
    notes: str,
) -> tuple[bool, str, str | None]:
    conn = get_connection()

    try:
        token = secrets.token_urlsafe(24)
        conn.execute(
            """
            INSERT INTO bookings (
                slot_start, slot_end, name, email, phone, notes,
                status, cancel_token, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'booked', ?, ?);
            """,
            (
                slot_start.isoformat(),
                slot_end.isoformat(),
                name.strip(),
                email.strip().lower(),
                phone.strip(),
                notes.strip(),
                token,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return True, "Prenotazione confermata.", token

    except sqlite3.IntegrityError:
        return False, "Questo slot è appena stato prenotato da qualcun altro. Scegline un altro.", None

    finally:
        conn.close()


def cancel_booking(booking_id: int) -> None:
    conn = get_connection()
    conn.execute(
        """
        UPDATE bookings
        SET status = 'cancelled'
        WHERE id = ?;
        """,
        (booking_id,),
    )
    conn.commit()
    conn.close()


def delete_booking_forever(booking_id: int) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM bookings WHERE id = ?;", (booking_id,))
    conn.commit()
    conn.close()


def update_booking_status(booking_id: int, status: str) -> None:
    allowed = {"booked", "confirmed", "done", "no_show", "cancelled"}
    if status not in allowed:
        raise ValueError(f"Invalid booking status: {status}")

    conn = get_connection()
    conn.execute(
        """
        UPDATE bookings
        SET status = ?
        WHERE id = ?;
        """,
        (status, booking_id),
    )
    conn.commit()
    conn.close()


def fetch_booking_by_token(token: str) -> dict | None:
    if not token:
        return None

    conn = get_connection()
    row = conn.execute(
        """
        SELECT *
        FROM bookings
        WHERE cancel_token = ?
        LIMIT 1;
        """,
        (token.strip(),),
    ).fetchone()
    conn.close()

    return dict(row) if row else None


def fetch_booking_by_id(booking_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT *
        FROM bookings
        WHERE id = ?
        LIMIT 1;
        """,
        (booking_id,),
    ).fetchone()
    conn.close()

    return dict(row) if row else None


def status_label(status: str) -> str:
    labels = {
        "booked": "Prenotata",
        "confirmed": "Confermata",
        "done": "Fatta",
        "no_show": "No-show",
        "cancelled": "Cancellata",
    }
    return labels.get(status, status)


def status_emoji(status: str) -> str:
    emojis = {
        "booked": "🟡",
        "confirmed": "🟢",
        "done": "✅",
        "no_show": "⚠️",
        "cancelled": "⚪",
    }
    return emojis.get(status, "•")


# ============================================================
# DATE / SLOT LOGIC
# ============================================================

@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class MonthOption:
    year: int
    month: int
    label: str


def month_name_it(month: int) -> str:
    months = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "June",
        "July",
        "Aug",
        "Sept",
        "Oct",
        "Nov",
        "Dec",
    ]
    return months[month - 1]


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last_day))


def get_month_options(start_day: date, months_ahead: int) -> list[MonthOption]:
    options: list[MonthOption] = []

    for i in range(months_ahead + 1):
        d = add_months(start_day.replace(day=1), i)
        options.append(
            MonthOption(
                year=d.year,
                month=d.month,
                label=f"{month_name_it(d.month).capitalize()} {d.year}",
            )
        )

    return options


def get_days_in_month(year: int, month: int) -> list[date]:
    last_day = calendar.monthrange(year, month)[1]
    return [date(year, month, d) for d in range(1, last_day + 1)]


def monday_of_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def format_slot(slot: Slot) -> str:
    return f"{slot.start.strftime('%H:%M')} - {slot.end.strftime('%H:%M')}"


def format_date_it(day: date) -> str:
    weekdays = [
        "Lunedì",
        "Martedì",
        "Mercoledì",
        "Giovedì",
        "Venerdì",
        "Sabato",
        "Domenica",
    ]

    return f"{weekdays[day.weekday()]} {day.day} {month_name_it(day.month)}"


def overlaps_lunch_break(start_dt: datetime, end_dt: datetime) -> bool:
    if LUNCH_BREAK is None:
        return False

    lunch_start, lunch_end = LUNCH_BREAK
    lunch_start_dt = datetime.combine(start_dt.date(), lunch_start)
    lunch_end_dt = datetime.combine(start_dt.date(), lunch_end)

    return start_dt < lunch_end_dt and end_dt > lunch_start_dt


def generate_slots_for_day(day: date) -> list[Slot]:
    if day.weekday() not in WORKING_DAYS:
        return []

    slots: list[Slot] = []
    current = datetime.combine(day, WORK_START)
    end_of_day = datetime.combine(day, WORK_END)

    step = timedelta(minutes=SERVICE_DURATION_MINUTES + BREAK_BETWEEN_CALLS_MINUTES)
    duration = timedelta(minutes=SERVICE_DURATION_MINUTES)

    while current + duration <= end_of_day:
        slot_end = current + duration

        if not overlaps_lunch_break(current, slot_end):
            slots.append(Slot(start=current, end=slot_end))

        current += step

    return slots


def generate_slots(days: list[date]) -> list[Slot]:
    all_slots: list[Slot] = []
    for day in days:
        all_slots.extend(generate_slots_for_day(day))
    return all_slots


def get_booked_slot_map(start_dt: datetime, end_dt: datetime) -> dict[str, dict]:
    df = fetch_bookings(start_dt=start_dt, end_dt=end_dt, only_active=True)

    booked: dict[str, dict] = {}
    for _, row in df.iterrows():
        booked[row["slot_start"]] = row.to_dict()

    return booked


def is_past_slot(slot: Slot) -> bool:
    return slot.start <= datetime.now()


def get_available_slots_count(day: date, booked_map: dict[str, dict]) -> int:
    count = 0
    for slot in generate_slots_for_day(day):
        if is_past_slot(slot):
            continue
        if slot.start.isoformat() in booked_map:
            continue
        count += 1
    return count


def is_bookable_day(day: date, today: date, max_booking_day: date) -> bool:
    return today <= day <= max_booking_day and day.weekday() in WORKING_DAYS


# ============================================================
# UI HELPERS
# ============================================================

def load_admin_password() -> str:
    try:
        return st.secrets.get("ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD))
    except Exception:
        return os.getenv("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)


def inject_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --ink: #111827;
            --muted: #6b7280;
            --line: rgba(17, 24, 39, 0.10);
            --sand: #f7efe5;
            --sun: #f59e0b;
            --ocean: #0f766e;
            --sky: #e0f2fe;
            --paper: #fffaf3;
        }

        .block-container {
            padding-top: 1.8rem;
            padding-bottom: 3rem;
            max-width: 1180px;
        }

        section[data-testid="stSidebar"] {
            background: linear-gradient(180deg, #fff7ed 0%, #ecfeff 100%);
        }

        .ae-hero {
            position: relative;
            overflow: hidden;
            border-radius: 34px;
            padding: 2.3rem;
            min-height: 420px;
            border: 1px solid rgba(17, 24, 39, 0.10);
            background:
                radial-gradient(circle at top left, rgba(245, 158, 11, 0.34), transparent 32%),
                radial-gradient(circle at bottom right, rgba(15, 118, 110, 0.22), transparent 34%),
                linear-gradient(135deg, #fff7ed 0%, #ecfeff 100%);
            box-shadow: 0 24px 70px rgba(15, 23, 42, 0.10);
            margin-bottom: 1.2rem;
        }

        .ae-kicker {
            display: inline-flex;
            gap: 0.45rem;
            align-items: center;
            font-weight: 800;
            letter-spacing: .03em;
            text-transform: uppercase;
            font-size: .78rem;
            color: #0f766e;
            background: rgba(255,255,255,.75);
            border: 1px solid rgba(15,118,110,.18);
            border-radius: 999px;
            padding: .42rem .75rem;
            margin-bottom: 1rem;
        }

        .ae-hero h1 {
            font-size: clamp(3.2rem, 8vw, 6.4rem);
            line-height: .86;
            letter-spacing: -0.08em;
            margin: 0 0 .9rem 0;
            color: #111827;
        }

        .ae-hero h2 {
            font-size: clamp(1.45rem, 3.2vw, 2.6rem);
            letter-spacing: -0.04em;
            line-height: 1.03;
            max-width: 820px;
            margin: 0 0 1rem 0;
            color: #1f2937;
        }

        .ae-hero p {
            font-size: 1.08rem;
            color: #374151;
            max-width: 710px;
            line-height: 1.65;
            margin-bottom: 0;
        }

        .ae-badge {
            display: inline-block;
            padding: .45rem .78rem;
            border-radius: 999px;
            border: 1px solid rgba(17,24,39,.10);
            background: rgba(255,255,255,.78);
            font-size: .92rem;
            margin-right: .35rem;
            margin-bottom: .45rem;
            color: #1f2937;
            font-weight: 650;
        }

        .ae-card {
            padding: 1.25rem;
            border-radius: 24px;
            border: 1px solid rgba(17,24,39,.10);
            background: rgba(255,255,255,.88);
            box-shadow: 0 14px 40px rgba(15,23,42,.06);
            min-height: 164px;
        }

        .ae-card h4 {
            margin-top: 0;
            font-size: 1.12rem;
        }

        .ae-card p {
            color: #4b5563;
            line-height: 1.55;
        }

        .ae-dark-card {
            padding: 1.4rem;
            border-radius: 26px;
            background: #111827;
            color: white;
            box-shadow: 0 18px 50px rgba(17,24,39,.18);
        }

        .ae-dark-card p {
            color: rgba(255,255,255,.78);
            line-height: 1.6;
        }

        .ae-quote {
            padding: 1.2rem 1.3rem;
            border-radius: 22px;
            background:
                linear-gradient(135deg, rgba(245,158,11,.18), rgba(15,118,110,.12));
            border: 1px solid rgba(17,24,39,.10);
            color: #1f2937;
            font-size: 1.03rem;
            line-height: 1.65;
            margin-top: 1rem;
        }

        .ae-section-title {
            font-size: clamp(1.7rem, 3vw, 2.55rem);
            letter-spacing: -0.055em;
            line-height: 1;
            margin-top: 1.1rem;
            margin-bottom: .7rem;
            color: #111827;
        }

        .ae-small {
            color: #6b7280;
            font-size: .95rem;
        }

        .ae-photo-note {
            padding: .9rem 1rem;
            border-radius: 18px;
            background: #f9fafb;
            border: 1px dashed rgba(17,24,39,.18);
            color: #4b5563;
        }

        .calendar-heading {
            text-align: center;
            font-weight: 800;
            color: #374151;
            padding: 0.35rem 0;
        }

        .calendar-empty {
            min-height: 2.5rem;
        }

        div.stButton > button {
            border-radius: 999px;
            border: 1px solid rgba(17,24,39,.12);
            font-weight: 650;
        }

        div[data-testid="stLinkButton"] > a {
            border-radius: 999px;
            font-weight: 750;
            text-decoration: none;
        }

        [data-testid="stMetric"] {
            background: rgba(255,255,255,.88);
            border: 1px solid rgba(17,24,39,.10);
            padding: 1rem;
            border-radius: 22px;
            box-shadow: 0 10px 32px rgba(15,23,42,.05);
        }

        .stImage img {
            border-radius: 26px;
            box-shadow: 0 18px 50px rgba(15,23,42,.12);
        }

        hr {
            margin-top: 2rem !important;
            margin-bottom: 2rem !important;
        }

        .admin-hero {
            padding: 1.4rem 1.5rem;
            border-radius: 30px;
            background:
                radial-gradient(circle at top left, rgba(245,158,11,.28), transparent 34%),
                radial-gradient(circle at bottom right, rgba(15,118,110,.20), transparent 34%),
                linear-gradient(135deg, #fff7ed 0%, #ecfeff 100%);
            border: 1px solid rgba(17,24,39,.10);
            box-shadow: 0 20px 55px rgba(15,23,42,.08);
            margin-bottom: 1.2rem;
        }

        .admin-hero h1 {
            margin: 0;
            letter-spacing: -0.055em;
            font-size: clamp(2rem, 4vw, 3.6rem);
            line-height: .95;
            color: #111827;
        }

        .admin-hero p {
            color: #4b5563;
            max-width: 760px;
            margin: .65rem 0 0 0;
            line-height: 1.6;
        }

        .admin-day-card {
            padding: .85rem;
            border-radius: 22px;
            background: rgba(255,255,255,.92);
            border: 1px solid rgba(17,24,39,.10);
            box-shadow: 0 12px 34px rgba(15,23,42,.055);
            min-height: 175px;
        }

        .admin-day-card.today {
            border: 1px solid rgba(15,118,110,.45);
            box-shadow: 0 16px 42px rgba(15,118,110,.12);
        }

        .admin-day-title {
            font-weight: 850;
            letter-spacing: -.025em;
            color: #111827;
            margin-bottom: .35rem;
        }

        .admin-slot-pill {
            display: inline-block;
            padding: .22rem .5rem;
            border-radius: 999px;
            font-size: .78rem;
            font-weight: 750;
            margin: .14rem .12rem .14rem 0;
            border: 1px solid rgba(17,24,39,.10);
        }

        .admin-slot-booked {
            background: #111827;
            color: #ffffff;
        }

        .admin-slot-free {
            background: #d1fae5;
            color: #065f46;
        }

        .admin-slot-past {
            background: #f3f4f6;
            color: #6b7280;
        }

        .admin-progress-wrap {
            width: 100%;
            height: 10px;
            border-radius: 999px;
            background: #e5e7eb;
            overflow: hidden;
            margin: .5rem 0;
        }

        .admin-progress-bar {
            height: 10px;
            border-radius: 999px;
            background: linear-gradient(90deg, #0f766e, #f59e0b);
        }

        .timeline-row {
            padding: .9rem 1rem;
            border-radius: 20px;
            border: 1px solid rgba(17,24,39,.10);
            background: rgba(255,255,255,.92);
            box-shadow: 0 10px 26px rgba(15,23,42,.045);
            margin-bottom: .65rem;
        }

        .timeline-row.booked {
            border-left: 6px solid #111827;
        }

        .timeline-row.free {
            border-left: 6px solid #10b981;
        }

        .timeline-time {
            font-size: 1.05rem;
            font-weight: 850;
            color: #111827;
        }

        .timeline-meta {
            color: #6b7280;
            font-size: .92rem;
            line-height: 1.45;
        }

        .mini-label {
            font-size: .76rem;
            letter-spacing: .04em;
            text-transform: uppercase;
            font-weight: 850;
            color: #0f766e;
        }

        .admin-note-box {
            padding: .8rem .95rem;
            border-radius: 16px;
            background: #f9fafb;
            color: #374151;
            border: 1px solid rgba(17,24,39,.08);
            margin-top: .45rem;
        }

        .booking-flow {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: .8rem;
            margin: 1rem 0 1.2rem 0;
        }

        .booking-step {
            padding: 1rem 1.05rem;
            border-radius: 22px;
            background: rgba(255,255,255,.92);
            border: 1px solid rgba(17,24,39,.10);
            box-shadow: 0 12px 32px rgba(15,23,42,.055);
        }

        .booking-step strong {
            display: block;
            color: #111827;
            font-size: 1rem;
            margin-bottom: .25rem;
        }

        .booking-step span {
            color: #6b7280;
            font-size: .92rem;
            line-height: 1.45;
        }

        .selected-slot-card {
            padding: 1.2rem 1.25rem;
            border-radius: 26px;
            background:
                radial-gradient(circle at top left, rgba(245,158,11,.20), transparent 32%),
                radial-gradient(circle at bottom right, rgba(15,118,110,.18), transparent 34%),
                #ffffff;
            border: 1px solid rgba(17,24,39,.10);
            box-shadow: 0 18px 50px rgba(15,23,42,.08);
            margin-bottom: 1rem;
        }

        .selected-slot-card h3 {
            margin: 0 0 .35rem 0;
            letter-spacing: -.035em;
            color: #111827;
        }

        .selected-slot-card p {
            margin: .1rem 0;
            color: #374151;
        }

        .booking-hint {
            padding: .9rem 1rem;
            border-radius: 20px;
            background: #f9fafb;
            border: 1px solid rgba(17,24,39,.08);
            color: #374151;
            line-height: 1.55;
        }

        .success-card {
            padding: 1.25rem;
            border-radius: 26px;
            background:
                linear-gradient(135deg, rgba(209,250,229,.9), rgba(236,254,255,.9));
            border: 1px solid rgba(16,185,129,.25);
            box-shadow: 0 18px 48px rgba(15,23,42,.08);
            margin: 1rem 0;
        }

        .success-card h3 {
            margin: 0 0 .4rem 0;
            letter-spacing: -.035em;
        }

        .review-card {
            padding: 1.25rem;
            border-radius: 26px;
            background: rgba(255,255,255,.92);
            border: 1px solid rgba(17,24,39,.10);
            box-shadow: 0 16px 42px rgba(15,23,42,.06);
            min-height: 180px;
        }

        .review-card h4 {
            margin-top: 0;
            margin-bottom: .25rem;
            color: #111827;
        }

        .review-tag {
            display: inline-block;
            padding: .24rem .55rem;
            border-radius: 999px;
            background: #ecfeff;
            color: #0f766e;
            font-weight: 800;
            font-size: .78rem;
            margin-bottom: .8rem;
        }

        .review-card p {
            color: #374151;
            line-height: 1.6;
        }

        .empty-review-box {
            padding: 1.2rem;
            border-radius: 24px;
            border: 1px dashed rgba(17,24,39,.22);
            background: #fffaf3;
            color: #374151;
        }


        .manage-card {
            padding: 1.2rem 1.25rem;
            border-radius: 26px;
            background: rgba(255,255,255,.92);
            border: 1px solid rgba(17,24,39,.10);
            box-shadow: 0 16px 42px rgba(15,23,42,.06);
            margin-bottom: 1rem;
        }

        .manage-card h3 {
            margin-top: 0;
            letter-spacing: -.035em;
        }

        .status-pill {
            display: inline-block;
            padding: .28rem .65rem;
            border-radius: 999px;
            background: #f3f4f6;
            border: 1px solid rgba(17,24,39,.08);
            font-size: .82rem;
            font-weight: 850;
            color: #111827;
        }

        .prep-card {
            padding: 1rem 1.1rem;
            border-radius: 22px;
            background:
                radial-gradient(circle at top left, rgba(245,158,11,.17), transparent 30%),
                #ffffff;
            border: 1px solid rgba(17,24,39,.10);
            box-shadow: 0 12px 34px rgba(15,23,42,.055);
            min-height: 150px;
        }

        .prep-card h4 {
            margin-top: 0;
        }

        .danger-zone {
            padding: 1rem;
            border-radius: 22px;
            background: #fff1f2;
            border: 1px solid #fecdd3;
            color: #881337;
        }

        </style>
        """,
        unsafe_allow_html=True,
    )


def reset_selected_slot() -> None:
    st.session_state.pop("selected_slot_start", None)
    st.session_state.pop("selected_slot_end", None)


def render_month_pills(month_options: list[MonthOption]) -> MonthOption:
    if "selected_month_index" not in st.session_state:
        st.session_state.selected_month_index = 0

    st.markdown("**Scegli il mese**")

    cols = st.columns(min(len(month_options), 4))

    for idx, month_option in enumerate(month_options):
        with cols[idx % len(cols)]:
            selected = idx == st.session_state.selected_month_index
            prefix = "●" if selected else "○"
            label = f"{prefix} {month_option.label}"

            if st.button(label, key=f"month_pill_{idx}", use_container_width=True):
                st.session_state.selected_month_index = idx
                st.session_state.pop("selected_calendar_day", None)
                reset_selected_slot()
                st.rerun()

    return month_options[st.session_state.selected_month_index]


def render_month_calendar(
    selected_month: MonthOption,
    today: date,
    max_booking_day: date,
) -> date | None:
    first_day = date(selected_month.year, selected_month.month, 1)
    month_days = get_days_in_month(selected_month.year, selected_month.month)

    month_start_dt = datetime.combine(month_days[0], time(0, 0))
    month_end_dt = datetime.combine(month_days[-1] + timedelta(days=1), time(0, 0))
    booked_map = get_booked_slot_map(month_start_dt, month_end_dt)

    st.markdown(f"### {selected_month.label}")

    weekday_labels = ["Lun", "Mar", "Mer", "Gio", "Ven", "Sab", "Dom"]
    header_cols = st.columns(7)

    for idx, label in enumerate(weekday_labels):
        with header_cols[idx]:
            st.markdown(
                f"<div class='calendar-heading'>{label}</div>",
                unsafe_allow_html=True,
            )

    start_padding = first_day.weekday()
    cells: list[date | None] = [None] * start_padding + month_days

    while len(cells) % 7 != 0:
        cells.append(None)

    selected_day_iso = st.session_state.get("selected_calendar_day")
    selected_day = date.fromisoformat(selected_day_iso) if selected_day_iso else None

    for week_index in range(0, len(cells), 7):
        week = cells[week_index: week_index + 7]
        cols = st.columns(7)

        for col_idx, day in enumerate(week):
            with cols[col_idx]:
                if day is None:
                    st.markdown("<div class='calendar-empty'></div>", unsafe_allow_html=True)
                    continue

                bookable = is_bookable_day(day, today, max_booking_day)
                available_count = get_available_slots_count(day, booked_map) if bookable else 0
                is_selected = selected_day == day

                if not bookable:
                    label = f"{day.day}\n—"
                    st.button(label, key=f"day_disabled_{day.isoformat()}", disabled=True, use_container_width=True)
                elif available_count == 0:
                    label = f"{day.day}\nPieno"
                    st.button(label, key=f"day_full_{day.isoformat()}", disabled=True, use_container_width=True)
                else:
                    prefix = "●" if is_selected else "○"
                    label = f"{prefix} {day.day}\n{available_count} slot"

                    if st.button(label, key=f"day_{day.isoformat()}", use_container_width=True):
                        st.session_state.selected_calendar_day = day.isoformat()
                        reset_selected_slot()
                        st.rerun()

    if "selected_calendar_day" in st.session_state:
        return date.fromisoformat(st.session_state.selected_calendar_day)

    return None


def render_slot_picker_for_days(days: list[date], booked_map: dict[str, dict]) -> None:
    if not days:
        st.info("Nessun giorno disponibile nel periodo selezionato.")
        return

    cols = st.columns(len(days))

    for idx, day in enumerate(days):
        with cols[idx]:
            st.markdown(f"**{format_date_it(day)}**")

            slots = generate_slots_for_day(day)

            if not slots:
                st.caption("Nessuno slot")
                continue

            for slot in slots:
                key = slot.start.isoformat()
                occupied = key in booked_map
                past = is_past_slot(slot)

                label = format_slot(slot)

                if occupied:
                    st.button(f"🔒 {label}", disabled=True, key=f"occ_{key}")
                elif past:
                    st.button(f"⏳ {label}", disabled=True, key=f"past_{key}")
                else:
                    if st.button(f"✅ {label}", key=f"free_{key}"):
                        st.session_state.selected_slot_start = slot.start.isoformat()
                        st.session_state.selected_slot_end = slot.end.isoformat()
                        st.rerun()


def render_booking_form() -> None:
    if "selected_slot_start" not in st.session_state:
        st.markdown(
            """
            <div class="booking-hint">
                <strong>Prima scegli uno slot libero.</strong><br>
                Poi ti chiedo solo poche informazioni: nome, email e il dubbio principale.
                L'obiettivo è arrivare alla call già con un minimo di contesto.
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    selected_start = datetime.fromisoformat(st.session_state.selected_slot_start)
    selected_end = datetime.fromisoformat(st.session_state.selected_slot_end)

    st.markdown(
        f"""
        <div class="selected-slot-card">
            <div class="ae-kicker">Slot selezionato</div>
            <h3>{format_date_it(selected_start.date())}</h3>
            <p><strong>{selected_start.strftime('%H:%M')} - {selected_end.strftime('%H:%M')}</strong> · {SERVICE_DURATION_MINUTES} minuti</p>
            <p>Call con Aussie Ema · Australia, viaggi e scelte autentiche</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("booking_form", clear_on_submit=False):
        st.subheader("Ultimo step: raccontami da dove parti")
        st.caption("Tieni il messaggio semplice. Non serve scrivere un tema: bastano due righe oneste.")

        topic = st.selectbox(
            "Tema principale della call *",
            [
                "Voglio capire se partire per l'Australia ha senso per me",
                "Ho dubbi sui primi passi pratici",
                "Voglio parlare di lavoro e aspettative",
                "Voglio una visione più realistica della vita lì",
                "Ho già deciso di partire e voglio organizzarmi meglio",
                "Altro",
            ],
        )

        name = st.text_input("Nome e cognome *", value=st.session_state.get("prefill_name", ""))
        email = st.text_input("Email *", value=st.session_state.get("prefill_email", ""))
        phone = st.text_input("Telefono, opzionale", value=st.session_state.get("prefill_phone", ""))
        notes = st.text_area(
            "Il dubbio più grande che vuoi portare in call",
            value=st.session_state.get("prefill_notes", ""),
            placeholder=(
                "Esempio: vorrei partire ma ho paura di non trovare lavoro, "
                "non so da quale città iniziare, non capisco quanto budget serva, "
                "oppure ho bisogno di un confronto sincero prima di decidere."
            ),
        )

        st.markdown(
            """
            <div class="booking-hint">
                <strong>Nota importante.</strong> Questa non è una consulenza legale o da migration agent.
                È una call pratica e personale basata sull'esperienza.
            </div>
            """,
            unsafe_allow_html=True,
        )

        col_a, col_b = st.columns([1, 1])
        submitted = col_a.form_submit_button("Conferma la call", type="primary")
        cancel_selection = col_b.form_submit_button("Cambio slot")

    if cancel_selection:
        reset_selected_slot()
        st.rerun()

    if submitted:
        if not name.strip() or not email.strip():
            st.error("Nome ed email sono obbligatori.")
            return

        if "@" not in email or "." not in email:
            st.error("Inserisci una email valida.")
            return

        full_notes = f"Tema principale: {topic}\n\n{notes.strip()}".strip()

        success, message, booking_token = create_booking(
            slot_start=selected_start,
            slot_end=selected_end,
            name=name,
            email=email,
            phone=phone,
            notes=full_notes,
        )

        if success:
            st.balloons()
            st.success(message)

            details = (
                f"Call con Aussie Ema\n"
                f"Nome: {name}\n"
                f"Email: {email}\n"
                f"Telefono: {phone}\n\n"
                f"Tema principale: {topic}\n\n"
                f"Note:\n{notes}\n\n"
                f"Instagram: {INSTAGRAM_URL}"
            )
            location = VIDEO_CALL_LINK or "Videochiamata"

            gcal_url = google_calendar_url(
                SERVICE_NAME,
                selected_start,
                selected_end,
                details,
                location,
            )
            ics_content = build_ics_content(
                SERVICE_NAME,
                selected_start,
                selected_end,
                details,
                location,
            )

            sent_to_user, user_email_msg = send_email(
                to_email=email,
                subject="Conferma videochiamata con Aussie Ema",
                body=build_booking_email_body_with_manage_link(name, selected_start, selected_end, full_notes, booking_token),
            )

            if ADMIN_EMAIL:
                send_email(
                    to_email=ADMIN_EMAIL,
                    subject=f"Nuova prenotazione: {name}",
                    body=(
                        f"Nuova prenotazione\n\n"
                        f"Nome: {name}\n"
                        f"Email: {email}\n"
                        f"Telefono: {phone}\n"
                        f"Quando: {selected_start.strftime('%d/%m/%Y %H:%M')} - {selected_end.strftime('%H:%M')}\n\n"
                        f"Tema principale: {topic}\n\n"
                        f"Note:\n{notes}"
                    ),
                    reply_to=email,
                )

            st.markdown(
                f"""
                <div class="success-card">
                    <h3>Sei prenotato.</h3>
                    <p><strong>{format_date_it(selected_start.date())}</strong> · {selected_start.strftime('%H:%M')} - {selected_end.strftime('%H:%M')}</p>
                    <p>Ho ricevuto la tua richiesta. Se hai lasciato qualche nota, la leggerò prima della call.</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

            if sent_to_user:
                st.success("Ti ho inviato anche una email di conferma.")
            else:
                st.caption(f"Email automatica non inviata: {user_email_msg}")

            st.markdown('<div class="ae-section-title">Prima della call</div>', unsafe_allow_html=True)
            prep1, prep2, prep3 = st.columns(3)
            with prep1:
                st.markdown("<div class='prep-card'><h4>1. Perché vuoi partire?</h4><p>Prova a separare il desiderio vero dalla pressione esterna o dal contenuto visto online.</p></div>", unsafe_allow_html=True)
            with prep2:
                st.markdown("<div class='prep-card'><h4>2. Cosa ti blocca?</h4><p>Paura, soldi, lavoro, inglese, famiglia, città: porta il dubbio più concreto.</p></div>", unsafe_allow_html=True)
            with prep3:
                st.markdown("<div class='prep-card'><h4>3. Cosa vuoi decidere?</h4><p>La call funziona meglio se usciamo con un prossimo passo chiaro.</p></div>", unsafe_allow_html=True)

            cal_cols = st.columns([1, 1, 1])
            with cal_cols[0]:
                st.link_button("Salva su Google Calendar", gcal_url, use_container_width=True)
            with cal_cols[1]:
                st.download_button(
                    "Scarica evento .ics",
                    data=ics_content,
                    file_name="aussie_ema_call.ics",
                    mime="text/calendar",
                    use_container_width=True,
                    key="ics_after_booking",
                )
            with cal_cols[2]:
                st.link_button("Gestisci prenotazione", manage_booking_url(booking_token), use_container_width=True)

            reset_selected_slot()
        else:
            st.error(message)
            reset_selected_slot()
            st.rerun()


# ============================================================
# EMAIL / CALENDAR HELPERS
# ============================================================

def get_secret_or_env(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name, os.getenv(name, default))
    except Exception:
        value = os.getenv(name, default)
    return str(value) if value is not None else default


def smtp_is_configured() -> bool:
    return bool(
        get_secret_or_env("SMTP_HOST")
        and get_secret_or_env("SMTP_PORT")
        and get_secret_or_env("SMTP_USER")
        and get_secret_or_env("SMTP_PASSWORD")
        and get_secret_or_env("SMTP_FROM")
    )


def send_email(to_email: str, subject: str, body: str, reply_to: str | None = None) -> tuple[bool, str]:
    """
    Sends email through SMTP if configured in .streamlit/secrets.toml.
    """
    if not smtp_is_configured():
        return False, "SMTP non configurato."

    try:
        smtp_host = get_secret_or_env("SMTP_HOST")
        smtp_port = int(get_secret_or_env("SMTP_PORT", "587"))
        smtp_user = get_secret_or_env("SMTP_USER")
        smtp_password = get_secret_or_env("SMTP_PASSWORD")
        smtp_from = get_secret_or_env("SMTP_FROM")

        msg = EmailMessage()
        msg["From"] = smtp_from
        msg["To"] = to_email
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        return True, "Email inviata."

    except Exception as exc:
        return False, f"Errore invio email: {exc}"


def build_booking_email_body(name: str, slot_start: datetime, slot_end: datetime, notes: str = "") -> str:
    meet_line = f"\nLink videochiamata: {VIDEO_CALL_LINK}\n" if VIDEO_CALL_LINK else ""
    notes_line = f"\nNote che mi hai lasciato:\n{notes}\n" if notes.strip() else ""

    return f"""Ciao {name},

la tua videochiamata è confermata.

Quando:
{format_date_it(slot_start.date())}
{slot_start.strftime('%H:%M')} - {slot_end.strftime('%H:%M')}

Call:
{SERVICE_NAME}
{meet_line}{notes_line}
A presto,
Ema
"""



def manage_booking_url(token: str | None) -> str:
    if not token:
        return SITE_URL
    base = SITE_URL.rstrip("/")
    return f"{base}/?page=Gestisci%20prenotazione&token={quote_plus(token)}"


def build_booking_email_body_with_manage_link(
    name: str,
    slot_start: datetime,
    slot_end: datetime,
    notes: str = "",
    token: str | None = None,
) -> str:
    meet_line = f"\nLink videochiamata: {VIDEO_CALL_LINK}\n" if VIDEO_CALL_LINK else ""
    notes_line = f"\nNote che mi hai lasciato:\n{notes}\n" if notes.strip() else ""
    manage_line = f"\nGestisci la prenotazione:\n{manage_booking_url(token)}\n" if token else ""

    return f"""Ciao {name},

la tua videochiamata è confermata.

Quando:
{format_date_it(slot_start.date())}
{slot_start.strftime('%H:%M')} - {slot_end.strftime('%H:%M')}

Call:
{SERVICE_NAME}
{meet_line}{notes_line}{manage_line}
Prima della call, prova a pensare a queste tre cose:
1. Perché vuoi partire?
2. Cosa ti blocca davvero?
3. Qual è la domanda più importante che vuoi farmi?

A presto,
Ema
"""


def build_reminder_email_body(row: dict | pd.Series) -> str:
    slot_start, slot_end = booking_to_datetimes(row)
    meet_line = f"\nLink videochiamata: {VIDEO_CALL_LINK}\n" if VIDEO_CALL_LINK else ""

    return f"""Ciao {row.get('name', '')},

ti ricordo la nostra videochiamata.

Quando:
{format_date_it(slot_start.date())}
{slot_start.strftime('%H:%M')} - {slot_end.strftime('%H:%M')}

{meet_line}
Se vuoi arrivare preparato, porta una domanda concreta:
qual è il dubbio principale che vuoi chiarire?

A presto,
Ema
"""


def build_review_request_email_body(row: dict | pd.Series) -> str:
    review_line = f"\nPuoi lasciarmi una recensione qui:\n{REVIEW_FORM_URL}\n" if REVIEW_FORM_URL else "\nPuoi rispondere direttamente a questa email o scrivermi su Instagram.\n"

    return f"""Ciao {row.get('name', '')},

grazie per la call.

Se ti è stata utile, mi farebbe piacere ricevere un tuo feedback.
Mi aiuta a migliorare e, se mi dai il permesso, posso usarlo nella pagina recensioni del sito.

{review_line}
Grazie ancora,
Ema
"""

def google_calendar_url(title: str, slot_start: datetime, slot_end: datetime, details: str = "", location: str = "") -> str:
    start = slot_start.strftime("%Y%m%dT%H%M%S")
    end = slot_end.strftime("%Y%m%dT%H%M%S")
    return (
        "https://calendar.google.com/calendar/render"
        f"?action=TEMPLATE"
        f"&text={quote_plus(title)}"
        f"&dates={start}/{end}"
        f"&details={quote_plus(details)}"
        f"&location={quote_plus(location)}"
    )


def build_ics_content(title: str, slot_start: datetime, slot_end: datetime, description: str = "", location: str = "") -> str:
    now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    start = slot_start.strftime("%Y%m%dT%H%M%S")
    end = slot_end.strftime("%Y%m%dT%H%M%S")
    uid = f"{secrets.token_hex(12)}@aussie-ema-booking"

    def clean(value: str) -> str:
        return (
            str(value).replace("\\", "\\\\")
            .replace("\n", "\\n")
            .replace(",", "\\,")
            .replace(";", "\\;")
        )

    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Aussie Ema//Booking//IT
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:{uid}
DTSTAMP:{now_utc}
DTSTART:{start}
DTEND:{end}
SUMMARY:{clean(title)}
DESCRIPTION:{clean(description)}
LOCATION:{clean(location)}
END:VEVENT
END:VCALENDAR
"""


def booking_calendar_details(row: dict | pd.Series) -> tuple[str, str, str]:
    name = str(row.get("name", ""))
    email = str(row.get("email", ""))
    phone = str(row.get("phone", ""))
    notes = str(row.get("notes", ""))

    title = f"{SERVICE_NAME} - {name}".strip()
    location = VIDEO_CALL_LINK or "Videochiamata"
    details = (
        f"Prenotazione con {name}\n"
        f"Email: {email}\n"
        f"Telefono: {phone}\n\n"
        f"Note:\n{notes}\n\n"
        f"Instagram: {INSTAGRAM_URL}"
    )
    return title, details, location


def booking_to_datetimes(row: dict | pd.Series) -> tuple[datetime, datetime]:
    return datetime.fromisoformat(str(row["slot_start"])), datetime.fromisoformat(str(row["slot_end"]))


def count_day_status(day: date, booked_map: dict[str, dict]) -> tuple[int, int, int]:
    total = booked = free = 0
    for slot in generate_slots_for_day(day):
        total += 1
        if slot.start.isoformat() in booked_map:
            booked += 1
        elif not is_past_slot(slot):
            free += 1
    return total, booked, free


def render_admin_week_calendar(week_start: date) -> None:
    week_days = [week_start + timedelta(days=i) for i in range(7)]
    start_dt = datetime.combine(week_days[0], time(0, 0))
    end_dt = datetime.combine(week_days[-1] + timedelta(days=1), time(0, 0))
    booked_map = get_booked_slot_map(start_dt, end_dt)

    cols = st.columns(7)

    for idx, day in enumerate(week_days):
        slots = generate_slots_for_day(day)
        total, booked, free = count_day_status(day, booked_map)
        pct = int((booked / total) * 100) if total else 0
        is_today = day == date.today()

        pills_html = ""
        for slot in slots:
            key = slot.start.isoformat()
            label = slot.start.strftime("%H:%M")
            if key in booked_map:
                cls = "admin-slot-booked"
                label = f"{label} booked"
            elif is_past_slot(slot):
                cls = "admin-slot-past"
                label = f"{label} past"
            else:
                cls = "admin-slot-free"
                label = f"{label} free"
            pills_html += f"<span class='admin-slot-pill {cls}'>{label}</span>"

        if not slots:
            pills_html = "<span class='admin-slot-pill admin-slot-past'>no slots</span>"

        today_class = "today" if is_today else ""

        with cols[idx]:
            st.markdown(
                f"""
                <div class="admin-day-card {today_class}">
                    <div class="admin-day-title">{format_date_it(day)}</div>
                    <div class="mini-label">{booked} booked · {free} free</div>
                    <div class="admin-progress-wrap">
                        <div class="admin-progress-bar" style="width:{pct}%"></div>
                    </div>
                    <div>{pills_html}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_admin_day_timeline(selected_day: date) -> None:
    start_dt = datetime.combine(selected_day, time(0, 0))
    end_dt = start_dt + timedelta(days=1)
    booked_map = get_booked_slot_map(start_dt, end_dt)
    slots = generate_slots_for_day(selected_day)

    if not slots:
        st.info("Nessuno slot configurato per questo giorno.")
        return

    for slot in slots:
        key = slot.start.isoformat()
        booking = booked_map.get(key)

        if booking:
            name = booking.get("name", "")
            email = booking.get("email", "")
            phone = booking.get("phone", "")
            notes = booking.get("notes", "")

            st.markdown(
                f"""
                <div class="timeline-row booked">
                    <div class="timeline-time">🔒 {slot.start.strftime('%H:%M')} - {slot.end.strftime('%H:%M')}</div>
                    <div><strong>{name}</strong> · <span class="status-pill">{status_emoji(str(booking.get("status", "")))} {status_label(str(booking.get("status", "")))}</span></div>
                    <div class="timeline-meta">{email} · {phone or "no phone"}</div>
                    <div class="admin-note-box">{notes or "Nessuna nota lasciata."}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            b_start, b_end = booking_to_datetimes(booking)
            title, details, location = booking_calendar_details(booking)
            gcal_url = google_calendar_url(title, b_start, b_end, details, location)
            ics = build_ics_content(title, b_start, b_end, details, location)

            action_cols = st.columns([1, 1, 1, 1, 1, 1, 1])
            with action_cols[0]:
                st.link_button("Calendar", gcal_url, use_container_width=True)
            with action_cols[1]:
                st.download_button(
                    ".ics",
                    data=ics,
                    file_name=f"booking_{booking['id']}.ics",
                    mime="text/calendar",
                    use_container_width=True,
                    key=f"ics_admin_{booking['id']}",
                )
            with action_cols[2]:
                mailto = f"mailto:{email}?subject={quote_plus('Info sulla tua call con Aussie Ema')}"
                st.link_button("Email", mailto, use_container_width=True)
            with action_cols[3]:
                if st.button("Conferma", key=f"confirm_from_timeline_{booking['id']}", use_container_width=True):
                    update_booking_status(int(booking["id"]), "confirmed")
                    st.success("Call confermata.")
                    st.rerun()
            with action_cols[4]:
                if st.button("Fatta", key=f"done_from_timeline_{booking['id']}", use_container_width=True):
                    update_booking_status(int(booking["id"]), "done")
                    st.success("Call segnata come fatta.")
                    st.rerun()
            with action_cols[5]:
                if st.button("Cancella", key=f"cancel_from_timeline_{booking['id']}", use_container_width=True):
                    cancel_booking(int(booking["id"]))
                    st.success("Prenotazione cancellata. Slot liberato.")
                    st.rerun()
            with action_cols[6]:
                delete_confirm = st.checkbox("Del", key=f"delete_check_timeline_{booking['id']}")
                if st.button("Elimina", key=f"delete_from_timeline_{booking['id']}", disabled=not delete_confirm, use_container_width=True):
                    delete_booking_forever(int(booking["id"]))
                    st.success("Prenotazione eliminata definitivamente.")
                    st.rerun()

        else:
            status = "⏳ passato" if is_past_slot(slot) else "✅ libero"
            st.markdown(
                f"""
                <div class="timeline-row free">
                    <div class="timeline-time">{status} · {slot.start.strftime('%H:%M')} - {slot.end.strftime('%H:%M')}</div>
                    <div class="timeline-meta">Slot non prenotato</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render_next_bookings_panel(active_df: pd.DataFrame, limit: int = 6) -> None:
    st.subheader("Prossime call")

    if active_df.empty:
        st.info("Nessuna call in programma nel periodo selezionato.")
        return

    upcoming = active_df.copy()
    upcoming["slot_start_dt"] = pd.to_datetime(upcoming["slot_start"])
    upcoming = upcoming[upcoming["slot_start_dt"] >= pd.Timestamp.now()]
    upcoming = upcoming.sort_values("slot_start_dt").head(limit)

    if upcoming.empty:
        st.info("Nessuna call futura nel periodo selezionato.")
        return

    for _, row in upcoming.iterrows():
        b_start, b_end = booking_to_datetimes(row)
        title, details, location = booking_calendar_details(row)
        gcal_url = google_calendar_url(title, b_start, b_end, details, location)

        with st.container(border=True):
            left, right = st.columns([1.35, 0.65])
            with left:
                st.markdown(f"**{b_start.strftime('%d/%m/%Y · %H:%M')}** · {row['name']}")
                st.caption(f"{row['email']} · {row.get('phone', '') or 'no phone'}")
                if str(row.get("notes", "")).strip():
                    st.write(str(row["notes"])[:220])
            with right:
                st.link_button("Google Calendar", gcal_url, use_container_width=True)
                st.link_button(
                    "Email",
                    f"mailto:{row['email']}?subject={quote_plus('Info sulla tua call con Aussie Ema')}",
                    use_container_width=True,
                )


def render_email_settings_box() -> None:
    with st.expander("Email automatiche e calendario: stato integrazioni"):
        if smtp_is_configured():
            st.success("SMTP configurato: puoi inviare email automatiche dal sito.")
        else:
            st.warning("SMTP non configurato: il sito crea link email, Google Calendar e .ics, ma non invia email automatiche.")

        st.write(
            """
            **Email automatiche**
            Sì, si possono mandare direttamente dal sito. In questa versione ho già messo il codice SMTP.
            Devi solo aggiungere le credenziali in `.streamlit/secrets.toml`.

            ```toml
            SMTP_HOST = "smtp.gmail.com"
            SMTP_PORT = "587"
            SMTP_USER = "tua_email@gmail.com"
            SMTP_PASSWORD = "app_password_google"
            SMTP_FROM = "tua_email@gmail.com"
            ```

            **Google Calendar**
            In questa versione hai già:
            - link "Google Calendar"
            - download file `.ics`
            - link email rapido

            Per creare automaticamente eventi nel calendario di Ema serve Google Calendar API con OAuth.
            Lo farei nello step successivo, perché richiede consenso Google, credentials e refresh token.
            """
        )


# ============================================================
# PAGES
# ============================================================

def page_landing() -> None:
    hero_col, photo_col = st.columns([1.35, 0.85], vertical_alignment="center")

    with hero_col:
        st.markdown(
            f"""
            <div class="ae-hero">
                <div class="ae-kicker">🌏 Australia, senza filtri</div>
                <h1>{APP_TITLE}</h1>
                <h2>{APP_SUBTITLE}</h2>
                <p>
                    Ciao, sono Ema. Se stai pensando di partire per l'Australia ma hai mille domande,
                    ti offro una call per fare chiarezza. Ti racconto quello che ho vissuto,
                    gli errori che eviterei e le cose che avrei voluto sapere prima.
                </p>
                <br>
                <span class="ae-badge">working holiday</span>
                <span class="ae-badge">primi passi</span>
                <span class="ae-badge">vita vera</span>
                <span class="ae-badge">scelte consapevoli</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with photo_col:
        if not show_asset("hero", caption="Aussie mood", use_container_width=True):
            st.markdown(
                """
                <div class="ae-photo-note">
                    Aggiungi la foto hero in <code>assets/hero.jpg</code>
                    o <code>assets/hero.png</code>
                </div>
                """,
                unsafe_allow_html=True,
            )

    cta1, cta2, cta3 = st.columns([1, 1, 1])

    with cta1:
        if st.button("Prenota una call", type="primary", use_container_width=True):
            st.session_state.page = "Prenota"
            st.rerun()

    with cta2:
        st.link_button("Instagram @_aussie_ema_", INSTAGRAM_URL, use_container_width=True)

    with cta3:
        st.button("45 min · 30 Euro", disabled=True, use_container_width=True)

    st.divider()

    intro_img, intro_text = st.columns([0.78, 1.22], vertical_alignment="center")

    with intro_img:
        if not show_asset("profile", caption="Ema", use_container_width=True):
            st.info("Aggiungi `assets/profile.jpg`")

    with intro_text:
        st.markdown('<div class="ae-section-title">Non ti vendo il sogno perfetto.</div>', unsafe_allow_html=True)
        st.write(
            """
            Ti aiuto a guardare l'idea di partire con più lucidità.

            L'Australia può essere un'esperienza enorme, ma non è uguale per tutti.
            Dipende da cosa cerchi, da quanto sei pronto a metterti in gioco e da quanto sai organizzarti
            prima di arrivare lì.

            In call partiamo da te: cosa vuoi fare, cosa ti blocca, cosa immagini e cosa invece
            forse stai dando per scontato.
            """
        )
        st.markdown(
            """
            <div class="ae-quote">
                “Ti parlo da persona che ci è passata: senza fare il guru, senza promettere miracoli,
                ma dicendoti le cose che secondo me contano davvero.”
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.divider()

    st.markdown('<div class="ae-section-title">Cosa possiamo chiarire insieme</div>', unsafe_allow_html=True)

    card1, card2, card3 = st.columns(3)

    with card1:
        st.markdown(
            """
            <div class="ae-card">
                <h4>🌏 Parto o non parto?</h4>
                <p>
                    Mettiamo ordine tra entusiasmo, paura, aspettative e motivazioni.
                    Non è una scelta da fare solo perché “lo fanno tutti”.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with card2:
        st.markdown(
            """
            <div class="ae-card">
                <h4>🎒 Cosa preparo prima?</h4>
                <p>
                    Parliamo dei primi passi pratici, delle cose da non lasciare al caso
                    e degli errori che ti complicano la vita appena arrivi.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with card3:
        st.markdown(
            """
            <div class="ae-card">
                <h4>🧭 Che vita mi aspetta?</h4>
                <p>
                    Ti racconto il lato bello e quello meno instagrammabile:
                    adattamento, lavoro, routine, amicizie e momenti no.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.write("")
    card4, card5, card6 = st.columns(3)

    with card4:
        st.markdown(
            """
            <div class="ae-card">
                <h4>💬 Dubbi personali</h4>
                <p>
                    Puoi farmi domande specifiche sulla tua situazione.
                    Meglio una domanda concreta che mille video salvati e mai guardati.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with card5:
        st.markdown(
            """
            <div class="ae-card">
                <h4>⚠️ Aspettative realistiche</h4>
                <p>
                    Ti aiuto a distinguere tra sogno, contenuto social e vita quotidiana.
                    È qui che spesso si capisce se partire ha davvero senso.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with card6:
        st.markdown(
            """
            <div class="ae-dark-card">
                <h4>🔥 Zero fuffa</h4>
                <p>
                    Se una cosa secondo me è sottovalutata, te lo dico.
                    Se stai idealizzando troppo, te lo dico.
                    Lo scopo è farti pensare meglio, non convincerti a partire.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.divider()

    st.markdown('<div class="ae-section-title">Un po’ di Australia, un po’ di me</div>', unsafe_allow_html=True)

    gallery1, gallery2 = st.columns(2)

    with gallery1:
        if not show_asset("australia_1", caption="Momenti dall'Australia", use_container_width=True):
            st.info("Aggiungi `assets/australia_1.jpg`")

    with gallery2:
        if not show_asset("australia_2", caption="Esperienze vere, non brochure", use_container_width=True):
            st.info("Aggiungi `assets/australia_2.jpg`")

    st.divider()

    st.markdown('<div class="ae-section-title">Come funziona la call</div>', unsafe_allow_html=True)

    step1, step2, step3 = st.columns(3)

    with step1:
        st.markdown(
            """
            <div class="ae-card">
                <h4>1. Scegli lo slot</h4>
                <p>
                    Vai al calendario, scegli mese e giorno oppure scorri settimana per settimana.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with step2:
        st.markdown(
            """
            <div class="ae-card">
                <h4>2. Mi scrivi il contesto</h4>
                <p>
                    Due righe bastano: dove sei ora, cosa vuoi fare e quali sono i dubbi più grossi.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with step3:
        st.markdown(
            """
            <div class="ae-card">
                <h4>3. Ci sentiamo</h4>
                <p>
                    Facciamo una call semplice, concreta e senza pressione.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.write("")

    final_cta_left, final_cta_right = st.columns([1.2, 0.8], vertical_alignment="center")

    with final_cta_left:
        st.markdown(
            """
            <div class="ae-quote">
                Se hai già l'idea in testa ma ti manca una persona con cui parlarne seriamente,
                prenota uno slot. Non serve avere già tutto chiaro: anzi, partiamo proprio da lì.
            </div>
            """,
            unsafe_allow_html=True,
        )

    with final_cta_right:
        if st.button("Prenota la tua videochiamata", type="primary", use_container_width=True):
            st.session_state.page = "Prenota"
            st.rerun()
        st.link_button("Guardami prima su Instagram", INSTAGRAM_URL, use_container_width=True)
        if st.button("Leggi recensioni", use_container_width=True):
            st.session_state.page = "Recensioni"
            st.rerun()




def page_manage_booking() -> None:
    st.markdown(
        """
        <div class="admin-hero">
            <div class="ae-kicker">🧭 Gestione prenotazione</div>
            <h1>Gestisci la tua call</h1>
            <p>
                Da qui puoi controllare la prenotazione, cancellarla o riprogrammare scegliendo un nuovo slot.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        query_token = st.query_params.get("token", "")
    except Exception:
        query_token = ""

    token = st.text_input("Codice prenotazione", value=query_token)
    booking = fetch_booking_by_token(token)

    if not token:
        st.info("Inserisci il codice prenotazione ricevuto nel link di gestione.")
        return

    if booking is None:
        st.error("Non ho trovato nessuna prenotazione con questo codice.")
        return

    slot_start, slot_end = booking_to_datetimes(booking)
    status = str(booking.get("status", ""))

    st.markdown(
        f"""
        <div class="manage-card">
            <span class="status-pill">{status_emoji(status)} {status_label(status)}</span>
            <h3>{format_date_it(slot_start.date())}</h3>
            <p><strong>{slot_start.strftime('%H:%M')} - {slot_end.strftime('%H:%M')}</strong> · {SERVICE_DURATION_MINUTES} minuti</p>
            <p>{booking.get("name", "")} · {booking.get("email", "")}</p>
            <p class="ae-small">{booking.get("notes", "") or "Nessuna nota lasciata."}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if status in {"cancelled", "done", "no_show"}:
        st.warning("Questa prenotazione non è più modificabile da qui.")
        if st.button("Prenota una nuova call", type="primary"):
            st.session_state.page = "Prenota"
            st.rerun()
        return

    title, details, location = booking_calendar_details(booking)
    gcal_url = google_calendar_url(title, slot_start, slot_end, details, location)
    ics = build_ics_content(title, slot_start, slot_end, details, location)

    c1, c2 = st.columns(2)
    with c1:
        st.link_button("Salva su Google Calendar", gcal_url, use_container_width=True)
    with c2:
        st.download_button(
            "Scarica evento .ics",
            data=ics,
            file_name="aussie_ema_call.ics",
            mime="text/calendar",
            use_container_width=True,
            key="ics_manage_user",
        )

    st.divider()

    action1, action2 = st.columns(2)

    with action1:
        st.markdown(
            """
            <div class="danger-zone">
                <strong>Cancella la call</strong><br>
                Libera lo slot. Potrai prenotare di nuovo quando vuoi.
            </div>
            """,
            unsafe_allow_html=True,
        )
        confirm_cancel = st.checkbox("Confermo che voglio cancellare questa call")
        if st.button("Cancella prenotazione", disabled=not confirm_cancel, use_container_width=True):
            cancel_booking(int(booking["id"]))
            st.success("Prenotazione cancellata. Lo slot è stato liberato.")
            st.rerun()

    with action2:
        st.markdown(
            """
            <div class="manage-card">
                <strong>Riprogramma</strong><br>
                Cancello questa prenotazione e ti porto al calendario per scegliere un nuovo slot.
            </div>
            """,
            unsafe_allow_html=True,
        )
        confirm_reschedule = st.checkbox("Confermo che voglio riprogrammare")
        if st.button("Riprogramma call", disabled=not confirm_reschedule, use_container_width=True):
            cancel_booking(int(booking["id"]))
            st.session_state.prefill_name = booking.get("name", "")
            st.session_state.prefill_email = booking.get("email", "")
            st.session_state.prefill_phone = booking.get("phone", "")
            st.session_state.prefill_notes = booking.get("notes", "")
            st.session_state.page = "Prenota"
            st.success("Vecchia prenotazione cancellata. Ora scegli un nuovo slot.")
            st.rerun()


def page_reviews() -> None:
    st.markdown(
        """
        <div class="admin-hero">
            <div class="ae-kicker">⭐ Recensioni</div>
            <h1>Cosa dicono dopo la call</h1>
            <p>
                Qui raccolgo feedback reali di persone con cui ho parlato.
                Niente frasi finte, niente numeri gonfiati: solo impressioni vere quando ci saranno.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not REVIEWS:
        left, right = st.columns([1.1, 0.9], vertical_alignment="center")

        with left:
            st.markdown(
                """
                <div class="empty-review-box">
                    <h3>Le recensioni arriveranno qui.</h3>
                    <p>
                        Meglio lasciare questa pagina onesta e vuota, piuttosto che inventare testimonianze.
                        Quando arrivano i primi feedback veri, li puoi aggiungere nella lista <code>REVIEWS</code>
                        in alto nel file <code>app.py</code>.
                    </p>
                </div>
                """,
                unsafe_allow_html=True,
            )

        with right:
            st.markdown(
                """
                <div class="ae-card">
                    <h4>Hai già fatto una call?</h4>
                    <p>
                        Scrivimi su Instagram e raccontami se ti è stata utile.
                        Con il tuo permesso, posso aggiungere qui il tuo feedback.
                    </p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.link_button("Scrivimi su Instagram", INSTAGRAM_URL, use_container_width=True)

    else:
        cols = st.columns(3)

        for idx, review in enumerate(REVIEWS):
            with cols[idx % 3]:
                st.markdown(
                    f"""
                    <div class="review-card">
                        <h4>{review.get("name", "Anonimo")}</h4>
                        <span class="review-tag">{review.get("tag", "Call Australia")}</span>
                        <p>“{review.get("text", "")}”</p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    st.divider()

    st.markdown('<div class="ae-section-title">Vuoi parlarne anche tu?</div>', unsafe_allow_html=True)
    st.write(
        """
        Se stai pensando all'Australia e vuoi un confronto concreto, prenota una call.
        Non devi avere già tutto chiaro: spesso la call serve proprio a capire da dove partire.
        """
    )

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("Prenota una call", type="primary", use_container_width=True):
            st.session_state.page = "Prenota"
            st.rerun()
    with c2:
        st.link_button("Vai su Instagram", INSTAGRAM_URL, use_container_width=True)


def page_booking() -> None:
    st.title("Prenota una videochiamata")
    st.caption(
        f"Durata: {SERVICE_DURATION_MINUTES} minuti. "
        "Gli slot occupati non sono prenotabili."
    )

    st.markdown(
        """
        <div class="booking-flow">
            <div class="booking-step">
                <strong>1. Scegli il giorno</strong>
                <span>Puoi usare il calendario mensile oppure la vista settimana per settimana.</span>
            </div>
            <div class="booking-step">
                <strong>2. Scegli l'orario</strong>
                <span>Vedi solo slot liberi. Quelli occupati non sono prenotabili.</span>
            </div>
            <div class="booking-step">
                <strong>3. Lascia il contesto</strong>
                <span>Scrivimi il dubbio principale, così arrivo preparato alla call.</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption("Orari mostrati in ora italiana. Se sei già all'estero, controlla bene il fuso orario.")

    today = date.today()
    max_booking_day = add_months(today, BOOKING_MONTHS_AHEAD)

    weekly_mode = st.toggle(
        "Vista settimana per settimana",
        value=st.session_state.get("booking_mode", "day") == "week",
        help="Disattivato: scegli mese e giorno dal calendario. Attivato: scorri settimana per settimana.",
    )

    mode = "week" if weekly_mode else "day"

    if st.session_state.get("booking_mode") != mode:
        st.session_state.booking_mode = mode
        reset_selected_slot()
        st.rerun()

    st.divider()

    if mode == "day":
        st.subheader("Scegli mese e giorno")

        month_options = get_month_options(today, BOOKING_MONTHS_AHEAD)
        selected_month = render_month_pills(month_options)

        selected_day = render_month_calendar(
            selected_month=selected_month,
            today=today,
            max_booking_day=max_booking_day,
        )

        if selected_day is None:
            st.info("Seleziona un giorno disponibile dal calendario.")
            st.divider()
            render_booking_form()
            return

        st.divider()
        st.subheader(f"Slot disponibili per {format_date_it(selected_day)}")

        start_dt = datetime.combine(selected_day, time(0, 0))
        end_dt = start_dt + timedelta(days=1)
        booked_map = get_booked_slot_map(start_dt, end_dt)

        render_slot_picker_for_days([selected_day], booked_map)

    else:
        st.subheader("Scorri settimana per settimana")

        initial_week = monday_of_week(today)
        min_week = monday_of_week(today)
        max_week = monday_of_week(max_booking_day)

        if "booking_week_start" not in st.session_state:
            st.session_state.booking_week_start = initial_week.isoformat()

        current_week_start = date.fromisoformat(st.session_state.booking_week_start)

        if current_week_start < min_week:
            current_week_start = min_week
        if current_week_start > max_week:
            current_week_start = max_week

        nav1, nav2, nav3 = st.columns([1, 2, 1])

        with nav1:
            prev_disabled = current_week_start <= min_week
            if st.button("← Settimana prima", disabled=prev_disabled):
                current_week_start -= timedelta(days=7)
                st.session_state.booking_week_start = current_week_start.isoformat()
                reset_selected_slot()
                st.rerun()

        with nav2:
            week_end = current_week_start + timedelta(days=6)
            st.markdown(
                f"<h4 style='text-align:center;'>{current_week_start.strftime('%d/%m/%Y')} - {week_end.strftime('%d/%m/%Y')}</h4>",
                unsafe_allow_html=True,
            )

        with nav3:
            next_disabled = current_week_start >= max_week
            if st.button("Settimana dopo →", disabled=next_disabled):
                current_week_start += timedelta(days=7)
                st.session_state.booking_week_start = current_week_start.isoformat()
                reset_selected_slot()
                st.rerun()

        week_days = [current_week_start + timedelta(days=i) for i in range(7)]
        week_days = [d for d in week_days if today <= d <= max_booking_day]

        start_dt = datetime.combine(week_days[0], time(0, 0))
        end_dt = datetime.combine(week_days[-1] + timedelta(days=1), time(0, 0))
        booked_map = get_booked_slot_map(start_dt, end_dt)

        render_slot_picker_for_days(week_days, booked_map)

    st.divider()
    render_booking_form()


def page_admin() -> None:
    st.markdown(
        """
        <div class="admin-hero">
            <div class="ae-kicker">⚡ Control room</div>
            <h1>Admin prenotazioni</h1>
            <p>
                Qui vedi subito cosa devi gestire: call prenotate, slot liberi, saturazione della settimana,
                dettagli dei clienti e azioni rapide per email e calendario.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    admin_password = load_admin_password()

    if "admin_logged_in" not in st.session_state:
        st.session_state.admin_logged_in = False

    if not st.session_state.admin_logged_in:
        login_col, hint_col = st.columns([0.75, 1.25], vertical_alignment="center")

        with login_col:
            password = st.text_input("Password admin", type="password")
            if st.button("Entra", type="primary", use_container_width=True):
                if password == admin_password:
                    st.session_state.admin_logged_in = True
                    st.rerun()
                else:
                    st.error("Password errata.")

        with hint_col:
            st.markdown(
                """
                <div class="ae-card">
                    <h4>Area privata</h4>
                    <p>
                        Da qui puoi vedere agenda, slot ancora liberi, prossime call,
                        dettagli dei contatti e strumenti rapidi per calendario/email.
                    </p>
                </div>
                """,
                unsafe_allow_html=True,
            )
            st.caption("In locale la password di default è admin123. Cambiala prima di mettere online.")
        return

    today = date.today()

    top_actions = st.columns([1, 1, 1, 2])

    with top_actions[0]:
        if st.button("Logout", use_container_width=True):
            st.session_state.admin_logged_in = False
            st.rerun()

    with top_actions[1]:
        if st.button("Aggiorna", use_container_width=True):
            st.rerun()

    with top_actions[2]:
        st.link_button("Instagram", INSTAGRAM_URL, use_container_width=True)

    with top_actions[3]:
        selected_day = st.date_input("Giorno focus", value=today, key="admin_focus_day")

    start_day = selected_day - timedelta(days=selected_day.weekday())
    end_day = start_day + timedelta(days=6)

    active_df = fetch_bookings(
        start_dt=datetime.combine(start_day, time(0, 0)),
        end_dt=datetime.combine(end_day + timedelta(days=1), time(0, 0)),
        only_active=True,
    )

    all_future_df = fetch_bookings(
        start_dt=datetime.combine(today, time(0, 0)),
        end_dt=datetime.combine(today + timedelta(days=90), time(0, 0)),
        only_active=True,
    )

    week_days = [start_day + timedelta(days=i) for i in range(7)]
    booked_map_week = get_booked_slot_map(
        datetime.combine(start_day, time(0, 0)),
        datetime.combine(end_day + timedelta(days=1), time(0, 0)),
    )

    booked_week_slots = len(active_df)
    free_week_slots = 0

    for day in week_days:
        for slot in generate_slots_for_day(day):
            if slot.start.isoformat() not in booked_map_week and not is_past_slot(slot):
                free_week_slots += 1

    selected_day_bookings = fetch_bookings(
        start_dt=datetime.combine(selected_day, time(0, 0)),
        end_dt=datetime.combine(selected_day + timedelta(days=1), time(0, 0)),
        only_active=True,
    )

    next_booking_label = "Nessuna"
    if not all_future_df.empty:
        tmp = all_future_df.copy()
        tmp["slot_start_dt"] = pd.to_datetime(tmp["slot_start"])
        tmp = tmp[tmp["slot_start_dt"] >= pd.Timestamp.now()].sort_values("slot_start_dt")
        if not tmp.empty:
            first = tmp.iloc[0]
            next_dt = pd.to_datetime(first["slot_start"]).strftime("%d/%m %H:%M")
            next_booking_label = f"{next_dt} · {first['name']}"

    st.write("")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Call questa settimana", booked_week_slots)
    m2.metric("Slot liberi settimana", free_week_slots)
    m3.metric("Call nel giorno", len(selected_day_bookings))
    m4.metric("Prossima call", next_booking_label)

    render_email_settings_box()

    st.divider()

    st.subheader("Calendario settimanale operativo")
    st.caption("Nero = prenotato · verde = libero · grigio = passato/non disponibile.")

    if "admin_week_start_pro" not in st.session_state:
        st.session_state.admin_week_start_pro = start_day.isoformat()

    current_week_start = date.fromisoformat(st.session_state.admin_week_start_pro)
    desired_week_start = selected_day - timedelta(days=selected_day.weekday())

    if desired_week_start != current_week_start:
        current_week_start = desired_week_start
        st.session_state.admin_week_start_pro = current_week_start.isoformat()

    nav1, nav2, nav3 = st.columns([1, 2, 1])

    with nav1:
        if st.button("← Settimana prima", use_container_width=True):
            current_week_start -= timedelta(days=7)
            st.session_state.admin_week_start_pro = current_week_start.isoformat()
            st.session_state.admin_focus_day = current_week_start
            st.rerun()

    with nav2:
        week_end = current_week_start + timedelta(days=6)
        st.markdown(
            f"<h3 style='text-align:center; margin-top:.25rem;'>{current_week_start.strftime('%d/%m/%Y')} - {week_end.strftime('%d/%m/%Y')}</h3>",
            unsafe_allow_html=True,
        )

    with nav3:
        if st.button("Settimana dopo →", use_container_width=True):
            current_week_start += timedelta(days=7)
            st.session_state.admin_week_start_pro = current_week_start.isoformat()
            st.session_state.admin_focus_day = current_week_start
            st.rerun()

    render_admin_week_calendar(current_week_start)

    st.divider()

    left, right = st.columns([1.25, 0.75], vertical_alignment="top")

    with left:
        st.subheader(f"Timeline del giorno · {format_date_it(selected_day)}")
        st.caption("La vista più utile: slot per slot, con dati cliente e azioni rapide.")
        render_admin_day_timeline(selected_day)

    with right:
        render_next_bookings_panel(all_future_df, limit=8)

    st.divider()

    st.subheader("Database prenotazioni")

    range_col1, range_col2 = st.columns(2)

    with range_col1:
        range_start = st.date_input("Da", value=today, key="admin_table_start")
    with range_col2:
        range_end = st.date_input("A", value=today + timedelta(days=30), key="admin_table_end")

    table_df = fetch_bookings(
        start_dt=datetime.combine(range_start, time(0, 0)),
        end_dt=datetime.combine(range_end + timedelta(days=1), time(0, 0)),
        only_active=False,
    )

    if table_df.empty:
        st.info("Nessuna prenotazione nel periodo selezionato.")
    else:
        display_df = table_df.copy()
        display_df["slot_start"] = pd.to_datetime(display_df["slot_start"]).dt.strftime("%d/%m/%Y %H:%M")
        display_df["slot_end"] = pd.to_datetime(display_df["slot_end"]).dt.strftime("%H:%M")

        display_df = display_df[
            ["id", "slot_start", "slot_end", "name", "email", "phone", "notes", "status", "created_at"]
        ]

        st.dataframe(display_df, use_container_width=True, hide_index=True)

        csv = table_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Scarica CSV completo",
            data=csv,
            file_name="prenotazioni_aussie_ema.csv",
            mime="text/csv",
            use_container_width=True,
        )

        st.subheader("Gestione rapida")

        active_only = table_df[table_df["status"] == "booked"]

        if active_only.empty:
            st.info("Non ci sono prenotazioni attive da gestire.")
        else:
            options = {
                f"#{int(row['id'])} - {pd.to_datetime(row['slot_start']).strftime('%d/%m/%Y %H:%M')} - {row['name']}": int(row["id"])
                for _, row in active_only.iterrows()
            }

            selected_label = st.selectbox("Seleziona prenotazione", list(options.keys()))
            selected_id = options[selected_label]
            selected_row = active_only[active_only["id"] == selected_id].iloc[0]

            b_start, b_end = booking_to_datetimes(selected_row)
            title, details, location = booking_calendar_details(selected_row)
            gcal_url = google_calendar_url(title, b_start, b_end, details, location)
            ics = build_ics_content(title, b_start, b_end, details, location)

            action_col1, action_col2, action_col3, action_col4 = st.columns(4)

            with action_col1:
                st.link_button("Google Calendar", gcal_url, use_container_width=True)

            with action_col2:
                st.download_button(
                    "Scarica .ics",
                    data=ics,
                    file_name=f"booking_{selected_id}.ics",
                    mime="text/calendar",
                    use_container_width=True,
                    key=f"ics_manage_{selected_id}",
                )

            with action_col3:
                if st.button("Conferma call", use_container_width=True):
                    update_booking_status(selected_id, "confirmed")
                    st.success("Call confermata.")
                    st.rerun()

            with action_col4:
                if st.button("Segna fatta", use_container_width=True):
                    update_booking_status(selected_id, "done")
                    st.success("Call segnata come fatta.")
                    st.rerun()

            extra_col1, extra_col2, extra_col3, extra_col4 = st.columns(4)

            with extra_col1:
                if st.button("Invia reminder", use_container_width=True):
                    ok, msg = send_email(
                        to_email=str(selected_row["email"]),
                        subject="Reminder videochiamata con Aussie Ema",
                        body=build_reminder_email_body(selected_row),
                    )
                    if ok:
                        st.success("Reminder inviato.")
                    else:
                        st.warning(msg)

            with extra_col2:
                if st.button("Richiedi recensione", use_container_width=True):
                    ok, msg = send_email(
                        to_email=str(selected_row["email"]),
                        subject="Ti va di lasciare un feedback sulla call?",
                        body=build_review_request_email_body(selected_row),
                    )
                    if ok:
                        st.success("Richiesta recensione inviata.")
                    else:
                        st.warning(msg)

            with extra_col3:
                if st.button("Cancella call", use_container_width=True):
                    cancel_booking(selected_id)
                    st.success("Prenotazione cancellata. Lo slot è di nuovo libero.")
                    st.rerun()

            with extra_col4:
                confirm_delete = st.checkbox("Confermo eliminazione")
                if st.button("Elimina definitivamente", disabled=not confirm_delete, use_container_width=True):
                    delete_booking_forever(selected_id)
                    st.success("Prenotazione eliminata definitivamente.")
                    st.rerun()

    with st.expander("Impostazioni tecniche"):
        st.write(
            f"""
            **Configurazione attuale**

            - Durata servizio: {SERVICE_DURATION_MINUTES} minuti
            - Pausa tra call: {BREAK_BETWEEN_CALLS_MINUTES} minuti
            - Orario: {WORK_START.strftime('%H:%M')} - {WORK_END.strftime('%H:%M')}
            - Giorni lavorativi: {WORKING_DAYS}
            - Prenotabile fino a: {BOOKING_MONTHS_AHEAD} mesi avanti
            - Database: `{DB_PATH}`
            - SMTP configurato: `{smtp_is_configured()}`
            - Video call link: `{VIDEO_CALL_LINK or "non impostato"}`
            """
        )


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon="🌏",
        layout="wide",
    )

    init_db()
    inject_css()

    try:
        requested_page = st.query_params.get("page", "")
    except Exception:
        requested_page = ""

    if "page" not in st.session_state:
        st.session_state.page = "Landing"

    if requested_page in {"Landing", "Prenota", "Recensioni", "Gestisci prenotazione", "Admin"}:
        st.session_state.page = requested_page

    pages = ["Landing", "Prenota", "Recensioni", "Gestisci prenotazione", "Admin"]
    selected = st.sidebar.radio(
        "Navigazione",
        pages,
        index=pages.index(st.session_state.page),
    )
    st.session_state.page = selected

    st.sidebar.divider()
    st.sidebar.caption("Aussie Ema")
    st.sidebar.caption("Australia, viaggi e tanto altro")
    st.sidebar.link_button("Instagram @_aussie_ema_", INSTAGRAM_URL)

    st.sidebar.divider()

    if selected == "Landing":
        page_landing()
    elif selected == "Prenota":
        page_booking()
    elif selected == "Recensioni":
        page_reviews()
    elif selected == "Gestisci prenotazione":
        page_manage_booking()
    elif selected == "Admin":
        page_admin()


if __name__ == "__main__":
    main()
