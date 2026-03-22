"""
seed_students.py

Run this ONCE to:
  1. Create the StudentRecords DynamoDB table (if it doesn't exist)
  2. Seed it with realistic dummy students across all SC programs

Usage:
    python seed_students.py

The script is idempotent — running it again just overwrites existing records.
"""

import boto3
import time
import random
from decimal import Decimal
from botocore.exceptions import ClientError
from dotenv import load_dotenv
import os

load_dotenv()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
client   = boto3.client("dynamodb",  region_name=AWS_REGION)

TABLE_NAME = "StudentRecords"

# ── SC programs (reflects real Samar College offerings) ─────
PROGRAMS = {
    "BSIT":  "Bachelor of Science in Information Technology",
    "BSCS":  "Bachelor of Science in Computer Science",
    "BSEd":  "Bachelor of Secondary Education",
    "BEEd":  "Bachelor of Elementary Education",
    "BSCrim":"Bachelor of Science in Criminology",
    "BSBA":  "Bachelor of Science in Business Administration",
    "BSN":   "Bachelor of Science in Nursing",
}

INSTRUCTORS = [
    "Mr. Reyes, J.", "Ms. Santos, A.", "Dr. Lim, R.", "Prof. Cruz, M.",
    "Mr. Flores, B.", "Ms. Garcia, N.", "Dr. Tan, P.", "Prof. Bautista, L.",
    "Mr. Mendoza, C.", "Ms. Villanueva, S.", "Dr. Aquino, F.", "Prof. Dela Cruz, E.",
]

