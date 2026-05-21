import boto3
import random
from dotenv import load_dotenv
import os

load_dotenv()
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
client   = boto3.client("dynamodb",  region_name=AWS_REGION)
TABLE_NAME = "StudentRecords"

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
    "Mr. Pascual, R.", "Ms. Reyes, C.", "Dr. Morales, T.", "Prof. Gomez, A.",
]
DAYS  = ["MWF","TTh","MWF","TTh","MWF","Sat"]
TIMES = ["7:00-8:00 AM","8:00-9:00 AM","9:00-10:00 AM","10:00-11:00 AM",
         "11:00-12:00 PM","1:00-2:00 PM","2:00-3:00 PM","3:00-4:00 PM",
         "4:00-5:00 PM","7:30-9:00 AM","9:00-10:30 AM","1:30-3:00 PM"]
ROOMS = ["Room 101","Room 102","Room 103","Room 201","Room 202","Room 203",
         "Room 301","Room 302","Lab 1","Lab 2","AVR","Room 105"]

# School years — year_level 1 is currently enrolled (2024-2025)
# So a 3rd year student: Y1=2022-2023, Y2=2023-2024, Y3(current)=2024-2025
SCHOOL_YEARS = {1:"2022-2023", 2:"2023-2024", 3:"2024-2025", 4:"2025-2026"}
# The "current" school year depends on the student year level
def get_school_year(yr_level, student_year_level):
    # base = current enrollment year (2024-2025 for year_level student)
    # past years go backwards
    offset = student_year_level - yr_level
    base = 2025  # current year end
    end_year = base - offset
    return f"{end_year-1}-{end_year}"

TUITION_PER_SUBJECT = {
    "GE001":3000,"GE002":3000,"GE003":3200,"GE004":3000,"PE001":800,"NSTP1":500,"NSTP2":500,
    "IT101":3500,"IT102":3800,"IT201":3800,"IT202":3800,"IT203":4000,"IT301":4200,
    "IT302":3800,"IT303":4000,"IT401":4200,"IT402":4500,
    "CS101":3500,"CS102":3800,"CS201":3800,"CS202":4000,"CS203":4000,"CS301":4200,
    "CS302":4000,"CS303":4200,"CS401":4500,"CS402":4800,
    "ED101":3200,"ED102":3200,"ED201":3500,"ED202":3500,"ED203":3500,"ED301":4000,"ED302":5000,
    "MATH1":3000,"SCI01":3200,
    "EE101":3200,"EE102":3200,"EE201":3500,"EE202":3500,"EE203":3500,"EE301":4000,"EE302":5000,
    "CR101":3200,"CR102":3500,"CR201":3500,"CR202":3500,"CR203":3800,"CR301":3500,"CR302":3500,"CR401":5000,
    "BA101":3200,"BA102":3500,"BA201":3500,"BA202":3500,"BA203":3800,"BA301":3500,"BA302":3500,"BA401":4000,
    "NUR101":4500,"NUR102":5500,"NUR201":5500,"NUR202":5500,"NUR203":4500,
    "NUR301":5500,"NUR302":5500,"NUR401":4000,
}
DEFAULT_TUITION = 3500
MINIMUM_PAYMENT = 1500

MISC_FEES_TEMPLATE = {
    "Registration Fee":500,"Library Fee":300,"Athletic Fee":200,
    "Development Fee":400,"Student ID Fee":150,"Guidance Fee":100,
    "Medical/Dental Fee":200,"Cultural Fee":100,"Computer Lab Fee":250,"Examination Fee":300,
}

