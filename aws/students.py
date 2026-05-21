import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from config import AWS_REGION

dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
students_table = dynamodb.Table("StudentRecords")


def get_student_by_uid(uid):
    try:
        resp = students_table.get_item(Key={"student_id": uid})
        return resp.get("Item")
    except ClientError as e:
        print(f"[students] get_student_by_uid error: {e}")
        return None


def get_student_by_email(email):
    try:
        resp = students_table.query(
            IndexName="email-index",
            KeyConditionExpression=Key("email").eq(email),
        )
        items = resp.get("Items", [])
        return items[0] if items else None
    except ClientError:
        try:
            resp = students_table.scan(FilterExpression=Key("email").eq(email))
            items = resp.get("Items", [])
            return items[0] if items else None
        except ClientError as e2:
            print(f"[students] get_student_by_email error: {e2}")
            return None


def upsert_student(record):
    try:
        students_table.put_item(Item=record)
        return True
    except ClientError as e:
        print(f"[students] upsert_student error: {e}")
        return False


def list_all_students():
    try:
        resp = students_table.scan()
        return resp.get("Items", [])
    except ClientError as e:
        print(f"[students] list_all_students error: {e}")
        return []


def _php(val):
    try:
        return f"PHP {int(val):,}"
    except Exception:
        return f"PHP {val}"


