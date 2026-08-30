import boto3
import random
from decimal import Decimal
from dotenv import load_dotenv
import os

load_dotenv()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
client   = boto3.client("dynamodb",  region_name=AWS_REGION)
TABLE_NAME = "StudentRecords"

MINIMUM_PAYMENT = 1500

# ─── Exact subject data per semester ─────────────────────────────────────────

SUBJECTS_1ST_YEAR_1ST_SEM = [
    {"schedule_code":"10091","subject_code":"PE - 1","description":"PATHFiT 1","units":2,"time":"06:00 PM - 07:00 PM","day":"Fri/Sa","room":"NB ROOM 1"},
    {"schedule_code":"10003","subject_code":"English 01","description":"Basic English","units":3,"time":"03:00 PM - 04:00 PM","day":"MW","room":"302"},
    {"schedule_code":"10011","subject_code":"GE 1","description":"Understanding Self","units":3,"time":"10:00 AM - 11:00 AM","day":"MW","room":"303"},
    {"schedule_code":"10031","subject_code":"IT_102","description":"Discrete Mathematics","units":3,"time":"01:00 PM - 03:00 PM","day":"MW","room":"ANX 5"},
    {"schedule_code":"10087","subject_code":"Math 01","description":"Basic Mathematics","units":3,"time":"09:00 AM - 10:00 AM","day":"MW","room":"301"},
    {"schedule_code":"n0001","subject_code":"NSTP 1","description":"National Service Training Program","units":3,"time":"02:00 PM - 05:00 PM","day":"Sun","room":"NSTP Rm"},
    {"schedule_code":"10015","subject_code":"GE 2","description":"Reading in Phil History","units":3,"time":"07:00 AM - 08:00 AM","day":"TTH","room":"304"},
    {"schedule_code":"10025","subject_code":"IT-101","description":"Intro to Computing","units":3,"time":"01:00 PM - 03:00 PM","day":"TTH","room":"ANX 5"},
    {"schedule_code":"10037","subject_code":"IT 103","description":"Computer Programming 1","units":3,"time":"03:00 PM - 05:00 PM","day":"TTHS","room":"ANNEX 4"},
    {"schedule_code":"10007","subject_code":"GE Elec1 Fil 1","description":"Komunikasyon sa Akademikong Filipino","units":3,"time":"08:00 AM - 09:00 AM","day":"TTHS","room":"NB Room 1"},
]

SUBJECTS_1ST_YEAR_2ND_SEM = [
    {"schedule_code":"10020","subject_code":"IT 104","description":"Computer Programming II","units":3,"time":"03:00 PM - 05:00 PM","day":"Fri/Sa","room":"ANX 5"},
    {"schedule_code":"10002","subject_code":"English 02","description":"English Fundamentals","units":3,"time":"10:00 AM - 11:00 AM","day":"MW","room":"302"},
    {"schedule_code":"10005","subject_code":"GE Elec Fil 2","description":"Pagbasa at Pagsulat Tungo sa Pananaliksik","units":3,"time":"09:00 AM - 10:00 AM","day":"MW","room":"NB Rm. 1"},
    {"schedule_code":"10025","subject_code":"IT 105","description":"Introduction to Human Computer Interaction","units":3,"time":"11:00 AM - 01:00 PM","day":"MW","room":"ANX 5"},
    {"schedule_code":"10054","subject_code":"Math 02","description":"College Algebra","units":3,"time":"01:00 PM - 02:00 PM","day":"MW","room":"301"},
    {"schedule_code":"N0002","subject_code":"NSTP 2","description":"National Service Training Program","units":2,"time":"02:00 PM - 05:00 PM","day":"Sun","room":"NSTP Rm"},
    {"schedule_code":"10008","subject_code":"GE 3","description":"The Contemporary World","units":3,"time":"09:00 AM - 10:00 AM","day":"TTH","room":"302"},
    {"schedule_code":"10011","subject_code":"GE 4","description":"Mathematics in the Modern World","units":3,"time":"08:00 AM - 09:00 AM","day":"TTH","room":"301"},
    {"schedule_code":"10028","subject_code":"IT 106","description":"Platform Technologies","units":3,"time":"01:00 PM - 03:00 PM","day":"TTH","room":"ANNEX 4"},
    {"schedule_code":"10057","subject_code":"PE -2","description":"Physical Activities Toward Health & Fitness 2","units":2,"time":"11:00 AM - 12:00 PM","day":"TTH","room":"NB Room 1"},
]