SUBJECTS_BY_PROGRAM = {
    "BSIT":[
        ("IT101","Introduction to Computing",3),("IT102","Computer Programming 1",3),
        ("IT201","Data Structures and Algorithms",3),("IT202","Object-Oriented Programming",3),
        ("IT203","Database Management Systems",3),("IT301","Web Development",3),
        ("IT302","Systems Analysis and Design",3),("IT303","Computer Networks",3),
        ("IT401","Software Engineering",3),("IT402","Capstone Project 1",3),
        ("GE001","Understanding the Self",3),("GE002","Readings in Philippine History",3),
        ("GE003","Mathematics in the Modern World",3),("GE004","Purposive Communication",3),
        ("PE001","Physical Education 1",2),("NSTP1","National Service Training Program 1",3),
        ("NSTP2","National Service Training Program 2",3),
    ],
    "BSCS":[
        ("CS101","Discrete Mathematics",3),("CS102","Computer Programming 1",3),
        ("CS201","Data Structures",3),("CS202","Algorithm Design",3),
        ("CS203","Computer Organization",3),("CS301","Operating Systems",3),
        ("CS302","Theory of Computation",3),("CS303","Software Engineering",3),
        ("CS401","Artificial Intelligence",3),("CS402","Machine Learning",3),
        ("GE001","Understanding the Self",3),("GE003","Mathematics in the Modern World",3),
        ("GE004","Purposive Communication",3),("PE001","Physical Education 1",2),
        ("NSTP1","National Service Training Program 1",3),
    ],
    "BSEd":[
        ("ED101","Child and Adolescent Development",3),("ED102","The Teaching Profession",3),
        ("ED201","Facilitating Learning",3),("ED202","Curriculum Development",3),
        ("ED203","Assessment of Student Learning 1",3),("ED301","Field Study 1",3),
        ("ED302","Practice Teaching",6),("MATH1","College Algebra",3),("SCI01","Earth Science",3),
        ("GE001","Understanding the Self",3),("GE004","Purposive Communication",3),
        ("PE001","Physical Education 1",2),("NSTP1","National Service Training Program 1",3),
    ],
    "BEEd":[
        ("EE101","Child Development",3),("EE102","Foundations of Special Education",3),
        ("EE201","Teaching Multiliteracies",3),("EE202","Assessment in Elementary School",3),
        ("EE203","Principles of Teaching 1",3),("EE301","Field Study 1",3),
        ("EE302","Practice Teaching",6),("GE001","Understanding the Self",3),
        ("GE004","Purposive Communication",3),("PE001","Physical Education 1",2),
        ("NSTP1","National Service Training Program 1",3),
    ],
    "BSCrim":[
        ("CR101","Introduction to Criminology",3),("CR102","Criminal Law 1",3),
        ("CR201","Criminal Law 2",3),("CR202","Law Enforcement Administration",3),
        ("CR203","Criminalistics 1",3),("CR301","Juvenile Delinquency",3),
        ("CR302","Criminal Procedure",3),("CR401","Practicum",6),
        ("GE001","Understanding the Self",3),("GE004","Purposive Communication",3),
        ("PE001","Physical Education 1",2),("NSTP1","National Service Training Program 1",3),
    ],
    "BSBA":[
        ("BA101","Principles of Management",3),("BA102","Accounting 1",3),
        ("BA201","Marketing Management",3),("BA202","Business Statistics",3),
        ("BA203","Financial Management",3),("BA301","Human Resource Management",3),
        ("BA302","Operations Management",3),("BA401","Entrepreneurship",3),
        ("GE001","Understanding the Self",3),("GE003","Mathematics in the Modern World",3),
        ("GE004","Purposive Communication",3),("PE001","Physical Education 1",2),
        ("NSTP1","National Service Training Program 1",3),
    ],
    "BSN":[
        ("NUR101","Anatomy and Physiology",3),("NUR102","Fundamentals of Nursing",5),
        ("NUR201","Medical-Surgical Nursing 1",5),("NUR202","Maternal and Child Nursing",5),
        ("NUR203","Pharmacology",3),("NUR301","Psychiatric Nursing",5),
        ("NUR302","Community Health Nursing",5),("NUR401","Nursing Research",3),
        ("GE001","Understanding the Self",3),("GE004","Purposive Communication",3),
        ("PE001","Physical Education 1",2),("NSTP1","National Service Training Program 1",3),
    ],
}

SCHOLARSHIPS = [
    None, None, None,
    {"name":"Academic Excellence Scholarship","discount_pct":100,"grantor":"Samar College","type":"Merit"},
    {"name":"Dean's List Scholarship","discount_pct":50,"grantor":"Samar College","type":"Merit"},
    {"name":"CHED TDP Scholarship","discount_pct":100,"grantor":"CHED","type":"Government"},
    {"name":"UniFAST Scholarship","discount_pct":100,"grantor":"CHED / UniFAST","type":"Government"},
    {"name":"PESFA Scholarship","discount_pct":80,"grantor":"TESDA / CHED","type":"Government"},
    {"name":"Barangay Scholarship","discount_pct":30,"grantor":"Local Government Unit","type":"LGU"},
    {"name":"Athletic Scholarship","discount_pct":50,"grantor":"Samar College","type":"Athletic"},
    {"name":"Working Student Discount","discount_pct":20,"grantor":"Samar College","type":"Special"},
]