# ── Subject catalogue per program (code, name, units) ────────
SUBJECTS_BY_PROGRAM = {
    "BSIT": [
        ("IT101", "Introduction to Computing",          3),
        ("IT102", "Computer Programming 1",             3),
        ("IT201", "Data Structures and Algorithms",     3),
        ("IT202", "Object-Oriented Programming",        3),
        ("IT203", "Database Management Systems",        3),
        ("IT301", "Web Development",                    3),
        ("IT302", "Systems Analysis and Design",        3),
        ("IT303", "Computer Networks",                  3),
        ("IT401", "Software Engineering",               3),
        ("IT402", "Capstone Project 1",                 3),
        ("GE001", "Understanding the Self",             3),
        ("GE002", "Readings in Philippine History",     3),
        ("GE003", "Mathematics in the Modern World",    3),
        ("GE004", "Purposive Communication",            3),
        ("PE001", "Physical Education 1",               2),
        ("NSTP1", "National Service Training Program 1",3),
    ],
    "BSCS": [
        ("CS101", "Discrete Mathematics",               3),
        ("CS102", "Computer Programming 1",             3),
        ("CS201", "Data Structures",                    3),
        ("CS202", "Algorithm Design",                   3),
        ("CS203", "Computer Organization",              3),
        ("CS301", "Operating Systems",                  3),
        ("CS302", "Theory of Computation",              3),
        ("CS303", "Software Engineering",               3),
        ("CS401", "Artificial Intelligence",            3),
        ("CS402", "Machine Learning",                   3),
        ("GE001", "Understanding the Self",             3),
        ("GE003", "Mathematics in the Modern World",    3),
        ("GE004", "Purposive Communication",            3),
        ("PE001", "Physical Education 1",               2),
    ],
    "BSEd": [
        ("ED101", "Child and Adolescent Development",   3),
        ("ED102", "The Teaching Profession",            3),
        ("ED201", "Facilitating Learning",              3),
        ("ED202", "Curriculum Development",             3),
        ("ED203", "Assessment of Student Learning 1",  3),
        ("ED301", "Field Study 1",                      3),
        ("ED302", "Practice Teaching",                  6),
        ("MATH1", "College Algebra",                    3),
        ("SCI01", "Earth Science",                      3),
        ("GE001", "Understanding the Self",             3),
        ("GE004", "Purposive Communication",            3),
        ("PE001", "Physical Education 1",               2),
    ],
    "BEEd": [
        ("EE101", "Child Development",                  3),
        ("EE102", "Foundations of Special Education",  3),
        ("EE201", "Teaching Multiliteracies",           3),
        ("EE202", "Assessment in Elementary School",   3),
        ("EE203", "Principles of Teaching 1",          3),
        ("EE301", "Field Study 1",                      3),
        ("EE302", "Practice Teaching",                  6),
        ("GE001", "Understanding the Self",             3),
        ("GE004", "Purposive Communication",            3),
        ("PE001", "Physical Education 1",               2),
    ],
    "BSCrim": [
        ("CR101", "Introduction to Criminology",       3),
        ("CR102", "Criminal Law 1",                    3),
        ("CR201", "Criminal Law 2",                    3),
        ("CR202", "Law Enforcement Administration",    3),
        ("CR203", "Criminalistics 1",                  3),
        ("CR301", "Juvenile Delinquency",              3),
        ("CR302", "Criminal Procedure",                3),
        ("CR401", "Practicum",                         6),
        ("GE001", "Understanding the Self",            3),
        ("GE004", "Purposive Communication",           3),
        ("PE001", "Physical Education 1",              2),
    ],
    "BSBA": [
        ("BA101", "Principles of Management",          3),
        ("BA102", "Accounting 1",                      3),
        ("BA201", "Marketing Management",              3),
        ("BA202", "Business Statistics",               3),
        ("BA203", "Financial Management",              3),
        ("BA301", "Human Resource Management",         3),
        ("BA302", "Operations Management",             3),
        ("BA401", "Entrepreneurship",                  3),
        ("GE001", "Understanding the Self",            3),
        ("GE003", "Mathematics in the Modern World",   3),
        ("GE004", "Purposive Communication",           3),
        ("PE001", "Physical Education 1",              2),
    ],
    "BSN": [
        ("NUR101", "Anatomy and Physiology",           3),
        ("NUR102", "Fundamentals of Nursing",          5),
        ("NUR201", "Medical-Surgical Nursing 1",       5),
        ("NUR202", "Maternal and Child Nursing",       5),
        ("NUR203", "Pharmacology",                     3),
        ("NUR301", "Psychiatric Nursing",              5),
        ("NUR302", "Community Health Nursing",         5),
        ("NUR401", "Nursing Research",                 3),
        ("GE001", "Understanding the Self",            3),
        ("GE004", "Purposive Communication",           3),
        ("PE001", "Physical Education 1",              2),
    ],
}

DAYS = ["MWF", "TTh", "MWF", "TTh", "MWF"]
TIMES = [
    "7:00-8:00 AM", "8:00-9:00 AM", "9:00-10:00 AM",
    "10:00-11:00 AM", "11:00-12:00 PM", "1:00-2:00 PM",
    "2:00-3:00 PM", "3:00-4:00 PM", "4:00-5:00 PM",
]

