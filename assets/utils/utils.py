from datetime import datetime
import zoneinfo

def get_current_time():
    """Get current time in configured timezone."""
    from bot import config  # Import here to avoid circular import
    return datetime.now(zoneinfo.ZoneInfo(config.timezone))

def convert_to_local(dt: datetime) -> datetime:
    """Convert UTC datetime to configured timezone."""
    from bot import config  # Import here to avoid circular import
    if dt.tzinfo is None:  # If datetime is naive, assume it's UTC
        dt = dt.replace(tzinfo=zoneinfo.ZoneInfo('UTC'))
    return dt.astimezone(zoneinfo.ZoneInfo(config.timezone))

def convert_to_utc(dt: datetime) -> datetime:
    """Convert local datetime to UTC."""
    from bot import config  # Import here to avoid circular import
    if dt.tzinfo is None:  # If datetime is naive, assume it's in configured timezone
        dt = dt.replace(tzinfo=zoneinfo.ZoneInfo(config.timezone))
    return dt.astimezone(zoneinfo.ZoneInfo('UTC')) 