ORGANIZATIONS = [
    "Junior Philippine Computer Society (JPCS)","SC Supreme Student Government",
    "SC Dance Troupe","SC Chorale","Red Cross Youth",
    "Junior Nurses Association of the Philippines (JNAP)",
    "Criminology Society","Business Club","Campus Ministry",
    "SC Debate Team","SC Math Circle","Theater Arts Guild","Environmental Awareness Club",
]
BLOOD_TYPES = ["A+","A-","B+","B-","AB+","AB-","O+","O-"]
MEDICAL_CONDITIONS = [
    None,None,None,None,
    "Asthma (mild, managed with inhaler)","Myopia (corrected with glasses)",
    "Allergic Rhinitis","Hypertension (monitored)","Diabetes Type 2 (diet-controlled)",
]

DUMMY_STUDENTS = [
    ("uid-bsit-001","2021-00101","Juan Dela Cruz","juan.delacruz@spc.edu.ph","BSIT",3,"BSIT-3A",
     "2003-04-12","Catbalogan City, Samar","#12 Rizal St., Catbalogan City, Samar","Male","Single",
     "Roberto Dela Cruz","Farmer","Maria Dela Cruz","Teacher","Maria Dela Cruz","09171234567","Mother"),
    ("uid-bsit-002","2022-00202","Maria Santos","maria.santos@spc.edu.ph","BSIT",2,"BSIT-2B",
     "2004-07-25","Calbayog City, Samar","#5 Magsaysay Ave., Calbayog City, Samar","Female","Single",
     "Jose Santos","Tricycle Driver","Lorna Santos","Vendor","Jose Santos","09289876543","Father"),
    ("uid-bsit-003","2020-00303","Carlo Reyes","carlo.reyes@spc.edu.ph","BSIT",4,"BSIT-4A",
     "2002-01-08","Basey, Samar","Brgy. Poblacion, Basey, Samar","Male","Single",
     "Fernando Reyes","Fisherman","Gloria Reyes","Laundrywoman","Gloria Reyes","09351122334","Mother"),
    ("uid-bsit-004","2023-00404","Ana Lim","ana.lim@spc.edu.ph","BSIT",1,"BSIT-1A",
     "2005-11-19","Catbalogan City, Samar","#33 Mabini St., Catbalogan City, Samar","Female","Single",
     "Antonio Lim","Businessman","Rose Lim","Nurse","Rose Lim","09456789012","Mother"),
    ("uid-bsit-005","2022-00505","Jose Garcia","jose.garcia@spc.edu.ph","BSIT",2,"BSIT-2A",
     "2004-03-30","Marabut, Samar","Brgy. Guinsorongan, Marabut, Samar","Male","Single",
     "Pedro Garcia","Jeepney Driver","Carmen Garcia","Seamstress","Pedro Garcia","09567890123","Father"),
    ("uid-bscs-001","2021-00601","Rizalina Cruz","rizalina.cruz@spc.edu.ph","BSCS",3,"BSCS-3A",
     "2003-09-14","Catbalogan City, Samar","#8 Luna St., Catbalogan City, Samar","Female","Single",
     "Ramon Cruz","Government Employee","Teresita Cruz","Homemaker","Ramon Cruz","09678901234","Father"),
    ("uid-bscs-002","2022-00702","Miguel Flores","miguel.flores@spc.edu.ph","BSCS",2,"BSCS-2A",
     "2004-06-22","Paranas, Samar","Brgy. San Miguel, Paranas, Samar","Male","Single",
     "Eduardo Flores","OFW (Saudi Arabia)","Melinda Flores","Teacher","Melinda Flores","09789012345","Mother"),
    ("uid-bsed-001","2021-00801","Luisa Villanueva","luisa.villanueva@spc.edu.ph","BSEd",3,"BSEd-3A",
     "2003-02-17","Catbalogan City, Samar","Brgy. Canlapwas, Catbalogan City, Samar","Female","Single",
     "Danilo Villanueva","Security Guard","Nida Villanueva","Market Vendor","Nida Villanueva","09890123456","Mother"),
    ("uid-bsed-002","2022-00902","Ramon Aquino","ramon.aquino@spc.edu.ph","BSEd",2,"BSEd-2A",
     "2004-12-05","Gandara, Samar","Brgy. Imelda, Gandara, Samar","Male","Single",
     "Rodrigo Aquino","Farmer","Sylvia Aquino","Homemaker","Rodrigo Aquino","09901234567","Father"),
    ("uid-beed-001","2021-01001","Pia Mendoza","pia.mendoza@spc.edu.ph","BEEd",3,"BEEd-3A",
     "2003-08-28","Catbalogan City, Samar","#21 Del Pilar St., Catbalogan City, Samar","Female","Single",
     "Carlos Mendoza","Electrician","Josephine Mendoza","Barangay Health Worker","Josephine Mendoza","09012345678","Mother"),
    ("uid-beed-002","2023-01102","Felix Bautista","felix.bautista@spc.edu.ph","BEEd",1,"BEEd-1A",
     "2005-05-11","Calbiga, Samar","Brgy. San Isidro, Calbiga, Samar","Male","Single",
     "Ernesto Bautista","Carpenter","Linda Bautista","Laundrywoman","Ernesto Bautista","09123456789","Father"),
    ("uid-crim-001","2020-01201","Andres Tan","andres.tan@spc.edu.ph","BSCrim",4,"BSCrim-4A",
     "2002-10-03","Catbalogan City, Samar","#7 Burgos St., Catbalogan City, Samar","Male","Single",
     "William Tan","Businessman","Shirley Tan","Bookkeeper","Shirley Tan","09234567890","Mother"),
    ("uid-crim-002","2022-01302","Celia Ramos","celia.ramos@spc.edu.ph","BSCrim",2,"BSCrim-2A",
     "2004-04-18","Catbalogan City, Samar","Brgy. Tinaplacan, Catbalogan City, Samar","Female","Single",
     "Noel Ramos","PNP Officer","Elvira Ramos","Teacher","Noel Ramos","09345678901","Father"),
    ("uid-bsba-001","2021-01401","Nathaniel Ocampo","nathaniel.ocampo@spc.edu.ph","BSBA",3,"BSBA-3A",
     "2003-07-07","Catbalogan City, Samar","#14 Arteche St., Catbalogan City, Samar","Male","Single",
     "Salvador Ocampo","Accountant","Perla Ocampo","Bank Teller","Perla Ocampo","09456789013","Mother"),
    ("uid-bsba-002","2023-01502","Sophia Torres","sophia.torres@spc.edu.ph","BSBA",1,"BSBA-1A",
     "2005-09-24","Catbalogan City, Samar","#2 Session Rd., Catbalogan City, Samar","Female","Single",
     "Renato Torres","Lawyer","Maricel Torres","Physical Therapist","Maricel Torres","09567890124","Mother"),
    ("uid-bsn-001","2021-01601","Angeline Castillo","angeline.castillo@spc.edu.ph","BSN",3,"BSN-3A",
     "2003-01-31","Catbalogan City, Samar","#9 Quezon Blvd., Catbalogan City, Samar","Female","Single",
     "Isidro Castillo","Barangay Captain","Corazon Castillo","Registered Nurse","Corazon Castillo","09678901235","Mother"),
    ("uid-bsn-002","2022-01702","Eduardo Navarro","eduardo.navarro@spc.edu.ph","BSN",2,"BSN-2A",
     "2004-08-16","Catbalogan City, Samar","Brgy. Mercedes, Catbalogan City, Samar","Male","Single",
     "Ricardo Navarro","OFW (Dubai)","Lourdes Navarro","Midwife","Lourdes Navarro","09789012346","Mother"),
]