# ── Dummy student data ────────────────────────────────────────
DUMMY_STUDENTS = [
    # (student_id, student_number, full_name, email, program_code, year, section)
    # BSIT
    ("uid-bsit-001", "2021-00101", "Juan Dela Cruz",        "juan.delacruz@spc.edu.ph",   "BSIT",   3, "BSIT-3A"),
    ("uid-bsit-002", "2022-00202", "Maria Santos",          "maria.santos@spc.edu.ph",    "BSIT",   2, "BSIT-2B"),
    ("uid-bsit-003", "2020-00303", "Carlo Reyes",           "carlo.reyes@spc.edu.ph",     "BSIT",   4, "BSIT-4A"),
    ("uid-bsit-004", "2023-00404", "Ana Lim",               "ana.lim@spc.edu.ph",         "BSIT",   1, "BSIT-1A"),
    ("uid-bsit-005", "2022-00505", "Jose Garcia",           "jose.garcia@spc.edu.ph",     "BSIT",   2, "BSIT-2A"),
    # BSCS
    ("uid-bscs-001", "2021-00601", "Rizalina Cruz",         "rizalina.cruz@spc.edu.ph",   "BSCS",   3, "BSCS-3A"),
    ("uid-bscs-002", "2022-00702", "Miguel Flores",         "miguel.flores@spc.edu.ph",   "BSCS",   2, "BSCS-2A"),
    # BSEd
    ("uid-bsed-001", "2021-00801", "Luisa Villanueva",      "luisa.villanueva@spc.edu.ph","BSEd",   3, "BSEd-3A"),
    ("uid-bsed-002", "2022-00902", "Ramon Aquino",          "ramon.aquino@spc.edu.ph",    "BSEd",   2, "BSEd-2A"),
    # BEEd
    ("uid-beed-001", "2021-01001", "Pia Mendoza",           "pia.mendoza@spc.edu.ph",     "BEEd",   3, "BEEd-3A"),
    ("uid-beed-002", "2023-01102", "Felix Bautista",        "felix.bautista@spc.edu.ph",  "BEEd",   1, "BEEd-1A"),
    # BSCrim
    ("uid-crim-001", "2020-01201", "Andres Tan",            "andres.tan@spc.edu.ph",      "BSCrim", 4, "BSCrim-4A"),
    ("uid-crim-002", "2022-01302", "Celia Ramos",           "celia.ramos@spc.edu.ph",     "BSCrim", 2, "BSCrim-2A"),
    # BSBA
    ("uid-bsba-001", "2021-01401", "Nathaniel Ocampo",      "nathaniel.ocampo@spc.edu.ph","BSBA",   3, "BSBA-3A"),
    ("uid-bsba-002", "2023-01502", "Sophia Torres",         "sophia.torres@spc.edu.ph",   "BSBA",   1, "BSBA-1A"),
    # BSN
    ("uid-bsn-001",  "2021-01601", "Angeline Castillo",     "angeline.castillo@spc.edu.ph","BSN",   3, "BSN-3A"),
    ("uid-bsn-002",  "2022-01702", "Eduardo Navarro",       "eduardo.navarro@spc.edu.ph", "BSN",    2, "BSN-2A"),
]


def make_grade_entry(code: str, name: str, units: int, performance: str) -> dict:
    """
    Generate realistic grade entries.
    performance: "excellent" | "good" | "average" | "poor"
    """
    ranges = {
        "excellent": (92, 99),
        "good":      (82, 91),
        "average":   (75, 81),
        "poor":      (65, 74),
    }
    lo, hi = ranges.get(performance, (75, 85))

    prelim     = random.randint(lo, hi)
    midterm    = random.randint(lo, hi)
    semi_final = random.randint(lo, hi)
    final_exam = random.randint(lo, hi)

    # weighted average: prelim 20%, midterm 30%, semi 20%, final 30%
    avg = (prelim * 0.2) + (midterm * 0.3) + (semi_final * 0.2) + (final_exam * 0.3)
    avg = round(avg, 1)

    # Convert to 5-point scale (Samar College system)
    if avg >= 97: grade = "1.0"
    elif avg >= 94: grade = "1.25"
    elif avg >= 91: grade = "1.5"
    elif avg >= 88: grade = "1.75"
    elif avg >= 85: grade = "2.0"
    elif avg >= 82: grade = "2.25"
    elif avg >= 79: grade = "2.5"
    elif avg >= 76: grade = "2.75"
    elif avg >= 75: grade = "3.0"
    else:           grade = "5.0"

    return {
        "subject_code": code,
        "subject_name": name,
        "units":        str(units),
        "prelim":       str(prelim),
        "midterm":      str(midterm),
        "semi_final":   str(semi_final),
        "final":        str(final_exam),
        "final_grade":  grade,
        "remarks":      "Passed" if float(grade) < 4.0 else "Failed",
    }


