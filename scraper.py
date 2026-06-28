import requests
from bs4 import BeautifulSoup
import urllib3
import pdfplumber
import io

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT       = 60
BASE_URL      = "https://srbgnrexams.ac.in"
LOGIN_URL_2   = f"{BASE_URL}/hdfc/Students/Login.aspx"
DASHBOARD_URL = f"{BASE_URL}/hdfc/Students/DashBoard.aspx"
MARKS_URL     = f"{BASE_URL}/hdfc/Students/MarksMemo/Login.aspx"

def scrape_exam_options(session, marks_page_res):
    soup = BeautifulSoup(marks_page_res.text, "html.parser")
    select = soup.find("select", {"name": "ctl00$ContentPlaceHolder1$ddlExam"})
    if select is None:
        raise ValueError("Exam dropdown not found on marks page")
    exam_names = {}
    for option in select.find_all("option"):
        value = option.get("value", "").strip()
        text = option.get_text(strip=True)
        if value and value.isdigit():
            exam_names[int(value)] = text
    if not exam_names:
        raise ValueError("No exam options found in dropdown")
    return exam_names

def get_exam_ids_for_student(regd_no, exam_names):
    try:
        joining_year = int(regd_no[2:4])
    except:
        joining_year = 15  # fallback to earliest

    relevant_ids = []
    for exam_id, exam_name in exam_names.items():
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
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
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