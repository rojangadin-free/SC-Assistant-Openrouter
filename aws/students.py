"""
aws/students.py

DynamoDB operations for the StudentRecords table.

Table schema:
  Partition key : student_id  (String)  — matches Cognito sub (uid)
  Sort key      : (none)

Attributes stored per student:
  student_id        – Cognito sub / uid
  student_number    – e.g. "2021-00123"
  full_name
  email
  program           – e.g. "Bachelor of Science in Information Technology"
  year_level        – 1 | 2 | 3 | 4
  semester          – "1st" | "2nd"
  school_year       – e.g. "2024-2025"
  section           – e.g. "BSIT-2A"
  enrolled_subjects – list of subject dicts (see below)
  grades            – list of grade dicts (see below)
  gpa               – float, computed from grades
  remarks           – "Good Standing" | "Probationary" | "Dean's List"

Subject dict:
  { subject_code, subject_name, units, schedule, instructor }

Grade dict:
  { subject_code, subject_name, units,
    prelim, midterm, semi_final, final, final_grade, remarks }
    remarks: "Passed" | "Failed" | "Incomplete"
"""

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from config import AWS_REGION

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
students_table = dynamodb.Table("StudentRecords")


# ── CRUD ────────────────────────────────────────────────────

def get_student_by_uid(uid: str) -> dict | None:
    """Fetch a student record by Cognito uid (partition key)."""
    try:
        resp = students_table.get_item(Key={"student_id": uid})
        return resp.get("Item")
    except ClientError as e:
        print(f"[students] get_student_by_uid error: {e}")
        return None


def get_student_by_email(email: str) -> dict | None:
    """
    Fetch by email using a GSI named 'email-index'.
    Falls back to a full scan if the GSI doesn't exist yet.
    """
    try:
        resp = students_table.query(
            IndexName="email-index",
            KeyConditionExpression=Key("email").eq(email),
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except ClientError:
        # Fallback: scan (slow, only for dev/demo)
        try:
            resp = students_table.scan(
                FilterExpression=Key("email").eq(email)
            )
            items = resp.get("Items", [])
            return items[0] if items else None
        except ClientError as e2:
            print(f"[students] get_student_by_email scan error: {e2}")
            return None


def upsert_student(record: dict) -> bool:
    """Create or fully replace a student record."""
    try:
        students_table.put_item(Item=record)
        return True
    except ClientError as e:
        print(f"[students] upsert_student error: {e}")
        return False


def update_student_grades(student_id: str, grades: list) -> bool:
    """Replace the grades list and recompute GPA."""
    try:
        gpa = _compute_gpa(grades)
        remarks = _compute_remarks(gpa)
        students_table.update_item(
            Key={"student_id": student_id},
            UpdateExpression="SET grades = :g, gpa = :gpa, remarks = :r",
            ExpressionAttributeValues={
                ":g": grades,
                ":gpa": str(round(gpa, 2)),
                ":r": remarks,
            },
        )
        return True
    except ClientError as e:
        print(f"[students] update_student_grades error: {e}")
        return False


def list_all_students() -> list:
    """Return all student records (for admin use only)."""
    try:
        resp = students_table.scan()
        return resp.get("Items", [])
    except ClientError as e:
        print(f"[students] list_all_students error: {e}")
        return []


# ── HELPERS ─────────────────────────────────────────────────

def _compute_gpa(grades: list) -> float:
    """
    Weighted GPA using Samar College's standard grading:
    1.0 = 99-100  …  5.0 = below 70  (INC = excluded)
    """
    total_units = 0
    weighted_sum = 0.0
    for g in grades:
        if g.get("remarks") in ("Incomplete", "Failed"):
            continue
        try:
            units = float(g.get("units", 0))
            fg = float(g.get("final_grade", 0))
            weighted_sum += fg * units
            total_units += units
        except (ValueError, TypeError):
            continue
    return round(weighted_sum / total_units, 2) if total_units > 0 else 0.0


def _compute_remarks(gpa: float) -> str:
    if gpa == 0.0:
        return "No Grades"
    if gpa <= 1.25:
        return "Dean's List"
    if gpa <= 2.0:
        return "Good Standing"
    if gpa <= 3.0:
        return "Satisfactory"
    return "Probationary"


def format_student_context(student: dict) -> str:
    """
    Convert a student record into a plain-text block that the RAG
    chain can include in the system prompt as personal context.
    """
    if not student:
        return ""

    lines = [
        "=== STUDENT PERSONAL ACADEMIC RECORD ===",
        f"Name          : {student.get('full_name', 'N/A')}",
        f"Student No.   : {student.get('student_number', 'N/A')}",
        f"Program       : {student.get('program', 'N/A')}",
        f"Year / Section: {student.get('year_level', 'N/A')} - {student.get('section', 'N/A')}",
        f"Semester      : {student.get('semester', 'N/A')} Sem, S.Y. {student.get('school_year', 'N/A')}",
        f"GPA           : {student.get('gpa', 'N/A')}",
        f"Standing      : {student.get('remarks', 'N/A')}",
        "",
    ]

    # Enrolled subjects
    subjects = student.get("enrolled_subjects", [])
    if subjects:
        lines.append("ENROLLED SUBJECTS:")
        lines.append(f"{'Code':<12} {'Subject':<45} {'Units':<6} {'Schedule':<25} Instructor")
        lines.append("-" * 110)
        for s in subjects:
            lines.append(
                f"{s.get('subject_code',''):<12} "
                f"{s.get('subject_name',''):<45} "
                f"{s.get('units',''):<6} "
                f"{s.get('schedule',''):<25} "
                f"{s.get('instructor','')}"
            )
        lines.append("")

    # Grades
    grade_list = student.get("grades", [])
    if grade_list:
        lines.append("GRADES THIS SEMESTER:")
        lines.append(f"{'Code':<12} {'Subject':<40} {'Units':<6} {'Prelim':<8} {'Midterm':<9} {'SemiFinal':<11} {'Final':<7} {'Grade':<7} Remarks")
        lines.append("-" * 120)
        for g in grade_list:
            lines.append(
                f"{g.get('subject_code',''):<12} "
                f"{g.get('subject_name',''):<40} "
                f"{g.get('units',''):<6} "
                f"{str(g.get('prelim','')):<8} "
                f"{str(g.get('midterm','')):<9} "
                f"{str(g.get('semi_final','')):<11} "
                f"{str(g.get('final','')):<7} "
                f"{str(g.get('final_grade','')):<7} "
                f"{g.get('remarks','')}"
            )
        lines.append("")

    lines.append("=== END OF STUDENT RECORD ===")
    return "\n".join(lines)