SUBJECTS_2ND_YEAR_1ST_SEM = [
    {"schedule_code":"10053","subject_code":"IT 202","description":"Object-Oriented Programming","units":3,"time":"03:00 PM - 05:00 PM","day":"F/S","room":"ANX 4"},
    {"schedule_code":"10048","subject_code":"IT 201","description":"Data Structure & Algorithms","units":3,"time":"05:00 AM - 07:00 AM","day":"Fri/Sa","room":"ANX 5"},
    {"schedule_code":"10063","subject_code":"IT 204","description":"Human Computer Interaction II","units":3,"time":"03:00 PM - 05:00 PM","day":"MW","room":"ANX 5"},
    {"schedule_code":"10101","subject_code":"PE - 3","description":"Physical Activities Toward Health & Fitness 3","units":2,"time":"07:00 PM - 08:00 PM","day":"MW","room":"NB ROOM 1"},
    {"schedule_code":"10104","subject_code":"Research201","description":"Methods of Research in IT","units":3,"time":"06:00 PM - 07:00 PM","day":"MW","room":"301"},
    {"schedule_code":"10018","subject_code":"CAS GE 5","description":"Purposive Communication","units":3,"time":"11:00 AM - 12:00 PM","day":"TTH","room":"302"},
    {"schedule_code":"10021","subject_code":"CAS GE 6","description":"Art Appreciation","units":3,"time":"12:00 PM - 01:00 PM","day":"TTH","room":"302"},
    {"schedule_code":"10058","subject_code":"IT 203","description":"Computer Programming III","units":3,"time":"05:00 PM - 07:00 PM","day":"TTH","room":"ANX 4"},
]

SUBJECTS_2ND_YEAR_2ND_SEM = [
    {"schedule_code":"10038","subject_code":"I.T. 207","description":"Integrative Programming & Technologies I","units":3,"time":"05:00 PM - 07:00 PM","day":"F/S","room":"ANNEX 4"},
    {"schedule_code":"10073","subject_code":"PE-4","description":"(PATHFit 4): Sports, Outdoor and Adventure Activities","units":2,"time":"01:00 PM - 02:00 PM","day":"F/S","room":"304"},
    {"schedule_code":"10015","subject_code":"CAS GE 8","description":"Ethics (with topic on Obligation to Pay Taxes)","units":3,"time":"10:00 AM - 11:00 AM","day":"MW","room":"305"},
    {"schedule_code":"10016","subject_code":"CAS GE 9","description":"Life & Works of Rizal","units":3,"time":"07:00 AM - 08:00 AM","day":"MW","room":"305"},
    {"schedule_code":"10033","subject_code":"I.T. 206","description":"Networking I","units":3,"time":"05:00 PM - 07:00 PM","day":"MW","room":"SCTI LAB 1"},
    {"schedule_code":"10031","subject_code":"IT 205","description":"Fundamentals of Database System","units":3,"time":"08:00 AM - 10:00 AM","day":"MW","room":"ANNEX 4"},
    {"schedule_code":"10012","subject_code":"CAS GE 7","description":"Science, Technology and Society","units":3,"time":"04:00 PM - 05:00 PM","day":"TTH","room":"304"},
    {"schedule_code":"10041","subject_code":"I.T. 208","description":"Web System and Technologies I","units":3,"time":"05:00 PM - 07:00 PM","day":"TTH","room":"ANNEX 4"},
]

SUBJECTS_3RD_YEAR_1ST_SEM = [
    {"schedule_code":"10026","subject_code":"CAS GE 11","description":"Gender and Society","units":3,"time":"06:00 PM - 07:00 PM","day":"F/S","room":"NB Rm. 1"},
    {"schedule_code":"10123","subject_code":"I.T.302","description":"Advanced Database System","units":3,"time":"07:00 AM - 09:00 AM","day":"F/S","room":"ANX 4"},
    {"schedule_code":"10121","subject_code":"IT 305","description":"Integrative Programming & Technologies II","units":3,"time":"12:00 PM - 01:00 PM","day":"MS","room":"ANX 4"},
    {"schedule_code":"10022","subject_code":"CAS GE 10","description":"Entrepreneurial Mind","units":3,"time":"05:00 PM - 06:00 PM","day":"MW","room":"305"},
    {"schedule_code":"10076","subject_code":"IT 304","description":"System Integration & Architecture I","units":3,"time":"03:00 PM - 05:00 PM","day":"Sun","room":"SCTI LAB 1"},
    {"schedule_code":"10122","subject_code":"IT 306","description":"Web System & Technologies II","units":3,"time":"07:00 PM - 09:00 PM","day":"TTH","room":"ANX 4"},
    {"schedule_code":"10028","subject_code":"CAS GE 12","description":"Philippine Indigenous Communities","units":3,"time":"06:00 PM - 07:00 PM","day":"TTH","room":"NB ROOM 1"},
    {"schedule_code":"10070","subject_code":"I.T.303","description":"Mobile Application Development","units":3,"time":"03:00 PM - 05:00 PM","day":"TTH","room":"SCTI LAB 2"},
]

