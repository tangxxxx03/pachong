from datetime import datetime, timedelta

def in_last_days(date_str, days=7):
    """
    date_str: YYYY-MM-DD
    """
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return False

    today = datetime.today().date()
    return today - timedelta(days=days) <= d <= today