# ─── Grade helpers ────────────────────────────────────────────

def make_grade_entry(code, name, units, performance):
    ranges = {"excellent":(92,99),"good":(82,91),"average":(75,81),"poor":(65,74)}
    lo,hi = ranges.get(performance,(75,85))
    p=random.randint(lo,hi); m=random.randint(lo,hi)
    sf=random.randint(lo,hi); f=random.randint(lo,hi)
    avg=round((p*0.2)+(m*0.3)+(sf*0.2)+(f*0.3),1)
    if avg>=97:   g="1.0"
    elif avg>=94: g="1.25"
    elif avg>=91: g="1.5"
    elif avg>=88: g="1.75"
    elif avg>=85: g="2.0"
    elif avg>=82: g="2.25"
    elif avg>=79: g="2.5"
    elif avg>=76: g="2.75"
    elif avg>=75: g="3.0"
    else:         g="5.0"
    return {"subject_code":code,"subject_name":name,"units":str(units),
            "prelim":str(p),"midterm":str(m),"semi_final":str(sf),"final":str(f),
            "final_grade":g,"remarks":"Passed" if float(g)<4.0 else "Failed"}


def _compute_gpa(grades):
    t=sum(float(g["units"]) for g in grades if g["remarks"]=="Passed")
    w=sum(float(g["final_grade"])*float(g["units"]) for g in grades if g["remarks"]=="Passed")
    gpa=round(w/t,2) if t>0 else 0.0
    if gpa<=1.25:  s="Dean's List"
    elif gpa<=2.0: s="Good Standing"
    elif gpa<=3.0: s="Satisfactory"
    else:          s="Probationary"
    return gpa,s


