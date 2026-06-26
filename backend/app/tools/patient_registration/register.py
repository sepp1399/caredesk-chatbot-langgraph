"""Patient registration.

Pre-validates each field locally (codice fiscale, email regex, date format)
so the agent gets actionable feedback BEFORE hitting the backend. Local
errors return `status='error'` with a precise `code` so the agent can
re-prompt on the offending field only, not the whole form.

`bypass_duplicates=true` is sent so duplicate users are accepted and merged
downstream by the call center.
"""

import re
from datetime import datetime

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from app.integrations.caredesk import cd_post
from app.tools.shared._helpers import _err, _log, _ok, is_valid_codice_fiscale

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class _RegisterIn(BaseModel):
    name:           str = Field(description="Patient's first name.")
    surname:        str = Field(description="Patient's family name.")
    codice_fiscale: str = Field(description="Italian Codice Fiscale (16 alphanumeric chars).")
    birthdate:      str = Field(description="Date of birth DD/MM/YYYY.")
    phone:          str = Field(description="Mobile phone, digits only (country prefix optional).")
    email:          str = Field(description="Patient's email.")
    privacy_accepted: bool = Field(
        description="Must be True — read the privacy notice and confirm before calling.",
    )


@tool(args_schema=_RegisterIn)
async def register_patient(
    name: str,
    surname: str,
    codice_fiscale: str,
    birthdate: str,
    phone: str,
    email: str,
    privacy_accepted: bool,
) -> str:
    """
    Register a new patient.

    GUARD: only call after the agent has read back the full summary and
    received explicit confirmation in the current turn. The tool returns
    `status='error'` with a precise `code` (e.g. `invalid_cf`, `invalid_email`)
    so you can re-prompt the patient on the offending field WITHOUT
    re-asking everything.

    On success, returns the new `userid` — the agent should pass it to the
    originating flow (typically lab_booking) for the booking that triggered
    registration.
    """
    inputs = {"name": name, "surname": surname, "codice_fiscale": codice_fiscale,
              "birthdate": birthdate, "phone": phone, "email": email,
              "privacy_accepted": privacy_accepted}

    if not privacy_accepted:
        return _log("register_patient", inputs,
                    _err("privacy_not_accepted",
                         "Privacy notice not accepted — cannot register."))

    cf = (codice_fiscale or "").upper().strip()
    if not is_valid_codice_fiscale(cf):
        return _log("register_patient", inputs,
                    _err("invalid_cf", "Codice fiscale format invalid — re-ask."))

    try:
        datetime.strptime(birthdate, "%d/%m/%Y")
    except ValueError:
        return _log("register_patient", inputs,
                    _err("invalid_birthdate", "Use DD/MM/YYYY."))

    if not _EMAIL_RE.match(email or ""):
        return _log("register_patient", inputs,
                    _err("invalid_email", "Email format invalid — re-ask."))

    phone_digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(phone_digits) < 8:
        return _log("register_patient", inputs,
                    _err("invalid_phone", "Phone too short — re-ask."))

    # Registration body uses the backend's short field names
    # (fname / lname / idnumber / birthday).
    body = {
        "fname":      name.strip(),
        "lname":      surname.strip(),
        "idnumber":   cf,
        "birthday":   birthdate,
        "phone":      phone_digits,
        "email":      email.strip(),
        "privacy":    1,
        "bypass_duplicates": True,
    }
    try:
        data = await cd_post("/users", body)
    except Exception as exc:
        return _log("register_patient", inputs, _err("backend_error", str(exc)))

    return _log("register_patient", inputs,
                _ok(userid=data.get("userid"),
                    name=name, surname=surname))