# 3rd year 2nd sem — current (school year 2025-2026, grades NOT yet submitted)
SUBJECTS_3RD_YEAR_2ND_SEM = [
    {"schedule_code":"10055","subject_code":"IT 307","description":"System Integration & Architecture II","units":3,"time":"07:30 AM - 10:00 AM","day":"F/S","room":"ANX 5"},
    {"schedule_code":"10074","subject_code":"IT 313","description":"Basic Linux Installation and Configuration","units":3,"time":"01:00 PM - 03:30 PM","day":"F/S","room":"ANX 5"},
    {"schedule_code":"10077","subject_code":"IT 314","description":"Capstone Project and Research 1","units":3,"time":"03:30 PM - 06:00 PM","day":"F/S","room":"ANX 4"},
    {"schedule_code":"10064","subject_code":"IT 310","description":"Application Development & Emerging Technologies","units":3,"time":"10:00 AM - 12:30 PM","day":"MW","room":"SCTI LAB 2"},
    {"schedule_code":"10071","subject_code":"IT 312","description":"Technopreneurship","units":3,"time":"05:00 PM - 06:00 PM","day":"MW","room":"305"},
    {"schedule_code":"10068","subject_code":"IT-311","description":"Information Management","units":3,"time":"06:00 PM - 08:30 PM","day":"MW","room":"ANX 5"},
    {"schedule_code":"10059","subject_code":"IT 308","description":"Event-Driven Programming","units":3,"time":"06:00 PM - 08:30 PM","day":"TTH","room":"ANX 4"},
    {"schedule_code":"10062","subject_code":"IT 309","description":"Information Assurance and Security I","units":3,"time":"01:00 PM - 03:30 PM","day":"TTH","room":"SCTI LAB 2"},
    {"schedule_code":"10023","subject_code":"Math 02","description":"College Algebra","units":3,"time":"08:00 AM - 09:00 AM","day":"TTH","room":"302"},
]

# ─── Exact tuition fee data ───────────────────────────────────────────────────

TUITION_1ST_YEAR_1ST_SEM = {
    "tuition": 13613.33,
    "other_fees": 6469.86,
    "total": 20083.19,
    "breakdown": {
        "Athletic": 210.00, "AV Fee": 306.22, "College": 13613.33,
        "Computer Lab": 2400.00, "Guidance": 360.00, "Handbook": 204.96,
        "ID": 250.00, "Inst": 360.00, "Insurance": 30.00, "IT and RF": 494.34,
        "Lib": 450.00, "MedDent": 360.00, "Misc": 320.53, "PRISAA": 100.00,
        "Publication": 266.88, "Reg": 306.93, "SSC": 50.00,
    }
}

TUITION_1ST_YEAR_2ND_SEM = {
    "tuition": 13613.33,
    "other_fees": 6084.90,
    "total": 19698.23,
    "breakdown": {
        "Athletic": 210.00, "AV Fee": 306.22, "College": 13613.33,
        "Computer Lab": 2400.00, "Guidance": 360.00, "Inst": 360.00,
        "IT and RF": 494.34, "Lib": 450.00, "MedDent": 360.00, "Misc": 320.53,
        "PRISAA": 200.00, "Publication": 266.88, "Reg": 306.93, "SSC": 50.00,
    }
}

TUITION_2ND_YEAR_1ST_SEM = {
    "tuition": 11898.13,
    "other_fees": 6550.29,
    "total": 18448.42,
    "breakdown": {
        "Athletic": 240.00, "AV Fee": 306.22, "College": 11898.13,
        "Computer Lab": 2400.00, "Guidance": 380.00, "ID": 259.40,
        "Inst": 360.00, "Insurance": 30.00, "IT and RF": 499.33, "Lib": 450.00,
        "MedDent": 400.00, "Misc": 350.53, "PRISAA": 200.00, "Publication": 296.88,
        "Reg": 327.93, "SSC": 50.00,
    }
}

TUITION_2ND_YEAR_2ND_SEM = {
    "tuition": 11898.13,
    "other_fees": 6260.89,
    "total": 18159.02,
    "breakdown": {
        "Athletic": 240.00, "AV Fee": 306.22, "College": 11898.13,
        "Computer Lab": 2400.00, "Guidance Fee": 380.00, "Inst": 360.00,
        "IT and RF": 499.33, "Lib": 450.00, "MedDent": 400.00, "Misc": 350.53,
        "PRISAA": 200.00, "Publication": 296.88, "Reg": 327.93, "SSC": 50.00,
    }
}

TUITION_3RD_YEAR_1ST_SEM = {
    "tuition": 12415.44,
    "other_fees": 8150.09,
    "total": 20565.53,
    "breakdown": {
        "Athletic": 240.00, "AV Fee": 306.22, "College Tuition": 12415.44,
        "Computer Lab": 4000.00, "Guidance Fee": 380.00, "ID": 259.40,
        "Inst": 360.00, "Insurance": 30.00, "IT and RF": 499.33, "Lib": 450.00,
        "MedDent": 400.00, "Misc": 350.33, "PRISAA": 200.00, "Publication": 296.88,
        "Reg": 327.93, "SSC": 50.00,
    }
}

