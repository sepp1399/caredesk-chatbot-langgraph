"""Lab-booking flow tools (insurance, doctor, service, area, availability, booking)."""

from app.tools.lab_booking.insurance     import (
    get_insurance_id_by_insurance_name,
    search_insurance_names,
    _fetch_insurance_map,
)
from app.tools.lab_booking.doctor        import (
    search_doctor_names,
    _fetch_doctor_map,
)
from app.tools.lab_booking.service       import search_available_services, _fetch_activities
from app.tools.lab_booking.area          import search_areas
from app.tools.lab_booking.availability  import get_new_dates, search_dates, SLOT_CACHE
from app.tools.lab_booking.booking       import book_appointment, request_deferred_appointment
from app.tools.shared.auth               import (
    authenticate_user_by_phone,
    authenticate_user_by_codice_fiscale,
    authenticate_user_by_birthdate,
)

# Tool list exposed to lab_booking agent. KB + transfer are appended inside
# the agent module so this stays focused on booking + identification tools.
LAB_BOOKING_TOOLS = [
    # Identity cascade (used mid-flow when the router's pre-flight phone
    # auth returned ambiguous / not_found / invalid_phone):
    authenticate_user_by_phone,
    authenticate_user_by_codice_fiscale,
    authenticate_user_by_birthdate,
    # Booking funnel:
    search_insurance_names,
    get_insurance_id_by_insurance_name,
    # search_doctor_names is dual-use: list mode (no commit) at phase 2
    # entry via pre_model_hook, and commit mode (commit=true) when the
    # patient has picked a doctor.
    search_doctor_names,
    search_available_services,
    search_areas,
    search_dates,
    get_new_dates,
    book_appointment,
    request_deferred_appointment,
]