def _build_sem_grades(all_subjects,weights,pl):
    picked=random.sample(all_subjects,min(7,len(all_subjects)))
    return [make_grade_entry(c,n,u,random.choices(pl,weights=weights)[0]) for c,n,u in picked]


def _build_enrolled(all_subjects):
    picked=random.sample(all_subjects,min(7,len(all_subjects)))
    used=set(); result=[]
    for code,name,units in picked:
        for _ in range(20):
            day=random.choice(DAYS); t=random.choice(TIMES); slot=f"{day} {t}"
            if slot not in used: used.add(slot); break
        result.append({"subject_code":code,"subject_name":name,"units":str(units),
                        "schedule":f"{day} {t}","room":random.choice(ROOMS),
                        "instructor":random.choice(INSTRUCTORS)})
    return result


# ─── Financial helpers ────────────────────────────────────────

def _make_payment_dates(school_year, semester, num):
    """Generate realistic payment dates for a given school year and semester."""
    y_start = int(school_year.split("-")[0])
    if semester == "1st":
        # 1st sem: August enrollment, payments Aug–Oct
        pool = [
            f"{y_start}-08-05", f"{y_start}-08-12", f"{y_start}-08-20",
            f"{y_start}-09-05", f"{y_start}-09-18", f"{y_start}-10-03",
            f"{y_start}-10-20", f"{y_start}-11-08",
        ]
    else:
        # 2nd sem: January enrollment, payments Jan–Mar
        pool = [
            f"{y_start+1}-01-08", f"{y_start+1}-01-15", f"{y_start+1}-01-25",
            f"{y_start+1}-02-05", f"{y_start+1}-02-18", f"{y_start+1}-03-03",
            f"{y_start+1}-03-17", f"{y_start+1}-04-02",
        ]
    return sorted(random.sample(pool, min(num, len(pool))))


def generate_sem_tuition(subjects, school_year, semester, scholarship, is_completed):
    """
    Generate one semester's tuition record.
    is_completed=True means the school year is finished — balance should be 0
    (cleared before enrollment to next year is allowed).
    """
    breakdown=[]; tuition_total=0
    for s in subjects:
        code=s.get("subject_code",""); name=s.get("subject_name","")
        fee=TUITION_PER_SUBJECT.get(code,DEFAULT_TUITION)
        breakdown.append({"subject_code":code,"subject_name":name,"fee":str(fee)})
        tuition_total+=fee

    misc={k:str(v) for k,v in MISC_FEES_TEMPLATE.items()}
    misc_total=sum(MISC_FEES_TEMPLATE.values())
    gross=tuition_total+misc_total

    discount=0
    if scholarship: discount=int(gross*scholarship.get("discount_pct",0)/100)
    net=gross-discount

    # Determine payment scenario
    if is_completed:
        # Completed semesters are always fully paid
        # (schools require clearance before next enrollment)
        scenario = "fully_paid"
    else:
        # Current semester — realistic partial payments
        scenario = random.choices(
            ["fully_paid","partial_high","partial_mid","minimum"],
            weights=[0.25,0.30,0.30,0.15]
        )[0]

    if scenario=="fully_paid":
        paid=net
    elif scenario=="partial_high":
        paid=random.randint(int(net*0.6),int(net*0.9))
    elif scenario=="partial_mid":
        paid=random.randint(int(net*0.3),int(net*0.6))
    else:
        paid=random.randint(MINIMUM_PAYMENT,max(MINIMUM_PAYMENT,int(net*0.3)))

    paid=max(min(paid,net),MINIMUM_PAYMENT)
    balance=net-paid

    # Number of payment transactions
    if scenario=="minimum":
        n_pay=1
    elif scenario=="fully_paid" and paid==net:
        # Fully paid — could be 1 lump sum or spread across 2-4 installments
        n_pay=random.randint(1,4)
    else:
        n_pay=random.randint(1,3)

    dates=_make_payment_dates(school_year, semester, n_pay)
    left=paid; history=[]
    for i,date in enumerate(dates):
        if i==len(dates)-1:
            amt=left
        else:
            mx=left-(500*(len(dates)-i-1)); amt=random.randint(500,max(500,mx))
        left-=amt
        history.append({
            "date":date,"amount":str(amt),
            "reference_no":f"OR-{random.randint(100000,999999)}",
            "payment_method":random.choice(["Cash","GCash","Bank Transfer","Cash","Cash"]),
        })

    if balance==0:                  status="Fully Paid"
    elif paid<=MINIMUM_PAYMENT+500: status="Minimum Paid"
    else:                           status="Partial"

    return {
        "tuition_breakdown":breakdown,"misc_fees":misc,
        "tuition_subtotal":str(tuition_total),"misc_subtotal":str(misc_total),
        "gross_total":str(gross),"scholarship_discount":str(discount),
        "net_assessment":str(net),"total_tuition":str(net),
        "total_paid":str(paid),"balance":str(balance),
        "payment_history":history,"payment_status":status,"minimum_payment":str(MINIMUM_PAYMENT),
    }