TUITION_3RD_YEAR_2ND_SEM = {
    "tuition": 13967.37,
    "other_fees": 9460.89,
    "total": 23428.26,
    "breakdown": {
        "Athletic": 240.00, "AV Fee": 306.22, "College": 13967.37,
        "Computer Lab": 5600.00, "Guidance": 380.00, "Inst": 360.00,
        "IT and RF": 499.33, "Lib": 450.00, "MedDent": 400.00, "Misc": 350.53,
        "PRISAA": 200.00, "Publication": 296.88, "Reg": 327.93, "SSC": 50.00,
    }
}

# Map semester index to tuition data
SEMESTER_TUITION = {
    (1, "1st"): TUITION_1ST_YEAR_1ST_SEM,
    (1, "2nd"): TUITION_1ST_YEAR_2ND_SEM,
    (2, "1st"): TUITION_2ND_YEAR_1ST_SEM,
    (2, "2nd"): TUITION_2ND_YEAR_2ND_SEM,
    (3, "1st"): TUITION_3RD_YEAR_1ST_SEM,
    (3, "2nd"): TUITION_3RD_YEAR_2ND_SEM,
}

SEMESTER_SUBJECTS = {
    (1, "1st"): SUBJECTS_1ST_YEAR_1ST_SEM,
    (1, "2nd"): SUBJECTS_1ST_YEAR_2ND_SEM,
    (2, "1st"): SUBJECTS_2ND_YEAR_1ST_SEM,
    (2, "2nd"): SUBJECTS_2ND_YEAR_2ND_SEM,
    (3, "1st"): SUBJECTS_3RD_YEAR_1ST_SEM,
    (3, "2nd"): SUBJECTS_3RD_YEAR_2ND_SEM,
}

# ─── Student roster ───────────────────────────────────────────────────────────
# (seed_id, student_number, full_name, email, gender)

DUMMY_STUDENTS = [
    ("uid-bsit-3a-001", "2023-00101", "Carl Anthony Unay",   "carl.unay@spc.edu.ph",        "Male"),
    ("uid-bsit-3a-002", "2023-00102", "Rojan Gadin",          "rojan.gadin@spc.edu.ph",       "Male"),
    ("uid-bsit-3a-003", "2023-00103", "Rose Ann Gabon",       "roseann.gabon@spc.edu.ph",     "Female"),
    ("uid-bsit-3a-004", "2023-00104", "Jennie Rose Abayan",   "jennierose.abayan@spc.edu.ph", "Female"),
    ("uid-bsit-3a-005", "2023-00105", "Chris Diocton",        "chris.diocton@spc.edu.ph",     "Male"),
    ("uid-bsit-3a-006", "2023-00106", "Hannah Joy Nacario",   "hannahjoy.nacario@spc.edu.ph", "Female"),
    ("uid-bsit-3a-007", "2023-00107", "Rajeth Jamorawon",     "rajeth.jamorawon@spc.edu.ph",  "Male"),
    ("uid-bsit-3a-008", "2023-00108", "Mariz Berber",         "mariz.berber@spc.edu.ph",      "Male"),
    ("uid-bsit-3a-009", "2023-00109", "Philip Brian Alvarez", "philipbrian.alvarez@spc.edu.ph","Male"),
    ("uid-bsit-3a-010", "2023-00110", "Patrick Dacles",       "patrick.dacles@spc.edu.ph",    "Male"),
]

INSTRUCTORS = [
    "Mr. Reyes, J.", "Ms. Santos, A.", "Dr. Lim, R.", "Prof. Cruz, M.",
    "Mr. Flores, B.", "Ms. Garcia, N.", "Dr. Tan, P.", "Prof. Bautista, L.",
    "Mr. Mendoza, C.", "Ms. Villanueva, S.", "Dr. Aquino, F.", "Prof. Dela Cruz, E.",
]

BLOOD_TYPES = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
RELIGIONS = ["Roman Catholic", "Roman Catholic", "Roman Catholic",
             "Iglesia ni Cristo", "Born Again Christian", "Seventh-day Adventist"]
