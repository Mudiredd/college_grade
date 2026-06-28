import requests
from bs4 import BeautifulSoup
import urllib3
import pdfplumber
import io

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT       = 60
BASE_URL      = "https://srbgnrexams.ac.in"
LOGIN_URL_1   = f"{BASE_URL}/Students/MarksMemo/Login.aspx"
LOGIN_URL_2   = f"{BASE_URL}/hdfc/Students/Login.aspx"
DASHBOARD_URL = f"{BASE_URL}/hdfc/Students/DashBoard.aspx"
MARKS_URL     = f"{BASE_URL}/hdfc/Students/MarksMemo/Login.aspx"

EXAM_NAMES = {
    1: "NOV-15", 2: "APR-16", 3: "NOV-16", 4: "APR-17", 5: "NOV-17",
    6: "APR-18", 7: "JULY-18", 8: "NOV-18", 9: "APR-19", 10: "JUN-19",
    11: "AUG-19", 12: "NOV-19", 13: "JAN-20", 14: "SEP-20", 15: "JAN-21",
    16: "DEC-20", 17: "JULY-21", 18: "AUG-21", 19: "SEP-21", 20: "OCT-21",
    21: "MAR-22", 22: "JULY-22", 23: "JUN-22", 24: "JAN-23", 25: "JUN-23",
    26: "OCT-23", 27: "JAN-24", 28: "MAY-24", 35: "JUNE-24", 36: "OCT-24",
    37: "NOV-24", 38: "APR-25", 39: "AUG-25", 40: "NOV-25", 41: "APR-26"
}

def get_exam_ids_for_student(regd_no):
    try:
        joining_year = int(regd_no[2:4])
    except:
        joining_year = 15  # fallback to earliest

    relevant_ids = []
    for exam_id, exam_name in EXAM_NAMES.items():
        year_str = exam_name.split('-')[-1]
        try:
            year = int(year_str)
            # Include from one year before joining (for NOV before first sem)
            if year >= joining_year - 1:
                relevant_ids.append(exam_id)
        except:
            pass
    return sorted(relevant_ids)

def _val(soup, name):
    el = soup.find("input", {"name": name})
    if el is None:
        raise ValueError(f"Field '{name}' not found on page — login may have failed or session expired")
    return el["value"]

def get_viewstate(session, url):
    response = session.get(url, verify=False, timeout=TIMEOUT)
    soup = BeautifulSoup(response.text, "html.parser")
    return {
        "__VIEWSTATE": _val(soup, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _val(soup, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": _val(soup, "__EVENTVALIDATION"),
    }

def login_step1(session, regd_no, password="0"):
    tokens = get_viewstate(session, LOGIN_URL_1)
    payload = {**tokens,
        "ctl00$ContentPlaceHolder1$txtRegdNo": regd_no,
        "ctl00$ContentPlaceHolder1$txtPassword": password,
        "ctl00$ContentPlaceHolder1$cmbSubmit": "Get Data"
    }
    return session.post(LOGIN_URL_1, data=payload, verify=False, timeout=TIMEOUT)

def login_step2(session, regd_no, password="0"):
    tokens = get_viewstate(session, LOGIN_URL_2)
    payload = {**tokens,
        "ctl00$ContentPlaceHolder1$txtRegdNo": regd_no,
        "ctl00$ContentPlaceHolder1$txtPassword": password,
        "ctl00$ContentPlaceHolder1$cmbSubmit": "Get Data"
    }
    return session.post(LOGIN_URL_2, data=payload, verify=False, timeout=TIMEOUT)

def click_marks_memo(session, dashboard_res):
    soup = BeautifulSoup(dashboard_res.text, "html.parser")
    payload = {
        "__VIEWSTATE": _val(soup, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _val(soup, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": _val(soup, "__EVENTVALIDATION"),
        "ctl00$body$navMM": "Marks Memo",
        "ctl00$body$hfStudId": _val(soup, "ctl00$body$hfStudId"),
        "ctl00$body$hfGroupId": _val(soup, "ctl00$body$hfGroupId"),
        "ctl00$body$hfBatchId": _val(soup, "ctl00$body$hfBatchId"),
        "ctl00$body$hfExamId": _val(soup, "ctl00$body$hfExamId"),
    }
    return session.post(DASHBOARD_URL, data=payload, verify=False, timeout=TIMEOUT)

def get_marks_pdf(session, marks_page_res, exam_id, semester):
    soup = BeautifulSoup(marks_page_res.text, "html.parser")
    payload = {
        "__VIEWSTATE": _val(soup, "__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": _val(soup, "__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": _val(soup, "__EVENTVALIDATION"),
        "ctl00$ContentPlaceHolder1$ddlExam": exam_id,
        "ctl00$ContentPlaceHolder1$ddlSemester": semester,
        "ctl00$ContentPlaceHolder1$hfStudId": _val(soup, "ctl00$ContentPlaceHolder1$hfStudId"),
        "ctl00$ContentPlaceHolder1$cmbSubmit": "Get Data"
    }
    return session.post(MARKS_URL, data=payload, verify=False, timeout=TIMEOUT)

def parse_pdf(pdf_bytes, exam_name, semester):
    subjects = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        tables = pdf.pages[0].extract_tables()
        if not tables:
            return None
        for table in tables:
            for row in table:
                if not row or len(row) < 5:
                    continue
                col1 = (row[1] or "").split("\n")
                col3 = (row[3] or "").split("\n")
                col4 = (row[4] or "").split("\n")
                if col1[0].strip() == "COURSE TITLE":
                    continue
                for i in range(len(col1)):
                    course = col1[i].strip() if i < len(col1) else ""
                    grade  = col3[i].strip() if i < len(col3) else ""
                    credit = col4[i].strip() if i < len(col4) else "0"
                    if len(course) > 3:
                        subjects.append({
                            "subject": course,
                            "grade": grade,
                            "credits": credit,
                            "status": "pass" if grade not in ["F", "ABS", ""] else "fail",
                            "exam": exam_name,
                            "semester": semester
                        })
    return subjects if subjects else None