def format_student_context(student):
    if not student:
        return ""
    try:
        L = []
        name   = student.get("full_name", "N/A")
        sno    = student.get("student_number", "N/A")
        prog   = student.get("program", "N/A")
        yr     = student.get("year_level", "N/A")
        sec    = student.get("section", "N/A")
        sem    = student.get("semester", "N/A")
        sy     = student.get("school_year", "N/A")
        gpa    = student.get("gpa", "N/A")
        cum    = student.get("cumulative_gpa") or gpa
        stand  = student.get("remarks", "N/A")

        L += [
            "<student_record>",
            "=" * 72,
            "STUDENT PERSONAL ACADEMIC RECORD — SAMAR COLLEGE",
            "=" * 72,
            f"Name              : {name}",
            f"Student No.       : {sno}",
            f"Program           : {prog}",
            f"Year Level        : {yr}",
            f"Section           : {sec}",
            f"Current Semester  : {sem} Sem, S.Y. {sy}",
            f"Student Status    : {student.get('student_status','Regular')}",
            f"Enrollment Status : {student.get('enrollment_status','Enrolled')}",
            f"Current Sem GPA   : {gpa}",
            f"Cumulative GPA    : {cum}",
            f"Academic Standing : {stand}",
            "",
        ]

        # Personal
        L += [
            "── PERSONAL INFORMATION ──────────────────────────────────────────",
            f"  Birthday          : {student.get('birthday','N/A')}",
            f"  Age               : {student.get('age','N/A')}",
            f"  Birthplace        : {student.get('birthplace','N/A')}",
            f"  Address           : {student.get('address','N/A')}",
            f"  Gender            : {student.get('gender','N/A')}",
            f"  Civil Status      : {student.get('civil_status','N/A')}",
            f"  Religion          : {student.get('religion','N/A')}",
            f"  Nationality       : {student.get('nationality','Filipino')}",
            f"  Blood Type        : {student.get('blood_type','N/A')}",
            f"  Height            : {student.get('height_cm','N/A')} cm",
            f"  Weight            : {student.get('weight_kg','N/A')} kg",
            f"  Phone             : {student.get('phone_number','N/A')}",
            f"  Email             : {student.get('email','N/A')}",
            "",
        ]

        # Family
        L += [
            "── FAMILY BACKGROUND ─────────────────────────────────────────────",
            f"  Father     : {student.get('father_name','N/A')} — {student.get('father_occupation','N/A')} ({student.get('father_education','N/A')})",
            f"  Mother     : {student.get('mother_name','N/A')} — {student.get('mother_occupation','N/A')} ({student.get('mother_education','N/A')})",
            f"  Monthly Family Income : {student.get('monthly_family_income','N/A')}",
            f"  No. of Siblings       : {student.get('number_of_siblings','N/A')}",
            f"  Emergency Contact     : {student.get('emergency_contact_name','N/A')} ({student.get('emergency_contact_relation','N/A')}) — {student.get('emergency_contact_phone','N/A')}",
            "",
        ]

        # Previous school
        ps = student.get("previous_school", {})
        if ps:
            L += [
                "── PREVIOUS SCHOOL ───────────────────────────────────────────────",
                f"  School      : {ps.get('school_name','N/A')} ({ps.get('school_type','N/A')})",
                f"  Graduated   : {ps.get('year_graduated','N/A')}",
                f"  Gen Average : {ps.get('general_average','N/A')}",
                f"  Track/Strand: {ps.get('track_strand','N/A')}",
                "",
            ]

        # Scholarship
        sch = student.get("scholarship", {})
        if sch and sch.get("name","None") != "None":
            L += [
                "── SCHOLARSHIP / FINANCIAL ASSISTANCE ────────────────────────────",
                f"  Name     : {sch.get('name','N/A')}",
                f"  Type     : {sch.get('type','N/A')}",
                f"  Grantor  : {sch.get('grantor','N/A')}",
                f"  Discount : {sch.get('discount_pct',0)}% off total assessment per semester",
                "",
            ]
        else:
            L += ["── SCHOLARSHIP : None", ""]

        # Health
        hr = student.get("health_record", {})
        if hr:
            vax = ", ".join(hr.get("vaccinations", [])) or "N/A"
            L += [
                "── HEALTH RECORD ─────────────────────────────────────────────────",
                f"  Medical Conditions : {hr.get('medical_conditions','None')}",
                f"  Allergies          : {hr.get('allergies','None')}",
                f"  Blood Pressure     : {hr.get('blood_pressure','N/A')}",
                f"  Vision             : {hr.get('vision','N/A')}",
                f"  Last Physical Exam : {hr.get('last_physical_exam','N/A')}",
                f"  Vaccinations       : {vax}",
                "",
            ]

        # Disciplinary
        dr = student.get("disciplinary_record", {})
        L.append("── DISCIPLINARY RECORD ───────────────────────────────────────────")
        if dr and dr.get("violations"):
            for v in dr["violations"]:
                L.append(f"  {v.get('date','N/A')} | {v.get('offense','N/A')} | Sanction: {v.get('sanction','N/A')} | Resolved: {'Yes' if v.get('resolved') else 'No'}")
        else:
            L.append(f"  {dr.get('status','No disciplinary record on file.')}")
        L.append("")

        # Awards
        awards = student.get("awards_honors", [])
        L.append("── AWARDS & HONORS ───────────────────────────────────────────────")
        if awards:
            for a in awards:
                L.append(f"  {a.get('award','N/A')} — {a.get('semester','')} S.Y. {a.get('school_year','')}")
        else:
            L.append("  No awards on record.")
        L.append("")

        # Organizations
        orgs = student.get("organizations", [])
        L.append("── ORGANIZATION MEMBERSHIPS ──────────────────────────────────────")
        if orgs:
            for o in orgs:
                L.append(f"  {o.get('name','N/A')} | {o.get('position','Member')} | Since {o.get('year_joined','N/A')}")
        else:
            L.append("  No organization membership on record.")
        L.append("")

        # Current enrollment
        enrolled = student.get("enrolled_subjects", [])
        if enrolled:
            L.append(f"── CURRENTLY ENROLLED — {sem} Sem S.Y. {sy} ──────────────────────────")
            L.append(f"  {'Code':<10} {'Subject':<40} {'Units':<6} {'Schedule':<22} {'Room':<10} Instructor")
            L.append("  " + "-"*112)
            for s in enrolled:
                L.append(
                    f"  {s.get('subject_code',''):<10} {s.get('subject_name',''):<40} "
                    f"{s.get('units',''):<6} {s.get('schedule',''):<22} "
                    f"{s.get('room',''):<10} {s.get('instructor','')}"
                )
            L.append("")

        # Current semester grades
        grades = student.get("grades", [])
        if grades:
            L.append(f"── GRADES — {sem} Sem S.Y. {sy} ──────────────────────────────────────")
            L.append(f"  {'Code':<10} {'Subject':<38} {'Units':<6} {'Prelim':<8} {'Midterm':<9} {'SemiFinal':<11} {'Final':<7} {'Grade':<7} Remarks")
            L.append("  " + "-"*108)
            for g in grades:
                L.append(
                    f"  {g.get('subject_code',''):<10} {g.get('subject_name',''):<38} "
                    f"{str(g.get('units','')):<6} {str(g.get('prelim','')):<8} "
                    f"{str(g.get('midterm','')):<9} {str(g.get('semi_final','')):<11} "
                    f"{str(g.get('final','')):<7} {str(g.get('final_grade','')):<7} "
                    f"{g.get('remarks','')}"
                )
            L.append("")

        # Current semester financials
        if student.get("total_tuition"):
            L.append(f"── TUITION ASSESSMENT — {sem} Sem S.Y. {sy} (CURRENT) ─────────────────")
            L.append(f"  {'Subject Code':<12} {'Subject Name':<42} Fee")
            L.append("  " + "-"*65)
            for item in student.get("tuition_breakdown", []):
                L.append(f"  {item.get('subject_code',''):<12} {item.get('subject_name',''):<42} {_php(item.get('fee',0))}")
            L.append("")
            L.append("  Miscellaneous Fees:")
            for k,v in student.get("misc_fees", {}).items():
                L.append(f"    {k:<30} {_php(v)}")
            L.append("")
            L += [
                f"  Gross Assessment     : {_php(student.get('gross_total', student.get('total_tuition',0)))}",
                f"  Scholarship Discount : {_php(student.get('scholarship_discount',0))}",
                f"  NET ASSESSMENT       : {_php(student.get('net_assessment', student.get('total_tuition',0)))}",
                f"  Total Paid           : {_php(student.get('total_paid',0))}",
                f"  BALANCE DUE          : {_php(student.get('balance',0))}",
                f"  Payment Status       : {student.get('payment_status','N/A')}",
                f"  Minimum Payment      : {_php(student.get('minimum_payment',1500))}",
                "",
            ]
            for p in student.get("payment_history", []):
                if p == student.get("payment_history", [])[0]:
                    L.append("  Payment Transactions:")
                L.append(f"    {p.get('date',''):<12} {_php(p.get('amount',0)):<16} {p.get('payment_method',''):<16} {p.get('reference_no','')}")
            if student.get("payment_history"):
                L.append("")

        # Per-year financial summaries (completed years)
        summaries = student.get("school_year_summaries", [])
        if summaries:
            L.append("── ANNUAL FINANCIAL SUMMARY (COMPLETED SCHOOL YEARS) ────────────")
            L.append(f"  {'School Year':<14} {'Year GPA':<10} {'Gross':<16} {'Discount':<14} {'Net':<16} {'Paid':<16} {'Balance':<12} Status")
            L.append("  " + "-"*110)
            for s in summaries:
                L.append(
                    f"  {s.get('school_year',''):<14} "
                    f"{s.get('year_gpa',''):<10} "
                    f"{_php(s.get('annual_gross',0)):<16} "
                    f"{_php(s.get('annual_discount',0)):<14} "
                    f"{_php(s.get('annual_net',0)):<16} "
                    f"{_php(s.get('annual_total_paid',0)):<16} "
                    f"{_php(s.get('annual_balance',0)):<12} "
                    f"{s.get('annual_payment_status','')}"
                )
            L.append("")

        # Full academic history with grades and financials per semester
        history = student.get("academic_history", [])
        if history:
            L.append("── COMPLETE ACADEMIC HISTORY — ALL PREVIOUS SEMESTERS ────────────")
            L.append("=" * 72)

            # Group into school years for cleaner display
            sy_groups = {}
            for h in history:
                key = h.get("school_year","")
                if key not in sy_groups:
                    sy_groups[key] = []
                sy_groups[key].append(h)

            for school_yr in sorted(sy_groups.keys()):
                sems = sy_groups[school_yr]
                yr_num = sems[0].get("year_level","?") if sems else "?"
                L.append(f"")
                L.append(f"  YEAR {yr_num} — S.Y. {school_yr}")
                L.append("  " + "=" * 68)

                for h in sorted(sems, key=lambda x: x.get("semester","")):
                    hs  = h.get("semester","?")
                    hgpa= h.get("gpa","N/A")
                    L.append(f"")
                    L.append(f"  {hs} Semester — GPA: {hgpa}")
                    L.append(f"  {'Code':<10} {'Subject':<38} {'Units':<6} {'Prelim':<8} {'Midterm':<9} {'SemiFinal':<11} {'Final':<7} {'Grade':<7} Remarks")
                    L.append("  " + "-"*108)
                    for g in h.get("grades", []):
                        L.append(
                            f"  {g.get('subject_code',''):<10} {g.get('subject_name',''):<38} "
                            f"{str(g.get('units','')):<6} {str(g.get('prelim','')):<8} "
                            f"{str(g.get('midterm','')):<9} {str(g.get('semi_final','')):<11} "
                            f"{str(g.get('final','')):<7} {str(g.get('final_grade','')):<7} "
                            f"{g.get('remarks','')}"
                        )
                    # Semester financials
                    h_gross   = h.get("gross_total","N/A")
                    h_disc    = h.get("scholarship_discount","0")
                    h_net     = h.get("net_assessment","N/A")
                    h_paid    = h.get("total_paid","N/A")
                    h_bal     = h.get("balance","N/A")
                    h_stat    = h.get("payment_status","N/A")
                    L += [
                        "",
                        f"  Financial Record — {hs} Sem S.Y. {school_yr}:",
                        f"    Gross Assessment     : {_php(h_gross)}",
                        f"    Scholarship Discount : {_php(h_disc)}",
                        f"    Net Assessment       : {_php(h_net)}",
                        f"    Total Paid           : {_php(h_paid)}",
                        f"    Balance              : {_php(h_bal)}",
                        f"    Payment Status       : {h_stat}",
                    ]
                    pays = h.get("payment_history", [])
                    if pays:
                        L.append(f"    Payment Transactions ({len(pays)}):")
                        for p in pays:
                            L.append(f"      {p.get('date',''):<12} {_php(p.get('amount',0)):<16} {p.get('payment_method',''):<16} OR#{p.get('reference_no','')}")
                    L.append("  " + "-"*70)

            L.append("")
            L.append("=" * 72)
            L.append("")

        L.append("</student_record>")
        return "\n".join(L)

    except Exception as e:
        import traceback
        print(f"[format_student_context] Error: {e}\n{traceback.format_exc()}")
        n=student.get("full_name","N/A"); g=student.get("gpa","N/A"); b=student.get("balance","N/A")
        return f"<student_record>\nName: {n}\nGPA: {g}\nBalance: PHP {b}\n</student_record>"