ED_LEVELS = ["Elementary Graduate", "High School Graduate", "College Graduate", "Vocational Course"]
INCOMES = ["Below PHP 10,000", "PHP 10,000-20,000", "PHP 20,000-30,000", "PHP 30,000-50,000"]
TRACKS = ["STEM", "ABM", "HUMSS", "GAS", "TVL"]
HS_SCHOOLS = [
    "Catbalogan City National High School", "Samar National School",
    "Calbayog City NHS", "Basey NHS", "Holy Infant Academy",
]
ORGANIZATIONS = [
    "Junior Philippine Computer Society (JPCS)", "SC Supreme Student Government",
    "SC Dance Troupe", "Red Cross Youth", "Campus Ministry", "SC Math Circle",
]
ADDRESSES = [
    "#12 Rizal St., Catbalogan City, Samar",
    "Brgy. Canlapwas, Catbalogan City, Samar",
    "#5 Magsaysay Ave., Catbalogan City, Samar",
    "Brgy. Tinaplacan, Catbalogan City, Samar",
    "#21 Del Pilar St., Catbalogan City, Samar",
    "Brgy. Mercedes, Catbalogan City, Samar",
    "#8 Luna St., Catbalogan City, Samar",
    "#7 Burgos St., Catbalogan City, Samar",
    "#14 Arteche St., Catbalogan City, Samar",
    "#33 Mabini St., Catbalogan City, Samar",
]
BIRTHDAYS = [
    "2004-03-15", "2003-11-08", "2004-07-22", "2004-01-30",
    "2003-09-05", "2004-04-17", "2003-12-25", "2004-06-10",
    "2003-08-01", "2004-02-19",
]
FATHER_NAMES = [
    "Roberto Unay", "Eduardo Gadin", "Felix Gabon", "Antonio Abayan",
    "Rodrigo Diocton", "Noel Nacario", "Pedro Jamorawon", "Carlos Berber",
    "Salvador Alvarez", "Renato Dacles",
]
MOTHER_NAMES = [
    "Maria Unay", "Lorna Gadin", "Gloria Gabon", "Rose Abayan",
    "Sylvia Diocton", "Elvira Nacario", "Carmen Jamorawon", "Josephine Berber",
    "Perla Alvarez", "Maricel Dacles",
]
FATHER_OCCS = ["Farmer", "Tricycle Driver", "Fisherman", "Businessman",
               "Jeepney Driver", "PNP Officer", "Carpenter", "Electrician",
               "Accountant", "Lawyer"]
MOTHER_OCCS = ["Teacher", "Vendor", "Laundrywoman", "Nurse",
               "Seamstress", "Teacher", "Homemaker", "Barangay Health Worker",
               "Bank Teller", "Physical Therapist"]


# ─── Grade helpers ────────────────────────────────────────────────────────────

def make_grade_entry(subject, performance):
    """Generate a grade entry for a completed semester subject."""
    ranges = {
        "excellent": (92, 99),
        "good":      (82, 91),
        "average":   (75, 81),
        "poor":      (65, 74),
    }
    lo, hi = ranges.get(performance, (75, 85))
    p  = random.randint(lo, hi)
    m  = random.randint(lo, hi)
    sf = random.randint(lo, hi)
    f  = random.randint(lo, hi)
    avg = round((p * 0.2) + (m * 0.3) + (sf * 0.2) + (f * 0.3), 1)

    if avg >= 97:   g = "1.0"
    elif avg >= 94: g = "1.25"
    elif avg >= 91: g = "1.5"
    elif avg >= 88: g = "1.75"
    elif avg >= 85: g = "2.0"
    elif avg >= 82: g = "2.25"
    elif avg >= 79: g = "2.5"
    elif avg >= 76: g = "2.75"
    elif avg >= 75: g = "3.0"
    else:           g = "5.0"

    return {
        "subject_code":  subject["subject_code"],
        "subject_name":  subject["description"],
        "units":         str(subject["units"]),
        "prelim":        str(p),
        "midterm":       str(m),
        "semi_final":    str(sf),
        "final":         str(f),
        "final_grade":   g,
        "remarks":       "Passed" if float(g) < 4.0 else "Failed",
    }


def compute_gpa(grades):
    total_units  = sum(float(g["units"]) for g in grades if g["remarks"] == "Passed")
    weighted_sum = sum(float(g["final_grade"]) * float(g["units"]) for g in grades if g["remarks"] == "Passed")
    gpa = round(weighted_sum / total_units, 2) if total_units > 0 else 0.0
    if gpa <= 1.25:   standing = "Dean's List"
    elif gpa <= 2.0:  standing = "Good Standing"
    elif gpa <= 3.0:  standing = "Satisfactory"
    else:             standing = "Probationary"
    return gpa, standing


def make_payment_dates(school_year, semester, num):
    y = int(school_year.split("-")[0])
    if semester == "1st":
        pool = [f"{y}-08-05", f"{y}-08-20", f"{y}-09-10",
                f"{y}-09-25", f"{y}-10-08", f"{y}-10-22", f"{y}-11-05"]
    else:
        pool = [f"{y+1}-01-08", f"{y+1}-01-20", f"{y+1}-02-05",
                f"{y+1}-02-18", f"{y+1}-03-04", f"{y+1}-03-18", f"{y+1}-04-02"]
    return sorted(random.sample(pool, min(num, len(pool))))


