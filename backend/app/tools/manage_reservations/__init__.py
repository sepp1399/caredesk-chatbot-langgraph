from app.tools.manage_reservations.list_reservations  import list_my_reservations
from app.tools.manage_reservations.cancel              import cancel_reservation
from app.tools.manage_reservations.reschedule          import reschedule_reservation
from app.tools.shared.auth                             import (
    authenticate_user_by_phone,
    authenticate_user_by_codice_fiscale,
    authenticate_user_by_birthdate,
)

# Manage-reservations tools + the identity cascade (needed when the
# router's pre-flight phone auth was ambiguous / not_found and the agent
# has to resolve identification before listing reservations). The agent
# also reuses search_dates / get_new_dates from lab_booking for the
# reschedule subflow — wired in the agent module.
MANAGE_RESERVATIONS_TOOLS = [
    authenticate_user_by_phone,
    authenticate_user_by_codice_fiscale,
    authenticate_user_by_birthdate,
    list_my_reservations,
    cancel_reservation,
    reschedule_reservation,
]