def build_student_record(
    student_id, student_number, full_name, email,
    program_code, year_level, section
) -> dict:
    program_name = PROGRAMS[program_code]
    all_subjects = SUBJECTS_BY_PROGRAM[program_code]

    # Pick 6-8 subjects for the semester
    semester_subjects = random.sample(all_subjects, min(7, len(all_subjects)))

    # Vary performance profile per student
    profiles = ["excellent", "good", "good", "average", "average", "poor"]
    perf_weights = {
        "excellent": [0.6, 0.3, 0.1, 0.0],
        "good":      [0.2, 0.5, 0.2, 0.1],
        "average":   [0.1, 0.3, 0.5, 0.1],
        "poor":      [0.0, 0.1, 0.4, 0.5],
    }
    student_profile = random.choice(profiles)
    weights = perf_weights[student_profile]
    perf_levels = ["excellent", "good", "average", "poor"]

    enrolled_subjects = []
    grades = []
    used_times = set()

    for code, name, units in semester_subjects:
        # Pick unique schedule slot
        for _ in range(20):
            day  = random.choice(DAYS)
            time = random.choice(TIMES)
            slot = f"{day} {time}"
            if slot not in used_times:
                used_times.add(slot)
                break

        instructor = random.choice(INSTRUCTORS)
        room = f"Room {random.randint(100, 315)}"

        enrolled_subjects.append({
            "subject_code": code,
            "subject_name": name,
            "units":        str(units),
            "schedule":     f"{day} {time}",
            "room":         room,
            "instructor":   instructor,
        })

        # Generate grade
        perf = random.choices(perf_levels, weights=weights)[0]
        grades.append(make_grade_entry(code, name, units, perf))

    # Compute GPA
    total_units  = sum(float(g["units"]) for g in grades if g["remarks"] == "Passed")
    weighted_sum = sum(
        float(g["final_grade"]) * float(g["units"])
        for g in grades if g["remarks"] == "Passed"
    )
    gpa = round(weighted_sum / total_units, 2) if total_units > 0 else 0.0

    if gpa <= 1.25:   standing = "Dean's List"
    elif gpa <= 2.0:  standing = "Good Standing"
    elif gpa <= 3.0:  standing = "Satisfactory"
    else:             standing = "Probationary"

    return {
        "student_id":        student_id,
        "student_number":    student_number,
        "full_name":         full_name,
        "email":             email,
        "program":           program_name,
        "program_code":      program_code,
        "year_level":        str(year_level),
        "section":           section,
        "semester":          "1st",
        "school_year":       "2024-2025",
        "enrolled_subjects": enrolled_subjects,
        "grades":            grades,
        "gpa":               str(gpa),
        "remarks":           standing,
    }


# ── Table creation ────────────────────────────────────────────

def create_table_if_not_exists():
    existing = [t.name for t in dynamodb.tables.all()]
    if TABLE_NAME in existing:
        print(f"Table '{TABLE_NAME}' already exists — skipping creation.")
        return dynamodb.Table(TABLE_NAME)

    print(f"Creating table '{TABLE_NAME}' ...")
    table = dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "student_id", "KeyType": "HASH"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "student_id",  "AttributeType": "S"},
            {"AttributeName": "email",        "AttributeType": "S"},
            {"AttributeName": "student_number","AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "email-index",
                "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "student-number-index",
                "KeySchema": [{"AttributeName": "student_number", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    # Wait until active
    print("Waiting for table to become ACTIVE ...")
    table.meta.client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    print(f"Table '{TABLE_NAME}' is ACTIVE.")
    return table


def seed_students(table):
    random.seed(42)  # reproducible data
    print(f"\nSeeding {len(DUMMY_STUDENTS)} student records ...")
    for args in DUMMY_STUDENTS:
        record = build_student_record(*args)
        table.put_item(Item=record)
        print(f"  ✓ {record['full_name']:<30} {record['program_code']:<8} "
              f"Year {record['year_level']}  GPA {record['gpa']}  ({record['remarks']})")
    print("\nSeeding complete.")


if __name__ == "__main__":
    table = create_table_if_not_exists()
    seed_students(table)
    print("\nAll done! StudentRecords table is ready.")