def _annual_summary(sem1, sem2):
    """Compute annual totals from two semester financial records."""
    def i(d,k): return int(d.get(k,0) or 0)
    return {
        "annual_gross":        str(i(sem1,"gross_total")+i(sem2,"gross_total")),
        "annual_discount":     str(i(sem1,"scholarship_discount")+i(sem2,"scholarship_discount")),
        "annual_net":          str(i(sem1,"net_assessment")+i(sem2,"net_assessment")),
        "annual_total_paid":   str(i(sem1,"total_paid")+i(sem2,"total_paid")),
        "annual_balance":      str(i(sem1,"balance")+i(sem2,"balance")),
        "annual_payment_status": "Fully Paid" if (i(sem1,"balance")+i(sem2,"balance"))==0 else "Has Balance",
    }


# ─── Main builder ─────────────────────────────────────────────

def build_student_record(
        student_id,student_number,full_name,email,program_code,year_level,section,
        birthday,birthplace,address,gender,civil_status,
        father_name,father_occ,mother_name,mother_occ,
        emergency_contact,emergency_phone,emergency_relation):

    random.seed(hash(full_name)%(2**31))
    program_name=PROGRAMS[program_code]; all_subjects=SUBJECTS_BY_PROGRAM[program_code]
    profiles=["excellent","good","good","average","average","poor"]
    pw={"excellent":[0.6,0.3,0.1,0.0],"good":[0.2,0.5,0.2,0.1],
        "average":[0.1,0.3,0.5,0.1],"poor":[0.0,0.1,0.4,0.5]}
    sp=random.choice(profiles); weights=pw[sp]; pl=["excellent","good","average","poor"]
    scholarship=random.choice(SCHOLARSHIPS)

    # ── Build academic history year by year ──────────────────
    # Each completed year has: 1st sem + 2nd sem, both FULLY PAID
    # (Philippine schools require full payment before next year enrollment)
    academic_history=[]    # list of semester records
    school_year_summaries=[]  # list of annual summaries

    completed_years = year_level - 1  # e.g. 3rd year = 2 completed years

    for yr in range(1, completed_years+1):
        sy = get_school_year(yr, year_level)

        # 1st semester of this year
        g1 = _build_sem_grades(all_subjects, weights, pl)
        gpa1,_ = _compute_gpa(g1)
        subj1 = [{"subject_code":g["subject_code"],"subject_name":g["subject_name"]} for g in g1]
        fin1 = generate_sem_tuition(subj1, sy, "1st", scholarship, is_completed=True)

        sem1_record = {
            "year_level":str(yr),"semester":"1st","school_year":sy,
            "gpa":str(gpa1),"grades":g1,
            **{k:fin1[k] for k in ["tuition_breakdown","misc_fees","gross_total",
               "scholarship_discount","net_assessment","total_tuition",
               "total_paid","balance","payment_history","payment_status","minimum_payment"]},
        }

        # 2nd semester of this year
        g2 = _build_sem_grades(all_subjects, weights, pl)
        gpa2,_ = _compute_gpa(g2)
        subj2 = [{"subject_code":g["subject_code"],"subject_name":g["subject_name"]} for g in g2]
        fin2 = generate_sem_tuition(subj2, sy, "2nd", scholarship, is_completed=True)

        sem2_record = {
            "year_level":str(yr),"semester":"2nd","school_year":sy,
            "gpa":str(gpa2),"grades":g2,
            **{k:fin2[k] for k in ["tuition_breakdown","misc_fees","gross_total",
               "scholarship_discount","net_assessment","total_tuition",
               "total_paid","balance","payment_history","payment_status","minimum_payment"]},
        }

        academic_history.append(sem1_record)
        academic_history.append(sem2_record)

        # Annual summary for this school year
        ann = _annual_summary(fin1, fin2)
        yr1_gpa,yr1_stand = _compute_gpa(g1+g2)
        school_year_summaries.append({
            "year_level":    str(yr),
            "school_year":   sy,
            "year_gpa":      str(yr1_gpa),
            "year_standing": yr1_stand,
            **ann,
        })

    # ── Current semester (1st sem of current year) ──────────
    current_sy = get_school_year(year_level, year_level)
    enrolled = _build_enrolled(all_subjects)
    current_grades = [make_grade_entry(
        s["subject_code"],s["subject_name"],int(s["units"]),
        random.choices(pl,weights=weights)[0]) for s in enrolled]
    cur_gpa,_ = _compute_gpa(current_grades)
    cur_fin = generate_sem_tuition(
        [{"subject_code":s["subject_code"],"subject_name":s["subject_name"]} for s in enrolled],
        current_sy, "1st", scholarship, is_completed=False
    )

    # Cumulative GPA across all years
    all_grades_ever = current_grades[:]
    for h in academic_history: all_grades_ever.extend(h["grades"])
    cum_gpa, cum_standing = _compute_gpa(all_grades_ever)

    # ── Supplementary records ────────────────────────────────
    age=2024-int(birthday[:4])
    religions=["Roman Catholic","Roman Catholic","Roman Catholic","Iglesia ni Cristo","Born Again Christian","Seventh-day Adventist"]
    ed_levels=["Elementary Graduate","High School Graduate","College Graduate","Vocational Course"]
    incomes=["Below PHP 10,000","PHP 10,000-20,000","PHP 20,000-30,000","PHP 30,000-50,000","PHP 50,000 and above"]
    tracks=["STEM","ABM","HUMSS","GAS","TVL"]
    hs_schools=["Catbalogan City National High School","Samar National School","Calbayog City NHS",
                "Basey NHS","Holy Infant Academy","Sacred Heart Academy"]

    condition=random.choice(MEDICAL_CONDITIONS)
    health_record={
        "medical_conditions":condition or "None",
        "allergies":random.choice(["None","Penicillin","Seafood","Dust","Pollen"]),
        "vaccinations":["BCG","Hepatitis B","Measles","Polio","DPT","COVID-19 (2 doses + booster)"],
        "last_physical_exam":random.choice(["2024-06-15","2023-07-01","2024-01-10","2023-12-05"]),
        "physician":random.choice(["Dr. Santos, M.D.","Dr. Reyes, M.D.","Dr. Lim, M.D.","School Clinic"]),
        "blood_pressure":random.choice(["Normal","Normal","Normal","120/80","110/70"]),
        "vision":random.choice(["Normal","20/20","With corrective lenses","Normal"]),
    }

    has_vio=random.random()<0.10
    if has_vio:
        vio=random.choice([
            {"date":"2023-09-15","offense":"Tardiness (3x in one week)","sanction":"Written Warning","resolved":True},
            {"date":"2024-02-10","offense":"Dress Code Violation","sanction":"Verbal Warning","resolved":True},
            {"date":"2023-11-20","offense":"Unauthorized Absence (3 days)","sanction":"Community Service (8 hrs)","resolved":True},
        ])
        disciplinary={"violations":[vio],"status":"Resolved"}
    else:
        disciplinary={"violations":[],"status":"No disciplinary record on file."}

    awards=[]
    if cum_gpa<=1.25: awards.append({"award":"Dean's List","semester":"1st Semester","school_year":"2023-2024"})
    if cum_gpa<=1.5 and year_level>=2: awards.append({"award":"With Honors Recognition","semester":"2nd Semester","school_year":"2023-2024"})
    if random.random()<0.15: awards.append({"award":"Best in Research Paper","semester":"1st Semester","school_year":"2022-2023"})
    if random.random()<0.10: awards.append({"award":"Leadership Award","semester":"2nd Semester","school_year":"2022-2023"})

    n_orgs=random.randint(0,3)
    orgs=[{"name":o,"position":random.choice(["Member","Member","Member","Secretary","Treasurer","Vice President","President"]),
           "year_joined":str(random.randint(2021,2024))}
          for o in random.sample(ORGANIZATIONS,min(n_orgs,len(ORGANIZATIONS)))]

    return {
        # ── Identity ──
        "student_id":student_id,"student_number":student_number,"full_name":full_name,
        "email":email,"program":program_name,"program_code":program_code,
        "year_level":str(year_level),"section":section,
        "student_status":"Regular","enrollment_status":"Enrolled",
        # ── Current semester ──
        "semester":"1st","school_year":current_sy,
        "enrolled_subjects":enrolled,"grades":current_grades,
        "gpa":str(cur_gpa),"cumulative_gpa":str(cum_gpa),"remarks":cum_standing,
        # ── Academic history (completed semesters with financials) ──
        "academic_history":academic_history,
        # ── Per-year summaries ──
        "school_year_summaries":school_year_summaries,
        # ── Current semester financials ──
        "tuition_breakdown":cur_fin["tuition_breakdown"],"misc_fees":cur_fin["misc_fees"],
        "tuition_subtotal":cur_fin["tuition_subtotal"],"misc_subtotal":cur_fin["misc_subtotal"],
        "gross_total":cur_fin["gross_total"],"scholarship_discount":cur_fin["scholarship_discount"],
        "net_assessment":cur_fin["net_assessment"],"total_tuition":cur_fin["total_tuition"],
        "total_paid":cur_fin["total_paid"],"balance":cur_fin["balance"],
        "payment_history":cur_fin["payment_history"],"payment_status":cur_fin["payment_status"],
        "minimum_payment":cur_fin["minimum_payment"],
        # ── Scholarship ──
        "scholarship":scholarship or {"name":"None","discount_pct":0,"grantor":"N/A","type":"N/A"},
        # ── Personal ──
        "birthday":birthday,"age":str(age),"birthplace":birthplace,"address":address,
        "gender":gender,"civil_status":civil_status,
        "religion":random.choice(religions),"nationality":"Filipino",
        "blood_type":random.choice(BLOOD_TYPES),
        "height_cm":str(random.randint(150,178)),"weight_kg":str(random.randint(48,85)),
        "phone_number":f"09{random.randint(100000000,999999999)}",
        # ── Family ──
        "father_name":father_name,"father_occupation":father_occ,"father_education":random.choice(ed_levels),
        "mother_name":mother_name,"mother_occupation":mother_occ,"mother_education":random.choice(ed_levels),
        "monthly_family_income":random.choice(incomes),"number_of_siblings":str(random.randint(0,6)),
        "emergency_contact_name":emergency_contact,"emergency_contact_phone":emergency_phone,
        "emergency_contact_relation":emergency_relation,
        # ── Health ──
        "health_record":health_record,
        # ── Disciplinary ──
        "disciplinary_record":disciplinary,
        # ── Awards ──
        "awards_honors":awards,
        # ── Organizations ──
        "organizations":orgs,
        # ── Previous school ──
        "previous_school":{
            "school_name":random.choice(hs_schools),"school_type":random.choice(["Public","Public","Private"]),
            "year_graduated":str(random.randint(2018,2024)),
            "general_average":f"{random.uniform(85.0,98.0):.2f}","track_strand":random.choice(tracks),
        },
    }


