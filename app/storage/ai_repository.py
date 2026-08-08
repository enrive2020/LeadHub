"""Репозиторий оценок AI."""

from datetime import UTC, datetime

from app.ai.schemas import LeadGrade, Qualification
from app.storage.database import get_connection


def save(lead_id: str, qualification: Qualification, model: str) -> None:
    """Сохраняет оценку. Повторный вызов перезаписывает предыдущую.

    ON CONFLICT DO UPDATE, а не DO NOTHING, в отличие от лидов и задач.
    Логика разная: заявка клиента — свершившийся факт, её переписывать нельзя.
    Оценка модели — суждение, и если мы переспросили (сменили промт, модель,
    вернули задачу из dead letter), актуальным считается свежее.
    """
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO lead_ai (lead_id, grade, score, reason, reply_draft, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (lead_id) DO UPDATE SET
                grade       = excluded.grade,
                score       = excluded.score,
                reason      = excluded.reason,
                reply_draft = excluded.reply_draft,
                model       = excluded.model,
                created_at  = excluded.created_at
            """,
            (
                lead_id,
                qualification.grade.value,
                qualification.score,
                qualification.reason,
                qualification.reply_draft,
                model,
                datetime.now(UTC).isoformat(),
            ),
        )


def get(lead_id: str) -> Qualification | None:
    """Оценка лида или None, если её ещё нет."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT grade, score, reason, reply_draft FROM lead_ai WHERE lead_id = ?",
            (lead_id,),
        ).fetchone()

    if row is None:
        return None

    return Qualification(
        grade=LeadGrade(row["grade"]),
        score=row["score"],
        reason=row["reason"],
        reply_draft=row["reply_draft"],
    )


def count_by_grade() -> dict[str, int]:
    """Сколько лидов какой оценки — для /healthz."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT grade, COUNT(*) AS total FROM lead_ai GROUP BY grade"
        ).fetchall()
    return {row["grade"]: row["total"] for row in rows}
