"""Exact BSIT subject labels from the seed lists, not verified current curricula.

Static data only: importing the AWS-connected seeder would have side effects.
Student identities, enrollment assignments, grades and finances remain synthetic.
"""

PROGRAM_NAME = "Bachelor of Science in Information Technology"
PROGRAM_CODE = "BSIT"
CATALOG_SOURCE = "tools/seed_students.py"

# Preserve source punctuation and repeated courses across different semesters.
BSIT_SUBJECTS = {
    (1, 1): (
        ("PE - 1", "PATHFiT 1", 2),
        ("English 01", "Basic English", 3),
        ("GE 1", "Understanding Self", 3),
        ("IT_102", "Discrete Mathematics", 3),
        ("Math 01", "Basic Mathematics", 3),
        ("NSTP 1", "National Service Training Program", 3),
        ("GE 2", "Reading in Phil History", 3),
        ("IT-101", "Intro to Computing", 3),
        ("IT 103", "Computer Programming 1", 3),
        ("GE Elec1 Fil 1", "Komunikasyon sa Akademikong Filipino", 3),
    ),
    (1, 2): (
        ("IT 104", "Computer Programming II", 3),
        ("English 02", "English Fundamentals", 3),
        ("GE Elec Fil 2", "Pagbasa at Pagsulat Tungo sa Pananaliksik", 3),
        ("IT 105", "Introduction to Human Computer Interaction", 3),
        ("Math 02", "College Algebra", 3),
        ("NSTP 2", "National Service Training Program", 2),
        ("GE 3", "The Contemporary World", 3),
        ("GE 4", "Mathematics in the Modern World", 3),
        ("IT 106", "Platform Technologies", 3),
        ("PE -2", "Physical Activities Toward Health & Fitness 2", 2),
    ),
    (2, 1): (
        ("IT 202", "Object-Oriented Programming", 3),
        ("IT 201", "Data Structure & Algorithms", 3),
        ("IT 204", "Human Computer Interaction II", 3),
        ("PE - 3", "Physical Activities Toward Health & Fitness 3", 2),
        ("Research201", "Methods of Research in IT", 3),
        ("CAS GE 5", "Purposive Communication", 3),
        ("CAS GE 6", "Art Appreciation", 3),
        ("IT 203", "Computer Programming III", 3),
    ),
    (2, 2): (
        ("I.T. 207", "Integrative Programming & Technologies I", 3),
        ("PE-4", "(PATHFit 4): Sports, Outdoor and Adventure Activities", 2),
        ("CAS GE 8", "Ethics (with topic on Obligation to Pay Taxes)", 3),
        ("CAS GE 9", "Life & Works of Rizal", 3),
        ("I.T. 206", "Networking I", 3),
        ("IT 205", "Fundamentals of Database System", 3),
        ("CAS GE 7", "Science, Technology and Society", 3),
        ("I.T. 208", "Web System and Technologies I", 3),
    ),
    (3, 1): (
        ("CAS GE 11", "Gender and Society", 3),
        ("I.T.302", "Advanced Database System", 3),
        ("IT 305", "Integrative Programming & Technologies II", 3),
        ("CAS GE 10", "Entrepreneurial Mind", 3),
        ("IT 304", "System Integration & Architecture I", 3),
        ("IT 306", "Web System & Technologies II", 3),
        ("CAS GE 12", "Philippine Indigenous Communities", 3),
        ("I.T.303", "Mobile Application Development", 3),
    ),
    (3, 2): (
        ("IT 307", "System Integration & Architecture II", 3),
        ("IT 313", "Basic Linux Installation and Configuration", 3),
        ("IT 314", "Capstone Project and Research 1", 3),
        ("IT 310", "Application Development & Emerging Technologies", 3),
        ("IT 312", "Technopreneurship", 3),
        ("IT-311", "Information Management", 3),
        ("IT 308", "Event-Driven Programming", 3),
        ("IT 309", "Information Assurance and Security I", 3),
        ("Math 02", "College Algebra", 3),
    ),
}