def build_financial_record(year_level, semester, school_year, is_completed):
    """Build tuition/payment record for a given semester."""
    tuition_data = SEMESTER_TUITION[(year_level, semester)]
    net = round(tuition_data["total"], 2)
    breakdown_items = [
        {"fee_name": k, "amount": str(round(v, 2))}
        for k, v in tuition_data["breakdown"].items()
    ]

    if is_completed:
        # Completed semesters: always fully paid
        paid = net
        scenario = "fully_paid"
    else:
        # Current semester: random partial payment, min 1500
        scenario = random.choices(
            ["fully_paid", "partial_high", "partial_mid", "minimum"],
            weights=[0.20, 0.30, 0.35, 0.15]
        )[0]
        if scenario == "fully_paid":
            paid = net
        elif scenario == "partial_high":
            paid = round(random.uniform(net * 0.6, net * 0.9), 2)
        elif scenario == "partial_mid":
            paid = round(random.uniform(net * 0.3, net * 0.6), 2)
        else:
            paid = round(random.uniform(MINIMUM_PAYMENT, max(MINIMUM_PAYMENT, net * 0.3)), 2)

    paid = max(min(paid, net), MINIMUM_PAYMENT)
    balance = round(net - paid, 2)

    n_pay = random.randint(1, 4) if scenario == "fully_paid" else random.randint(1, 3)
    if scenario == "minimum":
        n_pay = 1

    dates = make_payment_dates(school_year, semester, n_pay)
    left = paid
    history = []
    for i, date in enumerate(dates):
        if i == len(dates) - 1:
            amt = round(left, 2)
        else:
            mx = left - (500 * (len(dates) - i - 1))
            amt = round(random.uniform(500, max(500, mx)), 2)
        left = round(left - amt, 2)
        history.append({
            "date": date,
            "amount": str(amt),
            "reference_no": f"OR-{random.randint(100000, 999999)}",
            "payment_method": random.choice(["Cash", "GCash", "Bank Transfer", "Cash", "Cash"]),
        })

    if balance == 0:
        status = "Fully Paid"
    elif paid <= MINIMUM_PAYMENT + 500:
        status = "Minimum Paid"
    else:
        status = "Partial"

    return {
        "gross_total":           str(round(net, 2)),
        "scholarship_discount":  "0",
        "net_assessment":        str(round(net, 2)),
        "total_tuition":         str(round(net, 2)),
        "total_paid":            str(round(paid, 2)),
        "balance":               str(round(balance, 2)),
        "payment_history":       history,
        "payment_status":        status,
        "minimum_payment":       str(MINIMUM_PAYMENT),
        "tuition_breakdown":     breakdown_items,
        "misc_fees":             {k: str(round(v, 2)) for k, v in tuition_data["breakdown"].items()},
    }


def subjects_to_grade_entries(subjects, performance_weights):
    perf_pool = ["excellent", "good", "average", "poor"]
    return [
        make_grade_entry(s, random.choices(perf_pool, weights=performance_weights)[0])
        for s in subjects
    ]


def build_enrolled_subjects(subjects):
    """Build enrolled_subjects list for current semester (with instructor/schedule info)."""
    instructors = INSTRUCTORS[:]
    random.shuffle(instructors)
    enrolled = []
    for i, s in enumerate(subjects):
        enrolled.append({
            "subject_code": s["subject_code"],
            "subject_name": s["description"],
            "units":        str(s["units"]),
            "schedule":     f"{s['day']} {s['time']}",
            "room":         s["room"],
            "instructor":   instructors[i % len(instructors)],
            "schedule_code": s["schedule_code"],
        })
    return enrolled


def get_school_year(year_level):
    """Return school year string for a given year level (current = 3rd year → 2025-2026)."""
    # 3rd year 2nd sem (current) = 2025-2026
    # 3rd year 1st sem = 2025-2026
    # 2nd year = 2024-2025
    # 1st year = 2023-2024
    base_end = 2026  # current school year end
    offset = 3 - year_level
    end = base_end - offset
    return f"{end - 1}-{end}"


# ─── Main record builder ──────────────────────────────────────────────────────

