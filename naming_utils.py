from datetime import datetime

MONTHS_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"
}

def get_newspaper_name(msg_date: datetime.date) -> str:
    day = msg_date.day
    month_name = MONTHS_ES[msg_date.month]
    return f"La Provincia, {day} de {month_name}.pdf"