def create_table_if_not_exists():
    existing=[t.name for t in dynamodb.tables.all()]
    if TABLE_NAME in existing:
        print(f"Table '{TABLE_NAME}' already exists.")
        return dynamodb.Table(TABLE_NAME)
    print(f"Creating table '{TABLE_NAME}' ...")
    table=dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName":"student_id","KeyType":"HASH"}],
        AttributeDefinitions=[
            {"AttributeName":"student_id","AttributeType":"S"},
            {"AttributeName":"email","AttributeType":"S"},
            {"AttributeName":"student_number","AttributeType":"S"},
        ],
        GlobalSecondaryIndexes=[
            {"IndexName":"email-index","KeySchema":[{"AttributeName":"email","KeyType":"HASH"}],
             "Projection":{"ProjectionType":"ALL"}},
            {"IndexName":"student-number-index","KeySchema":[{"AttributeName":"student_number","KeyType":"HASH"}],
             "Projection":{"ProjectionType":"ALL"}},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.meta.client.get_waiter("table_exists").wait(TableName=TABLE_NAME)
    print(f"Table '{TABLE_NAME}' is ACTIVE.")
    return table


def seed_students(table):
    print(f"\nSeeding {len(DUMMY_STUDENTS)} students with full academic + financial history...\n")
    print(f"  {'Name':<28} {'Prog':<8} Yr  CurGPA  CumGPA  Scholarship")
    print(f"  {'-'*75}")
    for args in DUMMY_STUDENTS:
        record=build_student_record(*args)
        table.put_item(Item=record)
        schol=record["scholarship"]["name"][:20] if record["scholarship"]["name"]!="None" else "-"
        fn=record["full_name"]; pc=record["program_code"]
        yl=record["year_level"]; g=record["gpa"]; cg=record["cumulative_gpa"]
        hist_count=len(record["academic_history"])
        print(f"  OK {fn:<28} {pc:<8} Y{yl}  {g:<6}  {cg:<6}  {schol}  ({hist_count} past sems)")
    print(f"\nDone. {len(DUMMY_STUDENTS)} records written.")


if __name__ == "__main__":
    table=create_table_if_not_exists()
    seed_students(table)
    print("\nAll done!")