def build_student_record(idx, seed_id, student_number, full_name, email, gender):
    random.seed(hash(full_name) % (2 ** 31))

    # Performance profile
    profiles = ["excellent", "good", "good", "average", "average"]
    perf_profile = random.choice(profiles)
    pw = {
        "excellent": [0.6, 0.3, 0.1, 0.0],
        "good":      [0.2, 0.5, 0.2, 0.1],
        "average":   [0.1, 0.3, 0.5, 0.1],
    }
    weights = pw.get(perf_profile, pw["average"])

    # ── Build academic history (years 1 and 2, both sems each — fully paid) ──
    academic_history = []
    school_year_summaries = []

    for yr in [1, 2]:
        sy = get_school_year(yr)
        for sem in ["1st", "2nd"]:
            subjects = SEMESTER_SUBJECTS[(yr, sem)]
            grades = subjects_to_grade_entries(subjects, weights)
            gpa, standing = compute_gpa(grades)
            fin = build_financial_record(yr, sem, sy, is_completed=True)
            academic_history.append({
                "year_level":  str(yr),
                "semester":    sem,
                "school_year": sy,
                "gpa":         str(gpa),
                "grades":      grades,
                **{k: fin[k] for k in ["gross_total", "scholarship_discount",
                   "net_assessment", "total_tuition", "total_paid",
                   "balance", "payment_history", "payment_status",
                   "minimum_payment", "tuition_breakdown", "misc_fees"]},
            })

        # Annual summary
        sem1 = academic_history[-2]
        sem2 = academic_history[-1]
        all_yr_grades = sem1["grades"] + sem2["grades"]
        yr_gpa, yr_standing = compute_gpa(all_yr_grades)
        gross  = round(float(sem1["gross_total"]) + float(sem2["gross_total"]), 2)
        paid   = round(float(sem1["total_paid"])  + float(sem2["total_paid"]),  2)
        school_year_summaries.append({
            "year_level":            str(yr),
            "school_year":           sy,
            "year_gpa":              str(yr_gpa),
            "year_standing":         yr_standing,
            "annual_gross":          str(gross),
            "annual_discount":       "0",
            "annual_net":            str(gross),
            "annual_total_paid":     str(paid),
            "annual_balance":        "0",
            "annual_payment_status": "Fully Paid",
        })

    # ── 3rd year 1st sem (completed — must be fully paid to enroll 2nd sem) ──
    sy_3rd = get_school_year(3)
    subj_3a_1st = SEMESTER_SUBJECTS[(3, "1st")]
    grades_3a_1st = subjects_to_grade_entries(subj_3a_1st, weights)
    gpa_3a_1st, _ = compute_gpa(grades_3a_1st)
    fin_3a_1st = build_financial_record(3, "1st", sy_3rd, is_completed=True)

    academic_history.append({
        "year_level":  "3",
        "semester":    "1st",
        "school_year": sy_3rd,
        "gpa":         str(gpa_3a_1st),
        "grades":      grades_3a_1st,
        **{k: fin_3a_1st[k] for k in ["gross_total", "scholarship_discount",
           "net_assessment", "total_tuition", "total_paid", "balance",
           "payment_history", "payment_status", "minimum_payment",
           "tuition_breakdown", "misc_fees"]},
    })

    # ── Current semester: 3rd year 2nd sem, 2025-2026 — grades NOT submitted ──
    subj_current = SUBJECTS_3RD_YEAR_2ND_SEM
    enrolled_subjects = build_enrolled_subjects(subj_current)

    # Grades not yet submitted — all blank/pending
    current_grades = []
    for s in subj_current:
        current_grades.append({
            "subject_code": s["subject_code"],
            "subject_name": s["description"],
            "units":        str(s["units"]),
            "prelim":       "N/A",
            "midterm":      "N/A",
            "semi_final":   "N/A",
            "final":        "N/A",
            "final_grade":  "N/A",
            "remarks":      "Ongoing",
        })

    # Cumulative GPA based on all completed semesters
    all_completed_grades = []
    for h in academic_history:
        all_completed_grades.extend(h["grades"])
    cum_gpa, cum_standing = compute_gpa(all_completed_grades)

    # Current semester financials (random balance, min 1500 paid)
    fin_current = build_financial_record(3, "2nd", sy_3rd, is_completed=False)

    # ── Personal information ──
    age = 2026 - int(BIRTHDAYS[idx][:4])
    civil_status = "Single"
    father_name = FATHER_NAMES[idx]
    mother_name = MOTHER_NAMES[idx]
    father_occ  = FATHER_OCCS[idx]
    mother_occ  = MOTHER_OCCS[idx]

    health_record = {
        "medical_conditions": random.choice(["None", "None", "None", "Asthma (mild)", "Myopia"]),
        "allergies":          random.choice(["None", "None", "Seafood", "Dust"]),
        "vaccinations":       ["BCG", "Hepatitis B", "Measles", "Polio", "COVID-19 (2 doses + booster)"],
        "last_physical_exam": random.choice(["2024-06-15", "2025-01-10", "2025-06-01"]),
        "blood_pressure":     random.choice(["Normal", "120/80", "110/70"]),
        "vision":             random.choice(["Normal", "20/20", "With corrective lenses", "Normal"]),
    }

    awards = []
    if cum_gpa <= 1.25:
        awards.append({"award": "Dean's List", "semester": "1st Semester", "school_year": "2024-2025"})
    if cum_gpa <= 1.5:
        awards.append({"award": "With Honors Recognition", "semester": "2nd Semester", "school_year": "2023-2024"})

    n_orgs = random.randint(0, 2)
    orgs = [
        {
            "name":        o,
            "position":    random.choice(["Member", "Member", "Secretary", "Treasurer"]),
            "year_joined": str(random.randint(2023, 2025)),
        }
        for o in random.sample(ORGANIZATIONS, min(n_orgs, len(ORGANIZATIONS)))
    ]

    return {
        # Identity
        "student_id":         seed_id,
        "student_number":     student_number,
        "full_name":          full_name,
        "email":              email,
        "program":            "Bachelor of Science in Information Technology",
        "program_code":       "BSIT",
        "year_level":         "3",
        "section":            "BSIT-3A",
        "student_status":     "Regular",
        "enrollment_status":  "Enrolled",

        # Current semester info
        "semester":           "2nd",
        "school_year":        sy_3rd,
        "enrolled_subjects":  enrolled_subjects,
        "grades":             current_grades,
        "gpa":                "N/A",   # current sem grades not yet submitted
        "cumulative_gpa":     str(cum_gpa),
        "remarks":            cum_standing,

        # Academic history
        "academic_history":       academic_history,
        "school_year_summaries":  school_year_summaries,

        # Current semester financials
        **{k: fin_current[k] for k in ["gross_total", "scholarship_discount",
           "net_assessment", "total_tuition", "total_paid", "balance",
           "payment_history", "payment_status", "minimum_payment",
           "tuition_breakdown", "misc_fees"]},

        # Scholarship
        "scholarship": {"name": "None", "discount_pct": 0, "grantor": "N/A", "type": "N/A"},

        # Personal
        "birthday":    BIRTHDAYS[idx],
        "age":         str(age),
        "birthplace":  "Catbalogan City, Samar",
        "address":     ADDRESSES[idx],
        "gender":      gender,
        "civil_status":civil_status,
        "religion":    random.choice(RELIGIONS),
        "nationality": "Filipino",
        "blood_type":  random.choice(BLOOD_TYPES),
        "height_cm":   str(random.randint(150, 178)),
        "weight_kg":   str(random.randint(48, 82)),
        "phone_number":f"09{random.randint(100000000, 999999999)}",

        # Family
        "father_name":           father_name,
        "father_occupation":     father_occ,
        "father_education":      random.choice(ED_LEVELS),
        "mother_name":           mother_name,
        "mother_occupation":     mother_occ,
        "mother_education":      random.choice(ED_LEVELS),
        "monthly_family_income": random.choice(INCOMES),
        "number_of_siblings":    str(random.randint(0, 5)),
        "emergency_contact_name":   mother_name,
        "emergency_contact_phone":  f"09{random.randint(100000000, 999999999)}",
        "emergency_contact_relation": "Mother",

        # Health & records
        "health_record":       health_record,
        "disciplinary_record": {"violations": [], "status": "No disciplinary record on file."},
        "awards_honors":       awards,
        "organizations":       orgs,

        # Previous school
        "previous_school": {
            "school_name":    random.choice(HS_SCHOOLS),
            "school_type":    random.choice(["Public", "Public", "Private"]),
            "year_graduated": str(random.randint(2020, 2023)),
            "general_average":f"{random.uniform(85.0, 97.0):.2f}",
            "track_strand":   random.choice(TRACKS),
        },
    }


# ─── DynamoDB helpers ─────────────────────────────────────────────────────────

def create_table_if_not_exists():
    existing = [t.name for t in dynamodb.tables.all()]
    if TABLE_NAME in existing:
        print(f"Table '{TABLE_NAME}' already exists.")
        return dynamodb.Table(TABLE_NAME)
    print(f"Creating table '{TABLE_NAME}' ...")
    table = dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "student_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "student_id",     "AttributeType": "S"},
            {"AttributeName": "email",          "AttributeType": "S"},
            {"AttributeName": "student_number", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName":  "email-index",
                "KeySchema":  [{"AttributeName": "email", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName":  "student-number-index",
                "KeySchema":  [{"AttributeName": "student_number", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    print(f"Table '{TABLE_NAME}' is ACTIVE.")
    return table


def seed_students(table):
    print(f"\nSeeding {len(DUMMY_STUDENTS)} BSIT 3rd year students...\n")
    print(f"  {'Name':<28} {'No.':<14} {'Bal':>10}  {'CumGPA':<8} Status")
    print(f"  {'-'*70}")
    for idx, (seed_id, sno, name, email, gender) in enumerate(DUMMY_STUDENTS):
        record = build_student_record(idx, seed_id, sno, name, email, gender)
        table.put_item(Item=record)
        print(
            f"  OK {name:<28} {sno:<14}"
            f" PHP {record['balance']:>8}  "
            f"{record['cumulative_gpa']:<8} "
            f"{record['payment_status']}"
        )
    print(f"\nDone. {len(DUMMY_STUDENTS)} records written.")
    print("\nNote: Current semester (3rd Year 2nd Sem, S.Y. 2025-2026) grades are")
    print("      NOT submitted yet — all grade fields show 'N/A / Ongoing'.")


if __name__ == "__main__":
    table = create_table_if_not_exists()
    seed_students(table)
    print("\nAll done!")